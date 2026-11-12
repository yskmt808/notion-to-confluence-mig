"""
Unit tests for Sibling Page Concurrency (同階層兄弟ページの並行作成) & Concurrent Pass 2 Updates.
"""

import os
import sys
import unittest
import threading
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor import PageNode
from src.importer import ConfluenceImporter, ConfluenceClient


class TestSiblingConcurrency(unittest.TestCase):
    def test_sibling_pages_concurrent_creation(self):
        """通常ページの兄弟ノードが並行して作成され、親子ID構造が正しく維持されることを検証"""
        # ツリー構造:
        # Root (Level 0)
        #  ├── Block A (Level 1)
        #  │    ├── Page A-1 (Level 2)
        #  │    ├── Page A-2 (Level 2)
        #  │    └── Page A-3 (Level 2)
        #  └── Block B (Level 1)
        #       ├── Page B-1 (Level 2)
        #       └── Page B-2 (Level 2)
        root = PageNode(id="root-1", title="Root Page", node_type="page")
        block_a = PageNode(id="block-a", title="Block A", node_type="page")
        block_b = PageNode(id="block-b", title="Block B", node_type="page")
        for i in range(1, 4):
            block_a.children.append(PageNode(id=f"a-{i}", title=f"Page A-{i}", node_type="page"))
        for i in range(1, 3):
            block_b.children.append(PageNode(id=f"b-{i}", title=f"Page B-{i}", node_type="page"))
        root.children = [block_a, block_b]

        importer = ConfluenceImporter(
            url="https://mock-test.atlassian.net",
            username="user@example.com",
            token="token123",
            space_key="TEST",
            page_workers=4,
        )

        created_pages = {}
        active_threads = set()
        max_concurrent_threads = 0
        lock = threading.Lock()

        def mock_create_page(space_key, title, body, parent_id=None):
            nonlocal max_concurrent_threads
            tid = threading.get_ident()
            with lock:
                active_threads.add(tid)
                max_concurrent_threads = max(max_concurrent_threads, len(active_threads))

            # Simulate short network latency
            time.sleep(0.01)

            with lock:
                active_threads.remove(tid)
                p_id = f"conf_{len(created_pages) + 100}"
                created_pages[title] = {"id": p_id, "parent_id": parent_id}

            return {
                "id": p_id,
                "_links": {"webui": f"/spaces/{space_key}/pages/{p_id}"},
                "version": {"number": 1},
            }

        def mock_update_page(page_id, space_key, title, body, current_version=1, parent_id=None):
            return {
                "id": page_id,
                "_links": {"webui": f"/spaces/{space_key}/pages/{page_id}"},
                "version": {"number": current_version + 1},
            }

        importer.client.get_page_by_title = MagicMock(return_value=None)
        importer.client.get_database_by_title = MagicMock(return_value=None)
        importer.client.create_page = MagicMock(side_effect=mock_create_page)
        importer.client.update_page = MagicMock(side_effect=mock_update_page)
        importer.client.get_page_by_id = MagicMock(side_effect=lambda pid, expand=None: {
            "id": pid,
            "version": {"number": 1},
        })

        # Run migration
        migration_map = importer.import_tree(root, parent_id="space_home", overwrite=False)

        # 1. 全8ノードが正常に移行されていること
        self.assertEqual(len(migration_map), 8)
        self.assertIn("root-1", migration_map)
        self.assertIn("block-a", migration_map)
        self.assertIn("block-b", migration_map)
        for i in range(1, 4):
            self.assertIn(f"a-{i}", migration_map)
        for i in range(1, 3):
            self.assertIn(f"b-{i}", migration_map)

        # 2. 親子関係が正しく維持されていること
        root_conf_id = migration_map["root-1"]["confluence_id"]
        block_a_conf_id = migration_map["block-a"]["confluence_id"]
        block_b_conf_id = migration_map["block-b"]["confluence_id"]

        self.assertEqual(created_pages["Root Page"]["parent_id"], "space_home")
        self.assertEqual(created_pages["Block A"]["parent_id"], root_conf_id)
        self.assertEqual(created_pages["Block B"]["parent_id"], root_conf_id)

        for i in range(1, 4):
            self.assertEqual(created_pages[f"Page A-{i}"]["parent_id"], block_a_conf_id)
        for i in range(1, 3):
            self.assertEqual(created_pages[f"Page B-{i}"]["parent_id"], block_b_conf_id)

        # 3. 複数スレッドが実際に並行して動作したこと（max_concurrent_threads > 1）
        self.assertGreater(max_concurrent_threads, 1)

    def test_sibling_duplicate_titles_avoid_collision(self):
        """同階層で同一タイトルを持つ兄弟ページが並行作成された場合、衝突せずサフィックスが付与されること"""
        root = PageNode(id="root-dup", title="Root Page", node_type="page")
        # 3 child pages with identical title "Duplicate Spec"
        root.children = [
            PageNode(id="d1", title="Duplicate Spec", node_type="page"),
            PageNode(id="d2", title="Duplicate Spec", node_type="page"),
            PageNode(id="d3", title="Duplicate Spec", node_type="page"),
        ]

        importer = ConfluenceImporter(
            url="https://mock-test.atlassian.net",
            username="user@example.com",
            token="token123",
            space_key="TEST",
            page_workers=3,
        )

        created_titles = []
        lock = threading.Lock()

        def mock_create_page(space_key, title, body, parent_id=None):
            with lock:
                created_titles.append(title)
                p_id = f"conf_{len(created_titles)}"
            return {
                "id": p_id,
                "_links": {"webui": f"/spaces/{space_key}/pages/{p_id}"},
                "version": {"number": 1},
            }

        importer.client.get_page_by_title = MagicMock(return_value=None)
        importer.client.get_database_by_title = MagicMock(return_value=None)
        importer.client.create_page = MagicMock(side_effect=mock_create_page)
        importer.client.update_page = MagicMock(side_effect=lambda **kwargs: {
            "id": kwargs["page_id"],
            "_links": {"webui": f"/spaces/TEST/pages/{kwargs['page_id']}"},
            "version": {"number": 2},
        })
        importer.client.get_page_by_id = MagicMock(side_effect=lambda pid, expand=None: {
            "id": pid,
            "version": {"number": 1},
        })

        migration_map = importer.import_tree(root, parent_id="root_parent", overwrite=False)

        self.assertEqual(len(migration_map), 4)
        # Duplicate Spec should be resolved to unique titles: Duplicate Spec, Duplicate Spec (2), Duplicate Spec (3)
        self.assertIn("Duplicate Spec", created_titles)
        self.assertIn("Duplicate Spec (2)", created_titles)
        self.assertIn("Duplicate Spec (3)", created_titles)


if __name__ == "__main__":
    unittest.main()
