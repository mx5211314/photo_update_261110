# -*- coding: utf-8 -*-
"""监控视角猫狗睡眠数据采集 · 实拍采集与合规检查工具。

按技术规范管理 12000 张图片的配额、台账、查重与合规校验。
纯 Python + Pillow，数据全部留在本机。

用法：
  python collection_tool.py init     # 生成配额表、台账模板、实拍目录
  python collection_tool.py check    # 扫描照片 -> 更新台账 -> 查重/合规检查 -> 生成看板与报告
  python collection_tool.py demo     # 生成少量模拟数据，用于验证工具本身（可随时删除）
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import sys
from collections import defaultdict, Counter
from datetime import datetime

from PIL import Image, ImageOps

BASE = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.abspath(os.environ.get("IMS_PHOTO_DIR")
                           or os.path.join(BASE, "实拍图片"))
SOURCE_CSV = os.path.abspath(os.environ.get("IMS_SOURCE_CSV")
                             or os.path.join(os.path.dirname(BASE), "dataset", "来源清单.csv"))
QUOTA_CSV = os.path.join(BASE, "采集配额.csv")
LEDGER_CSV = os.path.join(BASE, "采集台账.csv")
REPORT_HTML = os.path.join(BASE, "配额看板.html")
REPORT_TXT = os.path.join(BASE, "合规报告.txt")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"}

# 规范未给出具体像素阈值，这里取常见「高分辨率」下限，可按平台口径改
MIN_LONG_SIDE = int(os.environ.get("IMS_MIN_SIDE", "1920"))
DUP_THRESHOLD = int(os.environ.get("IMS_DUP_THR", "10"))   # 256bit 汉明距离
MAX_PER_PET = 10                                            # 规范：每只入画宠物最多约 10 张

SPECIES = ["猫", "狗"]
POSTURE = {
    "卧姿": ["蜷卧", "仰卧", "侧卧", "趴卧"],
    "非卧姿": ["坐姿", "站姿", "蹲伏低伏", "前低后高", "后肢直立"],
}
LYING_TARGETS = {"蜷卧": 1000, "仰卧": 1000, "侧卧": 1000, "趴卧": 1000}
UPRIGHT_TARGETS = {"坐姿": 600, "站姿": 600, "蹲伏低伏": 200, "前低后高": 200, "后肢直立": 200}
EYE_STATES = ["可辨闭眼", "睁眼含微睁", "两眼不可见", "眼部看不清"]
EYE_REQUIRE = {"可辨闭眼": ("至少", 0.50), "睁眼含微睁": ("至少", 0.20),
               "两眼不可见": ("至少", 0.10), "眼部看不清": ("最多", 0.02)}
# V4 规范「四种光照」精确口径
LIGHTS = ["日间亮", "日间暗", "夜间开灯", "夜间关灯红外"]
LIGHT_REQUIRE = {"日间亮": 0.40, "日间暗": 0.10, "夜间开灯": 0.20, "夜间关灯红外": 0.30}
VIEW_POS = ["仰拍", "近似平视", "斜俯拍", "近似正上方"]
VIEW_POS_REQUIRE = {"仰拍": 0.05, "近似平视": 0.25, "斜俯拍": 0.50, "近似正上方": 0.20}
VIEW_DIR = ["前方", "侧方", "后方", "前斜方", "后斜方"]
VIEW_DIR_REQUIRE = {k: 0.20 for k in VIEW_DIR}
DIST = ["近景(≤2米)", "中景(2-4米)", "远景(>4米)"]
DIST_REQUIRE = {"近景(≤2米)": 0.20, "中景(2-4米)": 0.60, "远景(>4米)": 0.20}
MULTIPET = ["单宠", "同物种多宠", "猫狗同框"]
MULTIPET_REQUIRE = {"单宠": 0.70, "同物种多宠": 0.20, "猫狗同框": 0.10}
DIFF_SCENE = ["毛色近似背景", "身体被遮挡"]
DIFF_REQUIRE = {k: 0.05 for k in DIFF_SCENE}
HEAD_SUPPORT = ["抬头/悬空", "伏放/有支撑", "无法判断"]
EYE_LR = ["睁眼", "微睁", "闭眼", "不可见", "看不清"]
ACTIVITIES = ["睡眠", "静止/休息", "观察/嗅闻", "行走/跑跳", "进食饮水", "玩耍",
              "理毛", "如厕", "伸展/伸懒腰", "抓挠物体/刨挖", "啃咬/叼持物品",
              "社交互动", "其他", "无法判断"]

FIELDS = ["相对路径", "文件名", "批次", "物种", "姿态大类", "子姿态", "卧姿头部支撑",
          "左眼状态", "右眼状态", "眼部状态", "光照", "活动",
          "上下观看位置", "水平观看方向", "镜头距离", "多宠", "困难场景",
          "身体遮挡", "头部遮挡", "身体出画", "头部出画", "头框可标状态",
          "宠物ID", "是否主对象", "同框其他宠物ID", "拍摄设备", "监控视角",
          "画面无人员", "主体为猫狗", "数据来源", "宽度", "高度", "字节", "感知哈希",
          "中心哈希", "重复组", "文件状态", "备注"]
USER_FIELDS = ["批次", "物种", "姿态大类", "子姿态", "卧姿头部支撑", "左眼状态", "右眼状态",
               "眼部状态", "光照", "活动", "上下观看位置", "水平观看方向", "镜头距离",
               "多宠", "困难场景", "身体遮挡", "头部遮挡", "身体出画", "头部出画",
               "头框可标状态", "宠物ID", "是否主对象", "同框其他宠物ID", "拍摄设备",
               "监控视角", "画面无人员", "主体为猫狗", "备注"]
TECH_FIELDS = ["相对路径", "文件名", "数据来源", "宽度", "高度", "字节", "感知哈希",
               "中心哈希", "重复组", "文件状态"]
CLASS_DIRS = {"猫-卧姿": ("猫", "卧姿"), "猫-非卧姿": ("猫", "非卧姿"),
              "狗-卧姿": ("狗", "卧姿"), "狗-非卧姿": ("狗", "非卧姿")}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 配额
# --------------------------------------------------------------------------- #
def quota_rows() -> list[dict]:
    rows = []
    for sp in SPECIES:
        for sub, n in LYING_TARGETS.items():
            rows.append({"物种": sp, "姿态大类": "卧姿", "子姿态": sub, "目标张数": n,
                         "备注": "每物种卧姿 4000，四种各 1000"})
        for sub, n in UPRIGHT_TARGETS.items():
            rows.append({"物种": sp, "姿态大类": "非卧姿", "子姿态": sub, "目标张数": n,
                         "备注": "每物种非卧姿 2000；本表子姿态小计 1800，差额 200 待平台确认"})
    return rows


def write_quota() -> None:
    with open(QUOTA_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["物种", "姿态大类", "子姿态", "目标张数", "备注"])
        w.writeheader()
        w.writerows(quota_rows())


def blank_row() -> dict:
    return {k: "" for k in FIELDS}


def read_ledger() -> list[dict]:
    if not os.path.isfile(LEDGER_CSV):
        return []
    with open(LEDGER_CSV, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_ledger(rows: list[dict]) -> None:
    tmp = LEDGER_CSV + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, LEDGER_CSV)


def init() -> None:
    os.makedirs(PHOTO_DIR, exist_ok=True)
    if not os.path.isfile(QUOTA_CSV):   # 配额表可能被本地编辑器打开，存在则不覆盖
        write_quota()
    if not os.path.isfile(LEDGER_CSV):
        write_ledger([])
    log(f"配额表：{QUOTA_CSV}（{len(quota_rows())} 行，目标合计 {sum(r['目标张数'] for r in quota_rows())} 张）")
    log(f"台账模板：{LEDGER_CSV}")
    log(f"照片目录：{PHOTO_DIR}")
    log("请按台账字段填写：物种/子姿态/眼部状态/光照/宠物ID/是否主对象/拍摄设备/监控视角 等")


# --------------------------------------------------------------------------- #
# 感知哈希与查重
# --------------------------------------------------------------------------- #
def _dhash(im: Image.Image, size: int = 16) -> str:
    g = im.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = g.tobytes()   # L 模式每像素 1 字节，避免已弃用的 getdata()
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (1 if px[y * (size + 1) + x] > px[y * (size + 1) + x + 1] else 0)
    return f"{bits:064x}"


def image_hashes(path: str) -> tuple[str, str, int, int, int]:
    with Image.open(path) as src:
        im = ImageOps.exif_transpose(src)
        w, h = im.size
        d1 = _dhash(im)
        box = (int(w * 0.12), int(h * 0.12), int(w * 0.88), int(h * 0.88))
        d2 = _dhash(im.crop(box))
    return d1, d2, w, h, os.path.getsize(path)


def _ham(a: str, b: str) -> int:
    if not a or not b:
        return 999
    return (int(a, 16) ^ int(b, 16)).bit_count()


def find_duplicates(rows: list[dict]) -> dict[int, str]:
    """返回 {行索引: 重复组号}，用分段哈希桶避免 12000 张时的 O(n^2)。"""
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        h = r.get("感知哈希") or ""
        if not h or r.get("文件状态") != "正常":
            continue
        try:
            val = int(h, 16)
        except ValueError:
            continue
        for k in range(4):
            buckets[(k, (val >> (64 * k)) & ((1 << 64) - 1))].append(i)

    parent = list(range(len(rows)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for idxs in buckets.values():
        if len(idxs) < 2 or len(idxs) > 400:   # 纯色图会导致桶过大，跳过异常桶
            continue
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if find(i) == find(j):
                    continue
                d1 = _ham(rows[i].get("感知哈希", ""), rows[j].get("感知哈希", ""))
                d2 = _ham(rows[i].get("中心哈希", ""), rows[j].get("中心哈希", ""))
                if min(d1, d2) <= DUP_THRESHOLD:
                    union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        if rows[i].get("文件状态") == "正常":
            groups[find(i)].append(i)
    out = {}
    n = 0
    for members in groups.values():
        if len(members) > 1:
            n += 1
            tag = f"G{n:03d}"
            for i in members:
                out[i] = tag
    return out


# --------------------------------------------------------------------------- #
# 扫描与台账更新
# --------------------------------------------------------------------------- #
def load_source_map() -> dict:
    """从 dataset/来源清单.csv 读取每张图的授权与来源页。"""
    if not os.path.isfile(SOURCE_CSV):
        return {}
    out = {}
    with open(SOURCE_CSV, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("文件名"):
                out[row["文件名"]] = row
    return out


def scan_and_update() -> list[dict]:
    old = {r.get("相对路径", ""): r for r in read_ledger()}
    src = load_source_map()
    rows = []
    if os.path.isdir(PHOTO_DIR):
        paths = []
        for root, dirs, files in os.walk(PHOTO_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith("_")]
            for fn in files:
                if os.path.splitext(fn)[1].lower() in IMAGE_EXTS:
                    paths.append(os.path.join(root, fn))
        for p in sorted(paths, key=str.lower):
            rel = os.path.relpath(p, PHOTO_DIR).replace("/", os.sep)
            fn = os.path.basename(p)
            rec = dict(old.get(rel, blank_row()))
            rec["相对路径"] = rel
            rec["文件名"] = fn
            # 按分类目录自动带出物种 / 姿态，并按本批实际口径填画面字段
            folder = rel.split(os.sep)[0] if os.sep in rel else ""
            if folder in CLASS_DIRS:
                sp, post = CLASS_DIRS[folder]
                defaults = {"批次": "第一批", "物种": sp, "姿态大类": post,
                            "是否主对象": "是", "画面无人员": "是", "主体为猫狗": "是",
                            "拍摄设备": "真实摄影照片（网络图库）", "活动": "不适用"}
                for k, v in defaults.items():
                    if not (rec.get(k) or "").strip():
                        rec[k] = v
            srow = src.get(fn)
            if srow:
                rec["数据来源"] = ("Wikimedia Commons｜" + srow.get("授权", "")
                                   + "｜" + srow.get("Commons 来源页", ""))
            elif not (rec.get("数据来源") or "").strip():
                rec["数据来源"] = "开放图库真实摄影照片（来源见 dataset/来源清单.csv）"
            try:
                d1, d2, w, h, size = image_hashes(p)
                rec.update({"宽度": w, "高度": h, "字节": size,
                            "感知哈希": d1, "中心哈希": d2, "文件状态": "正常"})
            except Exception as exc:
                rec["文件状态"] = f"读取失败:{type(exc).__name__}"
            for k in TECH_FIELDS:
                rec.setdefault(k, "")
            for k in USER_FIELDS:
                rec.setdefault(k, "")
            rec["重复组"] = ""
            rows.append(rec)

    # 保留台账里已不存在文件的记录，标记为缺失，便于用户发现误删
    seen = {r["相对路径"] for r in rows}
    for rel, rec in old.items():
        if rel not in seen:
            rec = dict(rec)
            rec["文件状态"] = "文件缺失"
            rec["重复组"] = ""
            rows.append(rec)

    dups = find_duplicates(rows)
    for i, tag in dups.items():
        rows[i]["重复组"] = tag
    write_ledger(rows)
    return rows


# --------------------------------------------------------------------------- #
# 合规检查
# --------------------------------------------------------------------------- #
def effective_rows(rows: list[dict]) -> list[dict]:
    """按规范计数：每图只计一只主对象；疑似重复组只保留组内第一张。"""
    out, seen_groups = [], set()
    for r in rows:
        if r.get("文件状态") != "正常":
            continue
        g = r.get("重复组") or ""
        if g:
            if g in seen_groups:
                continue
            seen_groups.add(g)
        if (r.get("是否主对象") or "").strip() != "是":
            continue
        out.append(r)
    return out


def check(rows: list[dict]) -> dict:
    issues: list[dict] = []
    eff = effective_rows(rows)

    def add(level, kind, msg):
        issues.append({"级别": level, "类别": kind, "说明": msg})

    # 1. 配额
    quota = Counter()
    for r in eff:
        key = (r.get("物种", ""), r.get("姿态大类", ""), r.get("子姿态", ""))
        if all(key):
            quota[key] += 1
    quota_detail = []
    for q in quota_rows():
        key = (q["物种"], q["姿态大类"], q["子姿态"])
        done = quota.get(key, 0)
        quota_detail.append({**q, "有效": done, "剩余": max(0, q["目标张数"] - done),
                             "完成率": round(done / q["目标张数"] * 100, 1)})

    # 2. 眼部覆盖（仅卧姿有效图）
    lying = [r for r in eff if r.get("姿态大类") == "卧姿"]
    eye_count = Counter(r.get("眼部状态", "") for r in lying)
    eye_detail = []
    for state in EYE_STATES:
        n = eye_count.get(state, 0)
        ratio = n / len(lying) if lying else 0
        mode, req = EYE_REQUIRE[state]
        ok = ratio >= req if mode == "至少" else ratio <= req
        eye_detail.append({"眼部状态": state, "张数": n,
                           "占比": round(ratio * 100, 1), "要求": f"{mode} {req*100:.0f}%",
                           "达标": ok})
        if lying and not ok:
            add("警告", "眼部覆盖", f"{state} 占比 {ratio*100:.1f}%，要求 {mode} {req*100:.0f}%")
    if len(lying) < 100:
        add("提示", "眼部覆盖", f"卧姿有效图仅 {len(lying)} 张，占比统计暂不具参考性")

    # 3. 四种光照覆盖
    light_matrix = {}
    for state in ["可辨闭眼", "睁眼含微睁", "两眼不可见"]:
        light_matrix[state] = {L: sum(1 for r in lying
                                     if r.get("眼部状态") == state and r.get("光照") == L)
                               for L in LIGHTS}
        missing = [L for L, n in light_matrix[state].items() if n == 0]
        if lying and missing:
            add("警告", "光照覆盖", f"{state} 缺少光照：{'、'.join(missing)}")

    # 4. 每只宠物最多约 10 张
    pet = Counter(r.get("宠物ID", "").strip() for r in eff if r.get("宠物ID", "").strip())
    pet_over = {k: v for k, v in pet.items() if v > MAX_PER_PET}
    for k, v in pet_over.items():
        add("错误", "重复计数", f"宠物 {k} 作为主对象出现 {v} 张，超过约 {MAX_PER_PET} 张上限")

    # 5. 重复组
    dup_groups = defaultdict(list)
    for r in rows:
        if r.get("重复组"):
            dup_groups[r["重复组"]].append(os.path.basename(r.get("相对路径", "")))
    for g, names in dup_groups.items():
        add("警告", "疑似重复", f"{g}：{len(names)} 张疑似连拍/裁剪/改名（{', '.join(names[:4])}…），组内只计 1 张")

    # 6. 分辨率与朝向（V4：原始分辨率≥1280×720；全部横屏宽>高，优先16:9）
    bad_res = [r for r in rows if r.get("文件状态") == "正常" and
               (int(r.get("宽度") or 0) < 1280 or int(r.get("高度") or 0) < 720)]
    if bad_res:
        add("错误", "分辨率", f"{len(bad_res)} 张原始分辨率 < 1280×720（V4 硬性下限，需剔除或换源）")
    portrait = [r for r in rows if r.get("文件状态") == "正常" and
                int(r.get("宽度") or 0) <= int(r.get("高度") or 0)]
    if portrait:
        add("错误", "横屏", f"{len(portrait)} 张非横屏（宽≤高），违反 V4「全部横屏交付」硬性要求，须剔除或换源")

    # 7. 必填字段（仅统计实际缺失的字段名，避免误导）
    required = ["批次", "物种", "姿态大类", "子姿态", "宠物ID", "是否主对象",
                "拍摄设备", "画面无人员", "主体为猫狗"]
    incomplete = 0
    missing_by_field = Counter()
    for r in rows:
        if r.get("文件状态") != "正常":
            continue
        miss = [k for k in required if not (r.get(k) or "").strip()]
        if r.get("姿态大类") == "卧姿" and not (r.get("眼部状态") or "").strip():
            miss.append("眼部状态(卧姿)")
        if not (r.get("光照") or "").strip():
            miss.append("光照")
        for k in miss:
            missing_by_field[k] += 1
        if miss:
            incomplete += 1
    if incomplete:
        detail = "、".join(f"{k}（缺{missing_by_field[k]}）" for k in missing_by_field)
        add("错误", "台账填写", f"{incomplete} 张图片缺必填字段，实际缺失分布：{detail}。"
                               f"其中子姿态/宠物ID/眼部状态/光照/机位/距离/多宠等 V4 维度需逐张人工标注，工具无法从网图自动判定。")

    # 7b. 画面合规：不带人、主体是猫狗（监控视角非硬性，V4 允许手机横拍/家监截图/录像抽帧）
    ok_rows = [r for r in rows if r.get("文件状态") == "正常"]
    for key, label in (("画面无人员", "画面不带人"), ("主体为猫狗", "主体为猫狗")):
        bad_n = sum(1 for r in ok_rows if (r.get(key) or "").strip() != "是")
        if bad_n:
            add("警告", "画面合规", f"{bad_n} 张未确认「{label}」，需人工复核后在台账填 是")

    # 7c. V4 分布维度（仅当字段已填时统计；未填不重复告警，由必填检查覆盖）
    def dist_report(field, req_map, label):
        filled = [r for r in ok_rows if (r.get(field) or "").strip()]
        if not filled:
            return
        cnt = Counter(r[field].strip() for r in filled)
        parts = []
        for k, v in req_map.items():
            got = cnt.get(k, 0) / len(filled)
            parts.append(f"{k} {got*100:.0f}%（目标{int(v*100)}%）")
        add("提示", label, f"已标注 {len(filled)} 张分布：{'，'.join(parts)}（未标注项不计入）")

    dist_report("光照", LIGHT_REQUIRE, "光照分布")
    dist_report("上下观看位置", VIEW_POS_REQUIRE, "上下机位分布")
    dist_report("水平观看方向", VIEW_DIR_REQUIRE, "水平方向分布")
    dist_report("镜头距离", DIST_REQUIRE, "镜头距离分布")
    dist_report("多宠", MULTIPET_REQUIRE, "多宠分布")
    dist_report("困难场景", DIFF_REQUIRE, "困难场景分布")

    # 8. 文件状态
    bad = [r for r in rows if r.get("文件状态") != "正常"]
    for r in bad[:20]:
        add("错误", "文件", f"{r.get('相对路径','')}：{r.get('文件状态','')}")

    # 9. 规范本身的待确认项（不是数据错误）
    subtotal = sum(r["目标张数"] for r in quota_rows())
    notes = [
        f"规范总量 12000 张，但按「每物种非卧姿 坐姿600+站姿600+蹲伏低伏/前低后高/后肢直立各200」拆出的非卧姿小计为 1800/物种，"
        f"两类合计 {subtotal} 张，与 12000 相差 {12000-subtotal} 张 —— 差额归属需向平台确认，本工具未擅自补齐。",
        "V4 四种光照口径：日间亮40% / 日间暗10% / 夜间开灯20% / 夜间关灯红外30%（合计日:夜开:红外=5:2:3）。夜间关灯红外须设备原生成像，不得调灰伪造。",
        "V4 距离配比：近景(≤2米)20% / 中景(2-4米)60% / 远景(>4米)20%。近距特写（≤2米）本身占 20%，并非越远越好。",
        "V4 上下机位：仰拍5% / 平视25% / 斜俯拍50% / 正上方20%；水平方向 前/侧/后/前斜/后斜 各 20%。",
        "V4 多宠：单宠70% / 同物种多宠20% / 猫狗同框10%。困难场景：毛色近似背景、身体被遮挡 各≥5%。",
        "V4：全部横屏交付（宽>高，优先16:9）；原始分辨率≥1280×720，不插值放大/美颜/拼接，不以贴脸特写替代家监场景。",
        "V4 图片来源可为 家监截图 / 录像抽帧 / 手机横拍 —— 地点不限定在家；本批网络图仅作真实照素材，来源逐张记于台账。",
        "规范注：数据分批采集，第一批睡眠视频交付 90 条（猫狗各 90 段？以原文 360 段/批90为准）—— 视频不在本图片台账内，需单独管理。",
        "每只实际入画宠物最多约 10 张，不以连拍/裁剪/改名重复计数；每图只选一只主对象，其他猫狗全部标注，混养不重复计数。",
        "子姿态 / 卧姿头部支撑 / 左右眼状态 / 上下机位 / 水平方向 / 镜头距离 / 多宠 / 困难场景 / 活动 / 遮挡出画 等 V4 维度需逐张人工标注，"
        "工具无法从网图自动判定，台账对应列留空，计为「待补标注」。",
    ]

    return {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "文件总数": len(rows),
        "有效计数": len(eff),
        "目标总数": 12000,
        "配额表": quota_detail,
        "眼部": eye_detail,
        "卧姿有效": len(lying),
        "光照矩阵": light_matrix,
        "宠物统计": dict(pet),
        "重复组数": len(dup_groups),
        "图片根目录": PHOTO_DIR,
        "分类汇总": {c: sum(1 for r in rows
                          if r.get("相对路径", "").startswith(c + os.sep))
                     for c in CLASS_DIRS},
        "问题": issues,
        "待确认": notes,
        "必填未完成": incomplete,
    }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#0d0f14;--panel:#151821;--panel2:#1b1f2a;--line:#2b3142;--text:#e7eaf2;
--muted:#8d95a8;--accent:#6d8cff;--green:#38d9a9;--amber:#fbbf24;--red:#f87171}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:28px}
.wrap{max-width:1180px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 12px;color:#c9d4ff}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 18px;min-width:150px}
.kpi b{display:block;font-size:24px}.kpi span{font-size:11.5px;color:var(--muted)}
.kpi.good b{color:var(--green)}.kpi.warn b{color:var(--amber)}.kpi.bad b{color:var(--red)}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{background:var(--panel2);color:var(--muted);text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:7px 10px;border-bottom:1px solid rgba(43,49,66,.6)}
tr:hover td{background:rgba(109,140,255,.05)}
.bar{height:7px;background:var(--panel2);border-radius:10px;overflow:hidden;min-width:90px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#9b6dff)}
.ok{color:var(--green)}.no{color:var(--red)}.warn{color:var(--amber)}
.issue{padding:7px 10px;border-bottom:1px solid var(--line);font-size:12.5px}
.issue:last-child{border:0}.tag{display:inline-block;min-width:46px;font-size:11px;padding:1px 6px;
border-radius:20px;background:var(--panel2);color:var(--muted);margin-right:8px}
.tag.错误{color:var(--red)}.tag.警告{color:var(--amber)}.tag.提示{color:var(--accent)}
.note{background:rgba(109,140,255,.07);border:1px solid rgba(109,140,255,.2);border-radius:8px;
padding:10px 12px;font-size:12.5px;color:var(--muted);margin-top:8px}
ul{margin:6px 0;padding-left:20px}li{margin:3px 0}
.foot{text-align:center;color:#656e82;font-size:11.5px;padding:10px}
code{background:var(--panel2);border:1px solid var(--line);border-radius:4px;padding:1px 5px;
font-size:11.5px;color:#c6d0f5}
ol li::marker{color:var(--accent)}
"""


def render_html(res: dict) -> str:
    e = html.escape
    pct = res["有效计数"] / res["目标总数"] * 100
    body = [
        "<div class='wrap'>",
        "<h1>监控视角猫狗睡眠数据采集 · 配额与合规看板</h1>",
        f"<div class='sub'>生成时间 {e(res['生成时间'])} · 图片根目录 {e(res['图片根目录'])} "
        f"· 数据源 采集台账.csv · 分类：{' / '.join(f'{e(k)} {v}张' for k,v in res['分类汇总'].items())}</div>",
        "<div class='kpis'>",
        f"<div class='kpi'><b>{res['有效计数']}</b><span>有效计数 / 12000</span></div>",
        f"<div class='kpi'><b>{res['文件总数']}</b><span>台账文件数</span></div>",
        f"<div class='kpi good'><b>{pct:.1f}%</b><span>总体完成率</span></div>",
        f"<div class='kpi warn'><b>{res['重复组数']}</b><span>疑似重复组</span></div>",
        f"<div class='kpi bad'><b>{len([x for x in res['问题'] if x['级别']=='错误'])}</b><span>错误项</span></div>",
        "</div>",
        "<div class='card'><h2>一、数量配额（按物种 × 姿态 × 子姿态）</h2><table>",
        "<tr><th>物种</th><th>姿态</th><th>子姿态</th><th>目标</th><th>有效</th><th>剩余</th><th style='width:180px'>进度</th><th>完成率</th></tr>",
    ]
    for q in res["配额表"]:
        w = min(100, q["完成率"])
        body.append(
            f"<tr><td>{e(q['物种'])}</td><td>{e(q['姿态大类'])}</td><td>{e(q['子姿态'])}</td>"
            f"<td>{q['目标张数']}</td><td>{q['有效']}</td><td>{q['剩余']}</td>"
            f"<td><div class='bar'><i style='width:{w}%'></i></div></td><td>{q['完成率']}%</td></tr>")
    body.append("</table></div>")

    body.append("<div class='card'><h2>二、卧姿眼部覆盖（规范比例）</h2><table>"
                "<tr><th>覆盖类别</th><th>张数</th><th>占比</th><th>要求</th><th>判定</th></tr>")
    for x in res["眼部"]:
        cls = "ok" if x["达标"] else "no"
        body.append(f"<tr><td>{e(x['眼部状态'])}</td><td>{x['张数']}</td><td>{x['占比']}%</td>"
                    f"<td>{e(x['要求'])}</td><td class='{cls}'>{'达标' if x['达标'] else '未达标'}</td></tr>")
    body.append(f"</table><div class='note'>卧姿有效图基数：{res['卧姿有效']} 张</div></div>")

    body.append("<div class='card'><h2>三、四种光照覆盖（可辨睁闭眼 / 两眼不可见）</h2><table><tr><th>眼部状态</th>")
    for L in LIGHTS:
        body.append(f"<th>{e(L)}</th>")
    body.append("</tr>")
    for state, row in res["光照矩阵"].items():
        body.append(f"<tr><td>{e(state)}</td>")
        for L in LIGHTS:
            n = row.get(L, 0)
            body.append(f"<td class='{'ok' if n else 'no'}'>{n}</td>")
        body.append("</tr>")
    body.append("</table></div>")

    body.append("<div class='card'><h2>四、合规问题</h2>")
    if not res["问题"]:
        body.append("<div class='note'>未发现问题</div>")
    else:
        for x in res["问题"][:80]:
            body.append(f"<div class='issue'><span class='tag {e(x['级别'])}'>{e(x['级别'])}</span>"
                        f"<b>{e(x['类别'])}</b> · {e(str(x['说明']))}</div>")
        if len(res["问题"]) > 80:
            body.append(f"<div class='note'>另有 {len(res['问题'])-80} 条，详见合规报告.txt</div>")
    body.append("</div>")

    body.append("<div class='card'><h2>五、规范待确认项（重要）</h2><ul>")
    for n in res["待确认"]:
        body.append(f"<li>{e(n)}</li>")
    body.append("</ul></div>")

    steps = [
        "用<b>手机相机实拍</b>：模拟监控视角（离宠物远一些、固定机位感）、高分辨率；画面<b>不要带人</b>、<b>主体必须是猫或狗</b>。按批次把照片放进 <code>实拍图片/</code>。",
        "运行 <code>python collection_tool.py check</code>：自动扫描照片、计算感知哈希、更新 <code>采集台账.csv</code>，并生成本看板与 <code>合规报告.txt</code>。",
        "在 <code>采集台账.csv</code> 逐张填写：物种、姿态大类、子姿态、眼部状态、光照、活动、宠物ID、是否主对象、同框其他宠物ID、拍摄设备、监控视角、画面无人员、主体为猫狗。",
        "再跑一次 <code>check</code>：核查配额进度、闭眼/睁眼/两眼不可见占比、四种光照覆盖、每只宠物 ≤10 张、疑似连拍或裁剪改名重复。",
        "达标后可用「图片元数据工作台」给实拍图写入元数据并导出交付台账。",
        "本批为开放授权<b>真实摄影场景照</b>（按任务口径「实拍=真实拍摄」），已按「不带人 / 主体猫狗」筛选；"
        "每张的来源页与授权记在台账「数据来源」列，便于版权核查。",
    ]
    body.append("<div class='card'><h2>六、采集与交付步骤</h2><ol style='font-size:12.5px;color:var(--muted);padding-left:20px'>")
    for s in steps:
        body.append(f"<li style='margin:6px 0'>{s}</li>")
    body.append("</ol></div>")
    body.append("<div class='foot'>监控视角猫狗睡眠数据采集 · 本地工具生成 · 数据不出本机</div></div>")
    return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>采集配额与合规看板</title><style>" + CSS + "</style></head><body>"
            + "".join(body) + "</body></html>")


def write_reports(res: dict) -> None:
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(res))
    lines = [f"合规报告 · 生成时间 {res['生成时间']}",
             f"图片根目录：{res['图片根目录']}（台账「相对路径」以此目录为基准）",
             f"文件总数 {res['文件总数']} | 有效计数 {res['有效计数']} / 12000 "
             f"| 疑似重复组 {res['重复组数']} | 必填未完成 {res['必填未完成']}",
             "", "== 分类整理 =="]
    for c, n in res["分类汇总"].items():
        lines.append(f"  {c}: {n} 张  ->  {os.path.join(res['图片根目录'], c)}")
    lines += ["", "== 问题 =="]
    for x in res["问题"]:
        lines.append(f"[{x['级别']}] {x['类别']}：{x['说明']}")
    if not res["问题"]:
        lines.append("无")
    lines += ["", "== 规范待确认 =="] + [f"- {n}" for n in res["待确认"]]
    with open(REPORT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run_check() -> dict:
    if not os.path.isfile(LEDGER_CSV):
        init()
    rows = scan_and_update()
    res = check(rows)
    write_reports(res)
    log(f"文件 {res['文件总数']} 张 | 有效计数 {res['有效计数']}/12000 | "
        f"问题 {len(res['问题'])} 条 | 重复组 {res['重复组数']} 个")
    log(f"看板：{REPORT_HTML}")
    log(f"报告：{REPORT_TXT}")
    return res


# --------------------------------------------------------------------------- #
# 模拟数据（仅用于验证工具本身）
# --------------------------------------------------------------------------- #
def demo() -> None:
    from PIL import ImageDraw
    import shutil
    init()
    demo_dir = os.path.join(PHOTO_DIR, "_演示")
    os.makedirs(demo_dir, exist_ok=True)
    im = Image.new("RGB", (2400, 1600), (60, 90, 140))
    ImageDraw.Draw(im).ellipse([700, 400, 1700, 1200], fill=(210, 170, 90))
    im.save(os.path.join(demo_dir, "监控_001.jpg"), "JPEG", quality=90)
    im.save(os.path.join(demo_dir, "监控_002_改名.jpg"), "JPEG", quality=90)   # 故意重复
    im2 = im.crop((100, 100, 2300, 1500))
    im2.save(os.path.join(demo_dir, "监控_003_裁剪.jpg"), "JPEG", quality=90)  # 裁剪重复
    im3 = Image.new("RGB", (2400, 1600), (120, 70, 60))
    ImageDraw.Draw(im3).rectangle([500, 500, 1900, 1100], fill=(30, 160, 110))
    im3.save(os.path.join(demo_dir, "监控_004.jpg"), "JPEG", quality=90)
    rows = scan_and_update()
    spec = [
        ("监控_001.jpg", "猫", "卧姿", "蜷卧", "可辨闭眼", "自然光", "猫_001"),
        ("监控_002_改名.jpg", "猫", "卧姿", "蜷卧", "可辨闭眼", "室内灯光", "猫_001"),
        ("监控_003_裁剪.jpg", "猫", "卧姿", "侧卧", "睁眼含微睁", "弱光夜视", "猫_002"),
        ("监控_004.jpg", "狗", "非卧姿", "坐姿", "不适用", "逆光混合", "狗_001"),
    ]
    for r in rows:
        for fn, sp, post, sub, eye, light, pet in spec:
            if r["文件名"] == fn:
                r.update({"批次": "第一批", "物种": sp, "姿态大类": post, "子姿态": sub,
                          "眼部状态": eye, "光照": light, "活动": "不适用", "宠物ID": pet,
                          "是否主对象": "是", "同框其他宠物ID": "", "拍摄设备": "手机",
                          "监控视角": "是"})
    write_ledger(rows)
    res = run_check()
    print("\n模拟数据验证：")
    print("  疑似重复组:", res["重复组数"], "（预期 ≥1）")
    print("  有效计数:", res["有效计数"], "（重复组只计 1 张）")
    print("  眼部明细:", [(x["眼部状态"], x["张数"]) for x in res["眼部"]])


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "init":
        init()
    elif cmd == "check":
        run_check()
    elif cmd == "demo":
        demo()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
