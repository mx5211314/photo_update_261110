# -*- coding: utf-8 -*-
"""ImageMeta Studio 核心逻辑。

职责：图片扫描 / 元数据读写 / 尺寸处理 / AI 打标。
不依赖 Web 框架，便于独立测试。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from html import unescape
from xml.sax.saxutils import escape as _xml_escape

from PIL import Image, ImageOps, PngImagePlugin

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".jfif"}
# 可内嵌元数据的格式；bmp/gif 不支持，会自动转存 PNG
EMBEDDABLE = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}

TOOL_NAME = "ImageMeta Studio"
XMP_NS = "http://ns.adobe.com/xap/1.0/"
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
# TIFF/EXIF 里描述像素布局的结构标签：改写尺寸后必须丢弃，否则 libtiff 会报编码配置冲突
STRUCT_TAGS = {256, 257, 258, 259, 262, 273, 277, 278, 279, 284,
               317, 320, 322, 323, 330, 339, 34665, 34853}

DEFAULT_PROMPT = (
    "你是图片素材管理助手。分析这张图片，输出严格的 JSON 对象（不要 Markdown 代码块、不要额外文字），字段如下：\n"
    '{"title":"不超过20字的中文标题",'
    '"description":"60-120字的客观画面描述",'
    '"alt":"一句话的无障碍替代文本",'
    '"keywords":["5到8个中文关键词"],'
    '"category":"一个分类词"}\n'
    "只描述画面中可见的内容，不要臆测。文件名参考：{filename}"
)

DEFAULT_CONFIG = {
    "ai": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "concurrency": 3,
        "timeout": 60,
        "proxy_mode": "auto",  # auto=本机网关直连/外网走环境代理, direct=全直连, env=全走环境代理
        "prompt": DEFAULT_PROMPT,
    },
    "defaults": {
        "author": "",
        "copyright": "",
        "width": 0,
        "height": 0,
        "ratio": "free",
        "fit": "contain",
        "bg": "#FFFFFF",
        "format": "keep",
        "quality": 92,
        "suffix": "",
        "sidecar": True,
        "keep_tree": True,
    },
}


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def deep_merge(base: dict, patch: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def norm_meta(raw: dict | None) -> dict:
    """把任意来源（AI / 手工 / 文件已有）的元数据归一化。"""
    raw = raw or {}
    m = {k: str(raw.get(k) or "").strip() for k in
         ("title", "description", "alt", "author", "copyright", "category")}
    kw = raw.get("keywords", [])
    if isinstance(kw, str):
        kw = [x.strip() for x in re.split(r"[;,，；、|\n]+", kw) if x.strip()]
    m["keywords"] = [str(x).strip() for x in kw if str(x).strip()][:30]
    custom = raw.get("custom") or {}
    if isinstance(custom, str):
        try:
            custom = json.loads(custom)
        except Exception:
            custom = {}
    if isinstance(custom, list):
        custom = {str(d.get("key", "")): str(d.get("value", "")) for d in custom if isinstance(d, dict)}
    m["custom"] = {str(k): str(v) for k, v in custom.items() if str(k).strip()}
    m["source"] = str(raw.get("source") or "manual")
    return m


def _decode_exif_value(v, wide: bool = False) -> str:
    """EXIF 值解码。XP* 系列标签固定为 UTF-16LE 字节串，需 wide=True。"""
    if v is None:
        return ""
    if isinstance(v, bytes):
        if wide:
            for enc in ("utf-16le", "utf-8", "latin-1"):
                try:
                    return v.decode(enc).rstrip("\x00").strip()
                except Exception:
                    continue
            return ""
        for enc in ("utf-8", "latin-1"):
            try:
                s = v.decode(enc)
            except Exception:
                continue
            if s.startswith("UNICODE\x00"):
                try:
                    return s[8:].encode("latin-1").decode("utf-16le").rstrip("\x00").strip()
                except Exception:
                    return s[8:].strip()
            return s.rstrip("\x00").strip()
    return str(v).strip()


def _xml_tag(name: str) -> str:
    """把自定义字段名转成合法 XML 元素名。"""
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff.-]", "_", str(name)).strip(".-")
    if not s or s[0].isdigit() or s[0] in ".-":
        s = "f_" + s
    return s


# --------------------------------------------------------------------------- #
# JPEG XMP APP1 段读写
# --------------------------------------------------------------------------- #
def _jpeg_app_segments(data: bytes):
    """返回 (非 APPn 段起始位置, APPn 段列表)。"""
    if data[:2] != b"\xff\xd8":
        return None, []
    pos = 2
    segs = []
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xD9 or marker == 0xDA:  # EOI / SOS
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if length < 2 or pos + 2 + length > len(data):
            break
        seg = data[pos:pos + 2 + length]
        if 0xE0 <= marker <= 0xEF:
            segs.append(seg)
            pos += 2 + length
        else:
            break
    return pos, segs


def read_jpeg_xmp(data: bytes) -> str:
    pos, segs = _jpeg_app_segments(data)
    if pos is None:
        return ""
    for seg in segs:
        if seg[:2] == b"\xff\xe1" and seg[4:4 + len(XMP_HEADER)] == XMP_HEADER:
            return seg[4 + len(XMP_HEADER):].decode("utf-8", "ignore")
    return ""


def insert_jpeg_xmp(data: bytes, packet: str) -> bytes:
    """把 XMP packet 插入 JPEG；若已有 XMP 段则替换，不破坏 EXIF/ICC 段。"""
    pos, segs = _jpeg_app_segments(data)
    if pos is None:
        return data
    payload = XMP_HEADER + packet.encode("utf-8")
    if len(payload) + 2 > 65533:
        payload = payload[:65531]
    new_seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    kept = [s for s in segs
            if not (s[:2] == b"\xff\xe1" and s[4:4 + len(XMP_HEADER)] == XMP_HEADER)]
    return data[:2] + b"".join(kept) + new_seg + data[pos:]


def build_xmp_packet(meta: dict, width: int | None = None, height: int | None = None) -> str:
    m = norm_meta(meta)
    e = _xml_escape

    def alt(value: str) -> str:
        return (f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{e(value)}</rdf:li>'
                f'</rdf:Alt></dc:title>') if value else ""

    desc = (f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{e(m["description"])}'
            f'</rdf:li></rdf:Alt></dc:description>') if m["description"] else ""
    subject = ""
    if m["keywords"]:
        lis = "".join(f"<rdf:li>{e(k)}</rdf:li>" for k in m["keywords"])
        subject = f"<dc:subject><rdf:Bag>{lis}</rdf:Bag></dc:subject>"
    creator = (f'<dc:creator><rdf:Seq><rdf:li>{e(m["author"])}</rdf:li></rdf:Seq>'
               f'</dc:creator>') if m["author"] else ""
    rights = (f'<dc:rights><rdf:Alt><rdf:li xml:lang="x-default">{e(m["copyright"])}'
              f'</rdf:li></rdf:Alt></dc:rights>') if m["copyright"] else ""
    headline = (f'<photoshop:Headline>{e(m["title"])}</photoshop:Headline>') if m["title"] else ""
    category = (f'<photoshop:Category>{e(m["category"])}</photoshop:Category>') if m["category"] else ""
    alttext = (f'<Iptc4xmpCore:AltTextAccessibility>{e(m["alt"] or m["description"])}'
               f'</Iptc4xmpCore:AltTextAccessibility>') if (m["alt"] or m["description"]) else ""
    dims = ""
    if width and height:
        dims = f'<ims:Width>{width}</ims:Width><ims:Height>{height}</ims:Height>'
    custom = "".join(f"<ims:{_xml_tag(k)}>{e(v)}</ims:{_xml_tag(k)}>"
                     for k, v in m["custom"].items() if v)

    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about="" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
        'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" '
        'xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/2008-02-29/" '
        'xmlns:ims="https://workbuddy.local/imagemeta/1.0/">\n'
        f'   {alt(m["title"])}{desc}{subject}{creator}{rights}{headline}{category}{alttext}'
        f'   {dims}{custom}\n'
        f'   <xmp:CreatorTool>{e(TOOL_NAME)}</xmp:CreatorTool>'
        f'<xmp:ModifyDate>{e(now_iso())}</xmp:ModifyDate>\n'
        '  </rdf:Description>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    )


def parse_xmp_meta(packet: str) -> dict:
    """从已有 XMP 里尽量回读字段（宽松正则，失败不影响主流程）。"""
    if not packet:
        return {}

    def first(tag: str) -> str:
        m = re.search(rf"<(?:dc|photoshop|Iptc4xmpCore):{tag}[^>]*>(.*?)</(?:dc|photoshop|Iptc4xmpCore):{tag}>",
                      packet, re.S)
        if not m:
            return ""
        li = re.search(r"<rdf:li[^>]*>(.*?)</rdf:li>", m.group(1), re.S)
        return (li.group(1) if li else m.group(1)).strip()

    out = {
        "title": first("title"),
        "description": first("description"),
        "copyright": first("rights"),
        "author": first("creator"),
        "category": first("Category"),
    }
    kws = re.findall(r"<dc:subject>.*?</dc:subject>", packet, re.S)
    kws = re.findall(r"<rdf:li[^>]*>(.*?)</rdf:li>", kws[0], re.S) if kws else []
    out["keywords"] = [unescape(re.sub(r"<[^>]+>", "", k)).strip() for k in kws]
    for k, v in out.items():
        if isinstance(v, str):
            out[k] = unescape(re.sub(r"<[^>]+>", "", v)).strip()
    return out


# --------------------------------------------------------------------------- #
# 扫描 & 元数据读取
# --------------------------------------------------------------------------- #
def _read_xmp(path: str, im: Image.Image) -> str:
    ext = ext_of(path)
    try:
        if ext in (".jpg", ".jpeg", ".jfif"):
            with open(path, "rb") as f:
                return read_jpeg_xmp(f.read(2 * 1024 * 1024))
        if ext == ".png":
            v = im.info.get("XML:com.adobe.xmp")
            return v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v or "")
        if ext == ".webp":
            v = im.info.get("xmp")
            return v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v or "")
    except Exception:
        pass
    return ""


def read_meta(path: str) -> dict:
    """读取图片内已存在的元数据（EXIF / PNG 文本块 / XMP）。"""
    info = {"title": "", "description": "", "keywords": [], "author": "",
            "copyright": "", "alt": "", "category": "", "custom": {},
            "has_exif": False, "has_xmp": False}
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            info["has_exif"] = bool(ex)
            xp_title = _decode_exif_value(ex.get(0x9C9B), wide=True)
            xp_comment = _decode_exif_value(ex.get(0x9C9C), wide=True)
            xp_author = _decode_exif_value(ex.get(0x9C9D), wide=True)
            xp_kw = _decode_exif_value(ex.get(0x9C9E), wide=True)
            # XP* 系列是 UTF-16LE，中文可靠；优先于 ASCII 类型的标准 EXIF 字段
            desc = xp_comment or _decode_exif_value(ex.get(270))
            author = xp_author or _decode_exif_value(ex.get(315))
            copyright_ = _decode_exif_value(ex.get(33432))
            kw = [x.strip() for x in re.split(r"[;,，；]+", xp_kw) if x.strip()]

            # PNG / WebP 文本块
            for key, field in (("Title", "title"), ("Description", "description"),
                              ("Author", "author"), ("Copyright", "copyright")):
                v = im.info.get(key)
                if v and not info[field]:
                    info[field] = v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)
            pkw = im.info.get("Keywords")
            if pkw and not kw:
                pkw = pkw.decode("utf-8", "ignore") if isinstance(pkw, bytes) else str(pkw)
                kw = [x.strip() for x in re.split(r"[;,，；]+", pkw) if x.strip()]

            packet = _read_xmp(path, im)
            info["has_xmp"] = bool(packet)
            if packet:
                for tag in ("title", "description", "author"):
                    m = re.search(rf"<dc:{tag}[^>]*>.*?<rdf:li[^>]*>(.*?)</rdf:li>",
                                  packet, re.S)
                    if m and not info[tag]:
                        info[tag] = unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
                m = re.search(r"<dc:rights[^>]*>.*?<rdf:li[^>]*>(.*?)</rdf:li>", packet, re.S)
                if m and not info["copyright"]:
                    info["copyright"] = unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
                m = re.search(r"<dc:subject[^>]*>(.*?)</dc:subject>", packet, re.S)
                if m and not kw:
                    kw = [re.sub(r"<[^>]+>", "", x).strip()
                          for x in re.findall(r"<rdf:li[^>]*>(.*?)</rdf:li>", m.group(1), re.S)]
                m = re.search(r"<photoshop:Category>(.*?)</photoshop:Category>", packet, re.S)
                if m and not info["category"]:
                    info["category"] = unescape(m.group(1)).strip()
                for key, value in re.findall(r"<ims:([^>\s/]+)>(.*?)</ims:\1>", packet, re.S):
                    if key not in ("Width", "Height"):
                        info["custom"][key] = unescape(re.sub(r"<[^>]+>", "", value)).strip()

            cf = im.info.get("CustomFields")
            if cf and not info["custom"]:
                try:
                    cf = cf.decode("utf-8") if isinstance(cf, bytes) else str(cf)
                    info["custom"] = json.loads(cf)
                except Exception:
                    pass

            info.update({
                "title": xp_title or info["title"],
                "description": desc or info["description"],
                "author": author or info["author"],
                "copyright": copyright_ or info["copyright"],
                "keywords": kw,
            })
    except Exception as exc:
        info["read_error"] = str(exc)
    out = norm_meta({**info, "source": "existing"})
    # 诊断字段不参与写入，但要回传给界面展示
    for k in ("has_exif", "has_xmp", "read_error"):
        if k in info:
            out[k] = info[k]
    return out


def scan_images(root: str, recursive: bool = True) -> list[dict]:
    """扫描目录，返回每张图片的基础信息 + 已有元数据。"""
    if not os.path.isdir(root):
        raise NotADirectoryError(f"目录不存在：{root}")
    paths = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not d.startswith("_ImageMeta")]
            for fn in filenames:
                if ext_of(fn) in IMAGE_EXTS:
                    paths.append(os.path.join(dirpath, fn))
    else:
        for fn in os.listdir(root):
            p = os.path.join(root, fn)
            if os.path.isfile(p) and ext_of(fn) in IMAGE_EXTS:
                paths.append(p)
    paths.sort(key=lambda p: p.lower())

    items = []
    for p in paths:
        item = {
            "path": os.path.abspath(p),
            "name": os.path.basename(p),
            "rel": os.path.relpath(p, root),
            "ext": ext_of(p),
            "bytes": 0, "width": 0, "height": 0, "format": "", "mode": "",
            "error": "", "meta": norm_meta({}),
        }
        try:
            item["bytes"] = os.path.getsize(p)
            with Image.open(p) as im:
                item["width"], item["height"] = im.size
                item["format"] = im.format or ""
                item["mode"] = im.mode
                im.verify()  # 触发解码校验，损坏文件会抛错
            item["meta"] = read_meta(p)
        except Exception as exc:
            item["error"] = f"无法读取：{exc}"
        items.append(item)
    return items


def make_thumb(path: str, width: int = 200) -> bytes:
    with Image.open(path) as src:
        im = ImageOps.exif_transpose(src)
        im.thumbnail((width, width), Image.Resampling.LANCZOS)
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (28, 30, 36))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg.paste(im, mask=im.getchannel("A"))
            else:
                bg.paste(im.convert("RGB"))
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=78)
        return buf.getvalue()


# --------------------------------------------------------------------------- #
# 元数据写入
# --------------------------------------------------------------------------- #
def build_exif(base: Image.Exif | None, meta: dict) -> Image.Exif:
    ex = Image.Exif()
    if base:
        try:
            ex.load(base.tobytes())  # 保留原有 EXIF（含子 IFD）
        except Exception:
            for k, v in base.items():
                try:
                    ex[k] = v
                except Exception:
                    pass
        for tag in STRUCT_TAGS:
            ex.pop(tag, None)
    m = norm_meta(meta)
    try:
        # 270/315/33432 是 EXIF 的 ASCII 类型，中文写入会被编码成 '?'。
        # 非 ASCII 内容统一由 XP*（UTF-16LE）与 XMP 承载，这里只写纯 ASCII。
        for tag, value in ((270, m["description"]), (315, m["author"]),
                           (33432, m["copyright"])):
            if value.isascii():  # Pillow 对 ASCII 类型标签用 ascii 编码，非 ASCII 一律跳过
                ex[tag] = value
            else:
                ex.pop(tag, None)
        ex[305] = TOOL_NAME                       # Software
        ex[306] = datetime.now().strftime("%Y:%m:%d %H:%M:%S")  # DateTime
        ex[274] = 1                               # Orientation（已做 exif_transpose）
        pairs = {
            0x9C9B: m["title"],                   # XPTitle
            0x9C9C: m["description"],              # XPComment
            0x9C9D: m["author"],                   # XPArtist
            0x9C9E: "; ".join(m["keywords"]),      # XPKeywords
            0x9C9F: m["alt"] or m["description"], # XPSubject
        }
        for tag, value in pairs.items():
            try:
                ex[tag] = value.encode("utf-16le") if value else b""
            except Exception:
                ex[tag] = value
        comment = (m["title"] + " | " + m["description"]).strip(" |")
        if comment:
            ex[37510] = b"UNICODE\x00" + comment.encode("utf-16le")
    except Exception:
        pass
    return ex


def _build_pnginfo(meta: dict, packet: str) -> PngImagePlugin.PngInfo:
    m = norm_meta(meta)
    png = PngImagePlugin.PngInfo()

    def put(key: str, value: str):
        if not value:
            return
        try:
            value.encode("latin-1")
            png.add_text(key, value)
        except UnicodeEncodeError:
            png.add_itxt(key, value)

    put("Title", m["title"])
    put("Description", m["description"])
    put("Author", m["author"])
    put("Copyright", m["copyright"])
    put("Keywords", "; ".join(m["keywords"]))
    put("Comment", m["alt"] or m["description"])
    put("Software", TOOL_NAME)
    if m["custom"]:
        put("CustomFields", json.dumps(m["custom"], ensure_ascii=False))
    if packet:
        try:
            png.add_itxt("XML:com.adobe.xmp", packet)
        except Exception:
            pass
    return png


def _parse_hex_color(s: str) -> tuple[int, int, int]:
    s = (s or "#FFFFFF").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore
    except Exception:
        return (255, 255, 255)


def apply_resize(im: Image.Image, width: int, height: int, fit: str,
                 bg: str = "#FFFFFF") -> Image.Image:
    """按目标尺寸处理。fit: none / contain / cover / stretch。"""
    if not width or not height or fit == "none":
        return im
    if width <= 0 or height <= 0 or width > 20000 or height > 20000:
        raise ValueError(f"目标尺寸不合法：{width}×{height}")
    tw, th = int(width), int(height)
    ow, oh = im.size
    if (ow, oh) == (tw, th):
        return im

    if fit == "stretch":
        return im.resize((tw, th), Image.Resampling.LANCZOS)

    scale = (tw / ow, th / oh)
    if fit == "cover":
        r = max(*scale)
        nw, nh = max(1, round(ow * r)), max(1, round(oh * r))
        sized = im.resize((nw, nh), Image.Resampling.LANCZOS)
        left, top = (nw - tw) // 2, (nh - th) // 2
        return sized.crop((left, top, left + tw, top + th))

    # contain：等比缩放 + 补边（默认）
    r = min(*scale)
    nw, nh = max(1, round(ow * r)), max(1, round(oh * r))
    sized = im.resize((nw, nh), Image.Resampling.LANCZOS)
    if im.mode in ("RGBA", "LA", "P") or "transparency" in im.info:
        canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        canvas.paste(sized.convert("RGBA"), ((tw - nw) // 2, (th - nh) // 2))
        return canvas
    canvas = Image.new("RGB", (tw, th), _parse_hex_color(bg))
    canvas.paste(sized.convert("RGB"), ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def _flatten_for_jpeg(im: Image.Image, bg: str) -> Image.Image:
    if im.mode in ("RGBA", "LA", "P") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        canvas = Image.new("RGB", im.size, _parse_hex_color(bg))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return im.convert("RGB") if im.mode != "RGB" else im


def save_processed(path: str, out_path: str, meta: dict, opts: dict) -> dict:
    """处理单张图片：尺寸 → 元数据 → 落盘。返回结构化结果。"""
    notes = []
    opts = opts or {}
    width = int(opts.get("width") or 0)
    height = int(opts.get("height") or 0)
    fit = opts.get("fit") or "none"
    bg = opts.get("bg") or "#FFFFFF"
    quality = max(1, min(100, int(opts.get("quality") or 92)))
    fmt_mode = opts.get("format") or "keep"
    src_ext = ext_of(path)

    if fmt_mode == "keep":
        target_ext = src_ext if src_ext in EMBEDDABLE else ".png"
        if target_ext != src_ext:
            notes.append(f"{src_ext} 不支持内嵌元数据，已转存 PNG")
    else:
        target_ext = {"jpg": ".jpg", "png": ".png", "webp": ".webp"}[fmt_mode]

    out_path = os.path.splitext(out_path)[0] + target_ext
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with Image.open(path) as src:
        try:
            base_exif = src.getexif()
            base_copy = Image.Exif()
            base_copy.load(base_exif.tobytes())
        except Exception:
            base_copy = Image.Exif()
        im = ImageOps.exif_transpose(src)
        orig_size = im.size
        im = apply_resize(im, width, height, fit, bg)
        new_size = im.size

    m = norm_meta(meta)
    exif_bytes = build_exif(base_copy, m).tobytes()
    packet = build_xmp_packet(m, *new_size)

    buf = io.BytesIO()
    written = []
    if target_ext in (".jpg", ".jpeg", ".jfif"):
        im = _flatten_for_jpeg(im, bg)
        im.save(buf, "JPEG", quality=quality, exif=exif_bytes, optimize=True)
        with open(out_path, "wb") as f:
            f.write(insert_jpeg_xmp(buf.getvalue(), packet))
        written = ["EXIF", "XMP"]
    elif target_ext == ".png":
        pnginfo = _build_pnginfo(m, packet)
        im.save(buf, "PNG", pnginfo=pnginfo, optimize=True)
        with open(out_path, "wb") as f:
            f.write(buf.getvalue())
        written = ["PNG tEXt/iTXt", "XMP"]
    elif target_ext == ".webp":
        try:
            im.save(buf, "WEBP", quality=quality, exif=exif_bytes, xmp=packet.encode("utf-8"))
            written = ["EXIF", "XMP"]
        except TypeError:
            im.save(buf, "WEBP", quality=quality, exif=exif_bytes)
            written = ["EXIF"]
            notes.append("当前 Pillow 不支持 WebP XMP，已写入 EXIF")
        with open(out_path, "wb") as f:
            f.write(buf.getvalue())
    elif target_ext in (".tif", ".tiff"):
        try:
            im.save(buf, "TIFF", exif=exif_bytes)
            with open(out_path, "wb") as f:
                f.write(buf.getvalue())
            written = ["EXIF"]
            notes.append("TIFF 不写入 XMP 包，元数据以 EXIF + 台账为准")
        except OSError:
            # libtiff 对某些 EXIF 组合仍可能报错：降级转存 PNG，保证批次不中断
            out_path = os.path.splitext(out_path)[0] + ".png"
            buf2 = io.BytesIO()
            im.save(buf2, "PNG", pnginfo=_build_pnginfo(m, packet))
            with open(out_path, "wb") as f:
                f.write(buf2.getvalue())
            written = ["PNG tEXt/iTXt", "XMP"]
            notes.append("TIFF 编码不兼容，已自动转存 PNG（元数据完整保留）")
    else:
        raise ValueError(f"不支持的输出格式：{target_ext}")

    return {
        "ok": True,
        "input": path,
        "output": out_path,
        "orig_size": list(orig_size),
        "new_size": list(new_size),
        "bytes": os.path.getsize(out_path),
        "written": written,
        "notes": notes,
        "custom": m["custom"],
    }


# --------------------------------------------------------------------------- #
# AI 打标
# --------------------------------------------------------------------------- #
def fallback_meta(path: str) -> dict:
    """无 AI 接口时的基础兜底：从文件名 + 图片属性生成元数据。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    title = re.sub(r"[_\-]+", " ", stem)
    title = re.sub(r"\s+", " ", title).strip()[:40] or "未命名图片"
    parts = [p.strip() for p in re.split(r"[\s_\-—·、,，.。()（）\[\]{}]+", stem) if p.strip()]
    keywords = list(dict.fromkeys([p for p in parts if len(p) <= 20]))[:8]
    w = h = 0
    fmt = ext_of(path).lstrip(".").upper()
    try:
        with Image.open(path) as im:
            w, h = im.size
            fmt = im.format or fmt
    except Exception:
        pass
    return norm_meta({
        "title": title,
        "description": f"{title}。{fmt} 格式图片素材，尺寸 {w}×{h} 像素，可用于内容归档与检索。",
        "alt": title,
        "keywords": keywords or ["图片素材"],
        "category": "未分类",
        "source": "offline",
    })


def _image_for_llm(path: str, max_side: int = 1024) -> tuple[str, bytes]:
    with Image.open(path) as src:
        im = ImageOps.exif_transpose(src)
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if im.mode not in ("RGB", "L"):
            im = _flatten_for_jpeg(im, "#FFFFFF")
        elif im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return "image/jpeg", buf.getvalue()


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回 JSON 对象")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    return data


def call_llm(cfg: dict, path: str) -> dict:
    ai = cfg.get("ai", {})
    base = (ai.get("base_url") or "").rstrip("/")
    key = ai.get("api_key") or ""
    model = ai.get("model") or "gpt-4o-mini"
    timeout = int(ai.get("timeout") or 60)
    if not base:
        raise ValueError("未配置 AI 接口地址")
    prompt = (ai.get("prompt") or DEFAULT_PROMPT).replace(
        "{filename}", os.path.basename(path))
    mime, raw = _image_for_llm(path)
    data_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        "temperature": 0.2,
        "max_tokens": 600,
    }
    url = base + ("/chat/completions" if not base.endswith("chat/completions") else "")
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": "ImageMetaStudio/1.0"},
        method="POST",
    )
    # 本机网关必须直连：WorkBuddy / 系统可能注入 HTTP_PROXY，会把 127.0.0.1 请求也塞进代理
    from urllib.parse import urlparse
    host = (urlparse(base).hostname or "").lower()
    mode = str(ai.get("proxy_mode") or "auto").lower()
    local = host in ("localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0")
    if mode == "direct" or (mode == "auto" and local):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"AI 接口 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        extra = "（本机网关请确认服务已启动；可在 AI 设置里把代理策略改成「直连」）" if local else ""
        raise RuntimeError(f"AI 接口连接失败：{exc.reason}{extra}") from exc
    try:
        content = body["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"AI 返回结构异常：{str(body)[:200]}") from exc
    return _extract_json(content)


def ai_generate_one(path: str, cfg: dict, use_fallback: bool = True) -> tuple[dict, str]:
    """返回 (meta, error)。失败时按配置回退到离线元数据。"""
    try:
        raw = call_llm(cfg, path)
        meta = norm_meta({**raw, "source": "ai"})
        if not meta["title"] and not meta["description"]:
            raise ValueError("模型返回内容为空")
        return meta, ""
    except Exception as exc:
        err = str(exc)
        if use_fallback:
            return fallback_meta(path), err
        return norm_meta({}), err


def ping_llm(cfg: dict) -> tuple[bool, str]:
    """连通性测试：发一张纯色小图，避免消耗用户图片。"""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        Image.new("RGB", (32, 32), (200, 210, 220)).save(tmp, "JPEG")
        cfg = json.loads(json.dumps(cfg))
        cfg["ai"]["prompt"] = '只输出 JSON：{"title":"ok","description":"connectivity test","keywords":["test"],"category":"test"}'
        raw = call_llm(cfg, tmp)
        return True, f"连接成功，模型 {cfg['ai'].get('model')} 返回字段：{', '.join(list(raw)[:6])}"
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
