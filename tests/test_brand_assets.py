"""Brand asset and application-icon integration checks."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.tray import TRAY_ICON, make_icon


class BrandAssetTests(unittest.TestCase):
    def test_png_variants_use_the_supplied_gray_logo_crop(self) -> None:
        static = ROOT / "app" / "web" / "static"
        source_path = static / "brand-logo-source.png"
        self.assertTrue(source_path.is_file())
        with Image.open(source_path) as source:
            expected = source.convert("RGBA").crop((450, 120, 1090, 760))
        for size in (64, 180, 512):
            with Image.open(static / f"brand-mark-{size}.png") as image:
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.getpixel((size // 2, size // 2)),
                                 expected.resize((size, size), Image.Resampling.LANCZOS)
                                 .getpixel((size // 2, size // 2)))

    def test_ico_contains_small_and_large_windows_sizes(self) -> None:
        with Image.open(ROOT / "app" / "web" / "static" / "favicon.ico") as image:
            sizes = image.ico.sizes()
        self.assertTrue({(16, 16), (32, 32), (64, 64), (256, 256)} <= sizes)

    def test_tray_uses_the_shared_brand_asset(self) -> None:
        self.assertTrue(TRAY_ICON.is_file())
        icon = make_icon()
        self.assertEqual(icon.size, (64, 64))
        self.assertEqual(icon.mode, "RGBA")


if __name__ == "__main__":
    unittest.main()
