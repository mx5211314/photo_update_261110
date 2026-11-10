# -*- coding: utf-8 -*-
"""双击启动：无黑窗，自动打开浏览器。"""
import os
import sys
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

try:
    import server
    server.main(open_browser=True)
except Exception:
    try:
        with open(os.path.join(BASE, "启动错误.log"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
    except Exception:
        pass
