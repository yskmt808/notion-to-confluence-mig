"""
同期ブロック内のコールアウト抽出・描画、コールアウト内ページ内リンク名保持、無題DB検証のユニットテスト
"""

import unittest
from unittest.mock import MagicMock, patch
from src.extractor import NotionExtractor, PageNode
from src.converter import NotionToConfluenceConverter
from src.verifier import MigrationVerifier
from src.importer import ConfluenceClient
from storage_xml_helper import assert_valid_storage_xml


class TestSyncedBlockInPageLinkUntitledDb(unittest.TestCase):

    def test_synced_block_reference_fetches_original_children(self):
        """同期ブロック（参照側 duplicate）が同期元ブロックの子要素（コールアウト）を再帰取得すること"""
        extractor = NotionExtractor(token="dummy_token")
        
        # モックの作成:
        # parent_page -> contains synced_block_dup (synced_from: block_id="orig_id")
        # orig_id -> contains callout ("同期ブロック１")
        def mock_list_children(block_id, start_cursor=None, page_size=100):
            if block_id == "page_id":
                return {
                    "results": [{
                        "id": "dup_block_id",
                        "type": "synced_block",
                        "has_children": True,
                        "synced_block": {
                            "synced_from": {"type": "block_id", "block_id": "orig_block_id"}
                        }
                    }],
                    "has_more": False
                }
            elif block_id == "orig_block_id":
                return {
                    "results": [{
                        "id": "callout_id",
                        "type": "callout",
                        "has_children": False,
                        "callout": {
                            "rich_text": [{"type": "text", "text": {"content": "🍎 同期ブロック１"}, "plain_text": "🍎 同期ブロック１"}],
                            "color": "blue_background"
                        }
                    }],
                    "has_more": False
                }
            return {"results": [], "has_more": False}

        extractor.notion.blocks.children.list = MagicMock(side_effect=mock_list_children)
        blocks, children = extractor._extract_blocks_and_children("page_id")

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "synced_block")
        # 同期元の callout が children_blocks として抽出されていること
        self.assertEqual(len(blocks[0]["children_blocks"]), 1)
        self.assertEqual(blocks[0]["children_blocks"][0]["type"], "callout")
        self.assertEqual(
            blocks[0]["children_blocks"][0]["callout"]["rich_text"][0]["plain_text"],
            "🍎 同期ブロック１"
        )

    def test_synced_block_unwrapping_and_rendering(self):
        """同期ブロックが入れ子になっている場合にアンラップされ、コールアウトが描画されること"""
        conv = NotionToConfluenceConverter()
        blocks = [{
            "type": "synced_block",
            "children_blocks": [{
                "type": "synced_block",
                "children_blocks": [{
                    "type": "callout",
                    "callout": {
                        "rich_text": [{"type": "text", "text": {"content": "同期ブロック内のコールアウト本文"}}],
                        "color": "blue_background"
                    }
                }]
            }]
        }]

        html_out = conv.blocks_to_storage_format(blocks)
        assert_valid_storage_xml(self, html_out)
        # 紫色ボーダーのdivが1つだけ存在すること（多重ネストがアンラップされていること）
        self.assertEqual(html_out.count('border-left: 3px solid #8777D9'), 1)
        # コールアウトの内容が含まれていること
        self.assertIn("同期ブロック内のコールアウト本文", html_out)

    def test_inpage_anchor_link_preserves_custom_text(self):
        """コールアウト内のページ内リンク（#anchor付き）で、表示テキストがページ名に置き換わらず保持されること"""
        conv = NotionToConfluenceConverter()
        conv.migration_map = {
            "4137be77284d804189c8d080073331b4": {
                "confluence_id": "99999",
                "title": "エラーパターン",
                "url": "https://example.atlassian.net/wiki/spaces/TEST/pages/99999",
                "node_type": "page"
            }
        }

        # 熊本城 (画像へのページ内リンク), 見出し１ (見出しへのページ内リンク)
        rich_text = [
            {
                "type": "text",
                "text": {
                    "content": "熊本城",
                    "link": {"url": "/p/4137be77284d804189c8d080073331b4#4137be77284d80739dcaeedb54b028dd"}
                },
                "plain_text": "熊本城"
            },
            {
                "type": "text",
                "text": {"content": " | "},
                "plain_text": " | "
            },
            {
                "type": "text",
                "text": {
                    "content": "見出し１",
                    "link": {"url": "/p/4137be77284d804189c8d080073331b4#4157be77284d80e9a11bd66196c4f6e7"}
                },
                "plain_text": "見出し１"
            }
        ]

        html_out = conv.convert_rich_text(rich_text)
        assert_valid_storage_xml(self, html_out)
        # ac:card-appearance="inline" が付与されていないこと（付与されるとConfluence側でページ名に強制置換される）
        self.assertNotIn('ac:card-appearance="inline"', html_out)
        # 表示テキスト "熊本城" と "見出し１" が保持されていること
        self.assertIn('<ac:link-body>熊本城</ac:link-body>', html_out)
        self.assertIn('<ac:link-body>見出し１</ac:link-body>', html_out)
        # アンカー属性 ac:anchor が付与されていること
        self.assertIn('ac:anchor="4137be77284d80739dcaeedb54b028dd"', html_out)
        self.assertIn('ac:anchor="4157be77284d80e9a11bd66196c4f6e7"', html_out)
        # リンク先ページタイトルが設定されていること
        self.assertIn('<ri:page ri:content-title="エラーパターン" />', html_out)

    def test_get_database_by_title_strict_matching(self):
        """get_database_by_title が部分一致で無関係なデータベースを誤返却せず、厳密一致のみ返すこと"""
        client = ConfluenceClient("https://example.atlassian.net/wiki", "user", "token")
        
        # CQL検索結果に 'Test database' や 'リリース履歴' が含まれるが、
        # 探している 'Untitled Database' は存在しない場合
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"id": "111", "title": "Test database 2026-11-07"},
                {"id": "222", "title": "リリース履歴"}
            ]
        }

        with patch("requests.get", return_value=mock_response):
            found = client.get_database_by_title("TEST", "Untitled Database")
            # 誤って results[0] を返さず、None を返すこと
            self.assertIsNone(found)

    def test_verifier_accepts_untitled_database(self):
        """タイトルが空または Untitled Database のDBノードがタイトル検証でFAILにならないこと"""
        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.auth = ("user", "token")
        mock_client.get_database_views.return_value = [{"name": "すべてのデータ", "type": "table"}]
        mock_client.get_database_entries.return_value = []

        verifier = MigrationVerifier(mock_client)

        db_node = PageNode(
            id="db_uuid",
            title="Untitled Database",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}}},
            children=[]
        )

        mock_db_res = MagicMock()
        mock_db_res.ok = True
        mock_db_res.json.return_value = {
            "id": "conf_db_id",
            "title": "Untitled Database",
            "parentId": "parent_page_id"
        }

        with patch("requests.get", return_value=mock_db_res):
            res = verifier.verify_database_node(db_node, "conf_db_id", expected_parent_id="parent_page_id")
            self.assertEqual(res["status"], "PASS")
            self.assertEqual(len(res["issues"]), 0)

    def test_heading_anchor_macro_generation(self):
        """見出しにアンカーマクロ（見出し名およびNotionブロックID）が自動配置されること"""
        conv = NotionToConfluenceConverter()
        blocks = [
            {
                "id": "4157be77-284d-80e9-a11b-d66196c4f6e7",
                "type": "heading_1",
                "heading_1": {
                    "rich_text": [{"type": "text", "text": {"content": "見出し１"}, "plain_text": "見出し１"}]
                }
            },
            {
                "id": "4157be77-284d-80f7-8a09-fecacd375ec4",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "見出し２"}, "plain_text": "見出し２"}]
                }
            }
        ]
        html_out = conv.blocks_to_storage_format(blocks)
        assert_valid_storage_xml(self, html_out)
        self.assertIn('<ac:parameter ac:name="">見出し１</ac:parameter>', html_out)
        self.assertIn('<ac:parameter ac:name="">4157be77284d80e9a11bd66196c4f6e7</ac:parameter>', html_out)
        self.assertIn('<ac:parameter ac:name="">見出し２</ac:parameter>', html_out)
        self.assertIn('<ac:parameter ac:name="">4157be77284d80f78a09fecacd375ec4</ac:parameter>', html_out)

    def test_inpage_link_automatic_heading_anchor_fallback(self):
        """Notion HTMLエクスポートで#フラグメントが欠落して自ページリンクになっている場合でも、
        リンクテキストが見出し名と一致すれば ac:anchor が自動補完されること"""
        conv = NotionToConfluenceConverter()
        page_node = PageNode(
            id="4137be77-284d-8041-89c8-d080073331b4",
            title="エラーパターン",
            node_type="page",
            blocks=[
                {
                    "id": "callout_1",
                    "type": "callout",
                    "callout": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": "見出し１",
                                    # Notionエクスポート不具合で #4157... が落ちてファイル名のみのリンクになっている
                                    "link": {"url": "エラーパターン 4137be77284d804189c8d080073331b4.html"}
                                },
                                "plain_text": "見出し１",
                                "page_id": "4137be77284d804189c8d080073331b4"
                            },
                            {
                                "type": "text",
                                "text": {"content": " | "},
                                "plain_text": " | "
                            },
                            {
                                "type": "text",
                                "text": {
                                    "content": "熊本城",
                                    # 画像リンク（見出しではない）
                                    "link": {"url": "エラーパターン 4137be77284d804189c8d080073331b4.html"}
                                },
                                "plain_text": "熊本城",
                                "page_id": "4137be77284d804189c8d080073331b4"
                            }
                        ]
                    }
                },
                {
                    "id": "4157be77-284d-80e9-a11b-d66196c4f6e7",
                    "type": "heading_1",
                    "heading_1": {
                        "rich_text": [{"type": "text", "text": {"content": "見出し１"}, "plain_text": "見出し１"}]
                    }
                }
            ]
        )
        storage = conv.generate_page_content(page_node)
        assert_valid_storage_xml(self, storage)
        # 「見出し１」リンクには ac:anchor="見出し１" が自動補完されること
        self.assertIn('ac:anchor="見出し１"', storage)
        self.assertIn('<ac:link-body>見出し１</ac:link-body>', storage)
        # 「熊本城」は見出しではないため ac:anchor は付与されず、テキストが保持された自ページリンクとなること
        self.assertIn('<ac:link-body>熊本城</ac:link-body>', storage)
        self.assertNotIn('ac:anchor="熊本城"', storage)

    def test_image_anchor_generation_when_referenced(self):
        """画像ブロックIDがリンクからアンカー参照されている場合、画像要素にアンカーマクロが付与されること"""
        conv = NotionToConfluenceConverter()
        page_node = PageNode(
            id="page_1",
            title="Image Anchor Page",
            node_type="page",
            blocks=[
                {
                    "id": "p_link",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "画像へジャンプ", "link": {"url": "page_1.html#img_block_123"}},
                                "plain_text": "画像へジャンプ",
                                "page_id": "page_1"
                            }
                        ]
                    }
                },
                {
                    "id": "img_block_123",
                    "type": "image",
                    "image": {
                        "type": "file",
                        "file": {"url": "test.png"},
                        "caption": []
                    },
                    "_attachment_filename": "test.png"
                }
            ]
        )
        storage = conv.generate_page_content(page_node)
        assert_valid_storage_xml(self, storage)
        # 画像要素の直前にアンカーマクロが生成されていること
        self.assertIn('<ac:structured-macro ac:name="anchor"><ac:parameter ac:name="">img_block_123</ac:parameter></ac:structured-macro>', storage)
        # リンク側に ac:anchor="img_block_123" が設定されていること
        self.assertIn('ac:anchor="img_block_123"', storage)


if __name__ == "__main__":
    unittest.main()

