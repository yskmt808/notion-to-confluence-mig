"""
tests/test_smart_db_views.py
オンデマンド参照（カテゴリ別ページ等）＋スマート厳選によるデータベースビュー自動生成削減の検証テスト。
"""

import unittest
from unittest.mock import MagicMock, patch

from src.extractor import PageNode
from src.importer import ConfluenceImporter
from src.converter import NotionToConfluenceConverter


class TestSmartDbViews(unittest.TestCase):
    def _create_importer(self) -> ConfluenceImporter:
        importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="test_user",
            token="test_token",
            space_key="TEST",
            page_workers=1,
        )
        importer.client = MagicMock()
        importer.client.base_url = "https://example.atlassian.net/wiki"
        importer.client.auth = ("test_user", "test_token")
        importer.client.get_page_by_title.return_value = None
        importer.client.get_database_views.return_value = [
            {"id": "v-all", "name": "すべてのデータ", "type": "table"}
        ]
        importer.client.get_database_entries.return_value = []
        return importer

    @patch("src.importer.requests")
    def test_unreferenced_db_skips_gallery_and_high_cardinality_columns(self, mock_requests):
        """未参照のDBで一般列（担当者名など）やギャラリービューが自動生成されず、代表ステータス列(2〜7値)のみ厳選されること"""
        mock_get_resp = MagicMock()
        mock_get_resp.ok = True
        mock_get_resp.json.return_value = {"version": {"number": 1}}
        mock_requests.get.return_value = mock_get_resp

        importer = self._create_importer()
        importer.client.create_page.side_effect = lambda **kw: {"id": f"p-{kw['title']}", "_links": {"webui": f"/pages/p-{kw['title']}"}}
        importer.client.create_database.return_value = {"id": "db-1", "_links": {"webui": "/spaces/TEST/database/db-1"}}

        # 10個の異なる「メモ」値を持つが、ステータスは3種類のDB
        rows = []
        statuses = ["未着手", "進行中", "完了"]
        for i in range(10):
            rows.append(
                PageNode(
                    id=f"row-{i}",
                    title=f"Item {i}",
                    node_type="database_row",
                    properties={
                        "メモ": {"type": "select", "value": f"Note_{i}"},
                        "ステータス": {"type": "select", "value": statuses[i % 3]},
                    },
                )
            )
        db_node = PageNode(id="db-node-1", title="Tasks DB", node_type="database", children=rows)
        root = PageNode(id="root-1", title="Root", node_type="page", children=[db_node])

        importer._db_view_requirements = importer._analyze_database_view_requirements(root)
        importer._import_node_recursive(db_node, parent_id="root-conf", overwrite=False)

        # ギャラリーは未参照のため作成されないこと
        importer.client.create_database_view.assert_not_called()

        # setup_grouped_database_views は「ステータス」の3値のみ呼ばれ、「メモ」の10値は呼ばれないこと
        importer.client.setup_grouped_database_views.assert_called_once_with(
            "db-1", "ステータス", ["完了", "未着手", "進行中"]
        )

        # ボードビューは「ステータス別ボード」の1回のみ PUT されること（「メモ別ボード」は作られない）
        put_calls = mock_requests.put.call_args_list
        self.assertEqual(len(put_calls), 1)
        put_csv = put_calls[0].kwargs["json"]["body"]["value"]
        self.assertIn("add_view,ステータス別ボード,board,ステータス", put_csv)

    @patch("src.importer.requests")
    def test_unreferenced_db_without_status_creates_no_extra_views(self, mock_requests):
        """ステータス列がなく他ページからも参照されていないDBでは、追加のボード・ギャラリー・フィルタービューが一切生成されないこと"""
        importer = self._create_importer()
        importer.client.create_page.side_effect = lambda **kw: {"id": f"p-{kw['title']}", "_links": {"webui": f"/pages/p-{kw['title']}"}}
        importer.client.create_database.return_value = {"id": "db-2", "_links": {"webui": "/spaces/TEST/database/db-2"}}

        rows = [
            PageNode(
                id=f"row-{i}",
                title=f"Item {i}",
                node_type="database_row",
                properties={"タグ": {"type": "select", "value": f"Tag_{i}"}},
            )
            for i in range(6)
        ]
        db_node = PageNode(id="db-node-2", title="Plain DB", node_type="database", children=rows)
        root = PageNode(id="root-2", title="Root", node_type="page", children=[db_node])

        importer._db_view_requirements = importer._analyze_database_view_requirements(root)
        importer._import_node_recursive(db_node, parent_id="root-conf", overwrite=False)

        importer.client.create_database_view.assert_not_called()
        importer.client.setup_grouped_database_views.assert_not_called()
        mock_requests.put.assert_not_called()

    @patch("src.importer.requests")
    def test_referenced_category_pages_create_only_referenced_views_without_limit(self, mock_requests):
        """別ページ（カテゴリ別ページ等）から参照されているカテゴリ値だけが件数制限なしでピンポイント作成され、埋め込み時に自動解決されること"""
        importer = self._create_importer()
        importer.client.create_page.side_effect = lambda **kw: {"id": f"p-{kw['title']}", "_links": {"webui": f"/pages/p-{kw['title']}"}}
        importer.client.create_database.return_value = {"id": "db-cat", "_links": {"webui": "/spaces/TEST/database/db-cat"}}

        # カテゴリ列に10種類のユニーク値があるDB
        categories = ["開発", "デザイン", "営業", "人事", "総務", "法務", "経理", "CS", "広報", "情シス"]
        rows = [
            PageNode(
                id=f"row-{i}",
                title=f"Doc {i}",
                node_type="database_row",
                properties={"カテゴリ": {"type": "select", "value": cat}},
            )
            for i, cat in enumerate(categories)
        ]
        db_node = PageNode(id="db-node-cat", title="Docs DB", node_type="database", children=rows)

        # 別ページ1: 「開発カテゴリページ」から DB を参照
        ref_page_dev = PageNode(
            id="page-dev",
            title="開発カテゴリページ",
            node_type="page",
            blocks=[{"id": "b1", "type": "link_to_page", "link_to_page": {"database_id": "db-node-cat"}}],
        )
        # 別ページ2: 「デザイン一覧」からギャラリー付きで参照、見出し「営業」でリンク
        ref_page_design = PageNode(
            id="page-design",
            title="デザイン一覧",
            node_type="page",
            blocks=[
                {
                    "id": "b2",
                    "type": "child_database",
                    "is_linked_database": True,
                    "database_id": "db-node-cat",
                    "views": [{"name": "ギャラリー表示", "type": "gallery"}],
                }
            ],
        )

        root = PageNode(id="root-cat", title="Root", node_type="page", children=[db_node, ref_page_dev, ref_page_design])

        importer._db_view_requirements = importer._analyze_database_view_requirements(root)
        importer._import_node_recursive(db_node, parent_id="root-conf", overwrite=False)

        # ギャラリービューがオンデマンドで作成されること
        importer.client.create_database_view.assert_called_once_with("db-cat", view_name="ギャラリービュー", layout="gallery")

        # カテゴリ列は10種類あるが、他ページから参照されている「デザイン」「開発」の2つのみ作成されること
        importer.client.setup_grouped_database_views.assert_called_once_with(
            "db-cat", "カテゴリ", ["デザイン", "開発"]
        )

        # Converter がページタイトル「開発カテゴリページ」の文脈からカテゴリビューの savedViewId を正しく解決できること
        conv = NotionToConfluenceConverter()
        conv.migration_map = {
            "db-node-cat": {
                "node_type": "database",
                "url": "https://example.atlassian.net/wiki/spaces/TEST/database/db-cat",
                "views": {
                    "すべてのデータ": "v-all",
                    "カテゴリ: 開発": "v-cat-dev",
                    "カテゴリ: デザイン": "v-cat-design",
                },
                "views_info": [],
            }
        }
        html_out = conv.generate_page_content(ref_page_dev)
        self.assertIn("savedViewId=v-cat-dev", html_out)


if __name__ == "__main__":
    unittest.main()
