import os
import tempfile
import unittest
from src.extractor import PageNode
from src.converter import NotionToConfluenceConverter, DEFAULT_METADATA_TEMPLATE
from src.user_mapper import UserMappingManager
from storage_xml_helper import assert_valid_storage_xml


class TestDatabaseRowLayoutAndMetadata(unittest.TestCase):
    def setUp(self):
        self.user_mapper = UserMappingManager()
        self.user_mapper.add_mapping(
            notion_id="411d872b-0000-0000-0000-000000000001",
            notion_name="TestAdmin",
            notion_email="admin@example.com",
            confluence_account_id="712020:00000000-0000-0000-0000-000000000001",
            confluence_display_name="Test Admin",
        )
        self.conv = NotionToConfluenceConverter(timezone="Asia/Tokyo", user_mapper=self.user_mapper)

    def test_metadata_header_default(self):
        node = PageNode(
            id="page-1",
            title="テストページ",
            node_type="page",
            created_time="2026-11-08T11:14:00.000Z",
            created_by={"id": "411d872b-0000-0000-0000-000000000001"},
            last_edited_time="2026-11-08T11:20:00.000Z",
        )
        header_html = self.conv._render_metadata_header(node)
        self.assertIn("Notion元情報", header_html)
        self.assertIn('ri:account-id="712020:00000000-0000-0000-0000-000000000001"', header_html)
        self.assertIn("2026-11-08 20:14", header_html)

    def test_metadata_header_custom_template(self):
        custom_tpl = "📌 著者: {author} (作成: {created_date} / 更新: {last_edited_time})"
        conv = NotionToConfluenceConverter(
            timezone="Asia/Tokyo",
            user_mapper=self.user_mapper,
            metadata_template=custom_tpl,
        )
        node = PageNode(
            id="page-1",
            title="テストページ",
            node_type="page",
            created_time="2026-11-08T11:14:00.000Z",
            created_by={"id": "411d872b-0000-0000-0000-000000000001"},
            last_edited_time="2026-11-08T11:20:00.000Z",
        )
        header_html = conv._render_metadata_header(node)
        self.assertIn("📌 著者:", header_html)
        self.assertIn('ri:account-id="712020:00000000-0000-0000-0000-000000000001"', header_html)
        self.assertIn("作成: 2026-11-08", header_html)
        self.assertIn("更新: 2026-11-08 20:20", header_html)

    def test_metadata_header_disabled_empty(self):
        conv = NotionToConfluenceConverter(
            timezone="Asia/Tokyo",
            user_mapper=self.user_mapper,
            metadata_template="",
        )
        node = PageNode(
            id="page-1",
            title="テストページ",
            node_type="page",
            created_time="2026-11-08T11:14:00.000Z",
            created_by={"id": "411d872b-0000-0000-0000-000000000001"},
        )
        header_html = conv._render_metadata_header(node)
        self.assertEqual(header_html, "")

    def test_metadata_header_disabled_none_keyword(self):
        conv = NotionToConfluenceConverter(
            timezone="Asia/Tokyo",
            user_mapper=self.user_mapper,
            metadata_template="none",
        )
        node = PageNode(
            id="page-1",
            title="テストページ",
            node_type="page",
            created_time="2026-11-08T11:14:00.000Z",
            created_by={"id": "411d872b-0000-0000-0000-000000000001"},
        )
        header_html = conv._render_metadata_header(node)
        self.assertEqual(header_html, "")

    def test_metadata_header_from_file(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as tf:
            tf.write("<p class='custom-meta'>Author: {author_name} ({created_time})</p>")
            tf_path = tf.name

        try:
            conv = NotionToConfluenceConverter(
                timezone="Asia/Tokyo",
                user_mapper=self.user_mapper,
                metadata_template=tf_path,
            )
            node = PageNode(
                id="page-1",
                title="テストページ",
                node_type="page",
                created_time="2026-11-08T11:14:00.000Z",
                created_by={"id": "411d872b-0000-0000-0000-000000000001", "name": "TestAdmin"},
            )
            header_html = conv._render_metadata_header(node)
            self.assertIn("custom-meta", header_html)
            self.assertIn("Author: TestAdmin", header_html)
            self.assertIn("2026-11-08 20:14", header_html)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_database_row_empty_body_collapsed_properties(self):
        """本文を持たないレコード（テスト１行目など）の出力検証"""
        row_node = PageNode(
            id="row-1",
            title="テスト１行目",
            node_type="database_row",
            properties={"ステータス": {"type": "select", "value": "未着手"}},
            blocks=[],
            created_time="2026-11-08T11:14:00.000Z",
            created_by={"id": "411d872b-0000-0000-0000-000000000001"},
        )
        setattr(row_node, "parent_db_title", "テストDB直接リクエスト")

        html_out = self.conv.generate_page_content(row_node)
        assert_valid_storage_xml(self, html_out)

        # 1. メタデータヘッダが存在する
        self.assertIn("Notion元情報", html_out)
        self.assertIn('ri:account-id="712020:00000000-0000-0000-0000-000000000001"', html_out)

        # 2. 親DBナビゲーションリンクが存在する
        self.assertIn("所属データベース", html_out)
        self.assertIn("テストDB直接リクエスト", html_out)

        # 3. プロパティテーブルが expand マクロで折りたたまれている
        self.assertIn('<ac:structured-macro ac:name="expand">', html_out)
        self.assertIn('ac:name="title">📋 レコードプロパティ・行編集 (クリックして展開)', html_out)
        self.assertIn("ステータス", html_out)
        self.assertIn("未着手", html_out)

        # 4. 本文なしの場合、「（レコードの詳細本文はありません）」や不要な「<hr />」が出力されないこと
        self.assertNotIn("レコードの詳細本文はありません", html_out)
        self.assertNotIn("<hr />", html_out)

    def test_database_row_with_body_and_embedded_view(self):
        """本文を持つレコード（テスト２行目など）の出力検証"""
        row_node = PageNode(
            id="row-2",
            title="テスト２行目",
            node_type="database_row",
            properties={"ステータス": {"type": "select", "value": "進行中"}},
            blocks=[{
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": "本文が存在するページ"}}]
                }
            }],
            created_time="2026-11-08T11:15:00.000Z",
            created_by={"id": "411d872b-0000-0000-0000-000000000001"},
        )
        setattr(row_node, "parent_db_title", "テストDB直接リクエスト")
        setattr(row_node, "_entry_embed_url", "https://example.atlassian.net/wiki/database/123?entryId=456")

        html_out = self.conv.generate_page_content(row_node)
        assert_valid_storage_xml(self, html_out)

        # 1. Expandマクロ内にネイティブ行ビューURLのみ格納され、静的プロパティテーブルは重複排除されること
        self.assertIn('<ac:structured-macro ac:name="expand">', html_out)
        self.assertIn('https://example.atlassian.net/wiki/database/123?entryId=456', html_out)
        self.assertIn('data-card-appearance="embed"', html_out)
        self.assertNotIn("📋 プロパティ一覧", html_out)

        # 2. レコード本文ブロックおよび区切り線が展開されている
        self.assertIn("<hr />", html_out)
        self.assertIn("レコード詳細本文", html_out)
        self.assertIn("本文が存在するページ", html_out)

    def test_database_row_parent_db_url_anchor(self):
        """親データベースリンクがURL付きの場合、ri:pageではなくaタグで出力されることの検証"""
        row_node = PageNode(
            id="row-3",
            title="テスト3行目",
            node_type="database_row",
            properties={"名前": {"type": "title", "value": "テスト3"}},
        )
        setattr(row_node, "parent_db_title", "親データベース")
        setattr(row_node, "parent_db_url", "https://example.atlassian.net/wiki/database/789")

        html_out = self.conv.generate_page_content(row_node)
        assert_valid_storage_xml(self, html_out)
        self.assertIn('<a href="https://example.atlassian.net/wiki/database/789">親データベース</a>', html_out)
        self.assertNotIn('<ri:page ri:content-title="親データベース"', html_out)


if __name__ == "__main__":
    unittest.main()
