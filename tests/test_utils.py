import unittest
from src import utils

class UtilsTests(unittest.TestCase):
    def test_normalize_text(self):
        inp = "  #General-Chat 💬 "
        out = utils.__dict__.get('normalize_text')
        if out is None:
            self.skipTest('normalize_text not present')
        normalized = out(inp)
        self.assertIn('general', normalized)
        self.assertIn('chat', normalized)

    def test_reverse_polarity_if_needed(self):
        # Skip if PIL not available in this environment
        if not getattr(utils, '_HAS_PIL', False):
            self.skipTest('PIL not available')

        from PIL import Image, ImageDraw, ImageStat

        # Create a dark image with bright text (simulating Discord dark theme)
        img = Image.new('RGB', (300, 60), color=(8, 8, 12))
        draw = ImageDraw.Draw(img)
        draw.text((10, 10), "HiTest", fill=(255, 255, 255))

        # Ensure original mean is dark
        s1 = ImageStat.Stat(img.convert('L'))
        mean_before = s1.mean[0]
        self.assertLess(mean_before, 80)

        out, inverted = utils.reverse_polarity_if_needed(img)
        self.assertTrue(isinstance(out, Image.Image))
        self.assertTrue(inverted)

        s2 = ImageStat.Stat(out.convert('L'))
        mean_after = s2.mean[0]
        # After inversion mean should be much higher
        self.assertGreater(mean_after, mean_before)

    def test_ocr_image_to_text_quick_fixes(self):
        # Skip if Tesseract or PIL not available in runtime
        if not getattr(utils, '_HAS_PIL', False) or not getattr(utils, '_HAS_PYTESSERACT', False):
            self.skipTest('PIL or pytesseract not available')

        from PIL import Image, ImageDraw

        # Create a dark small tooltip-like image with white text starting near-left
        img = Image.new('RGB', (200, 36), color=(10, 10, 12))
        draw = ImageDraw.Draw(img)
        # Intentionally start text at x=2 to test padding
        draw.text((2, 6), "HiOCR", fill=(255, 255, 255))

        out = utils.ocr_image_to_text(img)
        # We expect some recognizable output (non-empty) — tolerate imperfect OCR
        self.assertTrue(isinstance(out, str))
        self.assertGreater(len(out.strip()), 1)

if __name__ == '__main__':
    unittest.main()
