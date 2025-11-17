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

if __name__ == '__main__':
    unittest.main()
