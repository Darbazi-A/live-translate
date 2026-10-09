"""
Draws the Live Translate icon (gradient tile + sound bars, same as the web logo) and
writes icon.png, icon.ico (Windows) and icon.icns (macOS) next to this file, plus
the favicons in public/.
Pure Python, no Pillow needed:  python packaging/make_icons.py
"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
C1 = (0x6F, 0x8C, 0xFF)   # --accent
C2 = (0x9B, 0x7B, 0xFF)   # --accent-2
# sound bars of the "waves" icon on its 24px grid: (x, half height)
BARS = [(4, 2), (8, 6), (12, 9), (16, 5), (20, 2)]
SS = 4                     # supersampling per axis


def inside_rounded(x, y, size, r):
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def inside_bars(x, y, size):
    # bars occupy the middle 62% of the tile
    scale = size * 0.62 / 24
    ox = oy = size * 0.19
    gx, gy = (x - ox) / scale, (y - oy) / scale
    half_w = 1.15
    for bx, hh in BARS:
        top, bottom = 12 - hh, 12 + hh
        if abs(gx - bx) <= half_w and top <= gy <= bottom:
            return True
        for cy in (top, bottom):   # round caps
            if (gx - bx) ** 2 + (gy - cy) ** 2 <= half_w ** 2:
                return True
    return False


def render(size):
    r = size * 0.23
    rows = []
    for py in range(size):
        row = bytearray([0])   # PNG filter byte
        for px in range(size):
            tile = bars = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = px + (sx + 0.5) / SS
                    y = py + (sy + 0.5) / SS
                    if inside_rounded(x, y, size, r):
                        tile += 1
                        if inside_bars(x, y, size):
                            bars += 1
            n = SS * SS
            t = (px + py) / (2 * size)
            base = [C1[i] + (C2[i] - C1[i]) * t for i in range(3)]
            if tile:
                w = bars / tile
                rgb = [round(base[i] * (1 - w) + 255 * w) for i in range(3)]
                row += bytes(rgb + [round(255 * tile / n)])
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return png(size, b"".join(rows))


def png(size, raw):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(images):
    # PNG-compressed entries (Windows Vista and later)
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    data = b""
    for size, img in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(img), offset + len(data))
        data += img
    return out + data


def icns(images):
    types = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09"}
    body = b"".join(types[s] + struct.pack(">I", len(img) + 8) + img for s, img in images if s in types)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main():
    imgs = {s: render(s) for s in (16, 32, 48, 64, 128, 256, 512)}
    with open(os.path.join(HERE, "icon.png"), "wb") as f:
        f.write(imgs[512])
    with open(os.path.join(HERE, "icon.ico"), "wb") as f:
        f.write(ico([(s, imgs[s]) for s in (16, 32, 48, 256)]))
    with open(os.path.join(HERE, "icon.icns"), "wb") as f:
        f.write(icns([(s, imgs[s]) for s in (16, 32, 64, 128, 256, 512)]))
    public = os.path.join(HERE, "..", "public")
    with open(os.path.join(public, "favicon.png"), "wb") as f:
        f.write(imgs[64])
    with open(os.path.join(public, "apple-touch-icon.png"), "wb") as f:
        f.write(render(180))
    print("icon.png, icon.ico, icon.icns written to", HERE, "and favicons to public/")


if __name__ == "__main__":
    main()
