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


if __name__ == "__main__":
    unittest.main()
