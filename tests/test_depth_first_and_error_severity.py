import unittest
from unittest.mock import MagicMock, patch
import logging
import requests
import json
import os
import shutil

import src.logger_util as logger_util
from src.cancellation import CancellationManager, MigrationCancelledException
from src.checkpoint import MigrationCheckpoint
from src.extractor import PageNode
from src.importer import ConfluenceImporter, ConfluenceClient


class TestDepthFirstAndErrorSeverity(unittest.TestCase):

    def setUp(self):
        logger_util.setup_notice_log_level()

    def test_notice_log_level_registration(self):
        """NOTICE (25) ログレベルが正常に登録され、Logger.notice() が機能することを検証"""
        self.assertEqual(logging.NOTICE, 25)
        test_logger = logging.getLogger("test_notice")
        with self.assertLogs("test_notice", level="NOTICE") as cm:
            test_logger.notice("Checkpoint lock acquired after waiting.")
        self.assertTrue(any("Checkpoint lock acquired" in msg for msg in cm.output))

    @patch("requests.post")
    def test_create_page_409_retry_warning_and_success(self, mock_post):
        """409 Conflict発生時にWARNINGログで再試行し、その後成功することを検証"""
        # 1回目: 409 Conflict, 2回目: 200 OK
        resp_409 = MagicMock()
        resp_409.status_code = 409
        resp_409.text = "Conflict: Parent lock contention"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "12345", "title": "Test Page"}

        mock_post.side_effect = [resp_409, resp_200]

        client = ConfluenceClient("https://example.atlassian.net/wiki", "user", "token")
        with self.assertLogs("src.importer", level="WARNING") as cm:
            with patch("time.sleep", return_value=None):
                result = client.create_page(space_key="TEST", title="Test Page", body="<p>Test</p>", parent_id="999")

        self.assertEqual(result["id"], "12345")
        self.assertTrue(any("[409 Conflict] Retrying create_page" in msg for msg in cm.output))

    @patch("requests.post")
    def test_create_page_409_retry_exhausted_error(self, mock_post):
        """409 Conflictでリトライ回数超過時にERRORログが出力されRuntimeErrorとなることを検証"""
        resp_409 = MagicMock()
        resp_409.status_code = 409
        resp_409.text = "Conflict: Parent lock contention"
        mock_post.return_value = resp_409

        client = ConfluenceClient("https://example.atlassian.net/wiki", "user", "token")
        with self.assertLogs("src.importer", level="ERROR") as cm:
            with patch("time.sleep", return_value=None):
                with self.assertRaises(RuntimeError):
                    client.create_page(space_key="TEST", title="Test Page", body="<p>Test</p>", parent_id="999")

        self.assertTrue(any("Failed to create page [Test Page]" in msg for msg in cm.output))

    @patch("requests.put")
    @patch.object(ConfluenceClient, "get_page_by_id")
    def test_update_page_409_retry_warning_and_success(self, mock_get_page, mock_put):
        """update_page で409発生時に最新バージョンを取得し再試行成功することを検証"""
        resp_409 = MagicMock()
        resp_409.status_code = 409
        resp_409.text = "Version conflict"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "12345", "version": {"number": 2}}

        mock_put.side_effect = [resp_409, resp_200]
        mock_get_page.return_value = {"id": "12345", "version": {"number": 1}}

        client = ConfluenceClient("https://example.atlassian.net/wiki", "user", "token")
        with self.assertLogs("src.importer", level="WARNING") as cm:
            with patch("time.sleep", return_value=None):
                result = client.update_page(page_id="12345", space_key="TEST", title="Test Page", body="<p>Body</p>", current_version=1)

        self.assertEqual(result["id"], "12345")
        self.assertTrue(any("[409 Conflict] Retrying update_page" in msg for msg in cm.output))

    def test_cancellation_manager_error_halt(self):
        """ERRORレベル事象発生時に trigger_error_halt() により後続処理が即座に停止することを検証"""
        cm = CancellationManager()
        self.assertFalse(cm.is_cancelled)
        self.assertFalse(cm.is_error_halt)

        cm.trigger_error_halt("Fatal 409 parent lock failure after retries exhausted")
        self.assertTrue(cm.is_cancelled)
        self.assertTrue(cm.is_error_halt)
        self.assertIn("Fatal 409", cm.halt_reason)

        with self.assertRaises(MigrationCancelledException):
            cm.check_cancelled()

    def test_depth_first_traversal_execution_order(self):
        """サブツリー内が深さ優先（DFS）順序で処理され、親ロック競合が発生しない順序であることを検証"""
        # ツリー構造:
        # Branch A
        #  ├── Page A.1 (depth 1)
        #  │    └── Page A.1.1 (depth 2)
        #  └── Page A.2 (depth 1)

        branch_a = PageNode(id="b_a", title="Branch A", node_type="page")
        page_a1 = PageNode(id="p_a1", title="Page A.1", node_type="page")
        page_a11 = PageNode(id="p_a11", title="Page A.1.1", node_type="page")
        page_a2 = PageNode(id="p_a2", title="Page A.2", node_type="page")

        page_a1.children = [page_a11]
        branch_a.children = [page_a1, page_a2]

        created_order = []

        importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="u",
            token="t",
            space_key="TEST",
            page_workers=2,
        )

        def mock_create_or_update(title, body, parent_id, overwrite):
            created_order.append(title)
            return f"cid_{title}", f"https://example.com/{title}"

        importer._create_or_update_page = mock_create_or_update
        importer.client.update_page = MagicMock(return_value={"id": "dummy"})
        importer.client.get_page_by_id = MagicMock(return_value={"id": "dummy", "version": {"number": 1}})
        importer.client.get_space_id = MagicMock(return_value="space_1")

        # ブランチ内は allow_sub_parallel=False で完全な深さ優先（DFS）で逐次処理される
        importer._import_node_recursive(branch_a, parent_id="root_id", overwrite=False, allow_sub_parallel=False)

        # 深さ優先順序の検証:
        # Branch A -> Page A.1 -> Page A.1.1 -> Page A.2 (Page A.2はA.1配下の深層作成が完了するまで作成されない)
        expected_order = ["Branch A", "Page A.1", "Page A.1.1", "Page A.2"]
        self.assertEqual(created_order, expected_order)

    def test_resume_missing_confluence_page_recreation(self):
        """チェックポイントに記録済みでもConfluenceサーバー上に実在しない場合、チェックポイントを解除して再作成されることを検証"""
        test_cp_file = "test_cp_missing.json"
        if os.path.exists(test_cp_file):
            os.remove(test_cp_file)

        cp = MigrationCheckpoint(test_cp_file)
        cp.record_pass1(node_id="node_p1", confluence_id="99999", title="Ghost Page", url="https://...", node_type="page")

        importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="u",
            token="t",
            space_key="TEST",
            checkpoint=cp,
        )

        # サーバー上に実在しない (None を返す)
        importer.client.get_page_by_id = MagicMock(return_value=None)
        recreated = []
        def mock_create(title, body, parent_id, overwrite):
            recreated.append(title)
            return "new_123", "https://new_url"

        importer._create_or_update_page = mock_create
        importer.client.update_page = MagicMock(return_value={"id": "new_123"})

        node = PageNode(id="node_p1", title="Ghost Page", node_type="page")
        cid = importer._import_node_recursive(node, parent_id=None, overwrite=False)

        self.assertEqual(cid, "new_123")
        self.assertIn("Ghost Page", recreated)
        self.assertEqual(cp.get_node_info("node_p1")["confluence_id"], "new_123")

        if os.path.exists(test_cp_file):
            os.remove(test_cp_file)


if __name__ == "__main__":
    unittest.main()
