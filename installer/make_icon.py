"""生成 SADK 的应用图标，产物是 assets/sadk.ico。

不引 Pillow：为了一个图标去加一条构建期依赖不划算，而 ICO 本身就是
「文件头 + 一串未压缩 BGRA 位图」，标准库足够了。图形按归一化坐标画，
所以每个尺寸都是独立重绘而不是缩放，小图标不会糊。

图案是终端提示符 `>_`——sonc 是命令行编译器，这比抽象几何图形更说明身份。
"""

from __future__ import annotations

from pathlib import Path
import struct

SIZES = (16, 32, 48, 64, 128, 256)

BACKGROUND = (0x1B, 0x26, 0x38)  # 深靛蓝，浅色和深色任务栏上都压得住
FOREGROUND = (0x5E, 0xE0, 0xC8)  # 亮青，终端荧光屏的味道

CORNER_RADIUS = 0.20
STROKE = 0.085
# 提示符 `>` 的三个折点，以及右侧下划线的两端，都用 0..1 归一化坐标
CHEVRON = ((0.26, 0.28), (0.50, 0.50), (0.26, 0.72))
UNDERSCORE = ((0.58, 0.70), (0.78, 0.70))

SUPERSAMPLE = 4  # 每个像素采 4x4 个点做抗锯齿，边缘不至于是锯齿状


def _distance_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _inside_rounded_rect(x: float, y: float) -> bool:
    r = CORNER_RADIUS
    cx = min(max(x, r), 1.0 - r)
    cy = min(max(y, r), 1.0 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _on_stroke(x: float, y: float) -> bool:
    half = STROKE / 2
    segments = [
        (*CHEVRON[0], *CHEVRON[1]),
        (*CHEVRON[1], *CHEVRON[2]),
        (*UNDERSCORE[0], *UNDERSCORE[1]),
    ]
    return any(_distance_to_segment(x, y, *seg) <= half for seg in segments)


def _render_bgra(size: int) -> bytes:
    """自下而上的 BGRA 扫描行，正好是 BMP 在 ICO 里要的顺序。"""
    rows: list[bytes] = []
    step = 1.0 / (size * SUPERSAMPLE)
    for py in range(size - 1, -1, -1):
        row = bytearray()
        for px in range(size):
            bg_hits = fg_hits = 0
            for sy in range(SUPERSAMPLE):
                y = (py * SUPERSAMPLE + sy + 0.5) * step
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx + 0.5) * step
                    if not _inside_rounded_rect(x, y):
                        continue
                    bg_hits += 1
                    if _on_stroke(x, y):
                        fg_hits += 1
            total = SUPERSAMPLE * SUPERSAMPLE
            alpha = round(255 * bg_hits / total)
            if bg_hits == 0:
                row += b"\x00\x00\x00\x00"
                continue
            # 前景覆盖率只在实心背景内插值，这样描边不会在圆角外侧渗出半透明毛边
            mix = fg_hits / bg_hits
            b, g, r = (round(bg + (fg - bg) * mix) for bg, fg in zip(BACKGROUND[::-1], FOREGROUND[::-1]))
            row += bytes((b, g, r, alpha))
        rows.append(bytes(row))
    return b"".join(rows)


def _bmp_entry(size: int) -> bytes:
    # biHeight 要写成两倍高度：BMP-in-ICO 约定图像后面还跟着一张 AND 掩码，
    # 哪怕 32 位色已经自带 alpha、掩码全零，缺了它资源解析器会算错偏移
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    mask_stride = ((size + 31) // 32) * 4
    return header + _render_bgra(size) + b"\x00" * (mask_stride * size)


def build_ico(output: Path) -> Path:
    images = [_bmp_entry(size) for size in SIZES]
    offset = 6 + 16 * len(images)

    directory = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    for size, image in zip(SIZES, images):
        # 256 在这个字节字段里溢出，按规范写 0
        dimension = 0 if size == 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(image), offset)
        offset += len(image)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(directory + entries + b"".join(images))
    return output


if __name__ == "__main__":
    path = build_ico(Path(__file__).resolve().parent / "assets" / "sadk.ico")
    print(f"{path} ({path.stat().st_size} bytes)")
