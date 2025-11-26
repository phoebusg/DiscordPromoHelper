import unittest
from src import utils


class OCRReliabilityTests(unittest.TestCase):
    def setUp(self):
        # Only require PIL for image generation; pytesseract can be faked in tests
        if not getattr(utils, '_HAS_PIL', False):
            self.skipTest('PIL not available')

    def _make_img(self, text: str, size=(200, 36), bg=(10, 10, 12), fg=(255, 255, 255), x=2, y=6):
        from PIL import Image, ImageDraw
        img = Image.new('RGB', size, color=bg)
        draw = ImageDraw.Draw(img)
        # draw the supplied text starting near the left edge to simulate clipped leading characters
        draw.text((x, y), text, fill=fg)
        return img

    def test_leading_alphanumeric_not_clipped(self):
        if not getattr(utils, '_HAS_PYTESSERACT', False):
            self.skipTest('pytesseract not available')
        img = self._make_img('ALead', x=1)
        out = utils.ocr_image_to_text(img)
        self.assertIsInstance(out, str)
        self.assertIn('lead', out.lower())

    def test_leading_punctuation_and_hash(self):
        if not getattr(utils, '_HAS_PYTESSERACT', False):
            self.skipTest('pytesseract not available')
        img = self._make_img('#ServerName', x=1)
        out = utils.ocr_image_to_text(img)
        self.assertIsInstance(out, str)
        # '#' may or may not be returned, but the word should be present
        self.assertIn('server', out.lower())

    def test_dark_theme_inversion_preserves_text(self):
        if not getattr(utils, '_HAS_PYTESSERACT', False):
            self.skipTest('pytesseract not available')
        img = self._make_img('#DarkToolTip', bg=(6, 6, 8), x=2)
        out = utils.ocr_image_to_text(img)
        self.assertIsInstance(out, str)
        self.assertIn('darktooltip'.lower(), out.replace(' ', '').lower())

    def test_leading_emoji_does_not_block_text(self):
        if not getattr(utils, '_HAS_PYTESSERACT', False):
            self.skipTest('pytesseract not available')
        # Some fonts won't draw emoji; test that even if an emoji exists, the textual part gets recognized
        emoji_text = '\u26A1Server'  # ⚡Server
        img = self._make_img(emoji_text, x=1)
        out = utils.ocr_image_to_text(img)
        self.assertIsInstance(out, str)
        self.assertIn('server', out.lower())

    def test_confidence_selection_prefers_highest(self):
        # This test simulates multiple image_to_data calls with different confidences
        # so we can verify the selection logic without requiring a real tesseract binary.
        from PIL import Image

        img = Image.new('RGB', (120, 36), color=(255, 255, 255))

        # Save originals and inject a fake pytesseract
        orig_pyt = getattr(utils, 'pytesseract', None)
        orig_has = getattr(utils, '_HAS_PYTESSERACT', False)

        class FakeOutput:
            DICT = object()

        class FakeTess:
            def __init__(self):
                self.Output = FakeOutput
                self._calls = 0

            def image_to_data(self, *args, **kwargs):
                # simulate increasing confidence on successive calls
                self._calls += 1
                if self._calls < 3:
                    return {'text': ['lowtext'], 'conf': ['10']}
                return {'text': ['BESTTEXT'], 'conf': ['95']}

            def image_to_string(self, *args, **kwargs):
                # Return empty string so ocr_image_to_text proceeds to image_to_data variants
                return ''

        try:
            utils.pytesseract = FakeTess()
            utils._HAS_PYTESSERACT = True
            out = utils.ocr_image_to_text(img)
            self.assertIsInstance(out, str)
            self.assertIn('besttext', out.lower())
        finally:
            utils.pytesseract = orig_pyt
            utils._HAS_PYTESSERACT = orig_has


if __name__ == '__main__':
    unittest.main()
