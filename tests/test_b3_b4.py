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

    @patch("requests.post")
    @patch("requests.get")
    def test_enrich_database_schema_and_views(self, mock_get, mock_post):
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
        mock_post.return_value = MagicMock(status_code=404, ok=False)

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

    @patch("requests.post")
    @patch("requests.get")
    def test_enrich_workspace_users_db_query_and_homonyms(self, mock_get, mock_post):
        # 1. /v1/users (2ページ分のページネーション + 同姓同名ユーザー)
        users_page1 = MagicMock(status_code=200, ok=True)
        users_page1.json.return_value = {
            "results": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "type": "person",
                    "name": "山田花子",
                    "person": {"email": "hanako@example.com"},
                },
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "type": "person",
                    "name": "田中太郎",
                    "person": {"email": "taro.a@example.com"},
                },
            ],
            "has_more": True,
            "next_cursor": "cursor_2",
        }
        users_page2 = MagicMock(status_code=200, ok=True)
        users_page2.json.return_value = {
            "results": [
                {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "type": "person",
                    "name": "田中太郎",
                    "person": {"email": "taro.b@example.com"},
                },
            ],
            "has_more": False,
            "next_cursor": None,
        }

        # 2. DBスキーマ
        db_res = MagicMock(status_code=200, ok=True)
        db_res.json.return_value = {
            "properties": {
                "タスク名": {"type": "title"},
                "担当者": {"type": "people"},
                "作成者": {"type": "created_by"},
                "最終更新者": {"type": "last_edited_by"},
            }
        }

        def get_side_effect(url, **kwargs):
            if url.endswith("/v1/users"):
                params = kwargs.get("params") or {}
                if params.get("start_cursor") == "cursor_2":
                    return users_page2
                return users_page1
            if "/databases/" in url:
                return db_res
            return MagicMock(status_code=404, ok=False)

        mock_get.side_effect = get_side_effect

        # 3. DB query レスポンス (1コールで people / created_by / last_edited_by を一括取得)
        query_res = MagicMock(status_code=200, ok=True)
        query_res.json.return_value = {
            "results": [
                {
                    "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "created_time": "2026-11-01T10:00:00.000Z",
                    "last_edited_time": "2026-11-02T12:00:00.000Z",
                    "created_by": {"id": "11111111-1111-1111-1111-111111111111"},
                    "last_edited_by": {"id": "22222222-2222-2222-2222-222222222222"},
                    "properties": {
                        "タスク名": {"type": "title", "title": [{"plain_text": "Row A"}]},
                        "担当者": {
                            "type": "people",
                            "people": [{"id": "11111111-1111-1111-1111-111111111111"}],
                        },
                        "作成者": {
                            "type": "created_by",
                            "created_by": {"id": "11111111-1111-1111-1111-111111111111"},
                        },
                        "最終更新者": {
                            "type": "last_edited_by",
                            "last_edited_by": {"id": "22222222-2222-2222-2222-222222222222"},
                        },
                    },
                }
            ],
            "has_more": False,
        }
        mock_post.return_value = query_res

        row_a = PageNode(
            id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            title="Row A",
            node_type="database_row",
            properties={"担当者": "山田花子"},
        )
        row_b = PageNode(
            id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            title="Row B",
            node_type="database_row",
            properties={"担当者": {"type": "people", "users": [{"name": "田中太郎"}]}},
        )
        db_node = PageNode(
            id="dddddddddddddddddddddddddddddddd",
            title="Tasks DB",
            node_type="database",
            children=[row_a, row_b],
        )
        root = PageNode(
            id="root_page",
            title="Root Page",
            node_type="page",
            comments=[{"created_by": {"name": "山田花子"}}],
            blocks=[
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "mention", "mention": {"type": "user", "user": {"name": "山田花子"}}},
                            {"type": "mention", "mention": {"type": "user", "user": {"name": "田中太郎"}}},
                        ]
                    },
                }
            ],
            children=[db_node],
        )

        summary = self.enricher.enrich_tree(root)

        self.assertEqual(summary["users_fetched"], 3)
        self.assertEqual(summary["rows_enriched"], 1)

        # GET は /v1/users x2 + DB schema x1 + views x1 のみ（ブロック再帰GET等は0回）
        called_urls = [c.args[0] for c in mock_get.call_args_list]
        self.assertFalse(any("/blocks/" in u or "/pages/" in u for u in called_urls))
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["json"]["page_size"], 100)

        # row_a の people / created_by / last_edited_by が ID と /v1/users 辞書で補完されていること
        self.assertEqual(row_a.created_time, "2026-11-01T10:00:00.000Z")
        self.assertEqual(row_a.created_by["name"], "山田花子")
        self.assertEqual(row_a.created_by["person"]["email"], "hanako@example.com")
        self.assertEqual(row_a.last_edited_by["name"], "田中太郎")
        self.assertEqual(row_a.properties["担当者"]["type"], "people")
        self.assertEqual(row_a.properties["担当者"]["users"][0]["name"], "山田花子")
        self.assertEqual(row_a.properties["担当者"]["users"][0]["person"]["email"], "hanako@example.com")
        self.assertEqual(row_a.properties["作成者"]["type"], "created_by")
        self.assertEqual(row_a.properties["作成者"]["users"][0]["name"], "山田花子")

        # 一意なユーザー「山田花子」のコメント・本文メンションは追加API 0回で補完される
        self.assertEqual(root.comments[0]["created_by"]["id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(root.comments[0]["created_by"]["person"]["email"], "hanako@example.com")
        hanako_mention = root.blocks[0]["paragraph"]["rich_text"][0]["mention"]["user"]
        self.assertEqual(hanako_mention["id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(hanako_mention["person"]["email"], "hanako@example.com")

        # 同姓同名「田中太郎」は名前のみ参照を自動確定しない + 警告を返す
        taro_mention = root.blocks[0]["paragraph"]["rich_text"][1]["mention"]["user"]
        self.assertNotIn("id", taro_mention)
        self.assertNotIn("id", row_b.properties["担当者"]["users"][0])

        warnings = summary["homonym_warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["name"], "田中太郎")
        self.assertEqual(len(warnings[0]["candidates"]), 2)
        self.assertIn("ページ 'Root Page' の本文メンション", warnings[0]["affected_locations"])
        self.assertIn("DB行 'Row B' の '担当者' 列", warnings[0]["affected_locations"])


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

