import unittest
from pathlib import Path
import tempfile
from src import storage

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test_timestamps.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_can_post_and_update(self):
        # Initially can post
        self.assertTrue(storage.can_post('chan1', path=str(self.path)))
        storage.update_timestamp('chan1', path=str(self.path))
        # Immediately after updating, cannot post with long cooldown
        self.assertFalse(storage.can_post('chan1', cooldown_minutes=60, path=str(self.path)))

    def test_cleanup_old_entries(self):
        # create two entries: one old, one recent
        import json
        from datetime import datetime, timedelta
        data = {
            'old': (datetime.now() - timedelta(days=60)).isoformat(),
            'recent': datetime.now().isoformat()
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))
        storage.cleanup_old_entries(path=str(self.path), days=30)
        remaining = json.loads(self.path.read_text())
        self.assertIn('recent', remaining)
        self.assertNotIn('old', remaining)

if __name__ == '__main__':
    unittest.main()
