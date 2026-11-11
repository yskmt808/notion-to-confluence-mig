"""
Unit tests for Confluence private mode restriction functionality.
"""

import unittest
from unittest.mock import MagicMock, patch
from src.extractor import PageNode
from src.importer import ConfluenceImporter, ConfluenceClient


class TestPrivateMode(unittest.TestCase):

    def test_client_set_page_private(self):
        """ConfluenceClient.set_page_private が正しく PUT /rest/api/content/{id}/restriction を呼ぶこと"""
        client = ConfluenceClient("https://example.atlassian.net/wiki", "user@example.com", "token123")
        with patch("requests.put") as mock_put:
            mock_put.return_value.ok = True
            mock_put.return_value.status_code = 200
            success = client.set_page_private("12345", account_id="acc-999")
            self.assertTrue(success)
            mock_put.assert_called_once()
            args, kwargs = mock_put.call_args
            self.assertIn("/rest/api/content/12345/restriction", args[0])
            payload = kwargs.get("json", [])
            self.assertEqual(len(payload), 2)
            ops = {p["operation"]: p["restrictions"]["user"][0]["accountId"] for p in payload}
            self.assertEqual(ops.get("read"), "acc-999")
            self.assertEqual(ops.get("update"), "acc-999")

    def test_importer_applies_private_mode_to_root(self):
        """private_mode 有効時、ルートノードにのみ set_page_private が呼ばれること"""
        with patch.object(ConfluenceClient, "get_current_user", return_value={"accountId": "acc-current"}), \
             patch.object(ConfluenceClient, "get_page_by_title", return_value=None), \
             patch.object(ConfluenceClient, "create_page", return_value={"id": "page-root", "_links": {"webui": "/root"}}), \
             patch.object(ConfluenceClient, "update_page", return_value={"id": "page-root", "_links": {"webui": "/root"}}), \
             patch.object(ConfluenceClient, "get_page_by_id", return_value={"version": {"number": 1}}), \
             patch.object(ConfluenceClient, "set_page_private") as mock_set_private, \
             patch.object(ConfluenceClient, "set_page_emoji"):

            importer = ConfluenceImporter(
                url="https://example.atlassian.net",
                username="test",
                token="test",
                space_key="TEST",
                private_mode=True,
            )

            root = PageNode(
                id="root-1",
                title="Root Private Page",
                node_type="page",
                blocks=[],
                children=[
                    PageNode(id="child-1", title="Child Page", node_type="page", blocks=[], children=[]),
                ],
            )

            with patch.object(importer, "_import_children_concurrently"):
                importer.import_tree(root)

            # ルートページに対して set_page_private が呼ばれたことを検証
            mock_set_private.assert_called_once_with("page-root", account_id="acc-current")


if __name__ == "__main__":
    unittest.main()

