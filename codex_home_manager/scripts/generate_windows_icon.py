from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def rounded_rectangle_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def draw_database_icon(draw: ImageDraw.ImageDraw, scale: float) -> None:
    x0 = int(70 * scale)
    y0 = int(70 * scale)
    x1 = int(186 * scale)
    y1 = int(180 * scale)
    ellipse_height = int(34 * scale)
    stroke_width = max(4, int(9 * scale))
    line_color = (255, 255, 255, 245)
    fill_color = (219, 234, 254, 56)

    draw.ellipse((x0, y0, x1, y0 + ellipse_height), fill=fill_color, outline=line_color, width=stroke_width)
    draw.line((x0, y0 + ellipse_height // 2, x0, y1 - ellipse_height // 2), fill=line_color, width=stroke_width)
    draw.line((x1, y0 + ellipse_height // 2, x1, y1 - ellipse_height // 2), fill=line_color, width=stroke_width)
    draw.arc((x0, y1 - ellipse_height, x1, y1), 0, 180, fill=line_color, width=stroke_width)
    draw.arc((x0, int(112 * scale), x1, int(112 * scale) + ellipse_height), 0, 180, fill=line_color, width=stroke_width)
    draw.arc((x0, int(144 * scale), x1, int(144 * scale) + ellipse_height), 0, 180, fill=line_color, width=stroke_width)


def render_icon(size: int) -> Image.Image:
    scale = size / 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    base = Image.new("RGBA", (size, size), (37, 99, 235, 255))
    pixels = base.load()
    for y in range(size):
        for x in range(size):
            green_mix = int(62 * (x / max(1, size - 1)) * (y / max(1, size - 1)))
            pixels[x, y] = (
                max(0, 37 - green_mix // 8),
                min(255, 99 + green_mix),
                max(0, 235 - green_mix // 2),
                255,
            )

    mask = rounded_rectangle_mask(size, max(6, int(46 * scale)))
    image.alpha_composite(Image.composite(base, Image.new("RGBA", (size, size), (0, 0, 0, 0)), mask))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (int(10 * scale), int(10 * scale), int(246 * scale), int(246 * scale)),
        radius=max(6, int(38 * scale)),
        outline=(255, 255, 255, 46),
        width=max(1, int(3 * scale)),
    )
    draw_database_icon(draw, scale)
    return image


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    icon_path = root / "packaging" / "windows" / "assets" / "codex-home-manager.ico"
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [render_icon(size) for size in sizes]
    images[-1].save(icon_path, sizes=[(size, size) for size in sizes], append_images=images[:-1])
    print(icon_path)


if __name__ == "__main__":
    main()
