"""MultiZipExtractor の統合抽出テスト (TCK_00004)。

単一ZIP委譲、複数ZIP並行抽出とタイトル順ソート、仮想統合ルート構築、
複数ZIP横断のグローバルDB CSVマップ解決、不正入力時のエラー処理を検証する。
"""

import os
import tempfile
import unittest
import zipfile

from src.html_extractor import MultiZipExtractor


def _write_notion_zip(
    zip_path: str,
    root_html_name: str,
    root_title: str,
    body_html: str = "<p>Body content</p>",
    extra_entries: dict = None,
):
    """テスト用の最小限 Notion HTML エクスポート ZIP を作成する。"""
    root_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{root_title}</title></head>
<body>
  <header><h1 class="page-title">{root_title}</h1></header>
  <div class="page-body">{body_html}</div>
</body>
</html>"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(root_html_name, root_html.encode("utf-8"))
        for entry_name, content in (extra_entries or {}).items():
            if isinstance(content, str):
                zf.writestr(entry_name, content.encode("utf-8"))
            else:
                zf.writestr(entry_name, content)


class TestMultiZipExtractor(unittest.TestCase):
    def test_no_existing_zips_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = os.path.join(tmpdir, "missing.zip")
            with self.assertRaises(FileNotFoundError):
                MultiZipExtractor([missing])

    def test_single_zip_delegates_directly_without_virtual_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            z1 = os.path.join(tmpdir, "part1.zip")
            _write_notion_zip(
                z1,
                "Solo Root 11111111111111111111111111111111.html",
                "Solo Root",
            )

            extractor = MultiZipExtractor([z1])
            self.assertEqual(extractor.zip_paths, [z1])

            root = extractor.extract_tree()
            self.assertEqual(root.title, "Solo Root")
            self.assertNotEqual(root.id, "multi_block_root_page")

    def test_missing_or_corrupted_zip_raises_error_instead_of_ignoring(self):
        """TCK_00015: 存在しないZIP・破損ZIP・ディレクトリ混在時は無視せず明示的に例外送出すること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            z1 = os.path.join(tmpdir, "part1.zip")
            _write_notion_zip(
                z1,
                "Solo Root 11111111111111111111111111111111.html",
                "Solo Root",
            )
            missing = os.path.join(tmpdir, "does_not_exist.zip")
            corrupted = os.path.join(tmpdir, "corrupted.zip")
            with open(corrupted, "wb") as f:
                f.write(b"this is not a valid zip archive")

            with self.assertRaises(FileNotFoundError):
                MultiZipExtractor([z1, missing])

            with self.assertRaises(zipfile.BadZipFile):
                MultiZipExtractor([corrupted])

            with self.assertRaises(zipfile.BadZipFile):
                MultiZipExtractor([z1, corrupted])

            with self.assertRaises(IsADirectoryError):
                MultiZipExtractor([tmpdir])

    def test_multiple_zips_build_sorted_integrated_tree_and_summary_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            z_b = os.path.join(tmpdir, "zip_b.zip")
            z_a = os.path.join(tmpdir, "zip_a.zip")
            _write_notion_zip(
                z_b,
                "Block B 22222222222222222222222222222222.html",
                "Block B",
            )
            _write_notion_zip(
                z_a,
                "Block A 11111111111111111111111111111111.html",
                "Block A",
            )

            custom_title = "カスタム統合ルート"
            extractor = MultiZipExtractor([z_b, z_a], root_title=custom_title)
            root = extractor.extract_tree()

            self.assertEqual(root.id, "multi_block_root_page")
            self.assertEqual(root.title, custom_title)
            self.assertEqual(root.node_type, "page")
            self.assertEqual(root.icon, "🌐")

            # 入力順 (B, A) に関わらずタイトル昇順 (Block A, Block B) に並ぶこと
            self.assertEqual([c.title for c in root.children], ["Block A", "Block B"])

            # 仮想ルートのブロック構成: callout + heading_2 + 各ブロックへの child_page
            block_types = [b["type"] for b in root.blocks]
            self.assertEqual(block_types, ["callout", "heading_2", "child_page", "child_page"])
            self.assertEqual(
                [b["child_page"]["title"] for b in root.blocks if b["type"] == "child_page"],
                ["Block A", "Block B"],
            )

    def test_cross_zip_global_db_csv_map_resolves_child_database_link(self):
        """別ZIPに定義されたDB CSVへのリンクが child_database として解決されること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_id = "aaaaaaaaaaaaaaaa1111111111111199"
            z_a = os.path.join(tmpdir, "block_a.zip")
            z_b = os.path.join(tmpdir, "block_b.zip")

            # ZIP A: ZIP B 側にある DB へのリンク (<figure class="link-to-page">) を含む
            link_body = (
                f'<figure class="link-to-page">'
                f'<a href="SharedTasks {db_id}.html">SharedTasks</a>'
                f"</figure>"
            )
            _write_notion_zip(
                z_a,
                "Block A 11111111111111111111111111111111.html",
                "Block A",
                body_html=link_body,
            )

            # ZIP B: SharedTasks DB (HTML + CSV) を保持
            db_html = (
                "<!DOCTYPE html><html><head><title>SharedTasks</title></head>"
                "<body><header><h1 class='page-title'>SharedTasks</h1></header></body></html>"
            )
            _write_notion_zip(
                z_b,
                "Block B 22222222222222222222222222222222.html",
                "Block B",
                extra_entries={
                    f"Block B/SharedTasks {db_id}.csv": "名前,状態\nTask 1,Open\n".encode("utf-8-sig"),
                    f"Block B/SharedTasks {db_id}.html": db_html,
                },
            )

            root = MultiZipExtractor([z_a, z_b]).extract_tree()
            block_a = next(c for c in root.children if c.title == "Block A")
            block_b = next(c for c in root.children if c.title == "Block B")

            # ZIP A 側のリンクブロックが global_db_csv_map により child_database と判定されること
            db_link_blocks = [b for b in block_a.blocks if b.get("type") == "child_database"]
            self.assertEqual(len(db_link_blocks), 1)
            self.assertEqual(db_link_blocks[0]["id"], "aaaaaaaa-aaaa-aaaa-1111-111111111199")
            self.assertEqual(db_link_blocks[0]["database_title"], "SharedTasks")

            # ZIP B 側では実DBノードと行が構築されていること
            shared_db = next(c for c in block_b.children if c.title == "SharedTasks")
            self.assertEqual(shared_db.node_type, "database")
            self.assertEqual([r.title for r in shared_db.children], ["Task 1"])


if __name__ == "__main__":
    unittest.main()
