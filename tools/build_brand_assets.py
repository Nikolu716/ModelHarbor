"""Build deterministic PNG and ICO variants of the gateway brand mark."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
BLACK = (9, 9, 9, 255)
WHITE = (245, 245, 247, 255)


def render_mark(size: int) -> Image.Image:
    scale = 4
    canvas = size * scale
    unit = canvas / 64
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(left: float, top: float, right: float, bottom: float):
        return tuple(round(value * unit) for value in (left, top, right, bottom))

    draw.rounded_rectangle(
        box(2, 2, 62, 62),
        radius=round(14 * unit),
        fill=BLACK,
        outline=WHITE,
        width=max(1, round(1.5 * unit)),
    )
    draw.rectangle(box(15, 15, 49, 23), fill=WHITE)
    draw.polygon(
        [
            (round(39 * unit), round(15 * unit)),
            (round(49 * unit), round(15 * unit)),
            (round(25 * unit), round(49 * unit)),
            (round(15 * unit), round(49 * unit)),
        ],
        fill=WHITE,
    )
    draw.rectangle(box(15, 41, 49, 49), fill=WHITE)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    for size in (64, 180, 512):
        render_mark(size).save(
            STATIC / f"brand-mark-{size}.png",
            format="PNG",
            optimize=True,
        )

    render_mark(256).save(
        STATIC / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
