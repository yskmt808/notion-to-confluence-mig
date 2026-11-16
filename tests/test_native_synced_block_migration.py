"""TCK_00024: HTMLエクスポートの同期ブロックをConfluenceネイティブ同期ブロックへ移行するテスト。

検証対象:
1. Notion API 0回で <div dir="auto"> 配下の子ブロックID列から共有グループを検出すること
2. 実機検証済み許可リスト要素のみを bodied-sync-block / sync-block 対象とし、
   混在する child_database (DB埋め込み) 等の非対象要素は従来通り同期ブロック外で維持すること
3. Pass 1 で同期元 bodied-sync-block を作成し、Pass 2 で同期先 sync-block に
   confluence-page/{source_confluence_id}/{resource_id} を埋め込むこと
4. 1箇所のみ出現する <div dir="auto"> は過剰に同期ブロック化しないこと
5. Storage Format XML 整形式検証および MigrationVerifier が正常 PASS すること
"""

import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from src.converter import NotionToConfluenceConverter
from src.extractor import (
    PageNode,
    group_and_tag_native_synced_blocks,
    is_sync_block_allowlisted_block,
)
from src.html_extractor import MultiZipExtractor
from src.importer import ConfluenceClient, ConfluenceImporter
from src.verifier import MigrationVerifier
from tests.storage_xml_helper import assert_valid_storage_xml


def _html_doc(title: str, body_html: str) -> str:
    return (
        "<!DOCTYPE html><html>"
        f"<head><meta charset='utf-8'><title>{title}</title></head>"
        f"<body><header><h1 class='page-title'>{title}</h1></header>"
        f"<div class='page-body'>{body_html}</div></body></html>"
    )


def _write_zip(path: str, entries: dict) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for k, v in entries.items():
            zf.writestr(k, v.encode("utf-8") if isinstance(v, str) else v)


class TestNativeSyncedBlockMigration(unittest.TestCase):
    DB_A_ID = "aaaa0000000000000000000000000010"
    A_DOC_ID = "aaaa0000000000000000000000000020"
    B_DOC_ID = "bbbb0000000000000000000000000020"

    SYNC_H1_ID = "11111111-1111-1111-1111-111111111101"
    SYNC_P1_ID = "11111111-1111-1111-1111-111111111102"
    SYNC_H2_ID = "11111111-1111-1111-1111-111111111103"
    SYNC_TODO_ID = "11111111-1111-1111-1111-111111111104"
    SYNC_CODE_ID = "11111111-1111-1111-1111-111111111105"
    SYNC_CALLOUT_ID = "11111111-1111-1111-1111-111111111106"

    SOLO_P_ID = "99999999-9999-9999-9999-999999999901"

    def test_allowlist_and_mixed_db_synced_block_splitting(self):
        """許可リスト判定と、同期ブロック内にDB要素が混在した際の分割維持を検証"""
        self.assertTrue(is_sync_block_allowlisted_block({
            "type": "heading_1",
            "heading_1": {"rich_text": []},
        }))
        self.assertTrue(is_sync_block_allowlisted_block({
            "type": "callout",
            "callout": {"rich_text": []},
            "children_blocks": [{"type": "paragraph", "paragraph": {"rich_text": []}}],
        }))
        # DB 要素および未展開外部埋め込みは非許可
        self.assertFalse(is_sync_block_allowlisted_block({
            "type": "child_database",
            "id": "db1",
        }))
        self.assertFalse(is_sync_block_allowlisted_block({
            "type": "callout",
            "callout": {"rich_text": []},
            "children_blocks": [{"type": "child_database", "id": "db1"}],
        }))
        self.assertFalse(is_sync_block_allowlisted_block({
            "type": "embed",
            "_is_unsupported_external_embed": True,
        }))

        shared_inner_html = (
            f'<div dir="auto">'
            f'<h1 id="{self.SYNC_H1_ID}" class="block-color-blue_background">共有見出しH1</h1>'
            f'<p id="{self.SYNC_P1_ID}">共有本文テキスト</p>'
            f'<figure id="{self.DB_A_ID}" class="link-to-page"><a href="A-DB {self.DB_A_ID}.html">A-DB</a></figure>'
            f'<h2 id="{self.SYNC_H2_ID}">共有セクションH2</h2>'
            f'<ul id="{self.SYNC_TODO_ID}" class="to-do-list"><li><div class="checkbox checkbox-on"></div><span>チェック項目</span></li></ul>'
            f'<pre id="{self.SYNC_CODE_ID}" class="code"><code>GET /health</code></pre>'
            f'<details><summary><h3>共有トグル見出し</h3></summary><p>トグル中身</p></details>'
            f'<aside id="{self.SYNC_CALLOUT_ID}" class="callout block-color-yellow_background" data-notion-callout-icon="🔄"><p>共有コールアウト</p></aside>'
            f'</div>'
        )
        solo_inner_html = (
            f'<div dir="auto">'
            f'<p id="{self.SOLO_P_ID}">単独ページのみに存在する同期ブロック</p>'
            f'</div>'
        )
        db_only_synced_html = (
            f'<div dir="auto">'
            f'<div class="collection-content" id="41a7be77-0000-0000-0000-000000000099">'
            f'<h4 class="collection-title">A-DB</h4>'
            f'</div>'
            f'</div>'
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_a = os.path.join(tmpdir, "block_a.zip")
            zip_b = os.path.join(tmpdir, "block_b.zip")

            _write_zip(
                zip_a,
                {
                    "Block A aaaa0000000000000000000000000001.html": _html_doc(
                        "Block A",
                        f'<figure class="link-to-page"><a href="A-DB {self.DB_A_ID}.html">A-DB</a></figure>'
                        f'<figure class="link-to-page"><a href="A-01 {self.A_DOC_ID}.html">A-01</a></figure>',
                    ),
                    f"Block A/A-DB {self.DB_A_ID}.html": _html_doc("A-DB", "<p>Master DB</p>"),
                    f"Block A/A-DB {self.DB_A_ID}.csv": "名前,メモ\nRow1,v1\n".encode("utf-8-sig"),
                    f"Block A/A-DB/Row1 aaaa0000000000000000000000000011.html": _html_doc("Row1", "<p>Row body</p>"),
                    f"Block A/A-01 {self.A_DOC_ID}.html": _html_doc("A-01", shared_inner_html + solo_inner_html),
                },
            )
            _write_zip(
                zip_b,
                {
                    "Block B bbbb0000000000000000000000000001.html": _html_doc(
                        "Block B",
                        f'<figure class="link-to-page"><a href="B-01 {self.B_DOC_ID}.html">B-01</a></figure>',
                    ),
                    f"Block B/B-01 {self.B_DOC_ID}.html": _html_doc("B-01", shared_inner_html + db_only_synced_html),
                },
            )

            root = MultiZipExtractor([zip_a, zip_b]).extract_tree()
            registry = group_and_tag_native_synced_blocks(root)

            # 共有されている2つの連続許可ラン ([H1, P1] と [H2..Callout]) がグルーピングされること
            # 単独ページのみの solo_inner_html と DBのみの db_only_synced_html は登録されないこと
            self.assertEqual(len(registry), 2)

            a_doc = next(c for c in root.children[0].children if c.title == "A-01")
            b_doc = next(c for c in root.children[1].children if c.title == "B-01")

            a_sync_blocks = [b for b in a_doc.blocks if b.get("type") == "synced_block"]
            b_sync_blocks = [b for b in b_doc.blocks if b.get("type") == "synced_block"]
            self.assertEqual(len(a_sync_blocks), 2)
            self.assertEqual(len(b_sync_blocks), 2)
            self.assertTrue(all(b["_native_sync"]["role"] == "original" for b in a_sync_blocks))
            self.assertTrue(all(b["_native_sync"]["role"] == "reference" for b in b_sync_blocks))

            # child_database (A-DB) は同期ブロックの外に従来形式で残っていること
            a_db_refs = [b for b in a_doc.blocks if b.get("type") == "child_database"]
            b_db_refs = [b for b in b_doc.blocks if b.get("type") == "child_database"]
            self.assertEqual(len(a_db_refs), 1)
            self.assertEqual(len(b_db_refs), 2)  # synced内リンク + db_only_synced_html

            # 単独ブロックの paragraph は synced_block に変換されず通常の paragraph のままであること
            self.assertTrue(
                any(
                    b.get("type") == "paragraph"
                    and b.get("id") == self.SOLO_P_ID
                    for b in a_doc.blocks
                )
            )

    def test_importer_pass1_and_pass2_creates_bodied_and_reference_sync_blocks(self):
        """Pass 1 で bodied-sync-block を作成し、Pass 2 で参照側へ sync-block を埋め込むこと"""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_a = os.path.join(tmpdir, "block_a.zip")
            zip_b = os.path.join(tmpdir, "block_b.zip")

            shared_html = (
                '<div dir="auto">'
                f'<h2 id="{self.SYNC_H2_ID}">共通テンプレート見出し</h2>'
                f'<p id="{self.SYNC_P1_ID}">AとBで同期される本文テキスト</p>'
                f'<aside id="{self.SYNC_CALLOUT_ID}" class="callout" data-notion-callout-icon="💡"><p>同期コールアウト</p></aside>'
                '</div>'
            )

            _write_zip(
                zip_a,
                {
                    "Block A aaaa0000000000000000000000000001.html": _html_doc(
                        "Block A",
                        f'<figure class="link-to-page"><a href="A-01 {self.A_DOC_ID}.html">A-01</a></figure>',
                    ),
                    f"Block A/A-01 {self.A_DOC_ID}.html": _html_doc("A-01", shared_html),
                },
            )
            _write_zip(
                zip_b,
                {
                    "Block B bbbb0000000000000000000000000001.html": _html_doc(
                        "Block B",
                        f'<figure class="link-to-page"><a href="B-01 {self.B_DOC_ID}.html">B-01</a></figure>',
                    ),
                    f"Block B/B-01 {self.B_DOC_ID}.html": _html_doc("B-01", shared_html),
                },
            )

            root = MultiZipExtractor([zip_a, zip_b]).extract_tree()

            importer = ConfluenceImporter(
                url="https://example.atlassian.net",
                username="user",
                token="tok",
                space_key="DOCS",
                page_workers=2,
            )
            mock_client = MagicMock(spec=ConfluenceClient)
            mock_client.base_url = "https://example.atlassian.net/wiki"
            mock_client.auth = ("user", "tok")
            mock_client.get_page_by_title.return_value = None
            mock_client.get_database_by_title.return_value = None
            mock_client.get_space_id.return_value = "sp-1"

            stored_pages = {}
            parents_map = {}

            def fake_create_page(space_key, title, body, parent_id=None, **kwargs):
                pid = f"conf_{len(stored_pages) + 100}"
                stored_pages[pid] = {"id": pid, "title": title, "body": body, "version": 1}
                parents_map[pid] = parent_id
                return {"id": pid, "_links": {"webui": f"/spaces/{space_key}/pages/{pid}"}}

            def fake_update_page(page_id, space_key, title, body, current_version, **kwargs):
                stored_pages[page_id] = {
                    "id": page_id,
                    "title": title,
                    "body": body,
                    "version": current_version + 1,
                }
                return {"id": page_id}

            mock_client.create_page.side_effect = fake_create_page
            mock_client.update_page.side_effect = fake_update_page
            mock_client.get_page_by_id.side_effect = lambda pid, expand=None: {
                "id": pid,
                "title": stored_pages[pid]["title"],
                "version": {"number": stored_pages[pid]["version"]},
                "ancestors": [{"id": parents_map[pid]}] if parents_map.get(pid) else [],
                "body": {"storage": {"value": stored_pages[pid]["body"]}, "view": {"value": stored_pages[pid]["body"]}},
            }
            importer.client = mock_client

            mapping = importer.import_tree(root, parent_id="top-0", overwrite=False)

            a_conf_id = mapping[f"{self.A_DOC_ID[:8]}-{self.A_DOC_ID[8:12]}-{self.A_DOC_ID[12:16]}-{self.A_DOC_ID[16:20]}-{self.A_DOC_ID[20:]}"]["confluence_id"]
            b_conf_id = mapping[f"{self.B_DOC_ID[:8]}-{self.B_DOC_ID[8:12]}-{self.B_DOC_ID[12:16]}-{self.B_DOC_ID[16:20]}-{self.B_DOC_ID[20:]}"]["confluence_id"]

            a_body = stored_pages[a_conf_id]["body"]
            b_body = stored_pages[b_conf_id]["body"]

            assert_valid_storage_xml(self, a_body, label="A-01 Original bodied-sync-block")
            assert_valid_storage_xml(self, b_body, label="B-01 Reference sync-block")

            # A-01 は bodied-sync-block として本文を保持すること
            self.assertIn('<ac:adf-node type="bodied-sync-block">', a_body)
            self.assertIn("共通テンプレート見出し", a_body)
            self.assertIn("AとBで同期される本文テキスト", a_body)

            # B-01 は sync-block として confluence-page/{a_conf_id}/{resource_id} を参照すること
            self.assertIn('<ac:adf-node type="sync-block">', b_body)
            self.assertIn(f"confluence-page/{a_conf_id}/", b_body)

            # Verifier でも A-01 / B-01 ともに誤警告・誤エラーなく PASS すること
            verifier = MigrationVerifier(mock_client)
            page_id_map = {nid: item["confluence_id"] for nid, item in mapping.items()}
            results = verifier.verify_tree(root, page_id_map, "top-0")
            a_res = next(r for r in results if r["title"] == "A-01")
            b_res = next(r for r in results if r["title"] == "B-01")
            self.assertEqual(a_res["status"], "PASS", f"Unexpected A-01 result: {a_res}")
            self.assertEqual(b_res["status"], "PASS", f"Unexpected B-01 result: {b_res}")


if __name__ == "__main__":
    unittest.main()
