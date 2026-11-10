# -*- coding: utf-8 -*-
"""数据集质量复核辅助工具。

用法：
  python dataset_qc.py sheet              # 为每个类别生成 50 张一版的复核拼图
  python dataset_qc.py clean 猫-卧姿 002 005 008   # 删除复核发现的异常图，并同步清理清单
  python dataset_qc.py stat              # 统计各类数量与尺寸分布
删除后重跑 dataset_builder.py 会自动补齐到每类 100 张。
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys
import time

from PIL import Image, ImageDraw, ImageOps

from dataset_builder import CLASSES, OUT_ROOT, TARGET_PER_CLASS, write_csv

# 拼图放在数据集目录外，避免被工作台扫描当成素材
QC_DIR = os.path.join(os.path.dirname(OUT_ROOT), "_qc_review")

FIELDS = ["序号", "类别", "动物", "姿态", "文件名", "相对路径", "Commons 来源页",
          "图片直链", "授权", "授权链接", "作者", "原始尺寸", "输出尺寸", "字节"]


def _state_path() -> str:
    return os.path.join(OUT_ROOT, "_state.json")


def make_sheets(per_sheet: int = 50) -> None:
    os.makedirs(QC_DIR, exist_ok=True)
    made = []
    for cls in CLASSES:
        files = sorted(glob.glob(os.path.join(OUT_ROOT, cls, "*.jpg")))
        for start in range(0, len(files), per_sheet):
            batch = files[start:start + per_sheet]
            cols, cw, ch = 10, 170, 140
            rows = (len(batch) + cols - 1) // cols
            sheet = Image.new("RGB", (cols * cw, rows * ch), (24, 26, 32))
            dr = ImageDraw.Draw(sheet)
            for i, f in enumerate(batch):
                gx, gy = (i % cols) * cw, (i // cols) * ch
                dr.text((gx + 6, gy + 4), os.path.basename(f).split("_")[-1].replace(".jpg", ""),
                        fill=(255, 255, 255))
                try:
                    with Image.open(f) as src:
                        im = ImageOps.exif_transpose(src).convert("RGB")
                        im.thumbnail((cw - 8, ch - 26), Image.Resampling.LANCZOS)
                    sheet.paste(im, (gx + (cw - im.width) // 2, gy + 20))
                except Exception as exc:
                    dr.text((gx + 8, gy + 50), "读取中", fill=(255, 120, 120))
            out = os.path.join(QC_DIR, f"复核_{cls}_{start // per_sheet + 1}.jpg")
            sheet.save(out, "JPEG", quality=88)
            made.append((out, len(batch)))
    for p, n in made:
        print(f"  {p}  ({n} 张)")


def clean(cls: str, numbers: list[str]) -> None:
    if cls not in CLASSES:
        raise SystemExit(f"未知类别：{cls}")
    removed = []
    for no in numbers:
        try:
            name = f"{cls}_{int(no):03d}.jpg"
        except ValueError:
            continue
        p = os.path.join(OUT_ROOT, cls, name)
        if os.path.isfile(p):
            os.remove(p)
            removed.append(name)
    sp = _state_path()
    state = {"used_titles": [], "records": []}
    if os.path.isfile(sp):
        with open(sp, "r", encoding="utf-8") as f:
            state = json.load(f)
    before = len(state.get("records", []))
    state["records"] = [r for r in state.get("records", []) if r.get("文件名") not in removed]
    with open(sp + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(sp + ".tmp", sp)
    write_csv(os.path.join(OUT_ROOT, "来源清单.csv"), state["records"])
    with open(os.path.join(OUT_ROOT, "来源清单.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "source": "Wikimedia Commons（开放授权）",
                   "items": state["records"]}, f, ensure_ascii=False, indent=2)
    left = len(glob.glob(os.path.join(OUT_ROOT, cls, "*.jpg")))
    print(f"[{cls}] 删除 {len(removed)} 张：{', '.join(removed) or '无'}")
    print(f"  清单记录 {before} → {len(state['records'])}，剩余 {left}/{TARGET_PER_CLASS} 张")


def stat() -> None:
    from PIL import Image
    for cls in CLASSES:
        files = sorted(glob.glob(os.path.join(OUT_ROOT, cls, "*.jpg")))
        widths = []
        for f in files:
            with Image.open(f) as im:
                widths.append(im.width)
        print(f"{cls:10s} {len(files):3d}/{TARGET_PER_CLASS} 张 | "
              f"宽度 {min(widths) if widths else 0}-{max(widths) if widths else 0}px | "
              f"合计 {sum(os.path.getsize(f) for f in files) / 1048576:.1f} MB")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "sheet":
        make_sheets()
    elif sys.argv[1] == "stat":
        stat()
    elif sys.argv[1] == "clean" and len(sys.argv) >= 4:
        clean(sys.argv[2], sys.argv[3:])
    else:
        print(__doc__)
