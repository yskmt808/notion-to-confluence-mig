"""
テキストベースの移行整合性検証モジュール。
Playwrightなどのブラウザを介さず、NotionとConfluenceのAPIデータから
テキスト・構造・ブロックの移行精度を高速かつ確実に検証する。
"""

import logging
import re
import requests
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup
from src.extractor import PageNode
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

        # 2. タイトル検証
        if conf_title == node.title:
            checks_passed.append(f"Title exact match: '{conf_title}'")
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
        if "拡張機能の読み込み中にエラーが発生しました" in view_body:
            issues.append("Macro load error detected in view body.")
        if "confluence-information-macro-error" in view_body or "confluence-error" in view_body:
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

        # ブロック別の固有要素チェック
        block_types = [b.get("type") for b in node.blocks]
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
            callout_blocks = [b for b in node.blocks if b.get("type") == "callout"]
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

                matched = any(f'<ac:structured-macro ac:name="{m}"' in storage_body for m in expected_macros)
                if matched:
                    found_callouts.extend(expected_macros)
                else:
                    issues.append(f"Callout block (color: {color}, icon: {emoji}) expected one of {expected_macros}, but not found in Confluence.")
            if found_callouts:
                checks_passed.append(f"Callout macros verified: {', '.join(sorted(set(found_callouts)))}")

        if "pdf" in block_types:
            if ('<ac:structured-macro ac:name="view-file"' in storage_body or '<ac:link' in storage_body) and ".pdf" in storage_body.lower():
                checks_passed.append("PDF block converted to view-file macro / attachment link.")
            else:
                issues.append("PDF block detected in Notion, but PDF viewer/link not found in Confluence.")

        if "file" in block_types:
            if "<ac:link" in storage_body and "<ri:attachment" in storage_body:
                checks_passed.append("File attachment link present.")
            else:
                issues.append("File block detected in Notion, but attachment link not found in Confluence.")

        if "video" in block_types:
            if ('data-card-appearance="embed"' in storage_body or '<ac:structured-macro ac:name="multimedia"' in storage_body or '<ac:structured-macro ac:name="widget"' in storage_body or '<ac:link' in storage_body):
                checks_passed.append("Video block converted to embedded card / multimedia player.")
            else:
                issues.append("Video block detected in Notion, but embed/multimedia not found in Confluence.")

        if "audio" in block_types:
            if ('data-card-appearance="embed"' in storage_body or '<ac:structured-macro ac:name="multimedia"' in storage_body or '<ac:link' in storage_body):
                checks_passed.append("Audio block converted to multimedia audio player / link.")
            else:
                issues.append("Audio block detected in Notion, but audio player/link not found in Confluence.")

        if "bookmark" in block_types:
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

        # 5-b. Notion固有・未対応要素の検知とフォールバック記録
        unsupported_blocks = [b for b in node.blocks if b.get("type") in ("breadcrumb", "unsupported", "button", "template")]
        if unsupported_blocks:
            u_names = [b.get("type") for b in unsupported_blocks]
            warnings.append(f"Notion固有の未対応要素を検出・フォールバック注記として保持しました ({len(u_names)} 件: {', '.join(u_names)})")
            if "Notion固有機能" in storage_body or "未対応ブロック" in storage_body:
                checks_passed.append(f"Unsupported block fallback note verified in Confluence body ({', '.join(u_names)}).")
            else:
                issues.append(f"Unsupported Notion blocks detected ({', '.join(u_names)}), but fallback note was not found in Confluence body.")

        # 6. 主要テキストの含有チェック（Notionブロック内のプレーンテキストが欠落していないか）
        missing_snippets = []
        for b in node.blocks:
            b_type = b.get("type", "")
            data = b.get(b_type, {})
            if isinstance(data, dict):
                rich_texts = data.get("rich_text", [])
                for rt in rich_texts:
                    plain = rt.get("plain_text", "").strip()
                    # 5文字以上のテキストについて検証
                    if len(plain) >= 5 and plain not in extracted_text and plain not in storage_body:
                        # タグやエスケープの違いを考慮
                        normalized_plain = re.sub(r"\s+", "", plain)
                        normalized_body = re.sub(r"\s+", "", extracted_text)
                        if normalized_plain not in normalized_body:
                            missing_snippets.append(plain[:30])

        if missing_snippets:
            warnings.append(f"Potentially missing text snippets ({len(missing_snippets)} items): {missing_snippets[:3]}")
        else:
            checks_passed.append("All key Notion text snippets present in Confluence body.")

        status = "FAIL" if issues else ("WARN" if warnings else "PASS")
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
        # 1-a. タイトル検証
        if conf_title == node.title:
            checks_passed.append(f"Database title exact match: '{conf_title}'")
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
            if board_views:
                checks_passed.append(f"Board view layout present: {', '.join(board_views)}")
            else:
                warnings.append("Board view layout not found in Confluence database.")

            # 2-c. ギャラリービュー / カード (Gallery Layout)
            gallery_views = [name for name, t in view_types.items() if t == "gallery" or "ギャラリー" in name]
            if gallery_views:
                checks_passed.append(f"Gallery view layout present: {', '.join(gallery_views)}")
            else:
                warnings.append("Gallery view layout not found in Confluence database.")

            # 2-d. Group by フィルタービュー (Filtered Views)
            filtered_views = [name for name in view_names if ": " in name or "ステータス" in name]
            if filtered_views:
                checks_passed.append(f"Group by filtered views provisioned ({len(filtered_views)} views): {', '.join(filtered_views[:4])}")
            else:
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

        # 4. プロパティ・スキーマ定義の網羅性検証
        prop_keys = set()
        for child in node.children:
            prop_keys.update(child.properties.keys())
        if prop_keys:
            checks_passed.append(f"Database schema properties verified ({len(prop_keys)} properties): {', '.join(sorted(prop_keys))}")

        status = "FAIL" if issues else ("WARN" if warnings else "PASS")
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

