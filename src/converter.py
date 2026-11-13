"""
Notion のブロック・リッチテキスト・データベース情報を
Confluence Storage Format (XHTML) に変換するモジュール。
"""

import os
import re
import html
import logging
import urllib.parse
import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from src.extractor import PageNode

logger = logging.getLogger(__name__)

UUID_PATTERN = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})(?![0-9a-fA-F])"
)


def extract_uuids(text: str) -> List[str]:
    """URLエンコードされた文字列等から安全に32桁UUIDを抽出する。"""
    if not text:
        return []
    unquoted = urllib.parse.unquote(str(text))
    matches = UUID_PATTERN.findall(unquoted)
    if not matches and unquoted != text:
        matches = UUID_PATTERN.findall(str(text))
    return matches


NOTION_TO_CONFLUENCE_COLOR_MAP = {
    "default": "Grey",
    "gray": "Grey",
    "brown": "Grey",
    "orange": "Yellow",
    "yellow": "Yellow",
    "green": "Green",
    "blue": "Blue",
    "purple": "Blue",
    "pink": "Red",
    "red": "Red",
}

NOTION_COLOR_TO_BADGE_STYLE = {
    "default": "background-color: rgb(235,236,240); color: rgb(23,43,77);",
    "gray": "background-color: rgb(235,236,240); color: rgb(23,43,77);",
    "brown": "background-color: rgb(235,220,211); color: rgb(83,63,4);",
    "orange": "background-color: rgb(255,227,128); color: rgb(23,43,77);",
    "yellow": "background-color: rgb(255,240,179); color: rgb(23,43,77);",
    "green": "background-color: rgb(227,252,239); color: rgb(0,102,68);",
    "blue": "background-color: rgb(222,235,255); color: rgb(7,71,166);",
    "purple": "background-color: rgb(234,230,255); color: rgb(64,50,148);",
    "pink": "background-color: rgb(255,205,210); color: rgb(191,38,0);",
    "red": "background-color: rgb(255,235,230); color: rgb(191,38,0);",
}

NOTION_TEXT_COLOR_MAP = {
    "gray": "#787774",
    "brown": "#976d57",
    "orange": "#c25c04",
    "yellow": "#b58700",
    "green": "#2b8457",
    "teal": "#2b8457",
    "blue": "#1f75cb",
    "purple": "#7647c2",
    "pink": "#b83085",
    "red": "#df3937",
}

NOTION_BG_COLOR_MAP = {
    "gray_background": "#ebeced",
    "brown_background": "#e9e5e3",
    "orange_background": "#faece3",
    "yellow_background": "#fbf3db",
    "green_background": "#edf3ec",
    "teal_background": "#edf3ec",
    "blue_background": "#e7f3f8",
    "purple_background": "#f4f0f7",
    "pink_background": "#fbf0f4",
    "red_background": "#fdebec",
}


def _render_badge(text: str, color: str = "default") -> str:
    style = NOTION_COLOR_TO_BADGE_STYLE.get(color, NOTION_COLOR_TO_BADGE_STYLE["default"])
    css = f"{style} padding: 2px 8px; border-radius: 3px; font-weight: 500; font-size: 12px; display: inline-block; margin: 2px 4px 2px 0;"
    return f'<span style="{css}">{html.escape(str(text))}</span>'


DEFAULT_METADATA_TEMPLATE = "📄 Notion元情報 — 作成者: {author} | 🕒 作成日時: {created_time}"


def clean_parameter_text(text: str) -> str:
    """<ac:parameter> タグ内部にHTMLタグが混入してConfluence APIが500エラーになるのを防ぐため、
    HTMLタグを完全に除去し、実体参照エスケープしたプレーンテキストを返す。"""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", "", text)
    return html.escape(clean.strip())


# Notionコールアウト背景色 -> Confluence パネルマクロ用 HEX カラー定義
CALLOUT_PANEL_COLORS = {
    "default": {"bg": "#f1f1ef", "border": "#e3e2e0"},
    "gray": {"bg": "#f1f1ef", "border": "#e3e2e0"},
    "brown": {"bg": "#f4eeee", "border": "#e8dfdf"},
    "orange": {"bg": "#faebdd", "border": "#f4d8be"},
    "yellow": {"bg": "#fbf3db", "border": "#f4e6b5"},
    "green": {"bg": "#edf3ec", "border": "#dbe8d9"},
    "teal": {"bg": "#edf3ec", "border": "#dbe8d9"},
    "blue": {"bg": "#edf5fb", "border": "#dbeaf6"},
    "purple": {"bg": "#f6f3f9", "border": "#eae3f2"},
    "pink": {"bg": "#faf1f5", "border": "#f2dfea"},
    "red": {"bg": "#fdebec", "border": "#f9d3d6"},
}


class NotionToConfluenceConverter:
    MERMAID_KEYWORDS = (
        "graph ", "flowchart ", "sequencediagram", "statediagram", "classdiagram",
        "erdiagram", "gantt", "pie", "gitgraph", "journey", "mindmap", "timeline",
        "quadrantchart", "sankey", "xychart-beta", "block-beta", "architecture-beta"
    )

    @classmethod
    def is_mermaid_code(cls, lang: str, code_str: str) -> Tuple[bool, str]:
        """コードブロックがMermaidダイアグラム定義であるかを判定する。"""
        l_norm = (lang or "").lower().strip()
        c_clean = (code_str or "").strip()
        c_lower = c_clean.lower()
        if l_norm == "mermaid":
            return True, c_clean
        if any(c_lower.startswith(kw) for kw in cls.MERMAID_KEYWORDS):
            return True, c_clean
        return False, c_clean

    def __init__(
        self,
        id_to_title_map: Optional[Dict[str, str]] = None,
        timezone: Optional[Union[str, datetime.tzinfo]] = None,
        user_mapper: Optional[Any] = None,
        metadata_template: Optional[str] = DEFAULT_METADATA_TEMPLATE,
    ):
        self.id_to_title_map = id_to_title_map or {}
        self.migration_map: Dict[str, Dict[str, Any]] = {}
        self.confluence_base_url: str = ""
        self.space_key: str = ""
        self.user_mapper = user_mapper
        self.current_node_id: Optional[str] = None
        self.current_page_title: Optional[str] = None
        self.current_page_headings: Set[str] = set()
        self.current_page_block_anchors: Dict[str, str] = {}
        self.referenced_anchors: Set[str] = set()
        self.inpage_repair_candidates: List[Dict[str, Any]] = []
        self.current_block_id: Optional[str] = None
        self.explicit_link_anchors: Dict[Tuple[str, str], str] = {}
        from src.timezone_util import resolve_timezone
        self.timezone = resolve_timezone(timezone)

        # テンプレート文字列またはファイルパスの解決 (明示的に .txt/.html/.md 等のパス指定時のみ読込)
        if metadata_template and any(metadata_template.endswith(ext) for ext in (".txt", ".html", ".md", ".tpl")) and os.path.isfile(metadata_template):
            try:
                with open(metadata_template, "r", encoding="utf-8") as f:
                    self.metadata_template = f.read().strip()
            except Exception as e:
                logger.warning(f"Could not read metadata template file {metadata_template}: {e}")
                self.metadata_template = metadata_template
        else:
            self.metadata_template = metadata_template

    def _get_attachment_filename(self, block: Dict[str, Any]) -> str:
        """ブロック情報からConfluence用の一意で分かりやすい添付ファイル名を導出する。"""
        if block.get("_attachment_filename"):
            return block["_attachment_filename"]
        b_type = block.get("type", "")
        b_id_short = block.get("id", "").replace("-", "")[:8]
        data = block.get(b_type, {})
        f_obj = data.get("file") or {}
        e_obj = data.get("external") or {}
        url = f_obj.get("url") or e_obj.get("url") or ""

        ext_map = {
            "image": "png",
            "pdf": "pdf",
            "file": "bin",
            "video": "mp4",
            "audio": "mp3",
        }
        default_ext = ext_map.get(b_type, "bin")
        if url:
            parsed_path = urllib.parse.urlparse(url).path
            base = os.path.basename(parsed_path)
            if base and "." in base:
                name, ext = os.path.splitext(urllib.parse.unquote(base))
                clean_ext = ext.lstrip(".").lower()
                if len(clean_ext) <= 5 and clean_ext.isalnum():
                    clean_name = re.sub(r"[^\w\-_.]", "_", name)[:30]
                    return f"{clean_name}_{b_id_short}.{clean_ext}"
        return f"{b_type}_{b_id_short}.{default_ext}"

    def format_property_for_confluence(self, prop: Any) -> str:
        """Notionプロパティ辞書をConfluenceのXHTML表現にフォーマットする。"""
        if prop is None:
            return "-"
        if not isinstance(prop, dict):
            return html.escape(str(prop))

        p_type = prop.get("type")

        if p_type in ("select", "status"):
            val = prop.get("value")
            if not val and isinstance(prop.get(p_type), dict):
                val = prop[p_type].get("name") or prop[p_type].get("value")
            if not val:
                return "-"
            color = prop.get("color", "default")
            return _render_badge(str(val), color)

        elif p_type == "multi_select":
            items = (
                prop.get("items")
                or prop.get("multi_select")
                or (prop.get("value") if isinstance(prop.get("value"), list) else [])
            )
            if not items:
                return "-"
            badges = []
            for item in items:
                if isinstance(item, dict):
                    val = item.get("value") or item.get("name")
                    color = item.get("color", "default")
                else:
                    val = str(item) if item else None
                    color = "default"
                if not val:
                    continue
                badges.append(_render_badge(str(val), color))
            return " ".join(badges) if badges else "-"

        elif p_type == "date":
            start = prop.get("start") or (prop.get("date") or {}).get("start") or prop.get("value")
            end = prop.get("end") or (prop.get("date") or {}).get("end")
            if not start:
                return "-"
            clean_start = str(start).split("T")[0]
            from src.timezone_util import format_iso_datetime
            start_time_str = f" {format_iso_datetime(start, tz=self.timezone, fmt='%H:%M %Z')}" if "T" in str(start) else ""
            if end:
                clean_end = str(end).split("T")[0]
                end_time_str = f" {format_iso_datetime(end, tz=self.timezone, fmt='%H:%M %Z')}" if "T" in str(end) else ""
                return f'<time datetime="{clean_start}" />{start_time_str} 〜 <time datetime="{clean_end}" />{end_time_str}'
            return f'<time datetime="{clean_start}" />{start_time_str}'

        elif p_type == "checkbox":
            val = prop.get("value", False)
            return "☑️ 完了" if val else "⬜ 未完了"

        elif p_type == "url":
            url = prop.get("value")
            if not url:
                return "-"
            return f'<a href="{html.escape(url)}">{html.escape(url)}</a>'

        elif p_type == "people":
            users = prop.get("users", [])
            if not users:
                return "-"
            if self.user_mapper:
                badges = []
                for u in users:
                    u_id = u.get("id")
                    u_name = u.get("name")
                    u_email = u.get("person", {}).get("email") if isinstance(u.get("person"), dict) else None
                    badges.append(self.user_mapper.format_mention(user_id=u_id, email=u_email, name=u_name))
                return "👤 " + ", ".join(badges)
            names = [u.get("name") for u in users if u.get("name")]
            return "👤 " + ", ".join(html.escape(n) for n in names)

        elif p_type == "relation":
            relations = prop.get("relations") or prop.get("relation") or []
            if not relations:
                return "-"
            links = []
            for r in relations:
                target_id = r.get("id", "")
                norm_id = target_id.replace("-", "").lower()
                target_title = None
                target_info = None
                if self.id_to_title_map:
                    target_title = self.id_to_title_map.get(target_id) or self.id_to_title_map.get(norm_id)
                if self.migration_map:
                    target_info = self.migration_map.get(target_id) or self.migration_map.get(norm_id)
                    if not target_info:
                        for mid, minfo in self.migration_map.items():
                            if mid.replace("-", "").lower() == norm_id:
                                target_info = minfo
                                break

                disp_title = (target_info.get("title") if target_info else None) or target_title or r.get("text")
                if target_info and target_info.get("node_type") == "database":
                    db_url = target_info.get("url") or f"{self.confluence_base_url}/database/{target_info.get('confluence_id')}"
                    links.append(f'<a href="{html.escape(db_url)}" data-card-appearance="inline">{html.escape(db_url)}</a>')
                elif disp_title:
                    escaped_title = html.escape(disp_title)
                    links.append(
                        f'<ac:link ac:card-appearance="inline">'
                        f'<ri:page ri:content-title="{escaped_title}" />'
                        f'<ac:link-body>{escaped_title}</ac:link-body>'
                        f'</ac:link>'
                    )
                else:
                    links.append(f'🔗 <em>{target_id[:8]}...</em>')
            return ", ".join(links)

        elif p_type == "rollup":
            r_type = prop.get("rollup_type")
            if r_type == "array":
                items = prop.get("items", [])
                if not items:
                    return "-"
                rendered_items = []
                for it in items:
                    rendered_items.append(self.format_property_for_confluence(it))
                return " ".join(rendered_items)
            elif r_type == "number":
                val = prop.get("value")
                return str(val) if val is not None else "-"
            elif r_type == "date":
                st = prop.get("start")
                return f"📅 {html.escape(st)}" if st else "-"
            else:
                val = prop.get("value")
                return html.escape(str(val)) if val is not None else "-"

        elif p_type in ("title", "rich_text", "text", "number", "email", "phone_number", "formula"):
            val = prop.get("value")
            if val is None:
                return "-"
            s_val = str(val)
            if p_type in ("rich_text", "text") and ("\n" in s_val or "\r" in s_val):
                return html.escape(s_val).replace("\r\n", "<br/>").replace("\n", "<br/>").replace("\r", "<br/>")
            return html.escape(s_val)

        else:
            val = prop.get("value")
            return html.escape(str(val)) if val is not None else "-"

    def convert_rich_text(self, rich_text_list: List[Dict[str, Any]], default_color: str = "default") -> str:
        """Notionのrich_text配列をConfluence Storage FormatのXHTML文字列に変換する。
        メンション（日付、ページ、データベース、ユーザー、リンクプレビュー等）や書式、カラーも完全サポート。"""
        if not rich_text_list:
            return ""

        result = []
        for item in rich_text_list:
            item_type = item.get("type", "text")

            # 1. メンション記法 (mention) の処理
            if item_type == "mention":
                mention_data = item.get("mention", {})
                m_type = mention_data.get("type")

                # 1-a. 日付メンション (<time datetime="YYYY-MM-DD" />)
                if m_type == "date":
                    date_info = mention_data.get("date", {})
                    start_date = date_info.get("start", "")
                    end_date = date_info.get("end")
                    if start_date:
                        date_val = str(start_date).split("T")[0]
                        from src.timezone_util import format_iso_datetime
                        s_time = f" {format_iso_datetime(start_date, tz=self.timezone, fmt='%H:%M %Z')}" if "T" in str(start_date) else ""
                        if end_date:
                            end_val = str(end_date).split("T")[0]
                            e_time = f" {format_iso_datetime(end_date, tz=self.timezone, fmt='%H:%M %Z')}" if "T" in str(end_date) else ""
                            time_html = f'<time datetime="{date_val}" />{s_time} → <time datetime="{end_val}" />{e_time}'
                        else:
                            time_html = f'<time datetime="{date_val}" />{s_time}'
                        result.append(time_html)
                        continue

                # 1-b. ページメンション (<ac:link ac:card-appearance="inline">)
                elif m_type == "page":
                    target_page_id = mention_data.get("page", {}).get("id", "")
                    norm_id = target_page_id.replace("-", "").lower()
                    target_title = None
                    target_info = None
                    if hasattr(self, "migration_map") and self.migration_map:
                        target_info = self.migration_map.get(target_page_id) or self.migration_map.get(norm_id)
                    if target_info and target_info.get("title"):
                        target_title = target_info.get("title")
                    elif self.id_to_title_map:
                        target_title = self.id_to_title_map.get(target_page_id) or self.id_to_title_map.get(norm_id)
                    if not target_title:
                        target_title = item.get("plain_text", "").replace("@", "").strip() or "Page"

                    escaped_title = html.escape(target_title)
                    link_html = (
                        f'<ac:link ac:card-appearance="inline">'
                        f'<ri:page ri:content-title="{escaped_title}" />'
                        f'<ac:link-body>{escaped_title}</ac:link-body>'
                        f'</ac:link>'
                    )
                    result.append(link_html)
                    continue

                # 1-c. データベースメンション (<ac:link ac:card-appearance="inline">)
                elif m_type == "database":
                    target_db_id = mention_data.get("database", {}).get("id", "")
                    norm_id = target_db_id.replace("-", "").lower()
                    target_title = None
                    target_info = None
                    if hasattr(self, "migration_map") and self.migration_map:
                        target_info = self.migration_map.get(target_db_id) or self.migration_map.get(norm_id)
                    if target_info and target_info.get("url"):
                        db_url = target_info.get("url")
                        escaped_url = html.escape(db_url)
                        link_html = f'<a href="{escaped_url}" data-card-appearance="inline">{escaped_url}</a>'
                    else:
                        if target_info and target_info.get("title"):
                            target_title = target_info.get("title")
                        elif self.id_to_title_map:
                            target_title = self.id_to_title_map.get(target_db_id) or self.id_to_title_map.get(norm_id)
                        if not target_title:
                            target_title = item.get("plain_text", "").replace("@", "").strip() or "Database"

                        escaped_title = html.escape(target_title)
                        link_html = (
                            f'<ac:link ac:card-appearance="inline">'
                            f'<ri:page ri:content-title="{escaped_title}" />'
                            f'<ac:link-body>{escaped_title}</ac:link-body>'
                            f'</ac:link>'
                        )
                    result.append(link_html)
                    continue

                # 1-d. ユーザーメンション
                elif m_type == "user":
                    user_info = mention_data.get("user", {})
                    user_id = user_info.get("id")
                    user_name = user_info.get("name") or item.get("plain_text", "")
                    user_email = user_info.get("person", {}).get("email") if isinstance(user_info.get("person"), dict) else None
                    if user_name.startswith("@"):
                        user_name = user_name[1:]
                    if self.user_mapper:
                        mention_html = self.user_mapper.format_mention(user_id=user_id, email=user_email, name=user_name)
                        result.append(mention_html)
                    else:
                        escaped_name = html.escape(user_name if user_name else "ユーザー")
                        result.append(f'<strong>@{escaped_name}</strong>')
                    continue

                # 1-e. リンクプレビューメンション
                elif m_type == "link_preview":
                    lp_obj = mention_data.get("link_preview") or {}
                    url = lp_obj.get("url", "")
                    if url:
                        result.append(f'<a href="{html.escape(url)}" data-card-appearance="inline">{html.escape(url)}</a>')
                        continue

            # 2. インライン数式 (equation) の処理
            elif item_type == "equation":
                expr = item.get("equation", {}).get("expression", "") or item.get("plain_text", "")
                result.append(f'<code>{html.escape(expr)}</code>')
                continue

            # 3. 通常テキスト (text) の処理
            content = item.get("text", {}).get("content", "") or item.get("plain_text", "")
            if not content:
                continue

            escaped = html.escape(content).replace("\n", "<br />")
            annotations = item.get("annotations", {})

            if annotations.get("code"):
                escaped = f"<code>{escaped}</code>"
            if annotations.get("bold"):
                escaped = f"<strong>{escaped}</strong>"
            if annotations.get("italic"):
                escaped = f"<em>{escaped}</em>"
            if annotations.get("underline"):
                escaped = f"<u>{escaped}</u>"
            if annotations.get("strikethrough"):
                escaped = f"<s>{escaped}</s>"

            # テキストカラー / 背景色
            color = annotations.get("color", "default")
            if color == "default" and default_color != "default":
                color = default_color
            if color and color != "default":
                if color == "teal":
                    color = "green"
                elif color == "teal_background":
                    color = "green_background"
                if color in NOTION_TEXT_COLOR_MAP:
                    escaped = f'<span style="color: {NOTION_TEXT_COLOR_MAP[color]};">{escaped}</span>'
                elif color in NOTION_BG_COLOR_MAP:
                    escaped = f'<span style="background-color: {NOTION_BG_COLOR_MAP[color]}; padding: 2px 4px; border-radius: 3px;">{escaped}</span>'

            # リンク
            text_obj = item.get("text") or {}
            link_obj = text_obj.get("link") or {}
            href = link_obj.get("url") if isinstance(link_obj, dict) else None
            if not href and item.get("href"):
                href = item.get("href")
            item_page_id = item.get("page_id")
            if href or item_page_id:
                # Notion内部リンク / クロスブロックリンク（UUID検出）
                target_uuid = None
                target_title = None
                target_info = None
                anchor_name = None

                if href and "#" in href:
                    anchor_part = urllib.parse.unquote(href.split("#", 1)[1].strip())
                    if anchor_part:
                        norm_anchor = anchor_part.replace("-", "").lower()
                        if hasattr(self, "current_page_block_anchors") and (
                            anchor_part in self.current_page_block_anchors
                            or norm_anchor in self.current_page_block_anchors
                        ):
                            anchor_name = self.current_page_block_anchors.get(anchor_part) or self.current_page_block_anchors.get(norm_anchor)
                        else:
                            anchor_name = anchor_part

                candidate_uuids = []
                if item_page_id:
                    candidate_uuids.append(item_page_id.replace("-", "").lower())
                if href:
                    for u in extract_uuids(href):
                        u_norm = u.replace("-", "").lower()
                        if u_norm not in candidate_uuids:
                            candidate_uuids.append(u_norm)

                # 候補UUIDからページまたはDBの情報を探索
                for c_uid in candidate_uuids:
                    for key in (c_uid, f"{c_uid[:8]}-{c_uid[8:12]}-{c_uid[12:16]}-{c_uid[16:20]}-{c_uid[20:]}"):
                        if self.id_to_title_map and key in self.id_to_title_map:
                            target_title = self.id_to_title_map[key]
                        if self.migration_map and key in self.migration_map:
                            target_info = self.migration_map[key]
                        if target_title or target_info:
                            target_uuid = c_uid
                            break
                    if target_title or target_info:
                        break

                # 自ページIDとの直接一致判定 (id_to_title_map未登録時や同一ページ内リンクの確実な解決)
                if not target_title and not target_info and getattr(self, "current_node_id", None):
                    curr_norm = self.current_node_id.replace("-", "").lower()
                    if curr_norm in candidate_uuids:
                        target_uuid = curr_norm
                        target_title = getattr(self, "current_page_title", None)

                # href にアンカー (#) が含まれる場合の自ページ判定
                if not target_title and not target_info and href and "#" in href:
                    base_href = href.split("#", 1)[0].strip()
                    if not base_href or getattr(self, "current_page_title", None):
                        target_uuid = getattr(self, "current_node_id", "").replace("-", "").lower()
                        target_title = getattr(self, "current_page_title", None)

                if target_info and target_info.get("node_type") == "database":
                    db_url = target_info.get("web_url") or target_info.get("url") or f"{self.confluence_base_url}/database/{target_info.get('confluence_id')}"
                    escaped = f'<a href="{html.escape(db_url)}" data-card-appearance="inline">{html.escape(db_url)}</a>'
                elif target_info or target_title or (href and href.startswith("#")):
                    disp_title = (target_info.get("title") if target_info else None) or target_title or getattr(self, "current_page_title", "") or escaped
                    escaped_title = html.escape(disp_title)

                    # 自ページ（現在変換中のページ）へのリンクかどうか判定
                    is_self_page = False
                    if href and href.startswith("#"):
                        is_self_page = True
                    elif target_uuid and getattr(self, "current_node_id", None):
                        norm_target = target_uuid.replace("-", "").lower()
                        norm_current = self.current_node_id.replace("-", "").lower()
                        if norm_target == norm_current:
                            is_self_page = True
                    elif disp_title and getattr(self, "current_page_title", None):
                        curr_t = self.current_page_title.strip()
                        disp_t = disp_title.strip()
                        if curr_t == disp_t:
                            is_self_page = True
                        else:
                            clean_curr = re.sub(r"\s*\(\d+\)$", "", curr_t).rstrip(".")
                            clean_disp = re.sub(r"\s*\(\d+\)$", "", disp_t).rstrip(".")
                            if clean_curr == clean_disp:
                                is_self_page = True

                    raw_plain = (item.get("plain_text") or (item.get("text") or {}).get("content", "")).strip()
                    clean_plain = re.sub(r"<[^>]+>", "", raw_plain).strip()

                    # 明示的なアンカー修復マッピングの適用 (InPageLinkRepairer / Notion API修復連携)
                    if is_self_page and not anchor_name and hasattr(self, "explicit_link_anchors"):
                        key = (getattr(self, "current_node_id", ""), clean_plain)
                        if key in self.explicit_link_anchors:
                            anchor_name = self.explicit_link_anchors[key]
                            if hasattr(self, "referenced_anchors"):
                                self.referenced_anchors.add(anchor_name)

                    # 自ページ内リンクでアンカーが未設定の場合（Notion HTMLエクスポートで#フラグメントが欠落した場合の救済）:
                    # リンクテキストが現在のページの見出しと一致すれば、見出しアンカーを自動補完
                    if is_self_page and not anchor_name and hasattr(self, "current_page_headings") and self.current_page_headings:
                        if clean_plain in self.current_page_headings:
                            anchor_name = clean_plain

                    # 自ページ内リンクで依然としてアンカーが未解決の場合、修復候補リストに記録
                    if is_self_page and not anchor_name:
                        cand = {
                            "page_id": getattr(self, "current_node_id", ""),
                            "page_title": getattr(self, "current_page_title", ""),
                            "block_id": getattr(self, "current_block_id", None),
                            "link_text": clean_plain or raw_plain,
                            "href": href,
                            "status": "unresolved_in_page_link",
                        }
                        if not hasattr(self, "inpage_repair_candidates"):
                            self.inpage_repair_candidates = []
                        if not any(
                            c.get("page_id") == cand["page_id"]
                            and c.get("link_text") == cand["link_text"]
                            and c.get("block_id") == cand["block_id"]
                            for c in self.inpage_repair_candidates
                        ):
                            self.inpage_repair_candidates.append(cand)

                    # ページ内リンク（アンカー指定あり、または同一自ページへのリンク）は、Confluenceがインラインカード化して
                    # アンカーや表示テキスト（例:「熊本城」）をページ名に置き換えるのを防ぐため card-appearance を付与しない。
                    # 一方、別ページへのリンク（ページ間リンク）は、常にConfluenceのインラインリンクオブジェクト（ac:card-appearance="inline"）とする。
                    is_in_page_link = bool(anchor_name or is_self_page)
                    card_attr = "" if is_in_page_link else ' ac:card-appearance="inline"'
                    anchor_attr = f' ac:anchor="{html.escape(anchor_name)}"' if anchor_name else ""
                    escaped = (
                        f'<ac:link{card_attr}{anchor_attr}>'
                        f'<ri:page ri:content-title="{escaped_title}" />'
                        f'<ac:link-body>{escaped}</ac:link-body>'
                        f'</ac:link>'
                    )
                elif href:
                    escaped = f'<a href="{html.escape(href)}">{escaped}</a>'

            result.append(escaped)

        return "".join(result)

    def _get_block_style_attr(self, color: str, base_padding: str = "6px 10px") -> Tuple[str, str]:
        """ブロックレベルの色指定から、タグのstyle属性とインライン用default_colorを返す。
        - 背景色(xxx_background): 行全体にスタイルを適用し、インライン重複を防ぐためdefault_colorは'default'とする。
        - 文字色(xxx): 行全体に文字色スタイルを適用し、default_colorとしても引き継ぐ。
        """
        if not color or color == "default":
            return "", "default"
        if color == "teal":
            color = "green"
        elif color == "teal_background":
            color = "green_background"

        if color in NOTION_BG_COLOR_MAP:
            bg_hex = NOTION_BG_COLOR_MAP[color]
            style_attr = f' style="background-color: {bg_hex}; padding: {base_padding}; border-radius: 4px; margin: 4px 0;"'
            return style_attr, "default"
        elif color in NOTION_TEXT_COLOR_MAP:
            txt_hex = NOTION_TEXT_COLOR_MAP[color]
            style_attr = f' style="color: {txt_hex};"'
            return style_attr, color
        return "", "default"

    def _collect_page_anchors(self, blocks: List[Dict[str, Any]]) -> None:
        """ページ内の見出しやブロックからアンカー名およびブロックIDマッピングを事前抽出する。"""
        if not blocks:
            return
        for b in blocks:
            if not isinstance(b, dict):
                continue
            b_type = b.get("type")
            b_id = b.get("id")

            # ブロック内のリンクから参照されているアンカーを抽出
            data_dict = b.get(b_type, {}) if isinstance(b.get(b_type), dict) else {}
            for rt in data_dict.get("rich_text", []):
                link_obj = (rt.get("text") or {}).get("link") or {}
                url = link_obj.get("url") if isinstance(link_obj, dict) else None
                if not url and rt.get("href"):
                    url = rt.get("href")
                if url and "#" in url:
                    anchor = urllib.parse.unquote(url.split("#", 1)[1].strip())
                    if anchor:
                        self.referenced_anchors.add(anchor)
                        self.referenced_anchors.add(anchor.replace("-", "").lower())

            if b_type in ("heading_1", "heading_2", "heading_3"):
                rich_texts = data_dict.get("rich_text", [])
                raw_text = "".join(t.get("plain_text", "") for t in rich_texts).strip()
                clean_text = re.sub(r"<[^>]+>", "", raw_text).strip()
                if clean_text:
                    self.current_page_headings.add(clean_text)
                    if b_id:
                        self.current_page_block_anchors[b_id] = clean_text
                        norm_id = b_id.replace("-", "").lower()
                        self.current_page_block_anchors[norm_id] = clean_text
            elif b_id:
                norm_id = b_id.replace("-", "").lower()
                self.current_page_block_anchors[b_id] = norm_id
                self.current_page_block_anchors[norm_id] = norm_id

            if b.get("children_blocks"):
                self._collect_page_anchors(b["children_blocks"])

    def blocks_to_storage_format(self, blocks: List[Dict[str, Any]], context_heading: str = "") -> str:
        """NotionブロックのリストをConfluence Storage Format (XHTML) に変換する。
        連続する箇条書き、番号付きリスト、ToDoリストを適切にタグでラップする。"""
        if not context_heading and not self.current_page_headings:
            self._collect_page_anchors(blocks)

        output = []
        current_list_type = None  # "bulleted", "numbered", "to_do"
        list_buffer = []
        current_heading = context_heading

        def flush_list():
            nonlocal current_list_type, list_buffer
            if not current_list_type:
                return
            if current_list_type == "bulleted":
                output.append("<ul>" + "".join(list_buffer) + "</ul>")
            elif current_list_type == "numbered":
                output.append("<ol>" + "".join(list_buffer) + "</ol>")
            elif current_list_type == "to_do":
                output.append(
                    '<ac:task-list>' + "".join(list_buffer) + '</ac:task-list>'
                )
            current_list_type = None
            list_buffer = []

        for b in blocks:
            if isinstance(b, dict):
                self.current_block_id = b.get("id")
            b_type = b.get("type")

            # リスト系の処理
            if b_type == "bulleted_list_item":
                if current_list_type != "bulleted":
                    flush_list()
                    current_list_type = "bulleted"
                b_color = b.get("bulleted_list_item", {}).get("color", "default")
                style_attr, inline_color = self._get_block_style_attr(b_color, base_padding="2px 6px")
                text = self.convert_rich_text(b.get("bulleted_list_item", {}).get("rich_text", []), default_color=inline_color)
                nested_html = ""
                if b.get("children_blocks"):
                    nested_html = self.blocks_to_storage_format(b["children_blocks"])
                list_buffer.append(f"<li{style_attr}>{text}{nested_html}</li>")
                continue

            elif b_type == "numbered_list_item":
                if current_list_type != "numbered":
                    flush_list()
                    current_list_type = "numbered"
                b_color = b.get("numbered_list_item", {}).get("color", "default")
                style_attr, inline_color = self._get_block_style_attr(b_color, base_padding="2px 6px")
                text = self.convert_rich_text(b.get("numbered_list_item", {}).get("rich_text", []), default_color=inline_color)
                nested_html = ""
                if b.get("children_blocks"):
                    nested_html = self.blocks_to_storage_format(b["children_blocks"])
                list_buffer.append(f"<li{style_attr}>{text}{nested_html}</li>")
                continue

            elif b_type == "to_do":
                if current_list_type != "to_do":
                    flush_list()
                    current_list_type = "to_do"
                todo_data = b.get("to_do", {})
                is_checked = todo_data.get("checked", False)
                status = "complete" if is_checked else "incomplete"
                b_color = todo_data.get("color", "default")
                text = self.convert_rich_text(todo_data.get("rich_text", []), default_color=b_color)
                nested_html = ""
                if b.get("children_blocks"):
                    nested_html = self.blocks_to_storage_format(b["children_blocks"])
                list_buffer.append(
                    f'<ac:task><ac:task-status>{status}</ac:task-status><ac:task-body>{text}{nested_html}</ac:task-body></ac:task>'
                )
                continue

            # リスト以外のブロックが来たらバッファをフラッシュ
            flush_list()

            if b_type == "paragraph":
                p_data = b.get("paragraph", {})
                p_color = p_data.get("color", "default")
                style_attr, inline_color = self._get_block_style_attr(p_color, base_padding="6px 10px")
                text = self.convert_rich_text(p_data.get("rich_text", []), default_color=inline_color)
                children_html = ""
                if b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"])
                output.append(f"<p{style_attr}>{text if text else '<br />'}</p>{children_html}")

            elif b_type == "heading_1":
                h_data = b.get("heading_1", {})
                h_color = h_data.get("color", "default")
                style_attr, inline_color = self._get_block_style_attr(h_color, base_padding="6px 10px")
                text = self.convert_rich_text(h_data.get("rich_text", []), default_color=inline_color)
                current_heading = text
                raw_h_text = "".join(t.get("plain_text", "") for t in h_data.get("rich_text", [])).strip()
                clean_h_text = re.sub(r"<[^>]+>", "", raw_h_text).strip()
                b_id = b.get("id")
                anchor_macro = ""
                if clean_h_text:
                    anchor_macro += (
                        f'<ac:structured-macro ac:name="anchor">'
                        f'<ac:parameter ac:name="">{html.escape(clean_h_text)}</ac:parameter>'
                        f'</ac:structured-macro>'
                    )
                if b_id:
                    norm_id = b_id.replace("-", "").lower()
                    if norm_id and norm_id != clean_h_text:
                        anchor_macro += (
                            f'<ac:structured-macro ac:name="anchor">'
                            f'<ac:parameter ac:name="">{norm_id}</ac:parameter>'
                            f'</ac:structured-macro>'
                        )
                if h_data.get("is_toggleable") and b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"], context_heading=text)
                    param_title = clean_parameter_text(text) or "詳細"
                    output.append(
                        f'{anchor_macro}'
                        f'<ac:structured-macro ac:name="expand">'
                        f'<ac:parameter ac:name="title">{param_title}</ac:parameter>'
                        f'<ac:rich-text-body>{children_html if children_html else "<p></p>"}</ac:rich-text-body>'
                        f'</ac:structured-macro>'
                    )
                else:
                    output.append(f"<h1{style_attr}>{anchor_macro}{text}</h1>")

            elif b_type == "heading_2":
                h_data = b.get("heading_2", {})
                h_color = h_data.get("color", "default")
                style_attr, inline_color = self._get_block_style_attr(h_color, base_padding="6px 10px")
                text = self.convert_rich_text(h_data.get("rich_text", []), default_color=inline_color)
                current_heading = text
                raw_h_text = "".join(t.get("plain_text", "") for t in h_data.get("rich_text", [])).strip()
                clean_h_text = re.sub(r"<[^>]+>", "", raw_h_text).strip()
                b_id = b.get("id")
                anchor_macro = ""
                if clean_h_text:
                    anchor_macro += (
                        f'<ac:structured-macro ac:name="anchor">'
                        f'<ac:parameter ac:name="">{html.escape(clean_h_text)}</ac:parameter>'
                        f'</ac:structured-macro>'
                    )
                if b_id:
                    norm_id = b_id.replace("-", "").lower()
                    if norm_id and norm_id != clean_h_text:
                        anchor_macro += (
                            f'<ac:structured-macro ac:name="anchor">'
                            f'<ac:parameter ac:name="">{norm_id}</ac:parameter>'
                            f'</ac:structured-macro>'
                        )
                if h_data.get("is_toggleable") and b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"], context_heading=text)
                    param_title = clean_parameter_text(text) or "詳細"
                    output.append(
                        f'{anchor_macro}'
                        f'<ac:structured-macro ac:name="expand">'
                        f'<ac:parameter ac:name="title">{param_title}</ac:parameter>'
                        f'<ac:rich-text-body>{children_html if children_html else "<p></p>"}</ac:rich-text-body>'
                        f'</ac:structured-macro>'
                    )
                else:
                    output.append(f"<h2{style_attr}>{anchor_macro}{text}</h2>")

            elif b_type == "heading_3":
                h_data = b.get("heading_3", {})
                h_color = h_data.get("color", "default")
                style_attr, inline_color = self._get_block_style_attr(h_color, base_padding="6px 10px")
                text = self.convert_rich_text(h_data.get("rich_text", []), default_color=inline_color)
                current_heading = text
                raw_h_text = "".join(t.get("plain_text", "") for t in h_data.get("rich_text", [])).strip()
                clean_h_text = re.sub(r"<[^>]+>", "", raw_h_text).strip()
                b_id = b.get("id")
                anchor_macro = ""
                if clean_h_text:
                    anchor_macro += (
                        f'<ac:structured-macro ac:name="anchor">'
                        f'<ac:parameter ac:name="">{html.escape(clean_h_text)}</ac:parameter>'
                        f'</ac:structured-macro>'
                    )
                if b_id:
                    norm_id = b_id.replace("-", "").lower()
                    if norm_id and norm_id != clean_h_text:
                        anchor_macro += (
                            f'<ac:structured-macro ac:name="anchor">'
                            f'<ac:parameter ac:name="">{norm_id}</ac:parameter>'
                            f'</ac:structured-macro>'
                        )
                if h_data.get("is_toggleable") and b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"], context_heading=text)
                    param_title = clean_parameter_text(text) or "詳細"
                    output.append(
                        f'{anchor_macro}'
                        f'<ac:structured-macro ac:name="expand">'
                        f'<ac:parameter ac:name="title">{param_title}</ac:parameter>'
                        f'<ac:rich-text-body>{children_html if children_html else "<p></p>"}</ac:rich-text-body>'
                        f'</ac:structured-macro>'
                    )
                else:
                    output.append(f"<h3{style_attr}>{anchor_macro}{text}</h3>")

            elif b_type == "table_of_contents":
                # Confluence 目次マクロ (TOC Macro)
                output.append(
                    '<ac:structured-macro ac:name="toc">'
                    '<ac:parameter ac:name="maxLevel">3</ac:parameter>'
                    '<ac:parameter ac:name="type">list</ac:parameter>'
                    '</ac:structured-macro>'
                )

            elif b_type == "column_list":
                # Confluence セクションマクロ (Section / Columns)
                columns_html = []
                for col in b.get("children_blocks", []):
                    col_content = self.blocks_to_storage_format(col.get("children_blocks", []))
                    columns_html.append(
                        f'<ac:structured-macro ac:name="column">'
                        f'<ac:rich-text-body>{col_content if col_content else "<p></p>"}</ac:rich-text-body>'
                        f'</ac:structured-macro>'
                    )
                output.append(
                    f'<ac:structured-macro ac:name="section">'
                    f'<ac:rich-text-body>{"".join(columns_html)}</ac:rich-text-body>'
                    f'</ac:structured-macro>'
                )

            elif b_type == "equation":
                # 数式ブロック: Confluence Cloud標準で100%エラーなく美しく表示するため高解像度画像として埋め込み
                expr = b.get("equation", {}).get("expression", "")
                block_id_short = b.get("id", "").replace("-", "")[:8]
                filename = b.get("_attachment_filename") or f"equation_{block_id_short}.png"
                output.append(
                    f'<p><ac:image ac:align="center">'
                    f'<ri:attachment ri:filename="{html.escape(filename)}" />'
                    f'</ac:image></p>'
                )

            elif b_type == "code":
                code_data = b.get("code", {})
                lang = code_data.get("language", "text")
                raw_code = "".join(t.get("plain_text", "") for t in code_data.get("rich_text", []))
                is_mermaid, clean_mermaid = self.is_mermaid_code(lang, raw_code)

                if is_mermaid:
                    # Task B.8: 方式② Confluence codeマクロ (language: mermaid) による完全ローカル・外部API不要のネイティブ移行
                    caption_list = code_data.get("caption", [])
                    caption_text = "".join(t.get("plain_text", "") for t in caption_list) if caption_list else ""
                    macro_title = caption_text or "📊 Mermaid ダイアグラム"
                    safe_cdata = raw_code.replace("]]>", "]]]]><![CDATA[>")
                    output.append(
                        f'<ac:structured-macro ac:name="code">'
                        f'<ac:parameter ac:name="language">mermaid</ac:parameter>'
                        f'<ac:parameter ac:name="title">{html.escape(macro_title)}</ac:parameter>'
                        f'<ac:parameter ac:name="linenumbers">true</ac:parameter>'
                        f'<ac:plain-text-body><![CDATA[{safe_cdata}]]></ac:plain-text-body>'
                        f'</ac:structured-macro>'
                    )
                else:
                    safe_cdata = raw_code.replace("]]>", "]]]]><![CDATA[>")
                    output.append(
                        f'<ac:structured-macro ac:name="code">'
                        f'<ac:parameter ac:name="language">{html.escape(lang)}</ac:parameter>'
                        f'<ac:parameter ac:name="linenumbers">true</ac:parameter>'
                        f'<ac:plain-text-body><![CDATA[{safe_cdata}]]></ac:plain-text-body>'
                        f'</ac:structured-macro>'
                    )

            elif b_type == "quote":
                q_data = b.get("quote", {})
                q_color = q_data.get("color", "default")
                style_attr, inline_color = self._get_block_style_attr(q_color, base_padding="8px 12px")
                text = self.convert_rich_text(q_data.get("rich_text", []), default_color=inline_color)
                children_html = ""
                if b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"])
                output.append(f"<blockquote{style_attr}><p>{text}</p>{children_html}</blockquote>")

            elif b_type == "callout":
                from src.icon_mapper import resolve_notion_icon_to_emoji
                callout_data = b.get("callout", {})
                color = callout_data.get("color", "default").lower()
                color_key = color.replace("_background", "").strip()
                color_info = CALLOUT_PANEL_COLORS.get(color_key, CALLOUT_PANEL_COLORS["default"])

                icon_obj = callout_data.get("icon") or {}
                emoji = resolve_notion_icon_to_emoji(icon_obj)
                text = self.convert_rich_text(callout_data.get("rich_text", []))

                # Confluenceのinfoマクロに固定表示される(i)アイコンを排除し、
                # Notion本来のアイコン（絵文字・Notionアイコン）のみを単一表示するためpanelマクロを採用
                prefix = f"{emoji} " if emoji else ""
                full_text = f"{prefix}{text}" if text else (emoji or "")

                children_html = ""
                if b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"])

                body_parts = []
                if full_text:
                    body_parts.append(f"<p>{full_text}</p>")
                if children_html:
                    body_parts.append(children_html)
                if not body_parts:
                    body_parts.append("<p></p>")

                output.append(
                    f'<ac:structured-macro ac:name="panel">'
                    f'<ac:parameter ac:name="bgColor">{color_info["bg"]}</ac:parameter>'
                    f'<ac:parameter ac:name="borderColor">{color_info["border"]}</ac:parameter>'
                    f'<ac:rich-text-body>{"".join(body_parts)}</ac:rich-text-body>'
                    f'</ac:structured-macro>'
                )

            elif b_type == "toggle":
                toggle_data = b.get("toggle", {})
                title_text = self.convert_rich_text(toggle_data.get("rich_text", [])) or "詳細"
                param_title = clean_parameter_text(title_text) or "詳細"
                children_html = ""
                if b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"], context_heading=title_text)
                output.append(
                    f'<ac:structured-macro ac:name="expand">'
                    f'<ac:parameter ac:name="title">{param_title}</ac:parameter>'
                    f'<ac:rich-text-body>{children_html if children_html else "<p></p>"}</ac:rich-text-body>'
                    f'</ac:structured-macro>'
                )

            elif b_type == "divider":
                output.append("<hr />")

            elif b_type == "table":
                output.append(self._convert_simple_table(b))

            elif b_type == "child_page":
                child_title = b.get("child_page", {}).get("title", "Untitled Page")
                target_id = b.get("id")
                target_info = None

                if hasattr(self, "migration_map") and self.migration_map:
                    # 1. target_id (Notion UUID) による完全一致探索
                    if target_id:
                        norm_target = target_id.replace("-", "").lower()
                        for mid, minfo in self.migration_map.items():
                            if mid.replace("-", "").lower() == norm_target:
                                target_info = minfo
                                break

                    # 2. タイトルによる探索（完全一致・前方一致・後方一致・部分一致）
                    if not target_info:
                        for m in self.migration_map.values():
                            if m.get("node_type") == "page":
                                m_title = m.get("title", "")
                                if (
                                    m_title == child_title
                                    or m_title.startswith(child_title)
                                    or m_title.endswith(child_title)
                                    or child_title in m_title
                                ):
                                    target_info = m
                                    break

                disp_title = (target_info.get("title") if target_info else None) or child_title
                escaped_title = html.escape(disp_title)
                output.append(
                    f'<p><ac:link ac:card-appearance="inline">'
                    f'<ri:page ri:content-title="{escaped_title}" />'
                    f'<ac:link-body>{escaped_title}</ac:link-body>'
                    f'</ac:link></p>'
                )

            elif b_type == "child_database":
                if b.get("is_linked_database"):
                    # Notionのリンクされたデータベースビュー（Linked Database View）
                    data_sources = b.get("data_sources", [])
                    views_info = b.get("views", [])
                    source_name = data_sources[0].get("name", "") if data_sources else ""

                    db_info = None
                    if hasattr(self, "migration_map") and self.migration_map:
                        # 1. UUIDによる直接探索を最優先 (ブロックID, database_id, data_sourcesのID)
                        candidate_ids = []
                        if b.get("database_id"):
                            candidate_ids.append(b.get("database_id"))
                        if b.get("id"):
                            candidate_ids.append(b.get("id"))
                        for ds in data_sources:
                            if isinstance(ds, dict) and ds.get("id"):
                                candidate_ids.append(ds.get("id"))

                        for cid in candidate_ids:
                            norm_cid = cid.replace("-", "").lower()
                            for mid, minfo in self.migration_map.items():
                                if mid.replace("-", "").lower() == norm_cid and minfo.get("node_type") == "database":
                                    db_info = minfo
                                    break
                            if db_info:
                                break

                        # 2. 見つからない場合はタイトルによるフォールバック
                        if not db_info and source_name:
                            for mid, minfo in self.migration_map.items():
                                if minfo.get("node_type") == "database":
                                    m_title = minfo.get("title", "")
                                    if m_title == source_name or m_title.endswith(source_name):
                                        db_info = minfo
                                        break

                    if db_info:
                        db_url = db_info.get("url")
                        conf_views = db_info.get("views", {})

                        primary_view = views_info[0] if views_info else {}
                        v_name = primary_view.get("name", "")
                        v_type = primary_view.get("type", "")
                        v_config = primary_view.get("configuration", {}) or {}
                        group_by = v_config.get("group_by")

                        # Notionで Group by（グループ化）が設定されたビューの場合：
                        # Confluence Databasesには単一ビュー内でのGroup by機能がないため、
                        # ハイブリッド方式（未完了グループ: 未着手・進行中等は常時展開の見出し＋完了グループ: 完了等はExpandマクロ折りたたみ）として自動近似移植する
                        if group_by and group_by.get("property_name"):
                            prop_name = group_by.get("property_name")
                            open_groups = []
                            collapsed_groups = []
                            for cv_name, cv_id in conf_views.items():
                                if cv_name.startswith(f"{prop_name}: "):
                                    gval = cv_name[len(f"{prop_name}: "):]
                                    if any(done_word in gval for done_word in ("完了", "Done", "Closed", "アーカイブ")):
                                        collapsed_groups.append((gval, cv_id))
                                    else:
                                        open_groups.append((gval, cv_id))

                            if open_groups or collapsed_groups:
                                hybrid_parts = []
                                for gval, vid in open_groups:
                                    card = self.generate_database_view_embed(db_url, vid)
                                    hybrid_parts.append(f"<h3>📌 {html.escape(prop_name)}: {html.escape(gval)} (常時展開)</h3>\n{card}")
                                for gval, vid in collapsed_groups:
                                    card = self.generate_database_view_embed(db_url, vid)
                                    title_text = html.escape(f"📂 {prop_name}: {gval} (折りたたみ保管)")
                                    hybrid_parts.append(
                                        f'<ac:structured-macro ac:name="expand">'
                                        f'<ac:parameter ac:name="title">{title_text}</ac:parameter>'
                                        f'<ac:rich-text-body>{card}</ac:rich-text-body>'
                                        f'</ac:structured-macro>'
                                    )
                                output.append("\n".join(hybrid_parts))
                                continue

                        target_view_id = None
                        views_list = db_info.get("views_info", [])

                        if v_type == "board" or "ボード" in v_name:
                            board_group_field = group_by.get("property_name") if group_by else None
                            # 1. views_info から type == 'board' を最優先で検索
                            if views_list:
                                if board_group_field:
                                    target_view_id = next((v["id"] for v in views_list if v.get("type") == "board" and board_group_field in v.get("name", "")), None)
                                if not target_view_id:
                                    target_view_id = next((v["id"] for v in views_list if v.get("type") == "board"), None)

                            # 2. conf_views からフォールバック
                            if not target_view_id:
                                if board_group_field:
                                    target_view_id = next((vid for name, vid in conf_views.items() if board_group_field in name and ("ボード" in name or "board" in name.lower())), None)
                                if not target_view_id:
                                    target_view_id = next((vid for name, vid in conf_views.items() if "ボード" in name or "board" in name.lower()), None)

                        elif v_type == "gallery" or "ギャラリー" in v_name:
                            # 1. views_info から type == 'gallery' を最優先で検索
                            if views_list:
                                target_view_id = next((v["id"] for v in views_list if v.get("type") == "gallery"), None)
                            # 2. conf_views からフォールバック
                            if not target_view_id:
                                target_view_id = next((vid for name, vid in conf_views.items() if "ギャラリー" in name or "gallery" in name.lower()), None)

                        elif "未着手" in v_name:
                            target_view_id = next((vid for name, vid in conf_views.items() if "未着手" in name), None)
                        elif "進行中" in v_name:
                            target_view_id = next((vid for name, vid in conf_views.items() if "進行中" in name), None)
                        elif "完了" in v_name:
                            target_view_id = next((vid for name, vid in conf_views.items() if "完了" in name), None)
                        else:
                            target_view_id = next((vid for name, vid in conf_views.items() if "すべてのデータ" in name or "default" in name.lower()), None)
                            if not target_view_id and v_name in conf_views:
                                target_view_id = conf_views[v_name]
                            if not target_view_id and conf_views:
                                target_view_id = next(iter(conf_views.values()), None)

                        embed_html = self.generate_database_view_embed(db_url, target_view_id)

                        # 完了グループは方式A（Expandマクロ折りたたみ）でラップ（全体ビューであるboard/galleryは除外）
                        is_completed_group = (v_type not in ("board", "gallery")) and (
                            v_name == "完了" or "ステータス: 完了" in v_name or (": 完了" in v_name)
                        )
                        if is_completed_group:
                            output.append(
                                f'<ac:structured-macro ac:name="expand">'
                                f'<ac:parameter ac:name="title">📂 ステータス: 完了 (折りたたみ保管)</ac:parameter>'
                                f'<ac:rich-text-body>{embed_html}</ac:rich-text-body>'
                                f'</ac:structured-macro>'
                            )
                        else:
                            output.append(embed_html)
                    else:
                        output.append(f'<p>📊 <em>データベースビュー: {html.escape(source_name)}</em></p>')

                else:
                    db_title = b.get("database_title") or b.get("child_database", {}).get("title", "Untitled Database")
                    escaped_title = html.escape(db_title)
                    db_url = None
                    if hasattr(self, "migration_map") and self.migration_map:
                        # Notion ID またはタイトル一致でマッピングされた Confluence Database の URL を検索
                        m_entry = self.migration_map.get(b.get("id"))
                        if m_entry and m_entry.get("node_type") == "database":
                            db_url = m_entry.get("url")
                        if not db_url:
                            for m in self.migration_map.values():
                                if m.get("node_type") == "database":
                                    m_title = m.get("title", "")
                                    if m_title == db_title or m_title.endswith(db_title):
                                        db_url = m.get("url")
                                        break

                    if db_url:
                        # Confluence Databases は page ではなく database オブジェクトのため、スマートリンク（URL）として出力
                        escaped_db_url = html.escape(db_url)
                        output.append(
                            f'<p><a href="{escaped_db_url}" data-card-appearance="inline">{escaped_db_url}</a></p>'
                        )
                    else:
                        output.append(
                            f'<p><ac:link ac:card-appearance="inline">'
                            f'<ri:page ri:content-title="{escaped_title}" />'
                            f'<ac:link-body>{escaped_title}</ac:link-body>'
                            f'</ac:link></p>'
                        )

            elif b_type == "link_to_page":
                link_data = b.get("link_to_page", {})
                target_page_id = link_data.get("page_id") or link_data.get("database_id")
                saved_view_id = link_data.get("saved_view_id")
                db_url = link_data.get("db_url")

                # migration_map からデータベース/ページ情報を逆引き
                db_info = None
                if target_page_id and hasattr(self, "migration_map") and self.migration_map:
                    norm_id = target_page_id.replace("-", "").lower()
                    db_info = self.migration_map.get(target_page_id) or self.migration_map.get(norm_id)
                    if not db_info:
                        for mid, minfo in self.migration_map.items():
                            if mid.replace("-", "").lower() == norm_id:
                                db_info = minfo
                                break

                if db_info and db_info.get("node_type") == "database":
                    db_url = db_info.get("url")
                    views = db_info.get("views", {})
                    active_context = (context_heading or current_heading).strip()
                    page_title_ctx = (getattr(self, "current_page_title", "") or "").strip()
                    combined_ctx = f"{active_context} {page_title_ctx}".strip()

                    # 見出し・トグル・ページタイトルの文脈に応じた最適なConfluence DatabasesビューIDの自動選択
                    if not saved_view_id and views:
                        act_lower = combined_ctx.lower()
                        if any(kw in act_lower for kw in ("ボード", "カンバン", "board")):
                            saved_view_id = next((vid for name, vid in views.items() if "ボード" in name or "board" in name.lower()), None)
                        elif any(kw in act_lower for kw in ("ギャラリー", "カード", "gallery")):
                            saved_view_id = next((vid for name, vid in views.items() if "ギャラリー" in name or "gallery" in name.lower()), None)
                        elif any(kw in act_lower for kw in ("リスト", "list", "テーブル", "table")):
                            saved_view_id = next((vid for name, vid in views.items() if "すべてのデータ" in name or "default" in name.lower()), None)
                        else:
                            # Group by / カテゴリフィルタービューの自動マッピング (見出し優先、なければページタイトルで一致判定)
                            for vname, vid in views.items():
                                clean_vname = vname.split(": ")[-1] if ": " in vname else vname
                                if clean_vname and (
                                    (active_context and clean_vname in active_context)
                                    or (page_title_ctx and len(clean_vname) >= 2 and clean_vname in page_title_ctx)
                                ):
                                    saved_view_id = vid
                                    break

                    output.append(self.generate_database_view_embed(db_url, saved_view_id))

                elif db_url:
                    output.append(self.generate_database_view_embed(db_url, saved_view_id))
                else:
                    target_title = None
                    if target_page_id and self.id_to_title_map:
                        norm_id = target_page_id.replace("-", "").lower()
                        target_title = self.id_to_title_map.get(target_page_id) or self.id_to_title_map.get(norm_id)
                    if target_title:
                        escaped_title = html.escape(target_title)
                        output.append(
                            f'<p><ac:link ac:card-appearance="inline">'
                            f'<ri:page ri:content-title="{escaped_title}" />'
                            f'<ac:link-body>{escaped_title}</ac:link-body>'
                            f'</ac:link></p>'
                        )
                    else:
                        output.append(
                            f'<p>🔗 <em>Notionリンク: {target_page_id}</em></p>'
                        )

            elif b_type == "synced_block":
                synced_data = b.get("synced_block", {})
                synced_from = synced_data.get("synced_from")
                children_blocks = b.get("children_blocks", [])
                if children_blocks:
                    # 子ブロックが単一の synced_block の場合は多重枠線を避けてアンラップ
                    while (
                        len(children_blocks) == 1
                        and children_blocks[0].get("type") == "synced_block"
                        and children_blocks[0].get("children_blocks")
                    ):
                        children_blocks = children_blocks[0]["children_blocks"]
                    children_html = self.blocks_to_storage_format(children_blocks)
                    # Notionの同期ブロック風に、左端に薄紫のバーを配したコンテナとして描画
                    output.append(
                        f'<div style="border-left: 3px solid #8777D9; padding-left: 14px; margin: 12px 0;">'
                        f'{children_html}'
                        f'</div>'
                    )
                elif synced_from:
                    sync_orig_id = synced_from.get("block_id", "")
                    output.append(
                        f'<p>🔄 <em>同期ブロック（同期元: {html.escape(sync_orig_id)}）</em></p>'
                    )

            elif b_type in ("embed", "figma", "miro", "trello", "maps", "google_drive", "twitter", "gist", "codepen"):
                embed_data = b.get(b_type, {})
                url = embed_data.get("url", "")
                caption = self.convert_rich_text(embed_data.get("caption", []))
                caption_html = f"<p><em>{caption}</em></p>" if caption else ""
                if url:
                    # Confluenceのスマートリンク Embed カード + 直リンク
                    output.append(
                        f'<p><a href="{html.escape(url)}" data-card-appearance="embed">{html.escape(url)}</a></p>'
                        f'<p>🔗 <a href="{html.escape(url)}">{html.escape(url)}</a></p>'
                        f'{caption_html}'
                    )

            elif b_type == "link_preview":
                lp_data = b.get("link_preview", {})
                url = lp_data.get("url", "")
                if url:
                    output.append(
                        f'<p><a href="{html.escape(url)}" data-card-appearance="block">{html.escape(url)}</a></p>'
                    )

            elif b_type in ("breadcrumb", "unsupported", "button", "template"):
                # Notion固有・Confluence非対応ブロックのフォールバック表示
                b_name_map = {
                    "breadcrumb": "ページ内パンくず (Breadcrumb)",
                    "button": "アクションボタン (Button)",
                    "template": "テンプレートボタン (Template)",
                    "unsupported": "未対応独自機能 (Unsupported)",
                }
                disp_name = b_name_map.get(b_type, f"Notionブロック ({b_type})")
                children_html = ""
                if b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"])
                output.append(
                    f'<ac:structured-macro ac:name="note">'
                    f'<ac:parameter ac:name="title">⚠️ Notion固有機能: {html.escape(disp_name)}</ac:parameter>'
                    f'<ac:rich-text-body><p>この要素（種別: <code>{html.escape(b_type)}</code>）はConfluence側に同等の機能が存在しないため、フォールバック注記として保持されています。</p>{children_html}</ac:rich-text-body>'
                    f'</ac:structured-macro>'
                )

            elif b_type == "bookmark":
                bm_data = b.get("bookmark", {})
                url = bm_data.get("url", "")
                caption = self.convert_rich_text(bm_data.get("caption", []))
                caption_html = f"<p><em>{caption}</em></p>" if caption else ""
                if url:
                    output.append(
                        f'<p><a href="{html.escape(url)}" data-card-appearance="block">{html.escape(url)}</a></p>{caption_html}'
                    )

            elif b_type == "image":
                img_data = b.get("image", {})
                img_file = img_data.get("file") or {}
                img_ext = img_data.get("external") or {}
                img_url = img_file.get("url") or img_ext.get("url", "")
                filename = b.get("_attachment_filename") or self._get_attachment_filename(b)
                caption = self.convert_rich_text(img_data.get("caption", []))
                caption_html = f"<p><em>{caption}</em></p>" if caption else ""
                b_id = b.get("id")
                img_anchor = ""
                if b_id:
                    norm_id = b_id.replace("-", "").lower()
                    if hasattr(self, "referenced_anchors") and (
                        b_id in self.referenced_anchors or norm_id in self.referenced_anchors
                    ):
                        img_anchor = (
                            f'<ac:structured-macro ac:name="anchor">'
                            f'<ac:parameter ac:name="">{norm_id}</ac:parameter>'
                            f'</ac:structured-macro>'
                        )
                if img_url or filename:
                    output.append(
                        f'<p>{img_anchor}<ac:image>'
                        f'<ri:attachment ri:filename="{html.escape(filename)}" />'
                        f'</ac:image></p>{caption_html}'
                    )

            elif b_type == "pdf":
                pdf_data = b.get("pdf", {})
                pdf_file = pdf_data.get("file") or {}
                pdf_ext = pdf_data.get("external") or {}
                pdf_url = pdf_file.get("url") or pdf_ext.get("url", "")
                filename = b.get("_attachment_filename") or self._get_attachment_filename(b)
                caption = self.convert_rich_text(pdf_data.get("caption", []))
                caption_html = f"<p><em>{caption}</em></p>" if caption else ""
                if b.get("_attachment_uploaded", True):
                    output.append(
                        f'<p><ac:structured-macro ac:name="view-file">'
                        f'<ac:parameter ac:name="name"><ri:attachment ri:filename="{html.escape(filename)}" /></ac:parameter>'
                        f'</ac:structured-macro></p>{caption_html}'
                    )
                elif pdf_url:
                    output.append(
                        f'<p>📄 <a href="{html.escape(pdf_url)}">PDF文書を開く: {html.escape(filename)}</a></p>{caption_html}'
                    )

            elif b_type == "file":
                file_data = b.get("file", {})
                file_file = file_data.get("file") or {}
                file_ext = file_data.get("external") or {}
                file_url = file_file.get("url") or file_ext.get("url", "")
                filename = b.get("_attachment_filename") or self._get_attachment_filename(b)
                caption = self.convert_rich_text(file_data.get("caption", []))
                caption_html = f"<p><em>{caption}</em></p>" if caption else ""
                if b.get("_attachment_uploaded", True):
                    safe_fn_cdata = filename.replace("]]>", "]]]]><![CDATA[>")
                    output.append(
                        f'<p><ac:link>'
                        f'<ri:attachment ri:filename="{html.escape(filename)}" />'
                        f'<ac:plain-text-link-body><![CDATA[📎 {safe_fn_cdata} (添付ファイルをダウンロード)]]></ac:plain-text-link-body>'
                        f'</ac:link></p>{caption_html}'
                    )
                elif file_url:
                    output.append(
                        f'<p>📎 <a href="{html.escape(file_url)}">添付ファイルを開く: {html.escape(filename)}</a></p>{caption_html}'
                    )

            elif b_type == "video":
                video_data = b.get("video", {})
                video_file = video_data.get("file") or {}
                video_ext = video_data.get("external") or {}
                video_url = video_file.get("url") or video_ext.get("url", "")
                caption = self.convert_rich_text(video_data.get("caption", []))
                caption_html = f"<p><em>{caption}</em></p>" if caption else ""

                is_embed_service = any(h in video_url.lower() for h in ("youtube.com", "youtu.be", "vimeo.com", "loom.com", "twitch.tv", "wistia.com"))
                if is_embed_service:
                    output.append(
                        f'<ac:structured-macro ac:name="info">'
                        f'<ac:parameter ac:name="title">🎥 動画コンテンツ</ac:parameter>'
                        f'<ac:rich-text-body>'
                        f'<p><a href="{html.escape(video_url)}" data-card-appearance="embed">{html.escape(video_url)}</a></p>'
                        f'<p>🔗 <a href="{html.escape(video_url)}">動画を別ウィンドウで開いて視聴する ({html.escape(video_url)})</a></p>'
                        f'{caption_html}'
                        f'</ac:rich-text-body>'
                        f'</ac:structured-macro>'
                    )
                else:
                    filename = b.get("_attachment_filename") or self._get_attachment_filename(b)
                    if b.get("_attachment_uploaded", True):
                        safe_fn_cdata = filename.replace("]]>", "]]]]><![CDATA[>")
                        output.append(
                            f'<p><ac:structured-macro ac:name="multimedia">'
                            f'<ac:parameter ac:name="name"><ri:attachment ri:filename="{html.escape(filename)}" /></ac:parameter>'
                            f'</ac:structured-macro></p>'
                            f'<p><ac:link>'
                            f'<ri:attachment ri:filename="{html.escape(filename)}" />'
                            f'<ac:plain-text-link-body><![CDATA[🎬 {safe_fn_cdata} (動画再生・ダウンロード)]]></ac:plain-text-link-body>'
                            f'</ac:link></p>{caption_html}'
                        )
                    else:
                        output.append(
                            f'<p>🎬 <a href="{html.escape(video_url)}">動画を再生・ダウンロード: {html.escape(filename)}</a></p>{caption_html}'
                        )

            elif b_type == "audio":
                audio_data = b.get("audio", {})
                audio_file = audio_data.get("file") or {}
                audio_ext = audio_data.get("external") or {}
                audio_url = audio_file.get("url") or audio_ext.get("url", "")
                caption = self.convert_rich_text(audio_data.get("caption", []))
                caption_html = f"<p><em>{caption}</em></p>" if caption else ""

                is_embed_service = any(h in audio_url.lower() for h in ("spotify.com", "soundcloud.com", "apple.com"))
                if is_embed_service:
                    output.append(
                        f'<ac:structured-macro ac:name="info">'
                        f'<ac:parameter ac:name="title">🎵 音声コンテンツ</ac:parameter>'
                        f'<ac:rich-text-body>'
                        f'<p><a href="{html.escape(audio_url)}" data-card-appearance="embed">{html.escape(audio_url)}</a></p>'
                        f'<p>🔗 <a href="{html.escape(audio_url)}">音声を別ウィンドウで再生する</a></p>'
                        f'{caption_html}'
                        f'</ac:rich-text-body>'
                        f'</ac:structured-macro>'
                    )
                else:
                    filename = b.get("_attachment_filename") or self._get_attachment_filename(b)
                    if b.get("_attachment_uploaded", True):
                        safe_fn_cdata = filename.replace("]]>", "]]]]><![CDATA[>")
                        output.append(
                            f'<p><ac:structured-macro ac:name="multimedia">'
                            f'<ac:parameter ac:name="name"><ri:attachment ri:filename="{html.escape(filename)}" /></ac:parameter>'
                            f'</ac:structured-macro></p>'
                            f'<p><ac:link>'
                            f'<ri:attachment ri:filename="{html.escape(filename)}" />'
                            f'<ac:plain-text-link-body><![CDATA[🎵 {safe_fn_cdata} (音声ダウンロード)]]></ac:plain-text-link-body>'
                            f'</ac:link></p>{caption_html}'
                        )
                    else:
                        output.append(
                            f'<p>🎵 <a href="{html.escape(audio_url)}">音声ファイルを再生・ダウンロード: {html.escape(filename)}</a></p>{caption_html}'
                        )

            else:
                # 未知・未対応のNotionブロック型
                logger.warning(f"Unknown or unsupported Notion block type: {b_type}")
                children_html = ""
                if b.get("children_blocks"):
                    children_html = self.blocks_to_storage_format(b["children_blocks"])
                output.append(
                    f'<ac:structured-macro ac:name="note">'
                    f'<ac:parameter ac:name="title">⚠️ 未対応ブロック: {html.escape(str(b_type))}</ac:parameter>'
                    f'<ac:rich-text-body><p>種別 <code>{html.escape(str(b_type))}</code> のNotionブロックが検出されました。</p>{children_html}</ac:rich-text-body>'
                    f'</ac:structured-macro>'
                )

        flush_list()
        return "\n".join(output)

    def _convert_simple_table(self, table_block: Dict[str, Any]) -> str:
        """シンプルテーブルのブロックをConfluenceのtableタグに変換する。"""
        rows = table_block.get("children_blocks", [])
        has_header = table_block.get("table", {}).get("has_column_header", False)

        html_rows = []
        for idx, row in enumerate(rows):
            cells = row.get("table_row", {}).get("cells", [])
            tag = "th" if (idx == 0 and has_header) else "td"
            cell_strs = []
            for cell in cells:
                cell_content = self.convert_rich_text(cell)
                cell_strs.append(f"<{tag}>{cell_content}</{tag}>")
            html_rows.append(f"<tr>{''.join(cell_strs)}</tr>")

        return f'<table class="wrapped"><tbody>{"".join(html_rows)}</tbody></table>'

    def _escape_csv_cell(self, val: Any) -> str:
        """Confluence Database CSV用の安全なセルエスケープ。
        Confluence Databases API (<data> CSV表現) はセル内改行をサポートせず行分割とみなすため、
        改行文字は半角スペースに正規化して1行1レコードを厳格に保証する。
        """
        if val is None:
            return '""'
        s = str(val).strip()
        if not s or s == "-":
            return '""'
        # 改行文字が含まれている場合、半角スペースに正規化して単一物理行を維持
        if '\n' in s or '\r' in s:
            s = re.sub(r'[\r\n]+', ' ', s).strip()
        if any(c in s for c in (',', '"')):
            escaped = s.replace('"', '""')
            return f'"{escaped}"'
        return s

    def _format_prop_for_csv(self, prop: Any, prop_name: str, fallback_title: str) -> str:
        """NotionプロパティをConfluence Database CSV用のセル値に変換する。"""
        if prop is None:
            return ""
        if not isinstance(prop, dict):
            return str(prop)

        p_type = prop.get("type")
        if p_type == "title":
            val = prop.get("value") or fallback_title
            return str(val) if val else ""
        elif p_type in ("select", "status"):
            val = prop.get("value")
            if not val and isinstance(prop.get(p_type), dict):
                val = prop[p_type].get("name") or prop[p_type].get("value")
            return str(val) if val else ""
        elif p_type == "multi_select":
            items = (
                prop.get("items")
                or prop.get("multi_select")
                or (prop.get("value") if isinstance(prop.get("value"), list) else [])
            )
            vals = []
            for it in items:
                if isinstance(it, dict):
                    v = it.get("value") or it.get("name")
                else:
                    v = str(it) if it else None
                if v:
                    vals.append(str(v))
            return "|".join(vals) if vals else ""
        elif p_type == "date":
            start = prop.get("start") or (prop.get("date") or {}).get("start") or prop.get("value")
            if not start:
                return ""
            return str(start).split("T")[0]
        elif p_type == "checkbox":
            return "完了" if prop.get("value") else "未完了"
        elif p_type == "url":
            val = prop.get("value")
            return str(val) if val else ""
        elif p_type == "number":
            val = prop.get("value")
            return str(val) if val is not None else ""
        elif p_type == "people":
            users = prop.get("users", [])
            if self.user_mapper and self.user_mapper.mappings:
                acc_ids = []
                for u in users:
                    u_id = u.get("id")
                    u_name = u.get("name")
                    u_email = u.get("person", {}).get("email") if isinstance(u.get("person"), dict) else None
                    m = self.user_mapper.resolve_user(user_id=u_id, email=u_email, name=u_name)
                    if m and m.is_resolved():
                        acc_ids.append(m.confluence_account_id)
                if acc_ids:
                    return "|".join(acc_ids)
            names = [u.get("name") for u in users if u.get("name")]
            return ", ".join(names) if names else ""
        elif p_type == "relation":
            relations = prop.get("relations") or prop.get("relation") or []
            titles = []
            for r in relations:
                t_id = r.get("id")
                norm_id = t_id.replace("-", "").lower() if t_id else ""
                t_title = None
                if self.id_to_title_map:
                    t_title = self.id_to_title_map.get(t_id) or self.id_to_title_map.get(norm_id)
                if not t_title and self.migration_map:
                    m = self.migration_map.get(t_id) or self.migration_map.get(norm_id)
                    if m:
                        t_title = m.get("title")
                titles.append(t_title if t_title else (r.get("text") or (t_id[:8] if t_id else "")))
            return ", ".join(t for t in titles if t)
        elif p_type == "rollup":
            r_type = prop.get("rollup_type")
            if r_type == "array":
                items = prop.get("items", [])
                vals = [str(it.get("value") or "") for it in items if it.get("value")]
                return ", ".join(vals) if vals else ""
            val = prop.get("value")
            return str(val) if val is not None else ""
        else:
            val = prop.get("value")
            return str(val) if val is not None else ""

    def get_select_field_configurations(self, db_node: PageNode) -> Tuple[List[str], List[str]]:
        """
        データベースノードから、単一選択 (single-select) と複数選択 (multi-select) のフィールド名を分類して返す。
        - single_select: select, status, checkbox
        - multi_select: multi_select
        全レコードの値を検証し、改行が含まれる列やキーカラムは除外する。
        """
        single_select = []
        multi_select = []
        schema = db_node.properties.get("schema", {})
        key_col_name = db_node.properties.get("title_column")
        if not key_col_name and schema:
            for p_name, p_info in schema.items():
                if isinstance(p_info, dict) and p_info.get("type") == "title":
                    key_col_name = p_name
                    break

        if schema:
            for p_name, p_info in schema.items():
                p_type = p_info.get("type", "")
                if p_name == key_col_name or p_type == "title":
                    continue
                if p_type in ("status", "select", "checkbox"):
                    single_select.append(p_name)
                elif p_type == "multi_select":
                    multi_select.append(p_name)
        elif db_node.children:
            col_types: Dict[str, str] = {}
            col_has_multiline: Set[str] = set()

            for child in db_node.children:
                for p_name, p_val in child.properties.items():
                    if isinstance(p_val, dict):
                        p_type = p_val.get("type", "text")
                        raw_v = p_val.get("value")
                    else:
                        p_type = "text"
                        raw_v = p_val

                    if p_name == key_col_name or p_type == "title":
                        continue

                    if p_type == "text" or (raw_v and ("\n" in str(raw_v) or "\r" in str(raw_v))):
                        col_has_multiline.add(p_name)
                    elif p_name not in col_types:
                        col_types[p_name] = p_type

            for p_name, p_type in col_types.items():
                if p_name in col_has_multiline or p_name == key_col_name:
                    continue
                if p_type in ("status", "select", "checkbox"):
                    single_select.append(p_name)
                elif p_type == "multi_select":
                    multi_select.append(p_name)

        return single_select, multi_select

    def generate_database_csv(self, db_node: PageNode, page_link_map: Dict[str, str]) -> str:
        """Confluence Databases (type: database) 用の構造化CSV (<fields>, <views>, <data>) を生成する。"""
        schema = db_node.properties.get("schema", {})
        key_col_name = db_node.properties.get("title_column")
        if not key_col_name and schema:
            for p_name, p_info in schema.items():
                if isinstance(p_info, dict) and p_info.get("type") == "title":
                    key_col_name = p_name
                    break
        if not key_col_name and db_node.children:
            for child in db_node.children:
                for p_name, p_val in child.properties.items():
                    if isinstance(p_val, dict) and p_val.get("type") == "title":
                        key_col_name = p_name
                        break
                if key_col_name:
                    break
        if not key_col_name:
            key_col_name = "リンク"

        fields: List[Tuple[str, str, str]] = [(key_col_name, "confluence-page-link", "")]

        prop_names = []
        if schema:
            for p_name, p_info in schema.items():
                p_type = p_info.get("type", "rich_text")
                if p_name == key_col_name or p_type == "title":
                    continue
                if p_type in ("select", "status", "multi_select", "checkbox"):
                    conf_type = "tag"
                elif p_type == "date":
                    conf_type = "date"
                elif p_type == "number":
                    conf_type = "number"
                elif p_type == "url":
                    conf_type = "hyperlink"
                elif p_type == "people" and self.user_mapper and self.user_mapper.mappings:
                    conf_type = "user"
                else:
                    conf_type = "text"

                cfg = 'number_format:number' if conf_type == "number" else ''
                fields.append((p_name, conf_type, cfg))
                prop_names.append(p_name)
        elif db_node.children:
            # 全子ノードのプロパティを走査して網羅的なスキーマを構築
            all_col_names: List[str] = []
            col_types: Dict[str, str] = {}
            col_has_multiline: Set[str] = set()

            for child in db_node.children:
                for p_name, p_val in child.properties.items():
                    if isinstance(p_val, dict):
                        p_type = p_val.get("type", "text")
                        raw_v = p_val.get("value")
                    else:
                        p_type = "text"
                        raw_v = p_val

                    if p_name == key_col_name or p_type == "title":
                        continue

                    if p_name not in all_col_names:
                        all_col_names.append(p_name)

                    if p_type == "text" or (raw_v and ("\n" in str(raw_v) or "\r" in str(raw_v))):
                        col_has_multiline.add(p_name)
                    elif p_name not in col_types:
                        col_types[p_name] = p_type

            for p_name in all_col_names:
                p_type = col_types.get(p_name, "text")
                if p_name in col_has_multiline:
                    conf_type = "text"
                elif p_type in ("select", "status", "multi_select", "checkbox"):
                    conf_type = "tag"
                elif p_type == "date":
                    conf_type = "date"
                elif p_type == "number":
                    conf_type = "number"
                elif p_type == "url":
                    conf_type = "hyperlink"
                elif p_type == "people" and self.user_mapper and self.user_mapper.mappings:
                    conf_type = "user"
                else:
                    conf_type = "text"

                cfg = 'number_format:number' if conf_type == "number" else ''
                fields.append((p_name, conf_type, cfg))
                prop_names.append(p_name)

        field_lines = ["<fields>", "field_name,type,configuration"]
        for f_name, f_type, f_cfg in fields:
            field_lines.append(f"{self._escape_csv_cell(f_name)},{f_type},{f_cfg}")
        field_lines.append("</fields>")

        view_lines = [
            "<views>",
            "view_name,layout,filters,sorts,hidden_fields",
            'すべてのデータ,table,"","",""',
            "</views>",
        ]

        header_cols = [f[0] for f in fields]
        data_lines = [
            "<data>",
            ",".join(self._escape_csv_cell(c) for c in header_cols),
        ]

        for child in db_node.children:
            row_cells = []
            child_link = str(page_link_map.get(child.id, "")).strip()
            # confluence-page-link は有効なURLまたは数値Content IDのみ許容される。
            # ページ未作成時などに文字列タイトルを入れるとConfluenceがContent ID解決に失敗し
            # 「Could not create database entity in Handling Service: CANVAS / Content ID not found for page link value」
            # で500/400エラーとなるため、有効なURLまたは数値ID以外は必ず空文字とする。
            safe_link_val = child_link if (child_link and (child_link.startswith("http://") or child_link.startswith("https://") or child_link.isdigit())) else ""
            row_cells.append(self._escape_csv_cell(safe_link_val))

            for p_name in prop_names:
                prop = child.properties.get(p_name)
                val_str = self._format_prop_for_csv(prop, p_name, child.title)
                row_cells.append(self._escape_csv_cell(val_str))

            data_lines.append(",".join(row_cells))

        data_lines.append("</data>")

        return "\n".join(field_lines + view_lines + data_lines)

    @staticmethod
    def generate_database_view_embed(db_web_url: str, saved_view_id: Optional[str] = None) -> str:
        """Confluence Databases のビューを埋め込むためのスマートカードHTMLを生成する。"""
        url = f"{db_web_url}?savedViewId={saved_view_id}" if saved_view_id else db_web_url
        escaped_url = html.escape(url)
        return f'<p><a href="{escaped_url}" data-layout="center" data-width="100.00" data-card-appearance="embed">{escaped_url}</a></p>'

    @classmethod
    def generate_grouped_database_views_html(
        cls,
        db_web_url: str,
        group_field: str,
        group_views: List[Dict[str, str]],
        style: str = "expand",
    ) -> str:
        """
        NotionのGroup by（グループ化）を再現するため、複数のフィルタービューを配置したHTMLを生成する。
        - group_views: [{"label": "未着手", "saved_view_id": "...", "count": 2}, ...]
        - style: "expand" (折りたたみトグル) または "heading" (見出し区切り)
        """
        parts = []
        for g in group_views:
            label = g.get("label", "その他")
            view_id = g.get("saved_view_id")
            count = g.get("count")
            count_str = f" ({count}件)" if count is not None else ""
            embed_card = cls.generate_database_view_embed(db_web_url, view_id)

            if style == "expand":
                title_text = html.escape(f"📂 {group_field}: {label}{count_str}")
                parts.append(
                    f'<ac:structured-macro ac:name="expand">'
                    f'<ac:parameter ac:name="title">{title_text}</ac:parameter>'
                    f'<ac:rich-text-body>{embed_card}</ac:rich-text-body>'
                    f'</ac:structured-macro>'
                )
            else:
                title_text = html.escape(f"📌 {group_field}: {label}{count_str}")
                parts.append(f"<h4>{title_text}</h4>\n{embed_card}")

        return "\n".join(parts)

    def generate_page_content(
        self,
        node: PageNode,
        db_url: Optional[str] = None,
        entry_id: Optional[str] = None,
        incoming_links: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """PageNodeのデータからConfluenceページ用の本文 (Storage Format) を生成する。"""
        self.current_node_id = getattr(node, "id", None)
        self.current_page_title = getattr(node, "title", None)
        self.current_page_headings = set()
        self.current_page_block_anchors = {}
        self.referenced_anchors = set()
        self._collect_page_anchors(getattr(node, "blocks", []))
        cover_html = ""
        cover_file = getattr(node, "_cover_filename", None) or (
            f"cover_{node.id.replace('-', '')[:8]}.jpg" if getattr(node, "cover", None) else None
        )
        if cover_file:
            # Notionのカバー画像のようにページの最上部に横幅いっぱいの美しいバナーとして表示
            cover_html = (
                f'<div style="width: 100%; max-height: 250px; overflow: hidden; margin-bottom: 24px; border-radius: 8px;">'
                f'<p style="text-align: center; margin: 0;">'
                f'<ac:image ac:align="center" ac:layout="full-width" ac:width="1200">'
                f'<ri:attachment ri:filename="{html.escape(cover_file)}" />'
                f'</ac:image>'
                f'</p>'
                f'</div>\n'
            )

        metadata_header = self._render_metadata_header(node)

        if node.node_type == "database":
            main_body = cover_html + metadata_header + self._generate_database_overview_content(node)
        elif node.node_type == "database_row":
            main_body = cover_html + metadata_header + self._generate_database_row_content(node, db_url=db_url, entry_id=entry_id)
        else:
            main_body = cover_html + metadata_header + self.blocks_to_storage_format(node.blocks)

        # Task B.9: Confluence受信リンクマクロ・バックリンク一覧の自動配置
        backlinks_html = ""
        if incoming_links:
            items_html = []
            for lk in incoming_links:
                t = lk.get("title", "Untitled Page")
                u = lk.get("url")
                if u:
                    items_html.append(f'<li><a href="{html.escape(u)}">{html.escape(t)}</a></li>')
                else:
                    items_html.append(
                        f'<li><ac:link ac:card-appearance="inline">'
                        f'<ri:page ri:content-title="{html.escape(t)}" />'
                        f'</ac:link></li>'
                    )
            backlinks_html = (
                f'\n<hr />\n'
                f'<ac:structured-macro ac:name="expand">'
                f'<ac:parameter ac:name="title">🔗 このページへのリンク元 (受信リンク: {len(incoming_links)}件)</ac:parameter>'
                f'<ac:rich-text-body>'
                f'<p>このページは以下のページから参照されています:</p>'
                f'<ul>{"".join(items_html)}</ul>'
                f'</ac:rich-text-body>'
                f'</ac:structured-macro>'
            )

        return main_body + backlinks_html

    def _generate_database_overview_content(self, db_node: PageNode) -> str:
        """データベースをConfluenceページとして表現する際の概要テーブルを生成する。"""
        content_parts = [
            '<ac:structured-macro ac:name="info">'
            '<ac:rich-text-body>'
            f'<p>Notionデータベース <strong>{html.escape(db_node.title)}</strong> から移行されたデータ一覧です。'
            '各行の詳細は配下の子ページとして格納されています。</p>'
            '</ac:rich-text-body>'
            '</ac:structured-macro>'
        ]

        if not db_node.children:
            content_parts.append("<p>レコードが存在しません。</p>")
            return "\n".join(content_parts)

        # 全レコードのプロパティキーを収集
        first_row_props = db_node.children[0].properties
        title_col = (db_node.properties or {}).get("title_column")
        if not title_col:
            title_col = "タスク名" if "タスク名" in first_row_props else (next(iter(first_row_props.keys()), "名前") if first_row_props else "名前")
        keys = [title_col]
        for k in first_row_props.keys():
            if k not in keys:
                keys.append(k)

        # 1. Confluence 動的連動マクロ: Page Properties Report (detailssummary)
        # 子ページ（database_row）の details マクロと自動リアルタイム同期・連動
        headings_csv = ",".join(keys)
        report_macro = (
            '<h2>📊 動的連動テーブル (Page Properties Report)</h2>'
            '<p><em>💡 各行（子ページ）のプロパティを編集すると、この親テーブルも自動でリアルタイム連動・更新されます。</em></p>'
            '<ac:structured-macro ac:name="detailssummary">'
            '<ac:parameter ac:name="cql">parent = currentContent()</ac:parameter>'
            f'<ac:parameter ac:name="headings">{html.escape(headings_csv)}</ac:parameter>'
            '</ac:structured-macro>'
        )
        content_parts.append(report_macro)

        # 2. 静的スナップショットテーブル（エクスポートや即時読み込み用のバックアップ）
        table_html = [
            '<h2>📋 静的スナップショットテーブル</h2>',
            '<table class="wrapped"><thead><tr>'
        ]
        for k in keys:
            table_html.append(f'<th>{html.escape(k)}</th>')
        table_html.append('</tr></thead><tbody>')

        for row in db_node.children:
            table_html.append('<tr>')
            for k in keys:
                prop = row.properties.get(k)
                if k == "タスク名" or (k == keys[0]):
                    # Confluenceネイティブ子ページリンクオブジェクト（スマートリンク・プレビュー対応）
                    record_title = row.title or (prop.get("value") if isinstance(prop, dict) else str(prop or ""))
                    escaped_title = html.escape(record_title)
                    table_html.append(
                        f'<td><ac:link ac:card-appearance="inline">'
                        f'<ri:page ri:content-title="{escaped_title}" />'
                        f'<ac:link-body>{escaped_title}</ac:link-body>'
                        f'</ac:link></td>'
                    )
                else:
                    formatted_val = self.format_property_for_confluence(prop)
                    table_html.append(f'<td>{formatted_val}</td>')
            table_html.append('</tr>')

        table_html.append('</tbody></table>')
        content_parts.append("".join(table_html))
        return "\n".join(content_parts)

    def _render_metadata_header(self, node: PageNode) -> str:
        """Notionの作成者・作成日時・最終更新日時を記録したヘッダHTMLを生成する。
        self.metadata_template が空または "none" の場合は空文字列を返す。
        """
        if not self.metadata_template or self.metadata_template.strip() == "" or self.metadata_template.strip().lower() == "none":
            return ""

        # 各種メタデータ変数の解決
        author_mention = ""
        author_name = ""
        if getattr(node, "created_by", None):
            cb = node.created_by
            u_id = cb.get("id") if isinstance(cb, dict) else None
            u_name = cb.get("name") if isinstance(cb, dict) else ""
            u_email = cb.get("person", {}).get("email") if isinstance(cb, dict) and isinstance(cb.get("person"), dict) else None
            author_name = u_name or ""
            if self.user_mapper:
                author_mention = self.user_mapper.format_mention(user_id=u_id, email=u_email, name=u_name)
            else:
                author_mention = f"@{html.escape(u_name or 'ユーザー')}"

        last_editor_mention = ""
        last_editor_name = ""
        if getattr(node, "last_edited_by", None):
            leb = node.last_edited_by
            le_id = leb.get("id") if isinstance(leb, dict) else None
            le_name = leb.get("name") if isinstance(leb, dict) else ""
            le_email = leb.get("person", {}).get("email") if isinstance(leb, dict) and isinstance(leb.get("person"), dict) else None
            last_editor_name = le_name or ""
            if self.user_mapper:
                last_editor_mention = self.user_mapper.format_mention(user_id=le_id, email=le_email, name=le_name)
            else:
                last_editor_mention = f"@{html.escape(le_name or 'ユーザー')}"

        from src.timezone_util import format_iso_datetime
        created_time_str = ""
        created_date_str = ""
        if getattr(node, "created_time", None):
            created_time_str = format_iso_datetime(node.created_time, tz=self.timezone, fmt="%Y-%m-%d %H:%M")
            created_date_str = format_iso_datetime(node.created_time, tz=self.timezone, fmt="%Y-%m-%d")

        last_edited_time_str = ""
        last_edited_date_str = ""
        if getattr(node, "last_edited_time", None):
            last_edited_time_str = format_iso_datetime(node.last_edited_time, tz=self.timezone, fmt="%Y-%m-%d %H:%M")
            last_edited_date_str = format_iso_datetime(node.last_edited_time, tz=self.timezone, fmt="%Y-%m-%d")

        # もし作成者も作成日時も存在しないノードの場合はヘッダ不要
        if not getattr(node, "created_by", None) and not getattr(node, "created_time", None):
            return ""

        context = {
            "author": author_mention or "-",
            "created_by": author_mention or "-",
            "author_name": html.escape(author_name) if author_name else "-",
            "created_time": created_time_str or "-",
            "created_date": created_date_str or "-",
            "last_edited_by": last_editor_mention or "-",
            "last_edited_name": html.escape(last_editor_name) if last_editor_name else "-",
            "last_edited_time": last_edited_time_str or "-",
            "last_edited_date": last_edited_date_str or "-",
            "title": html.escape(node.title or ""),
            "url": html.escape(node.url or ""),
            "notion_url": html.escape(node.url or ""),
        }

        try:
            rendered = self.metadata_template.format(**context)
        except Exception as e:
            logger.warning(f"Failed to render metadata template ({self.metadata_template}): {e}")
            rendered = f"📄 Notion元情報 — 作成者: {context['author']} | 🕒 作成日時: {context['created_time']}"

        rendered = rendered.strip()
        if not rendered:
            return ""

        if not (rendered.startswith("<p") or rendered.startswith("<div")):
            return (
                f'<p style="color: #6B778C; font-size: 12px; margin-bottom: 16px; padding: 4px 8px; '
                f'background-color: #F4F5F7; border-radius: 4px; display: inline-block;">'
                f'{rendered}'
                f'</p>\n'
            )
        return f"{rendered}\n"

    def _generate_database_row_content(
        self,
        row_node: PageNode,
        db_url: Optional[str] = None,
        entry_id: Optional[str] = None,
    ) -> str:
        """データベース行の詳細ページ用コンテンツ（折りたたみ行ビュー・プロパティ＋本文）を生成する。"""
        parts = []

        entry_embed_url = getattr(row_node, "_entry_embed_url", None)
        if not entry_embed_url and db_url and entry_id:
            entry_embed_url = f"{db_url}?entryId={entry_id}"

        # 1. 親データベースへのナビゲーションリンク
        parent_db_title = getattr(row_node, "parent_db_title", None)
        parent_db_url = getattr(row_node, "parent_db_url", None) or db_url
        if parent_db_title:
            escaped_db_title = html.escape(parent_db_title)
            if parent_db_url:
                db_link_html = f'<a href="{html.escape(parent_db_url)}">{escaped_db_title}</a>'
            else:
                db_link_html = (
                    f'<ac:link ac:card-appearance="inline">'
                    f'<ri:page ri:content-title="{escaped_db_title}" />'
                    f'<ac:link-body>{escaped_db_title}</ac:link-body>'
                    f'</ac:link>'
                )
            nav_panel = (
                f'<p style="color: #42526E; font-size: 13px; margin-bottom: 12px;">'
                f'📁 <strong>所属データベース</strong>: {db_link_html}'
                f'</p>'
            )
            parts.append(nav_panel)

        # 2. プロパティ一覧テーブルのHTML生成
        prop_rows = []
        for k, prop in row_node.properties.items():
            formatted_val = self.format_property_for_confluence(prop)
            prop_rows.append(
                f'<tr><th style="width: 150px; background-color: #f4f5f7;">{html.escape(k)}</th><td>{formatted_val}</td></tr>'
            )
        details_table = ""
        if prop_rows:
            details_table = (
                f'<table class="wrapped"><tbody>{"".join(prop_rows)}</tbody></table>'
            )

        # 3. 折りたたみマクロ（Expand）内にネイティブ行ビューまたはフォールバックプロパティテーブルを格納
        #    （右側プレビュー領域での展開崩れを防止し、単独閲覧時にクリックして編集・確認可能にする）
        expand_body_parts = []
        if entry_embed_url:
            escaped_embed_url = html.escape(entry_embed_url)
            expand_body_parts.append(
                f'<p><a href="{escaped_embed_url}" data-layout="center" data-width="100.00" data-card-appearance="embed">{escaped_embed_url}</a></p>'
            )
        elif details_table:
            expand_body_parts.append(details_table)
        else:
            expand_body_parts.append("<p>レコードプロパティはありません。</p>")

        if expand_body_parts:
            expand_macro = (
                f'<ac:structured-macro ac:name="expand">'
                f'<ac:parameter ac:name="title">📋 レコードプロパティ・行編集 (クリックして展開)</ac:parameter>'
                f'<ac:rich-text-body>'
                f'{"".join(expand_body_parts)}'
                f'</ac:rich-text-body>'
                f'</ac:structured-macro>'
            )
            parts.append(expand_macro)

        # 4. 本文ブロック（ブロックが存在する場合のみ区切り線と本文を配置）
        if row_node.blocks:
            parts.append('<hr />')
            parts.append('<h2>📄 レコード詳細本文</h2>')
            parts.append(self.blocks_to_storage_format(row_node.blocks))

        return "\n".join(parts)
