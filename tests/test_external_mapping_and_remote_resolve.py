"""別実行・別環境間の分割移行におけるクロスZIP DBビュー・参照解決テスト (TCK_00022)。

検証項目:
1. 先行移行のマッピングファイル (--external-mapping) の読込・マージ機構:
   - migration_result.json 形式 ({"mapping": {...}}) および 直接辞書形式 ({nid: {...}}) の両方に対応
   - migration_map, _node_to_title, _reserved_titles, id_to_title_map への初期ロード
   - Linked Database View やページリンクが外部マッピングの情報を用いて database-view マクロ / ac:link として展開されること
2. ConfluenceClient のタイトル揺れ・プレフィックス・正規化対応とスペース内キャッシュ:
   - title_prefix や normalize_db_title による記号・コロン揺れのあるタイトル照合
   - 同一タイトルに対する過剰な API コールがスペース内キャッシュで防止されること
3. リモート動的解決 (オンデマンド解決フォールバック):
   - migration_map に未登録の未解決外部 DB・ページ参照がある場合、Confluence API 経由で既存リソースを動的検索・補完
   - リモートDBのビュー一覧 (get_database_views) を補完し、database-view マクロが正常生成されること
   - 解決済みリソースがキャッシュされ、重複検索されないこと
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.converter import NotionToConfluenceConverter
from src.extractor import PageNode, normalize_db_title
from src.importer import ConfluenceClient, ConfluenceImporter


class TestConfluenceClientTitleMatchingAndCache(unittest.TestCase):
    def setUp(self):
        self.client = ConfluenceClient("https://example.atlassian.net", "user@example.com", "tok")

    @patch("src.importer.requests.get")
    def test_get_database_by_title_with_prefix_and_normalization(self, mock_get):
        # 既存Confluence DBには [HTML] A: プロジェクト管理 というプレフィックス・全角コロン付きタイトルで存在
        existing_title = "[HTML] A: プロジェクト管理"
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "results": [
                {"id": "db_remote_1", "title": existing_title}
            ]
        }
        mock_get.return_value = mock_res

        # Notion側でのタイトルは "A：プロジェクト管理" (プレフィックスなし、全角コロン)
        found = self.client.get_database_by_title(
            space_key="TEST",
            title="A：プロジェクト管理",
            title_prefix="[HTML] ",
            use_cache=True,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "db_remote_1")
        self.assertEqual(found["title"], existing_title)

        # 2回目の同一呼び出しでキャッシュが効き、APIリクエストが追加発行されないこと
        call_count_before = mock_get.call_count
        found_cached = self.client.get_database_by_title(
            space_key="TEST",
            title="A：プロジェクト管理",
            title_prefix="[HTML] ",
            use_cache=True,
        )
        self.assertEqual(found_cached["id"], "db_remote_1")
        self.assertEqual(mock_get.call_count, call_count_before)

    @patch("src.importer.requests.get")
    def test_get_page_by_title_cql_fallback_and_cache(self, mock_get):
        # 1回目 (params title) は見つからず空、CQL検索で [HTML] 設計書 が見つかるシナリオ
        def side_effect(url, **kwargs):
            r = MagicMock()
            r.status_code = 200
            if "search" in url:
                r.json.return_value = {"results": [{"id": "page_remote_9", "title": "[HTML] システム設計書"}]}
            else:
                r.json.return_value = {"results": []}
            return r

        mock_get.side_effect = side_effect

        found = self.client.get_page_by_title(
            "TEST", "システム設計書", title_prefix="[HTML] ", cql_fallback=True, use_cache=True
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "page_remote_9")

        # キャッシュ確認
        calls_before = mock_get.call_count
        found_c = self.client.get_page_by_title(
            "TEST", "システム設計書", title_prefix="[HTML] ", cql_fallback=True, use_cache=True
        )
        self.assertEqual(found_c["id"], "page_remote_9")
        self.assertEqual(mock_get.call_count, calls_before)


class TestExternalMappingPreload(unittest.TestCase):
    def test_preload_migration_result_json(self):
        """migration_result.json 形式のファイルを事前ロードして migration_map へ正しく反映されること"""
        mapping_data = {
            "migrated_at": "2026-11-15T00:00:00Z",
            "space_key": "TEST",
            "pages_count": 2,
            "mapping": {
                "aaaa-1111-2222-3333": {
                    "confluence_id": "10001",
                    "title": "[HTML] 先行移行マスターDB",
                    "url": "https://example.atlassian.net/wiki/spaces/TEST/database/10001",
                    "node_type": "database",
                    "views": {"すべてのデータ": "v101", "ボード": "v102"},
                    "views_info": [
                        {"id": "v101", "name": "すべてのデータ", "type": "table"},
                        {"id": "v102", "name": "ボード", "type": "board"},
                    ],
                },
                "bbbb-1111-2222-3333": {
                    "confluence_id": "10002",
                    "title": "[HTML] 先行移行仕様書ページ",
                    "url": "https://example.atlassian.net/wiki/spaces/TEST/pages/10002",
                    "node_type": "page",
                },
            },
        }

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(mapping_data, f, ensure_ascii=False)
            json_path = f.name

        try:
            importer = ConfluenceImporter(
                url="https://example.atlassian.net/wiki",
                username="user@example.com",
                token="tok",
                space_key="TEST",
                external_mapping=json_path,
            )

            # migration_map にハイフン付き・ハイフンなし両方で登録されていること
            self.assertIn("aaaa-1111-2222-3333", importer.migration_map)
            self.assertIn("aaaa111122223333", importer.migration_map)
            db_entry = importer.migration_map["aaaa-1111-2222-3333"]
            self.assertEqual(db_entry["confluence_id"], "10001")
            self.assertEqual(db_entry["views"]["ボード"], "v102")

            # converter の id_to_title_map および migration_map に同期されていること
            self.assertEqual(importer.converter.id_to_title_map.get("bbbb-1111-2222-3333"), "[HTML] 先行移行仕様書ページ")
            self.assertEqual(importer.converter.id_to_title_map.get("bbbb111122223333"), "[HTML] 先行移行仕様書ページ")
            self.assertIn("[HTML] 先行移行マスターDB", importer._reserved_titles)
        finally:
            if os.path.exists(json_path):
                os.remove(json_path)


class TestRemoteDatabaseAndPageDynamicResolution(unittest.TestCase):
    def setUp(self):
        self.importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="user@example.com",
            token="tok",
            space_key="TEST",
            title_prefix="[HTML] ",
        )
        self.mock_client = MagicMock()
        self.mock_client.base_url = "https://example.atlassian.net/wiki"
        self.mock_client.auth = ("user@example.com", "tok")
        self.importer.client = self.mock_client

    def test_dynamic_resolve_remote_database_on_linked_view(self):
        """未登録の外部Linked Database ViewがConfluence API経由で動的に解決され、埋め込みHTMLが生成されること"""
        db_id = "cccc1111222233334444555566667777"
        db_title = "先行移行タスク管理DB"

        # ConfluenceClient のモック動作設定
        self.mock_client.get_database_by_title.return_value = {
            "id": "conf_db_99",
            "title": f"[HTML] {db_title}",
        }
        self.mock_client.get_database_views.return_value = [
            {"id": "v_all", "name": "すべてのデータ", "type": "table"},
            {"id": "v_board", "name": "ステータス別ボード", "type": "board"},
        ]

        # Block B 側のページ（先行移行DBを参照する linked database view を持つ）
        b_page = PageNode(
            id="page_block_b_1",
            title="Block B 開発進捗ページ",
            node_type="page",
            blocks=[
                {
                    "id": "block_ldb_1",
                    "type": "child_database",
                    "is_linked_database": True,
                    "database_id": db_id,
                    "database_title": db_title,
                    "views": [
                        {
                            "name": "ステータス別ボード",
                            "type": "board",
                            "configuration": {"group_by": {"property_name": "ステータス"}},
                        }
                    ],
                    "_is_unresolved_external_db": True,
                }
            ],
            children=[],
        )

        # 動的解決を実行
        self.importer._resolve_external_and_remote_references(b_page)

        # _is_unresolved_external_db が解消されていること
        self.assertNotIn("_is_unresolved_external_db", b_page.blocks[0])

        # migration_map にリモートDBが登録されていること
        self.assertIn(db_id, self.importer.migration_map)
        db_info = self.importer.migration_map[db_id]
        self.assertEqual(db_info["confluence_id"], "conf_db_99")
        self.assertIn("ステータス別ボード", db_info["views"])
        self.assertTrue(db_info.get("_is_remote_resolved"))

        # converter によるストレージ形式生成で database-view (embed) が正しく出力されること
        content = self.importer.converter.generate_page_content(b_page)
        self.assertIn('data-card-appearance="embed"', content)
        self.assertIn("conf_db_99", content)
        self.assertIn("savedViewId=v_board", content)
        # 未解決警告マクロが出力されていないこと
        self.assertNotIn("未展開の外部データベース", content)

    def test_dynamic_resolve_remote_page_link(self):
        """未登録の外部ページ参照がConfluence API経由で動的に解決され、ac:link が生成されること"""
        ext_page_id = "dddd1111222233334444555566667777"
        ext_page_title = "共通API仕様書"

        self.mock_client.get_database_by_title.return_value = None
        self.mock_client.get_page_by_title.return_value = {
            "id": "conf_page_88",
            "title": f"[HTML] {ext_page_title}",
        }

        page_with_link = PageNode(
            id="page_block_b_2",
            title="Block B 詳細設計",
            node_type="page",
            blocks=[
                {
                    "id": "b_link_1",
                    "type": "link_to_page",
                    "link_to_page": {"page_id": ext_page_id},
                    "_is_unresolved_external_page": True,
                },
                {
                    "id": "b_para_1",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "共通API仕様書を参照"},
                                "page_id": ext_page_id,
                                "_is_unresolved_external_page": True,
                            }
                        ]
                    },
                },
            ],
            children=[],
        )

        self.importer._resolve_external_and_remote_references(page_with_link)

        # ブロックおよびインラインの未解決フラグが解消されていること
        self.assertNotIn("_is_unresolved_external_page", page_with_link.blocks[0])
        self.assertNotIn("_is_unresolved_external_page", page_with_link.blocks[1]["paragraph"]["rich_text"][0])

        # migration_map に登録されていること
        self.assertIn(ext_page_id, self.importer.migration_map)
        self.assertEqual(self.importer.migration_map[ext_page_id]["confluence_id"], "conf_page_88")

        # converter によるストレージ形式生成で ac:link が正しく出力されること
        content = self.importer.converter.generate_page_content(page_with_link)
        self.assertIn(f'<ri:page ri:content-title="[HTML] {ext_page_title}" />', content)
        self.assertNotIn("移行対象外の外部ページ", content)


class TestSplitExecutionCrossZipScenario(unittest.TestCase):
    """先行実行 (Run 1: Block A) と後発実行 (Run 2: Block B) の分割移行シナリオの統合テスト"""

    def test_run2_imports_mapping_and_resolves_run1_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Run 1 (Block A) のマイグレーション結果 JSON を作成
            run1_result_path = os.path.join(tmpdir, "migration_result_run1.json")
            db_a_id = "aaaa0000000000000000000000000010"
            page_a_id = "aaaa0000000000000000000000000020"
            run1_mapping = {
                "space_key": "TEST",
                "mapping": {
                    db_a_id: {
                        "confluence_id": "conf_db_run1_01",
                        "title": "[HTML] A-DB 基盤プロジェクト管理",
                        "url": "https://example.atlassian.net/wiki/spaces/TEST/database/conf_db_run1_01",
                        "node_type": "database",
                        "views": {"すべてのデータ": "v_run1_all", "ステータス別ボード": "v_run1_board"},
                        "views_info": [
                            {"id": "v_run1_all", "name": "すべてのデータ", "type": "table"},
                            {"id": "v_run1_board", "name": "ステータス別ボード", "type": "board"},
                        ],
                    },
                    page_a_id: {
                        "confluence_id": "conf_page_run1_02",
                        "title": "[HTML] A-01 テンプレート見出し集",
                        "url": "https://example.atlassian.net/wiki/spaces/TEST/pages/conf_page_run1_02",
                        "node_type": "page",
                    },
                },
            }
            with open(run1_result_path, "w", encoding="utf-8") as f:
                json.dump(run1_mapping, f, ensure_ascii=False)

            # 2. Run 2 (Block B) の Importer を初期化（Run 1 のマッピングを注入）
            importer_run2 = ConfluenceImporter(
                url="https://example.atlassian.net/wiki",
                username="user@example.com",
                token="tok",
                space_key="TEST",
                title_prefix="[HTML] ",
                external_mapping=run1_result_path,
            )

            mock_client = MagicMock()
            mock_client.base_url = "https://example.atlassian.net/wiki"
            mock_client.create_page.return_value = {"id": "conf_page_b_01"}
            mock_client.get_page_by_title.return_value = None
            mock_client.get_database_by_title.return_value = None
            mock_client.get_database_views.return_value = []
            importer_run2.client = mock_client

            # Block B のページツリー（Block A の DB と ページを参照）
            block_b_tree = PageNode(
                id="bbbb0000000000000000000000000001",
                title="Block B 開発・参照側",
                node_type="page",
                blocks=[
                    {
                        "id": "b_ldb_ref",
                        "type": "child_database",
                        "is_linked_database": True,
                        "database_id": db_a_id,
                        "database_title": "A-DB 基盤プロジェクト管理",
                        "views": [{"name": "ステータス別ボード", "type": "board"}],
                        "_is_unresolved_external_db": True,
                    },
                    {
                        "id": "b_link_page_ref",
                        "type": "link_to_page",
                        "link_to_page": {"page_id": page_a_id},
                        "_is_unresolved_external_page": True,
                    },
                ],
                children=[],
            )

            # インポート実行
            mapping = importer_run2.import_tree(block_b_tree, parent_id="root_parent", overwrite=True)

            # Block B のノードが移行マップに追加されていること
            self.assertIn("bbbb0000000000000000000000000001", mapping)
            # 先行移行された Block A の DB 情報が保持されていること
            self.assertIn(db_a_id, mapping)
            self.assertEqual(mapping[db_a_id]["confluence_id"], "conf_db_run1_01")

            # create_page または update_page に渡された body の検証
            # Pass 2 で更新されたページの body に、A-DB の埋め込みスマートカードと、A-01 へのリンクが含まれること
            update_call = mock_client.update_page.call_args
            self.assertIsNotNone(update_call)
            body_arg = update_call.kwargs.get("body", "")
            self.assertIn('data-card-appearance="embed"', body_arg)
            self.assertIn("conf_db_run1_01", body_arg)
            self.assertIn("savedViewId=v_run1_board", body_arg)
            self.assertIn('<ri:page ri:content-title="[HTML] A-01 テンプレート見出し集" />', body_arg)
            self.assertNotIn("未展開の外部データベース", body_arg)


if __name__ == "__main__":
    unittest.main()

