# -*- coding: utf-8 -*-
"""
refresh.py: 一次完整的看板刷新, 供计划任务调用.

    渲染 epd_update.py -> epd.png
    推屏 host/epdash.py -> 墨水屏

日志写到 refresh.log, 每次运行追加, 便于排查.

用法:
    python refresh.py            # 正常刷新
    python refresh.py --full     # 强制整屏
    python refresh.py --render   # 只渲染不推屏
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
PNG = BASE / "epd.png"
LOG = BASE / "refresh.log"


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run(cmd, timeout):
    log("运行 " + " ".join(str(x) for x in cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT), timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if out:
        for line in out.splitlines():
            log("  " + line)
    if proc.returncode != 0:
        raise RuntimeError("返回码 %d" % proc.returncode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()

    started = time.time()
    try:
        run([sys.executable, str(BASE / "epd_update.py"), str(PNG)], 180)
        if args.render:
            log("只渲染, 不推屏")
            return
        cmd = [sys.executable, str(ROOT / "host" / "epdash.py"), str(PNG)]
        if args.full:
            cmd.append("--full")
        for attempt in (1, 2):
            try:
                run(cmd, 240)
                break
            except Exception as exc:
                if attempt == 2:
                    raise
                log("推屏失败, 5 秒后重试: %s" % exc)
                time.sleep(5)
        log("完成, 用时 %.1f 秒" % (time.time() - started))
    except Exception as exc:
        log("失败: %s" % exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
