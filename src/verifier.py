"""
テキストベースの移行整合性検証モジュール。
Playwrightなどのブラウザを介さず、NotionとConfluenceのAPIデータから
テキスト・構造・ブロックの移行精度を高速かつ確実に検証する。
"""

import logging
import os
import re
import unicodedata
import urllib.parse
import requests
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup
from src.extractor import (
    PageNode,
    get_node_external_synced_blocks,
    is_substanceless_unsupported_block,
    is_unsupported_page_element_block,
)
from src.importer import ConfluenceClient

logger = logging.getLogger(__name__)


class MigrationVerifier:
    def __init__(self, confluence_client: ConfluenceClient):
        self.client = confluence_client

    def verify_page_node(
        self,
        node: PageNode,
        confluence_page_id: str,
        expected_parent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """NotionのPageNodeとConfluenceの実ページをテキストベースで比較・検証する。"""
        issues = []
        warnings = []
        checks_passed = []

        # 1. Confluenceからページ情報を取得 (Storage Format + View Format)
        conf_page = self.client.get_page_by_id(confluence_page_id, expand="version,ancestors,body.storage,body.view")
        if not conf_page:
            return {
                "page_id": confluence_page_id,
                "title": node.title,
                "status": "FAIL",
                "issues": [f"Confluence page {confluence_page_id} not found."],
                "warnings": [],
                "checks_passed": [],
            }

        conf_title = conf_page.get("title", "")
        storage_body = conf_page.get("body", {}).get("storage", {}).get("value", "")
        view_body = conf_page.get("body", {}).get("view", {}).get("value", "")
        ancestors = conf_page.get("ancestors", [])

        # 2. タイトル検証 (240文字制限による切り詰めやサフィックスを考慮)
        expected_title = node.title
        expected_truncated = expected_title[:237] + "..." if len(expected_title) > 240 else expected_title
        if (
            conf_title == expected_title
            or conf_title == expected_truncated
            or (len(conf_title) >= 237 and expected_title.startswith(conf_title.rstrip(".")))
        ):
            checks_passed.append(f"Title match: '{conf_title}'")
        else:
            issues.append(f"Title mismatch: Expected '{node.title}', got '{conf_title}'")

        # 3. 階層（親ページID）検証
        if expected_parent_id:
            actual_parent_id = str(ancestors[-1].get("id")) if ancestors else None
            if actual_parent_id == str(expected_parent_id):
                checks_passed.append(f"Parent hierarchy match (Parent ID: {actual_parent_id})")
            else:
                issues.append(f"Parent hierarchy mismatch: Expected parent {expected_parent_id}, got {actual_parent_id}")

        # 4. マクロエラー・拡張機能エラーの検知 (View HTML解析)
        if (
            "拡張機能の読み込み中にエラーが発生しました" in view_body
            or "レンダリング中にエラーが発生しました" in view_body
            or "Failed to find attachment" in view_body
        ):
            issues.append("Macro load/render error detected in view body.")
        if (
            "confluence-information-macro-error" in view_body
            or "confluence-error" in view_body
            or '<div class="error">' in view_body
        ):
            issues.append("Confluence macro error class detected in view body.")

        # 5. Notionブロックのテキスト網羅性検証
        soup = BeautifulSoup(storage_body, "html.parser")
        extracted_text = soup.get_text()

        # カバー画像の検証
        if getattr(node, "cover", None):
            cover_file = getattr(node, "_cover_filename", None) or f"cover_{node.id.replace('-', '')[:8]}"
            cover_prefix = cover_file.rsplit(".", 1)[0]
            if "<ac:image" in storage_body and cover_prefix in storage_body:
                checks_passed.append(f"Page cover header image present ({cover_prefix}).")
            else:
                issues.append(f"Cover image detected in Notion, but <ac:image> with {cover_prefix} not found in Confluence.")

        # ブロック別の固有要素チェック（カラム・トグル等のネスト配下も含めて再帰収集）
        all_blocks: List[Dict[str, Any]] = []

        def _collect_all_blocks(blocks_list: List[Dict[str, Any]]):
            for b in blocks_list:
                if isinstance(b, dict):
                    all_blocks.append(b)
                    if b.get("children_blocks"):
                        _collect_all_blocks(b["children_blocks"])

        _collect_all_blocks(node.blocks)
        block_types = [b.get("type") for b in all_blocks]
        if "equation" in block_types:
            # 数式添付ファイルが存在するか
            if "<ac:image" in storage_body and "equation_" in storage_body:
                checks_passed.append("LaTeX equation converted to image attachment.")
            else:
                issues.append("Equation block detected in Notion, but <ac:image> not found in Confluence.")

        if "code" in block_types:
            if "<ac:structured-macro ac:name=\"code\"" in storage_body:
                checks_passed.append("Code macro present.")
            else:
                issues.append("Code block detected in Notion, but code macro not found in Confluence.")

        if "table" in block_types:
            if soup.find("table"):
                checks_passed.append("Table element present.")
            else:
                issues.append("Table block detected in Notion, but <table> tag not found in Confluence.")

        if "to_do" in block_types:
            if "<ac:task-list>" in storage_body:
                checks_passed.append("Task list present.")
            else:
                issues.append("To-Do block detected in Notion, but <ac:task-list> not found in Confluence.")

        if "callout" in block_types:
            callout_blocks = [b for b in all_blocks if b.get("type") == "callout"]
            found_callouts = []
            for cb in callout_blocks:
                c_data = cb.get("callout", {})
                color = c_data.get("color", "default").lower()
                icon_obj = c_data.get("icon") or {}
                emoji = icon_obj.get("emoji") if icon_obj.get("type") == "emoji" else None
                warning_icons = {"⚠️", "🚨", "❗", "❕", "🛑", "🔥", "⚡", "⛔"}
                tip_icons = {"💡", "✨", "⭐", "🌟", "🌱", "🔑"}
                note_icons = {"📝", "📌", "📋", "✏️", "🗒️", "📖", "🔖"}
                is_red = any(c in color for c in ("red", "pink")) or emoji in {"🚨", "🛑", "⛔", "❌", "🔥"}
                is_yellow = any(c in color for c in ("yellow", "orange")) or emoji in {"⚠️", "❗", "❕", "⚡"}
                is_green = "green" in color or emoji in {"💡", "✨", "⭐", "🌟", "🌱", "🔑", "✅"}

                if is_red:
                    expected_macros = ["warning"]
                elif is_yellow:
                    expected_macros = ["note", "warning"]
                elif is_green:
                    expected_macros = ["tip"]
                else:
                    expected_macros = ["info", "note"]

                matched = any(f'<ac:structured-macro ac:name="{m}"' in storage_body for m in expected_macros) or '<ac:structured-macro ac:name="panel"' in storage_body
                if matched:
                    found_callouts.extend(expected_macros + (["panel"] if '<ac:structured-macro ac:name="panel"' in storage_body else []))
                else:
                    issues.append(f"Callout block (color: {color}, icon: {emoji}) expected one of {expected_macros} or 'panel', but not found in Confluence.")
            if found_callouts:
                checks_passed.append(f"Callout macros verified: {', '.join(sorted(set(found_callouts)))}")

        # 添付ファイル実体検証: ブロックツリーから添付ファイル名を収集し、Confluenceの添付ファイル一覧と比較
        expected_attachment_filenames: List[str] = []

        for b in all_blocks:
            if b.get("_attachment_uploaded") is False or is_unsupported_page_element_block(b):
                continue
            b_type = b.get("type", "")
            fn = b.get("_attachment_filename")
            if not fn and b_type in ("file", "pdf", "image", "audio", "video"):
                f_info = b.get(b_type, {})
                f_file = f_info.get("file") or {}
                u = f_file.get("url")
                if u and not u.startswith(("http://", "https://")):
                    fn = os.path.basename(urllib.parse.unquote(u))
            if fn:
                fn_nfc = unicodedata.normalize("NFC", str(fn))
                if fn_nfc not in expected_attachment_filenames:
                    expected_attachment_filenames.append(fn_nfc)

        if expected_attachment_filenames:
            conf_attachments = self.client.get_page_attachments(confluence_page_id) if hasattr(self.client, "get_page_attachments") else []
            attached_titles = {att.get("title") for att in conf_attachments if isinstance(att, dict) and att.get("title")}
            attached_titles_nfc = {unicodedata.normalize("NFC", t) for t in attached_titles}
            for exp_fn in expected_attachment_filenames:
                if exp_fn in attached_titles or exp_fn in attached_titles_nfc:
                    checks_passed.append(f"Attachment file '{exp_fn}' verified in Confluence attachments.")
                else:
                    issues.append(f"Attachment file '{exp_fn}' is missing from Confluence page {confluence_page_id} attachments.")

        real_pdf_blocks = [
            b for b in all_blocks
            if b.get("type") == "pdf" and not is_unsupported_page_element_block(b)
        ]
        if real_pdf_blocks:
            attached_pdf_blocks = [b for b in real_pdf_blocks if b.get("_attachment_uploaded", True)]
            external_pdf_blocks = [b for b in real_pdf_blocks if not b.get("_attachment_uploaded", True)]
            if attached_pdf_blocks:
                has_viewpdf = '<ac:structured-macro ac:name="viewpdf"' in storage_body
                if has_viewpdf and ".pdf" in storage_body.lower():
                    checks_passed.append("PDF block converted to viewpdf inline preview macro.")
                else:
                    issues.append("PDF block detected in Notion, but viewpdf inline preview macro not found in Confluence.")
                if view_body and 'data-macro-name="viewpdf"' in view_body:
                    checks_passed.append("PDF viewpdf viewer verified in rendered view body.")
            if external_pdf_blocks:
                if ('<a ' in storage_body or '📄 ' in storage_body) and ".pdf" in storage_body.lower():
                    checks_passed.append("External/fallback PDF link verified in Confluence body.")
                else:
                    issues.append("External PDF block detected in Notion, but PDF link/reference not found in Confluence.")

        real_file_blocks = [
            b for b in all_blocks
            if b.get("type") == "file" and not is_unsupported_page_element_block(b)
        ]
        if real_file_blocks:
            attached_file_blocks = [b for b in real_file_blocks if b.get("_attachment_uploaded", True)]
            external_file_blocks = [b for b in real_file_blocks if not b.get("_attachment_uploaded", True)]
            if attached_file_blocks:
                if "<ac:link" in storage_body and "<ri:attachment" in storage_body:
                    checks_passed.append("File attachment link present.")
                else:
                    issues.append("File block detected in Notion, but attachment link not found in Confluence.")
            if external_file_blocks:
                if "<a " in storage_body or "📎 " in storage_body:
                    checks_passed.append("External file link verified in Confluence body.")
                else:
                    issues.append("External file block detected in Notion, but link not found in Confluence.")

        real_video_blocks = [
            b for b in all_blocks
            if b.get("type") == "video" and not is_unsupported_page_element_block(b)
        ]
        if real_video_blocks:
            if ('data-card-appearance="embed"' in storage_body or '<ac:structured-macro ac:name="multimedia"' in storage_body or '<ac:structured-macro ac:name="widget"' in storage_body or '<ac:link' in storage_body):
                checks_passed.append("Video block converted to embedded card / multimedia player.")
            else:
                issues.append("Video block detected in Notion, but embed/multimedia not found in Confluence.")

        real_audio_blocks = [
            b for b in all_blocks
            if b.get("type") == "audio" and not is_unsupported_page_element_block(b)
        ]
        if real_audio_blocks:
            if ('data-card-appearance="embed"' in storage_body or '<ac:structured-macro ac:name="multimedia"' in storage_body or '<ac:link' in storage_body):
                checks_passed.append("Audio block converted to multimedia audio player / link.")
            else:
                issues.append("Audio block detected in Notion, but audio player/link not found in Confluence.")

        real_bookmark_blocks = [
            b for b in all_blocks
            if b.get("type") == "bookmark" and not is_unsupported_page_element_block(b)
        ]
        if real_bookmark_blocks:
            if 'data-card-appearance="block"' in storage_body:
                checks_passed.append("Bookmark block converted to Confluence smart card link (data-card-appearance='block').")
            else:
                issues.append("Bookmark block detected in Notion, but card appearance not found in Confluence.")

        if "table_of_contents" in block_types:
            if '<ac:structured-macro ac:name="toc"' in storage_body:
                checks_passed.append("Table of Contents converted to Confluence TOC macro.")
            else:
                issues.append("Table of contents block detected in Notion, but TOC macro not found in Confluence.")

        if "column_list" in block_types:
            if '<ac:structured-macro ac:name="section"' in storage_body and '<ac:structured-macro ac:name="column"' in storage_body:
                checks_passed.append("Column list converted to Confluence section / column layout macros.")
            else:
                issues.append("Column list block detected in Notion, but section/column macros not found in Confluence.")

        # 5-b. Notion固有・未対応要素（外部アプリ連携埋め込み・破損URLフォールバック含む）の検知と記録
        unsupported_blocks = [b for b in all_blocks if is_unsupported_page_element_block(b)]
        substanceless_blocks = [b for b in unsupported_blocks if is_substanceless_unsupported_block(b)]
        meaningful_unsupported_blocks = [b for b in unsupported_blocks if not is_substanceless_unsupported_block(b)]

        if substanceless_blocks:
            checks_passed.append(
                f"Substanceless unsupported Notion elements safely skipped ({len(substanceless_blocks)} item(s))."
            )

        if meaningful_unsupported_blocks:
            u_names = []
            for ub in meaningful_unsupported_blocks:
                svc = ub.get("service_name") or ub.get("_unsupported_service_name")
                b_t = ub.get("type", "unsupported")
                u_names.append(f"{b_t} ({svc})" if svc else str(b_t))
            warnings.append(f"Notion固有の未対応要素を検出・フォールバック注記として保持しました ({len(u_names)} 件: {', '.join(u_names)})")
            has_note_macro = '<ac:structured-macro ac:name="note"' in storage_body
            has_fallback_text = (
                "Notion固有機能" in storage_body
                or "未対応ブロック" in storage_body
                or "未対応ページ要素" in storage_body
            )
            if has_note_macro and has_fallback_text:
                checks_passed.append(f"Unsupported block fallback note verified in Confluence body ({', '.join(u_names)}).")
            else:
                issues.append(f"Unsupported Notion blocks detected ({', '.join(u_names)}), but fallback note was not found in Confluence body.")

        # 5-b2. HTMLエクスポート未収録・未展開の外部同期ブロック検知
        ext_synced = get_node_external_synced_blocks(node)
        if ext_synced:
            refs = [item["synced_from"] for item in ext_synced if item.get("synced_from")]
            ref_suffix = f" [同期元: {', '.join(refs[:3])}]" if refs else ""
            warnings.append(
                f"HTMLエクスポート未収録・未展開の外部同期ブロックを検出しました ({len(ext_synced)} 件){ref_suffix}"
            )

        # 5-c. ページ内リンク（アンカー指定または自ページへのリンク）において、カスタムリンク名がページタイトルで上書きされる不正なインラインスマートカードの検知
        def _is_self_page_title(target_t: str, ref_t: str) -> bool:
            if not target_t or not ref_t:
                return False
            if target_t == ref_t:
                return True
            # タイトル重複解決サフィックス " (2)" や切り詰め "..." のみを自ページと判定
            clean_ref = re.sub(r"\s*\(\d+\)$", "", ref_t).rstrip(".")
            if target_t.startswith(clean_ref):
                remainder = target_t[len(clean_ref):]
                if not remainder or remainder.startswith("...") or re.match(r"^\s*\(\d+\)$", remainder):
                    return True
            return False

        soup_storage = BeautifulSoup(storage_body, "html.parser")
        for a_link in soup_storage.find_all(lambda t: t.name in ("ac:link", "link")):
            if a_link.get("ac:card-appearance") == "inline":
                ri_page = a_link.find(lambda t: t.name in ("ri:page", "page"))
                page_title = ri_page.get("ri:content-title", "") if ri_page else ""
                has_anchor = bool(a_link.get("ac:anchor"))
                is_self_page = _is_self_page_title(page_title, conf_title) or _is_self_page_title(page_title, expected_title)
                if has_anchor or is_self_page:
                    link_body = a_link.find(lambda t: t.name in ("ac:link-body", "link-body", "ac:plain-text-link-body"))
                    if link_body:
                        body_txt = link_body.get_text(strip=True)
                        if body_txt and page_title and body_txt != page_title and not body_txt.startswith(("http://", "https://")):
                            issues.append(f"Inline smart card link overwriting custom link text '{body_txt}' with target page title '{page_title}'.")

        # 6. 主要テキストの含有チェック（Notionブロック内のプレーンテキストが欠落していないか）
        missing_snippets = []
        for b in node.blocks:
            b_type = b.get("type", "")
            data = b.get(b_type, {})
            if isinstance(data, dict):
                rich_texts = data.get("rich_text", [])
                for rt in rich_texts:
                    plain = rt.get("plain_text", "").strip()
                    # 2文字以上のテキストについて検証
                    if len(plain) >= 2:
                        normalized_plain = re.sub(r"\s+", "", plain)
                        normalized_extracted = re.sub(r"\s+", "", extracted_text)
                        normalized_storage = re.sub(r"\s+", "", storage_body)
                        if normalized_plain not in normalized_extracted and normalized_plain not in normalized_storage:
                            missing_snippets.append(plain[:30])

        if missing_snippets:
            warnings.append(f"Potentially missing text snippets ({len(missing_snippets)} items): {missing_snippets[:3]}")
        else:
            checks_passed.append("All key Notion text snippets present in Confluence body.")

        status = "FAIL" if issues else ("WARN" if warnings else "PASS")
        # 検証完了後に即時メモリ解放（大規模移行検証時の省メモリ化）
        node.blocks = []
        node.properties = {}
        node.comments = []
        node.external_synced_blocks = []
        return {
            "page_id": confluence_page_id,
            "title": node.title,
            "status": status,
            "issues": issues,
            "warnings": warnings,
            "checks_passed": checks_passed,
        }

    def verify_database_node(
        self,
        node: PageNode,
        confluence_db_id: str,
        expected_parent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """NotionのデータベースノードとConfluenceのデータベース（スキーマ・ビュー・行数）を比較・検証する。"""
        issues = []
        warnings = []
        checks_passed = []

        # 1. Confluence Databases APIからデータベース情報を取得
        url = f"{self.client.base_url}/api/v2/databases/{confluence_db_id}"
        try:
            res = requests.get(url, auth=self.client.auth, timeout=15)
            if not res.ok:
                return {
                    "page_id": confluence_db_id,
                    "title": node.title,
                    "status": "FAIL",
                    "issues": [f"Confluence database {confluence_db_id} not found: HTTP {res.status_code}"],
                    "warnings": [],
                    "checks_passed": [],
                }
            db_data = res.json()
        except Exception as e:
            return {
                "page_id": confluence_db_id,
                "title": node.title,
                "status": "FAIL",
                "issues": [f"Failed to query Confluence database {confluence_db_id}: {e}"],
                "warnings": [],
                "checks_passed": [],
            }

        conf_title = db_data.get("title", "")
        # 1-a. タイトル検証 (240文字制限による切り詰めや重複解決サフィックスを考慮)
        expected_title = node.title
        expected_truncated = expected_title[:237] + "..." if len(expected_title) > 240 else expected_title
        is_untitled_match = (
            (not expected_title.strip() or expected_title in ("Untitled Database", "無題のデータベース", "Untitled"))
            and conf_title in ("Untitled Database", "無題のデータベース", "Untitled", "")
        )
        is_dedup_suffix_match = bool(
            re.sub(r"\s*\(\d+\)$", "", conf_title).strip() == re.sub(r"\s*\(\d+\)$", "", expected_title).strip()
        )
        if (
            conf_title == expected_title
            or conf_title == expected_truncated
            or (len(conf_title) >= 237 and expected_title.startswith(conf_title.rstrip(".")))
            or is_untitled_match
            or is_dedup_suffix_match
        ):
            checks_passed.append(f"Database title match: '{conf_title}'")
        else:
            issues.append(f"Database title mismatch: Expected '{node.title}', got '{conf_title}'")

        # 1-b. 親ページID検証
        if expected_parent_id:
            actual_parent_id = str(db_data.get("parentId"))
            if actual_parent_id == str(expected_parent_id):
                checks_passed.append(f"Database parent match (Parent ID: {actual_parent_id})")
            else:
                issues.append(f"Database parent mismatch: Expected parent {expected_parent_id}, got {actual_parent_id}")

        # 2. ビュー設定の比較検証 (Views Comparison)
        conf_views = self.client.get_database_views(confluence_db_id)
        if not conf_views:
            issues.append(f"No views found in Confluence database {confluence_db_id}.")
        else:
            view_names = [v.get("name", "") for v in conf_views]
            view_types = {v.get("name", ""): v.get("type", "") for v in conf_views}
            checks_passed.append(f"Total Confluence database views provisioned: {len(conf_views)} views")

            # 2-a. テーブルビュー (Table Layout)
            has_table = any(t == "table" or "すべてのデータ" in name for name, t in view_types.items())
            if has_table:
                checks_passed.append("Table view layout present (すべてのデータ).")
            else:
                issues.append("Table view layout missing from Confluence database.")

            # 2-b. ボードビュー / カンバン (Board Layout)
            board_views = [name for name, t in view_types.items() if t == "board" or "ボード" in name]
            has_select_status = any(
                isinstance(p, dict) and p.get("type") in ("select", "status")
                for p in (node.properties.get("schema") or {}).values()
            )
            if board_views:
                checks_passed.append(f"Board view layout present: {', '.join(board_views)}")
            elif has_select_status:
                warnings.append("Board view layout not found in Confluence database.")

            # 2-c. ギャラリービュー / カード (Gallery Layout)
            gallery_views = [name for name, t in view_types.items() if t == "gallery" or "ギャラリー" in name]
            if gallery_views:
                checks_passed.append(f"Gallery view layout present: {', '.join(gallery_views)}")
            elif len(node.children) > 5:
                warnings.append("Gallery view layout not found in Confluence database.")

            # 2-d. Group by フィルタービュー (Filtered Views)
            filtered_views = [name for name in view_names if ": " in name or "ステータス" in name]
            if filtered_views:
                checks_passed.append(f"Group by filtered views provisioned ({len(filtered_views)} views): {', '.join(filtered_views[:4])}")
            elif has_select_status:
                warnings.append("Group by filtered views not found in Confluence database.")

        # 3. エントリ（行レコード）件数・子ページ検証
        expected_rows = len(node.children)
        try:
            actual_entries = self.client.get_database_entries(confluence_db_id, expected_count=expected_rows, timeout_sec=10)
            if len(actual_entries) == expected_rows:
                checks_passed.append(f"Database rows exact count match: {expected_rows} rows.")
            else:
                warnings.append(f"Database rows count mismatch: Expected {expected_rows} rows from Notion, found {len(actual_entries)} in Confluence.")
        except Exception as e:
            warnings.append(f"Could not verify database entries count: {e}")

        # 4. プロパティ・スキーマ定義の網羅性およびフィールド型検証
        prop_keys = set()
        for child in node.children:
            prop_keys.update(child.properties.keys())
        if prop_keys:
            checks_passed.append(f"Database schema properties verified ({len(prop_keys)} properties): {', '.join(sorted(prop_keys))}")

        schema = (node.properties or {}).get("schema") or {}
        if schema and hasattr(self.client, "get_database_fields"):
            try:
                conf_fields = self.client.get_database_fields(confluence_db_id)
                if isinstance(conf_fields, list) and conf_fields:
                    conf_field_types = {
                        (f.get("name") or f.get("title") or "").strip(): (f.get("type") or "").strip().lower()
                        for f in conf_fields
                        if isinstance(f, dict) and (f.get("name") or f.get("title"))
                    }
                    from src.converter import NotionToConfluenceConverter
                    expected_conf_type_map = {
                        "checkbox": {"checkbox"},
                        "date": {"date"},
                        "created_time": {"text"},
                        "last_edited_time": {"text"},
                        "number": {"number"},
                        "url": {"hyperlink", "link", "url"},
                        "select": {"tag", "select"},
                        "status": {"tag", "select"},
                        "multi_select": {"tag", "select"},
                        "text": {"text"},
                        "rich_text": {"text"},
                        "title": {"confluence-page-link", "text", "page-link", "pagelink"},
                    }
                    matched_fields = []
                    for f_name, f_meta in schema.items():
                        if not isinstance(f_meta, dict):
                            continue
                        n_type = (f_meta.get("type") or "").strip().lower()
                        if n_type == "date" and NotionToConfluenceConverter.column_requires_text_for_datetime(node, f_name, n_type):
                            expected_set = {"text"}
                        else:
                            expected_set = expected_conf_type_map.get(n_type)
                        if not expected_set or f_name not in conf_field_types:
                            continue
                        actual_type = conf_field_types[f_name]
                        if actual_type in expected_set:
                            matched_fields.append(f"{f_name}:{actual_type}")
                        else:
                            issues.append(
                                f"Database field '{f_name}' type mismatch: expected {sorted(expected_set)} for Notion '{n_type}', got '{actual_type}'."
                            )
                    if matched_fields:
                        checks_passed.append(
                            f"Database field types verified ({len(matched_fields)} fields): {', '.join(matched_fields)}"
                        )
            except Exception as fe:
                warnings.append(f"Could not verify database field types: {fe}")

        status = "FAIL" if issues else ("WARN" if warnings else "PASS")
        # 検証完了後に即時メモリ解放
        node.blocks = []
        node.properties = {}
        node.comments = []
        node.external_synced_blocks = []
        return {
            "page_id": confluence_db_id,
            "title": node.title,
            "status": status,
            "issues": issues,
            "warnings": warnings,
            "checks_passed": checks_passed,
        }

    def verify_tree(
        self,
        node: PageNode,
        page_id_map: Dict[str, str],
        expected_parent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """ツリー全体（通常ページおよびデータベース）を再帰的に検証する。"""
        results = []
        conf_id = page_id_map.get(node.id)
        if conf_id:
            if node.node_type == "database":
                res = self.verify_database_node(node, conf_id, expected_parent_id)
            else:
                res = self.verify_page_node(node, conf_id, expected_parent_id)
            results.append(res)
            this_parent_id = conf_id
        else:
            results.append({
                "page_id": None,
                "title": node.title,
                "status": "FAIL",
                "issues": [f"No Confluence page ID mapped for Notion page {node.id}"],
                "warnings": [],
                "checks_passed": [],
            })
            this_parent_id = expected_parent_id

        # 子ページの検証
        for child in node.children:
            results.extend(self.verify_tree(child, page_id_map, this_parent_id))

        return results

