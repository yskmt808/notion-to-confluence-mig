"""--zip-dir 複数ZIP並行移行におけるクロスZIP DB参照・テンプレート見出し・相互参照テスト。

検証対象:
1. Block A で定義された Database (HTML + CSV + 行ページ) を Block B から参照するケース
   - <figure class="link-to-page"> によるクロスZIP child_database 解決
   - Block B 側の DB 行リレーションから Block A 側の DB 行への参照解決
2. テンプレートとして使われる見出し (h1 / h2 / h3 / トグル見出し / 背景色付き見出し) や
   コールアウト・トグル・コード・テーブル・引用・ToDo 等のページ要素バリエーション
3. Block A ↔ Block B 間のページリンクおよび見出しアンカーリンクによる相互参照
4. `migrate.py --zip-dir ... --dry-run` の CLI エンドツーエンド疎通
"""

import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest.mock import patch

import migrate
from src.converter import NotionToConfluenceConverter
from src.html_extractor import MultiZipExtractor, PageNode
from tests.storage_xml_helper import assert_valid_storage_xml


def _build_html_doc(title: str, body_html: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        f'<head><meta charset="utf-8"><title>{title}</title></head>\n'
        "<body>\n"
        f'  <header><h1 class="page-title">{title}</h1></header>\n'
        f'  <div class="page-body">\n{body_html}\n  </div>\n'
        "</body>\n"
        "</html>\n"
    )


def _write_zip(zip_path: str, entries: dict) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            if isinstance(content, bytes):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content.encode("utf-8"))


def _collect_id_title_map(root: PageNode) -> dict:
    mapping = {}

    def _walk(node: PageNode) -> None:
        mapping[node.id] = node.title
        mapping[node.id.replace("-", "").lower()] = node.title
        for child in node.children:
            _walk(child)

    _walk(root)
    return mapping


def _find_node(root: PageNode, title_substring: str) -> PageNode:
    if title_substring in (root.title or ""):
        return root
    for child in root.children:
        found = _find_node(child, title_substring)
        if found is not None:
            return found
    return None


class TestMultiZipCrossReference(unittest.TestCase):
    # Notion風 32文字hex ID (ハイフン無し)
    BLOCK_A_ID = "aaaa0000000000000000000000000001"
    DB_A_ID = "aaaa0000000000000000000000000010"
    A_ROW_1_ID = "aaaa0000000000000000000000000011"
    A_ROW_2_ID = "aaaa0000000000000000000000000012"
    A_DOC_ID = "aaaa0000000000000000000000000020"
    A_H1_ID = "aaaa0000-0000-0000-0000-000000000101"
    A_H2_ID = "aaaa0000-0000-0000-0000-000000000102"
    A_H3_ID = "aaaa0000-0000-0000-0000-000000000103"

    BLOCK_B_ID = "bbbb0000000000000000000000000001"
    DB_B_ID = "bbbb0000000000000000000000000010"
    B_ROW_1_ID = "bbbb0000000000000000000000000011"
    B_DOC_ID = "bbbb0000000000000000000000000020"
    B_H1_ID = "bbbb0000-0000-0000-0000-000000000201"
    B_H2_ID = "bbbb0000-0000-0000-0000-000000000202"

    def _create_cross_ref_zips(self, zip_dir: str) -> tuple[str, str]:
        """Block A / Block B の相互参照・DB参照・テンプレート見出し入りZIPを作成する。"""
        zip_a_path = os.path.join(zip_dir, "block_a.zip")
        zip_b_path = os.path.join(zip_dir, "block_b.zip")

        db_a_title = "A-DB 基盤プロジェクト管理"
        db_b_title = "B-DB 開発タスク管理"
        a_doc_title = "A-01 テンプレート見出し集"
        b_doc_title = "B-01 開発設計書"

        # --- Block A 側コンテンツ ---
        block_a_root_html = _build_html_doc(
            "Block A 基盤・テンプレート",
            (
                '<aside class="callout" data-notion-callout-icon="🏢">'
                "<p>Block A: マスターDBとテンプレート見出しを提供するブロック</p>"
                "</aside>"
                f'<figure class="link-to-page"><a href="{db_a_title} {self.DB_A_ID}.html">{db_a_title}</a></figure>'
                f'<figure class="link-to-page"><a href="{a_doc_title} {self.A_DOC_ID}.html">{a_doc_title}</a></figure>'
            ),
        )

        db_a_html = _build_html_doc(
            db_a_title,
            "<p>Block A で定義されたマスタープロジェクトデータベースです。</p>",
        )
        db_a_csv = (
            "名前,ステータス,優先度,公開日,完了\n"
            "Project Alpha-01,進行中,高,2026-11-01,No\n"
            "Project Alpha-02,計画中,中,2026-12-01,Yes\n"
        ).encode("utf-8-sig")

        a_row_1_html = _build_html_doc(
            "Project Alpha-01",
            (
                "<p>基盤プロジェクト #1 の詳細ページです。</p>"
                f'<p>関連設計書: <a href="{b_doc_title} {self.B_DOC_ID}.html">{b_doc_title}</a></p>'
            ),
        )
        a_row_2_html = _build_html_doc(
            "Project Alpha-02",
            "<p>基盤プロジェクト #2 の詳細ページです。</p>",
        )

        # テンプレート見出し + 各種ページ要素 + Block B への相互参照
        a_doc_html = _build_html_doc(
            a_doc_title,
            (
                f'<h1 id="{self.A_H1_ID}" class="block-color-blue_background">H1 テンプレート概要</h1>'
                "<p>このセクションは複数ブロックから参照される H1 テンプレート見出しです。</p>"
                f'<h2 id="{self.A_H2_ID}">H2 テンプレートチェックリスト</h2>'
                '<ul class="to-do-list">'
                "<li><div class=\"checkbox checkbox-on\"></div><span class=\"to-do-children-checked\">要件定義レビュー</span></li>"
                "<li><div class=\"checkbox checkbox-off\"></div><span class=\"to-do-children-unchecked\">セキュリティ確認</span></li>"
                "</ul>"
                f'<h3 id="{self.A_H3_ID}">H3 テンプレートAPI仕様</h3>'
                '<pre class="code"><code>GET /api/v1/projects/{id}</code></pre>'
                "<details>"
                "<summary><h2>H2 トグル見出しテンプレート</h2></summary>"
                "<p>トグルを展開すると表示されるテンプレート手順です。</p>"
                "</details>"
                '<aside class="callout" data-notion-callout-icon="📌" data-notion-callout-background="yellow_background">'
                "<p>テンプレート利用時の注意: 各ブロックからアンカーリンクで参照してください。</p>"
                "</aside>"
                '<blockquote class="quote">設計の単一情報源 (SSOT) を維持すること。</blockquote>'
                '<table class="simple-table">'
                "<thead><tr><th>項目</th><th>テンプレート規則</th></tr></thead>"
                "<tbody><tr><td>見出し</td><td>H1〜H3 を用途別に使い分ける</td></tr></tbody>"
                "</table>"
                "<h2>Block B への相互参照</h2>"
                f'<p>開発側の設計書: <a href="{b_doc_title} {self.B_DOC_ID}.html">{b_doc_title}</a></p>'
                f'<p>設計書のテスト観点見出しへ: <a href="{b_doc_title} {self.B_DOC_ID}.html#{self.B_H2_ID}">Block B テスト観点</a></p>'
                '<div dir="auto">'
                '<h2 class="block-color-green_background">【同期】Block B 開発タスクDB・テスト観点</h2>'
                f'<figure class="link-to-page"><a href="{db_b_title} {self.DB_B_ID}.html">{db_b_title}</a></figure>'
                "</div>"
            ),
        )

        _write_zip(
            zip_a_path,
            {
                f"Block A 基盤・テンプレート {self.BLOCK_A_ID}.html": block_a_root_html,
                f"Block A 基盤・テンプレート/{db_a_title} {self.DB_A_ID}.html": db_a_html,
                f"Block A 基盤・テンプレート/{db_a_title} {self.DB_A_ID}.csv": db_a_csv,
                f"Block A 基盤・テンプレート/{db_a_title}/Project Alpha-01 {self.A_ROW_1_ID}.html": a_row_1_html,
                f"Block A 基盤・テンプレート/{db_a_title}/Project Alpha-02 {self.A_ROW_2_ID}.html": a_row_2_html,
                f"Block A 基盤・テンプレート/{a_doc_title} {self.A_DOC_ID}.html": a_doc_html,
            },
        )

        # --- Block B 側コンテンツ ---
        block_b_root_html = _build_html_doc(
            "Block B 開発・参照側",
            (
                '<aside class="callout" data-notion-callout-icon="💻">'
                "<p>Block B: Block A の DB とテンプレート見出しを参照する開発ブロック</p>"
                "</aside>"
                f'<figure class="link-to-page"><a href="{db_b_title} {self.DB_B_ID}.html">{db_b_title}</a></figure>'
                f'<figure class="link-to-page"><a href="{b_doc_title} {self.B_DOC_ID}.html">{b_doc_title}</a></figure>'
            ),
        )

        db_b_html = _build_html_doc(
            db_b_title,
            "<p>Block A のプロジェクトDBとリレーションを持つ開発タスクDBです。</p>",
        )
        db_b_csv = (
            "名前,進捗,プロジェクト参照\n"
            f"DevTask-001,In Progress,Project Alpha-01 ({self.A_ROW_1_ID}.html)\n"
        ).encode("utf-8-sig")

        b_row_1_html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>DevTask-001</title></head>"
            "<body><article class='page'>"
            "<header><h1 class='page-title'>DevTask-001</h1>"
            "<table class='properties'><tbody>"
            "<tr class='property-row-select'><th>進捗</th><td><span class='selected-value'>In Progress</span></td></tr>"
            f"<tr class='property-row-relation'><th>プロジェクト参照</th><td>"
            f"<a href='../{db_a_title}/Project Alpha-01 {self.A_ROW_1_ID}.html'>Project Alpha-01</a>"
            "</td></tr>"
            "</tbody></table></header>"
            "<div class='page-body'><p>Block A の Project Alpha-01 に紐づくタスクです。</p></div>"
            "</article></body></html>"
        )

        # Block A の DB・テンプレート見出しへの参照 + 自身の見出しバリエーション
        b_doc_html = _build_html_doc(
            b_doc_title,
            (
                f'<h1 id="{self.B_H1_ID}">B-01 システム設計概要</h1>'
                "<p>Block A で定義されているマスターDB・テンプレート見出しを同期ブロックとして埋め込みます:</p>"
                '<div dir="auto">'
                '<h1 class="block-color-blue_background">【同期】H1 テンプレート見出し</h1>'
                f'<figure class="link-to-page"><a href="{db_a_title} {self.DB_A_ID}.html">{db_a_title}</a></figure>'
                '<aside class="callout" data-notion-callout-icon="🔄"><p>同期ブロック内コールアウト</p></aside>'
                "</div>"
                f'<p>マスターDB行への直接リンク: <a href="Project Alpha-01 {self.A_ROW_1_ID}.html">Project Alpha-01</a></p>'
                f'<h2 id="{self.B_H2_ID}" class="block-color-green_background">Block B テスト観点テンプレート</h2>'
                "<p>Block A のテンプレート見出しへのアンカー相互参照:</p>"
                "<ul>"
                f'<li><a href="{a_doc_title} {self.A_DOC_ID}.html#{self.A_H1_ID}">A-01 H1テンプレート概要</a></li>'
                f'<li><a href="{a_doc_title} {self.A_DOC_ID}.html#{self.A_H2_ID}">A-01 H2チェックリスト</a></li>'
                f'<li><a href="{a_doc_title} {self.A_DOC_ID}.html#{self.A_H3_ID}">A-01 H3 API仕様</a></li>'
                "</ul>"
                "<details>"
                "<summary><h3>H3 補足設計トグル</h3></summary>"
                f'<p>テンプレート元ページ: <a href="{a_doc_title} {self.A_DOC_ID}.html">{a_doc_title}</a></p>'
                "</details>"
            ),
        )

        _write_zip(
            zip_b_path,
            {
                f"Block B 開発・参照側 {self.BLOCK_B_ID}.html": block_b_root_html,
                f"Block B 開発・参照側/{db_b_title} {self.DB_B_ID}.html": db_b_html,
                f"Block B 開発・参照側/{db_b_title} {self.DB_B_ID}.csv": db_b_csv,
                f"Block B 開発・参照側/{db_b_title}/DevTask-001 {self.B_ROW_1_ID}.html": b_row_1_html,
                f"Block B 開発・参照側/{b_doc_title} {self.B_DOC_ID}.html": b_doc_html,
            },
        )

        return zip_a_path, zip_b_path

    def test_block_b_references_block_a_database_and_relations(self):
        """Block A の DB 定義を Block B 側のページリンクと DB リレーションで参照できること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_a, zip_b = self._create_cross_ref_zips(tmpdir)
            root = MultiZipExtractor([zip_a, zip_b]).extract_tree()

            self.assertEqual(root.id, "multi_block_root_page")
            self.assertEqual([c.title for c in root.children], [
                "Block A 基盤・テンプレート",
                "Block B 開発・参照側",
            ])

            # Block A 側の実DB・行の構築確認
            db_a = _find_node(root, "A-DB 基盤プロジェクト管理")
            self.assertIsNotNone(db_a)
            self.assertEqual(db_a.node_type, "database")
            self.assertEqual([r.title for r in db_a.children], ["Project Alpha-01", "Project Alpha-02"])

            # Block B 側のページが Block A の DB を child_database として解決していること
            b_doc = _find_node(root, "B-01 開発設計書")
            self.assertIsNotNone(b_doc)
            child_db_blocks = [b for b in b_doc.blocks if b.get("type") == "child_database"]
            self.assertEqual(len(child_db_blocks), 1)
            self.assertEqual(child_db_blocks[0]["database_title"], "A-DB 基盤プロジェクト管理")
            self.assertEqual(
                child_db_blocks[0]["id"].replace("-", "").lower(),
                self.DB_A_ID,
            )

            # Block B の DB 行リレーションが Block A の行 ID を保持していること
            b_row_1 = _find_node(root, "DevTask-001")
            self.assertIsNotNone(b_row_1)
            rel_prop = b_row_1.properties.get("プロジェクト参照", {})
            self.assertEqual(rel_prop.get("type"), "relation")
            rel_ids = [
                item.get("id", "").replace("-", "").lower()
                for item in (rel_prop.get("relations") or rel_prop.get("relation") or [])
            ]
            self.assertIn(self.A_ROW_1_ID, rel_ids)

            # Storage Format 変換で統合ツリー全体の id_to_title_map を使ってリンク解決できること
            converter = NotionToConfluenceConverter(id_to_title_map=_collect_id_title_map(root))
            b_doc_xml = converter.generate_page_content(b_doc)
            assert_valid_storage_xml(self, b_doc_xml, label="B-01 開発設計書")
            self.assertIn('ri:content-title="A-DB 基盤プロジェクト管理"', b_doc_xml)
            self.assertIn('ri:content-title="Project Alpha-01"', b_doc_xml)

            b_row_xml = converter.generate_page_content(b_row_1)
            assert_valid_storage_xml(self, b_row_xml, label="DevTask-001")
            self.assertIn('ri:content-title="Project Alpha-01"', b_row_xml)

    def test_mutual_cross_references_and_template_heading_variations(self):
        """Block A ↔ Block B の相互参照とテンプレート見出し・ページ要素バリエーションが正常に変換されること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_a, zip_b = self._create_cross_ref_zips(tmpdir)
            root = MultiZipExtractor([zip_a, zip_b]).extract_tree()

            a_doc = _find_node(root, "A-01 テンプレート見出し集")
            b_doc = _find_node(root, "B-01 開発設計書")
            self.assertIsNotNone(a_doc)
            self.assertIsNotNone(b_doc)

            # A-01 のブロック種別バリエーションを検証
            a_block_types = [b.get("type") for b in a_doc.blocks]
            for expected_type in (
                "heading_1",
                "heading_2",
                "heading_3",
                "to_do",
                "code",
                "toggle",
                "callout",
                "quote",
                "table",
                "child_database",
            ):
                self.assertIn(expected_type, a_block_types, f"Missing block type: {expected_type}")

            # 相互同期ブロック (<div dir="auto">) 経由の要素が _via_synced_block でマーキングされていること
            self.assertTrue(any(b.get("_via_synced_block") for b in a_doc.blocks))
            self.assertTrue(any(b.get("_via_synced_block") for b in b_doc.blocks))

            # 相互参照 Storage Format XML の生成と構文検証
            converter = NotionToConfluenceConverter(id_to_title_map=_collect_id_title_map(root))
            a_xml = converter.generate_page_content(a_doc)
            b_xml = converter.generate_page_content(b_doc)

            assert_valid_storage_xml(self, a_xml, label="A-01 テンプレート見出し集")
            assert_valid_storage_xml(self, b_xml, label="B-01 開発設計書")

            # Block A -> Block B の相互参照リンク (ページ・見出しアンカー・B-DB)
            self.assertIn('ri:content-title="B-01 開発設計書"', a_xml)
            self.assertIn('ri:content-title="B-DB 開発タスク管理"', a_xml)
            self.assertIn(f'ac:anchor="{self.B_H2_ID}"', a_xml)

            # Block B -> Block A の相互参照リンク (ページ・H1/H2/H3見出しアンカー・A-DB)
            self.assertIn('ri:content-title="A-01 テンプレート見出し集"', b_xml)
            self.assertIn('ri:content-title="A-DB 基盤プロジェクト管理"', b_xml)
            self.assertIn(f'ac:anchor="{self.A_H1_ID}"', b_xml)
            self.assertIn(f'ac:anchor="{self.A_H2_ID}"', b_xml)
            self.assertIn(f'ac:anchor="{self.A_H3_ID}"', b_xml)

    def test_zip_dir_cli_dry_run_multiblock_cross_reference(self):
        """--zip-dir + --dry-run で相互参照を含む複数ZIPがエラーなく完走すること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_dir = os.path.join(tmpdir, "zips")
            os.makedirs(zip_dir)
            self._create_cross_ref_zips(zip_dir)
            cp_path = os.path.join(tmpdir, "checkpoint.json")
            orig_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                buf = io.StringIO()
                with patch("sys.argv", [
                    "migrate.py",
                    "--zip-dir", zip_dir,
                    "--dry-run",
                    "--no-dashboard",
                    "--checkpoint", cp_path,
                ]), redirect_stdout(buf):
                    migrate.main()
            finally:
                os.chdir(orig_cwd)

            out = buf.getvalue()
            self.assertIn("DRY RUN モード", out)
            self.assertIn("ドライラン結果サマリー: WARNING 0 件", out)
            self.assertIn("Block A 基盤・テンプレート", out)
            self.assertIn("Block B 開発・参照側", out)
            self.assertIn("A-DB 基盤プロジェクト管理", out)
            self.assertIn("B-DB 開発タスク管理", out)
            self.assertIn("A-01 テンプレート見出し集", out)
            self.assertIn("B-01 開発設計書", out)


if __name__ == "__main__":
    unittest.main()
