"""Build deterministic PNG and ICO variants of the gateway brand mark."""
from __future__ import annotations

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
SOURCE = STATIC / "brand-logo-source.png"
MARK_CROP = (450, 120, 1090, 760)


def render_mark(size: int) -> Image.Image:
    with Image.open(SOURCE) as source:
        mark = source.convert("RGBA").crop(MARK_CROP)
        return mark.resize((size, size), Image.Resampling.LANCZOS)


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
