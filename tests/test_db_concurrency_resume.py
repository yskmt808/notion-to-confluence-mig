"""
Task B.2: 大規模DBパイプライン、子ページ並行作成、およびアプローチB（非同期レジューム）のテストスイート。
- 子ページ並行作成（--page-workers）の動作検証
- チェックポイントのアトミック性・スレッドセーフ検証
- アプローチB: Pass 1での非同期インデックス非ブロック完了と、--resumeによる未完了行ビューの遅延解決
"""

import os
import sys
import unittest
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor import PageNode
from src.checkpoint import MigrationCheckpoint
from src.importer import ConfluenceImporter, ConfluenceClient


class TestDatabaseConcurrencyAndResume(unittest.TestCase):
    def setUp(self):
        self.checkpoint_file = "test_db_checkpoint.json"
        if os.path.exists(self.checkpoint_file):
            os.remove(self.checkpoint_file)
        self.checkpoint = MigrationCheckpoint(self.checkpoint_file)

    def tearDown(self):
        if os.path.exists(self.checkpoint_file):
            os.remove(self.checkpoint_file)

    def test_checkpoint_thread_safety(self):
        """マルチスレッド環境で同時にチェックポイントに書き込んでも破損しないことを検証"""
        threads = []
        errors = []

        def worker(idx):
            try:
                self.checkpoint.record_pass1(
                    node_id=f"thread_node_{idx}",
                    confluence_id=f"conf_{idx}",
                    title=f"Page {idx}",
                    url=f"https://wiki/pages/{idx}",
                    node_type="page",
                )
            except Exception as e:
                errors.append(e)

        for i in range(12):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        # 再読込して12件すべてが保存されているか検証
        cp2 = MigrationCheckpoint(self.checkpoint_file)
        self.assertTrue(cp2.load())
        self.assertEqual(len(cp2.nodes), 12)
        for i in range(12):
            self.assertTrue(cp2.is_pass1_done(f"thread_node_{i}"))

    def test_concurrent_child_page_creation(self):
        """--page-workers に基づいて子ページが並行作成されることを検証"""
        db_node = PageNode(id="db_root", title="マスタータスクDB", node_type="database")
        for i in range(4):
            child = PageNode(id=f"task_{i+1}", title=f"タスク {i+1}", node_type="database_row")
            db_node.children.append(child)

        importer = ConfluenceImporter(
            url="https://example.atlassian.net",
            username="user",
            token="tok",
            space_key="TEST",
            checkpoint=self.checkpoint,
            page_workers=4,
        )

        mock_client = MagicMock(spec=ConfluenceClient)
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.get_page_by_title.return_value = None
        mock_client.get_database_by_title.return_value = None
        mock_client.get_space_id.return_value = "space_123"

        created_pages = []
        lock = threading.Lock()

        def fake_create_page(space_key, title, body, parent_id=None):
            with lock:
                pid = f"conf_{len(created_pages) + 1}"
                created_pages.append(title)
                return {"id": pid, "_links": {"webui": f"/pages/{pid}"}}

        mock_client.create_page.side_effect = fake_create_page
        mock_client.create_database.return_value = {
            "id": "conf_db_99",
            "_links": {"webui": "/database/conf_db_99"},
        }
        mock_client.get_database_views.return_value = []
        mock_client.get_database_entries.return_value = []
        importer.client = mock_client

        mapping = importer.import_tree(db_node)

        # 4つの子ページが作成され、DBも作成されたことを確認
        self.assertEqual(len(created_pages), 4)
        self.assertIn("db_root", mapping)
        self.assertEqual(mapping["db_root"]["confluence_id"], "conf_db_99")
        for i in range(4):
            self.assertIn(f"task_{i+1}", mapping)

        # create_database に渡された CSV に全子ページのリンクが含まれていることを検証
        call_args = mock_client.create_database.call_args
        csv_body = call_args[1]["csv_body"]
        for i in range(4):
            self.assertIn(f"conf_{i+1}", csv_body)

    def test_approach_b_deferred_indexing_and_resume(self):
        """
        アプローチB:
        1. Pass 1でインデックス未完了時、待機ブロックせずに pending_row_views を記録して完了する
        2. --resume (resolve_pending_database_row_views) でインデックス完了エントリを検知し行ビューを埋め込む
        """
        db_node = PageNode(id="db_root", title="案件管理DB", node_type="database")
        c1 = PageNode(id="row_1", title="案件A", node_type="database_row")
        c2 = PageNode(id="row_2", title="案件B", node_type="database_row")
        db_node.children = [c1, c2]

        importer = ConfluenceImporter(
            url="https://example.atlassian.net",
            username="user",
            token="tok",
            space_key="TEST",
            checkpoint=self.checkpoint,
            page_workers=2,
        )

        mock_client = MagicMock(spec=ConfluenceClient)
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.get_page_by_title.return_value = None
        mock_client.get_database_by_title.return_value = None
        mock_client.get_space_id.return_value = "space_123"

        def fake_create_page(space_key, title, body, parent_id=None):
            pid = "101" if title == "案件A" else "102"
            return {"id": pid, "_links": {"webui": f"/pages/{pid}"}}

        mock_client.create_page.side_effect = fake_create_page
        mock_client.create_database.return_value = {
            "id": "db_500",
            "_links": {"webui": "/database/db_500"},
        }
        mock_client.get_database_views.return_value = []

        # Pass 1: インデックス未完了（空のエントリリストを返す）
        mock_client.get_database_entries.return_value = []
        importer.client = mock_client

        mapping = importer.import_tree(db_node)

        # 1. Pass 1 が即時終了し、pending_row_views に row_1, row_2 が記録されていることを確認
        self.assertEqual(mapping["db_root"]["pending_row_views"], ["row_1", "row_2"])
        cp_info = self.checkpoint.get_node_info("db_root")
        self.assertEqual(cp_info["pending_row_views"], ["row_1", "row_2"])

        # 2. Confluenceバックエンドのインデックスが完了した状態をシミュレート
        mock_client.get_database_entries.return_value = [
            {
                "id": "entry_aaa",
                "values": {
                    "field_link": {
                        "type": "pageLink",
                        "value": [{"contentId": "101"}]
                    }
                }
            },
            {
                "id": "entry_bbb",
                "values": {
                    "field_link": {
                        "type": "pageLink",
                        "value": [{"contentId": "102"}]
                    }
                }
            }
        ]
        mock_client.get_page_by_id.return_value = {"version": {"number": 1}}

        # 3. --resume 実行（resolve_pending_database_row_views）
        mock_client.update_page.reset_mock()
        importer.resolve_pending_database_row_views(db_node)

        # 4. update_page が各子ページに対して行ビュー（entry_aaa, entry_bbb）とともに呼ばれたか検証
        self.assertEqual(mock_client.update_page.call_count, 2)
        update_calls = mock_client.update_page.call_args_list

        called_entry_ids = []
        for call in update_calls:
            body = call[1]["body"]
            if "entryId=entry_aaa" in body:
                called_entry_ids.append("entry_aaa")
            if "entryId=entry_bbb" in body:
                called_entry_ids.append("entry_bbb")

        self.assertIn("entry_aaa", called_entry_ids)
        self.assertIn("entry_bbb", called_entry_ids)

        # 5. チェックポイントの pending_row_views が解消されていることを検証
        remaining_pending = self.checkpoint.get_pending_row_views("db_root")
        self.assertEqual(remaining_pending, [])


if __name__ == "__main__":
    unittest.main()
