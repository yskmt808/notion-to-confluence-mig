"""Notion REST API 抽出器 (src/extractor.py) の単体テスト (TCK_00005)。

_retry_notion_call、extract_tree の再帰・ページネーション・synced_block 循環防止、
_extract_database の通常DB/リンクドDBビューフォールバック、および _parse_row_properties の
全プロパティ型変換を notion_client.Client モックで検証する。
"""

import unittest
from unittest.mock import MagicMock, patch

from src.extractor import NotionExtractor, _retry_notion_call


def _rt(text: str):
    return [{"plain_text": text}]


class TestRetryNotionCall(unittest.TestCase):
    def test_succeeds_after_transient_failures_with_exponential_backoff(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("temporary 503")
            return "ok"

        with patch("src.extractor.time.sleep") as mock_sleep:
            result = _retry_notion_call(flaky, max_retries=4, base_delay=1.0)

        self.assertEqual(result, "ok")
        self.assertEqual(attempts["n"], 3)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [1.0, 2.0])

    def test_linked_database_error_raises_immediately_without_retry(self):
        attempts = {"n": 0}

        def fail_linked():
            attempts["n"] += 1
            raise ValueError("This is a linked database and cannot be queried")

        with patch("src.extractor.time.sleep") as mock_sleep:
            with self.assertRaises(ValueError):
                _retry_notion_call(fail_linked, max_retries=4, base_delay=1.0)

        self.assertEqual(attempts["n"], 1)
        mock_sleep.assert_not_called()

    def test_exhausted_retries_reraises_last_error(self):
        with patch("src.extractor.time.sleep") as mock_sleep:
            with self.assertRaises(ConnectionError):
                _retry_notion_call(
                    lambda: (_ for _ in ()).throw(ConnectionError("down")),
                    max_retries=3,
                    base_delay=0.5,
                )
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [0.5, 1.0])


class TestNotionExtractor(unittest.TestCase):
    def setUp(self):
        self._sleep_patcher = patch("src.extractor.time.sleep")
        self._sleep_patcher.start()
        self.addCleanup(self._sleep_patcher.stop)

    def _make_extractor(self):
        with patch("src.extractor.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            ext = NotionExtractor(token="ntn_test_token")
        return ext, mock_client

    def test_extract_tree_metadata_comments_pagination_and_nested_children(self):
        ext, notion = self._make_extractor()

        root_id = "11111111-1111-1111-1111-111111111111"
        child_id = "22222222-2222-2222-2222-222222222222"
        sub_from_toggle_id = "33333333-3333-3333-3333-333333333333"
        toggle_id = "toggle-1"

        pages_map = {
            root_id: {
                "id": root_id,
                "url": "https://notion.so/root",
                "icon": {"type": "emoji", "emoji": "🚀"},
                "cover": {"type": "external", "external": {"url": "https://example.com/c.jpg"}},
                "created_time": "2026-01-01T00:00:00Z",
                "created_by": {"id": "u1"},
                "last_edited_time": "2026-01-02T00:00:00Z",
                "last_edited_by": {"id": "u2"},
                "properties": {"Title": {"type": "title", "title": _rt("Root "), }},
            },
            child_id: {
                "id": child_id,
                "url": "https://notion.so/child",
                "properties": {"Name": {"type": "title", "title": _rt("Child Page")}},
            },
            sub_from_toggle_id: {
                "id": sub_from_toggle_id,
                "url": "https://notion.so/sub",
                "properties": {},  # Untitled Page フォールバック確認
            },
        }
        pages_map[root_id]["properties"]["Title"]["title"].append({"plain_text": "Page"})
        notion.pages.retrieve.side_effect = lambda page_id: pages_map[page_id]

        # コメント取得: ルートは成功、子ページは例外でもツリー抽出自体は継続する
        def comments_side_effect(block_id):
            if block_id == root_id:
                return {"results": [{"id": "c1", "rich_text": _rt("Top comment")}]}
            raise RuntimeError("comments permission denied")

        notion.comments.list.side_effect = comments_side_effect

        # blocks.children.list: root は 2 ページネーション、toggle 配下に child_page
        def blocks_side_effect(block_id, start_cursor=None, page_size=100):
            if block_id == root_id and start_cursor is None:
                return {
                    "results": [
                        {"id": "p1", "type": "paragraph", "has_children": False},
                        {"id": toggle_id, "type": "toggle", "has_children": True},
                    ],
                    "has_more": True,
                    "next_cursor": "cur-2",
                }
            if block_id == root_id and start_cursor == "cur-2":
                return {
                    "results": [
                        {"id": child_id, "type": "child_page", "child_page": {"title": "Child Page"}},
                    ],
                    "has_more": False,
                }
            if block_id == toggle_id:
                return {
                    "results": [
                        {
                            "id": sub_from_toggle_id,
                            "type": "child_page",
                            "child_page": {"title": "Untitled Page"},
                        }
                    ],
                    "has_more": False,
                }
            return {"results": [], "has_more": False}

        notion.blocks.children.list.side_effect = blocks_side_effect

        tree = ext.extract_tree(root_id)

        self.assertEqual(tree.title, "Root Page")
        self.assertEqual(tree.icon, "🚀")
        self.assertEqual(tree._cover_filename, "cover_11111111.jpg")
        self.assertEqual(len(tree.comments), 1)
        self.assertEqual(tree.created_by, {"id": "u1"})

        # toggle 内からの child_page と 2ページ目の child_page の両方が children に含まれること
        self.assertEqual([c.title for c in tree.children], ["Untitled Page", "Child Page"])
        self.assertEqual([b["type"] for b in tree.blocks], ["paragraph", "toggle", "child_page"])
        self.assertEqual(tree.blocks[1]["children_blocks"][0]["id"], sub_from_toggle_id)

    def test_synced_block_resolution_and_cycle_guard(self):
        ext, notion = self._make_extractor()
        root_id = "root-sync"
        orig_id = "orig-sync"

        notion.pages.retrieve.return_value = {
            "id": root_id,
            "properties": {"title": {"type": "title", "title": _rt("Sync Root")}},
        }
        notion.comments.list.return_value = {"results": []}

        def blocks_side_effect(block_id, start_cursor=None, page_size=100):
            if block_id == root_id:
                return {
                    "results": [
                        {
                            "id": "ref-sync",
                            "type": "synced_block",
                            "synced_block": {"synced_from": {"block_id": orig_id}},
                        }
                    ],
                    "has_more": False,
                }
            if block_id == orig_id:
                # 自己参照 synced_block を返し、無限再帰しないことを検証
                return {
                    "results": [
                        {"id": "p-in-sync", "type": "paragraph", "has_children": False},
                        {
                            "id": orig_id,
                            "type": "synced_block",
                            "synced_block": {"synced_from": None},
                        },
                    ],
                    "has_more": False,
                }
            return {"results": [], "has_more": False}

        notion.blocks.children.list.side_effect = blocks_side_effect

        tree = ext.extract_tree(root_id)
        sync_block = tree.blocks[0]
        self.assertEqual(len(sync_block["children_blocks"]), 2)
        self.assertEqual(sync_block["children_blocks"][0]["id"], "p-in-sync")
        # 2回目の orig_id は循環検知により空リスト
        self.assertEqual(sync_block["children_blocks"][1]["children_blocks"], [])

    def test_extract_database_with_pagination_and_row_blocks(self):
        ext, notion = self._make_extractor()
        root_id = "page-with-db"
        db_id = "db-1"

        notion.pages.retrieve.return_value = {
            "id": root_id,
            "properties": {"title": {"type": "title", "title": _rt("Host Page")}},
        }
        notion.comments.list.return_value = {"results": []}

        def blocks_side_effect(block_id, start_cursor=None, page_size=100):
            if block_id == root_id:
                return {
                    "results": [{"id": db_id, "type": "child_database"}],
                    "has_more": False,
                }
            if block_id == "row-1":
                return {
                    "results": [{"id": "rb-1", "type": "paragraph", "has_children": False}],
                    "has_more": False,
                }
            return {"results": [], "has_more": False}

        notion.blocks.children.list.side_effect = blocks_side_effect

        notion.databases.retrieve.return_value = {
            "id": db_id,
            "url": "https://notion.so/db1",
            "title": _rt("Project Tracker"),
            "icon": {"type": "emoji", "emoji": "📊"},
            "properties": {"Status": {"type": "status"}, "Owner": {"type": "people"}},
            "created_time": "2026-01-01T00:00:00Z",
        }

        # databases.query の 2ページページネーション
        def request_side_effect(path, method, body):
            self.assertEqual(path, f"databases/{db_id}/query")
            self.assertEqual(method, "POST")
            if body.get("start_cursor") is None:
                return {
                    "results": [
                        {
                            "id": "row-1",
                            "url": "https://notion.so/row1",
                            "icon": {"type": "emoji", "emoji": "✅"},
                            "properties": {
                                "Name": {"type": "title", "title": _rt("Task A")},
                                "Done": {"type": "checkbox", "checkbox": True},
                            },
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "r-cur-2",
                }
            return {
                "results": [
                    {
                        "id": "row-2",
                        "url": "https://notion.so/row2",
                        "properties": {
                            "Name": {"type": "title", "title": _rt("Task B")},
                            "Done": {"type": "checkbox", "checkbox": False},
                        },
                    }
                ],
                "has_more": False,
            }

        notion.request.side_effect = request_side_effect

        tree = ext.extract_tree(root_id)
        self.assertEqual(len(tree.children), 1)
        db_node = tree.children[0]
        self.assertEqual(db_node.node_type, "database")
        self.assertEqual(db_node.title, "Project Tracker")
        self.assertEqual(db_node.icon, "📊")
        self.assertIn("schema", db_node.properties)
        self.assertEqual(tree.blocks[0]["database_title"], "Project Tracker")

        self.assertEqual([r.title for r in db_node.children], ["Task A", "Task B"])
        self.assertEqual(db_node.children[0].node_type, "database_row")
        self.assertEqual(db_node.children[0].properties["Done"], {"type": "checkbox", "value": True})
        self.assertEqual(db_node.children[0].blocks[0]["id"], "rb-1")

    def test_linked_database_falls_back_to_views_api(self):
        ext, notion = self._make_extractor()
        root_id = "page-linked"
        db_id = "linked-db-1"

        notion.pages.retrieve.return_value = {
            "id": root_id,
            "properties": {"title": {"type": "title", "title": _rt("Linked Host")}},
        }
        notion.comments.list.return_value = {"results": []}
        notion.blocks.children.list.return_value = {
            "results": [{"id": db_id, "type": "child_database"}],
            "has_more": False,
        }
        notion.databases.retrieve.side_effect = RuntimeError("Target is a linked database")

        meta_resp = MagicMock(ok=True)
        meta_resp.json.return_value = {"data_sources": [{"id": "ds-1", "name": "Master Tasks"}]}
        views_resp = MagicMock(ok=True)
        views_resp.json.return_value = {"results": [{"id": "v-1"}]}
        view_detail_resp = MagicMock(ok=True)
        view_detail_resp.json.return_value = {
            "name": "Kanban",
            "type": "board",
            "filter": {"property": "Status"},
            "configuration": {"group_by": {"property_name": "Status"}},
        }

        with patch("src.extractor.requests.get", side_effect=[meta_resp, views_resp, view_detail_resp]) as mock_get:
            tree = ext.extract_tree(root_id)

        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(len(tree.children), 0)  # リンクドDBはブロックとしてのみ保持
        self.assertEqual(len(tree.blocks), 1)
        blk = tree.blocks[0]
        self.assertTrue(blk["is_linked_database"])
        self.assertEqual(blk["data_sources"][0]["name"], "Master Tasks")
        self.assertEqual(blk["views"][0]["name"], "Kanban")
        self.assertEqual(blk["views"][0]["type"], "board")

    def test_get_titles_fallbacks(self):
        ext, _ = self._make_extractor()
        self.assertEqual(ext._get_page_title({}), "Untitled Page")
        self.assertEqual(ext._get_page_title({"properties": {"Name": {"type": "title", "title": []}}}), "Untitled Page")
        self.assertEqual(ext._get_db_title({}), "Untitled Database")
        self.assertEqual(ext._get_db_title({"title": _rt("DB1")}), "DB1")

    def test_parse_row_properties_covers_all_supported_types(self):
        ext, _ = self._make_extractor()
        raw_props = {
            "Title": {"type": "title", "title": [{"plain_text": "Hello "}, {"plain_text": "World"}]},
            "Summary": {"type": "rich_text", "rich_text": _rt("Details")},
            "Category": {"type": "select", "select": {"name": "Backend", "color": "blue"}},
            "EmptySelect": {"type": "select", "select": None},
            "Status": {"type": "status", "status": {"name": "In Progress", "color": "yellow"}},
            "Tags": {
                "type": "multi_select",
                "multi_select": [{"name": "P1", "color": "red"}, {"name": "API"}],
            },
            "Due": {"type": "date", "date": {"start": "2026-11-13", "end": "2026-11-15", "time_zone": "Asia/Tokyo"}},
            "EmptyDate": {"type": "date", "date": None},
            "Approved": {"type": "checkbox", "checkbox": True},
            "Link": {"type": "url", "url": "https://example.com"},
            "Points": {"type": "number", "number": 42},
            "Email": {"type": "email", "email": "dev@example.com"},
            "Phone": {"type": "phone_number", "phone_number": "03-1234-5678"},
            "Owners": {
                "type": "people",
                "people": [{"id": "u-1", "name": "Alice"}, {"id": "u-2"}],
            },
            "Epic": {"type": "relation", "relation": [{"id": "rel-1"}, {"id": "rel-2"}]},
            "RollupArray": {
                "type": "rollup",
                "rollup": {
                    "type": "array",
                    "array": [
                        {"type": "select", "select": {"name": "High", "color": "red"}},
                        {"type": "title", "title": _rt("Sub")},
                        {"type": "number", "number": 7},
                        {"type": "date", "date": {"start": "2026-11-01"}},
                        {"type": "checkbox", "checkbox": True},
                    ],
                },
            },
            "RollupNum": {"type": "rollup", "rollup": {"type": "number", "number": 99}},
            "RollupDate": {"type": "rollup", "rollup": {"type": "date", "date": {"start": "2026-12-01"}}},
            "RollupOther": {"type": "rollup", "rollup": {"type": "incomplete", "incomplete": "N/A"}},
            "FormulaStr": {"type": "formula", "formula": {"type": "string", "string": "computed"}},
            "FormulaNum": {"type": "formula", "formula": {"type": "number", "number": 3.5}},
            "Created": {"type": "created_time", "created_time": "2026-01-01T00:00:00Z"},
            "EmptyUnknown": {},
        }

        parsed = ext._parse_row_properties(raw_props)

        self.assertEqual(parsed["Title"], {"type": "title", "value": "Hello World"})
        self.assertEqual(parsed["Summary"], {"type": "rich_text", "value": "Details"})
        self.assertEqual(parsed["Category"], {"type": "select", "value": "Backend", "color": "blue"})
        self.assertEqual(parsed["EmptySelect"], {"type": "select", "value": None, "color": "default"})
        self.assertEqual(parsed["Status"], {"type": "status", "value": "In Progress", "color": "yellow"})
        self.assertEqual(
            parsed["Tags"],
            {"type": "multi_select", "items": [{"value": "P1", "color": "red"}, {"value": "API", "color": "default"}]},
        )
        self.assertEqual(
            parsed["Due"],
            {"type": "date", "start": "2026-11-13", "end": "2026-11-15", "time_zone": "Asia/Tokyo"},
        )
        self.assertEqual(parsed["EmptyDate"], {"type": "date", "start": None, "end": None})
        self.assertEqual(parsed["Approved"], {"type": "checkbox", "value": True})
        self.assertEqual(parsed["Link"], {"type": "url", "value": "https://example.com"})
        self.assertEqual(parsed["Points"], {"type": "number", "value": 42})
        self.assertEqual(parsed["Email"], {"type": "email", "value": "dev@example.com"})
        self.assertEqual(parsed["Phone"], {"type": "phone_number", "value": "03-1234-5678"})
        self.assertEqual(
            parsed["Owners"],
            {"type": "people", "users": [{"id": "u-1", "name": "Alice"}, {"id": "u-2", "name": "Unknown User"}]},
        )
        self.assertEqual(parsed["Epic"], {"type": "relation", "relations": [{"id": "rel-1"}, {"id": "rel-2"}]})
        self.assertEqual(parsed["RollupArray"]["rollup_type"], "array")
        self.assertEqual(
            parsed["RollupArray"]["items"],
            [
                {"type": "select", "value": "High", "color": "red"},
                {"type": "title", "value": "Sub"},
                {"type": "number", "value": 7},
                {"type": "date", "start": "2026-11-01"},
                {"type": "checkbox", "value": "True"},
            ],
        )
        self.assertEqual(parsed["RollupNum"], {"type": "rollup", "rollup_type": "number", "value": 99})
        self.assertEqual(parsed["RollupDate"], {"type": "rollup", "rollup_type": "date", "start": "2026-12-01"})
        self.assertEqual(parsed["RollupOther"], {"type": "rollup", "rollup_type": "incomplete", "value": "N/A"})
        self.assertEqual(parsed["FormulaStr"], {"type": "formula", "value": "computed"})
        self.assertEqual(parsed["FormulaNum"], {"type": "formula", "value": 3.5})
        self.assertEqual(parsed["Created"], {"type": "created_time", "value": "2026-01-01T00:00:00Z"})
        self.assertEqual(parsed["EmptyUnknown"], {"type": "unknown", "value": ""})


if __name__ == "__main__":
    unittest.main()
