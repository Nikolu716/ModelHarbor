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
    def test_png_variants_have_expected_sizes_and_contrast(self) -> None:
        static = ROOT / "app" / "web" / "static"
        for size in (64, 180, 512):
            with Image.open(static / f"brand-mark-{size}.png") as image:
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.mode, "RGBA")

        with Image.open(static / "brand-mark-64.png") as image:
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertGreater(min(image.getpixel((18, 16))[:3]), 230)
            self.assertGreater(min(image.getpixel((32, 32))[:3]), 230)
            self.assertLess(max(image.getpixel((47, 32))[:3]), 20)

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
