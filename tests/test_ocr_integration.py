import unittest
from PIL import Image, ImageDraw

try:
    from src import discord_nav
except Exception:
    import discord_nav


class OCRIntegrationTests(unittest.TestCase):
    def test_ocr_from_image_dark_tooltip(self):
        # Create a synthetic dark tooltip image with white text (similar to Discord dark tooltips)
        img = Image.new('RGB', (260, 40), color=(8, 8, 12))
        draw = ImageDraw.Draw(img)
        draw.text((8, 8), 'MyServer-name_01', fill=(255, 255, 255))

        # Verify polarity helper detects dark tooltip and inverts
        try:
            from src import utils
        except Exception:
            import utils

        alt, inverted = utils.reverse_polarity_if_needed(img)
        self.assertTrue(inverted)
        # OCR using discord_nav helper (may be empty if tesseract not available in CI)
        txt = discord_nav.ocr_from_image(img)
        self.assertTrue(isinstance(txt, str))

    def test_ocr_from_image_light_bg(self):
        img = Image.new('RGB', (260, 40), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.text((8, 8), 'Server #2', fill=(10, 10, 10))

        # Light background should not trigger inversion
        try:
            from src import utils
        except Exception:
            import utils
        alt2, inverted2 = utils.reverse_polarity_if_needed(img)
        self.assertFalse(inverted2)
        txt = discord_nav.ocr_from_image(img)
        self.assertTrue(isinstance(txt, str))


if __name__ == '__main__':
    unittest.main()
