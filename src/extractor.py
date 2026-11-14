"""
Notion からページ、ブロック階層、データベースおよびレコードを再帰的に抽出するモジュール。
"""

import logging
import re
import time
import urllib.parse
import requests
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from notion_client import Client

logger = logging.getLogger(__name__)

# 認証必須・直接ダウンロード不可の外部アプリ連携ドメイン定義
# (ドメイン群, サービス表示名, 正規化後のブロック種別)
EXTERNAL_APP_SERVICE_RULES: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
    (("drive.google.com",), "Google Drive", "google_drive"),
    (("docs.google.com",), "Google Docs", "google_drive"),
    (("onedrive.live.com", "1drv.ms"), "OneDrive", "embed"),
    (("sharepoint.com",), "SharePoint", "embed"),
    (("dropbox.com", "dropboxusercontent.com"), "Dropbox", "embed"),
    (("box.com", "app.box.com"), "Box", "embed"),
    (("miro.com",), "Miro", "miro"),
    (("trello.com",), "Trello", "trello"),
    (("github.com", "gist.github.com"), "GitHub", "embed"),
    (("slack.com",), "Slack", "embed"),
)

KNOWN_UNSUPPORTED_SERVICE_NAMES = frozenset(rule[1] for rule in EXTERNAL_APP_SERVICE_RULES)
UNSUPPORTED_PAGE_ELEMENT_TYPES = frozenset({"breadcrumb", "unsupported", "button", "template"})


def is_broken_or_placeholder_url(url: Optional[str], allow_notion_page_url: bool = False) -> bool:
    """破損URL・プレースホルダーURL（空・#・app.notion.comundefined・undefined終端ホスト・不正app.notion.comパス等）を判定する。"""
    if not url or not isinstance(url, str):
        return True
    s = url.strip()
    if not s or s.startswith("#"):
        return True
    lower_s = s.lower()
    if lower_s in ("undefined", "null", "none", "about:blank"):
        return True
    if lower_s.startswith(("javascript:", "vbscript:", "about:")):
        return True

    try:
        parsed = urllib.parse.urlparse(s)
    except Exception:
        return True

    scheme = (parsed.scheme or "").lower()
    if scheme in ("http", "https"):
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return True
        if host in ("undefined", "null", "none"):
            return True
        if host.endswith("undefined") or host.endswith("null"):
            return True
        if host == "app.notion.com" or host.endswith(".app.notion.com"):
            path = (parsed.path or "").strip()
            lower_path = path.lower()
            lower_query = (parsed.query or "").lower()
            if not path or path == "/":
                return True
            if "undefined" in lower_path or "null" in lower_path or "undefined" in lower_query:
                return True
            if not allow_notion_page_url:
                if not path.startswith("/p/") and not path.startswith("/icons/"):
                    return True
    return False


def classify_external_app_url(url: Optional[str]) -> Optional[Tuple[str, str]]:
    """認証必須・外部アプリ連携ドメインのURLを判定し、(service_name, block_type) を返す。該当しない場合は None。"""
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    if not s.lower().startswith(("http://", "https://")):
        return None
    try:
        hostname = (urllib.parse.urlparse(s).hostname or "").lower().rstrip(".")
    except Exception:
        return None
    if not hostname:
        return None
    for domains, service_name, block_type in EXTERNAL_APP_SERVICE_RULES:
        for domain in domains:
            if hostname == domain or hostname.endswith("." + domain):
                return service_name, block_type
    return None


def extract_block_url(block: Dict[str, Any]) -> str:
    """ブロック辞書から代表URLを安全に取り出す（存在しない場合は空文字列）。"""
    if not isinstance(block, dict):
        return ""
    b_type = block.get("type", "")
    data = block.get(b_type)
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("url"), str):
        return data["url"].strip()
    f_obj = data.get("file") or {}
    if isinstance(f_obj, dict) and isinstance(f_obj.get("url"), str) and f_obj["url"].strip():
        return f_obj["url"].strip()
    e_obj = data.get("external") or {}
    if isinstance(e_obj, dict) and isinstance(e_obj.get("url"), str) and e_obj["url"].strip():
        return e_obj["url"].strip()
    return ""


def build_unsupported_external_embed_block(
    block_id: str,
    block_type: str = "unsupported",
    service_name: str = "外部埋め込み / 未対応リンク",
    url: str = "",
    raw_label: str = "",
    reason: str = "unsupported_external_embed",
    caption: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """未対応外部アプリ埋め込み・破損プレースホルダーURL用の安全なブロック辞書を生成する。"""
    safe_url = "" if is_broken_or_placeholder_url(url) else str(url).strip()
    payload: Dict[str, Any] = {
        "url": safe_url,
        "caption": caption or [],
    }
    if block_type in ("file", "pdf"):
        payload = {
            "type": "external",
            "caption": caption or [],
            "external": {"url": safe_url} if safe_url else None,
            "file": None,
        }
    return {
        "id": block_id,
        "type": block_type,
        block_type: payload,
        "_is_unsupported_external_embed": True,
        "_attachment_uploaded": False,
        "service_name": service_name,
        "raw_label": raw_label or "",
        "_unsupported_reason": reason,
    }


def is_unsupported_page_element_block(block: Dict[str, Any]) -> bool:
    """Confluence側でネイティブ変換不可・フォールバック注記対象となる未対応ページ要素かを判定する。"""
    if not isinstance(block, dict):
        return False
    b_type = block.get("type", "")
    if b_type in UNSUPPORTED_PAGE_ELEMENT_TYPES or b_type == "google_drive":
        return True
    if block.get("_is_unsupported_external_embed"):
        return True
    url = extract_block_url(block)
    if b_type in ("embed", "link_preview", "miro", "trello"):
        if is_broken_or_placeholder_url(url) or classify_external_app_url(url) is not None:
            return True
    if b_type in ("file", "pdf"):
        has_local_bytes = bool(block.get("_attachment_bytes") or block.get("_attachment_zip_entry"))
        if not has_local_bytes and not block.get("_attachment_uploaded", True):
            if is_broken_or_placeholder_url(url) or classify_external_app_url(url) is not None:
                return True
    return False


def normalize_notion_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """Notion APIから取得したブロックのうち、未対応外部アプリ・破損/空URLブロックを安全に正規化する。"""
    if not isinstance(block, dict):
        return block
    b_type = block.get("type", "")

    if b_type == "google_drive":
        gd = block.get("google_drive") if isinstance(block.get("google_drive"), dict) else {}
        raw_url = gd.get("url") or (gd.get("external") or {}).get("url") or ""
        safe_url = "" if is_broken_or_placeholder_url(raw_url) else raw_url.strip()
        block["google_drive"] = {**gd, "url": safe_url}
        block["_is_unsupported_external_embed"] = True
        block["_attachment_uploaded"] = False
        block["service_name"] = block.get("service_name") or "Google Drive"
        block["raw_label"] = block.get("raw_label") or gd.get("title") or gd.get("name") or ""
        block["_unsupported_reason"] = "external_app_auth_required" if safe_url else "broken_or_placeholder_url"
        return block

    if b_type == "unsupported":
        u_data = block.get("unsupported") if isinstance(block.get("unsupported"), dict) else {}
        raw_url = u_data.get("url") or ""
        safe_url = "" if is_broken_or_placeholder_url(raw_url) else raw_url.strip()
        block["unsupported"] = {**u_data, "url": safe_url}
        block["_is_unsupported_external_embed"] = True
        block["_attachment_uploaded"] = False
        block["service_name"] = block.get("service_name") or "未対応独自機能 (Unsupported)"
        block["_unsupported_reason"] = block.get("_unsupported_reason") or "notion_unsupported_block"
        return block

    if b_type in ("embed", "link_preview", "miro", "trello"):
        data = block.get(b_type) if isinstance(block.get(b_type), dict) else {}
        raw_url = data.get("url") or ""
        if is_broken_or_placeholder_url(raw_url):
            block[b_type] = {**data, "url": ""}
            block["_is_unsupported_external_embed"] = True
            block["_attachment_uploaded"] = False
            block["service_name"] = block.get("service_name") or (
                "Link Preview" if b_type == "link_preview" else "外部埋め込み (Embed)"
            )
            block["_unsupported_reason"] = "broken_or_placeholder_url"
        else:
            app_info = classify_external_app_url(raw_url)
            if app_info:
                svc_name, _ = app_info
                block["_is_unsupported_external_embed"] = True
                block["_attachment_uploaded"] = False
                block["service_name"] = block.get("service_name") or svc_name
                block["_unsupported_reason"] = "external_app_auth_required"
        return block

    if b_type in ("pdf", "file"):
        if block.get("_attachment_bytes") or block.get("_attachment_zip_entry"):
            return block
        data = block.get(b_type) if isinstance(block.get(b_type), dict) else {}
        f_obj = data.get("file") if isinstance(data.get("file"), dict) else {}
        e_obj = data.get("external") if isinstance(data.get("external"), dict) else {}
        target_url = (f_obj.get("url") or e_obj.get("url") or "").strip()
        if is_broken_or_placeholder_url(target_url):
            cleaned_data = dict(data)
            if e_obj:
                cleaned_data["external"] = {**e_obj, "url": ""}
            if f_obj and is_broken_or_placeholder_url(f_obj.get("url")):
                cleaned_data["file"] = {**f_obj, "url": ""}
            block[b_type] = cleaned_data
            block["_is_unsupported_external_embed"] = True
            block["_attachment_uploaded"] = False
            block["service_name"] = block.get("service_name") or (
                "PDF埋め込み" if b_type == "pdf" else "ファイル埋め込み"
            )
            block["_unsupported_reason"] = "broken_or_placeholder_url"
        else:
            app_info = classify_external_app_url(target_url)
            if app_info:
                svc_name, _ = app_info
                block["_is_unsupported_external_embed"] = True
                block["_attachment_uploaded"] = False
                block["service_name"] = block.get("service_name") or svc_name
                block["_unsupported_reason"] = "external_app_auth_required"
        return block

    return block


def _retry_notion_call(fn, max_retries: int = 4, base_delay: float = 1.0):
    """Notion API呼び出しの一時的接続断やレートリミットを指数バックオフで再試行する。"""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err_str = str(e).lower()
            if "linked database" in err_str:
                raise
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"Notion API error ({e}), retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(delay)


@dataclass
class PageNode:
    id: str
    title: str
    node_type: str  # "page" | "database" | "database_row"
    icon: Optional[str] = None  # 絵文字アイコン (例: "🚀", "📝")
    cover: Optional[Dict[str, Any]] = None  # カバー画像
    url: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    children: List["PageNode"] = field(default_factory=list)
    comments: List[Dict[str, Any]] = field(default_factory=list)
    created_time: Optional[str] = None
    created_by: Optional[Dict[str, Any]] = None
    last_edited_time: Optional[str] = None
    last_edited_by: Optional[Dict[str, Any]] = None
    _cover_filename: Optional[str] = None
    external_synced_blocks: List[Dict[str, Any]] = field(default_factory=list)


def collect_external_synced_blocks_from_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """ブロックツリーから本文未展開の外部同期ブロック情報を再帰的に収集する。"""
    found: List[Dict[str, Any]] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        b_type = b.get("type")
        children = b.get("children_blocks") or []
        if b_type == "synced_block":
            synced_from = (b.get("synced_block") or {}).get("synced_from")
            sync_ref = synced_from.get("block_id") if isinstance(synced_from, dict) else None
            is_ext = bool(b.get("_is_external_synced_block"))
            if (is_ext and not children) or (not children and sync_ref):
                found.append({
                    "block_id": b.get("id", ""),
                    "synced_from": sync_ref,
                    "message": b.get(
                        "_external_synced_warning",
                        "外部同期ブロックの本文が未展開です"
                        + (f" (同期元: {sync_ref})" if sync_ref else ""),
                    ),
                })
        if children:
            found.extend(collect_external_synced_blocks_from_blocks(children))
    return found


def get_node_external_synced_blocks(node: Any) -> List[Dict[str, Any]]:
    """PageNodeに記録された未展開の外部同期ブロック一覧を取得する。"""
    recorded = getattr(node, "external_synced_blocks", None)
    if recorded:
        return list(recorded)
    return collect_external_synced_blocks_from_blocks(getattr(node, "blocks", None) or [])


class NotionExtractor:
    def __init__(self, token: str):
        self.token = token
        self.notion = Client(auth=token, notion_version="2022-06-28")

    def extract_tree(self, root_page_id: str) -> PageNode:
        """指定されたルートページから配下の全ページ・ブロック・DBを再帰的に抽出する。"""
        logger.info(f"Extracting Notion page: {root_page_id}")
        page_info = _retry_notion_call(lambda: self.notion.pages.retrieve(page_id=root_page_id))
        title = self._get_page_title(page_info)
        url = page_info.get("url")

        from src.icon_mapper import resolve_notion_icon_to_emoji

        icon_obj = page_info.get("icon") or {}
        icon_emoji = resolve_notion_icon_to_emoji(icon_obj)
        cover_obj = page_info.get("cover")

        root_node = PageNode(
            id=root_page_id,
            title=title,
            node_type="page",
            icon=icon_emoji,
            cover=cover_obj,
            url=url,
            created_time=page_info.get("created_time"),
            created_by=page_info.get("created_by"),
            last_edited_time=page_info.get("last_edited_time"),
            last_edited_by=page_info.get("last_edited_by"),
        )
        if cover_obj:
            root_node._cover_filename = f"cover_{root_page_id.replace('-', '')[:8]}.jpg"

        # ページ最上位コメントの抽出
        try:
            c_res = _retry_notion_call(lambda: self.notion.comments.list(block_id=root_page_id))
            root_node.comments = c_res.get("results", [])
            if root_node.comments:
                logger.info(f"  Extracted {len(root_node.comments)} top-level comments for page [{title}]")
        except Exception as e:
            logger.debug(f"  Could not extract comments for page {root_page_id}: {e}")

        # ブロックツリーと子ページの抽出
        root_node.blocks, root_node.children = self._extract_blocks_and_children(root_page_id)
        return root_node

    def _get_page_title(self, page_info: Dict[str, Any]) -> str:
        props = page_info.get("properties", {})
        for _, prop in props.items():
            if prop.get("type") == "title":
                title_list = prop.get("title", [])
                if title_list:
                    return "".join(t.get("plain_text", "") for t in title_list)
        return "Untitled Page"

    def _get_db_title(self, db_info: Dict[str, Any]) -> str:
        title_list = db_info.get("title", [])
        if title_list:
            return "".join(t.get("plain_text", "") for t in title_list)
        return "Untitled Database"

    def _extract_blocks_and_children(self, block_id: str, visited_synced: Optional[Set[str]] = None):
        """ブロック直下の子ブロック群をページネーション対応で全件取得し、
        child_page / child_database を子ノードとして再帰抽出する。"""
        if visited_synced is None:
            visited_synced = set()
        blocks = []
        child_nodes: List[PageNode] = []

        cursor = None
        while True:
            res = _retry_notion_call(lambda: self.notion.blocks.children.list(block_id=block_id, start_cursor=cursor, page_size=100))
            results = res.get("results", [])

            for b in results:
                b_type = b.get("type")

                if b_type == "child_page":
                    # 子ページを発見
                    child_id = b["id"]
                    child_title = b.get("child_page", {}).get("title", "Untitled Page")
                    logger.info(f"  Found child page: [{child_title}] ({child_id})")
                    child_node = self.extract_tree(child_id)
                    child_nodes.append(child_node)
                    # 本文内のリンクブロックとしても保持する
                    blocks.append(b)

                elif b_type == "child_database":
                    db_id = b["id"]
                    try:
                        # 1. 通常のデータベース取得を試行 (2022-06-28)
                        db_node = self._extract_database(db_id)
                        child_nodes.append(db_node)
                        b["database_title"] = db_node.title
                        blocks.append(b)
                        logger.info(f"  Found and extracted regular database: [{db_node.title}] ({db_id})")
                    except Exception as e:
                        # 2. "linked database" の場合、2025-09-03 Views APIでリンク先とビュー定義を抽出
                        err_str = str(e)
                        if "linked database" in err_str.lower():
                            logger.info(f"  Detected linked database view: ({db_id})")
                            try:
                                headers_v2 = {
                                    "Authorization": f"Bearer {self.token}",
                                    "Notion-Version": "2025-09-03",
                                    "Content-Type": "application/json",
                                }
                                db_meta_res = requests.get(f"https://api.notion.com/v1/databases/{db_id}", headers=headers_v2, timeout=15)
                                if db_meta_res.ok:
                                    db_meta = db_meta_res.json()
                                    data_sources = db_meta.get("data_sources", [])
                                    views_info = []
                                    v_res = requests.get(f"https://api.notion.com/v1/views?database_id={db_id}", headers=headers_v2, timeout=15)
                                    if v_res.ok:
                                        for v in v_res.json().get("results", []):
                                            vid = v["id"]
                                            v_detail = requests.get(f"https://api.notion.com/v1/views/{vid}", headers=headers_v2, timeout=15)
                                            if v_detail.ok:
                                                vd = v_detail.json()
                                                views_info.append({
                                                    "name": vd.get("name"),
                                                    "type": vd.get("type"),
                                                    "filter": vd.get("filter"),
                                                    "configuration": vd.get("configuration", {}),
                                                })
                                    b["is_linked_database"] = True
                                    b["data_sources"] = data_sources
                                    b["views"] = views_info
                                    src_name = data_sources[0].get("name") if data_sources else "Unknown"
                                    logger.info(f"  Extracted linked view pointing to: [{src_name}] (Views: {[v['name'] for v in views_info]})")
                                    blocks.append(b)
                            except Exception as le:
                                logger.warning(f"Error inspecting linked view {db_id}: {le}")
                        else:
                            logger.warning(f"Error extracting database {db_id}: {e}")

                elif b_type == "synced_block":
                    # 同期ブロック: 元ブロックの参照(synced_from)がある場合は同期元ブロックから子要素を取得
                    synced_data = b.get("synced_block") or {}
                    synced_from = synced_data.get("synced_from")
                    target_id = synced_from.get("block_id") if synced_from else b["id"]

                    if target_id not in visited_synced:
                        new_visited = set(visited_synced)
                        new_visited.add(target_id)
                        nested_blocks, nested_children = self._extract_blocks_and_children(target_id, new_visited)
                        b["children_blocks"] = nested_blocks
                        child_nodes.extend(nested_children)
                    else:
                        logger.warning(f"Circular synced_block reference detected for {target_id}")
                        b["children_blocks"] = []
                    blocks.append(b)

                else:
                    # 通常のブロック
                    # ネストされたブロック（toggle, bulleted_list等）があれば再帰的に取得
                    if b.get("has_children"):
                        nested_blocks, nested_children = self._extract_blocks_and_children(b["id"], visited_synced)
                        b["children_blocks"] = nested_blocks
                        child_nodes.extend(nested_children)
                    normalize_notion_block(b)
                    blocks.append(b)

            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")

        return blocks, child_nodes

    def _extract_database(self, db_id: str) -> PageNode:
        """データベースのメタデータと全レコード（各レコードのプロパティおよび本文）を抽出する。"""
        db_info = _retry_notion_call(lambda: self.notion.databases.retrieve(database_id=db_id))
        title = self._get_db_title(db_info)
        schema = db_info.get("properties", {})

        from src.icon_mapper import resolve_notion_icon_to_emoji

        db_icon_obj = db_info.get("icon") or {}
        db_icon = resolve_notion_icon_to_emoji(db_icon_obj)

        db_node = PageNode(
            id=db_id,
            title=title,
            node_type="database",
            icon=db_icon,
            url=db_info.get("url"),
            properties={"schema": schema},
            created_time=db_info.get("created_time"),
            created_by=db_info.get("created_by"),
            last_edited_time=db_info.get("last_edited_time"),
            last_edited_by=db_info.get("last_edited_by"),
        )

        # 全レコードを取得 (クエリ)
        cursor = None
        while True:
            query_kwargs = {"page_size": 100}
            if cursor:
                query_kwargs["start_cursor"] = cursor
            res = _retry_notion_call(lambda: self.notion.request(path=f"databases/{db_id}/query", method="POST", body=query_kwargs))
            results = res.get("results", [])

            for row in results:
                row_id = row["id"]
                row_title = self._get_page_title(row)
                row_props = self._parse_row_properties(row.get("properties", {}))

                # レコード内のブロックを取得
                row_blocks, row_children = self._extract_blocks_and_children(row_id)

                row_icon_obj = row.get("icon") or {}
                row_icon = resolve_notion_icon_to_emoji(row_icon_obj)

                row_node = PageNode(
                    id=row_id,
                    title=row_title,
                    node_type="database_row",
                    icon=row_icon,
                    url=row.get("url"),
                    properties=row_props,
                    blocks=row_blocks,
                    children=row_children,
                    created_time=row.get("created_time"),
                    created_by=row.get("created_by"),
                    last_edited_time=row.get("last_edited_time"),
                    last_edited_by=row.get("last_edited_by"),
                )
                db_node.children.append(row_node)

            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")

        logger.info(f"  Extracted database [{title}] with {len(db_node.children)} rows.")
        return db_node

    def _parse_row_properties(self, props: Dict[str, Any]) -> Dict[str, Any]:
        """Notionレコードのプロパティを型情報や色情報を含めた辞書形式に整理する。"""
        parsed = {}
        for name, p in props.items():
            p_type = p.get("type")
            if p_type == "title":
                val = "".join(t.get("plain_text", "") for t in p.get("title", []))
                parsed[name] = {"type": "title", "value": val}
            elif p_type == "rich_text":
                val = "".join(t.get("plain_text", "") for t in p.get("rich_text", []))
                parsed[name] = {"type": "rich_text", "value": val}
            elif p_type in ("select", "status"):
                opt = p.get(p_type)
                if opt:
                    parsed[name] = {
                        "type": p_type,
                        "value": opt.get("name", ""),
                        "color": opt.get("color", "default"),
                    }
                else:
                    parsed[name] = {"type": p_type, "value": None, "color": "default"}
            elif p_type == "multi_select":
                items = [
                    {"value": opt.get("name", ""), "color": opt.get("color", "default")}
                    for opt in p.get("multi_select", [])
                ]
                parsed[name] = {"type": "multi_select", "items": items}
            elif p_type == "date":
                dt = p.get("date")
                if dt:
                    parsed[name] = {
                        "type": "date",
                        "start": dt.get("start"),
                        "end": dt.get("end"),
                        "time_zone": dt.get("time_zone"),
                    }
                else:
                    parsed[name] = {"type": "date", "start": None, "end": None}
            elif p_type == "checkbox":
                parsed[name] = {"type": "checkbox", "value": p.get("checkbox", False)}
            elif p_type == "url":
                parsed[name] = {"type": "url", "value": p.get("url")}
            elif p_type == "number":
                parsed[name] = {"type": "number", "value": p.get("number")}
            elif p_type == "email":
                parsed[name] = {"type": "email", "value": p.get("email")}
            elif p_type == "phone_number":
                parsed[name] = {"type": "phone_number", "value": p.get("phone_number")}
            elif p_type == "people":
                people_list = [
                    {"id": u.get("id"), "name": u.get("name", "Unknown User")}
                    for u in p.get("people", [])
                ]
                parsed[name] = {"type": "people", "users": people_list}
            elif p_type == "relation":
                relations = [{"id": r.get("id")} for r in p.get("relation", [])]
                parsed[name] = {"type": "relation", "relations": relations}
            elif p_type == "rollup":
                rollup_data = p.get("rollup", {})
                r_type = rollup_data.get("type")
                if r_type == "array":
                    items = []
                    for item in rollup_data.get("array", []):
                        sub_type = item.get("type")
                        if sub_type in ("select", "status"):
                            opt = item.get(sub_type, {})
                            items.append({
                                "type": sub_type,
                                "value": opt.get("name"),
                                "color": opt.get("color", "default"),
                            })
                        elif sub_type == "title":
                            val = "".join(t.get("plain_text", "") for t in item.get("title", []))
                            items.append({"type": "title", "value": val})
                        elif sub_type == "number":
                            items.append({"type": "number", "value": item.get("number")})
                        elif sub_type == "date":
                            dt = item.get("date", {})
                            items.append({"type": "date", "start": dt.get("start") if dt else None})
                        else:
                            items.append({"type": sub_type, "value": str(item.get(sub_type, ""))})
                    parsed[name] = {"type": "rollup", "rollup_type": "array", "items": items}
                elif r_type == "number":
                    parsed[name] = {"type": "rollup", "rollup_type": "number", "value": rollup_data.get("number")}
                elif r_type == "date":
                    dt = rollup_data.get("date", {})
                    parsed[name] = {"type": "rollup", "rollup_type": "date", "start": dt.get("start") if dt else None}
                else:
                    parsed[name] = {"type": "rollup", "rollup_type": r_type, "value": str(rollup_data.get(r_type, ""))}
            elif p_type == "formula":
                form_data = p.get("formula", {})
                f_type = form_data.get("type", "string")
                val = form_data.get(f_type)
                parsed[name] = {"type": "formula", "value": val}
            else:
                raw_val = p.get(p_type)
                parsed[name] = {"type": p_type or "unknown", "value": str(raw_val) if raw_val is not None else ""}
        return parsed
