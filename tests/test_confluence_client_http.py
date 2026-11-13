"""ConfluenceClient の HTTP 層（requests モック）単体テスト (TCK_00003)。

上位ロジックのテストは `importer.client` を MagicMock で丸ごと差し替えているため、
実際のリクエスト URL・ペイロード・ページネーション・リトライ・エラー処理が未検証だった。
ここでは `src.importer.requests` をモックし、クライアント自身の挙動を検証する。
"""

import unittest
from unittest.mock import MagicMock, patch

from src.importer import ConfluenceClient

BASE = "https://example.atlassian.net/wiki"


def R(status=200, data=None, text=""):
    """requests.Response 互換のモック。"""
    r = MagicMock()
    r.status_code = status
    r.ok = status < 400
    r.text = text
    r.json.return_value = data if data is not None else {}
    return r


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.client = ConfluenceClient("https://example.atlassian.net", "user@example.com", "tok")
        p = patch("src.importer.requests")
        self.req = p.start()
        self.addCleanup(p.stop)
        s = patch("src.importer.time.sleep")
        self.sleep = s.start()
        self.addCleanup(s.stop)


class TestBaseUrlNormalization(unittest.TestCase):
    def test_wiki_suffix_and_whitespace(self):
        for raw in (
            "https://example.atlassian.net",
            "https://example.atlassian.net/",
            "  https://example.atlassian.net/wiki  ",
            "https://example.atlassian.net/wiki/",
        ):
            with self.subTest(raw=raw):
                c = ConfluenceClient(raw, "u", "t")
                self.assertEqual(c.base_url, BASE)
                self.assertEqual(c.auth, ("u", "t"))


class TestPageLookup(ClientTestCase):
    def test_get_page_by_title_returns_first_result_and_sends_params(self):
        self.req.get.return_value = R(200, {"results": [{"id": "1"}, {"id": "2"}]})
        self.assertEqual(self.client.get_page_by_title("SP", "Title"), {"id": "1"})
        args, kwargs = self.req.get.call_args
        self.assertEqual(args[0], f"{BASE}/rest/api/content")
        self.assertEqual(kwargs["params"], {"spaceKey": "SP", "title": "Title", "expand": "version,ancestors"})
        self.assertEqual(kwargs["auth"], ("user@example.com", "tok"))

    def test_get_page_by_title_none_on_empty_or_error(self):
        self.req.get.return_value = R(200, {"results": []})
        self.assertIsNone(self.client.get_page_by_title("SP", "T"))
        self.req.get.return_value = R(500)
        self.assertIsNone(self.client.get_page_by_title("SP", "T"))

    def test_get_page_by_title_truncates_long_title(self):
        self.req.get.return_value = R(200, {"results": []})
        self.client.get_page_by_title("SP", "A" * 300)
        sent = self.req.get.call_args.kwargs["params"]["title"]
        self.assertEqual(len(sent), 255)
        self.assertTrue(sent.endswith("..."))

    def test_get_page_by_id(self):
        self.req.get.return_value = R(200, {"id": "9"})
        self.assertEqual(self.client.get_page_by_id("9"), {"id": "9"})
        args, kwargs = self.req.get.call_args
        self.assertEqual(args[0], f"{BASE}/rest/api/content/9")
        self.assertEqual(kwargs["params"], {"expand": "version,ancestors"})
        self.req.get.return_value = R(404)
        self.assertIsNone(self.client.get_page_by_id("9"))

    def test_get_space_id(self):
        self.req.get.return_value = R(200, {"id": 12345})
        self.assertEqual(self.client.get_space_id("SP"), "12345")
        self.assertEqual(self.req.get.call_args.args[0], f"{BASE}/rest/api/space/SP")
        self.req.get.return_value = R(200, {})
        self.assertIsNone(self.client.get_space_id("SP"))
        self.req.get.return_value = R(404)
        self.assertIsNone(self.client.get_space_id("SP"))

    def test_get_page_attachments(self):
        self.req.get.return_value = R(200, {"results": [{"title": "a.png"}]})
        self.assertEqual(self.client.get_page_attachments("5"), [{"title": "a.png"}])
        self.assertEqual(self.req.get.call_args.args[0], f"{BASE}/rest/api/content/5/child/attachment")
        self.req.get.return_value = R(403)
        self.assertEqual(self.client.get_page_attachments("5"), [])
        self.req.get.side_effect = RuntimeError("boom")
        self.assertEqual(self.client.get_page_attachments("5"), [])

    def test_get_database_by_id_v2_then_v1_fallback(self):
        self.req.get.return_value = R(200, {"id": "d1", "v": 2})
        self.assertEqual(self.client.get_database_by_id("d1"), {"id": "d1", "v": 2})
        self.assertEqual(self.req.get.call_count, 1)
        self.assertEqual(self.req.get.call_args.args[0], f"{BASE}/api/v2/databases/d1")

        self.req.get.reset_mock()
        self.req.get.side_effect = [R(404), R(200, {"id": "d1", "v": 1})]
        self.assertEqual(self.client.get_database_by_id("d1"), {"id": "d1", "v": 1})
        self.assertEqual(self.req.get.call_args.args[0], f"{BASE}/rest/api/content/d1")

        self.req.get.side_effect = [R(404), R(404)]
        self.assertIsNone(self.client.get_database_by_id("d1"))
        self.req.get.side_effect = RuntimeError("network")
        self.assertIsNone(self.client.get_database_by_id("d1"))

    def test_get_database_by_title_escapes_cql_and_matches_exactly(self):
        self.req.get.return_value = R(200, {"results": [{"id": "x", "title": 'A"B\\C'}]})
        found = self.client.get_database_by_title("TEST", 'A"B\\C')
        self.assertEqual(found, {"id": "x", "title": 'A"B\\C'})
        cql = self.req.get.call_args.kwargs["params"]["cql"]
        self.assertEqual(cql, 'type = database AND space = "TEST" AND title ~ "A\\"B\\\\C"')

    def test_get_database_by_title_matches_truncated_title_only(self):
        long_title = "D" * 300
        truncated = long_title[:252] + "..."
        self.req.get.return_value = R(200, {"results": [{"id": "t", "title": truncated}]})
        self.assertEqual(self.client.get_database_by_title("SP", long_title), {"id": "t", "title": truncated})
        self.req.get.return_value = R(200, {"results": [{"id": "z", "title": "other"}]})
        self.assertIsNone(self.client.get_database_by_title("SP", long_title))


class TestPageMutations(ClientTestCase):
    def test_move_page_position(self):
        self.req.put.return_value = R(200)
        self.assertTrue(self.client.move_page_position("10", "after", "20"))
        self.assertEqual(self.req.put.call_args.args[0], f"{BASE}/rest/api/content/10/move/after/20")
        self.req.put.return_value = R(400)
        self.assertFalse(self.client.move_page_position("10", "after", "20"))
        self.req.put.side_effect = RuntimeError("x")
        self.assertFalse(self.client.move_page_position("10", "after", "20"))

    def test_create_comment(self):
        self.req.post.return_value = R(200, {"id": "c1"})
        self.assertEqual(self.client.create_comment("7", "<p>hi</p>"), {"id": "c1"})
        payload = self.req.post.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "comment")
        self.assertEqual(payload["container"], {"id": "7", "type": "page"})
        self.assertEqual(payload["body"]["storage"], {"value": "<p>hi</p>", "representation": "storage"})
        self.req.post.return_value = R(400, text="bad")
        self.assertEqual(self.client.create_comment("7", "<p>hi</p>"), {})

    def test_delete_page_and_database_status_codes(self):
        for status, expected in ((200, True), (204, True), (404, False), (500, False)):
            with self.subTest(status=status):
                self.req.delete.return_value = R(status)
                self.assertEqual(self.client.delete_page("1"), expected)
                self.assertEqual(self.client.delete_database("2"), expected)
        self.assertEqual(self.req.delete.call_args_list[-2].args[0], f"{BASE}/rest/api/content/1")
        self.assertEqual(self.req.delete.call_args_list[-1].args[0], f"{BASE}/api/v2/databases/2")

    def test_add_labels_sanitizes_names(self):
        self.req.post.return_value = R(200)
        self.client.add_labels("5", ["Hello World!", "Q&A", "v1.2", "tag_1-x"])
        self.assertEqual(self.req.post.call_args.args[0], f"{BASE}/rest/api/content/5/label")
        names = [p["name"] for p in self.req.post.call_args.kwargs["json"]]
        self.assertEqual(names, ["hello-world", "qa", "v12", "tag_1-x"])
        self.assertTrue(all(p["prefix"] == "global" for p in self.req.post.call_args.kwargs["json"]))

    def test_add_labels_noop_and_never_raises(self):
        self.client.add_labels("5", [])
        self.req.post.assert_not_called()
        self.req.post.return_value = R(500)
        self.client.add_labels("5", ["a"])
        self.req.post.side_effect = RuntimeError("net")
        self.client.add_labels("5", ["a"])


class TestUploadAttachment(ClientTestCase):
    URL = f"{BASE}/rest/api/content/12/child/attachment"

    def test_new_attachment_posts_with_headers_and_file_tuple(self):
        self.req.get.return_value = R(200, {"results": []})
        self.req.post.return_value = R(200, {"results": [{"id": "att1"}]})
        out = self.client.upload_attachment("12", "photo.jpg", b"BYTES", "image/jpeg")
        self.assertEqual(out, {"results": [{"id": "att1"}]})
        self.assertEqual(self.req.get.call_args.kwargs["params"], {"filename": "photo.jpg"})
        args, kwargs = self.req.post.call_args
        self.assertEqual(args[0], self.URL)
        self.assertEqual(kwargs["headers"], {"X-Atlassian-Token": "nocheck"})
        self.assertEqual(kwargs["files"], {"file": ("photo.jpg", b"BYTES", "image/jpeg")})

    def test_mime_type_is_detected_when_missing_or_octet_stream(self):
        self.req.get.return_value = R(200, {"results": []})
        self.req.post.return_value = R(200, {})
        for given in (None, "application/octet-stream"):
            with self.subTest(given=given):
                self.client.upload_attachment("12", "doc.pdf", b"x", given)
                self.assertEqual(self.req.post.call_args.kwargs["files"]["file"][2], "application/pdf")

    def test_existing_attachment_is_updated_via_data_endpoint(self):
        self.req.get.return_value = R(200, {"results": [{"id": "att9"}]})
        self.req.post.return_value = R(200, {"id": "att9", "updated": True})
        out = self.client.upload_attachment("12", "a.png", b"x", "image/png")
        self.assertEqual(out, {"id": "att9", "updated": True})
        self.assertEqual(self.req.post.call_count, 1)
        self.assertEqual(self.req.post.call_args.args[0], f"{self.URL}/att9/data")

    def test_failed_update_falls_back_to_create(self):
        self.req.get.return_value = R(200, {"results": [{"id": "att9"}]})
        self.req.post.side_effect = [R(500), R(200, {"created": True})]
        out = self.client.upload_attachment("12", "a.png", b"x", "image/png")
        self.assertEqual(out, {"created": True})
        self.assertEqual(self.req.post.call_args_list[0].args[0], f"{self.URL}/att9/data")
        self.assertEqual(self.req.post.call_args_list[1].args[0], self.URL)

    def test_lookup_exception_does_not_block_create(self):
        self.req.get.side_effect = RuntimeError("lookup failed")
        self.req.post.return_value = R(201, {"created": True})
        self.assertEqual(self.client.upload_attachment("12", "a.png", b"x", "image/png"), {"created": True})

    def test_create_failure_returns_none(self):
        self.req.get.return_value = R(200, {"results": []})
        self.req.post.return_value = R(413, text="too large")
        self.assertIsNone(self.client.upload_attachment("12", "a.png", b"x", "image/png"))


class TestSetPageEmoji(ClientTestCase):
    def _props(self):
        return ["emoji-title-published", "emoji-title-draft"]

    def test_noop_for_empty_or_non_emoji(self):
        for value in ("", "abc", "not-an-emoji"):
            with self.subTest(value=value):
                self.client.set_page_emoji("5", value)
        self.req.get.assert_not_called()
        self.req.put.assert_not_called()
        self.req.post.assert_not_called()

    def test_existing_property_is_updated_with_incremented_version(self):
        self.req.get.return_value = R(200, {"version": {"number": 4}})
        self.req.put.return_value = R(200)
        self.client.set_page_emoji("5", "🚀")
        self.assertEqual(self.req.put.call_count, 2)
        for call, key in zip(self.req.put.call_args_list, self._props()):
            self.assertEqual(call.args[0], f"{BASE}/rest/api/content/5/property/{key}")
            self.assertEqual(call.kwargs["json"], {"key": key, "value": "1f680", "version": {"number": 5}})
        self.req.post.assert_not_called()

    def test_missing_property_is_created(self):
        self.req.get.return_value = R(404)
        self.req.post.return_value = R(200)
        self.client.set_page_emoji("5", "🚀")
        self.assertEqual(self.req.post.call_count, 2)
        for call, key in zip(self.req.post.call_args_list, self._props()):
            self.assertEqual(call.args[0], f"{BASE}/rest/api/content/5/property")
            self.assertEqual(call.kwargs["json"], {"key": key, "value": "1f680"})
        self.req.put.assert_not_called()

    def test_codepoint_encoding_drops_variation_selector_and_keeps_zwj(self):
        self.req.get.return_value = R(404)
        self.req.post.return_value = R(200)
        self.client.set_page_emoji("5", "❤️")
        self.assertEqual(self.req.post.call_args.kwargs["json"]["value"], "2764")
        self.client.set_page_emoji("5", "👨\u200d💻")
        self.assertEqual(self.req.post.call_args.kwargs["json"]["value"], "1f468-200d-1f4bb")

    def test_rate_limit_is_retried(self):
        self.req.get.return_value = R(200, {"version": {"number": 1}})
        self.req.put.side_effect = [R(429), R(200), R(200)]
        self.client.set_page_emoji("5", "🚀")
        self.assertEqual(self.req.put.call_count, 3)
        self.assertTrue(self.sleep.called)

    def test_other_http_errors_are_not_retried(self):
        self.req.get.return_value = R(200, {"version": {"number": 1}})
        self.req.put.return_value = R(500)
        self.client.set_page_emoji("5", "🚀")
        self.assertEqual(self.req.put.call_count, 2)  # プロパティごとに 1 回のみ


class TestDatabaseCreateAndFieldOptions(ClientTestCase):
    def test_create_database_payload(self):
        self.req.post.return_value = R(200, {"id": "db1"})
        out = self.client.create_database("sp-1", "My DB", "parent-9", "csv-body")
        self.assertEqual(out, {"id": "db1"})
        args, kwargs = self.req.post.call_args
        self.assertEqual(args[0], f"{BASE}/api/v2/databases")
        self.assertEqual(
            kwargs["json"],
            {"spaceId": "sp-1", "title": "My DB", "body": {"representation": "csv", "value": "csv-body"}, "parentId": "parent-9"},
        )

    def test_create_database_without_parent_and_with_long_title(self):
        self.req.post.return_value = R(201, {"id": "db1"})
        self.client.create_database("sp-1", "T" * 300, None, "csv")
        payload = self.req.post.call_args.kwargs["json"]
        self.assertNotIn("parentId", payload)
        self.assertEqual(len(payload["title"]), 255)
        self.assertTrue(payload["title"].endswith("..."))

    def test_create_database_raises_with_status_on_failure(self):
        self.req.post.return_value = R(400, text="invalid csv")
        with self.assertRaises(RuntimeError) as ctx:
            self.client.create_database("sp-1", "My DB", None, "csv")
        self.assertIn("400", str(ctx.exception))
        self.assertIn("invalid csv", str(ctx.exception))

    def test_update_field_options_noop_when_nothing_to_set(self):
        self.assertTrue(self.client.update_database_field_options("d1", [], []))
        self.req.get.assert_not_called()
        self.req.put.assert_not_called()

    def test_update_field_options_builds_csv_and_bumps_version(self):
        self.req.get.return_value = R(200, {"version": {"number": 7}})
        self.req.put.return_value = R(200)
        self.assertTrue(self.client.update_database_field_options("d1", ["状態"], ["タグ"]))
        args, kwargs = self.req.put.call_args
        self.assertEqual(args[0], f"{BASE}/api/v2/databases/d1")
        payload = kwargs["json"]
        self.assertEqual(payload["id"], "d1")
        self.assertEqual(payload["version"], {"number": 8})
        csv_body = payload["body"]["value"]
        self.assertEqual(payload["body"]["representation"], "csv")
        self.assertIn("edit_field,状態,allow_multiple_values,false", csv_body)
        self.assertIn("edit_field,タグ,allow_multiple_values,true", csv_body)
        self.assertIn("operation_type,entry_index,状態,タグ", csv_body)

    def test_update_field_options_failure_paths(self):
        self.req.get.return_value = R(404)
        self.assertFalse(self.client.update_database_field_options("d1", ["a"], []))
        self.req.put.assert_not_called()
        self.req.get.return_value = R(200, {"version": {"number": 1}})
        self.req.put.return_value = R(500, text="err")
        self.assertFalse(self.client.update_database_field_options("d1", ["a"], []))


class TestDatabaseEntries(ClientTestCase):
    def test_single_page_returns_when_expected_count_reached(self):
        self.req.get.return_value = R(200, {"results": [{"id": "e1"}, {"id": "e2"}]})
        out = self.client.get_database_entries("d1", expected_count=2, timeout_sec=2)
        self.assertEqual([e["id"] for e in out], ["e1", "e2"])
        args, kwargs = self.req.get.call_args
        self.assertEqual(args[0], f"{BASE}/api/v2/databases/d1/entries")
        self.assertEqual(kwargs["params"], {"limit": 100})

    def test_pagination_follows_relative_and_absolute_next_links(self):
        self.req.get.side_effect = [
            R(200, {"results": [{"id": "e1"}], "_links": {"next": "/api/v2/databases/d1/entries?cursor=abc"}}),
            R(200, {"results": [{"id": "e2"}], "_links": {"next": "https://other.example/api/next?c=2"}}),
            R(200, {"results": [{"id": "e3"}]}),
        ]
        out = self.client.get_database_entries("d1", expected_count=3, timeout_sec=2)
        self.assertEqual([e["id"] for e in out], ["e1", "e2", "e3"])
        urls = [c.args[0] for c in self.req.get.call_args_list]
        self.assertEqual(
            urls,
            [
                f"{BASE}/api/v2/databases/d1/entries",
                f"{BASE}/api/v2/databases/d1/entries?cursor=abc",
                "https://other.example/api/next?c=2",
            ],
        )
        # 2 ページ目以降は params を付けない（next リンクに含まれるため）
        self.assertNotIn("params", self.req.get.call_args_list[1].kwargs)

    def test_polls_until_index_is_ready(self):
        self.req.get.side_effect = [R(200, {"results": []}), R(200, {"results": [{"id": "e1"}]})]
        out = self.client.get_database_entries("d1", expected_count=1, timeout_sec=2)
        self.assertEqual([e["id"] for e in out], ["e1"])
        self.assertEqual(self.req.get.call_count, 2)
        self.sleep.assert_called_with(2)

    def test_zero_timeout_returns_empty_without_requests(self):
        self.assertEqual(self.client.get_database_entries("d1", timeout_sec=0), [])
        self.req.get.assert_not_called()

    def test_http_error_stops_pagination_and_swallows_exceptions(self):
        self.req.get.return_value = R(500)
        self.assertEqual(self.client.get_database_entries("d1", timeout_sec=0), [])
        self.req.get.side_effect = RuntimeError("net")
        self.assertEqual(self.client.get_database_entries("d1", timeout_sec=0), [])


class TestDatabaseViews(ClientTestCase):
    VIEWS = f"{BASE}/api/v2/databases/d1/views"

    def test_get_database_views_success(self):
        self.req.get.return_value = R(200, {"results": [{"id": "v1", "name": "All"}]})
        self.assertEqual(self.client.get_database_views("d1"), [{"id": "v1", "name": "All"}])
        self.assertEqual(self.req.get.call_args.args[0], self.VIEWS)
        self.assertEqual(self.req.get.call_count, 1)

    def test_empty_results_are_retried_then_returned(self):
        self.req.get.return_value = R(200, {"results": []})
        self.assertEqual(self.client.get_database_views("d1"), [])
        self.assertEqual(self.req.get.call_count, 3)

    def test_404_is_retried_with_delay_until_available(self):
        self.req.get.side_effect = [R(404), R(404), R(200, {"results": [{"id": "v1"}]})]
        self.assertEqual(self.client.get_database_views("d1", delay=0.5), [{"id": "v1"}])
        self.assertEqual(self.sleep.call_count, 2)
        self.sleep.assert_called_with(0.5)

    def test_exceptions_exhaust_retries_and_return_empty(self):
        self.req.get.side_effect = RuntimeError("net")
        self.assertEqual(self.client.get_database_views("d1"), [])
        self.assertEqual(self.req.get.call_count, 3)

    def test_create_view_returns_existing_without_calling_api(self):
        existing = {"id": "v9", "name": "Board"}
        with patch.object(self.client, "get_database_views", return_value=[existing]):
            self.assertEqual(self.client.create_database_view("d1", "Board", "board"), existing)
        self.req.get.assert_not_called()
        self.req.put.assert_not_called()

    def test_create_view_builds_metadata_with_filters_and_sorts(self):
        self.req.get.return_value = R(200, {"version": {"number": 3}})
        self.req.put.return_value = R(200)
        new_view = {"id": "v2", "name": "進行中"}
        with patch.object(self.client, "get_database_views", side_effect=[[], [new_view]]):
            out = self.client.create_database_view(
                "d1",
                "進行中",
                layout="board",
                filters=[{"field": "状態", "operator": "!=", "value": "完了"}],
                sorts=[{"field": "期日", "direction": "desc"}],
            )
        self.assertEqual(out, new_view)
        args, kwargs = self.req.put.call_args
        self.assertEqual(args[0], f"{BASE}/api/v2/databases/d1")
        self.assertEqual(kwargs["json"]["version"], {"number": 4})
        csv_body = kwargs["json"]["body"]["value"]
        self.assertIn("add_view,進行中,board", csv_body)
        self.assertIn("add_filter,進行中,状態,\"['状態' != '完了']\"", csv_body)
        self.assertIn("add_sort,進行中,期日,\"['期日' desc]\"", csv_body)
        self.assertTrue(csv_body.startswith("<metadata>"))
        self.assertIn("<data>", csv_body)

    def test_create_view_failure_paths(self):
        with patch.object(self.client, "get_database_views", return_value=[]):
            self.req.get.return_value = R(404)
            self.assertIsNone(self.client.create_database_view("d1", "V"))
            self.req.put.assert_not_called()

            self.req.get.return_value = R(200, {"version": {"number": 1}})
            self.req.put.return_value = R(400, text="bad")
            self.assertIsNone(self.client.create_database_view("d1", "V"))

        # PUT 成功でも作成後の一覧に見つからなければ None
        self.req.put.return_value = R(200)
        with patch.object(self.client, "get_database_views", side_effect=[[], []]):
            self.assertIsNone(self.client.create_database_view("d1", "V"))

    def test_setup_grouped_views_reuses_existing_without_put(self):
        existing = [{"id": "v1", "name": "状態: A"}, {"id": "v2", "name": "状態: B"}]
        with patch.object(self.client, "get_database_views", return_value=existing):
            out = self.client.setup_grouped_database_views("d1", "状態", ["A", "B"])
        self.assertEqual(out, [{"label": "A", "saved_view_id": "v1"}, {"label": "B", "saved_view_id": "v2"}])
        self.req.get.assert_not_called()
        self.req.put.assert_not_called()

    def test_setup_grouped_views_creates_only_missing_ones(self):
        self.req.get.return_value = R(200, {"version": {"number": 2}})
        self.req.put.return_value = R(200)
        before = [{"id": "v1", "name": "状態: A"}]
        after = before + [{"id": "v2", "name": "状態: B"}, {"id": "v3", "name": "状態: C"}]
        with patch.object(self.client, "get_database_views", side_effect=[before, after]):
            out = self.client.setup_grouped_database_views("d1", "状態", ["A", "B", "C"])
        self.assertEqual(
            out,
            [
                {"label": "A", "saved_view_id": "v1"},
                {"label": "B", "saved_view_id": "v2"},
                {"label": "C", "saved_view_id": "v3"},
            ],
        )
        kwargs = self.req.put.call_args.kwargs
        self.assertEqual(kwargs["json"]["version"], {"number": 3})
        csv_body = kwargs["json"]["body"]["value"]
        self.assertNotIn("状態: A", csv_body)
        for val in ("B", "C"):
            self.assertIn(f"add_view,状態: {val},table", csv_body)
            self.assertIn(f"add_filter,状態: {val},状態,\"['状態' = '{val}']\"", csv_body)

    def test_setup_grouped_views_degrades_to_existing_on_api_failure(self):
        existing = [{"id": "v1", "name": "状態: A"}]
        with patch.object(self.client, "get_database_views", return_value=existing):
            self.req.get.return_value = R(404)
            out = self.client.setup_grouped_database_views("d1", "状態", ["A", "B"])
            self.assertEqual(out, [{"label": "A", "saved_view_id": "v1"}])

            self.req.get.return_value = R(200, {"version": {"number": 1}})
            self.req.put.return_value = R(500)
            out = self.client.setup_grouped_database_views("d1", "状態", ["A", "B"])
            self.assertEqual(out, [{"label": "A", "saved_view_id": "v1"}])


if __name__ == "__main__":
    unittest.main()

