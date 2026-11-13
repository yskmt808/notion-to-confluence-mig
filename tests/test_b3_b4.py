"""
Task B.3 (API Enrichment Mode) および Task B.4 (Worksheet Mode) のテストスイート。
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor import PageNode
from src.api_enricher import NotionApiEnricher
from src.worksheet import WorksheetManager


class TestTaskB3ApiEnricher(unittest.TestCase):
    """Task B.3: Notion API によるDB属性・ビュー情報のハイブリッド補完テスト"""

    def setUp(self):
        self.enricher = NotionApiEnricher(token="fake_token")

    @patch("requests.get")
    def test_enrich_database_schema_and_views(self, mock_get):
        # モック設定: databases.retrieve
        mock_db_res = MagicMock()
        mock_db_res.status_code = 200
        mock_db_res.ok = True
        mock_db_res.json.return_value = {
            "title": [{"plain_text": "Task DB"}],
            "properties": {
                "ステータス": {"type": "select", "select": {"options": [{"name": "進行中"}]}},
                "タグ": {"type": "multi_select", "multi_select": {"options": [{"name": "Core"}, {"name": "UI"}]}},
                "完了フラグ": {"type": "checkbox", "checkbox": {}},
            },
        }

        # モック設定: views API
        mock_views_res = MagicMock()
        mock_views_res.status_code = 200
        mock_views_res.ok = True
        mock_views_res.json.return_value = {
            "results": [{"id": "v_123"}]
        }

        mock_view_detail = MagicMock()
        mock_view_detail.status_code = 200
        mock_view_detail.ok = True
        mock_view_detail.json.return_value = {
            "id": "v_123",
            "name": "カンバン",
            "type": "board",
            "configuration": {"group_by": "ステータス"},
        }

        def get_side_effect(url, **kwargs):
            if "/views?database_id=" in url:
                return mock_views_res
            elif "/views/v_123" in url:
                return mock_view_detail
            elif "/databases/" in url:
                return mock_db_res
            return MagicMock(status_code=404, ok=False)

        mock_get.side_effect = get_side_effect

        db_node = PageNode(
            id="00000000000000000000000000000001",
            title="Task DB",
            node_type="database",
            children=[
                PageNode(
                    id="row_1",
                    title="Task 1",
                    node_type="database_row",
                    properties={
                        "ステータス": "進行中",
                        "タグ": "Core, UI",  # カンマ区切りテキスト
                        "完了フラグ": "Yes",  # 文字列Yes
                    },
                )
            ],
        )

        root = PageNode(id="root", title="Root", node_type="page", children=[db_node])

        summary = self.enricher.enrich_tree(root)

        self.assertEqual(summary["databases_enriched"], 1)
        self.assertEqual(summary["properties_mapped"], 3)
        self.assertEqual(summary["views_enriched"], 1)

        # db_nodeのスキーマがマージされていること
        schema = db_node.properties.get("schema", {})
        self.assertEqual(schema["ステータス"]["type"], "select")
        self.assertEqual(schema["タグ"]["type"], "multi_select")
        self.assertEqual(schema["完了フラグ"]["type"], "checkbox")

        # 子レコードのプロパティ型がスキーマに合わせて正規化されていること
        row = db_node.children[0]
        self.assertEqual(row.properties["ステータス"]["type"], "select")
        self.assertEqual(row.properties["タグ"]["type"], "multi_select")
        self.assertEqual(len(row.properties["タグ"]["value"]), 2)
        self.assertEqual(row.properties["完了フラグ"]["type"], "checkbox")
        self.assertTrue(row.properties["完了フラグ"]["value"])


class TestTaskB4WorksheetManager(unittest.TestCase):
    """Task B.4: 人間確認用チェックシート出力＆再読込テスト"""

    def setUp(self):
        self.ws_mgr = WorksheetManager()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.test_csv = os.path.join(self._tmpdir.name, "test_worksheet.csv")
        self.test_json = os.path.join(self._tmpdir.name, "test_worksheet.json")

        # テスト用DBツリー構築
        self.db_node = PageNode(
            id="db-001",
            title="Projects DB",
            node_type="database",
            children=[
                PageNode(
                    id="row_1",
                    title="Project A",
                    node_type="database_row",
                    properties={
                        "ステータス": "進行中",
                        "タグ": "Frontend, Backend",
                        "期日": "2026-12-31",
                    },
                )
            ],
        )
        self.tree = PageNode(id="root", title="Root", node_type="page", children=[self.db_node])

    def test_export_and_import_csv_worksheet(self):
        # 1. チェックシート出力 (CSV)
        self.ws_mgr.export_worksheet(self.tree, self.test_csv)
        self.assertTrue(os.path.exists(self.test_csv))

        with open(self.test_csv, "r", encoding="utf-8-sig") as f:
            content = f.read()

        self.assertIn("Projects DB", content)
        self.assertIn("SECTION: COLUMNS", content)
        self.assertIn("SECTION: VIEWS", content)
        self.assertIn("ステータス", content)

        # 2. 人間によるチェックシート編集のシミュレーション
        # （例: 'タグ' の confirmed_type を 'multi_select' に明示変更）
        edited_content = content.replace(",タグ,multi_select,multi_select,", ",タグ,multi_select,multi_select,")
        with open(self.test_csv, "w", encoding="utf-8-sig") as f:
            f.write(edited_content)

        # 3. チェックシート読込
        imported_data = self.ws_mgr.import_worksheet(self.test_csv)
        self.assertIn("db-001", imported_data)
        self.assertEqual(imported_data["db-001"]["columns"]["タグ"], "multi_select")

        # 4. ツリーへ適用
        stats = self.ws_mgr.apply_worksheet_to_tree(self.tree, imported_data)
        self.assertEqual(stats["databases_updated"], 1)
        self.assertEqual(self.db_node.properties["schema"]["タグ"]["type"], "multi_select")

    def test_export_and_import_json_worksheet(self):
        # 1. チェックシート出力 (JSON)
        self.ws_mgr.export_worksheet(self.tree, self.test_json)
        self.assertTrue(os.path.exists(self.test_json))

        with open(self.test_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("databases", data)
        self.assertEqual(data["databases"][0]["database_title"], "Projects DB")

        # 2. 読込と適用
        imported_data = self.ws_mgr.import_worksheet(self.test_json)
        stats = self.ws_mgr.apply_worksheet_to_tree(self.tree, imported_data)
        self.assertEqual(stats["databases_updated"], 1)


if __name__ == "__main__":
    unittest.main()

