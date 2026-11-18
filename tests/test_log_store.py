import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

import log_store
from astrbot_plugin_message_bridge.event_log import EventLog


class LogQueryTests(unittest.TestCase):
    def test_component_query_filter_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            astrbot = root / "runtime/AstrBot"
            logs = astrbot / "data/logs"
            logs.mkdir(parents=True)
            (logs / "latest.log").write_text('2026-11-17 10:00:00 INFO normal start\n2026-11-17 10:00:01 ERROR LarkAgentX download failed access_token=private-value https://media.test/a?key=secret\n', encoding="utf-8")
            result = log_store.query_logs(root, astrbot, {"component": "feishu", "q": "failed", "level": "ERROR"})
            self.assertEqual(len(result["entries"]), 1)
            text = result["entries"][0]["text"]
            self.assertNotIn("private-value", text)
            self.assertNotIn("key=secret", text)
            self.assertEqual(result["entries"][0]["component"], "feishu")
            self.assertEqual(log_store.query_logs(root, astrbot, {"component": "snowluma"})["entries"], [])

    def test_event_log_records_evidence_without_message_bodies_and_queries_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            astrbot = root / "runtime/AstrBot"
            path = astrbot / "data/plugin_data/astrbot_plugin_message_bridge"
            path.mkdir(parents=True)
            events = EventLog(path)
            events.write("send_failed", "qq", False, message_id="123", body="private message", cookie="private credential")
            events.handler.close()
            text = (path / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("private", text)
            result = log_store.query_logs(root, astrbot, {"component": "bridge", "level": "ERROR", "since": "2020-01-01T00:00:00Z"})
            self.assertEqual(len(result["entries"]), 1)
            self.assertIn("send_failed", result["entries"][0]["text"])
            self.assertEqual(log_store.query_logs(root, astrbot, {"until": "2020-01-01T00:00:00Z"})["entries"], [])

    def test_large_log_is_bounded_and_older_rotated_file_is_searchable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "runtime/logs"
            logs.mkdir(parents=True)
            (logs / "supervisor.log").write_text("x" * 300_000 + "\n2026-11-17T10:00:00Z INFO recent evidence\n", encoding="utf-8")
            (logs / "supervisor.log.1").write_text("2026-11-16T10:00:00Z ERROR old evidence\n", encoding="utf-8")
            result = log_store.query_logs(root, root / "runtime/AstrBot", {"q": "evidence", "limit": "1"})
            self.assertEqual(len(result["entries"]), 1)
            self.assertTrue(result["bounded"])
            result = log_store.query_logs(root, root / "runtime/AstrBot", {"q": "old evidence"})
            self.assertEqual(len(result["entries"]), 1)
            with self.assertRaises(ValueError):
                log_store.query_logs(root, root, {"component": "../../credentials.json"})


if __name__ == "__main__":
    unittest.main()
