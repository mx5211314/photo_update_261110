# -*- coding: utf-8 -*-
"""ImageMeta Studio 回归自测：核心层 + 任务层 + HTTP 接口层。

运行：python selftest.py
不会修改用户真实配置（测试期间把 config/inbox 重定向到临时目录）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import core
import server

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  → " + str(detail)[:180]) if detail else ""))
    return ok


def md5(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def make_fixtures(root: str) -> dict:
    from PIL import Image
    os.makedirs(os.path.join(root, "子文件夹"), exist_ok=True)
    files = {}
    p = os.path.join(root, "城市 夜景.jpg")
    Image.new("RGB", (800, 600), (32, 88, 160)).save(p, "JPEG"); files["jpg"] = p
    p = os.path.join(root, "screenshot_2026-11-10.png")
    Image.new("RGBA", (400, 900), (20, 180, 120, 128)).save(p, "PNG"); files["png"] = p
    p = os.path.join(root, "宽屏.webp")
    Image.new("RGB", (1920, 1080), (200, 60, 60)).save(p, "WEBP"); files["webp"] = p
    p = os.path.join(root, "扫描件.tiff")
    Image.new("RGB", (640, 480), (240, 220, 20)).save(p, "TIFF"); files["tiff"] = p
    p = os.path.join(root, "子文件夹", "老素材.bmp")
    Image.new("RGB", (300, 200), (10, 10, 10)).save(p, "BMP"); files["bmp"] = p
    p = os.path.join(root, "损坏.jpg")
    with open(p, "wb") as f:
        f.write(b"this is not an image")
    files["bad"] = p
    return files


class MockLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        payload = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "title": "模型生成的标题", "description": "模型生成的中文描述内容",
                "alt": "替代文本", "keywords": ["夜景", "城市", "模型"],
                "category": "风景"}, ensure_ascii=False)}}]
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def http_json(base: str, path: str, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, raw


def wait_job(base: str, jid: str, timeout: float = 40) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, j = http_json(base, "/api/job?id=" + jid)
        if j.get("status") != "running":
            return j
        time.sleep(0.12)
    raise TimeoutError("任务超时：" + jid)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="imsmeta_test_")
    src = os.path.join(tmp, "输入图片")
    out_root = os.path.join(tmp, "输出区")
    files = make_fixtures(src)
    orig_hash = {k: (v, md5(v)) for k, v in files.items() if k != "bad"}

    server.CONFIG_PATH = os.path.join(tmp, "config.json")
    server.INBOX_DIR = os.path.join(tmp, "inbox")
    server.LOG_PATH = os.path.join(tmp, "server.log")

    meta = core.norm_meta({
        "title": "城市夜景 & 测试", "description": "中文描述，含特殊字符 <标签> & 引号\"'",
        "keywords": ["夜景", "城市", "测试"], "author": "Xin",
        "copyright": "© 2026 Xin", "alt": "夜景替代文本", "category": "风景",
        "custom": {"规格": "1080*720", "项目": "CES"}, "source": "manual",
    })

    print("\n=== A. 核心层 ===")
    items = core.scan_images(src, True)
    check("扫描递归识别多格式（含中文名/空格/子目录）", len(items) == 6, f"共 {len(items)} 个")
    check("损坏文件被标记且不影响其他文件", sum(1 for i in items if i["error"]) == 1,
          [i["name"] for i in items if i["error"]])
    check("缩略图为有效 JPEG", core.make_thumb(files["jpg"], 120)[:2] == b"\xff\xd8")
    check("离线兜底元数据可用", bool(core.fallback_meta(files["png"])["title"]))

    packet = core.build_xmp_packet(meta, 1080, 720)
    xml = packet[packet.index("<x:xmpmeta"): packet.index("</x:xmpmeta>") + len("</x:xmpmeta>")]
    try:
        ET.fromstring(xml); xml_ok = True
    except Exception as exc:
        xml_ok = False
    check("XMP 是合法 XML 且含自定义字段", xml_ok and "<ims:规格>1080*720</ims:规格>" in packet)

    r = core.save_processed(files["jpg"], os.path.join(out_root, "a.jpg"), meta,
                             {"width": 1080, "height": 720, "fit": "contain"})
    back = core.read_meta(r["output"])
    check("JPEG 1080×720 补边尺寸精确", r["new_size"] == [1080, 720])
    check("JPEG 中文标题/描述/关键词回读", back["title"] == meta["title"] and
          back["description"] == meta["description"] and back["keywords"] == meta["keywords"],
          f"{back['title']!r} / {back['description']!r}")
    check("JPEG 版权含非 ASCII 字符正确回读", back["copyright"] == meta["copyright"], back["copyright"])
    check("JPEG 自定义字段与分类回读", back["custom"] == meta["custom"] and back["category"] == "风景")
    check("JPEG 同时含 EXIF 与 XMP", back.get("has_exif") and back.get("has_xmp"))

    r2 = core.save_processed(r["output"], os.path.join(out_root, "a2.jpg"), meta, {})
    with open(r2["output"], "rb") as f:
        raw = f.read()
    check("重复写 XMP 不产生重复段", raw.count(b"http://ns.adobe.com/xap/1.0/\x00") == 1)
    check("重复写保留唯一 EXIF APP1", raw.count(b"Exif\x00\x00") == 1)

    rp = core.save_processed(files["png"], os.path.join(out_root, "b.png"), meta,
                             {"width": 1080, "height": 720, "fit": "cover", "format": "png"})
    bp = core.read_meta(rp["output"])
    check("透明 PNG 裁剪铺满 + 元数据", rp["new_size"] == [1080, 720] and bp["title"] == meta["title"])

    rw = core.save_processed(files["webp"], os.path.join(out_root, "c.webp"), meta,
                             {"width": 1080, "height": 720, "fit": "stretch", "format": "webp"})
    bw = core.read_meta(rw["output"])
    check("WebP 拉伸 + 元数据", rw["new_size"] == [1080, 720] and bw["copyright"] == meta["copyright"])

    rb = core.save_processed(files["bmp"], os.path.join(out_root, "d.png"), meta, {})
    check("BMP 自动转 PNG 并提示", rb["output"].lower().endswith(".png") and bool(rb["notes"]),
          rb["notes"])
    rt = core.save_processed(files["tiff"], os.path.join(out_root, "e.tiff"), meta, {})
    check("TIFF 支持写入", rt["ok"] and os.path.isfile(rt["output"]))
    try:
        core.save_processed(files["jpg"], os.path.join(out_root, "x.jpg"), meta,
                            {"width": -5, "height": 720, "fit": "contain"})
        bad_dim = False
    except ValueError:
        bad_dim = True
    check("非法尺寸被拒绝", bad_dim)
    check("核心处理未改动任何原图", all(md5(v) == h for v, h in orig_hash.values()))

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockLLM)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_url = f"http://127.0.0.1:{mock.server_address[1]}/v1"
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    cfg["ai"]["base_url"] = mock_url
    cfg["ai"]["api_key"] = "test-key"
    m_ai, err_ai = core.ai_generate_one(files["jpg"], cfg)
    check("AI（mock 视觉接口）成功生成中文元数据",
          m_ai["source"] == "ai" and m_ai["title"] == "模型生成的标题" and not err_ai, err_ai)
    ok_ping, ping_msg = core.ping_llm(cfg)
    check("AI 连通性测试", ok_ping, ping_msg)
    cfg_bad = json.loads(json.dumps(cfg))
    cfg_bad["ai"]["base_url"] = "http://127.0.0.1:1/v1"
    cfg_bad["ai"]["timeout"] = 3
    m_fb, err_fb = core.ai_generate_one(files["jpg"], cfg_bad)
    check("AI 接口失败自动离线兜底且记录原因",
          m_fb["source"] == "offline" and bool(err_fb), err_fb[:80])

    print("\n=== B. 任务层 ===")
    j1 = server.new_job("ai", 3)
    server.run_ai_job(j1, [files["jpg"], files["png"], files["webp"]], cfg, offline_only=True)
    check("离线补全任务完成", j1["status"] == "done" and j1["done"] == 3 and
          all(x["meta"]["source"] == "offline" for x in j1["results"]))

    sitems = [{"path": i["path"], "meta": meta} for i in items if not i["error"]]
    sitems.append({"path": files["bad"], "meta": meta})  # 故意混入损坏文件，验证失败隔离
    j2 = server.new_job("process", len(sitems))
    server.run_process_job(j2, src, sitems, {
        "width": 1080, "height": 720, "fit": "contain", "format": "keep",
        "quality": 90, "suffix": "_meta", "out_dir": out_root,
        "keep_tree": True, "sidecar": True,
    })
    s = j2["summary"]
    check("批量处理成功 5 张 / 失败 1 张（失败隔离）",
          s.get("success") == 5 and s.get("failed") == 1, str(s))
    check("输出不覆盖原图（原文件哈希未变）",
          all(md5(v) == h for v, h in orig_hash.values()))
    check("台账 CSV / JSON 生成且可解析",
          os.path.isfile(s["sidecar"].get("csv", "")) and
          isinstance(json.load(open(s["sidecar"]["json"], encoding="utf-8"))["items"], list))
    outs = [x for x in j2["results"] if x["ok"]]
    check("输出尺寸统一为 1080×720",
          all(x["new_size"] == [1080, 720] for x in outs),
          str([x["new_size"] for x in outs]))
    check("输出元数据可回读",
          all(core.read_meta(x["output"])["title"] == meta["title"] for x in outs))
    op = server._output_path(files["jpg"], src, src, {"suffix": "", "keep_tree": True})
    check("输出目录等于输入目录时自动改名避让", os.path.abspath(op) != os.path.abspath(files["jpg"]), op)
    reserved = set()
    o1 = server._output_path(os.path.join(src, "甲", "同名.png"), src, out_root,
                            {"suffix": "", "keep_tree": False}, reserved)
    o2 = server._output_path(os.path.join(src, "乙", "同名.png"), src, out_root,
                            {"suffix": "", "keep_tree": False}, reserved)
    check("同名文件并发输出路径预分配不冲突", o1 != o2, f"{os.path.basename(o1)} / {os.path.basename(o2)}")
    op_suffix = server._output_path(files["jpg"], src, os.path.join(tmp, "suffix_out"),
                                    {"suffix": ".jpg", "keep_tree": False})
    check("误填 .jpg 后缀不会产生双重扩展名",
          not op_suffix.lower().endswith(".jpg.jpg"), os.path.basename(op_suffix))
    j3 = server.new_job("process", 2)
    ghost = os.path.join(src, "已被移走的图.png")
    server.run_process_job(j3, src,
                           [{"path": ghost, "meta": meta}, {"path": files["jpg"], "meta": meta}],
                           {"width": 0, "height": 0, "fit": "none", "format": "keep",
                            "suffix": "", "out_dir": os.path.join(tmp, "out_missing"),
                            "keep_tree": False, "sidecar": False})
    f3 = [x for x in j3["results"] if not x["ok"]]
    check("原图失效时给出可读中文原因且不影响同批其他图片",
          len(f3) == 1 and "重新扫描" in f3[0]["error"] and j3["summary"]["success"] == 1,
          f3[0]["error"][:70] if f3 else "无失败项")

    nd = server.normalize_dimensions({"width": 1000, "height": 0, "ratio": "16:9"})
    check("16:9 锁定自动算高（与前端 Math.round 一致）", nd["height"] == 563, nd)
    nd2 = server.normalize_dimensions({"width": 0, "height": 720, "ratio": "16:9"})
    check("只填高度时按 16:9 反算宽度", nd2["width"] == 1280, nd2)
    nd3 = server.normalize_dimensions({"width": 800, "height": 600, "ratio": "free"})
    check("自由比例不改写用户尺寸", nd3["width"] == 800 and nd3["height"] == 600)
    j4 = server.new_job("process", 1)
    server.run_process_job(j4, src, [{"path": files["png"], "meta": meta}],
                           {"width": 1280, "height": 0, "ratio": "16:9", "fit": "cover",
                            "format": "jpg", "quality": 90, "suffix": "",
                            "out_dir": os.path.join(tmp, "out_169"), "keep_tree": False,
                            "sidecar": False})
    r4 = j4["results"][0] if j4["results"] else {}
    check("竖版原图按 16:9 锁定裁剪铺满输出 1280×720",
          j4["summary"]["success"] == 1 and r4.get("new_size") == [1280, 720],
          str(r4.get("new_size")))

    af = server.autofill_meta(os.path.join(src, "猫-卧姿", "猫-卧姿_007.jpg"), src)
    check("按目录名自动填充类别/姿态/编号",
          af["category"] == "猫-卧姿" and af["custom"].get("姿态") == "卧姿"
          and af["custom"].get("编号") == "007" and "猫" in af["keywords"], str(af)[:120])
    mf = {"文件名": "x.jpg", "作者": "Someone", "授权": "CC0",
          "Commons 来源页": "https://commons.wikimedia.org/wiki/File:x.jpg",
          "授权链接": "https://creativecommons.org/publicdomain/zero/1.0/",
          "原始尺寸": "1920×1080", "图片直链": "https://upload.wikimedia.org/x.jpg"}
    am = server.apply_manifest(server.autofill_meta("x.jpg", src), mf)
    check("来源清单合并作者/授权/来源",
          am["author"] == "Someone" and am["copyright"] == "CC0"
          and am["custom"]["来源"] == "Wikimedia Commons")

    # 模拟 WorkBuddy / 系统注入了失效代理：本机网关仍应直连成功
    old_proxy = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
    try:
        mp, ep = core.ai_generate_one(files["jpg"], cfg)
        check("失效环境代理下本机网关仍直连成功", mp["source"] == "ai" and not ep, ep[:100])
    finally:
        for k, v in old_proxy.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    mock.shutdown()
    mock.server_close()

    print("\n=== C. HTTP 接口层 ===")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        st, h = http_json(base, "/api/health")
        check("健康检查", st == 200 and h.get("ok"))
        st, html = http_json(base, "/")
        check("首页可访问", st == 200 and b"ImageMeta" in (html if isinstance(html, bytes) else b""))
        ok_static = True
        for p in ("/app.js", "/style.css", "/favicon.svg"):
            try:
                st, _ = http_json(base, p)
                ok_static &= (st == 200)
            except Exception:
                ok_static = False
        check("静态资源齐全", ok_static)
        st, c = http_json(base, "/api/config")
        check("配置接口不回显密钥", c["ai"]["api_key"] == "" and "api_key_set" in c["ai"])
        st, c = http_json(base, "/api/config", {"ai": {"api_key": "secret-xyz", "model": "vision-test"}})
        check("配置保存后仅返回脱敏状态", c["config"]["ai"]["api_key"] == "" and c["config"]["ai"]["api_key_set"])
        st, b = http_json(base, "/api/browse?path=")
        check("目录浏览返回磁盘与快捷位置", bool(b["drives"]) and len(b["quick"]) >= 3)
        st, sc = http_json(base, "/api/scan", {"root": src, "recursive": True})
        check("扫描接口", sc["ok"] and len(sc["items"]) == 6)
        try:
            req = urllib.request.Request(base + "/api/thumb?path=" +
                                          urllib.parse.quote(files["jpg"]) + "&w=100")
            with urllib.request.urlopen(req, timeout=10) as resp:
                thumb = resp.read()
            check("缩略图接口", thumb[:2] == b"\xff\xd8")
        except Exception as exc:
            check("缩略图接口", False, str(exc))
        st, u = http_json(base, "/api/upload", {"files": [{
            "name": "拖入的截图.png",
            "data": "data:image/png;base64," + __import__("base64").b64encode(
                open(files["png"], "rb").read()).decode()}]})
        check("拖拽上传接口", u["ok"] and u["saved"] == 1)
        st, v = http_json(base, "/api/verify",
                          {"paths": [files["jpg"], os.path.join(src, "不存在.png")]})
        check("转换前校验接口能识别失效原图", v["total"] == 2 and len(v["missing"]) == 1)
        st, a = http_json(base, "/api/autofill", {"root": src, "paths": [files["jpg"]]})
        check("自动填充接口返回元数据", a["ok"] and len(a["items"]) == 1
              and isinstance(a["items"][0]["meta"]["keywords"], list))
        probe = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=probe.serve_forever, daemon=True).start()
        try:
            check("端口占用检测可用（防重复启动）", server.port_busy(probe.server_address[1]))
            check("服务类禁止多进程共享端口", server.LocalHTTPServer.allow_reuse_address is False)
        finally:
            probe.shutdown(); probe.server_close()
        st, j = http_json(base, "/api/ai/job", {"paths": [files["jpg"], files["png"]], "offline": True})
        jf = wait_job(base, j["job_id"])
        check("AI 任务接口 + 轮询", jf["status"] == "done" and jf["done"] == 2)
        st, j = http_json(base, "/api/process/job", {
            "root": src,
            "items": [{"path": files["jpg"], "meta": meta}, {"path": files["png"], "meta": meta}],
            "options": {"width": 1080, "height": 720, "fit": "contain", "format": "keep",
                        "quality": 90, "suffix": "_http", "out_dir": out_root,
                        "keep_tree": True, "sidecar": False}})
        jf = wait_job(base, j["job_id"])
        check("处理任务接口 + 轮询", jf["status"] == "done" and jf["summary"]["success"] == 2,
              str(jf["summary"]))
        st, cc = http_json(base, "/api/job/cancel", {"id": j["job_id"]})
        check("取消接口幂等", st == 200)
    finally:
        httpd.shutdown(); httpd.server_close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n===== 汇总：{passed}/{len(RESULTS)} 项通过 =====")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  未通过：{name}  {detail}")
    print("临时测试目录：" + tmp)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
