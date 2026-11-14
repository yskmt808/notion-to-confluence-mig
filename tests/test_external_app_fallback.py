"""外部アプリ連携埋め込み・破損/プレースホルダーURLのフォールバック処理テスト (TCK_00012)。

Google Drive / Docs / OneDrive / Dropbox / Box / Miro / Trello / GitHub / Slack 等の
認証必須外部アプリ埋め込み、https://app.notion.comundefined 等の破損/プレースホルダーURL、
空の link_preview / embed、および独立した <iframe>/<embed>/<object> について、
HTML ZIP 抽出 / Notion API 抽出 -> Importer 非ダウンロード -> Converter noteマクロ変換
(XML整形式) -> Verifier WARN判定 -> Telemetry 記録までを一貫して検証する。
"""

import json
import logging
import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

try:
    from storage_xml_helper import assert_valid_storage_xml
except ImportError:
    from tests.storage_xml_helper import assert_valid_storage_xml

from src.converter import NotionToConfluenceConverter
from src.extractor import NotionExtractor, PageNode
from src.html_extractor import HtmlZipExtractor
from src.importer import ConfluenceImporter
from src.telemetry import SanitizedTelemetry
from src.verifier import MigrationVerifier


def _wrap_html(title: str, body_html: str, page_id: str = "11111111-1111-1111-1111-111111111111") -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
  <article id="{page_id}" class="page sans">
    <header><h1 class="page-title">{title}</h1></header>
    <div class="page-body">
      {body_html}
    </div>
  </article>
</body>
</html>"""


class TestHtmlExternalAppAndBrokenUrlFallback(unittest.TestCase):
    """HTML ZIP における外部アプリ連携埋め込み・破損URL・iframe等の抽出〜検証一貫テスト。"""

    def _extract_from_html(self, body_html: str, extra_files=None) -> PageNode:
        html_doc = _wrap_html("External Embed Test Page", body_html)
        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        zpath = os.path.join(tmpdir_obj.name, "test_ext.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("External Embed Test Page 11111111111111111111111111111111.html", html_doc)
            for fname, content in (extra_files or {}).items():
                zf.writestr(fname, content)
        tree = HtmlZipExtractor(zpath).extract_tree()
        self.assertIsNotNone(tree)
        return tree

    def test_broken_and_placeholder_urls_never_download_and_render_note_macro(self):
        """app.notion.comundefined / undefined終端ホスト / 空 / # / 不正app.notion.comパスが安全にフォールバックされること。"""
        body = """
        <figure id="4187be77-284d-80ab-bc01-ef17eb6ab09f" dir="ltr">
          <div class="source"><a href="https://app.notion.comundefined"></a></div>
        </figure>
        <figure id="blk-undef-host">
          <div class="source"><a href="https://example.comundefined/file.pdf">壊れたPDFリンク</a></div>
        </figure>
        <figure id="blk-empty-href">
          <div class="source"><a href="">空URLソース</a></div>
        </figure>
        <figure id="blk-hash-href">
          <div class="source"><a href="#">ハッシュのみ</a></div>
        </figure>
        <figure id="blk-ws-href">
          <div class="source"><a href="   ">空白のみ</a></div>
        </figure>
        <figure id="blk-notion-bad-path">
          <div class="source"><a href="https://app.notion.com/undefined">壊れたNotionパス</a></div>
        </figure>
        <figure id="blk-notion-root">
          <div class="source"><a href="https://app.notion.com/">Notionルート</a></div>
        </figure>
        """
        tree = self._extract_from_html(body)
        self.assertEqual(len(tree.blocks), 7)

        for b in tree.blocks:
            self.assertTrue(b.get("_is_unsupported_external_embed"), f"Expected unsupported flag on {b}")
            self.assertFalse(b.get("_attachment_uploaded", True))
            self.assertNotEqual(b.get("type"), "file")
            self.assertNotEqual(b.get("type"), "pdf")

        # Importer で HTTP GET や添付アップロードが一切走らないこと
        with patch("src.importer.ConfluenceClient") as mock_cli_cls, \
             patch("src.importer.requests.get") as mock_requests_get:
            mock_cli = mock_cli_cls.return_value
            mock_cli.base_url = "https://example.atlassian.net/wiki"
            importer = ConfluenceImporter("https://example.atlassian.net/wiki", "u", "t", "SPACE")
            with self.assertNoLogs("src.importer", level=logging.WARNING):
                importer._process_attachments(tree.blocks, "page-100")

            mock_requests_get.assert_not_called()
            mock_cli.upload_attachment.assert_not_called()
            for b in tree.blocks:
                self.assertFalse(b.get("_attachment_uploaded", True))

        # Converter が破損 href を一切出力せず note マクロを出力し、XML整形式を満たすこと
        converter = NotionToConfluenceConverter()
        storage_xml = converter.blocks_to_storage_format(tree.blocks)
        assert_valid_storage_xml(self, storage_xml)
        self.assertNotIn("app.notion.comundefined", storage_xml)
        self.assertNotIn("example.comundefined", storage_xml)
        self.assertNotIn('href=""', storage_xml)
        self.assertNotIn('href="#"', storage_xml)
        self.assertEqual(storage_xml.count('<ac:structured-macro ac:name="note">'), 7)
        self.assertIn("⚠️ 未対応ページ要素:", storage_xml)

        # Verifier が WARN 判定（FAIL 誤検知なし）となること
        mock_vclient = MagicMock()
        mock_vclient.get_page_attachments.return_value = []
        mock_vclient.get_page_by_id.return_value = {
            "id": "page-100",
            "title": tree.title,
            "body": {"storage": {"value": storage_xml}, "view": {"value": storage_xml}},
        }
        verifier = MigrationVerifier(mock_vclient)
        res = verifier.verify_page_node(tree, "page-100")
        self.assertEqual(res["status"], "WARN")
        self.assertEqual(res["issues"], [])
        self.assertTrue(any("未対応要素" in w for w in res["warnings"]))

    def test_google_drive_and_external_auth_apps_extracted_safely(self):
        """Google Drive / Docs / OneDrive / SharePoint / Dropbox(.pdf含む) / Box / Miro / Trello / GitHub / Slack が安全に処理されること。"""
        body = """
        <figure id="gd-1">
          <div class="source"><a href="https://drive.google.com/file/d/abc123/view">Project Spec (Google Drive)</a></div>
        </figure>
        <figure id="gdoc-2">
          <div class="source"><a href="https://docs.google.com/spreadsheets/d/xyz789/edit">Budget Sheet</a></div>
        </figure>
        <figure id="od-3">
          <div class="source"><a href="https://onedrive.live.com/view.aspx?resid=123">OneDrive Doc</a></div>
        </figure>
        <figure id="sp-4">
          <div class="source"><a href="https://contoso.sharepoint.com/sites/team/doc.docx">SharePoint File</a></div>
        </figure>
        <figure id="dbx-5">
          <div class="source"><a href="https://www.dropbox.com/s/secret/manual.pdf?dl=0">Dropbox PDF</a></div>
        </figure>
        <figure id="box-6">
          <div class="source"><a href="https://app.box.com/s/999888">Box Folder</a></div>
        </figure>
        <figure id="miro-7">
          <div class="source"><a href="https://miro.com/app/board/uXjV123=/">Miro Board</a></div>
        </figure>
        <figure id="trello-8">
          <div class="source"><a href="https://trello.com/b/abcd/sprint-board">Trello Board</a></div>
        </figure>
        <figure id="gh-9">
          <div class="source"><a href="https://github.com/org/repo/issues/42">GitHub Issue</a></div>
        </figure>
        <figure id="slack-10">
          <div class="source"><a href="https://workspace.slack.com/archives/C123/p456">Slack Thread</a></div>
        </figure>
        """
        tree = self._extract_from_html(body)
        self.assertEqual(len(tree.blocks), 10)

        expected_services = [
            ("google_drive", "Google Drive"),
            ("google_drive", "Google Docs"),
            ("embed", "OneDrive"),
            ("embed", "SharePoint"),
            ("embed", "Dropbox"),
            ("embed", "Box"),
            ("miro", "Miro"),
            ("trello", "Trello"),
            ("embed", "GitHub"),
            ("embed", "Slack"),
        ]
        for blk, (exp_type, exp_svc) in zip(tree.blocks, expected_services):
            self.assertEqual(blk["type"], exp_type)
            self.assertEqual(blk.get("service_name"), exp_svc)
            self.assertTrue(blk.get("_is_unsupported_external_embed"))
            self.assertFalse(blk.get("_attachment_uploaded", True))

        # Dropbox の .pdf URL も PDF 添付として誤ダウンロードされないこと
        with patch("src.importer.ConfluenceClient") as mock_cli_cls, \
             patch("src.importer.requests.get") as mock_requests_get:
            mock_cli = mock_cli_cls.return_value
            mock_cli.base_url = "https://example.atlassian.net/wiki"
            importer = ConfluenceImporter("https://example.atlassian.net/wiki", "u", "t", "SPACE")
            importer._process_attachments(tree.blocks, "page-200")
            mock_requests_get.assert_not_called()
            mock_cli.upload_attachment.assert_not_called()

        converter = NotionToConfluenceConverter()
        storage_xml = converter.blocks_to_storage_format(tree.blocks)
        assert_valid_storage_xml(self, storage_xml)
        self.assertEqual(storage_xml.count('<ac:structured-macro ac:name="note">'), 10)
        self.assertIn("https://drive.google.com/file/d/abc123/view", storage_xml)
        self.assertIn("https://www.dropbox.com/s/secret/manual.pdf?dl=0", storage_xml)
        self.assertNotIn('ac:name="viewpdf"', storage_xml)

        # Telemetry にもサービス名・種別がサニタイズ記録され、URLやページタイトルは漏洩しないこと
        telemetry = SanitizedTelemetry()
        telemetry.record_tree(tree)
        report = telemetry.generate_report()
        report_json = json.dumps(report, ensure_ascii=False)
        self.assertIn("google_drive:Google Drive", report["diagnostics"]["unsupported_elements_detected"])
        self.assertIn("embed:Dropbox", report["diagnostics"]["unsupported_elements_detected"])
        self.assertNotIn("drive.google.com/file/d/abc123", report_json)
        self.assertNotIn("External Embed Test Page", report_json)
        self.assertEqual(report["warnings_count"], 10)

    def test_standalone_and_nested_iframe_embed_object_handling(self):
        """独立した <iframe>/<embed>/<object>（カラム・トグル内ネスト含む）が抽出・変換・検証されること。"""
        body = """
        <iframe id="if-gd" src="https://drive.google.com/embeddedfolderview?id=123"></iframe>
        <div class="column-list">
          <div class="column">
            <embed id="emb-broken" src="https://app.notion.comundefined" />
          </div>
          <div class="column">
            <object id="obj-empty" data=""></object>
          </div>
        </div>
        <details>
          <summary>埋め込み詳細</summary>
          <div class="indented">
            <iframe id="if-figma" src="https://www.figma.com/file/abc/Design"></iframe>
          </div>
        </details>
        """
        tree = self._extract_from_html(body)
        self.assertEqual(len(tree.blocks), 3)  # iframe, column_list, toggle
        self.assertEqual(tree.blocks[0]["type"], "google_drive")
        self.assertTrue(tree.blocks[0]["_is_unsupported_external_embed"])

        col_list = tree.blocks[1]
        col1_blk = col_list["children_blocks"][0]["children_blocks"][0]
        col2_blk = col_list["children_blocks"][1]["children_blocks"][0]
        self.assertTrue(col1_blk["_is_unsupported_external_embed"])
        self.assertTrue(col2_blk["_is_unsupported_external_embed"])

        toggle_blk = tree.blocks[2]
        figma_blk = toggle_blk["children_blocks"][0]
        # figma.com は既存どおり embed スマートリンクとして維持されること
        self.assertEqual(figma_blk["type"], "embed")
        self.assertFalse(figma_blk.get("_is_unsupported_external_embed", False))

        converter = NotionToConfluenceConverter()
        storage_xml = converter.blocks_to_storage_format(tree.blocks)
        assert_valid_storage_xml(self, storage_xml)
        self.assertIn("https://www.figma.com/file/abc/Design", storage_xml)
        self.assertIn('data-card-appearance="embed"', storage_xml)
        self.assertNotIn("app.notion.comundefined", storage_xml)
        self.assertEqual(storage_xml.count('<ac:structured-macro ac:name="note">'), 3)

        # Verifier がカラム内ネストの未対応要素を検知して WARN になること
        mock_vclient = MagicMock()
        mock_vclient.get_page_attachments.return_value = []
        mock_vclient.get_page_by_id.return_value = {
            "id": "page-nested",
            "title": tree.title,
            "body": {"storage": {"value": storage_xml}, "view": {"value": storage_xml}},
        }
        res = MigrationVerifier(mock_vclient).verify_page_node(tree, "page-nested")
        self.assertEqual(res["status"], "WARN")
        self.assertEqual(res["issues"], [])
        self.assertTrue(any("3 件" in w for w in res["warnings"]))

    def test_preserves_figma_maps_codepen_and_local_pdf_attachments(self):
        """回帰確認: figma.com / maps.google / codepen.io は embed 維持、ローカル添付PDF(TCK_00011)も壊れないこと。"""
        body = """
        <figure id="fig-1"><div class="source"><a href="https://www.figma.com/file/111/UI">Figma</a></div></figure>
        <figure id="map-1"><div class="source"><a href="https://maps.google.com/?q=Tokyo">Google Maps</a></div></figure>
        <figure id="cp-1"><div class="source"><a href="https://codepen.io/pen/abc">CodePen</a></div></figure>
        <figure id="pdf-file-1"><div class="source"><a href="doc_preview_off.pdf" data-notion-file="doc_preview_off.pdf">doc_preview_off.pdf</a></div></figure>
        <figure id="pdf-embed-1"><div class="source"><a href="embed_doc.pdf">attachment:0000:embed_doc.pdf</a></div></figure>
        """
        tree = self._extract_from_html(
            body,
            extra_files={
                "doc_preview_off.pdf": b"%PDF-1.4 file",
                "embed_doc.pdf": b"%PDF-1.4 embed",
            },
        )
        self.assertEqual(len(tree.blocks), 5)
        self.assertEqual([b["type"] for b in tree.blocks], ["embed", "embed", "embed", "file", "pdf"])
        for emb in tree.blocks[:3]:
            self.assertFalse(emb.get("_is_unsupported_external_embed", False))

        converter = NotionToConfluenceConverter()
        storage_xml = converter.blocks_to_storage_format(tree.blocks)
        assert_valid_storage_xml(self, storage_xml)
        self.assertEqual(storage_xml.count('data-card-appearance="embed"'), 3)
        self.assertNotIn('<ac:structured-macro ac:name="note">', storage_xml)
        self.assertIn('<ac:link><ri:attachment ri:filename="doc_preview_off.pdf" />', storage_xml)
        self.assertIn('<ac:structured-macro ac:name="viewpdf">', storage_xml)


class TestNotionApiExternalAppFallback(unittest.TestCase):
    """Notion API 抽出 (NotionExtractor) 経由の外部アプリ・破損URLブロック処理テスト。"""

    def test_notion_extractor_normalizes_external_app_and_broken_blocks(self):
        with patch("src.extractor.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            extractor = NotionExtractor(token="ntn_dummy")

        api_blocks = [
            {
                "id": "b-gd",
                "type": "google_drive",
                "has_children": False,
                "google_drive": {"url": "https://drive.google.com/file/d/999/view", "title": "Design Doc"},
            },
            {
                "id": "b-unsup",
                "type": "unsupported",
                "has_children": False,
                "unsupported": {},
            },
            {
                "id": "b-lp-empty",
                "type": "link_preview",
                "has_children": False,
                "link_preview": {"url": ""},
            },
            {
                "id": "b-lp-github",
                "type": "link_preview",
                "has_children": False,
                "link_preview": {"url": "https://github.com/org/repo/pull/10"},
            },
            {
                "id": "b-emb-empty",
                "type": "embed",
                "has_children": False,
                "embed": {"url": "https://app.notion.comundefined", "caption": []},
            },
            {
                "id": "b-pdf-empty",
                "type": "pdf",
                "has_children": False,
                "pdf": {"type": "external", "external": {"url": ""}, "caption": []},
            },
            {
                "id": "b-file-od",
                "type": "file",
                "has_children": False,
                "file": {"type": "external", "external": {"url": "https://onedrive.live.com/edit.aspx?id=1"}, "caption": []},
            },
            {
                "id": "b-figma-ok",
                "type": "embed",
                "has_children": False,
                "embed": {"url": "https://www.figma.com/file/xyz/Mock", "caption": []},
            },
        ]
        mock_client.blocks.children.list.return_value = {"results": api_blocks, "has_more": False}

        extracted, _ = extractor._extract_blocks_and_children("page-api-1")
        self.assertEqual(len(extracted), 8)

        for b in extracted[:7]:
            self.assertTrue(b.get("_is_unsupported_external_embed"), f"Missing unsupported flag for {b}")
            self.assertFalse(b.get("_attachment_uploaded", True))

        # broken URL は空にサニタイズされていること
        self.assertEqual(extracted[4]["embed"]["url"], "")
        # figma は通常の embed として保持
        self.assertFalse(extracted[7].get("_is_unsupported_external_embed", False))

        # Importer は HTTP ダウンロードを試みないこと
        with patch("src.importer.ConfluenceClient") as mock_cli_cls, \
             patch("src.importer.requests.get") as mock_requests_get:
            mock_cli = mock_cli_cls.return_value
            mock_cli.base_url = "https://example.atlassian.net/wiki"
            importer = ConfluenceImporter("https://example.atlassian.net/wiki", "u", "t", "SPACE")
            importer._process_attachments(extracted, "conf-api-1")
            mock_requests_get.assert_not_called()
            mock_cli.upload_attachment.assert_not_called()

        # Converter が XML 整形式で note マクロ + figma embed を出力すること
        converter = NotionToConfluenceConverter()
        storage_xml = converter.blocks_to_storage_format(extracted)
        assert_valid_storage_xml(self, storage_xml)
        self.assertEqual(storage_xml.count('<ac:structured-macro ac:name="note">'), 7)
        self.assertNotIn("app.notion.comundefined", storage_xml)
        self.assertIn("https://drive.google.com/file/d/999/view", storage_xml)
        self.assertIn("https://github.com/org/repo/pull/10", storage_xml)
        self.assertIn("https://www.figma.com/file/xyz/Mock", storage_xml)

        # Verifier が FAIL（attachment link not found 等）を出さず WARN を返すこと
        node = PageNode(id="page-api-1", title="API Embed Test", node_type="page", blocks=extracted)
        mock_vclient = MagicMock()
        mock_vclient.get_page_attachments.return_value = []
        mock_vclient.get_page_by_id.return_value = {
            "id": "conf-api-1",
            "title": "API Embed Test",
            "body": {"storage": {"value": storage_xml}, "view": {"value": storage_xml}},
        }
        res = MigrationVerifier(mock_vclient).verify_page_node(node, "conf-api-1")
        self.assertEqual(res["status"], "WARN")
        self.assertEqual(res["issues"], [])


if __name__ == "__main__":
    unittest.main()
