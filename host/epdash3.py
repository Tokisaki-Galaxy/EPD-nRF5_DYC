# -*- coding: utf-8 -*-
"""
三色看板更新器.

- 从 PNG 提取黑白层和红色层
- 红色没变时走黑白窗口局刷, 2.6 秒, 红色像素不被驱动所以保持
- 红色变了才走三色刷新, 整屏或窗口, 17.9 秒
- 三色刷完把黑白画面写回 DTM2 垫底, 再切黑白模式, 保证新旧图一致
- --fast 用自写寄存器波形, 2.6 秒, 但红色驱动力不足

用法:
    python epdash3.py image.png
    python epdash3.py image.png --full
    python epdash3.py image.png --dry-run
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from bleak import BleakClient, BleakScanner

CMD = "62750002-d828-918d-fb46-b6c11c675aec"
W, H = 800, 480
ROW = W // 8
BLOCK = 8
FULL_EVERY = 12

BASE = Path(__file__).resolve().parent
STATE = BASE / ".epdash3_state.npz"

VCOM = [0x00, 0x0F, 0x0F, 0x00, 0x00, 0x01,
        0x00, 0x0F, 0x01, 0x0F, 0x01, 0x02,
        0x00, 0x0F, 0x0F, 0x00, 0x00, 0x01] + [0] * 24
WW = [0x10, 0x0F, 0x0F, 0x00, 0x00, 0x01,
      0x84, 0x0F, 0x01, 0x0F, 0x01, 0x02,
      0x20, 0x0F, 0x0F, 0x00, 0x00, 0x01] + [0] * 24
BB = [0x80, 0x0F, 0x0F, 0x00, 0x00, 0x01,
      0x84, 0x0F, 0x01, 0x0F, 0x01, 0x02,
      0x40, 0x0F, 0x0F, 0x00, 0x00, 0x01] + [0] * 24
RED = [0xCC, 0x0F, 0x0F, 0x0F, 0x0F, 0x01] + [0] * 54


def pkt(cmd, payload=b""):
    return bytes([cmd]) + bytes(payload)


def rle_encode(data):
    """固件 RLE 格式: 高位 1 是重复段, 长度 = 低位 + 3; 高位 0 是字面段, 长度 = 值 + 1."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        j = i
        while j < n and data[j] == data[i] and j - i < 130:
            j += 1
        run = j - i
        if run >= 3:
            out.append(0x80 | (run - 3))
            out.append(data[i])
            i = j
            continue
        start = i
        while i < n and i - start < 128:
            if i + 2 < n and data[i] == data[i + 1] == data[i + 2]:
                break
            i += 1
        count = i - start
        out.append(count - 1)
        out += data[start:i]
    return bytes(out)


def rle_chunks(enc, limit):
    """按完整 token 切段, 每个 BLE 写必须自带完整压缩序列."""
    segs = []
    cur = bytearray()
    i = 0
    n = len(enc)
    while i < n:
        c = enc[i]
        if c & 0x80:
            tok = enc[i:i + 2]
            i += 2
        else:
            cnt = c + 1
            tok = enc[i:i + 1 + cnt]
            i += 1 + cnt
        if len(cur) + len(tok) > limit:
            segs.append(bytes(cur))
            cur = bytearray()
        cur += tok
    if cur:
        segs.append(bytes(cur))
    return segs


def load_layers(path):
    """返回 (bw, red) 两个 1bpp 数组. bw 1=白 0=黑, red 0=红 1=不红."""
    img = Image.open(path).convert("RGB").resize((W, H), Image.LANCZOS)
    a = np.asarray(img).astype(np.int32)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    sat = a.max(axis=2) - a.min(axis=2)
    red = (r > 120) & (r - g > 40) & (r - b > 40) & (sat > 50)
    lum = (r * 299 + g * 587 + b * 114) // 1000
    black = (lum < 120) & ~red
    bw = np.where(black, 0, 1).astype(np.uint8)
    rl = np.where(red, 0, 1).astype(np.uint8)
    return np.packbits(bw, axis=1), np.packbits(rl, axis=1)


def diff_box(old_bw, old_red, bw, red):
    changed = (old_bw != bw) | (old_red != red)
    rows = np.flatnonzero(changed.any(axis=1))
    if rows.size == 0:
        return None
    y0 = int(rows[0]) // BLOCK * BLOCK
    y1 = min(H - 1, (int(rows[-1]) // BLOCK + 1) * BLOCK - 1)
    cols = np.flatnonzero(changed.any(axis=0))
    x0 = int(cols[0]) * 8 // BLOCK * BLOCK
    x1 = min(W - 1, ((int(cols[-1]) * 8) // BLOCK + 1) * BLOCK - 1)
    return int(x0), int(y0), int(x1), int(y1)


def crop(frame, x0, y0, x1, y1):
    x0 &= 0xFFF8
    x1 |= 7
    return frame[y0:y1 + 1, x0 // 8:(x1 + 1) // 8].tobytes()


async def find_device():
    for _ in range(6):
        res = await BleakScanner.discover(timeout=6, return_adv=True)
        for _, value in res.items():
            dev, _adv = value if isinstance(value, tuple) else (value, None)
            if "epd" in (dev.name or "").lower():
                return dev
        await asyncio.sleep(2)
    return None


class Panel:
    def __init__(self, client):
        self.c = client
        self.notes = []

    async def start(self):
        await self.c.start_notify(CMD, lambda _h, d: self.notes.append(bytes(d)))

    async def svc(self, code):
        await self.c.write_gatt_char(CMD, pkt(code), response=True)

    async def panel(self, code, data=None):
        await self.c.write_gatt_char(CMD, pkt(0x03, [code]), response=True)
        if data is not None:
            await self.c.write_gatt_char(CMD, pkt(0x04, bytes(data)), response=True)

    async def window(self, x0, y0, x1, y1):
        x0 &= 0xFFF8
        x1 |= 7
        await self.panel(0x90, [x0 >> 8, x0 & 255, x1 >> 8, x1 & 255,
                                y0 >> 8, y0 & 255, y1 >> 8, y1 & 255, 0x00])

    async def send(self, data):
        for i in range(0, len(data), 200):
            await self.c.write_gatt_char(CMD, pkt(0x04, data[i:i + 200]), response=True)
        return len(data)

    async def send_rle(self, data, black):
        """用 0x30 的压缩写入. begin=0 避免窗口被重置成全屏."""
        enc = rle_encode(data)
        flags = 0x04 if black else 0x05
        sent = 0
        for seg in rle_chunks(enc, 240):
            await self.c.write_gatt_char(CMD, bytes([0x30, flags]) + seg, response=True)
            sent += len(seg)
        return sent, len(data)

    async def lut(self):
        await self.panel(0x20, VCOM)
        await self.panel(0x22, RED)
        await self.panel(0x23, WW)
        await self.panel(0x24, BB)
        await self.panel(0x25, [0] * 42)

    async def mode_kw(self):
        """黑白模式: 新旧双缓冲, WW/KK 写零, 未变化像素不驱动."""
        await self.panel(0x00, [0x3F])
        await self.panel(0x50, [0xA9, 0x07])
        await self.panel(0x20, VCOM)
        await self.panel(0x21, [0] * 42)
        await self.panel(0x22, WW)
        await self.panel(0x23, BB)
        await self.panel(0x24, [0] * 60)
        await self.panel(0x25, [0] * 42)

    async def mode_kwr(self, fast=False):
        await self.panel(0x50, [0x77])
        if fast:
            await self.panel(0x00, [0x2F])
            await self.lut()
        else:
            await self.panel(0x00, [0x0F])

    async def refresh(self):
        self.notes.clear()
        await self.svc(0x39)
        for _ in range(800):
            for n in self.notes:
                if n.startswith(b"ms="):
                    return int(n[3:])
            await asyncio.sleep(0.05)
        return -1


async def push(old, new, force_full, fast=False):
    old_bw, old_red = old if old else (None, None)
    bw, red = new
    count = 0
    if STATE.exists():
        try:
            data = np.load(STATE)
            count = int(data["count"])
            if old_bw is None:
                old_bw = data["bw"]
                old_red = data["red"]
        except Exception:
            old_bw = None

    box = diff_box(old_bw, old_red, bw, red) if old_bw is not None else (0, 0, W - 1, H - 1)
    do_full = force_full or old_bw is None or count >= FULL_EVERY
    if do_full:
        box = (0, 0, W - 1, H - 1)
        count = 0
    elif box is None:
        print("画面无变化, 跳过")
        return

    red_changed = old_red is not None and int((old_red != red).sum()) > 2
    colour = do_full or red_changed
    label = "整屏三色" if do_full else ("窗口三色" if red_changed else "黑白局刷")
    print("%s 窗口 x=%d..%d y=%d..%d" % (label, box[0], box[2], box[1], box[3]))

    dev = await find_device()
    if dev is None:
        print("找不到设备")
        sys.exit(2)
    client = BleakClient(dev, timeout=30)
    await client.connect()
    try:
        p = Panel(client)
        await p.start()
        if do_full:
            await p.svc(0x01)
            await asyncio.sleep(1.0)
        else:
            await p.panel(0x04)     # 上电, 不复位
            await asyncio.sleep(0.2)

        tx0 = time.time()
        if colour:
            await p.mode_kwr(fast)
            if do_full:
                await p.window(0, 0, W - 1, H - 1)
                await p.panel(0x10)
                sent, raw = await p.send_rle(bw.tobytes(), True)
                await p.panel(0x13)
                s2, r2 = await p.send_rle(red.tobytes(), False)
            else:
                await p.panel(0x91)
                await p.window(*box)
                await p.panel(0x10)
                sent, raw = await p.send_rle(crop(bw, *box), True)
                await p.panel(0x13)
                s2, r2 = await p.send_rle(crop(red, *box), False)
            sent += s2
            raw += r2
            tx = time.time() - tx0

            ms = await p.refresh()
            if not do_full:
                try:
                    await p.panel(0x92)
                except Exception:
                    pass

            # 垫新图: 三色刷完 DTM2 里是红色数据, 直接切黑白会把它当成新图.
            # 把当前黑白画面写回 DTM2, 不刷新, 红色像素物理上不受影响.
            await p.panel(0x13)
            if do_full:
                await p.send_rle(bw.tobytes(), False)
            else:
                await p.send_rle(crop(bw, *box), False)
            await p.mode_kw()
        else:
            await p.mode_kw()
            await p.panel(0x91)
            await p.window(*box)
            await p.panel(0x13)     # 黑白模式 DTM2 就是新图
            sent, raw = await p.send_rle(crop(bw, *box), False)
            tx = time.time() - tx0
            ms = await p.refresh()
            await p.panel(0x92)
            count += 1

        print("刷新 %d ms, 传输 %.1f 秒, 原始 %d 字节, 压缩后 %d 字节 (%.1f%%)"
              % (ms, tx, raw, sent, 100.0 * sent / raw if raw else 0))
        np.savez(STATE, bw=bw, red=red, count=count)
    finally:
        await client.disconnect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--fast", action="store_true",
                    help="用自写寄存器波形, 2.6 秒, 但红色驱动力不足, 慎用")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bw, red = load_layers(args.image)
    print("红像素 %d" % int((np.unpackbits(red, axis=1) == 0).sum()))
    if args.dry_run:
        return

    old = None
    if STATE.exists():
        try:
            d = np.load(STATE)
            old = (d["bw"], d["red"])
        except Exception:
            old = None
    asyncio.run(push(old, (bw, red), args.full, args.fast))


if __name__ == "__main__":
    main()

