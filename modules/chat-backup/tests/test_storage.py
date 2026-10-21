import tempfile
import unittest
from pathlib import Path

from chat_backup.storage import BackupStore


class StorageTests(unittest.TestCase):
    def test_checkpoint_is_scoped_to_source_target_and_never_stores_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BackupStore(Path(directory))
            store.save(
                "source@example.com", "target@example.com", "conversation-1", "Title", "complete",
                target_url="https://chatgpt.com/c/copied",
            )
            self.assertTrue(store.is_complete("source@example.com", "target@example.com", "conversation-1"))
            self.assertFalse(store.is_complete("source@example.com", "other@example.com", "conversation-1"))
            store.close()
            raw = (Path(directory) / "chat-backup.db").read_bytes()
            self.assertNotIn(b"source@example.com", raw)
            self.assertNotIn(b"target@example.com", raw)

    def test_cleanup_warning_still_counts_as_complete_for_retry_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BackupStore(Path(directory))
            store.save("a@example.com", "b@example.com", "id", "Title", "complete_cleanup_failed")
            self.assertTrue(store.is_complete("a@example.com", "b@example.com", "id"))
            store.close()


if __name__ == "__main__":
    unittest.main()
