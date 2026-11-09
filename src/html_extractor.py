"""
Notion 公式の HTML/CSV エクスポート (ZIP ファイル) から
ページ、ブロック階層、データベースおよびレコードを解析し、
PageNode ツリーを構築するオフライン抽出モジュール。
"""

import os
import re
import csv
import io
import uuid
import base64
import hashlib
import zipfile
import logging
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, Union
from bs4 import BeautifulSoup, NavigableString, Tag

from src.extractor import PageNode

logger = logging.getLogger(__name__)

UUID_PATTERN = re.compile(r"([0-9a-fA-F]{32})")


def normalize_id(raw_id: str) -> str:
    """32桁の16進数文字列をハイフン付きUUID形式に正規化する。"""
    cleaned = raw_id.strip().replace("-", "")
    match = UUID_PATTERN.search(cleaned)
    if match:
        cleaned = match.group(1).lower()
    if len(cleaned) == 32:
        return f"{cleaned[:8]}-{cleaned[8:12]}-{cleaned[12:16]}-{cleaned[16:20]}-{cleaned[20:]}"
    return raw_id.strip()


class HtmlZipExtractor:
    """NotionのHTMLエクスポートZIPファイルを解析してPageNodeツリーを構築するクラス。"""

    def __init__(self, zip_path: str, global_db_csv_map: Optional[Dict[str, str]] = None):
        self.zip_path = zip_path
        self.global_db_csv_map = global_db_csv_map or {}
        if not os.path.exists(zip_path):
            raise FileNotFoundError(f"Export zip file not found: {zip_path}")

    def extract_tree(self) -> PageNode:
        """ZIPファイル全体を解析し、ルートPageNodeを返す。"""
        logger.info(f"Opening and scanning Notion HTML export ZIP: {self.zip_path}")
        with zipfile.ZipFile(self.zip_path, "r") as zf:
            file_list = zf.namelist()

            # 1. HTMLファイルとCSVファイルをインデックス化
            html_files = [f for f in file_list if f.endswith(".html")]
            csv_files = [f for f in file_list if f.endswith(".csv")]

            # 2. ルートHTMLファイルを特定（パスのスラッシュが最も少ないファイル）
            root_candidates = sorted(html_files, key=lambda f: f.count("/"))
            if not root_candidates:
                raise ValueError("No HTML files found in ZIP archive.")
            root_file = root_candidates[0]
            logger.info(f"Identified root HTML file: {root_file}")

            # 3. データベースの特定: CSVファイルと同名(拡張子違い)のHTML、またはUUID一致するCSVをデータベースと判定
            db_csv_map = dict(self.global_db_csv_map)  # グローバルDBマップを引き継ぐ
            for c in csv_files:
                base_c = os.path.splitext(c)[0]
                db_id_match = UUID_PATTERN.findall(c)
                if db_id_match:
                    db_id = normalize_id(db_id_match[-1])
                    db_csv_map[db_id] = c
                # UUIDがない場合でも、拡張子を除いたファイルパスでマッピング
                db_csv_map[base_c] = c

            # 同名HTMLが存在する場合のマッピング補完
            for h in html_files:
                base_h = os.path.splitext(h)[0]
                if base_h in db_csv_map:
                    h_uid = UUID_PATTERN.findall(h)
                    if h_uid:
                        db_csv_map[normalize_id(h_uid[-1])] = db_csv_map[base_h]

            logger.info(f"Detected {len(db_csv_map)} database CSV mappings in ZIP.")

            # 4. 全ファイルからPageNodeを再帰的に構築
            root_node = self._build_page_node(zf, root_file, db_csv_map)
            return root_node

    def _build_page_node(
        self,
        zf: zipfile.ZipFile,
        html_file: str,
        db_csv_map: Dict[str, str],
        parent_dir: str = "",
    ) -> PageNode:
        """指定されたHTMLファイルをパースしてPageNodeを構築し、配下の子ページ・子DBを再帰的に紐付ける。"""
        raw_html = zf.read(html_file).decode("utf-8")
        soup = BeautifulSoup(raw_html, "html.parser")

        # ページIDの抽出 (ファイル名末尾の32桁16進数)
        uid_matches = UUID_PATTERN.findall(html_file)
        node_id = normalize_id(uid_matches[-1]) if uid_matches else "root"

        from src.icon_mapper import resolve_notion_icon_to_emoji

        # ページタイトル
        title = "Untitled Page"
        title_el = soup.find("title") or soup.find(class_="page-title")
        if title_el:
            title = title_el.text.strip()

        # アイコン (絵文字またはNotion標準SVGアイコンを解決)
        icon = None
        icon_meta = soup.find("meta", attrs={"name": "data-notion-page-icon"})
        if icon_meta and icon_meta.get("content"):
            icon = resolve_notion_icon_to_emoji(icon_meta["content"])
        if not icon:
            icon_span = soup.find("span", class_="icon")
            if icon_span and icon_span.get("data-emoji"):
                icon = resolve_notion_icon_to_emoji(icon_span["data-emoji"])
        if not icon:
            header_icon_div = soup.find("div", class_=lambda c: c and "page-header-icon" in c)
            if header_icon_div and header_icon_div.find("img"):
                icon = resolve_notion_icon_to_emoji(header_icon_div.find("img").get("src"))

        # カバー画像
        cover = None
        cover_img = soup.find("img", class_="page-cover-image")
        if cover_img and cover_img.get("src"):
            cover_src = cover_img["src"]
            cover = {"type": "external", "external": {"url": cover_src}}

        # ノードタイプの判定（データベースか通常ページか）
        is_database = node_id in db_csv_map
        node_type = "database" if is_database else "page"

        # 本文ブロックのパース
        blocks = []
        page_body = soup.find(class_="page-body")
        if page_body and not is_database:
            blocks = self._parse_elements_to_blocks(page_body, zf, os.path.dirname(html_file), db_csv_map=db_csv_map)

        comments = self._extract_page_comments(soup)

        # プロパティテーブルの抽出（存在する場合）
        page_properties = {}
        prop_table = soup.find("table", class_="properties")
        if prop_table:
            for tr in prop_table.find_all("tr"):
                th = tr.find("th")
                td = tr.find("td")
                if th and td:
                    p_name = th.get_text(strip=True)
                    p_val = td.get_text(strip=True)
                    page_properties[p_name] = {"type": "text", "value": p_val}

        node = PageNode(
            id=node_id,
            title=title,
            node_type=node_type,
            icon=icon,
            cover=cover,
            properties=page_properties,
            blocks=blocks,
            children=[],
            comments=comments,
        )

        # データベースの場合、CSVから各行レコードを抽出して子ノードとして追加
        if is_database:
            csv_path = db_csv_map[node_id]
            self._attach_database_rows(zf, node, csv_path, html_file)
            return node

        # 通常ページの場合、同名フォルダ内の子ページ/子データベースを走査
        # Notionエクスポートでは `{タイトル} {ID}.html` に対して `{タイトル}/` というフォルダ内に配下の子ファイルが格納される
        sub_dir = re.sub(r"\s+[0-9a-fA-F]{32}\.html$", "", html_file)
        if sub_dir == html_file:
            sub_dir = os.path.splitext(html_file)[0]
        sub_dir = sub_dir + "/"

        processed_bases = set()
        for f in zf.namelist():
            if f.startswith(sub_dir) and f != html_file:
                # 直下の子ファイルのみ（さらに深い孫フォルダのファイルは除外）
                relative = f[len(sub_dir) :]
                parts = relative.split("/")
                if len(parts) == 1:
                    base_f = os.path.splitext(f)[0]
                    if f.endswith(".html"):
                        child_node = self._build_page_node(zf, f, db_csv_map, parent_dir=sub_dir)
                        node.children.append(child_node)
                        processed_bases.add(base_f)
                    elif f.endswith(".csv"):
                        # 対応するHTMLが存在しない単独CSVデータベースの場合
                        if base_f not in processed_bases:
                            child_db_node = self._build_database_node_from_csv(zf, f)
                            node.children.append(child_db_node)
                            processed_bases.add(base_f)

        return node

    def _build_database_node_from_csv(self, zf: zipfile.ZipFile, csv_file: str) -> PageNode:
        """HTMLファイルが存在しない単独のCSVファイルから直接データベースPageNodeを構築する。"""
        filename = os.path.basename(csv_file)
        base_name = os.path.splitext(filename)[0]

        # タイトルとIDの抽出
        uid_matches = UUID_PATTERN.findall(filename)
        if uid_matches:
            db_id = normalize_id(uid_matches[-1])
            title = re.sub(r"\s+[0-9a-fA-F]{32}$", "", base_name).strip()
        else:
            db_id = str(uuid.uuid5(uuid.NAMESPACE_URL, csv_file))
            title = base_name.strip()

        db_node = PageNode(
            id=db_id,
            title=title or "Untitled Database",
            node_type="database",
            icon="📊",
            properties={"schema": {}},
            blocks=[],
            children=[],
        )

        # レコードの読み込み（仮想的なHTMLパスを渡して同名フォルダ内の行HTMLも探す）
        fake_html_path = os.path.splitext(csv_file)[0] + ".html"
        self._attach_database_rows(zf, db_node, csv_file, fake_html_path)
        logger.info(f"Built database PageNode from standalone CSV: [{db_node.title}] (ID: {db_id}, {len(db_node.children)} rows)")
        return db_node

    def _attach_database_rows(
        self,
        zf: zipfile.ZipFile,
        db_node: PageNode,
        csv_path: str,
        db_html_path: str,
    ):
        """CSVおよび同名ディレクトリ内の行レコードHTMLファイルを解析し、db_node.childrenに格納する。"""
        raw_csv = zf.read(csv_path).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw_csv))
        rows = list(reader)

        # 同名ディレクトリ内の各行HTMLファイルをインデックス化
        base_dir = re.sub(r"\s+[0-9a-fA-F]{32}\.html$", "", db_html_path) + "/"
        row_html_map = {}  # row_id -> html_path
        for f in zf.namelist():
            if f.startswith(base_dir) and f.endswith(".html"):
                m = UUID_PATTERN.findall(f)
                if m:
                    row_id = normalize_id(m[-1])
                    row_html_map[row_id] = f

        logger.info(f"Extracting {len(rows)} records from database CSV: [{db_node.title}]")

        for row in rows:
            # タスク名またはタイトル列
            row_title = row.get("タスク名") or row.get("名前") or row.get("Title") or row.get("Name")
            if not row_title:
                # 最初の列をタイトルとする
                row_title = next(iter(row.values()), "Untitled Record")

            # 行IDの特定（CSV値内のHTMLリンク、またはタイトル名マッチング）
            row_id = None
            row_html_file = None
            for col_val in row.values():
                m = UUID_PATTERN.findall(col_val)
                if m:
                    cand_id = normalize_id(m[-1])
                    if cand_id in row_html_map:
                        row_id = cand_id
                        row_html_file = row_html_map[cand_id]
                        break

            if not row_html_file:
                # タイトルによるファイル名マッチング
                for r_id, r_path in row_html_map.items():
                    if row_title in r_path:
                        row_id = r_id
                        row_html_file = r_path
                        break

            if not row_id:
                row_id = f"{db_node.id}_row_{len(db_node.children)}"

            # レコード本文ブロックおよびコメントの抽出
            row_blocks = []
            row_comments = []
            if row_html_file and row_html_file in zf.namelist():
                row_soup = BeautifulSoup(zf.read(row_html_file).decode("utf-8"), "html.parser")
                row_body = row_soup.find(class_="page-body")
                if row_body:
                    row_blocks = self._parse_elements_to_blocks(row_body, zf, os.path.dirname(row_html_file))
                row_comments = self._extract_page_comments(row_soup)

            # プロパティ辞書の整形
            row_properties = {}
            for col_name, val in row.items():
                if not val:
                    continue
                # リレーションのパース（例: "タイトル (フォルダ/ファイル名 00000000...html)"）
                rel_match = UUID_PATTERN.findall(val)
                if rel_match and ".html" in val:
                    target_id = normalize_id(rel_match[-1])
                    row_properties[col_name] = {
                        "type": "relation",
                        "relation": [{"id": target_id}],
                        "value": [{"id": target_id, "text": val.split("(")[0].strip()}],
                    }
                elif col_name in ("カテゴリ", "Tags", "Labels") and "," in val:
                    # 複数選択
                    items = [item.strip() for item in val.split(",") if item.strip()]
                    row_properties[col_name] = {
                        "type": "multi_select",
                        "multi_select": [{"name": item} for item in items],
                        "value": [{"name": item} for item in items],
                    }
                elif val in ("Yes", "No", "true", "false", "True", "False"):
                    # チェックボックス
                    is_checked = val in ("Yes", "true", "True")
                    row_properties[col_name] = {
                        "type": "checkbox",
                        "checkbox": is_checked,
                        "value": is_checked,
                    }
                else:
                    row_properties[col_name] = {
                        "type": "select",
                        "select": {"name": val},
                        "value": val,
                    }

            row_node = PageNode(
                id=row_id,
                title=row_title,
                node_type="database_row",
                properties=row_properties,
                blocks=row_blocks,
                children=[],
                comments=row_comments,
            )
            db_node.children.append(row_node)

    @staticmethod
    def _parse_data_uri(src: str) -> Optional[Tuple[bytes, str, str]]:
        """Data URI (data:image/...;base64,... または URLエンコード形式) を解析し、
        バイナリデータ、一意なファイル名、MIMEタイプを返す。
        """
        if not src or not src.startswith("data:image/"):
            return None
        header, sep, raw_data = src.partition(",")
        if not sep:
            return None

        parts = header[5:].split(";")
        mime_type = parts[0].strip().lower()
        is_base64 = any(p.strip().lower() == "base64" for p in parts[1:])

        try:
            if is_base64:
                clean_b64 = re.sub(r"\s+", "", raw_data)
                data_bytes = base64.b64decode(clean_b64)
            else:
                data_bytes = urllib.parse.unquote_to_bytes(raw_data)
        except Exception as e:
            logger.warning(f"Failed to decode data URI image: {e}")
            return None

        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "image/bmp": ".bmp",
        }
        ext = ext_map.get(mime_type.lower(), ".png")
        content_hash = hashlib.md5(data_bytes).hexdigest()[:10]
        filename = f"embedded_image_{content_hash}{ext}"
        return data_bytes, filename, mime_type

    def _read_zip_attachment_bytes(
        self, zf: zipfile.ZipFile, current_dir: str, src: str
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """ZIP内から相対パス指定されたファイルの実体バイナリを安全に探索・取得する。"""
        if not src or src.startswith(("http://", "https://", "data:")):
            return None, None

        clean_src = urllib.parse.unquote(src).replace("\\", "/").lstrip("/")
        cand1 = os.path.normpath(os.path.join(current_dir, clean_src)).replace("\\", "/")
        cand2 = clean_src
        basename = os.path.basename(clean_src)

        name_map = {f.replace("\\", "/"): f for f in zf.namelist()}

        target = None
        if cand1 in name_map:
            target = name_map[cand1]
        elif cand2 in name_map:
            target = name_map[cand2]
        else:
            # 末尾一致のフォールバック探索
            for norm_f, orig_f in name_map.items():
                if norm_f.endswith("/" + basename) or norm_f == basename:
                    target = orig_f
                    break

        if target:
            try:
                data = zf.read(target)
                return data, target
            except Exception as e:
                logger.warning(f"Error reading attachment from ZIP ({target}): {e}")
                return None, target

        return None, cand1

    def _extract_page_comments(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Notion HTMLエクスポートから最上位ページコメントを抽出する。"""
        comments = []
        for details in soup.find_all("details"):
            summary = details.find("summary")
            if not summary:
                continue
            stext = summary.get_text().strip()
            if stext in ("ページコメント", "Page comments", "Comments"):
                for li in details.find_all("li"):
                    user_span = li.find(class_="user")
                    author = "Notion User"
                    created_time = ""
                    if user_span:
                        b_tag = user_span.find("b")
                        if b_tag:
                            author = b_tag.get_text().strip()
                        else:
                            author = user_span.get_text().strip()
                        time_tag = user_span.find("time") or li.find("time")
                        if time_tag:
                            created_time = time_tag.get_text().strip()

                    text_divs = [d for d in li.find_all("div", recursive=False) if not d.find(class_="user")]
                    c_text = ""
                    if text_divs:
                        c_text = text_divs[0].get_text().strip()
                    elif li.find("div"):
                        all_divs = li.find_all("div")
                        if len(all_divs) >= 2:
                            c_text = all_divs[1].get_text().strip()

                    if c_text:
                        comments.append({
                            "created_by": {"name": author},
                            "created_time": created_time,
                            "rich_text": [{"type": "text", "text": {"content": c_text}, "plain_text": c_text}],
                        })
        return comments

    def _parse_elements_to_blocks(
        self,
        container: Tag,
        zf: zipfile.ZipFile,
        current_dir: str,
        db_csv_map: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """HTMLコンテナ配下の要素をNotion互換のブロック辞書リストに変換する。"""
        db_csv_map = db_csv_map or {}
        blocks = []

        for el in container.children:
            if not isinstance(el, Tag):
                continue

            tag = el.name.lower()
            el_id = el.get("id") or f"b_{len(blocks)}"
            classes = el.get("class", [])

            # 1. 見出し
            if tag == "h1":
                blocks.append({
                    "id": el_id,
                    "type": "heading_1",
                    "heading_1": {"rich_text": self._parse_inline_rich_text(el), "is_toggleable": False},
                })
            elif tag == "h2":
                blocks.append({
                    "id": el_id,
                    "type": "heading_2",
                    "heading_2": {"rich_text": self._parse_inline_rich_text(el), "is_toggleable": False},
                })
            elif tag == "h3":
                blocks.append({
                    "id": el_id,
                    "type": "heading_3",
                    "heading_3": {"rich_text": self._parse_inline_rich_text(el), "is_toggleable": False},
                })

            # 2. 段落
            elif tag == "p":
                blocks.append({
                    "id": el_id,
                    "type": "paragraph",
                    "paragraph": {"rich_text": self._parse_inline_rich_text(el)},
                })

            # 3. 箇条書きリスト
            elif tag == "ul" and "bulleted-list" in classes:
                for li in el.find_all("li", recursive=False):
                    child_blocks = []
                    indented = li.find(class_="indented")
                    if indented:
                        child_blocks = self._parse_elements_to_blocks(indented, zf, current_dir, db_csv_map=db_csv_map)
                    blocks.append({
                        "id": el_id,
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": self._parse_inline_rich_text(li, skip_classes=["indented"])},
                        "children_blocks": child_blocks,
                        "has_children": bool(child_blocks),
                    })

            # 4. 番号付きリスト
            elif tag == "ol" and "numbered-list" in classes:
                for li in el.find_all("li", recursive=False):
                    child_blocks = []
                    indented = li.find(class_="indented")
                    if indented:
                        child_blocks = self._parse_elements_to_blocks(indented, zf, current_dir, db_csv_map=db_csv_map)
                    blocks.append({
                        "id": el_id,
                        "type": "numbered_list_item",
                        "numbered_list_item": {"rich_text": self._parse_inline_rich_text(li, skip_classes=["indented"])},
                        "children_blocks": child_blocks,
                        "has_children": bool(child_blocks),
                    })

            # 5. ToDoリスト
            elif tag == "ul" and "to-do-list" in classes:
                for li in el.find_all("li", recursive=False):
                    inp = li.find("input", type="checkbox")
                    is_checked = False
                    if inp:
                        is_checked = inp.has_attr("checked") or "checkbox-on" in inp.get("class", [])

                    child_blocks = []
                    indented = li.find(class_="indented")
                    if indented:
                        child_blocks = self._parse_elements_to_blocks(indented, zf, current_dir, db_csv_map=db_csv_map)

                    blocks.append({
                        "id": el_id,
                        "type": "to_do",
                        "to_do": {
                            "rich_text": self._parse_inline_rich_text(li, skip_classes=["indented", "checkbox"]),
                            "checked": is_checked,
                        },
                        "children_blocks": child_blocks,
                        "has_children": bool(child_blocks),
                    })

            # 6. トグル
            elif tag == "details":
                summary = el.find("summary")
                summary_text = self._parse_inline_rich_text(summary) if summary else []
                child_blocks = []
                indented = el.find(class_="indented")
                if indented:
                    child_blocks = self._parse_elements_to_blocks(indented, zf, current_dir, db_csv_map=db_csv_map)
                else:
                    # summary 以外の要素
                    content_div = soup_tag = Tag(name="div")
                    for c in el.children:
                        if c != summary and isinstance(c, Tag):
                            content_div.append(c)
                    child_blocks = self._parse_elements_to_blocks(content_div, zf, current_dir, db_csv_map=db_csv_map)

                blocks.append({
                    "id": el_id,
                    "type": "toggle",
                    "toggle": {"rich_text": summary_text},
                    "children_blocks": child_blocks,
                    "has_children": bool(child_blocks),
                })

            # 7. 引用
            elif tag == "blockquote":
                child_blocks = []
                # 内部にpやリスト等の子要素がある場合
                child_tags = [c for c in el.children if isinstance(c, Tag)]
                if child_tags and any(t.name in ("p", "ul", "ol", "blockquote") for t in child_tags):
                    child_blocks = self._parse_elements_to_blocks(el, zf, current_dir, db_csv_map=db_csv_map)
                blocks.append({
                    "id": el_id,
                    "type": "quote",
                    "quote": {"rich_text": self._parse_inline_rich_text(el)},
                    "children_blocks": child_blocks,
                    "has_children": bool(child_blocks),
                })

            # 8. コールアウト
            elif tag == "aside" or "callout" in classes:
                from src.icon_mapper import resolve_notion_icon_to_emoji
                bg_color = el.get("data-notion-callout-background") or "default"
                if "block-color-" in str(classes):
                    for c in classes:
                        if c.startswith("block-color-"):
                            bg_color = c.replace("block-color-", "")
                            break

                icon_val = el.get("data-notion-callout-icon")
                if not icon_val:
                    icon_el = el.find("span", class_="icon")
                    if icon_el and icon_el.get("data-emoji"):
                        icon_val = icon_el["data-emoji"]
                if not icon_val:
                    icon_img = el.find("img", class_="icon")
                    if icon_img and icon_img.get("src"):
                        icon_val = icon_img["src"]

                resolved_emoji = resolve_notion_icon_to_emoji(icon_val) if icon_val else None

                blocks.append({
                    "id": el_id,
                    "type": "callout",
                    "callout": {
                        "rich_text": self._parse_inline_rich_text(el, skip_classes=["icon"]),
                        "icon": {"type": "emoji", "emoji": resolved_emoji} if resolved_emoji else None,
                        "color": bg_color,
                    },
                })

            # 9. コードブロック
            elif tag == "pre" and ("code" in classes or el.find("code")):
                code_el = el.find("code") or el
                lang = el.get("data-notion-code-syntax")
                if not lang:
                    for c in code_el.get("class", []):
                        if c.startswith("language-"):
                            lang = c.replace("language-", "")
                            break
                lang = lang or "plain text"
                code_text = code_el.text

                # Mermaid構文の自動検知 (Task B.8)
                if lang.lower() in ("plain text", "text", ""):
                    from src.converter import NotionToConfluenceConverter
                    is_mermaid, _ = NotionToConfluenceConverter.is_mermaid_code(lang, code_text)
                    if is_mermaid:
                        lang = "mermaid"

                blocks.append({
                    "id": el_id,
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": code_text}, "plain_text": code_text}],
                        "language": lang,
                    },
                })

            # 10. 数式 (Equation)
            elif (tag == "figure" and "equation" in classes) or el.get("data-notion-equation"):
                expr = el.get("data-notion-equation") or el.text.strip()
                blocks.append({
                    "id": el_id,
                    "type": "equation",
                    "equation": {"expression": expr},
                })

            # 11. シンプルテーブル
            elif tag == "table" and ("simple-table" in classes or "table" in classes):
                has_col_header = bool(el.find("thead") or el.find("th"))
                table_rows = []
                for tr in el.find_all("tr"):
                    row_cells = []
                    for td in tr.find_all(["td", "th"]):
                        row_cells.append(self._parse_inline_rich_text(td))
                    table_rows.append({
                        "type": "table_row",
                        "table_row": {"cells": row_cells},
                    })
                blocks.append({
                    "id": el_id,
                    "type": "table",
                    "table": {
                        "table_width": len(table_rows[0]["table_row"]["cells"]) if table_rows else 0,
                        "has_column_header": has_col_header,
                        "has_row_header": False,
                    },
                    "children_blocks": table_rows,
                })

            # 12. 区切り線
            elif tag == "hr":
                blocks.append({"id": el_id, "type": "divider", "divider": {}})

            # 12-b. 目次 (Table of Contents)
            elif tag == "nav" or "table-of-contents" in classes or "table_of_contents" in classes:
                blocks.append({
                    "id": el_id,
                    "type": "table_of_contents",
                    "table_of_contents": {},
                })

            # 12-c. マルチコラム (Multi-columns / column_list)
            elif "column-list" in classes or "column_list" in classes:
                col_blocks = []
                col_divs = el.find_all(class_=lambda c: c and ("column" in c and "column-list" not in c), recursive=False)
                if not col_divs:
                    col_divs = el.find_all(class_="column")
                for c_idx, c_div in enumerate(col_divs):
                    c_id = c_div.get("id") or f"{el_id}_col_{c_idx}"
                    c_children = self._parse_elements_to_blocks(c_div, zf, current_dir, db_csv_map=db_csv_map)
                    col_blocks.append({
                        "id": c_id,
                        "type": "column",
                        "column": {},
                        "children_blocks": c_children,
                    })
                blocks.append({
                    "id": el_id,
                    "type": "column_list",
                    "column_list": {},
                    "children_blocks": col_blocks,
                })

            # 13. データベースビュー (collection-content)
            elif "collection-content" in classes or el.find(class_="collection-title"):
                db_title = "Untitled Database"
                title_el = el.find(class_="collection-title")
                if title_el:
                    db_title = title_el.text.strip()

                # 直前の見出しからビュータイプやグループ化を推測
                prev_text = ""
                heading_sibling = el.find_previous_sibling(["h2", "h3", "h1"])
                if heading_sibling:
                    prev_text = heading_sibling.text.strip()
                elif el.find_previous_sibling(["p"]):
                    prev_text = el.find_previous_sibling(["p"]).text.strip()

                view_type = "table"
                group_field = None
                if "カンバン" in prev_text or "Board" in prev_text or "board" in prev_text:
                    view_type = "board"
                    group_field = "優先度"
                elif "ギャラリー" in prev_text or "Gallery" in prev_text:
                    view_type = "gallery"
                elif "グループ化" in prev_text or "Group by" in prev_text:
                    view_type = "table"
                    group_field = "ステータス"

                view_info = {
                    "name": prev_text or db_title,
                    "type": view_type,
                    "configuration": {"group_by": {"property_name": group_field} if group_field else None},
                }

                blocks.append({
                    "id": el_id,
                    "type": "child_database",
                    "is_linked_database": True,
                    "data_sources": [{"name": db_title}],
                    "views": [view_info],
                })

            # 14. 画像 / 子ページリンク / 外部ファイル・動画・音声・埋め込み
            elif tag == "figure":
                a_tag = el.find("a")
                href = a_tag.get("href") if a_tag else ""
                img_tag = el.find("img")
                figcaption = el.find("figcaption")
                caption_text = figcaption.text.strip() if figcaption else ""
                caption_rich = [{"type": "text", "text": {"content": caption_text}}] if caption_text else []

                # 子ページリンク判定
                # 注: 子ページアイコンとして <img> (例: airplane_purple.svg) が内包されるため、画像ブロック判定より優先して評価する
                is_subpage_link = "link-to-page" in classes or (
                    a_tag and (a_tag.get("data-notion-page-id") or (href.endswith(".html") and "image" not in classes))
                )

                if is_subpage_link:
                    p_title = a_tag.get_text().strip() if a_tag else "Subpage"
                    target_id = None
                    if a_tag:
                        page_id_attr = a_tag.get("data-notion-page-id")
                        if page_id_attr:
                            target_id = normalize_id(page_id_attr)
                        else:
                            m = UUID_PATTERN.findall(href)
                            if m:
                                target_id = normalize_id(m[-1])

                    is_db = target_id in db_csv_map if target_id else False
                    if is_db:
                        blocks.append({
                            "id": target_id or el_id,
                            "type": "child_database",
                            "is_linked_database": False,
                            "database_title": p_title,
                            "child_database": {"title": p_title},
                        })
                    else:
                        blocks.append({
                            "id": target_id or el_id,
                            "type": "child_page",
                            "child_page": {"title": p_title},
                        })

                # 画像ブロック (figure.image または内部にimgタグを含む場合)
                elif "image" in classes or img_tag:
                    src = img_tag.get("src") if img_tag else href
                    if src:
                        data_uri_info = self._parse_data_uri(src)
                        if data_uri_info:
                            data_bytes, filename, mime = data_uri_info
                            block_obj = {
                                "id": el_id,
                                "type": "image",
                                "image": {
                                    "type": "file",
                                    "caption": caption_rich,
                                    "external": None,
                                    "file": {"url": filename},
                                },
                                "_attachment_filename": filename,
                                "_attachment_bytes": data_bytes,
                            }
                            logger.info(f"Loaded inline base64/data URI image: {filename} ({len(data_bytes)} bytes)")
                            blocks.append(block_obj)
                        else:
                            is_external = src.startswith(("http://", "https://"))
                            img_filename = os.path.basename(urllib.parse.unquote(src))
                            block_obj = {
                                "id": el_id,
                                "type": "image",
                                "image": {
                                    "type": "external" if is_external else "file",
                                    "caption": caption_rich,
                                    "external": {"url": src} if is_external else None,
                                    "file": {"url": src} if not is_external else None,
                                },
                                "_attachment_filename": img_filename,
                            }
                            if not is_external:
                                # ZIP内相対パスから画像実体バイナリを取得
                                data, resolved_path = self._read_zip_attachment_bytes(zf, current_dir, src)
                                if data:
                                    block_obj["_attachment_bytes"] = data
                                    logger.info(f"Loaded inline image attachment from ZIP: {resolved_path} ({len(data)} bytes)")
                                else:
                                    logger.warning(f"Image attachment not found in ZIP: src='{src}', resolved='{resolved_path}'")
                            blocks.append(block_obj)
                elif href:
                    lower_href = href.lower()
                    if lower_href.endswith(".pdf"):
                        blocks.append({
                            "id": el_id,
                            "type": "pdf",
                            "pdf": {"type": "external", "external": {"url": href}},
                        })
                    elif lower_href.endswith((".mp3", ".wav", ".m4a", ".aac")):
                        blocks.append({
                            "id": el_id,
                            "type": "audio",
                            "audio": {"type": "external", "external": {"url": href}},
                        })
                    elif lower_href.endswith((".mp4", ".mov", ".webm")) or "youtube.com" in lower_href or "youtu.be" in lower_href:
                        blocks.append({
                            "id": el_id,
                            "type": "video",
                            "video": {"type": "external", "external": {"url": href}},
                        })
                    elif "bookmark" in classes or el.find(class_="bookmark"):
                        blocks.append({
                            "id": el_id,
                            "type": "bookmark",
                            "bookmark": {"url": href},
                        })
                    elif any(svc in lower_href for svc in ("figma.com", "maps.google", "codepen.io")):
                        blocks.append({
                            "id": el_id,
                            "type": "embed",
                            "embed": {"url": href},
                        })
                    else:
                        blocks.append({
                            "id": el_id,
                            "type": "file",
                            "file": {"type": "external", "external": {"url": href}},
                        })

            # 15. 単独のimgタグ
            elif tag == "img":
                src = el.get("src")
                if src:
                    data_uri_info = self._parse_data_uri(src)
                    if data_uri_info:
                        data_bytes, filename, mime = data_uri_info
                        block_obj = {
                            "id": el_id,
                            "type": "image",
                            "image": {
                                "type": "file",
                                "caption": [],
                                "external": None,
                                "file": {"url": filename},
                            },
                            "_attachment_filename": filename,
                            "_attachment_bytes": data_bytes,
                        }
                        logger.info(f"Loaded inline base64/data URI image: {filename} ({len(data_bytes)} bytes)")
                        blocks.append(block_obj)
                    else:
                        is_external = src.startswith(("http://", "https://"))
                        img_filename = os.path.basename(urllib.parse.unquote(src))
                        block_obj = {
                            "id": el_id,
                            "type": "image",
                            "image": {
                                "type": "external" if is_external else "file",
                                "caption": [],
                                "external": {"url": src} if is_external else None,
                                "file": {"url": src} if not is_external else None,
                            },
                            "_attachment_filename": img_filename,
                        }
                        if not is_external:
                            data, resolved_path = self._read_zip_attachment_bytes(zf, current_dir, src)
                            if data:
                                block_obj["_attachment_bytes"] = data
                                logger.info(f"Loaded inline image attachment from ZIP: {resolved_path} ({len(data)} bytes)")
                            else:
                                logger.warning(f"Image attachment not found in ZIP: src='{src}', resolved='{resolved_path}'")
                        blocks.append(block_obj)

            # 16. その他・未知のNotion固有要素 (例: breadcrumb 等)
            else:
                # テキストが存在していれば段落として拾う
                el_text = el.text.strip()
                if el_text:
                    blocks.append({
                        "id": el_id,
                        "type": "paragraph",
                        "paragraph": {"rich_text": self._parse_inline_rich_text(el)},
                    })

        return blocks

    def _parse_inline_rich_text(
        self,
        node: Tag,
        skip_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """HTMLタグ配下のインラインテキスト（太字・斜体・コード・リンク等）をNotion rich_text形式にパースする。"""
        skip_classes = skip_classes or []
        rich_texts = []

        def walk(el: Any, marks: Dict[str, Any]):
            for child in el.children:
                if isinstance(child, NavigableString):
                    text = str(child)
                    if not text:
                        continue
                    rich_texts.append({
                        "type": "text",
                        "text": {"content": text, "link": {"url": marks.get("link")} if marks.get("link") else None},
                        "plain_text": text,
                        "annotations": {
                            "bold": marks.get("bold", False),
                            "italic": marks.get("italic", False),
                            "strikethrough": marks.get("strikethrough", False),
                            "underline": marks.get("underline", False),
                            "code": marks.get("code", False),
                            "color": marks.get("color", "default"),
                        },
                    })
                elif isinstance(child, Tag):
                    child_classes = child.get("class", [])
                    if any(sc in child_classes for sc in skip_classes):
                        continue

                    new_marks = dict(marks)
                    c_tag = child.name.lower()
                    if c_tag in ("b", "strong"):
                        new_marks["bold"] = True
                    elif c_tag in ("i", "em"):
                        new_marks["italic"] = True
                    elif c_tag in ("s", "del", "strike"):
                        new_marks["strikethrough"] = True
                    elif c_tag == "u":
                        new_marks["underline"] = True
                    elif c_tag == "code":
                        new_marks["code"] = True
                    elif c_tag == "a":
                        new_marks["link"] = child.get("href")
                    elif c_tag == "mark":
                        hl = child.get("data-notion-highlight")
                        if hl:
                            new_marks["color"] = hl
                    walk(child, new_marks)

        walk(node, {})
        return rich_texts


class MultiZipExtractor:
    """複数のNotion HTML/CSVエクスポートZIPファイルを並行解析し、統合PageNodeツリーを構築するクラス。"""

    def __init__(self, zip_paths: List[str], root_title: str = "【統合移行】マルチブロック移行ルート"):
        self.zip_paths = [p for p in zip_paths if os.path.exists(p)]
        self.root_title = root_title
        if not self.zip_paths:
            raise FileNotFoundError("有効なZIPファイルが1つも指定されていません。")

    def extract_tree(self) -> PageNode:
        """全ZIPファイルを並行パースし、相互依存関係を内包する単一の統合PageNodeツリーを返す。"""
        if len(self.zip_paths) == 1:
            logger.info("単一ZIPファイルが指定されたため、通常のHtmlZipExtractorを実行します。")
            return HtmlZipExtractor(self.zip_paths[0]).extract_tree()

        logger.info(f"🚀 複数エクスポートZIPの並行抽出開始: {len(self.zip_paths)} ファイル")

        # 1. 全ZIPを事前スキャンし、グローバルなDB CSVマップを構築
        global_db_csv_map: Dict[str, str] = {}
        for z_path in self.zip_paths:
            try:
                with zipfile.ZipFile(z_path, "r") as zf:
                    for f in zf.namelist():
                        if f.endswith(".csv"):
                            db_id_match = UUID_PATTERN.findall(f)
                            if db_id_match:
                                db_id = normalize_id(db_id_match[-1])
                                global_db_csv_map[db_id] = f
                            global_db_csv_map[os.path.splitext(f)[0]] = f
            except Exception as e:
                logger.warning(f"ZIP事前スキャン警告 ({z_path}): {e}")

        logger.info(f"検出されたグローバルDB定義数: {len(global_db_csv_map)} 件")

        # 2. 各ZIPを並行してアンパック＆PageNode構築
        def _parse_single_zip(z_path: str) -> PageNode:
            extractor = HtmlZipExtractor(z_path, global_db_csv_map=global_db_csv_map)
            return extractor.extract_tree()

        block_nodes: List[PageNode] = []
        with ThreadPoolExecutor(max_workers=min(4, len(self.zip_paths))) as executor:
            futures = [executor.submit(_parse_single_zip, zp) for zp in self.zip_paths]
            for fut in futures:
                block_node = fut.result()
                block_nodes.append(block_node)

        # ブロックノードをタイトル順にソート（Block A, Block B...）
        block_nodes.sort(key=lambda n: n.title)

        logger.info(f"全 {len(block_nodes)} ブロックの並行抽出が完了しました:")
        for idx, bn in enumerate(block_nodes, 1):
            logger.info(f"  [{idx}] {bn.title} (ID: {bn.id}, 子要素数: {len(bn.children)})")

        # 3. 仮想統合ルートPageNodeを構築
        summary_blocks = [
            {
                "id": "root_callout",
                "type": "callout",
                "callout": {
                    "icon": {"type": "emoji", "emoji": "🌐"},
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": f"このページツリーは、分割エクスポートされた複数のZIPファイル（{len(self.zip_paths)}ブロック・計100ページ規模）から統合移行されたルートページです。"
                            },
                        }
                    ],
                },
            },
            {
                "id": "root_h2",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "移行ブロック一覧"}}],
                    "is_toggleable": False,
                },
            },
        ]

        for bn in block_nodes:
            summary_blocks.append({
                "id": f"link_{bn.id}",
                "type": "child_page",
                "child_page": {"title": bn.title},
            })

        combined_root = PageNode(
            id="multi_block_root_page",
            title=self.root_title,
            node_type="page",
            properties={},
            blocks=summary_blocks,
            children=block_nodes,
        )

        return combined_root
