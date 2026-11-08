"""
Task B.5, B.6, B.7 の機能検証テストスイート。
- Task B.5: 開発者報告用サニタイズド診断ログ生成
- Task B.6: チェックポイント保存と再開（レジューム）機能
- Task B.7: SIGINT/SIGTERM 安全中断（Graceful Cancellation）機能
"""

import os
import sys
import json
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor import PageNode
from src.telemetry import SanitizedTelemetry
from src.checkpoint import MigrationCheckpoint
from src.cancellation import CancellationManager, MigrationCancelledException
from src.importer import ConfluenceImporter


class TestTaskB5SanitizedTelemetry(unittest.TestCase):
    """Task B.5: サニタイズド診断レポートのテスト"""

    def setUp(self):
        self.telemetry = SanitizedTelemetry()

    def test_string_sanitization(self):
        # 1. BearerトークンとNotionトークンのマスキング
        raw_text = "Authorization: Bearer ntn_1234567890abcdef and token ntn_abcdef12345"
        sanitized = self.telemetry.sanitize_string(raw_text)
        self.assertNotIn("1234567890abcdef", sanitized)
        self.assertIn("[REDACTED_TOKEN]", sanitized)

        # 2. メールアドレスのマスキング
        email_text = "User contact is developer@example.com for inquiry."
        sanitized_email = self.telemetry.sanitize_string(email_text)
        self.assertNotIn("developer@example.com", sanitized_email)
        self.assertIn("[REDACTED_EMAIL]", sanitized_email)

        # 3. UUIDのマスキング
        uuid_text = "Page ID: 12345678-1234-5678-9abc-def012345678 in block"
        sanitized_uuid = self.telemetry.sanitize_string(uuid_text)
        self.assertNotIn("12345678-1234-5678-9abc-def012345678", sanitized_uuid)
        self.assertIn("[UUID]", sanitized_uuid)

    def test_tree_structure_telemetry(self):
        root = PageNode(id="p1", title="Secret Top Secret Roadmap", node_type="page", blocks=[
            {"id": "b1", "type": "heading_1"},
            {"id": "b2", "type": "paragraph"},
            {"id": "b3", "type": "column_list"},
            {"id": "b4", "type": "breadcrumb"},
        ])
        child = PageNode(id="p2", title="Confidential Client List", node_type="database", blocks=[
            {"id": "b5", "type": "table"},
        ])
        root.children.append(child)

        self.telemetry.record_tree(root)
        report = self.telemetry.generate_report()

        # タイトルや本文の文字列がレポートに含まれないことを検証
        report_str = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("Secret Top Secret", report_str)
        self.assertNotIn("Confidential Client List", report_str)

        # 構造統計が正しく記録されていること
        self.assertEqual(report["tree_structure_summary"]["total_nodes"], 2)
        self.assertEqual(report["tree_structure_summary"]["pages_count"], 1)
        self.assertEqual(report["tree_structure_summary"]["databases_count"], 1)
        self.assertEqual(report["diagnostics"]["block_types_distribution"]["heading_1"], 1)
        self.assertEqual(report["diagnostics"]["block_types_distribution"]["column_list"], 1)
        self.assertIn("breadcrumb", report["diagnostics"]["unsupported_elements_detected"])

    def test_save_debug_reports(self):
        json_path = "test_debug_report.json"
        md_path = "test_debug_report.md"
        try:
            self.telemetry.record_timing("step1_extract_seconds", 1.23)
            self.telemetry.record_macro("toc")
            self.telemetry.record_macro("section")
            self.telemetry.record_warning("Some warning with user@corp.com")
            self.telemetry.save_reports(json_path=json_path, md_path=md_path)

            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(md_path))

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertTrue(data["sanitized"])
            self.assertEqual(data["diagnostics"]["confluence_macros_distribution"]["toc"], 1)

            with open(md_path, "r", encoding="utf-8") as f:
                md_text = f.read()
            self.assertIn("Notion to Confluence 移行診断レポート", md_text)
            self.assertIn("[REDACTED_EMAIL]", md_text)
            self.assertNotIn("user@corp.com", md_text)
        finally:
            if os.path.exists(json_path):
                os.remove(json_path)
            if os.path.exists(md_path):
                os.remove(md_path)


class TestTaskB6CheckpointResume(unittest.TestCase):
    """Task B.6: チェックポイント保存と再開（レジューム）のテスト"""

    def setUp(self):
        self.checkpoint_file = "test_checkpoint.json"
        if os.path.exists(self.checkpoint_file):
            os.remove(self.checkpoint_file)
        self.checkpoint = MigrationCheckpoint(self.checkpoint_file)

    def tearDown(self):
        if os.path.exists(self.checkpoint_file):
            os.remove(self.checkpoint_file)

    def test_atomic_save_and_load(self):
        self.checkpoint.record_pass1(
            node_id="node_001",
            confluence_id="10001",
            title="Page 1",
            url="https://wiki/pages/10001",
            node_type="page",
        )
        self.assertTrue(self.checkpoint.is_pass1_done("node_001"))
        self.assertFalse(self.checkpoint.is_pass2_done("node_001"))

        # 新規インスタンスで読み込み確認
        cp2 = MigrationCheckpoint(self.checkpoint_file)
        self.assertTrue(cp2.load())
        self.assertTrue(cp2.is_pass1_done("node_001"))
        self.assertEqual(cp2.get_node_info("node_001")["confluence_id"], "10001")

        # Pass 2の完了記録
        cp2.record_pass2("node_001")
        self.assertTrue(cp2.is_pass2_done("node_001"))

        # ステータス設定
        cp2.mark_completed()
        self.assertEqual(cp2.status, "COMPLETED")

    def test_importer_skips_completed_nodes(self):
        # 1ノード目だけ事前にPass 1完了を記録
        self.checkpoint.record_pass1(
            node_id="node_001",
            confluence_id="conf_9999",
            title="Page 1 Already Done",
            url="https://wiki/pages/9999",
            node_type="page",
        )

        importer = ConfluenceImporter(
            url="https://example.atlassian.net",
            username="user",
            token="tok",
            space_key="TEST",
            checkpoint=self.checkpoint,
        )

        # Mock Confluence Client
        importer.client = MagicMock()
        importer.client.base_url = "https://example.atlassian.net/wiki"
        importer.client.get_page_by_title.return_value = None
        importer.client.create_page.return_value = {"id": "conf_8888", "_links": {"webui": "/pages/8888"}}
        importer.client.update_page.return_value = {"id": "conf_8888", "_links": {"webui": "/pages/8888"}}
        importer.client.get_page_by_id.return_value = {"version": {"number": 1}}

        root = PageNode(id="node_001", title="Page 1 Already Done", node_type="page")
        child = PageNode(id="node_002", title="Page 2 New", node_type="page")
        root.children.append(child)

        mapping = importer.import_tree(root, parent_id=None, overwrite=True)

        # node_001 は再作成されず、既存の conf_9999 が再利用されていること
        self.assertEqual(mapping["node_001"]["confluence_id"], "conf_9999")
        # node_002 は新規作成されたこと
        self.assertEqual(mapping["node_002"]["confluence_id"], "conf_8888")
        # node_002 がチェックポイントに保存されたこと
        self.assertTrue(self.checkpoint.is_pass1_done("node_002"))


class TestTaskB7Cancellation(unittest.TestCase):
    """Task B.7: 安全な中断（Graceful Cancellation）のテスト"""

    def test_cancellation_trigger(self):
        callback_called = False
        def on_cancel():
            nonlocal callback_called
            callback_called = True

        mgr = CancellationManager(on_cancel_callback=on_cancel)
        self.assertFalse(mgr.is_cancelled)

        # シグナル擬似ハンドリング
        import signal
        mgr._handle_signal(signal.SIGINT, None)
        self.assertTrue(mgr.is_cancelled)
        self.assertTrue(callback_called)

        # check_cancelled で例外が送出されること
        with self.assertRaises(MigrationCancelledException):
            mgr.check_cancelled()


if __name__ == "__main__":
    unittest.main()
