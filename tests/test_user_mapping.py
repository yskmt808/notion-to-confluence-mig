import unittest
import os
import tempfile
import csv
from src.user_mapper import UserMappingManager, UserMapping
from src.converter import NotionToConfluenceConverter
from src.extractor import PageNode


class TestUserMapping(unittest.TestCase):
    def setUp(self):
        self.manager = UserMappingManager()
        self.manager.add_mapping(
            notion_name="testuser",
            notion_email="testuser@example.com",
            notion_id="412d872b-0000-0000-0000-000000000001",
            confluence_account_id="712020:00000000-0000-0000-0000-000000000001",
            confluence_display_name="testuser",
        )
        self.manager.add_mapping(
            notion_name="testadmin",
            notion_email="testadmin@example.com",
            notion_id="411d872b-0000-0000-0000-000000000002",
            confluence_account_id="712020:00000000-0000-0000-0000-000000000002",
            confluence_display_name="Test Admin",
        )

    def test_resolve_by_various_keys(self):
        # By ID
        u1 = self.manager.resolve_user(user_id="412d872b-0000-0000-0000-000000000001")
        self.assertIsNotNone(u1)
        self.assertEqual(u1.confluence_account_id, "712020:00000000-0000-0000-0000-000000000001")

        # By Email
        u2 = self.manager.resolve_user(email="testadmin@example.com")
        self.assertIsNotNone(u2)
        self.assertEqual(u2.confluence_account_id, "712020:00000000-0000-0000-0000-000000000002")

        # By Name
        u3 = self.manager.resolve_user(name="testuser")
        self.assertIsNotNone(u3)
        self.assertEqual(u3.confluence_account_id, "712020:00000000-0000-0000-0000-000000000001")

        # Unresolved
        u_none = self.manager.resolve_user(name="Unknown User")
        self.assertNull_or_None = self.assertIsNone(u_none)

    def test_format_mention(self):
        # Mapped user
        html_mapped = self.manager.format_mention(user_id="412d872b-0000-0000-0000-000000000001")
        self.assertEqual(
            html_mapped,
            '<ac:link><ri:user ri:account-id="712020:00000000-0000-0000-0000-000000000001" /></ac:link>',
        )

        # Unmapped user
        html_unmapped = self.manager.format_mention(name="テスト太郎")
        self.assertEqual(html_unmapped, "@テスト太郎")

    def test_converter_rich_text_user_mention(self):
        conv = NotionToConfluenceConverter(user_mapper=self.manager)
        rich_texts = [
            {
                "type": "mention",
                "plain_text": "@testuser",
                "mention": {
                    "type": "user",
                    "user": {
                        "id": "412d872b-0000-0000-0000-000000000001",
                        "name": "testuser",
                    },
                },
            }
        ]
        res = conv.convert_rich_text(rich_texts)
        self.assertIn('<ri:user ri:account-id="712020:00000000-0000-0000-0000-000000000001" />', res)

    def test_converter_people_property_and_csv(self):
        conv = NotionToConfluenceConverter(user_mapper=self.manager)
        prop = {
            "type": "people",
            "users": [
                {
                    "id": "412d872b-0000-0000-0000-000000000001",
                    "name": "testuser",
                }
            ],
        }
        # Render in Page Properties
        prop_html = conv.format_property_for_confluence(prop)
        self.assertIn('<ri:user ri:account-id="712020:00000000-0000-0000-0000-000000000001" />', prop_html)

        # Render in DB CSV
        db_node = PageNode(
            id="test-db",
            title="担当者管理DB",
            node_type="database",
            properties={
                "schema": {"担当者": {"type": "people"}},
            },
            children=[
                PageNode(
                    id="row-1",
                    title="タスク1",
                    node_type="database_row",
                    properties={"担当者": prop},
                )
            ],
        )
        csv_str = conv.generate_database_csv(db_node, {})
        # Should generate "user" field type and account ID in data row
        self.assertIn("担当者,user,", csv_str)
        self.assertIn("712020:00000000-0000-0000-0000-000000000001", csv_str)

    def test_converter_people_without_user_mapping_falls_back_to_text(self):
        conv_no_map = NotionToConfluenceConverter(user_mapper=None)
        prop = {
            "type": "people",
            "users": [
                {
                    "id": "412d872b-0000-0000-0000-000000000001",
                    "name": "testuser",
                }
            ],
        }
        db_node = PageNode(
            id="test-db",
            title="担当者管理DB",
            node_type="database",
            properties={
                "schema": {"担当者": {"type": "people"}},
            },
            children=[
                PageNode(
                    id="row-1",
                    title="タスク1",
                    node_type="database_row",
                    properties={"担当者": prop},
                )
            ],
        )
        csv_str = conv_no_map.generate_database_csv(db_node, {})
        # Should generate "text" field type and plain name in data row
        self.assertIn("担当者,text,", csv_str)
        self.assertIn("testuser", csv_str)

    def test_export_template(self):
        tree = PageNode(
            id="root",
            title="Root",
            node_type="page",
            comments=[
                {
                    "created_by": {
                        "id": "412d872b-0000-0000-0000-000000000001",
                        "name": "testuser",
                        "person": {"email": "testuser@example.com"},
                    }
                }
            ],
        )
        with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as tf:
            tf_path = tf.name

        try:
            count = UserMappingManager.export_template(tf_path, tree)
            self.assertEqual(count, 1)
            with open(tf_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["notion_name"], "testuser")
                self.assertEqual(rows[0]["notion_email"], "testuser@example.com")
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_collect_users_from_tree_includes_block_mentions(self):
        tree = PageNode(
            id="root",
            title="Root",
            node_type="page",
            blocks=[
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "text", "text": {"content": "担当者: "}},
                            {
                                "type": "mention",
                                "mention": {
                                    "type": "user",
                                    "user": {
                                        "id": "412d872b-0000-0000-0000-000000000001",
                                        "name": "testuser",
                                        "person": {"email": "testuser@example.com"},
                                    },
                                },
                            },
                        ]
                    },
                    "children_blocks": [
                        {
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {
                                "rich_text": [
                                    {
                                        "type": "mention",
                                        "mention": {
                                            "type": "user",
                                            "user": {"name": "nested_user"},
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                }
            ],
        )
        users = UserMappingManager.collect_users_from_tree(tree)
        names = {u["notion_name"] for u in users}
        self.assertIn("testuser", names)
        self.assertIn("nested_user", names)

    def test_html_extractor_inline_user_mention_and_people_table(self):
        import zipfile
        from src.html_extractor import HtmlZipExtractor

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "mentions.zip")
            db_id = "1234567890abcdef1234567890abcdef"
            row_id = "abcdef1234567890abcdef1234567890"
            root_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Root</title></head>
<body>
<article class="page">
  <h1 class="page-title">Root</h1>
  <div class="page-body">
    <p>確認お願いします: <span class="user">@testuser</span></p>
    <figure class="link-to-page"><a href="Tasks {db_id}.html">Tasks</a></figure>
  </div>
</article>
</body></html>"""
            db_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Tasks</title></head>
<body><article class="page"><h1 class="page-title">Tasks</h1></article></body></html>"""
            db_csv = "タスク名,担当者\nタスクA,testuser\n"
            row_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>タスクA</title></head>
<body>
<article class="page">
  <h1 class="page-title">タスクA</h1>
  <table class="properties">
    <tr class="property-row property-row-people"><th>担当者</th><td><span class="user">@testuser</span></td></tr>
  </table>
  <div class="page-body"><p>詳細本文</p></div>
</article>
</body></html>"""

            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("Root 00000000000000000000000000000001.html", root_html)
                zf.writestr(f"Root/Tasks {db_id}.html", db_html)
                zf.writestr(f"Root/Tasks {db_id}.csv", db_csv)
                zf.writestr(f"Root/Tasks/タスクA {row_id}.html", row_html)

            tree = HtmlZipExtractor(zip_path).extract_tree()
            # 本文中の <span class="user"> が user mention として抽出され、Confluence <ri:user> に変換される
            p_block = tree.blocks[0]
            rt = p_block["paragraph"]["rich_text"]
            mentions = [x for x in rt if x.get("type") == "mention"]
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["mention"]["user"]["name"], "testuser")

            conv = NotionToConfluenceConverter(user_mapper=self.manager)
            xhtml = conv.blocks_to_storage_format(tree.blocks)
            self.assertIn('<ri:user ri:account-id="712020:00000000-0000-0000-0000-000000000001" />', xhtml)

            # DB行の property-row-people が {"type": "people", "users": [...]} に正規化されていること
            db_node = tree.children[0]
            row_node = db_node.children[0]
            self.assertEqual(row_node.properties["担当者"]["type"], "people")
            self.assertEqual(row_node.properties["担当者"]["users"][0]["name"], "testuser")

            # collect_users_from_tree で本文メンションのユーザーが収集されること
            collected = UserMappingManager.collect_users_from_tree(tree)
            self.assertTrue(any(u["notion_name"] == "testuser" for u in collected))

    def test_homonym_safety_and_dry_run_warning_output(self):
        import io
        import zipfile
        from contextlib import redirect_stdout
        from unittest.mock import patch, MagicMock
        import migrate

        # 同姓同名をマッピングに登録した場合、名前のみ resolve_user は None になること
        dup_mgr = UserMappingManager()
        dup_mgr.add_mapping(notion_name="田中太郎", notion_id="id-1", notion_email="a@example.com", confluence_account_id="acc-1")
        dup_mgr.add_mapping(notion_name="田中太郎", notion_id="id-2", notion_email="b@example.com", confluence_account_id="acc-2")
        self.assertIsNone(dup_mgr.resolve_user(name="田中太郎"))
        self.assertIsNotNone(dup_mgr.resolve_user(user_id="id-2"))

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "homonym.zip")
            db_id = "1234567890abcdef1234567890abcdef"
            row_id = "abcdef1234567890abcdef1234567890"
            root_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Root</title></head>
<body>
<article class="page">
  <header>
    <h1 class="page-title">Root</h1>
    <details><summary>ページコメント</summary><ul><li><div><span class="user"><b>田中太郎</b></span></div><div>コメント</div></li></ul></details>
  </header>
  <div class="page-body">
    <p>メンション: <span class="user">@田中太郎</span></p>
  </div>
</article>
</body></html>"""
            db_html = "<html><body><article><h1 class='page-title'>DB</h1></article></body></html>"
            db_csv = "タスク名,担当者\n行1,田中太郎\n"
            row_html = """<html><head><title>行1</title></head><body><article><h1 class='page-title'>行1</h1>
<table class='properties'><tr class='property-row property-row-people'><th>担当者</th><td><span class='user'>田中太郎</span></td></tr></table>
</article></body></html>"""

            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("Root 00000000000000000000000000000001.html", root_html)
                zf.writestr(f"Root/DB {db_id}.html", db_html)
                zf.writestr(f"Root/DB {db_id}.csv", db_csv)
                zf.writestr(f"Root/DB/行1 {row_id}.html", row_html)

            users_res = MagicMock(status_code=200, ok=True)
            users_res.json.return_value = {
                "results": [
                    {"id": "u-1", "name": "田中太郎", "person": {"email": "taro1@example.com"}},
                    {"id": "u-2", "name": "田中太郎", "person": {"email": "taro2@example.com"}},
                ],
                "has_more": False,
            }
            db_res = MagicMock(status_code=200, ok=True)
            db_res.json.return_value = {"properties": {"タスク名": {"type": "title"}, "担当者": {"type": "people"}}}

            def fake_get(url, **kwargs):
                if url.endswith("/v1/users"):
                    return users_res
                if "/databases/" in url:
                    return db_res
                return MagicMock(status_code=404, ok=False)

            cp_path = os.path.join(tmpdir, "cp.json")
            buf = io.StringIO()
            with patch("requests.get", side_effect=fake_get), \
                 patch("requests.post", return_value=MagicMock(status_code=404, ok=False)), \
                 patch.dict(os.environ, {"NOTION_TOKEN": "ntn_dummy"}, clear=False), \
                 patch("sys.argv", ["migrate.py", "--zip", zip_path, "--enrich-via-api", "--dry-run", "--checkpoint", cp_path]), \
                 redirect_stdout(buf):
                migrate.main()

            out = buf.getvalue()
            self.assertIn("WARNING", out)
            self.assertIn("同姓同名ユーザーを 1 件検知", out)
            self.assertIn("田中太郎", out)
            self.assertIn("taro1@example.com", out)
            self.assertIn("taro2@example.com", out)
            self.assertIn("本文メンション", out)
            self.assertIn("コメント", out)
            self.assertIn("'担当者' 列", out)


if __name__ == "__main__":
    unittest.main()
