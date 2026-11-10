# -*- coding: utf-8 -*-
"""ImageMeta Studio 本地服务。仅监听 127.0.0.1，数据不出本机。"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import socket
import string
import sys
import threading
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
INBOX_DIR = os.path.join(BASE_DIR, "inbox")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "server.log")
PORT = 8765
MAX_UPLOAD = 120 * 1024 * 1024

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允许多个进程绑定同一端口，会造成请求被随机分发、
    # 后台任务表各自独立（前端轮询到另一个实例就查不到任务）。必须关掉。
    allow_reuse_address = False
    daemon_threads = True


def port_busy(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.35)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = core.deep_merge(cfg, json.load(f))
        except Exception as exc:
            log(f"读取配置失败，使用默认值：{exc}")
    return cfg


def save_config(patch: dict) -> dict:
    cfg = load_config()
    incoming = patch or {}
    ai = dict(incoming.get("ai") or {})
    # 空 api_key 表示「保留已保存的密钥」，避免前端明文回传
    if not ai.get("api_key"):
        ai.pop("api_key", None)
    patch_clean = {k: v for k, v in incoming.items() if k != "ai"}
    patch_clean["ai"] = ai
    cfg = core.deep_merge(cfg, patch_clean)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return cfg


def public_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    out["ai"]["api_key"] = ""
    out["ai"]["api_key_set"] = bool(cfg["ai"].get("api_key"))
    return out


def log(msg: str) -> None:
    line = f"[{core.now_iso()}] {msg}"
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #
def new_job(kind: str, total: int) -> dict:
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "kind": kind, "status": "running", "total": total,
           "done": 0, "results": [], "errors": [], "cancel": False,
           "started": core.now_iso(), "finished": "", "summary": {}}
    with _jobs_lock:
        _jobs[jid] = job
        if len(_jobs) > 20:
            for k in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-20]:
                _jobs.pop(k, None)
    return job


def job_snapshot(jid: str) -> dict:
    with _jobs_lock:
        j = _jobs.get(jid)
        if not j:
            return {"error": "任务不存在"}
        # results 只增不减，前端自行按 index 增量取
        return {k: v for k, v in j.items() if k != "cancel"}


def run_ai_job(job: dict, paths: list[str], cfg: dict, offline_only: bool = False) -> None:
    conc = max(1, min(8, int(cfg["ai"].get("concurrency") or 3)))
    lock = threading.Lock()

    def one(p: str):
        if job["cancel"]:
            return None
        if offline_only:
            return {"path": p, "meta": core.fallback_meta(p), "error": ""}
        meta, err = core.ai_generate_one(p, cfg, use_fallback=True)
        return {"path": p, "meta": meta, "error": err}

    try:
        with ThreadPoolExecutor(max_workers=conc) as pool:
            futures = {pool.submit(one, p): p for p in paths}
            for fut in as_completed(futures):
                if job["cancel"]:
                    break
                p = futures[fut]
                try:
                    res = fut.result()
                    if res is None:
                        continue
                except Exception as exc:
                    res = {"path": p, "meta": core.fallback_meta(p), "error": str(exc)}
                with lock:
                    job["results"].append(res)
                    job["done"] += 1
                    if res["error"]:
                        job["errors"].append({"path": p, "error": res["error"]})
        job["status"] = "cancelled" if job["cancel"] else "done"
        job["summary"] = {
            "ai": sum(1 for r in job["results"] if r["meta"].get("source") == "ai"),
            "offline": sum(1 for r in job["results"] if r["meta"].get("source") == "offline"),
        }
    except Exception as exc:
        job["status"] = "error"
        job["errors"].append({"path": "", "error": f"{exc}\n{traceback.format_exc()[:500]}"})
    finally:
        job["finished"] = core.now_iso()


def _output_path(path: str, root: str, out_dir: str, opts: dict,
                 reserved: set | None = None) -> str:
    keep_tree = bool(opts.get("keep_tree", True))
    suffix = str(opts.get("suffix") or "")
    suffix = re.sub(r'[\\/:*?"<>|]', "_", suffix)
    # 后缀只应是文字标记；用户常误填成 .jpg，会导致输出名出现双重扩展名
    suffix = re.sub(r"\.(jpe?g|png|webp|tiff?|bmp|gif|jfif)$", "", suffix, flags=re.I)
    if keep_tree:
        try:
            rel = os.path.relpath(path, root)
            if rel.startswith(".."):
                rel = os.path.basename(path)
        except ValueError:
            rel = os.path.basename(path)
    else:
        rel = os.path.basename(path)
    stem, ext = os.path.splitext(rel)
    rel = f"{stem}{suffix}{ext}"
    out = os.path.abspath(os.path.join(out_dir, rel))
    inp = os.path.abspath(path)
    if out == inp:
        out = os.path.splitext(out)[0] + "_meta" + os.path.splitext(out)[1]
    # 永不覆盖既有文件（包括原图）；reserved 用于并发任务的路径预分配
    taken = reserved if reserved is not None else set()
    base, e = os.path.splitext(out)
    if os.path.exists(out) or out.lower() in taken:
        i = 2
        while os.path.exists(f"{base}_{i}{e}") or f"{base}_{i}{e}".lower() in taken:
            i += 1
        out = f"{base}_{i}{e}"
    if reserved is not None:
        reserved.add(out.lower())
    return out


def _write_sidecars(out_dir: str, results: list[dict]) -> dict:
    csv_path = os.path.join(out_dir, "metadata.csv")
    json_path = os.path.join(out_dir, "metadata.json")
    headers = ["原始文件", "输出文件", "标题", "描述", "替代文本", "关键词",
               "分类", "作者", "版权", "自定义字段", "原始尺寸", "输出尺寸",
               "写入标准", "备注", "状态"]
    rows = []
    for r in results:
        m = core.norm_meta(r.get("meta") or {})
        rows.append([
            r.get("input", ""), r.get("output", ""), m["title"], m["description"],
            m["alt"], "; ".join(m["keywords"]), m["category"], m["author"],
            m["copyright"], json.dumps(m["custom"], ensure_ascii=False) if m["custom"] else "",
            "×".join(map(str, r.get("orig_size", []))), "×".join(map(str, r.get("new_size", []))),
            "+".join(r.get("written", [])), "; ".join(r.get("notes", [])),
            "成功" if r.get("ok") else "失败",
        ])
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": core.now_iso(), "count": len(results),
                       "items": results}, f, ensure_ascii=False, indent=2)
        return {"csv": csv_path, "json": json_path}
    except Exception as exc:
        return {"error": f"台账写入失败：{exc}"}


RATIO_MAP = {"16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3),
             "3:2": (3, 2), "1:1": (1, 1)}


def normalize_dimensions(opts: dict) -> dict:
    """按锁定的画面比例补齐另一边；服务端兜底，前端算错也以这里为准。"""
    o = dict(opts or {})
    ratio = str(o.get("ratio") or "free")
    try:
        w = int(o.get("width") or 0)
        h = int(o.get("height") or 0)
    except (TypeError, ValueError):
        w = h = 0
    if ratio in RATIO_MAP:
        rw, rh = RATIO_MAP[ratio]
        if w > 0:
            h = max(1, int(w * rh / rw + 0.5))  # 与前端 Math.round 保持一致
        elif h > 0:
            w = max(1, int(h * rw / rh + 0.5))
    o["width"], o["height"] = max(0, w), max(0, h)
    return o


def run_process_job(job: dict, root: str, items: list[dict], opts: dict) -> None:
    opts = normalize_dimensions(opts)
    out_dir = os.path.abspath(opts.get("out_dir") or os.path.join(root, "_ImageMeta_输出"))
    sidecar = bool(opts.get("sidecar", True))
    workers = max(1, min(6, (os.cpu_count() or 4)))
    lock = threading.Lock()
    results: list[dict] = []

    def one(task):
        if job["cancel"]:
            return None
        item, out = task
        path = item["path"]
        meta = core.norm_meta(item.get("meta") or {})
        missing = ("原图已不在该路径（可能被移动、重命名或删除），请重新扫描目录后再转换。"
                   f"缺失文件：{path}")
        if not os.path.isfile(path):
            return {"ok": False, "input": path, "output": "", "meta": meta,
                    "error": missing, "notes": [], "written": [],
                    "orig_size": [], "new_size": [], "bytes": 0}
        try:
            r = core.save_processed(path, out, meta, opts)
            r["meta"] = meta
            return r
        except FileNotFoundError:
            return {"ok": False, "input": path, "output": "", "meta": meta,
                    "error": missing, "notes": [], "written": [],
                    "orig_size": [], "new_size": [], "bytes": 0}
        except Exception as exc:
            return {"ok": False, "input": path, "output": out, "meta": meta,
                    "error": str(exc), "notes": [], "written": [],
                    "orig_size": [], "new_size": [], "bytes": 0}

    try:
        os.makedirs(out_dir, exist_ok=True)
        # 主线程串行预分配输出路径，避免并发任务算出同一个目标文件
        reserved: set[str] = set()
        tasks = [(it, _output_path(it["path"], root, out_dir, opts, reserved))
                 for it in items]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, t): t for t in tasks}
            for fut in as_completed(futures):
                if job["cancel"]:
                    break
                it = futures[fut][0]
                try:
                    r = fut.result()
                    if r is None:
                        continue
                except Exception as exc:
                    r = {"ok": False, "input": it["path"], "output": "",
                         "meta": core.norm_meta(it.get("meta")), "error": str(exc),
                         "notes": [], "written": [], "orig_size": [], "new_size": [], "bytes": 0}
                with lock:
                    results.append(r)
                    job["results"].append(r)
                    job["done"] += 1
                    if not r["ok"]:
                        job["errors"].append({"path": r["input"], "error": r.get("error", "未知错误")})
        extra = _write_sidecars(out_dir, results) if (sidecar and results) else {}
        job["summary"] = {
            "out_dir": out_dir,
            "success": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
            "sidecar": extra,
            "total_bytes": sum(r.get("bytes", 0) for r in results),
        }
        job["status"] = "cancelled" if job["cancel"] else "done"
    except Exception as exc:
        job["status"] = "error"
        job["errors"].append({"path": "", "error": f"{exc}\n{traceback.format_exc()[:500]}"})
    finally:
        job["finished"] = core.now_iso()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "ImageMetaStudio/1.0"

    def log_message(self, fmt, *args):
        # 仅记录错误，避免日志噪音与路径泄露
        if "404" in (fmt % args) or "500" in (fmt % args):
            log("HTTP " + (fmt % args))

    # ---- 响应封装 ----
    def _json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data: bytes, mime: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception, status: int = 400):
        log(f"请求失败：{exc}")
        self._json({"error": str(exc)}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_UPLOAD:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # ---- 静态资源 ----
    def _static(self, rel: str):
        name = os.path.basename(rel) or "index.html"
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            path = os.path.join(BASE_DIR, name)
        mime = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                ".ico": "image/x-icon"}.get(os.path.splitext(name)[1].lower(),
                                            "application/octet-stream")
        if not os.path.isfile(path):
            return self._json({"error": "资源不存在"}, 404)
        with open(path, "rb") as f:
            self._bytes(f.read(), mime)

    # ---- GET ----
    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        p = parsed.path
        try:
            if p in ("/", "/index.html"):
                return self._static("index.html")
            if p in ("/app.js", "/style.css", "/favicon.svg"):
                return self._static(p.lstrip("/"))
            if p == "/api/health":
                return self._json({"ok": True, "version": "1.0", "port": PORT,
                                   "python": sys.version.split()[0]})
            if p == "/api/config":
                return self._json(public_config(load_config()))
            if p == "/api/browse":
                return self._json(browse(q.get("path", [""])[0]))
            if p == "/api/thumb":
                path = unquote(q.get("path", [""])[0])
                w = max(32, min(600, int(q.get("w", ["200"])[0])))
                if not os.path.isfile(path):
                    return self._json({"error": "文件不存在"}, 404)
                return self._bytes(core.make_thumb(path, w), "image/jpeg")
            if p == "/api/job":
                return self._json(job_snapshot(q.get("id", [""])[0]))
            return self._json({"error": "未知接口"}, 404)
        except Exception as exc:
            return self._error(exc)

    # ---- POST ----
    def do_POST(self):
        parsed = urlparse(self.path)
        p = parsed.path
        try:
            body = self._body()
            if p == "/api/config":
                save_config(body)
                return self._json({"ok": True, "config": public_config(load_config())})
            if p == "/api/scan":
                root = os.path.abspath(body.get("root") or "")
                items = core.scan_images(root, bool(body.get("recursive", True)))
                return self._json({"ok": True, "root": root, "items": items})
            if p == "/api/upload":
                return self._json(upload_files(body.get("files") or []))
            if p == "/api/verify":
                paths = body.get("paths") or []
                missing = [x for x in paths if not os.path.isfile(x)]
                return self._json({"ok": True, "total": len(paths), "missing": missing})
            if p == "/api/autofill":
                root = os.path.abspath(body.get("root") or "")
                manifest = load_manifest(body.get("manifest") or "")
                items, hit = [], 0
                for path in (body.get("paths") or []):
                    m = autofill_meta(path, root)
                    row = manifest.get(os.path.basename(path))
                    if row:
                        apply_manifest(m, row)
                        hit += 1
                    items.append({"path": path, "meta": m})
                return self._json({"ok": True, "items": items,
                                   "manifest_rows": len(manifest), "manifest_hit": hit})
            if p == "/api/ai/ping":
                ok, msg = core.ping_llm(load_config())
                return self._json({"ok": ok, "message": msg})
            if p == "/api/ai/job":
                paths = [x for x in (body.get("paths") or []) if os.path.isfile(x)]
                if not paths:
                    raise ValueError("所选图片已不在原路径（可能被移动或删除），请重新扫描目录")
                job = new_job("ai", len(paths))
                threading.Thread(target=run_ai_job,
                                 args=(job, paths, load_config(), bool(body.get("offline"))),
                                 daemon=True).start()
                return self._json({"ok": True, "job_id": job["id"], "total": len(paths)})
            if p == "/api/process/job":
                items = body.get("items") or []
                root = os.path.abspath(body.get("root") or (os.path.dirname(items[0]["path"]) if items else ""))
                if not items:
                    raise ValueError("请先选择要处理的图片")
                job = new_job("process", len(items))
                threading.Thread(target=run_process_job,
                                 args=(job, root, items, body.get("options") or {}),
                                 daemon=True).start()
                return self._json({"ok": True, "job_id": job["id"], "total": len(items)})
            if p == "/api/job/cancel":
                jid = body.get("id") or ""
                with _jobs_lock:
                    j = _jobs.get(jid)
                    if j:
                        j["cancel"] = True
                return self._json({"ok": bool(j)})
            if p == "/api/reveal":
                target = os.path.abspath(body.get("path") or "")
                if not os.path.exists(target):
                    raise ValueError("路径不存在")
                os.startfile(target if os.path.isdir(target) else os.path.dirname(target))
                return self._json({"ok": True})
            return self._json({"error": "未知接口"}, 404)
        except Exception as exc:
            return self._error(exc)


def load_manifest(path: str = "") -> dict:
    """读取数据集来源清单（文件名 → 行），用于把作者/授权/来源页带进元数据。"""
    candidates = [path] if path else [
        os.path.join(os.path.dirname(BASE_DIR), "dataset", "来源清单.csv"),
        os.path.join(BASE_DIR, "dataset", "来源清单.csv"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8-sig", newline="") as f:
                    return {row.get("文件名", ""): row for row in csv.DictReader(f)}
            except Exception as exc:
                log(f"读取来源清单失败：{exc}")
    return {}


def autofill_meta(path: str, root: str) -> dict:
    """按所在目录名（如 猫-卧姿）推断类别、动物、姿态、编号。"""
    try:
        rel = os.path.relpath(path, root) if root else os.path.basename(path)
    except ValueError:
        rel = os.path.basename(path)
    parts = rel.replace("/", os.sep).split(os.sep)
    folder = parts[-2] if len(parts) >= 2 else ""
    stem = os.path.splitext(os.path.basename(path))[0]
    animal = "猫" if "猫" in folder else ("狗" if "狗" in folder else "")
    pose = "非卧姿" if "非卧姿" in folder else ("卧姿" if "卧姿" in folder else "")
    seq = re.search(r"(\d+)$", stem)
    no = seq.group(1) if seq else ""
    title = " ".join(x for x in (folder, no) if x) or stem
    desc = (f"{animal}照片，{pose}，编号 {no}，来自 Wikimedia Commons 开放授权图库。"
            if animal and pose else f"{stem}，图片素材。")
    custom = {"数据集": "猫狗姿态图片集"}
    if animal:
        custom["动物"] = animal
    if pose:
        custom["姿态"] = pose
    if folder:
        custom["类别"] = folder
    if no:
        custom["编号"] = no
    return core.norm_meta({
        "title": title, "description": desc,
        "alt": f"{animal}{pose}照片" if animal else title,
        "keywords": [x for x in (animal, pose, "动物照片", "开放授权") if x],
        "category": folder or "未分类", "custom": custom, "source": "manual",
    })


def apply_manifest(meta: dict, row: dict) -> dict:
    """把清单里的作者/授权/来源页并入元数据。"""
    if not row:
        return meta
    if row.get("作者"):
        meta["author"] = str(row["作者"])[:120]
    if row.get("授权"):
        meta["copyright"] = str(row["授权"])
    meta["custom"] = {**(meta.get("custom") or {}),
                      "来源": "Wikimedia Commons",
                      "来源页": row.get("Commons 来源页", ""),
                      "授权": row.get("授权", ""),
                      "授权链接": row.get("授权链接", ""),
                      "原图尺寸": row.get("原始尺寸", ""),
                      "图片直链": row.get("图片直链", "")}
    return meta


def browse(path: str) -> dict:
    """本地目录浏览：空路径返回盘符与常用目录。"""
    path = path.strip().strip('"')
    if not path:
        drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
        home = os.path.expanduser("~")
        quick = [{"name": n, "path": p} for n, p in [
            ("桌面", os.path.join(home, "Desktop")),
            ("下载", os.path.join(home, "Downloads")),
            ("图片", os.path.join(home, "Pictures")),
            ("文档", os.path.join(home, "Documents")),
            ("工作台收件箱", INBOX_DIR),
        ] if os.path.isdir(p)]
        return {"path": "", "parent": "", "drives": drives, "quick": quick, "dirs": []}
    if not os.path.isdir(path):
        raise NotADirectoryError(f"目录不存在：{path}")
    dirs = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not name.startswith(".") and not name.startswith("_ImageMeta"):
                dirs.append({"name": name, "path": full})
    except PermissionError as exc:
        raise PermissionError(f"无权访问该目录：{exc}")
    img_count = sum(1 for n in os.listdir(path) if core.ext_of(n) in core.IMAGE_EXTS)
    parent = os.path.dirname(path.rstrip("\\/")) or ""
    if parent and os.path.abspath(parent) == os.path.abspath(path):
        parent = ""
    return {"path": os.path.abspath(path), "parent": parent,
            "drives": [], "quick": [], "dirs": dirs, "image_count": img_count}


def upload_files(files: list[dict]) -> dict:
    os.makedirs(INBOX_DIR, exist_ok=True)
    saved = []
    for f in files:
        name = os.path.basename(str(f.get("name") or "")).replace("\\", "_").replace("/", "_")
        if core.ext_of(name) not in core.IMAGE_EXTS:
            continue
        data = str(f.get("data") or "")
        if "," in data[:100] and data.strip().startswith("data:"):
            data = data.split(",", 1)[1]
        raw = base64.b64decode(data, validate=False)
        if len(raw) > 40 * 1024 * 1024:
            continue
        target = os.path.join(INBOX_DIR, name)
        stem, ext = os.path.splitext(target)
        i = 2
        while os.path.exists(target):
            target = f"{stem}_{i}{ext}"
            i += 1
        with open(target, "wb") as out:
            out.write(raw)
        saved.append(target)
    return {"ok": True, "root": INBOX_DIR, "saved": len(saved)}


def main(open_browser: bool = True):
    if sys.stdout is None or sys.stderr is None:  # pythonw.exe 下无控制台
        try:
            fh = open(LOG_PATH, "a", encoding="utf-8")
            if sys.stdout is None:
                sys.stdout = fh
            if sys.stderr is None:
                sys.stderr = fh
        except Exception:
            pass
    os.makedirs(WEB_DIR, exist_ok=True)
    os.makedirs(INBOX_DIR, exist_ok=True)
    load_config()
    url = f"http://127.0.0.1:{PORT}"
    if port_busy(PORT):
        log("已有工作台实例在运行，直接打开浏览器，不重复启动服务")
        if open_browser:
            webbrowser.open(url)
        return
    try:
        server = LocalHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        log(f"端口 {PORT} 绑定失败：{exc}；仅打开浏览器")
        if open_browser:
            webbrowser.open(url)
        return
    log(f"服务已启动 {url}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
