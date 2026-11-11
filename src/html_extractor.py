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

UUID_PATTERN = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})(?![0-9a-fA-F])"
)


def extract_uuids(text: str) -> List[str]:
    """URLエンコードされた文字列等から安全に32桁UUIDを抽出する（%20等の16進数衝突を防止）。"""
    if not text:
        return []
    unquoted = urllib.parse.unquote(str(text))
    matches = UUID_PATTERN.findall(unquoted)
    if not matches and unquoted != text:
        matches = UUID_PATTERN.findall(str(text))
    return matches


def normalize_id(raw_id: str) -> str:
    """32桁の16進数文字列をハイフン付きUUID形式に正規化する。"""
    if not raw_id:
        return ""
    unquoted = urllib.parse.unquote(str(raw_id)).strip()
    matches = UUID_PATTERN.findall(unquoted)
    if matches:
        cleaned = matches[-1].replace("-", "").lower()
    else:
        cleaned = unquoted.replace("-", "").lower()
    if len(cleaned) == 32 and all(c in "0123456789abcdef" for c in cleaned):
        return f"{cleaned[:8]}-{cleaned[8:12]}-{cleaned[12:16]}-{cleaned[16:20]}-{cleaned[20:]}"
    return raw_id.strip()


_MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_TIME_SUFFIX_RE = r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM|am|pm))?(?:\s*\(?[A-Za-z0-9_+:\-\/]+\)?)?)?$"


def _parse_date_string(val: str) -> Optional[str]:
    """NotionのCSV/HTMLに出現する日付文字列を YYYY-MM-DD 形式に変換する。日付でない場合は None を返す。"""
    if not val or not isinstance(val, str):
        return None
    s = val.strip().lstrip("@").strip()
    if not s or "\n" in s or "\r" in s:
        return None

    # 範囲表記 (→, ->, 〜) がある場合は開始日部分を抽出
    start_part = re.split(r"\s*(?:→|->|〜)\s*", s)[0].strip()
    if not start_part:
        return None

    # 1. YYYY-MM-DD または YYYY/MM/DD (ISO日時 2026-11-10T14:30... も含む)
    m_iso = re.match(
        r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?(?:\s*(?:AM|PM|am|pm))?(?:\s*\(?[A-Za-z0-9_+:\-\/]+\)?)?)?$",
        start_part,
    )
    if m_iso:
        y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            datetime(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None

    # 2. YYYY年M月D日 (曜日や時刻が付随する場合も含む)
    m_jp = re.match(
        r"^(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日(?:\s*[\(（][^\)）]{1,5}[\)）])?" + _TIME_SUFFIX_RE,
        start_part,
    )
    if m_jp:
        y, mo, d = int(m_jp.group(1)), int(m_jp.group(2)), int(m_jp.group(3))
        try:
            datetime(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None

    # 3. Month D, YYYY / Mon D, YYYY (例: November 10, 2026 / Nov 10, 2026 10:00 AM)
    m_en1 = re.match(
        r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})" + _TIME_SUFFIX_RE,
        start_part,
    )
    if m_en1:
        mo_name = m_en1.group(1).lower()
        if mo_name in _MONTH_MAP:
            mo = _MONTH_MAP[mo_name]
            d = int(m_en1.group(2))
            y = int(m_en1.group(3))
            try:
                datetime(y, mo, d)
                return f"{y:04d}-{mo:02d}-{d:02d}"
            except ValueError:
                return None

    # 4. D Month YYYY / D Mon YYYY (例: 10 November 2026)
    m_en2 = re.match(
        r"^(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})" + _TIME_SUFFIX_RE,
        start_part,
    )
    if m_en2:
        mo_name = m_en2.group(2).lower()
        if mo_name in _MONTH_MAP:
            d = int(m_en2.group(1))
            mo = _MONTH_MAP[mo_name]
            y = int(m_en2.group(3))
            try:
                datetime(y, mo, d)
                return f"{y:04d}-{mo:02d}-{d:02d}"
            except ValueError:
                return None

    return None


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
            # Note: Notionエクスポートでは `{DB名} {UUID}.csv` と `{DB名} {UUID}_all.csv` の両方が出力される場合があるため、
            # `_all.csv` よりも通常の `.csv` を優先してマッピングする。
            db_csv_map = dict(self.global_db_csv_map)  # グローバルDBマップを引き継ぐ
            sorted_csv_files = sorted(csv_files, key=lambda x: (1 if x.endswith("_all.csv") else 0, x))
            for c in sorted_csv_files:
                base_c = os.path.splitext(c)[0]
                clean_base_c = re.sub(r"_all$", "", base_c)
                db_id_match = extract_uuids(c)
                if db_id_match:
                    db_id = normalize_id(db_id_match[-1])
                    if db_id not in db_csv_map or not c.endswith("_all.csv"):
                        db_csv_map[db_id] = c
                # UUIDがない場合でも、拡張子を除いたファイルパスでマッピング
                if base_c not in db_csv_map or not c.endswith("_all.csv"):
                    db_csv_map[base_c] = c
                if clean_base_c not in db_csv_map or not c.endswith("_all.csv"):
                    db_csv_map[clean_base_c] = c

            # 同名HTMLが存在する場合のマッピング補完
            for h in html_files:
                base_h = os.path.splitext(h)[0]
                if base_h in db_csv_map:
                    h_uid = extract_uuids(h)
                    if h_uid:
                        db_csv_map[normalize_id(h_uid[-1])] = db_csv_map[base_h]

            logger.info(f"Detected {len(db_csv_map)} database CSV mappings in ZIP.")

            # 4. 全ファイルからPageNodeを再帰的に構築
            root_node = self._build_page_node(zf, root_file, db_csv_map)
            return root_node

    def _extract_icon_from_soup(self, soup: BeautifulSoup) -> Optional[str]:
        """BeautifulSoupオブジェクトからNotionのページアイコン（絵文字またはNotion標準SVG）を抽出する。"""
        from src.icon_mapper import resolve_notion_icon_to_emoji, is_unicode_emoji

        # 1. meta[name=data-notion-page-icon] または link[rel=icon]
        icon_meta = soup.find("meta", attrs={"name": "data-notion-page-icon"})
        if icon_meta and icon_meta.get("content"):
            icon = resolve_notion_icon_to_emoji(icon_meta["content"])
            if icon:
                return icon

        icon_link = soup.find("link", attrs={"rel": lambda r: r and "icon" in (r if isinstance(r, list) else [r])})
        if icon_link and icon_link.get("href"):
            icon = resolve_notion_icon_to_emoji(icon_link["href"])
            if icon:
                return icon

        # 2. article または div.page の data-notion-page-icon / data-icon 属性
        page_art = (
            soup.find("article", class_=lambda c: c and "page" in c)
            or soup.find("article")
            or soup.find(class_="page")
        )
        if page_art:
            for attr_name in ("data-notion-page-icon", "data-icon", "data-emoji"):
                if page_art.get(attr_name):
                    icon = resolve_notion_icon_to_emoji(page_art[attr_name])
                    if icon:
                        return icon

        # 3. page-header-icon コンテナ内の探索
        header_icon_div = soup.find(class_=lambda c: c and "page-header-icon" in c)
        if header_icon_div:
            for attr_name in ("data-notion-page-icon", "data-icon", "data-emoji"):
                if header_icon_div.get(attr_name):
                    icon = resolve_notion_icon_to_emoji(header_icon_div[attr_name])
                    if icon:
                        return icon
            img = header_icon_div.find("img")
            if img:
                for attr_name in ("src", "data-src", "alt"):
                    if img.get(attr_name):
                        icon = resolve_notion_icon_to_emoji(img[attr_name])
                        if icon:
                            return icon
            icon_span = header_icon_div.find("span", class_="icon") or header_icon_div.find("span")
            if icon_span:
                for attr_name in ("data-emoji", "data-icon", "aria-label"):
                    if icon_span.get(attr_name):
                        icon = resolve_notion_icon_to_emoji(icon_span[attr_name])
                        if icon:
                            return icon
                txt = icon_span.get_text(strip=True)
                if txt:
                    icon = resolve_notion_icon_to_emoji(txt)
                    if icon:
                        return icon
            txt = header_icon_div.get_text(strip=True)
            if txt:
                icon = resolve_notion_icon_to_emoji(txt)
                if icon:
                    return icon

        # 4. header コンテナ内の探索 (プロパティテーブル内 .property-icon は除外)
        header = soup.find("header")
        if header:
            for img in header.find_all("img"):
                if "page-cover-image" in (img.get("class") or []):
                    continue
                if img.find_parent("table", class_="properties"):
                    continue
                for attr_name in ("src", "alt"):
                    if img.get(attr_name):
                        icon = resolve_notion_icon_to_emoji(img[attr_name])
                        if icon:
                            return icon
            for icon_span in header.find_all("span", class_="icon"):
                if "property-icon" in (icon_span.get("class") or []) or icon_span.find_parent("table", class_="properties"):
                    continue
                if icon_span.get("data-emoji"):
                    icon = resolve_notion_icon_to_emoji(icon_span["data-emoji"])
                    if icon:
                        return icon
                txt = icon_span.get_text(strip=True)
                if txt:
                    icon = resolve_notion_icon_to_emoji(txt)
                    if icon:
                        return icon

        return None

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

        # ページタイトル
        title = "Untitled Page"
        title_el = soup.find("title") or soup.find(class_="page-title")
        if title_el:
            title = title_el.text.strip()
        # タイトル末尾に Notion のリンクアーティファクト " (パス/ファイル名.html)" が混入している場合は除去
        title = re.sub(r"\s*\([^)]*\.html\)$", "", title).strip()
        title = urllib.parse.unquote(title)

        # アイコン (絵文字またはNotion標準SVGアイコンを解決)
        icon = self._extract_icon_from_soup(soup)

        # カバー画像
        cover = None
        cover_img = soup.find("img", class_="page-cover-image")
        if cover_img and cover_img.get("src"):
            cover_src = cover_img["src"]
            cover = {"type": "external", "external": {"url": cover_src}}

        # ノードタイプの判定（データベースか通常ページか）
        is_database = node_id in db_csv_map
        node_type = "database" if is_database else "page"
        if is_database:
            if not icon:
                icon = "🗄️"
            if title in ("Untitled Page", "Untitled", ""):
                title = "Untitled Database"

        # 本文ブロックのパース
        blocks = []
        page_body = soup.find(class_="page-body")
        if page_body and not is_database:
            blocks = self._parse_elements_to_blocks(page_body, zf, os.path.dirname(html_file), db_csv_map=db_csv_map)

        comments = self._extract_page_comments(soup)

        # プロパティテーブルの抽出（存在する場合）
        prop_table = soup.find("table", class_="properties")
        page_properties = self._extract_properties_from_table(prop_table)

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

        title = re.sub(r"\s*\([^)]*\.html\)$", "", title).strip()
        title = urllib.parse.unquote(title)

        db_node = PageNode(
            id=db_id,
            title=title or "Untitled Database",
            node_type="database",
            icon="🗄️",
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
        clean_db_path = re.sub(r"_all\.html$", ".html", db_html_path)
        base_dir = re.sub(r"\s+[0-9a-fA-F]{32}\.html$", "", clean_db_path)
        if base_dir.endswith(".html"):
            base_dir = os.path.splitext(base_dir)[0]
        base_dir = base_dir.rstrip("/") + "/"

        def _norm_title_key(t: str) -> str:
            if not t:
                return ""
            # 句読点・記号・連続空白を除去して照合用の正規化キーを生成
            t = urllib.parse.unquote(t).strip().lower()
            return re.sub(r"[\s_:\-\.,\(\)\[\]#]+", "", t)

        row_html_map: Dict[str, str] = {}  # row_id -> html_path
        row_html_by_title: Dict[str, Tuple[str, str]] = {}  # norm_title -> (row_id, html_path)
        row_html_by_stem: Dict[str, Tuple[str, str]] = {}  # norm_stem -> (row_id, html_path)

        for f in zf.namelist():
            if f.startswith(base_dir) and f.endswith(".html"):
                m = extract_uuids(f)
                r_id = normalize_id(m[-1]) if m else normalize_id(f)
                row_html_map[r_id] = f

                # ファイル名ステム
                stem = os.path.splitext(os.path.basename(f))[0]
                stem_no_id = re.sub(r"\s+[0-9a-fA-F]{32}$", "", stem).strip()
                row_html_by_stem[_norm_title_key(stem_no_id)] = (r_id, f)

                # HTML内のタイトルを事前読み込み
                try:
                    f_soup = BeautifulSoup(zf.read(f).decode("utf-8"), "html.parser")
                    f_title_el = f_soup.find("title") or f_soup.find(class_="page-title")
                    if f_title_el:
                        f_title = f_title_el.get_text(strip=True)
                        row_html_by_title[_norm_title_key(f_title)] = (r_id, f)
                except Exception:
                    pass

        # DB HTML（テーブル表示用HTML）が存在する場合、cell-title 内のリンクから行タイトルと行UUIDを抽出
        html_title_to_uuid: Dict[str, str] = {}
        target_db_html = db_html_path if db_html_path in zf.namelist() else (clean_db_path if clean_db_path in zf.namelist() else None)
        if target_db_html:
            try:
                db_soup = BeautifulSoup(zf.read(target_db_html).decode("utf-8"), "html.parser")
                for cell in db_soup.find_all(class_=lambda c: c and "cell-title" in c):
                    a_el = cell.find("a")
                    if a_el:
                        a_txt = a_el.get_text(strip=True)
                        a_target_id = None
                        if a_el.get("data-notion-page-id"):
                            a_target_id = normalize_id(a_el["data-notion-page-id"])
                        else:
                            a_uids = extract_uuids(a_el.get("href", ""))
                            if a_uids:
                                a_target_id = normalize_id(a_uids[-1])
                        if a_target_id and a_txt:
                            html_title_to_uuid[_norm_title_key(a_txt)] = a_target_id
            except Exception as e:
                logger.debug(f"Failed parsing db_html_path for row mapping: {e}")

        logger.info(f"Extracting {len(rows)} records from database CSV: [{db_node.title}]")
        used_row_ids = set()

        for row in rows:
            clean_row = {k.lstrip("\ufeff"): v for k, v in row.items()}
            # タスク名またはタイトル列
            raw_title = (
                clean_row.get("タスク名")
                or clean_row.get("名前")
                or clean_row.get("Title")
                or clean_row.get("Name")
                or clean_row.get("タイトル")
            )
            title_col_val = raw_title
            if not raw_title:
                # 最初の列をタイトルとする
                raw_title = next(iter(clean_row.values()), "Untitled Record")
                title_col_val = raw_title

            # NotionのCSVエクスポートにおけるリンクアーティファクト " (パス/ファイル名.html)" を除去
            raw_title = re.sub(r"\s*\([^)]*\.html\)$", "", raw_title).strip()
            # タイトル内の改行文字をスペースに正規化（ファイル名やConfluenceタイトルとの不整合を防止）
            row_title = re.sub(r"[\r\n]+", " ", raw_title).strip() if raw_title else "Untitled Record"
            row_title = urllib.parse.unquote(row_title)
            norm_k = _norm_title_key(row_title)

            # 行IDの特定（タイトル列のみを検査し、リレーション列のID誤爆を防止）
            row_id = None
            row_html_file = None

            # 1. タイトル列に埋め込まれたUUID
            if title_col_val:
                t_uids = extract_uuids(title_col_val)
                if t_uids:
                    cand_id = normalize_id(t_uids[-1])
                    if cand_id in row_html_map and cand_id not in used_row_ids:
                        row_id = cand_id
                        row_html_file = row_html_map[cand_id]

            # 2. DB HTML の cell-title からのマッピング
            if not row_html_file and norm_k in html_title_to_uuid:
                cand_id = html_title_to_uuid[norm_k]
                if cand_id in row_html_map and cand_id not in used_row_ids:
                    row_id = cand_id
                    row_html_file = row_html_map[cand_id]

            # 3. HTML内の <title> 完全一致・正規化一致
            if not row_html_file and norm_k in row_html_by_title:
                cand_id, cand_path = row_html_by_title[norm_k]
                if cand_id not in used_row_ids:
                    row_id = cand_id
                    row_html_file = cand_path

            # 4. ファイル名ステム一致
            if not row_html_file and norm_k in row_html_by_stem:
                cand_id, cand_path = row_html_by_stem[norm_k]
                if cand_id not in used_row_ids:
                    row_id = cand_id
                    row_html_file = cand_path

            # 5. 部分一致
            if not row_html_file:
                for r_id, r_path in row_html_map.items():
                    if r_id in used_row_ids:
                        continue
                    if norm_k and norm_k in _norm_title_key(r_path):
                        row_id = r_id
                        row_html_file = r_path
                        break

            if not row_id:
                row_id = f"{db_node.id}_row_{len(db_node.children)}"
            used_row_ids.add(row_id)

            # レコード本文ブロック、アイコン、カバーおよびコメントの抽出
            row_blocks = []
            row_comments = []
            row_icon = None
            row_cover = None
            if row_html_file and row_html_file in zf.namelist():
                row_soup = BeautifulSoup(zf.read(row_html_file).decode("utf-8"), "html.parser")
                row_icon = self._extract_icon_from_soup(row_soup)
                cover_img = row_soup.find("img", class_="page-cover-image")
                if cover_img and cover_img.get("src"):
                    row_cover = {"type": "external", "external": {"url": cover_img["src"]}}
                row_body = row_soup.find(class_="page-body")
                if row_body:
                    row_blocks = self._parse_elements_to_blocks(row_body, zf, os.path.dirname(row_html_file))
                row_comments = self._extract_page_comments(row_soup)

            # プロパティ辞書の整形
            row_properties = {}
            for col_name, val in clean_row.items():
                if not val:
                    continue
                clean_col = col_name.strip()
                clean_val = val.strip()

                # リレーションのパース（単一およびカンマ区切りの複数リレーション、URLリンク形式に対応）
                # 例1: "タイトル (フォルダ/ファイル名 00000000...html)"
                # 例2: "Task A (https://www.notion.so/..., Task B (...html))"
                unquoted_clean_val = urllib.parse.unquote(clean_val)
                rel_uids = extract_uuids(unquoted_clean_val) or extract_uuids(clean_val)
                is_rel = False
                if rel_uids and (".html" in clean_val or "notion.so" in clean_val or "notion.com" in clean_val):
                    is_rel = True

                if is_rel:
                    rel_items = []
                    # カンマ区切りまたはパターンで分割
                    # "(...)" 形式の各トークンからタイトルとUUIDを抽出
                    token_matches = re.findall(r"([^,\(\)]*?)\s*\(([^)]+)\)", clean_val)
                    if token_matches:
                        for t_name, t_ref in token_matches:
                            t_uids = extract_uuids(t_ref)
                            if t_uids:
                                tid = normalize_id(t_uids[-1])
                                rel_items.append({
                                    "id": tid,
                                    "text": t_name.strip() or tid[:8],
                                })
                    if not rel_items:
                        # トークン分割できなかった場合は抽出した全UUIDをリレーション化
                        for u in rel_uids:
                            tid = normalize_id(u)
                            rel_items.append({
                                "id": tid,
                                "text": clean_val.split("(")[0].strip() or tid[:8],
                            })
                    row_properties[clean_col] = {
                        "type": "relation",
                        "relations": rel_items,
                        "relation": rel_items,
                        "value": rel_items,
                    }
                elif clean_col in ("カテゴリ", "Tags", "Labels") and "," in clean_val:
                    # 複数選択
                    items = [item.strip() for item in clean_val.split(",") if item.strip()]
                    row_properties[clean_col] = {
                        "type": "multi_select",
                        "multi_select": [{"name": item} for item in items],
                        "value": [{"name": item} for item in items],
                    }
                elif clean_val in ("Yes", "No", "true", "false", "True", "False"):
                    # チェックボックス
                    is_checked = clean_val in ("Yes", "true", "True")
                    row_properties[clean_col] = {
                        "type": "checkbox",
                        "checkbox": is_checked,
                        "value": is_checked,
                    }
                else:
                    # 値に改行が含まれている場合は、タグ選択肢(select)ではなくテキスト(text)として保持
                    if "\n" in clean_val or "\r" in clean_val:
                        row_properties[clean_col] = {
                            "type": "text",
                            "value": clean_val,
                        }
                    else:
                        row_properties[clean_col] = {
                            "type": "select",
                            "select": {"name": clean_val},
                            "value": clean_val,
                        }

            row_node = PageNode(
                id=row_id,
                title=row_title,
                node_type="database_row",
                icon=row_icon,
                cover=row_cover,
                properties=row_properties,
                blocks=row_blocks,
                children=[],
                comments=row_comments,
            )
            db_node.children.append(row_node)

    @staticmethod
    def _parse_date_string(val: str) -> Optional[str]:
        return _parse_date_string(val)

    def _extract_properties_from_table(
        self,
        prop_table: Optional[Tag],
        col_types_out: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """<table class="properties"> から各行の型と値を抽出する。"""
        result: Dict[str, Dict[str, Any]] = {}
        if not prop_table:
            return result

        for tr in prop_table.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not th or not td:
                continue
            p_name = th.get_text(strip=True)
            if not p_name:
                continue

            tr_classes = tr.get("class") or []
            if isinstance(tr_classes, str):
                tr_classes = [tr_classes]

            detected_type = None
            for cls in tr_classes:
                if cls.startswith("property-row-") and cls != "property-row":
                    raw_t = cls[len("property-row-"):]
                    if raw_t in ("multi_select",):
                        detected_type = "multi_select"
                    elif raw_t in ("select",):
                        detected_type = "select"
                    elif raw_t in ("status",):
                        detected_type = "status"
                    elif raw_t in ("date", "created_time", "last_edited_time"):
                        detected_type = "date"
                    elif raw_t in ("checkbox",):
                        detected_type = "checkbox"
                    elif raw_t in ("url",):
                        detected_type = "url"
                    elif raw_t in ("number",):
                        detected_type = "number"
                    elif raw_t in ("relation",):
                        detected_type = "relation"
                    elif raw_t in ("text", "rich_text", "email", "phone_number"):
                        detected_type = "text"

            if col_types_out is not None and detected_type:
                if detected_type == "multi_select" or p_name not in col_types_out:
                    col_types_out[p_name] = detected_type

            # 1. リレーションリンクの検出
            rel_links = td.find_all("a")
            relations = []
            for a in rel_links:
                target_id = None
                if a.get("data-notion-page-id"):
                    target_id = normalize_id(a["data-notion-page-id"])
                else:
                    href = a.get("href", "")
                    if not href.startswith(("http://", "https://")) or "notion.so" in href or "notion.com" in href:
                        href_uids = extract_uuids(href)
                        if href_uids:
                            target_id = normalize_id(href_uids[-1])
                if target_id:
                    relations.append({
                        "id": target_id,
                        "text": a.get_text(strip=True) or target_id[:8],
                    })
            if relations:
                if col_types_out is not None:
                    col_types_out.setdefault(p_name, "relation")
                result[p_name] = {
                    "type": "relation",
                    "relations": relations,
                    "relation": relations,
                    "value": relations,
                }
                continue

            # 2. 日付 (property-row-date または <time> タグ)
            time_tags = td.find_all("time")
            if detected_type == "date" or time_tags:
                iso_start = None
                iso_end = None
                if time_tags:
                    t0 = time_tags[0]
                    raw_s = t0.get("datetime") or t0.get_text(strip=True)
                    iso_start = _parse_date_string(raw_s) or _parse_date_string(t0.get_text(strip=True))
                    if len(time_tags) >= 2:
                        t1 = time_tags[1]
                        raw_e = t1.get("datetime") or t1.get_text(strip=True)
                        iso_end = _parse_date_string(raw_e) or _parse_date_string(t1.get_text(strip=True))
                    elif any(sep in t0.get_text() for sep in ("→", "->", "〜")):
                        parts = re.split(r"\s*(?:→|->|〜)\s*", t0.get_text(strip=True))
                        if len(parts) >= 2:
                            iso_end = _parse_date_string(parts[1])
                else:
                    td_txt = td.get_text(strip=True)
                    iso_start = _parse_date_string(td_txt)
                    if iso_start and any(sep in td_txt for sep in ("→", "->", "〜")):
                        parts = re.split(r"\s*(?:→|->|〜)\s*", td_txt)
                        if len(parts) >= 2:
                            iso_end = _parse_date_string(parts[1])

                if col_types_out is not None:
                    col_types_out.setdefault(p_name, "date")
                if iso_start:
                    prop_obj: Dict[str, Any] = {
                        "type": "date",
                        "start": iso_start,
                        "value": iso_start,
                    }
                    if iso_end:
                        prop_obj["end"] = iso_end
                    result[p_name] = prop_obj
                continue

            # 3. 複数選択 / 単一選択 / ステータス (<span class="selected-value">)
            sel_spans = td.find_all("span", class_=lambda c: c and "selected-value" in c)
            if detected_type == "multi_select" or len(sel_spans) > 1:
                if col_types_out is not None:
                    col_types_out[p_name] = "multi_select"
                if sel_spans:
                    items = [s.get_text(strip=True) for s in sel_spans if s.get_text(strip=True)]
                else:
                    raw_t = td.get_text(strip=True)
                    items = [x.strip() for x in raw_t.split(",") if x.strip()] if raw_t else []
                if items:
                    item_objs = [{"name": it, "value": it} for it in items]
                    result[p_name] = {
                        "type": "multi_select",
                        "items": item_objs,
                        "multi_select": item_objs,
                        "value": item_objs,
                    }
                continue

            if detected_type in ("select", "status") or len(sel_spans) == 1:
                actual_t = "status" if detected_type == "status" else "select"
                if col_types_out is not None and col_types_out.get(p_name) != "multi_select":
                    col_types_out.setdefault(p_name, actual_t)
                val_str = sel_spans[0].get_text(strip=True) if sel_spans else td.get_text(strip=True)
                if val_str:
                    result[p_name] = {
                        "type": actual_t,
                        actual_t: {"name": val_str},
                        "value": val_str,
                    }
                continue

            # 4. チェックボックス
            cb_el = td.find(class_=lambda c: c and "checkbox" in c)
            if detected_type == "checkbox" or cb_el is not None:
                if col_types_out is not None:
                    col_types_out.setdefault(p_name, "checkbox")
                if cb_el is not None:
                    cb_cls = cb_el.get("class") or []
                    if isinstance(cb_cls, str):
                        cb_cls = [cb_cls]
                    is_checked = any("checkbox-on" in c for c in cb_cls)
                else:
                    txt = td.get_text(strip=True)
                    is_checked = txt in ("Yes", "true", "True", "☑", "✅")
                result[p_name] = {
                    "type": "checkbox",
                    "checkbox": is_checked,
                    "value": is_checked,
                }
                continue

            # 5. URL
            if detected_type == "url":
                a_el = td.find("a")
                u_val = a_el.get("href") if (a_el and a_el.get("href")) else td.get_text(strip=True)
                if u_val:
                    result[p_name] = {
                        "type": "url",
                        "url": u_val,
                        "value": u_val,
                    }
                continue

            # 6. 数値
            if detected_type == "number":
                n_val = td.get_text(strip=True)
                if n_val:
                    result[p_name] = {
                        "type": "number",
                        "number": n_val,
                        "value": n_val,
                    }
                continue

            # 7. その他テキスト (日付形式なら日付として判別)
            p_val = td.get_text(strip=True)
            if p_val:
                iso_d = _parse_date_string(p_val)
                if iso_d and detected_type != "text":
                    if col_types_out is not None:
                        col_types_out.setdefault(p_name, "date")
                    result[p_name] = {
                        "type": "date",
                        "start": iso_d,
                        "value": iso_d,
                    }
                else:
                    result[p_name] = {"type": "text", "value": p_val}

        return result

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

        def _get_element_block_color(classes_list: Any) -> str:
            if isinstance(classes_list, str):
                classes_list = [classes_list]
            for c in classes_list or []:
                if c.startswith("block-color-"):
                    col = c.replace("block-color-", "")
                    if col == "teal":
                        return "green"
                    elif col == "teal_background":
                        return "green_background"
                    return col
            return "default"

        for el in container.children:
            if not isinstance(el, Tag):
                continue

            tag = el.name.lower()
            el_id = el.get("id") or f"b_{len(blocks)}"
            classes = el.get("class", [])

            # 1. 見出し
            if tag == "h1":
                b_color = _get_element_block_color(classes)
                blocks.append({
                    "id": el_id,
                    "type": "heading_1",
                    "heading_1": {"rich_text": self._parse_inline_rich_text(el, ignore_block_color=True), "color": b_color, "is_toggleable": False},
                })
            elif tag == "h2":
                b_color = _get_element_block_color(classes)
                blocks.append({
                    "id": el_id,
                    "type": "heading_2",
                    "heading_2": {"rich_text": self._parse_inline_rich_text(el, ignore_block_color=True), "color": b_color, "is_toggleable": False},
                })
            elif tag == "h3":
                b_color = _get_element_block_color(classes)
                blocks.append({
                    "id": el_id,
                    "type": "heading_3",
                    "heading_3": {"rich_text": self._parse_inline_rich_text(el, ignore_block_color=True), "color": b_color, "is_toggleable": False},
                })

            # 2. 段落
            elif tag == "p":
                b_color = _get_element_block_color(classes)
                blocks.append({
                    "id": el_id,
                    "type": "paragraph",
                    "paragraph": {"rich_text": self._parse_inline_rich_text(el, ignore_block_color=True), "color": b_color},
                })

            # 3. 箇条書きリスト
            elif tag == "ul" and "bulleted-list" in classes:
                for li in el.find_all("li", recursive=False):
                    li_id = li.get("id") or el_id
                    li_color = _get_element_block_color(li.get("class", []))
                    rich_texts, child_blocks = self._extract_list_item_content(
                        li, zf, current_dir, db_csv_map=db_csv_map
                    )
                    blocks.append({
                        "id": li_id,
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": rich_texts, "color": li_color},
                        "children_blocks": child_blocks,
                        "has_children": bool(child_blocks),
                    })

            # 4. 番号付きリスト
            elif tag == "ol" and "numbered-list" in classes:
                for li in el.find_all("li", recursive=False):
                    li_id = li.get("id") or el_id
                    li_color = _get_element_block_color(li.get("class", []))
                    rich_texts, child_blocks = self._extract_list_item_content(
                        li, zf, current_dir, db_csv_map=db_csv_map
                    )
                    blocks.append({
                        "id": li_id,
                        "type": "numbered_list_item",
                        "numbered_list_item": {"rich_text": rich_texts, "color": li_color},
                        "children_blocks": child_blocks,
                        "has_children": bool(child_blocks),
                    })

            # 5. ToDoリスト
            elif tag == "ul" and "to-do-list" in classes:
                for li in el.find_all("li", recursive=False):
                    li_id = li.get("id") or el_id
                    inp = li.find("input", type="checkbox", recursive=False) or li.find("input", type="checkbox")
                    is_checked = False
                    if inp:
                        is_checked = inp.has_attr("checked") or "checkbox-on" in inp.get("class", [])

                    rich_texts, child_blocks = self._extract_list_item_content(
                        li, zf, current_dir, db_csv_map=db_csv_map, skip_classes=["indented", "checkbox"]
                    )

                    blocks.append({
                        "id": li_id,
                        "type": "to_do",
                        "to_do": {
                            "rich_text": rich_texts,
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
                b_color = _get_element_block_color(classes)
                rich_texts, child_blocks = self._extract_list_item_content(el, zf, current_dir, db_csv_map=db_csv_map)
                blocks.append({
                    "id": el_id,
                    "type": "quote",
                    "quote": {"rich_text": rich_texts, "color": b_color},
                    "children_blocks": child_blocks,
                    "has_children": bool(child_blocks),
                })

            # 8. コールアウト
            elif tag == "aside" or "callout" in classes:
                from src.icon_mapper import resolve_notion_icon_to_emoji, is_unicode_emoji
                bg_color = el.get("data-notion-callout-background") or "default"
                if "block-color-" in str(classes):
                    for c in classes:
                        if c.startswith("block-color-"):
                            bg_color = c.replace("block-color-", "")
                            break

                icon_val = el.get("data-notion-callout-icon")
                if not icon_val:
                    icon_el = el.find("span", class_="icon")
                    if icon_el:
                        if icon_el.get("data-emoji"):
                            icon_val = icon_el["data-emoji"]
                        elif is_unicode_emoji(icon_el.get_text(strip=True)):
                            icon_val = icon_el.get_text(strip=True)
                if not icon_val:
                    icon_img = el.find("img", class_="icon")
                    if icon_img and icon_img.get("src"):
                        icon_val = icon_img["src"]

                resolved_emoji = resolve_notion_icon_to_emoji(icon_val) if icon_val else None

                callout_rich_text, callout_children = self._extract_callout_content(
                    el, zf, current_dir, db_csv_map=db_csv_map
                )

                blocks.append({
                    "id": el_id,
                    "type": "callout",
                    "callout": {
                        "rich_text": callout_rich_text,
                        "icon": {"type": "emoji", "emoji": resolved_emoji} if resolved_emoji else None,
                        "color": bg_color,
                    },
                    "children_blocks": callout_children,
                    "has_children": bool(callout_children),
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
            elif tag == "figure" or "link-to-page" in classes or "file" in classes or "source" in classes or "pdf" in classes:
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
                    is_external = href.startswith(("http://", "https://"))
                    att_filename = os.path.basename(urllib.parse.unquote(href))
                    zip_bytes = None
                    if not is_external:
                        zip_bytes, resolved_path = self._read_zip_attachment_bytes(zf, current_dir, href)

                    if "bookmark" in classes or el.find(class_="bookmark"):
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
                    elif lower_href.endswith(".pdf"):
                        pdf_obj = {
                            "id": el_id,
                            "type": "pdf",
                            "pdf": {
                                "type": "file" if zip_bytes else "external",
                                "caption": caption_rich,
                                "external": {"url": href} if is_external or not zip_bytes else None,
                                "file": {"url": att_filename} if zip_bytes else None,
                            },
                            "_attachment_filename": att_filename,
                        }
                        if zip_bytes:
                            pdf_obj["_attachment_bytes"] = zip_bytes
                            logger.info(f"Loaded inline PDF attachment from ZIP: {resolved_path} ({len(zip_bytes)} bytes)")
                        blocks.append(pdf_obj)
                    elif lower_href.endswith((".mp3", ".wav", ".m4a", ".aac")):
                        audio_obj = {
                            "id": el_id,
                            "type": "audio",
                            "audio": {
                                "type": "file" if zip_bytes else "external",
                                "caption": caption_rich,
                                "external": {"url": href} if is_external or not zip_bytes else None,
                                "file": {"url": att_filename} if zip_bytes else None,
                            },
                            "_attachment_filename": att_filename,
                        }
                        if zip_bytes:
                            audio_obj["_attachment_bytes"] = zip_bytes
                            logger.info(f"Loaded inline audio attachment from ZIP: {resolved_path} ({len(zip_bytes)} bytes)")
                        blocks.append(audio_obj)
                    elif lower_href.endswith((".mp4", ".mov", ".webm")) or "youtube.com" in lower_href or "youtu.be" in lower_href:
                        video_obj = {
                            "id": el_id,
                            "type": "video",
                            "video": {
                                "type": "file" if zip_bytes else "external",
                                "caption": caption_rich,
                                "external": {"url": href} if is_external or not zip_bytes else None,
                                "file": {"url": att_filename} if zip_bytes else None,
                            },
                            "_attachment_filename": att_filename,
                        }
                        if zip_bytes:
                            video_obj["_attachment_bytes"] = zip_bytes
                            logger.info(f"Loaded inline video attachment from ZIP: {resolved_path} ({len(zip_bytes)} bytes)")
                        blocks.append(video_obj)
                    else:
                        file_obj = {
                            "id": el_id,
                            "type": "file",
                            "file": {
                                "type": "file" if zip_bytes else "external",
                                "caption": caption_rich,
                                "external": {"url": href} if is_external or not zip_bytes else None,
                                "file": {"url": att_filename} if zip_bytes else None,
                            },
                            "_attachment_filename": att_filename,
                        }
                        if zip_bytes:
                            file_obj["_attachment_bytes"] = zip_bytes
                            logger.info(f"Loaded inline file attachment from ZIP: {resolved_path} ({len(zip_bytes)} bytes)")
                        blocks.append(file_obj)

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

            # 16. 同期ブロックやネストされたラッパー (div / section または内部にブロック要素を内包する要素)
            elif (
                any(self._is_block_tag(sub) for sub in el.children if isinstance(sub, Tag))
                or (
                    tag in ("div", "section")
                    and "column-list" not in classes
                    and "column" not in classes
                    and any(self._is_block_tag(sub) for sub in el.find_all() if isinstance(sub, Tag))
                )
            ):
                sub_blocks = self._parse_elements_to_blocks(el, zf, current_dir, db_csv_map=db_csv_map)
                blocks.extend(sub_blocks)

            # 17. その他・未知のNotion固有要素 (例: breadcrumb 等)
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

    def _is_block_tag(self, node: Tag) -> bool:
        """与えられたHTML要素がブロック要素（リスト・コールアウト・画像・テーブル・コード等）であるかを判定する。"""
        if not isinstance(node, Tag) or not node.name:
            return False
        t_name = node.name.lower()
        t_classes = node.get("class") or []
        if isinstance(t_classes, str):
            t_classes = [t_classes]
        if t_name in ("ul", "ol", "aside", "figure", "blockquote", "pre", "table", "details", "hr", "h1", "h2", "h3", "nav"):
            return True
        if any(
            c in t_classes
            for c in (
                "indented",
                "callout",
                "column-list",
                "column_list",
                "collection-content",
                "link-to-page",
                "image",
                "table-of-contents",
                "table_of_contents",
                "file",
                "source",
                "pdf",
            )
        ):
            return True
        if t_name == "img" and "icon" not in t_classes and "page-header-icon" not in t_classes and "property-icon" not in t_classes:
            return True
        return False

    def _extract_list_item_content(
        self,
        li: Tag,
        zf: zipfile.ZipFile,
        current_dir: str,
        db_csv_map: Optional[Dict[str, str]] = None,
        skip_classes: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """<li> 要素から、自身のインライン rich_text と配下のネストされた子ブロック群 (children_blocks) を分離して抽出する。
        コールアウト (<aside>) やネストされたリスト (<ul>/<ol>)、画像 (<figure>) 等が .indented 内にある場合も、
        <li> 直下に配置されている場合も、本文テキストに混入させず正確に子ブロックとして抽出する。
        """
        import copy
        skip_cls = list(skip_classes or ["indented"])
        inline_container = Tag(name="span")
        block_container = Tag(name="div")
        first_p_consumed = False

        for child in li.children:
            if isinstance(child, NavigableString):
                inline_container.append(copy.copy(child))
            elif isinstance(child, Tag):
                c_name = child.name.lower()
                c_classes = child.get("class") or []
                if isinstance(c_classes, str):
                    c_classes = [c_classes]

                if "indented" in c_classes:
                    for sub in child.children:
                        if isinstance(sub, Tag):
                            block_container.append(copy.copy(sub))
                elif self._is_block_tag(child):
                    block_container.append(copy.copy(child))
                elif c_name == "p":
                    # 最初の <p> かつ既存のインラインテキストが空の場合はリスト項目自身のテキストとして採用
                    has_nested_block = any(self._is_block_tag(sub) for sub in child.children if isinstance(sub, Tag))
                    if not first_p_consumed and not inline_container.get_text(strip=True) and not has_nested_block:
                        first_p_consumed = True
                        for sub in child.children:
                            inline_container.append(copy.copy(sub))
                    else:
                        block_container.append(copy.copy(child))
                elif c_name in ("span", "div") and any(self._is_block_tag(sub) for sub in child.children if isinstance(sub, Tag)):
                    for sub in child.children:
                        if isinstance(sub, NavigableString):
                            inline_container.append(copy.copy(sub))
                        elif isinstance(sub, Tag):
                            sub_cls = sub.get("class") or []
                            if isinstance(sub_cls, str):
                                sub_cls = [sub_cls]
                            if "indented" in sub_cls:
                                for s2 in sub.children:
                                    if isinstance(s2, Tag):
                                        block_container.append(copy.copy(s2))
                            elif self._is_block_tag(sub) or sub.name.lower() == "p":
                                block_container.append(copy.copy(sub))
                            else:
                                inline_container.append(copy.copy(sub))
                else:
                    inline_container.append(copy.copy(child))

        rich_texts = self._parse_inline_rich_text(inline_container, skip_classes=skip_cls)
        # 先頭・末尾の改行や空白のみのノードを整理
        if rich_texts:
            rich_texts[0]["plain_text"] = rich_texts[0]["plain_text"].lstrip("\n\r ")
            if "text" in rich_texts[0] and isinstance(rich_texts[0]["text"], dict):
                rich_texts[0]["text"]["content"] = rich_texts[0]["text"].get("content", "").lstrip("\n\r ")
            rich_texts[-1]["plain_text"] = rich_texts[-1]["plain_text"].rstrip("\n\r ")
            if "text" in rich_texts[-1] and isinstance(rich_texts[-1]["text"], dict):
                rich_texts[-1]["text"]["content"] = rich_texts[-1]["text"].get("content", "").rstrip("\n\r ")
            rich_texts = [rt for rt in rich_texts if rt.get("plain_text")]

        child_blocks = self._parse_elements_to_blocks(block_container, zf, current_dir, db_csv_map=db_csv_map)
        return rich_texts, child_blocks

    def _extract_callout_content(
        self,
        el: Tag,
        zf: zipfile.ZipFile,
        current_dir: str,
        db_csv_map: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """コールアウト (<aside class="callout">) から、メインテキスト (rich_text) と
        内部にネストされた子ブロック (埋め込み画像 <figure class="image">、追加段落 <p>、リスト等) を分離抽出する。
        """
        import copy
        # NotionのHTMLエクスポートでは <aside><div style="font-size:1.5em">(icon)</div><div style="width:100%">(content)</div></aside>
        # の構造になっていることが多い。アイコン用div以外のコンテンツコンテナを特定する。
        direct_divs = [d for d in el.find_all("div", recursive=False)]
        content_root: Tag = el
        if len(direct_divs) >= 2:
            first_div = direct_divs[0]
            if first_div.find(class_="icon") or "1.5em" in (first_div.get("style") or ""):
                if len(direct_divs) == 2 and not any(
                    isinstance(c, Tag) and c not in direct_divs for c in el.children
                ):
                    content_root = direct_divs[1]

        inline_container = Tag(name="span")
        block_container = Tag(name="div")
        first_p_consumed = False

        def _process_nodes(nodes):
            nonlocal first_p_consumed
            for child in nodes:
                if isinstance(child, NavigableString):
                    if not first_p_consumed and not block_container.contents:
                        inline_container.append(copy.copy(child))
                    elif str(child).strip():
                        p_tag = Tag(name="p")
                        p_tag.append(copy.copy(child))
                        block_container.append(p_tag)
                elif isinstance(child, Tag):
                    c_name = child.name.lower()
                    c_classes = child.get("class") or []
                    if isinstance(c_classes, str):
                        c_classes = [c_classes]

                    # アイコン要素はスキップ
                    if "icon" in c_classes or (
                        c_name == "div"
                        and (child.find(class_="icon") or "1.5em" in (child.get("style") or ""))
                        and not child.get_text(strip=True)
                    ):
                        continue

                    if "indented" in c_classes:
                        for sub in child.children:
                            if isinstance(sub, Tag):
                                block_container.append(copy.copy(sub))
                    elif c_name == "p":
                        has_nested_block = any(self._is_block_tag(sub) for sub in child.children if isinstance(sub, Tag))
                        if not first_p_consumed and not inline_container.get_text(strip=True) and not block_container.contents and not has_nested_block:
                            first_p_consumed = True
                            for sub in child.children:
                                inline_container.append(copy.copy(sub))
                        elif has_nested_block:
                            _process_nodes(list(child.children))
                        else:
                            block_container.append(copy.copy(child))
                    elif self._is_block_tag(child):
                        block_container.append(copy.copy(child))
                    elif c_name == "div":
                        _process_nodes(list(child.children))
                    else:
                        if not first_p_consumed and not block_container.contents:
                            inline_container.append(copy.copy(child))
                        else:
                            block_container.append(copy.copy(child))

        _process_nodes(list(content_root.children))

        rich_texts = self._parse_inline_rich_text(inline_container, skip_classes=["icon"], ignore_block_color=True)
        if rich_texts:
            rich_texts[0]["plain_text"] = rich_texts[0]["plain_text"].lstrip("\n\r ")
            if "text" in rich_texts[0] and isinstance(rich_texts[0]["text"], dict):
                rich_texts[0]["text"]["content"] = rich_texts[0]["text"].get("content", "").lstrip("\n\r ")
            rich_texts[-1]["plain_text"] = rich_texts[-1]["plain_text"].rstrip("\n\r ")
            if "text" in rich_texts[-1] and isinstance(rich_texts[-1]["text"], dict):
                rich_texts[-1]["text"]["content"] = rich_texts[-1]["text"].get("content", "").rstrip("\n\r ")
            rich_texts = [rt for rt in rich_texts if rt.get("plain_text")]

        child_blocks = self._parse_elements_to_blocks(block_container, zf, current_dir, db_csv_map=db_csv_map)
        return rich_texts, child_blocks

    def _parse_inline_rich_text(
        self,
        node: Tag,
        skip_classes: Optional[List[str]] = None,
        ignore_block_color: bool = False,
    ) -> List[Dict[str, Any]]:
        """HTMLタグ配下のインラインテキスト（太字・斜体・コード・リンク・文字色・背景色等）をNotion rich_text形式にパースする。"""
        skip_classes = list(skip_classes or [])
        skip_classes.extend(["icon", "page-header-icon", "property-icon"])
        rich_texts = []

        def _extract_highlight_color(tag_el: Tag, is_root: bool = False) -> Optional[str]:
            hl = tag_el.get("data-notion-highlight")
            if not hl:
                t_cls = tag_el.get("class") or []
                if isinstance(t_cls, str):
                    t_cls = [t_cls]
                for cls_item in t_cls:
                    if cls_item.startswith("highlight-"):
                        hl = cls_item[len("highlight-"):]
                        break
                    elif not (is_root and ignore_block_color) and cls_item.startswith("block-color-"):
                        hl = cls_item[len("block-color-"):]
                        break
            if hl:
                # Notion HTMLエクスポートでは green が teal / teal_background として出力されるため正規化
                if hl == "teal":
                    return "green"
                elif hl == "teal_background":
                    return "green_background"
                return hl
            return None

        def walk(el: Any, marks: Dict[str, Any]):
            for child in el.children:
                if isinstance(child, NavigableString):
                    text = str(child)
                    if not text:
                        continue
                    item_dict = {
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
                    }
                    if marks.get("page_id"):
                        item_dict["page_id"] = marks["page_id"]
                    rich_texts.append(item_dict)
                elif isinstance(child, Tag):
                    child_classes = child.get("class", [])
                    if any(sc in child_classes for sc in skip_classes):
                        continue
                    if self._is_block_tag(child):
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
                        if child.get("data-notion-page-id"):
                            new_marks["page_id"] = normalize_id(child["data-notion-page-id"])
                        elif child.get("href"):
                            h_uids = extract_uuids(child["href"])
                            if h_uids:
                                new_marks["page_id"] = normalize_id(h_uids[-1])

                    hl_color = _extract_highlight_color(child)
                    if hl_color and (hl_color != "default" or "color" not in new_marks):
                        new_marks["color"] = hl_color

                    walk(child, new_marks)

        root_hl = _extract_highlight_color(node, is_root=True)
        initial_marks: Dict[str, Any] = {}
        if root_hl and root_hl != "default":
            initial_marks["color"] = root_hl
        walk(node, initial_marks)
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
                "id": bn.id,
                "type": "child_page",
                "child_page": {"title": bn.title},
            })

        combined_root = PageNode(
            id="multi_block_root_page",
            title=self.root_title,
            node_type="page",
            icon="🌐",
            properties={},
            blocks=summary_blocks,
            children=block_nodes,
        )

        return combined_root
