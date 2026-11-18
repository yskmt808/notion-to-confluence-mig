"""HTMLエクスポート未収録の外部同期ブロック検知および --dry-run / 移行後検証 WARNING カウントのテスト (TCK_00009)。"""

import contextlib
import io
import json
import logging
import os
import signal
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import migrate
from src.converter import NotionToConfluenceConverter
from src.extractor import PageNode
from src.html_extractor import HtmlZipExtractor
from src.telemetry import SanitizedTelemetry
from src.verifier import MigrationVerifier
try:
    from storage_xml_helper import assert_valid_storage_xml
except ImportError:
    from tests.storage_xml_helper import assert_valid_storage_xml


def _build_html_doc(title: str, body_inner: str, page_id: str = "11111111-1111-1111-1111-111111111111") -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
  <article id="{page_id}" class="page">
    <header><h1 class="page-title">{title}</h1></header>
    <div class="page-body">
      {body_inner}
    </div>
  </article>
</body>
</html>"""


class TestExternalSyncedBlockDetection(unittest.TestCase):
    """HtmlZipExtractor による外部同期ブロック検知と通常同期ブロック非誤検知の単体テスト。"""

    def test_detects_empty_and_external_ref_synced_blocks(self):
        root_html = _build_html_doc(
            "External Sync Root",
            """
            <p>通常段落</p>
            <div dir="auto" id="ext-empty-1"></div>
            <div dir="auto"><div dir="auto" id="ext-nested-2"></div></div>
            <div class="synced-block" data-synced-from="orig-ext-999" id="ext-attr-3"></div>
            <div class="synced-block" data-synced-from="https://www.notion.so/other/Page-aaaa1111bbbb2222cccc3333dddd4444" id="ext-link-4">
              <a href="https://www.notion.so/other/Page-aaaa1111bbbb2222cccc3333dddd4444">External Synced Ref</a>
            </div>
            """,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            zpath = os.path.join(tmpdir, "ext_sync.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("External Sync Root 11111111111111111111111111111111.html", root_html)

            tree = HtmlZipExtractor(zpath).extract_tree()

        self.assertEqual(len(tree.external_synced_blocks), 4)
        synced_blocks = [b for b in tree.blocks if b.get("type") == "synced_block"]
        self.assertEqual(len(synced_blocks), 4)
        self.assertTrue(all(b.get("_is_external_synced_block") for b in synced_blocks))
        self.assertEqual(synced_blocks[0]["id"], "ext-empty-1")
        self.assertEqual(synced_blocks[2]["synced_block"]["synced_from"]["block_id"], "orig-ext-999")
        self.assertIn("notion.so", synced_blocks[3]["synced_block"]["synced_from"]["block_id"])

        # Storage Format 変換が well-formed XML を出力すること
        conv = NotionToConfluenceConverter()
        storage_xml = conv.blocks_to_storage_format(tree.blocks)
        assert_valid_storage_xml(self, storage_xml)
        self.assertIn("外部同期ブロック", storage_xml)
        self.assertIn("orig-ext-999", storage_xml)

    def test_detects_nested_external_synced_blocks_in_column_and_toggle(self):
        html = _build_html_doc(
            "Nested External Sync",
            """
            <div class="column-list">
              <div class="column">
                <div dir="auto" id="col-ext-sync"></div>
              </div>
              <div class="column"></div>
            </div>
            <details>
              <summary>トグル見出し</summary>
              <div class="indented">
                <div class="synced-block" data-synced-from="orig-in-toggle"></div>
              </div>
            </details>
            """,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            zpath = os.path.join(tmpdir, "nested_ext.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("Nested External Sync 22222222222222222222222222222222.html", html)

            tree = HtmlZipExtractor(zpath).extract_tree()

        # 空の column div 自体は誤検知せず、column 内の <div dir="auto"> と toggle 内の synced-block の2件のみ検知
        self.assertEqual(len(tree.external_synced_blocks), 2)
        refs = [item.get("synced_from") for item in tree.external_synced_blocks]
        self.assertIn("orig-in-toggle", refs)

    def test_internal_synced_blocks_do_not_produce_false_positives(self):
        """本文がHTMLエクスポートに含まれている通常の内部同期ブロックは警告対象にならないこと。"""
        html = _build_html_doc(
            "Internal Sync Only",
            """
            <div dir="auto">
              <div dir="auto">
                <aside class="block-color-blue_background callout" id="callout-ok">
                  <p>同期ブロック１（本文あり）</p>
                </aside>
              </div>
            </div>
            <div dir="auto">
              <p>通常の同期段落テキスト</p>
            </div>
            <div class="synced-block" data-synced-from="orig-ok">
              <p>展開済みの同期ブロック本文</p>
            </div>
            """,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            zpath = os.path.join(tmpdir, "internal_sync.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("Internal Sync Only 33333333333333333333333333333333.html", html)

            tree = HtmlZipExtractor(zpath).extract_tree()

        self.assertEqual(tree.external_synced_blocks, [])
        self.assertEqual(len(tree.blocks), 3)
        self.assertEqual(tree.blocks[0]["type"], "callout")
        self.assertEqual(tree.blocks[1]["type"], "paragraph")
        self.assertEqual(tree.blocks[2]["type"], "paragraph")


class TestTelemetryAndVerifierExternalSyncedBlocks(unittest.TestCase):
    """SanitizedTelemetry と MigrationVerifier での警告記録・WARN判定の単体テスト。"""

    def test_telemetry_counts_external_synced_blocks_without_leaking_titles(self):
        root = PageNode(
            id="aaaa1111bbbb2222cccc3333dddd4444",
            title="Secret Roadmap Page",
            node_type="page",
            blocks=[
                {
                    "id": "sync-1",
                    "type": "synced_block",
                    "synced_block": {"synced_from": {"type": "block_id", "block_id": "orig-111"}},
                    "children_blocks": [],
                    "_is_external_synced_block": True,
                    "_external_synced_warning": "外部同期ブロックの本文がHTMLエクスポートに含まれていません",
                },
                {
                    "id": "sync-2",
                    "type": "synced_block",
                    "synced_block": {"synced_from": None},
                    "children_blocks": [],
                    "_is_external_synced_block": True,
                    "_external_synced_warning": "外部同期ブロックの本文がHTMLエクスポートに含まれていません",
                },
            ],
            external_synced_blocks=[
                {"block_id": "sync-1", "synced_from": "orig-111", "message": "missing"},
                {"block_id": "sync-2", "synced_from": None, "message": "missing"},
            ],
        )
        telemetry = SanitizedTelemetry()
        telemetry.record_tree(root)
        report = telemetry.generate_report()

        self.assertEqual(telemetry.total_warnings_count, 2)
        self.assertEqual(report["warnings_count"], 2)
        self.assertEqual(report["diagnostics"]["external_synced_blocks_count"], 2)
        report_json = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("Secret Roadmap Page", report_json)

    def test_verifier_marks_external_synced_block_as_warn_and_internal_as_pass(self):
        mock_client = MagicMock()
        mock_client.get_page_attachments.return_value = []
        verifier = MigrationVerifier(mock_client)

        # 1. 外部同期ブロックを含むページ -> WARN
        ext_node = PageNode(
            id="p-ext",
            title="Ext Sync Page",
            node_type="page",
            blocks=[
                {"id": "p1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "通常本文"}]}},
                {
                    "id": "s1",
                    "type": "synced_block",
                    "synced_block": {"synced_from": {"type": "block_id", "block_id": "orig-ext-id"}},
                    "children_blocks": [],
                    "_is_external_synced_block": True,
                },
            ],
            external_synced_blocks=[
                {"block_id": "s1", "synced_from": "orig-ext-id", "message": "missing"}
            ],
        )
        mock_client.get_page_by_id.return_value = {
            "id": "1001",
            "title": "Ext Sync Page",
            "ancestors": [{"id": "999"}],
            "body": {
                "storage": {"value": "<p>通常本文</p><p>🔄 <em>同期ブロック（同期元: orig-ext-id）</em></p>"},
                "view": {"value": "<p>通常本文</p>"},
            },
        }
        res_warn = verifier.verify_page_node(ext_node, "1001", expected_parent_id="999")
        self.assertEqual(res_warn["status"], "WARN")
        self.assertTrue(any("外部同期ブロック" in w for w in res_warn["warnings"]))
        self.assertEqual(res_warn["issues"], [])

        # 2. 内部同期ブロック（展開済み）のみのページ -> PASS
        int_node = PageNode(
            id="p-int",
            title="Internal Sync Page",
            node_type="page",
            blocks=[
                {
                    "id": "s-ok",
                    "type": "synced_block",
                    "synced_block": {"synced_from": None},
                    "children_blocks": [
                        {"id": "p2", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "展開済み本文"}]}}
                    ],
                }
            ],
        )
        mock_client.get_page_by_id.return_value = {
            "id": "1002",
            "title": "Internal Sync Page",
            "ancestors": [{"id": "999"}],
            "body": {
                "storage": {"value": "<div><p>展開済み本文</p></div>"},
                "view": {"value": "<p>展開済み本文</p>"},
            },
        }
        res_pass = verifier.verify_page_node(int_node, "1002", expected_parent_id="999")
        self.assertEqual(res_pass["status"], "PASS")
        self.assertEqual(res_pass["warnings"], [])


class TestExternalSyncedBlockCliIntegration(unittest.TestCase):
    """HTML ZIP を用いた --dry-run および移行後検証 (Step 3) の結合テスト。"""

    CONFLUENCE_ENV = {
        "CONFLUENCE_URL": "https://example.atlassian.net/wiki",
        "CONFLUENCE_USERNAME": "bot@example.com",
        "CONFLUENCE_API_TOKEN": "dummy-token",
        "CONFLUENCE_SPACE_KEY": "DOCS",
    }

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir_obj.name
        os.chdir(self.tmpdir)

        root_logger = logging.getLogger()
        self._orig_handlers = list(root_logger.handlers)
        self._orig_level = root_logger.level
        self._orig_sigint = signal.getsignal(signal.SIGINT)
        self._orig_sigterm = signal.getsignal(signal.SIGTERM)

    def tearDown(self):
        signal.signal(signal.SIGINT, self._orig_sigint)
        signal.signal(signal.SIGTERM, self._orig_sigterm)

        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            if h not in self._orig_handlers:
                try:
                    h.close()
                except Exception:
                    pass
        root_logger.handlers = self._orig_handlers
        root_logger.setLevel(self._orig_level)

        os.chdir(self._orig_cwd)
        self._tmpdir_obj.cleanup()

    def _run_main(self, argv, env=None):
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.dict(os.environ, env or {}, clear=True), \
             patch.object(sys, "argv", ["migrate.py"] + argv), \
             contextlib.redirect_stdout(stdout_buf), \
             contextlib.redirect_stderr(stderr_buf):
            migrate.main()
        return stdout_buf.getvalue(), stderr_buf.getvalue()

    def _create_mixed_sync_zip(self, zip_path: str):
        root_id = "11111111111111111111111111111111"
        child_ext_id = "22222222222222222222222222222222"
        child_ok_id = "33333333333333333333333333333333"

        root_html = _build_html_doc(
            "Root Hub",
            """
            <p>ルート本文</p>
            <div dir="auto" id="root-ext-sync"></div>
            """,
            page_id=root_id,
        )
        child_ext_html = _build_html_doc(
            "External Ref Child",
            """
            <p>子ページ本文</p>
            <div class="synced-block" data-synced-from="orig-outside-777" id="child-ext-sync"></div>
            """,
            page_id=child_ext_id,
        )
        child_ok_html = _build_html_doc(
            "Internal Sync Child",
            """
            <div dir="auto">
              <aside class="block-color-blue_background callout" id="ok-callout">
                <p>内部同期ブロック本文</p>
              </aside>
            </div>
            """,
            page_id=child_ok_id,
        )
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"Root Hub {root_id}.html", root_html.encode("utf-8"))
            zf.writestr(f"Root Hub/External Ref Child {child_ext_id}.html", child_ext_html.encode("utf-8"))
            zf.writestr(f"Root Hub/Internal Sync Child {child_ok_id}.html", child_ok_html.encode("utf-8"))

    def test_dry_run_counts_external_synced_warnings_and_lists_affected_pages(self):
        zpath = os.path.join(self.tmpdir, "mixed_sync.zip")
        self._create_mixed_sync_zip(zpath)
        report_dir = os.path.join(self.tmpdir, "reports")
        os.makedirs(report_dir, exist_ok=True)
        cp_path = os.path.join(self.tmpdir, "cp_dry.json")

        with patch("migrate.ConfluenceImporter") as mock_imp_cls:
            out, _ = self._run_main([
                "--zip", zpath,
                "--dry-run",
                "--checkpoint", cp_path,
                "--generate-debug-report",
                "--debug-report-dir", report_dir,
            ])

        mock_imp_cls.assert_not_called()
        self.assertIn("ドライラン結果サマリー: WARNING 2 件", out)
        self.assertIn("外部同期ブロックを 2 件検出しました（対象ページ: 2 件）", out)
        self.assertIn("[HTML] Root Hub", out)
        self.assertIn("[HTML] External Ref Child", out)
        self.assertNotIn("- [page] [HTML] Internal Sync Child", out)

        with open(os.path.join(report_dir, "debug_report.json"), "r", encoding="utf-8") as f:
            report_data = json.load(f)
        self.assertEqual(report_data["warnings_count"], 2)
        self.assertEqual(report_data["diagnostics"]["external_synced_blocks_count"], 2)

    def test_post_migration_verification_counts_external_synced_warnings(self):
        zpath = os.path.join(self.tmpdir, "mixed_sync.zip")
        self._create_mixed_sync_zip(zpath)
        cp_path = os.path.join(self.tmpdir, "cp_full.json")

        root_id = "11111111-1111-1111-1111-111111111111"
        child_ext_id = "22222222-2222-2222-2222-222222222222"
        child_ok_id = "33333333-3333-3333-3333-333333333333"

        mock_client = MagicMock()
        mock_client.get_page_attachments.return_value = []

        def fake_get_page_by_id(page_id, expand=None):
            pages = {
                "5001": {
                    "id": "5001",
                    "title": "[HTML] Root Hub",
                    "ancestors": [{"id": "999"}],
                    "body": {"storage": {"value": "<p>ルート本文</p>"}, "view": {"value": "<p>ルート本文</p>"}},
                },
                "5002": {
                    "id": "5002",
                    "title": "[HTML] External Ref Child",
                    "ancestors": [{"id": "5001"}],
                    "body": {
                        "storage": {"value": "<p>子ページ本文</p><p>🔄 <em>同期ブロック（同期元: orig-outside-777）</em></p>"},
                        "view": {"value": "<p>子ページ本文</p>"},
                    },
                },
                "5003": {
                    "id": "5003",
                    "title": "[HTML] Internal Sync Child",
                    "ancestors": [{"id": "5001"}],
                    "body": {
                        "storage": {"value": '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>内部同期ブロック本文</p></ac:rich-text-body></ac:structured-macro>'},
                        "view": {"value": "<p>内部同期ブロック本文</p>"},
                    },
                },
            }
            return pages.get(page_id)

        mock_client.get_page_by_id.side_effect = fake_get_page_by_id

        mock_importer = MagicMock()
        mock_importer.client = mock_client
        mock_importer.import_tree.return_value = {
            root_id: {"title": "[HTML] Root Hub", "node_type": "page", "confluence_id": "5001", "url": "https://example.invalid/5001"},
            child_ext_id: {"title": "[HTML] External Ref Child", "node_type": "page", "confluence_id": "5002", "url": "https://example.invalid/5002"},
            child_ok_id: {"title": "[HTML] Internal Sync Child", "node_type": "page", "confluence_id": "5003", "url": "https://example.invalid/5003"},
        }

        with patch("migrate.ConfluenceImporter", return_value=mock_importer):
            out, _ = self._run_main(
                [
                    "--zip", zpath,
                    "--no-dashboard",
                    "--confluence-parent-id", "999",
                    "--checkpoint", cp_path,
                ],
                env=self.CONFLUENCE_ENV,
            )

        self.assertIn("検証サマリー: 合計 3 ページ | 正常: 1 | 警告: 2 | 失敗: 0", out)
        self.assertIn("⚠️ [WARN] [HTML] Root Hub (ID: 5001)", out)
        self.assertIn("⚠️ [WARN] [HTML] External Ref Child (ID: 5002)", out)
        self.assertNotIn("✅ [PASS] [HTML] Internal Sync Child (ID: 5003)", out)
        self.assertIn("HTMLエクスポート未収録・未展開の外部同期ブロックを検出しました", out)
        self.assertIn("orig-outside-777", out)

    def _create_synced_external_db_zip(self, zip_path: str):
        root_id = "11111111-1111-1111-1111-111111111111"
        child_id = "22222222-2222-2222-2222-222222222222"
        ext_db_id = "4187be77-284d-80fc-8b42-f0957d728070"

        root_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Root</title></head>
<body>
  <article id="{root_id}" class="page sans">
    <div class="page-body">
      <p>ルート本文</p>
      <figure id="{child_id}" class="link-to-page"><a href="Root/Child {child_id.replace('-', '')}.html">Child</a></figure>
    </div>
  </article>
</body></html>"""

        child_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Child</title></head>
<body>
  <article id="{child_id}" class="page sans">
    <div class="page-body">
      <div dir="auto">
        <div class="collection-content" dir="ltr" id="{ext_db_id}">
          <h4 class="collection-title">外部DB</h4>
          <div class="collection-content-wrapper">
            <table class="collection-content"><tbody></tbody></table>
          </div>
        </div>
      </div>
    </div>
  </article>
</body></html>"""

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(f"Root {root_id.replace('-', '')}.html", root_html)
            zf.writestr(f"Root/Child {child_id.replace('-', '')}.html", child_html)

    def test_synced_external_db_dry_run_converter_and_verifier_warnings(self):
        """同期ブロック経由の外部DB参照時にdry-run WARNING・本文警告・検証WARNが出力されること"""
        zpath = os.path.join(self.tmpdir, "synced_ext_db.zip")
        self._create_synced_external_db_zip(zpath)
        cp_path = os.path.join(self.tmpdir, "cp_ext_db.json")
        report_dir = os.path.join(self.tmpdir, "reports_ext_db")

        # 1. Extractor + Converter 単体確認
        tree = HtmlZipExtractor(zpath).extract_tree()
        child_node = tree.children[0]
        self.assertEqual(len(child_node.unresolved_external_dbs), 1)
        self.assertEqual(child_node.unresolved_external_dbs[0]["database_name"], "外部DB")
        self.assertEqual(child_node.unresolved_external_dbs[0]["database_id"], "4187be77-284d-80fc-8b42-f0957d728070")
        self.assertTrue(child_node.unresolved_external_dbs[0]["via_synced_block"])

        storage = NotionToConfluenceConverter().blocks_to_storage_format(child_node.blocks)
        assert_valid_storage_xml(self, storage)
        self.assertEqual(
            storage,
            '<ac:structured-macro ac:name="note"><ac:parameter ac:name="title">未展開の外部データベース: 外部DB</ac:parameter><ac:rich-text-body><p>移行対象外（エクスポートZIP未収録）の外部データベースのため展開できませんでした。</p></ac:rich-text-body></ac:structured-macro>',
        )

        # 2. dry-run サマリー・テレメトリ確認
        os.makedirs(report_dir, exist_ok=True)
        with patch("migrate.ConfluenceImporter") as mock_imp_cls:
            out, _ = self._run_main([
                "--zip", zpath,
                "--dry-run",
                "--no-dashboard",
                "--checkpoint", cp_path,
                "--generate-debug-report",
                "--debug-report-dir", report_dir,
            ])

        mock_imp_cls.assert_not_called()
        self.assertIn("ドライラン結果サマリー: WARNING 1 件", out)
        self.assertIn("移行対象外（ZIP未収録）の外部データベース参照を 1 件検出しました（対象ページ: 1 件）", out)
        self.assertIn("[HTML] Child", out)
        self.assertIn("外部DB", out)

        with open(os.path.join(report_dir, "debug_report.json"), "r", encoding="utf-8") as f:
            report_data = json.load(f)
        self.assertEqual(report_data["warnings_count"], 1)
        self.assertEqual(report_data["diagnostics"]["unresolved_external_dbs_count"], 1)

        # 3. Verifier が WARN を返すこと
        mock_client = MagicMock()
        mock_client.get_page_attachments.return_value = []
        mock_client.get_page_by_id.return_value = {
            "id": "6002",
            "title": "Child",
            "ancestors": [{"id": "6001"}],
            "body": {"storage": {"value": storage}, "view": {"value": storage}},
        }
        res = MigrationVerifier(mock_client).verify_page_node(child_node, "6002")
        self.assertEqual(res["status"], "WARN")
        self.assertTrue(any("外部データベース参照を検出しました" in w and "外部DB" in w for w in res["warnings"]))

    def test_error_pattern_zip_detects_unresolved_external_db_warning(self):
        """更新された error_pattern_ExportBlock.zip で外部DB参照がWARNINGとして検出されること"""
        repo_root = os.path.dirname(os.path.dirname(__file__))
        zip_path = os.path.join(repo_root, "error_pattern_ExportBlock.zip")
        if not os.path.exists(zip_path):
            self.skipTest("error_pattern_ExportBlock.zip not found")

        tree = HtmlZipExtractor(zip_path).extract_tree()
        telemetry = SanitizedTelemetry()
        telemetry.record_tree(tree)
        self.assertGreaterEqual(telemetry.unresolved_external_dbs_count, 1)
        self.assertGreaterEqual(telemetry.total_warnings_count, 1)


class TestUnifiedMissingAndUnsupportedWarningsPlanA(unittest.TestCase):
    """案A: 5カテゴリ統一WARNING検知・noteマクロ変換・dry-run・Verifier(WARN)の統合テスト。"""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir_obj.name
        os.chdir(self.tmpdir)
        root_logger = logging.getLogger()
        self._orig_handlers = list(root_logger.handlers)
        self._orig_level = root_logger.level

    def tearDown(self):
        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            if h not in self._orig_handlers:
                try:
                    h.close()
                except Exception:
                    pass
        root_logger.handlers = self._orig_handlers
        root_logger.setLevel(self._orig_level)
        os.chdir(self._orig_cwd)
        self._tmpdir_obj.cleanup()

    def _run_main(self, argv):
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.object(sys, "argv", ["migrate.py"] + argv), \
             contextlib.redirect_stdout(stdout_buf), \
             contextlib.redirect_stderr(stderr_buf):
            migrate.main()
        return stdout_buf.getvalue(), stderr_buf.getvalue()

    def test_missing_external_pages_and_attachments_across_pipeline(self):
        """ZIP未収録の外部ページ参照・ローカル添付(画像/PDF/音声/動画/ファイル)・通常リンクの境界を全段検証する。"""
        root_id = "aaaa1111-1111-1111-1111-111111111111"
        missing_child_id = "bbbb2222-2222-2222-2222-222222222222"
        missing_inline_id = "cccc3333333333333333333333333333"
        present_child_id = "dddd4444-4444-4444-4444-444444444444"

        root_html = _build_html_doc(
            "Secret Hub Page",
            f"""
            <p>通常のテキスト段落</p>
            <p><a href="#local-anchor">ページ内アンカー</a> と <a href="https://example.com/public">通常のWebリンク</a> と <a href="Root/PresentChild {present_child_id.replace('-', '')}.html">収録済み子ページ</a></p>
            <figure id="{missing_child_id}" class="link-to-page"><a href="MissingChild {missing_child_id.replace('-', '')}.html">未収録子ページ</a></figure>
            <div dir="auto">
              <p>同期ブロック内の <a href="https://www.notion.so/org/Outside-{missing_inline_id}">外部Notionリンク</a></p>
              <figure class="image"><img src="assets/missing_photo.png" /></figure>
            </div>
            <figure class="pdf"><div class="source"><a href="assets/missing_spec.pdf">missing_spec.pdf</a></div></figure>
            <figure><div class="source"><a href="assets/missing_audio.mp3">missing_audio.mp3</a></div></figure>
            <figure><div class="source"><a href="assets/missing_video.mp4">missing_video.mp4</a></div></figure>
            <figure><div class="source"><a href="assets/missing_sheet.xlsx">missing_sheet.xlsx</a></div></figure>
            <figure><a href="https://drive.google.com/file/d/xyz/view">極秘仕様書.gdoc</a></figure>
            """,
            page_id=root_id,
        )
        present_child_html = _build_html_doc(
            "PresentChild",
            "<p>収録済み子ページの本文</p>",
            page_id=present_child_id,
        )

        zpath = os.path.join(self.tmpdir, "plan_a_missing.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr(f"Secret Hub Page {root_id.replace('-', '')}.html", root_html)
            zf.writestr(f"Secret Hub Page/PresentChild {present_child_id.replace('-', '')}.html", present_child_html)

        tree = HtmlZipExtractor(zpath).extract_tree()

        # 1. Extractor: 外部ページ(未収録child_page + 同期ブロック内inline)、ZIP未収録添付5種が検出され、通常リンク・収録済みページは誤検知されないこと
        self.assertEqual(len(tree.unresolved_external_pages), 2)
        kinds = {p["kind"] for p in tree.unresolved_external_pages}
        self.assertEqual(kinds, {"child_page", "inline_link"})
        inline_entry = next(p for p in tree.unresolved_external_pages if p["kind"] == "inline_link")
        self.assertTrue(inline_entry["via_synced_block"])

        self.assertEqual(len(tree.missing_zip_attachments), 5)
        att_types = {a["type"] for a in tree.missing_zip_attachments}
        self.assertEqual(att_types, {"image", "pdf", "audio", "video", "file"})
        img_entry = next(a for a in tree.missing_zip_attachments if a["type"] == "image")
        self.assertTrue(img_entry["via_synced_block"])

        # 2. Importer._process_attachments: missing_in_zip はクラッシュせずアップロード対象外のままスキップされること
        from src.importer import ConfluenceImporter
        mock_client = MagicMock()
        mock_client.get_page_attachments.return_value = []
        importer = ConfluenceImporter("https://example.invalid/wiki", "u", "t", "SPACE")
        importer.client = mock_client
        importer._process_attachments(tree.blocks, "9001")
        mock_client.upload_attachment.assert_not_called()

        # 3. Converter: 統一 note マクロとインラインフォールバックが生成され、<ri:attachment> や ⚠️タイトル絵文字が混入しないこと
        conv = NotionToConfluenceConverter(id_to_title_map={present_child_id: "PresentChild"})
        storage = conv.blocks_to_storage_format(tree.blocks)
        assert_valid_storage_xml(self, storage)
        self.assertNotIn("<ri:attachment", storage)
        self.assertIn("移行対象外の外部ページ: 未収録子ページ", storage)
        self.assertIn("（⚠️ 移行対象外ページ）", storage)
        self.assertIn("未収録の添付画像: missing_photo.png", storage)
        self.assertIn("未収録のPDF: missing_spec.pdf", storage)
        self.assertIn("未収録の音声ファイル: missing_audio.mp3", storage)
        self.assertIn("未収録の動画ファイル: missing_video.mp4", storage)
        self.assertIn("未収録の添付ファイル: missing_sheet.xlsx", storage)
        self.assertIn("未対応ページ要素: Google Drive", storage)
        # note マクロの title パラメータには ⚠️ を含めない (TCK_00012 準拠)
        import re as _re
        note_titles = _re.findall(r'<ac:parameter ac:name="title">(.*?)</ac:parameter>', storage)
        self.assertTrue(len(note_titles) >= 7)
        self.assertTrue(all("⚠️" not in t for t in note_titles), f"Unexpected emoji in note title: {note_titles}")

        # 4. Telemetry: 件数集計が一致しページタイトル等PIIが漏洩しないこと
        telemetry = SanitizedTelemetry()
        telemetry.record_tree(tree)
        report = telemetry.generate_report()
        self.assertEqual(report["diagnostics"]["unresolved_external_pages_count"], 2)
        self.assertEqual(report["diagnostics"]["missing_zip_attachments_count"], 5)
        self.assertGreaterEqual(len(report["diagnostics"]["unsupported_elements_detected"]), 1)
        self.assertEqual(report["warnings_count"], 8)  # 2 ext_pages + 5 missing_atts + 1 unsupported
        self.assertNotIn("Secret Hub Page", json.dumps(report, ensure_ascii=False))

        # 5. CLI --dry-run: 全カテゴリの WARNING が整理されたサマリーで表示されること
        report_dir = os.path.join(self.tmpdir, "reports_plan_a")
        os.makedirs(report_dir, exist_ok=True)
        cp_path = os.path.join(self.tmpdir, "cp_plan_a.json")
        with patch("migrate.ConfluenceImporter") as mock_imp_cls:
            out, _ = self._run_main([
                "--zip", zpath,
                "--dry-run",
                "--no-dashboard",
                "--checkpoint", cp_path,
                "--generate-debug-report",
                "--debug-report-dir", report_dir,
            ])
        mock_imp_cls.assert_not_called()
        self.assertIn("ドライラン結果サマリー: WARNING 8 件", out)
        self.assertIn("未対応要素・外部アプリ埋め込み（フォールバック注記対象）を 1 件検出しました", out)
        self.assertIn("移行対象外（ZIP未収録）の外部ページ参照を 2 件検出しました", out)
        self.assertIn("ZIP未収録の添付ファイル・メディアを 5 件検出しました", out)
        self.assertIn("[同期ブロック経由]", out)

        # 6. Verifier: 添付・メディア欠落で FAIL にならず、WARN と checks_passed が記録されること
        mock_client.get_page_by_id.return_value = {
            "id": "9001",
            "title": "Secret Hub Page",
            "ancestors": [{"id": "9000"}],
            "body": {"storage": {"value": storage}, "view": {"value": storage}},
        }
        vres = MigrationVerifier(mock_client).verify_page_node(tree, "9001", expected_parent_id="9000")
        self.assertEqual(vres["status"], "WARN", f"Expected WARN, got issues={vres['issues']}, warnings={vres['warnings']}")
        self.assertEqual(vres["issues"], [])
        self.assertTrue(any("外部ページ参照を検出しました (2 件)" in w for w in vres["warnings"]))
        self.assertTrue(any("ZIP未収録の添付ファイル・メディアを検出しました (5 件)" in w for w in vres["warnings"]))
        self.assertTrue(any("Unresolved external page fallback verified" in c for c in vres["checks_passed"]))
        self.assertTrue(any("Missing ZIP attachment fallback note verified" in c for c in vres["checks_passed"]))

    def test_multizip_cross_zip_resolution_clears_external_page_and_db_warnings(self):
        """マルチZIP分割エクスポート時に別ZIP収録のページ・DBは警告対象から自動解消されること。"""
        from src.html_extractor import MultiZipExtractor

        root1_id = "11111111-1111-1111-1111-111111111111"
        page2_id = "22222222-2222-2222-2222-222222222222"
        db2_id = "33333333-3333-3333-3333-333333333333"

        zip1_html = _build_html_doc(
            "Workspace A",
            f"""
            <p><a href="Workspace B/Page Two {page2_id.replace('-', '')}.html">Page Two</a></p>
            <div class="collection-content" id="{db2_id}">
              <h4 class="collection-title">Shared DB</h4>
            </div>
            """,
            page_id=root1_id,
        )
        zip2_html = _build_html_doc(
            "Workspace B",
            f"""
            <figure id="{page2_id}" class="link-to-page"><a href="Workspace B/Page Two {page2_id.replace('-', '')}.html">Page Two</a></figure>
            """,
            page_id="44444444-4444-4444-4444-444444444444",
        )
        page2_html = _build_html_doc("Page Two", "<p>Body in Zip2</p>", page_id=page2_id)

        z1 = os.path.join(self.tmpdir, "part1.zip")
        z2 = os.path.join(self.tmpdir, "part2.zip")
        with zipfile.ZipFile(z1, "w") as zf1:
            zf1.writestr(f"Workspace A {root1_id.replace('-', '')}.html", zip1_html)
        with zipfile.ZipFile(z2, "w") as zf2:
            zf2.writestr("Workspace B 44444444444444444444444444444444.html", zip2_html)
            zf2.writestr(f"Workspace B/Page Two {page2_id.replace('-', '')}.html", page2_html)
            zf2.writestr(f"Workspace B/Shared DB {db2_id.replace('-', '')}.csv", "Name,Status\nItem1,Open\n")

        root = MultiZipExtractor([z1, z2]).extract_tree()
        telemetry = SanitizedTelemetry()
        telemetry.record_tree(root)
        self.assertEqual(telemetry.unresolved_external_dbs_count, 0)
        self.assertEqual(telemetry.unresolved_external_pages_count, 0)
        self.assertEqual(telemetry.total_warnings_count, 0)

    def test_invisible_empty_div_with_uuid_is_not_flagged_as_external_synced_block(self):
        """TCK_00026: Notion上の不可視・空要素 (<div id="<uuid>" dir="auto"></div>) は外部同期ブロックと誤検知されないこと。"""
        from migrate import collect_external_synced_pages

        doc = _build_html_doc(
            "Invisible Div Page",
            """
            <p>Normal paragraph</p>
            <div id="4157be77-284d-80dc-82fb-cb5ee2c54c71" dir="auto"></div>
            """,
        )
        zip_path = os.path.join(self.tmpdir, "inv.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Invisible Div Page 12345678123412341234123456789012.html", doc)
        tree = HtmlZipExtractor(zip_path).extract_tree()
        count, pages = collect_external_synced_pages(tree)
        self.assertEqual(count, 0)
        self.assertEqual(pages, [])

    def test_multihop_nested_synced_block_expansion_and_empty_removal_via_api(self):
        """TCK_00026: 多段ネスト同期ブロックのNotion APIピンポイント展開および空同期ブロックの除去が行われること。"""
        from unittest.mock import patch
        from migrate import collect_external_synced_pages
        from src.api_enricher import NotionApiEnricher

        page_id = "4137be77-284d-8041-89c8-d080073331b4"
        outer_sync_id = "4157be77-284d-805c-bc1f-d19bd475dde9"
        inner_sync_id = "4157be77-284d-801e-b467-cab868436607"
        orig_sync_id = "4157be77-284d-805b-845e-dd942bbeddfe"
        dup_sync_id = "41b7be77-284d-80c4-bf31-f0dc1c5deb97"
        empty_sync_id = "4157be77-284d-80dc-82fb-cb5ee2c54c71"

        doc = _build_html_doc(
            "MultiHop Sync Page",
            f"""
            <div dir="auto"><figure class="callout" id="{outer_sync_id}"><p>同期ブロック１</p></figure></div>
            <div dir="auto"><div dir="auto"></div></div>
            """,
            page_id=page_id,
        )
        zip_path = os.path.join(self.tmpdir, "multihop.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(f"MultiHop Sync Page {page_id.replace('-', '')}.html", doc)
        tree = HtmlZipExtractor(zip_path).extract_tree()
        before_count, _ = collect_external_synced_pages(tree)
        self.assertEqual(before_count, 1)

        children_map = {
            page_id: [
                {
                    "id": outer_sync_id,
                    "type": "synced_block",
                    "has_children": True,
                    "synced_block": {"synced_from": None},
                },
                {
                    "id": dup_sync_id,
                    "type": "synced_block",
                    "has_children": False,
                    "synced_block": {"synced_from": {"block_id": outer_sync_id}},
                },
                {
                    "id": empty_sync_id,
                    "type": "synced_block",
                    "has_children": False,
                    "synced_block": {"synced_from": None},
                },
            ],
            outer_sync_id: [
                {
                    "id": inner_sync_id,
                    "type": "synced_block",
                    "has_children": False,
                    "synced_block": {"synced_from": {"block_id": orig_sync_id}},
                }
            ],
            orig_sync_id: [
                {
                    "id": "99999999-9999-9999-9999-999999999999",
                    "type": "callout",
                    "has_children": False,
                    "callout": {
                        "rich_text": [{"plain_text": "同期ブロック１", "annotations": {}}],
                        "icon": {"type": "emoji", "emoji": "💡"},
                    },
                }
            ],
            empty_sync_id: [],
        }

        enricher = NotionApiEnricher(token="secret_dummy")
        with patch.object(
            enricher,
            "_fetch_block_children_cached",
            side_effect=lambda bid: (200, children_map.get(bid, [])),
        ):
            expanded = enricher.enrich_external_synced_blocks(tree)

        self.assertEqual(expanded, 1)
        after_count, _ = collect_external_synced_pages(tree)
        self.assertEqual(after_count, 0)
        storage = NotionToConfluenceConverter().generate_page_content(tree)
        self.assertEqual(storage.count("同期ブロック１"), 2)

    def test_sharepoint_and_external_urls_with_guids_are_not_flagged_as_missing_pages(self):
        """TCK_00027: SharePoint等のNotion外URLにGUIDが含まれていてもZIP未収録の外部ページ参照として誤警告されないこと。"""
        sp_url = "https://contoso.sharepoint.com/:x:/r/sites/team/_layouts/15/Doc.aspx?sourcedoc=%7B12345678-1234-1234-1234-123456789abc%7D&action=default"
        teams_url = "https://teams.microsoft.com/l/entity/87654321-4321-4321-4321-cba987654321/0"
        doc = _build_html_doc(
            "External Links Page",
            f"""
            <p><a href="{sp_url}">SharePoint Excel</a></p>
            <p><a href="{teams_url}">Teams Channel</a></p>
            """,
        )
        zip_path = os.path.join(self.tmpdir, "sp.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("External Links Page 12345678123412341234123456789012.html", doc)
        tree = HtmlZipExtractor(zip_path).extract_tree()
        telemetry = SanitizedTelemetry()
        telemetry.record_tree(tree)
        self.assertEqual(telemetry.unresolved_external_pages_count, 0)
        self.assertEqual(tree.unresolved_external_pages, [])
        storage = NotionToConfluenceConverter().generate_page_content(tree)
        self.assertNotIn("外部ページ参照（ZIP未収録）", storage)
        self.assertIn("SharePoint Excel", storage)

    def test_page_mention_links_detected_and_warnings_hidden_by_default_except_dry_run_flag(self):
        """TCK_00031: ページメンションリンク(app.notion.com及びmention[page])をZIP外警告対象に含め、デフォルト非表示・--show-external-warnings時のみdry-run詳細出力・Step3は件数のみにすること。"""
        from src.extractor import mark_unresolved_external_references

        ext_mention_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        api_mention_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        doc = _build_html_doc(
            "Mention Warning Page",
            f"""
            <p><a href="https://app.notion.com/p/{ext_mention_id.replace('-', '')}">外部メンションページ</a></p>
            """,
            page_id="12345678-1234-1234-1234-123456789012",
        )
        zpath = os.path.join(self.tmpdir, "mention_warn.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("Mention Warning Page 12345678123412341234123456789012.html", doc)

        # 1. app.notion.com リンクおよび mention[type=page] が両方 ZIP外ページ参照として検出されること
        tree = HtmlZipExtractor(zpath).extract_tree()
        self.assertEqual(len(tree.unresolved_external_pages), 1)
        self.assertEqual(tree.unresolved_external_pages[0]["target_title"], "外部メンションページ")

        api_node = PageNode(
            id="22222222-2222-2222-2222-222222222222",
            title="API Mention Page",
            node_type="page",
            blocks=[
                {
                    "id": "b-mention-1",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "mention",
                                "plain_text": "外部APIメンション",
                                "mention": {"type": "page", "page": {"id": api_mention_id}},
                            }
                        ]
                    },
                }
            ],
        )
        mark_unresolved_external_references(api_node)
        self.assertEqual(len(api_node.unresolved_external_pages), 1)
        self.assertEqual(api_node.unresolved_external_pages[0]["kind"], "page_mention")
        self.assertEqual(api_node.unresolved_external_pages[0]["target_title"], "外部APIメンション")

        # 2. --dry-run デフォルトでは詳細が非表示で件数サマリーのみ出力されること
        cp_path = os.path.join(self.tmpdir, "cp_m1.json")
        out_default, _ = self._run_main(["--zip", zpath, "--dry-run", "--no-dashboard", "--checkpoint", cp_path])
        self.assertIn("移行対象外（ZIP未収録）の外部ページ参照を 1 件検出しました", out_default)
        self.assertIn("--show-external-warnings", out_default)
        self.assertNotIn("外部メンションページ", out_default)

        # 3. --dry-run --show-external-warnings 指定時のみ詳細が出力されること
        cp_path2 = os.path.join(self.tmpdir, "cp_m2.json")
        out_detail, _ = self._run_main([
            "--zip", zpath, "--dry-run", "--no-dashboard", "--show-external-warnings", "--checkpoint", cp_path2
        ])
        self.assertIn("移行対象外（ZIP未収録）の外部ページ参照を 1 件検出しました", out_detail)
        self.assertIn("外部メンションページ", out_detail)

        # 4. Step 3 (print_verification_results) では外部リンク警告は件数のみ表示され個別ページ行に出ないこと
        verify_results = [
            {
                "page_id": "9001",
                "title": "Mention Warning Page",
                "status": "WARN",
                "checks_passed": [],
                "warnings": ["移行対象外（ZIP未収録）の外部ページ参照を検出しました (3 件) [参照先: A, B, C]"],
                "issues": [],
            }
        ]
        import io
        from contextlib import redirect_stdout
        buf_v = io.StringIO()
        with redirect_stdout(buf_v):
            migrate.print_verification_results(verify_results, verbose=False, show_all=False)
        v_out = buf_v.getvalue()
        self.assertIn("移行対象外（ZIP未収録）の外部ページ参照警告: 3 件 (対象ページ: 1 件)", v_out)
        self.assertNotIn("[参照先: A, B, C]", v_out)
        self.assertNotIn("[WARN] Mention Warning Page", v_out)


if __name__ == "__main__":
    unittest.main()
