# -*- coding: utf-8 -*-
"""从 Wikimedia Commons 构建「猫/狗 × 卧姿/非卧姿」图片数据集。

- 来源：Wikimedia Commons 的语义分类（Sleeping cats / Sitting dogs 等），全部为开放授权
- 每类 100 张，1600px 级高清，统一转存 JPEG
- 记录每张的来源页、作者、授权、原始尺寸，输出 CSV / JSON 清单
- 支持断点续传：已下载的文件会保留，重跑只补齐缺的
运行：python dataset_builder.py
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image, ImageOps

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.abspath(os.environ.get("IMS_OUT") or
                           os.path.join(os.path.dirname(BASE), "dataset"))
API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"}

# 直连 + 不校验 MITM 证书：本机 WorkBuddy 注入的代理会让 Commons 握手超时
_CTX = ssl._create_unverified_context()
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_CTX))

TARGET_PER_CLASS = int(os.environ.get("IMS_TARGET", "100"))
MIN_W, MIN_H = 1200, 800       # 原图最小尺寸，保证清晰度
THUMB_W = 1600                  # 请求的高清缩略图宽度
WORKERS = 6
BATCH_INFO = 40

CLASSES = {
    "猫-卧姿": ["Sleeping cats", "Lying cats", "Cats sleeping", "Sleeping kittens"],
    "猫-非卧姿": ["Sitting cats", "Standing cats", "Walking cats", "Playing cats", "Running cats"],
    "狗-卧姿": ["Sleeping dogs", "Lying dogs", "Dogs sleeping", "Sleeping puppies",
              "Resting dogs", "Sleepy dogs", "Dogs lying", "Dogs resting",
              "Puppies sleeping", "Sleeping hounds", "Dogs on beds", "Dog beds"],
    "狗-非卧姿": ["Sitting dogs", "Standing dogs", "Walking dogs", "Running dogs", "Playing dogs"],
}
# 候选枯竭的类改用全文搜索扩源（namespace=6），关键词覆盖中英文/多姿态
SEARCHES = {
    "狗-卧姿": ["sleeping dog", "dog lying down", "dog asleep", "resting dog", "sleepy dog",
              "dog on bed", "puppy sleeping", "躺着的狗", "睡觉的狗", "狗狗趴着"],
    "狗-非卧姿": ["sitting dog", "standing dog", "dog walking", "running dog", "playing dog",
               "dog stretching", "jumping dog", "dog outdoors", "狗站着", "狗走路", "狗狗玩耍"],
}
POSE = {"猫-卧姿": "卧姿", "猫-非卧姿": "非卧姿",
        "狗-卧姿": "卧姿", "狗-非卧姿": "非卧姿"}
ANIMAL = {"猫-卧姿": "猫", "猫-非卧姿": "猫",
          "狗-卧姿": "狗", "狗-非卧姿": "狗"}

BAD_NAME = re.compile(
    r"\b(logo|icon|diagram|chart|chart|map|coat of arms|stamp|poster|drawing|sketch|"
    r"painting|sculpture|cartoon|clipart|graphic|illustration|book cover|sign|flag|"
    r"tattoo|meme|engraving|woodcut|silhouette|emoji|plush|toy|statue|"
    r"etching|lithograph|vintage|artwork|art print|fine art)\b|"
    r"蚀刻|版画|素描|插画|绘画|画作|水墨|油画|卡通画", re.I)
OK_MIME = {"image/jpeg", "image/png", "image/webp"}
# Commons 分类里混有拍摄环境照 / 误归类文件；要求文件名或图片描述含对应动物词
ANIMAL_NAME = {
    "猫": re.compile(r"cat|kitten|kitty|feline|katze|gato|chat|gatto|kucing|kot\b|猫|ねこ", re.I),
    "狗": re.compile(r"dog|puppy|hund|perro|chien|cane|canine|koira|狗|いぬ", re.I),
}
# 姿态语义过滤：Commons 分类里混有大量姿态相反的图片
LYING_WORD = re.compile(r"sleep|lying|lie\s?down|laid|nap|asleep|doz|rest|curl|schlaf|liegend|snooz|dorm", re.I)
UPRIGHT_WORD = re.compile(r"sitt|stand|walk|run|jump|play|stretch|climb|perch|upright|sitzen|steh|lauf", re.I)
# 用户要求：画面不带人、主体是猫狗、尽量环境全景而非近距特写
HUMAN_WORD = re.compile(
    r"\b(person|people|human|man|woman|child|boy|girl|hand|hands|owner|holding|held|"
    r"petting|hug|hugging|caress|girl|boy|kid|baby|人|手|主人|抱)\b", re.I)
# 软过滤（IMS_SOFT_HUMAN=1）：只拦强人信号，放宽 kid/baby/owner 等易误杀词，
# 用于候选极度匮乏时放宽召回，事后仍须肉眼复核剔除真实带人图。
SOFT_HUMAN_WORD = re.compile(
    r"\b(person|people|human|man|woman|holding|held|hug|hugging|petting|主人|抱)\b|人|手", re.I)
USE_SOFT_HUMAN = os.environ.get("IMS_SOFT_HUMAN", "0") == "1"
CLOSEUP_WORD = re.compile(r"portrait|close.?up|closeup|face|head\s?shot|muzzle|eyes|nose|paw|detail", re.I)
WIDE_WORD = re.compile(r"room|floor|garden|yard|house|bed|sofa|couch|street|park|grass|"
                       r"ground|from above|wide|surveillance|camera|monitor|balcony|terrace", re.I)
# 补齐轮放宽辅助条件（姿态词/特写词），但「不带人」始终严格排除
RELAX_POSE = os.environ.get("IMS_RELAX_POSE", "0") == "1"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(params: dict, tries: int = 4) -> dict:
    url = API + "?" + urllib.parse.urlencode({"format": "json", "formatversion": "2", **params})
    last = None
    for i in range(tries):
        try:
            with OPENER.open(urllib.request.Request(url, headers=UA), timeout=16) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if i < tries - 1:
                # Commons 限流 429 需要更长的退避
                time.sleep(3.0 * (i + 1) if getattr(exc, "code", None) == 429
                           else 0.6 * (i + 1))
    raise RuntimeError(f"API 请求失败（重试 {tries} 次）：{last}")


def collect_titles(category: str, limit: int = 500) -> list[str]:
    titles, cont = [], {}
    while len(titles) < limit:
        p = {"action": "query", "list": "categorymembers",
             "cmtitle": "Category:" + category, "cmtype": "file",
             "cmlimit": min(500, limit - len(titles)), **cont}
        d = api(p)
        titles += [m["title"] for m in d.get("query", {}).get("categorymembers", [])]
        nxt = d.get("continue")
        if not nxt:
            break
        cont = nxt
    return titles


def collect_search(query: str, limit: int = 200) -> list[str]:
    """Commons 全文搜索（namespace=6 文件），可挖到分类之外的候选。"""
    titles, cont = [], {}
    while len(titles) < limit:
        p = {"action": "query", "list": "search", "srsearch": query,
             "srnamespace": 6, "srlimit": min(50, limit - len(titles)), **cont}
        d = api(p)
        for r in d.get("query", {}).get("search", []):
            titles.append(r["title"])
        nxt = d.get("continue")
        if not nxt:
            break
        cont = nxt
    return titles


def interleave(lists: list[list[str]]) -> list[str]:
    """各分类轮流取，避免整批都来自同一个分类。"""
    out, i = [], 0
    while any(i < len(x) for x in lists):
        for x in lists:
            if i < len(x):
                out.append(x[i])
        i += 1
    return out


def _info_chunk(chunk: list[str], animal: str = "", pose: str = "") -> list[dict]:
    try:
        d = api({"action": "query", "titles": "|".join(chunk), "prop": "imageinfo",
                 "iiprop": "url|size|mime|extmetadata", "iiurlwidth": THUMB_W})
    except Exception as exc:
        log(f"  imageinfo 批次失败：{exc}")
        return []
    out = []
    for page in d.get("query", {}).get("pages", []):
        ii = (page.get("imageinfo") or [{}])[0]
        if not ii:
            continue
        em = ii.get("extmetadata", {}) or {}
        name = page.get("title", "")
        if ii.get("mime") not in OK_MIME:
            continue
        if (ii.get("width") or 0) < MIN_W or (ii.get("height") or 0) < MIN_H:
            continue
        desc = html.unescape(re.sub(r"<[^>]+>", "", em.get("ImageDescription", {}).get("value", "")))
        if BAD_NAME.search(name) or BAD_NAME.search(desc):
            continue
        if animal and animal in ANIMAL_NAME:
            if not ANIMAL_NAME[animal].search(name) and not ANIMAL_NAME[animal].search(desc):
                continue
        text = name + " " + desc
        # 「不带人」是硬条件，任何模式都排除；其余辅助过滤在补齐轮可放宽
        human_re = SOFT_HUMAN_WORD if USE_SOFT_HUMAN else HUMAN_WORD
        if human_re.search(text):
            continue
        if not RELAX_POSE:
            if CLOSEUP_WORD.search(text):
                continue
            if pose:
                if pose == "卧姿":
                    if not LYING_WORD.search(text):
                        continue
                elif not UPRIGHT_WORD.search(text):
                    continue
        url = ii.get("thumburl") or ii.get("url")
        if not url:
            continue
        lic = em.get("LicenseShortName", {}).get("value", "") or "未标注"
        if re.search(r"fair use|non.free|all rights reserved", lic, re.I):
            continue
        artist = html.unescape(re.sub(r"<[^>]+>", "", em.get("Artist", {}).get("value", "")))
        out.append({
            "title": name, "url": url,
            "page": ii.get("descriptionurl", ""),
            "license": lic.strip(),
            "license_url": em.get("LicenseUrl", {}).get("value", ""),
            "artist": re.sub(r"\s+", " ", artist).strip()[:150],
            "orig_w": ii.get("width", 0), "orig_h": ii.get("height", 0),
            "category": "", "wide": bool(WIDE_WORD.search(text)),
        })
    return out


def fetch_info(titles: list[str], animal: str = "", pose: str = "") -> list[dict]:
    """并发查询若干批 imageinfo，减少偶发网络延迟的累积。"""
    chunks = [titles[k:k + BATCH_INFO] for k in range(0, len(titles), BATCH_INFO)]
    out = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_info_chunk, c, animal, pose) for c in chunks]
        for fut in as_completed(futs):
            try:
                out.extend(fut.result())
            except Exception as exc:
                log(f"  imageinfo 线程异常：{exc}")
    # 含 room/floor/garden 等环境词的候选优先下载，更接近监控视角
    out.sort(key=lambda r: not r.get("wide", False))
    return out


def fetch_image(rec: dict) -> tuple[bool, bytes, tuple[int, int], str]:
    """下载并统一转成高质量 JPEG；返回 (成功, jpeg字节, 尺寸, 错误)。"""
    last = ""
    for i in range(3):
        try:
            with OPENER.open(urllib.request.Request(rec["url"], headers=UA), timeout=35) as r:
                raw = r.read()
            if len(raw) < 8000:
                raise ValueError("文件过小，疑似占位图")
            im = Image.open(io.BytesIO(raw))
            im.load()
            im = ImageOps.exif_transpose(im)
            if im.width < 900 or im.height < 600:
                raise ValueError(f"实际尺寸过小 {im.size}")
            # V4 硬要求：原始分辨率≥1280×720 且 全部横屏(宽>高)
            if im.width < 1280 or im.height < 720:
                raise ValueError(f"分辨率不足1280x720 {im.size}")
            if im.width <= im.height:
                raise ValueError("非横屏(宽<=高)，跳过")
            if im.mode != "RGB":
                bg = Image.new("RGB", im.size, (255, 255, 255))
                if im.mode in ("RGBA", "LA", "P"):
                    rgba = im.convert("RGBA")
                    bg.paste(rgba, mask=rgba.getchannel("A"))
                    im = bg
                else:
                    im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92, optimize=True)
            return True, buf.getvalue(), im.size, ""
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:100]}"
            if i < 2:
                time.sleep(1.0 * (i + 1))
    return False, b"", (0, 0), last


def load_json(path: str, default):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_class(cls: str, used_titles: set, md5_seen: set, records: list) -> int:
    out_dir = os.path.join(OUT_ROOT, cls)
    os.makedirs(out_dir, exist_ok=True)
    existing = sorted(f for f in os.listdir(out_dir) if f.lower().endswith(".jpg"))
    for f in existing:
        try:
            with open(os.path.join(out_dir, f), "rb") as fh:
                md5_seen.add(hashlib.md5(fh.read()).hexdigest())
        except OSError:
            pass
    need = max(0, TARGET_PER_CLASS - len(existing))
    log(f"[{cls}] 已有 {len(existing)} 张，还需 {need} 张")
    if not need:
        return 0

    # 只补少量缺口时，不必把全部分类都翻一遍
    if cls in SEARCHES:
        # 用全文搜索扩源（候选枯竭时）
        q_limit = min(200, max(80, need * 9))
        by_q = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(collect_search, q, q_limit): q for q in SEARCHES[cls]}
            for fut in as_completed(futs):
                q = futs[fut]
                try:
                    by_q[q] = fut.result()
                    log(f"  搜索 {q!r}: {len(by_q[q])} 个候选")
                except Exception as exc:
                    by_q[q] = []
                    log(f"  搜索 {q!r} 采集失败：{exc}")
        lists = [by_q[q] for q in SEARCHES[cls]]
    else:
        cat_subset = CLASSES[cls]       # 全部分类，保证候选充足
        cat_limit = min(300, max(120, need * 7))
        by_cat = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(collect_titles, cat, cat_limit): cat for cat in cat_subset}
            for fut in as_completed(futs):
                cat = futs[fut]
                try:
                    by_cat[cat] = fut.result()
                    log(f"  分类 {cat}: {len(by_cat[cat])} 个候选")
                except Exception as exc:
                    by_cat[cat] = []
                    log(f"  分类 {cat} 采集失败：{exc}")
        lists = [by_cat.get(cat, []) for cat in CLASSES[cls]]
    titles = [t for t in interleave(lists) if t not in used_titles]
    log(f"  合并去重后候选 {len(titles)} 个")

    info = []
    title_pos = 0

    def more_info() -> None:
        nonlocal title_pos
        batch = titles[title_pos:title_pos + BATCH_INFO * 5]
        title_pos += len(batch)
        for rec in fetch_info(batch, ANIMAL[cls], POSE[cls]):
            rec["category"] = cls
            info.append(rec)

    more_info()
    log(f"  首批合格候选 {len(info)} 个，开始下载…")

    # 删过文件后编号有空洞，必须取最大编号+1，否则新文件会覆盖仍存在的旧图
    nums = [int(m.group(1)) for f in existing
            for m in [re.search(r"_(\d+)\.jpg$", f)] if m]
    idx = (max(nums) if nums else 0) + 1
    done = 0
    ci = 0
    fails = []
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        while done < need:
            if ci >= len(info):
                if title_pos >= len(titles):
                    log("  候选已用尽，停止补齐")
                    break
                more_info()
                log(f"  补充候选，累计 {len(info)} 个")
                if ci >= len(info):
                    continue
            batch = info[ci:ci + WORKERS * 2]
            ci += len(batch)
            futs = {pool.submit(fetch_image, rec): rec for rec in batch}
            for fut in as_completed(futs):
                rec = futs[fut]
                try:
                    ok, data, size, err = fut.result()
                except Exception as exc:
                    ok, data, size, err = False, b"", (0, 0), str(exc)
                if not ok:
                    fails.append({"title": rec["title"], "error": err})
                    continue
                digest = hashlib.md5(data).hexdigest()
                if digest in md5_seen:
                    continue
                filename = f"{cls}_{idx:03d}.jpg"
                path = os.path.join(out_dir, filename)
                with open(path, "wb") as f:
                    f.write(data)
                md5_seen.add(digest)
                used_titles.add(rec["title"])
                records.append({
                    "序号": idx, "类别": cls, "动物": ANIMAL[cls], "姿态": POSE[cls],
                    "文件名": filename, "相对路径": os.path.join(cls, filename),
                    "Commons 来源页": rec["page"], "图片直链": rec["url"],
                    "授权": rec["license"], "授权链接": rec["license_url"],
                    "作者": rec["artist"], "原始尺寸": f"{rec['orig_w']}×{rec['orig_h']}",
                    "输出尺寸": f"{size[0]}×{size[1]}", "字节": len(data),
                })
                idx += 1
                done += 1
                if done % 10 == 0 or done == need:
                    log(f"  [{cls}] 进度 {len(existing) + done}/{TARGET_PER_CLASS}")
                if done >= need:
                    for f in futs:
                        f.cancel()
                    break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if done < need:
        log(f"  [{cls}] 警告：只拿到 {done}/{need} 张，失败 {len(fails)} 次")
    return done


def main() -> int:
    os.makedirs(OUT_ROOT, exist_ok=True)
    state_path = os.path.join(OUT_ROOT, "_state.json")
    csv_path = os.path.join(OUT_ROOT, "来源清单.csv")
    json_path = os.path.join(OUT_ROOT, "来源清单.json")
    state = load_json(state_path, {"used_titles": [], "records": []})
    used_titles = set(state.get("used_titles", []))
    records = list(state.get("records", []))
    md5_seen = set()
    log(f"输出目录：{OUT_ROOT}")

    for cls in CLASSES:
        build_class(cls, used_titles, md5_seen, records)
        save_json(state_path, {"used_titles": sorted(used_titles), "records": records})
        # 随时落盘 CSV，防止中断丢清单
        write_csv(csv_path, records)
        save_json(json_path, {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "source": "Wikimedia Commons（开放授权）",
                              "items": records})

    counts = {c: len([f for f in os.listdir(os.path.join(OUT_ROOT, c))
                      if f.lower().endswith(".jpg")]) for c in CLASSES}
    log("完成统计：" + json.dumps(counts, ensure_ascii=False))
    log(f"清单：{csv_path}")
    total = sum(counts.values())
    return 0 if all(v >= TARGET_PER_CLASS for v in counts.values()) else 2


def write_csv(path: str, records: list[dict]) -> None:
    fields = ["序号", "类别", "动物", "姿态", "文件名", "相对路径", "Commons 来源页",
              "图片直链", "授权", "授权链接", "作者", "原始尺寸", "输出尺寸", "字节"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(records)


if __name__ == "__main__":
    code = main()
    # 线程池里可能还在跑已取消不掉的任务；清单已同步落盘，直接退出避免 atexit 等待
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)
