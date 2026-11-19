"""
Notion からページ、ブロック階層、データベースおよびレコードを再帰的に抽出するモジュール。
"""

import logging
import re
import time
import unicodedata
import urllib.parse
import requests
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from notion_client import Client

logger = logging.getLogger(__name__)


def normalize_notion_id(raw_id: Optional[str]) -> str:
    """Notion ID（UUID）からハイフンを除去し小文字正規化する。"""
    if not raw_id:
        return ""
    return str(raw_id).replace("-", "").strip().lower()


def normalize_db_title(title: Optional[str]) -> str:
    """データベースタイトルやコレクション名の記号・OS禁止文字・空白差分を正規化して比較可能にする。"""
    if not title:
        return ""
    t = unicodedata.normalize("NFC", str(title).strip())
    # ファイル名禁止文字 (: / \ ? * " < > | 等) やピリオド (. ．) を空白に置換
    t = re.sub(r"[:：/\\?*\"<>|.．]+", " ", t)
    # 連続空白を単一スペースにして前後の空白を除去し、小文字化
    return re.sub(r"\s+", " ", t).strip().lower()


DEFAULT_DB_CHILD_RATE_THRESHOLD: float = 20.0


def database_row_has_substantive_body(row_node: Any) -> bool:
    """データベース行が実質的な本文コンテンツまたは配下の子ページを持つか判定する。"""
    if getattr(row_node, "children", None):
        return True
    for b in (getattr(row_node, "blocks", None) or []):
        if not isinstance(b, dict):
            continue
        b_type = b.get("type")
        if b_type != "paragraph":
            return True
        if b.get("children_blocks"):
            return True
        for rt in (b.get("paragraph") or {}).get("rich_text", []):
            if not isinstance(rt, dict):
                continue
            if (rt.get("plain_text") or "").strip():
                return True
            if rt.get("href") or rt.get("type") == "mention":
                return True
            link_obj = (rt.get("text") or {}).get("link") or {}
            if isinstance(link_obj, dict) and link_obj.get("url"):
                return True
    return False


def is_empty_database_row(row_node: Any) -> bool:
    """データベース行が完全に空レコード（タイトル無題、本文なし、プロパティ値なし）であるかを判定する。"""
    if getattr(row_node, "node_type", "") != "database_row":
        return False
    title_clean = (getattr(row_node, "title", None) or "").strip()
    is_untitled = not title_clean or title_clean in ("Untitled Page", "無題", "Untitled")
    if not is_untitled or database_row_has_substantive_body(row_node):
        return False
    for k, v in (getattr(row_node, "properties", None) or {}).items():
        if k in ("schema", "title"):
            continue
        if v not in (None, "", [], {}):
            return False
    return True


def analyze_database_child_page_stats(db_node: Any) -> Dict[str, Any]:
    """データベース配下の行レコードについて、本文/子ページ存在数・存在率・子ページスキップ可否を集計する。"""
    children = getattr(db_node, "children", None) or []
    active_rows = [c for c in children if not is_empty_database_row(c)]
    total_rows = len(active_rows)
    body_rows = sum(1 for c in active_rows if database_row_has_substantive_body(c))
    body_ratio_pct = (body_rows / total_rows * 100.0) if total_rows > 0 else 0.0
    return {
        "database_id": str(getattr(db_node, "id", "") or ""),
        "database_title": str(getattr(db_node, "title", "") or ""),
        "total_rows": total_rows,
        "body_rows": body_rows,
        "body_ratio_pct": body_ratio_pct,
        "skip_child_pages": body_rows == 0,
    }


def format_low_child_rate_warning(stat: Dict[str, Any]) -> str:
    """低子ページ存在率データベース向けの統一WARNINGメッセージを生成する。"""
    return (
        f"子ページ存在率が低いデータベースを検出しました "
        f"[{stat['database_title']}] ({stat['body_rows']}/{stat['total_rows']}件, "
        f"{stat['body_ratio_pct']:.1f}% < {stat['threshold_pct']:.1f}%)。"
        f"不要な行本文/子ページがある場合は移行前にNotion側で削除してください。"
    )


def collect_low_child_rate_databases(
    root_node: Any,
    threshold_pct: float = DEFAULT_DB_CHILD_RATE_THRESHOLD,
) -> List[Dict[str, Any]]:
    """ツリー内のデータベースのうち、0%超かつ閾値(%)未満の子ページ存在率のものを収集する。"""
    if not root_node or threshold_pct is None or threshold_pct <= 0:
        return []
    found: List[Dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if getattr(node, "node_type", "") == "database":
            stats = analyze_database_child_page_stats(node)
            if stats["body_rows"] > 0 and stats["body_ratio_pct"] < threshold_pct:
                stats["threshold_pct"] = float(threshold_pct)
                stats["message"] = format_low_child_rate_warning(stats)
                found.append(stats)
        for c in (getattr(node, "children", None) or []):
            _walk(c)

    _walk(root_node)
    return found

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


def _rich_text_has_content(rt_list: Any) -> bool:
    """rich_text / caption 配列に空白以外の有効なテキストが含まれるか判定する。"""
    if not isinstance(rt_list, list):
        return False
    for rt in rt_list:
        if isinstance(rt, dict):
            plain = rt.get("plain_text")
            if isinstance(plain, str) and plain.strip():
                return True
            text_obj = rt.get("text")
            if isinstance(text_obj, dict) and str(text_obj.get("content") or "").strip():
                return True
        elif isinstance(rt, str) and rt.strip():
            return True
    return False


def is_substanceless_unsupported_block(block: Dict[str, Any]) -> bool:
    """
    未対応ページ要素のうち、保持すべき実質的情報（有効URL・添付実体・ラベル・キャプション・子要素）が
    一切無い「実質的に無意味」な要素かを判定する。
    """
    if not is_unsupported_page_element_block(block):
        return False

    # 1. ローカル添付実体や子ブロックがある場合は意味ありとして保持
    if block.get("_attachment_bytes") or block.get("_attachment_zip_entry"):
        return False
    if block.get("children_blocks"):
        return False

    b_type = str(block.get("type") or "")
    data = block.get(b_type) if isinstance(block.get(b_type), dict) else {}

    # 2. 有効な外部URLがある場合はリンク保持のため意味あり
    raw_url = extract_block_url(block)
    allow_notion = b_type == "bookmark"
    if (
        raw_url
        and raw_url.lower().startswith(("http://", "https://"))
        and not is_broken_or_placeholder_url(raw_url, allow_notion_page_url=allow_notion)
    ):
        return False

    # 3. ユーザー入力ラベル・キャプション・リッチテキストがある場合は情報保持のため意味あり
    raw_label = str(block.get("raw_label") or block.get("_display_name") or "").strip()
    if raw_label and not is_broken_or_placeholder_url(raw_label, allow_notion_page_url=allow_notion):
        return False
    if _rich_text_has_content(data.get("caption")) or _rich_text_has_content(data.get("rich_text")):
        return False
    for title_key in ("title", "name", "text"):
        val = data.get(title_key)
        if isinstance(val, str) and val.strip() and not is_broken_or_placeholder_url(val):
            return False

    # 4. 上記すべてが空であれば、空ウィジェット残骸 / 空breadcrumb / 空button等として実質無意味と判定
    return True


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
    unresolved_external_dbs: List[Dict[str, Any]] = field(default_factory=list)
    unresolved_external_pages: List[Dict[str, Any]] = field(default_factory=list)
    missing_zip_attachments: List[Dict[str, Any]] = field(default_factory=list)


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


def collect_unsupported_page_elements_from_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """ブロックツリーから実質情報を含む未対応要素・外部アプリ埋め込みを収集する。"""
    found: List[Dict[str, Any]] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if is_unsupported_page_element_block(b) and not is_substanceless_unsupported_block(b):
            b_type = str(b.get("type") or "unsupported")
            svc = str(b.get("service_name") or b.get("_unsupported_service_name") or "").strip()
            raw_label = str(b.get("raw_label") or b.get("_display_name") or "").strip()
            via_sync = bool(b.get("_via_synced_block"))
            via_suffix = " [同期ブロック経由]" if via_sync else ""
            label_parts = [p for p in (svc, raw_label) if p]
            target_label = " / ".join(label_parts) if label_parts else b_type
            found.append({
                "block_id": str(b.get("id", "")),
                "type": b_type,
                "service_name": svc,
                "via_synced_block": via_sync,
                "message": f"未対応要素・外部埋め込み '{target_label}' ({b_type}) をフォールバック注記に変換します{via_suffix}",
            })
        children = b.get("children_blocks") or []
        if children:
            found.extend(collect_unsupported_page_elements_from_blocks(children))
    return found


def get_node_unsupported_page_elements(node: Any) -> List[Dict[str, Any]]:
    """PageNode内の実質情報を含む未対応要素一覧を取得する。"""
    return collect_unsupported_page_elements_from_blocks(getattr(node, "blocks", None) or [])


def collect_missing_zip_attachments_from_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """ブロックツリーからZIP内に実体ファイルが見つからなかったローカル添付/メディアを収集する。"""
    found: List[Dict[str, Any]] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if b.get("_attachment_missing_in_zip"):
            b_type = str(b.get("type") or "file")
            filename = str(
                b.get("_display_name")
                or b.get("_attachment_filename")
                or b.get("_missing_attachment_src")
                or "不明ファイル"
            ).strip()
            via_sync = bool(b.get("_via_synced_block"))
            via_suffix = " [同期ブロック経由]" if via_sync else ""
            default_msg = (
                f"ZIP未収録の添付ファイル・メディア '{filename}' ({b_type}) を検出しました{via_suffix}"
            )
            found.append({
                "block_id": str(b.get("id", "")),
                "type": b_type,
                "filename": filename,
                "src": str(b.get("_missing_attachment_src") or ""),
                "via_synced_block": via_sync,
                "message": b.get("_missing_attachment_warning") or default_msg,
            })
        children = b.get("children_blocks") or []
        if children:
            found.extend(collect_missing_zip_attachments_from_blocks(children))
    return found


def get_node_missing_zip_attachments(node: Any) -> List[Dict[str, Any]]:
    """PageNodeに記録されたZIP未収録添付ファイル・メディア一覧を取得する。"""
    recorded = getattr(node, "missing_zip_attachments", None)
    if recorded:
        return list(recorded)
    return collect_missing_zip_attachments_from_blocks(getattr(node, "blocks", None) or [])


_ATTACHMENT_LINK_EXTENSIONS = (
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".aac",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".csv", ".txt",
)
_INLINE_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


def is_notion_page_link_target(href: Optional[str], page_id: Optional[str] = None) -> bool:
    """リンク先がNotionの内部ページ参照（ローカル .html または notion.so/site ページURL）かを判定する。"""
    clean_href = (href or "").strip()
    if clean_href.startswith("#"):
        return False
    if clean_href.lower().startswith(("mailto:", "tel:", "javascript:", "attachment:")):
        return False
    if not clean_href:
        return bool(page_id)
    parsed = urllib.parse.urlparse(clean_href)
    path_lower = urllib.parse.unquote(parsed.path or "").strip().lower()
    if path_lower.endswith(_ATTACHMENT_LINK_EXTENSIONS):
        return False
    is_http = parsed.scheme.lower() in ("http", "https")
    if is_http:
        host = (parsed.hostname or "").lower()
        if any(h in host for h in ("notion.so", "notion.site", "notion.com")):
            return bool(page_id) or bool(_INLINE_UUID_RE.search(clean_href))
        return False
    if page_id:
        return True
    return path_lower.endswith(".html") or bool(_INLINE_UUID_RE.search(clean_href))


def _extract_rich_text_page_ref(rt: Dict[str, Any]) -> Tuple[str, Optional[str], str, str]:
    """rich_text要素から (href, page_id, plain_text, kind) を抽出する。mention(page) も対象とする。"""
    link_obj = (rt.get("text") or {}).get("link") if isinstance(rt.get("text"), dict) else None
    href = (link_obj.get("url") if isinstance(link_obj, dict) else None) or rt.get("href") or ""
    item_page_id = rt.get("page_id")
    kind = "inline_link"
    mention_obj = rt.get("mention") if rt.get("type") == "mention" and isinstance(rt.get("mention"), dict) else None
    if mention_obj and mention_obj.get("type") == "page":
        page_m = mention_obj.get("page") or {}
        if isinstance(page_m, dict) and page_m.get("id"):
            item_page_id = item_page_id or page_m.get("id")
        kind = "page_mention"
    plain = str(rt.get("plain_text") or (rt.get("text") or {}).get("content", "") or "").strip()
    return href, item_page_id, plain, kind


def _iter_block_rich_text_lists(block: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    """ブロック内の rich_text 配列群（table_row の cells 含む）を取得する。"""
    lists: List[List[Dict[str, Any]]] = []
    b_type = block.get("type")
    if isinstance(b_type, str) and isinstance(block.get(b_type), dict):
        rt = block[b_type].get("rich_text")
        if isinstance(rt, list):
            lists.append(rt)
        if b_type == "table_row":
            cells = block[b_type].get("cells")
            if isinstance(cells, list):
                for cell in cells:
                    if isinstance(cell, list):
                        lists.append(cell)
    return lists


def _match_node_ref(
    candidate_ids: List[str],
    title: str,
    known_node_ids: set,
    known_node_titles: set,
    allow_substring_title: bool = True,
) -> bool:
    if any(cid in known_node_ids for cid in candidate_ids):
        return True
    clean_t = (title or "").strip()
    if not clean_t or clean_t in ("Untitled Page", "Subpage"):
        return False
    for kt in known_node_titles:
        if kt == clean_t:
            return True
        if allow_substring_title and (kt.endswith(clean_t) or kt.startswith(clean_t)):
            return True
    return False


def collect_unresolved_external_pages_from_blocks(
    blocks: List[Dict[str, Any]],
    known_node_ids: Optional[set] = None,
    known_node_titles: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """ブロックツリーから移行対象外（ZIP/ツリー未収録）の外部ページ参照を再帰的に収集する。"""
    found: List[Dict[str, Any]] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        b_type = b.get("type")
        children = b.get("children_blocks") or []
        via_sync = bool(b.get("_via_synced_block"))
        via_suffix = " [同期ブロック経由]" if via_sync else ""

        if b_type == "child_page":
            title = str((b.get("child_page") or {}).get("title") or "").strip()
            raw_id = str(b.get("id") or "")
            c_ids = [raw_id.replace("-", "").lower()] if _INLINE_UUID_RE.search(raw_id) else []
            if known_node_ids is not None or known_node_titles is not None:
                is_unresolved = not _match_node_ref(
                    c_ids, title, known_node_ids or set(), known_node_titles or set()
                )
            else:
                is_unresolved = bool(b.get("_is_unresolved_external_page"))
            if is_unresolved:
                label = title or raw_id or "外部ページ"
                found.append({
                    "block_id": raw_id,
                    "target_id": raw_id,
                    "target_title": label,
                    "kind": "child_page",
                    "via_synced_block": via_sync,
                    "message": b.get("_external_page_warning")
                    or f"移行対象外（ZIP未収録）の外部ページ '{label}' を参照しています{via_suffix}",
                })

        elif b_type == "link_to_page":
            link_data = b.get("link_to_page") or {}
            page_id = str(link_data.get("page_id") or "")
            if page_id:
                c_ids = [page_id.replace("-", "").lower()]
                if known_node_ids is not None or known_node_titles is not None:
                    is_unresolved = not _match_node_ref(
                        c_ids, "", known_node_ids or set(), known_node_titles or set(), allow_substring_title=False
                    )
                else:
                    is_unresolved = bool(b.get("_is_unresolved_external_page"))
                if is_unresolved:
                    found.append({
                        "block_id": str(b.get("id", "")),
                        "target_id": page_id,
                        "target_title": page_id,
                        "kind": "link_to_page",
                        "via_synced_block": via_sync,
                        "message": b.get("_external_page_warning")
                        or f"移行対象外（ZIP未収録）の外部ページリンク (ID: {page_id}) を参照しています{via_suffix}",
                    })

        # インラインリンク・ページメンション (rich_text) の未収録ページ参照
        for rt_list in _iter_block_rich_text_lists(b):
            for rt in rt_list:
                if not isinstance(rt, dict):
                    continue
                href, item_page_id, plain, ref_kind = _extract_rich_text_page_ref(rt)

                if known_node_ids is not None or known_node_titles is not None:
                    if not is_notion_page_link_target(href, item_page_id):
                        continue
                    c_ids = []
                    if item_page_id:
                        c_ids.append(str(item_page_id).replace("-", "").lower())
                    if href:
                        for m in _INLINE_UUID_RE.findall(href):
                            c_ids.append(m.replace("-", "").lower())
                    is_unresolved = not _match_node_ref(
                        c_ids,
                        plain if not c_ids else "",
                        known_node_ids or set(),
                        known_node_titles or set(),
                        allow_substring_title=False,
                    )
                else:
                    is_unresolved = bool(rt.get("_is_unresolved_external_page"))

                if is_unresolved:
                    rt_via_sync = bool(rt.get("_via_synced_block") or via_sync)
                    rt_via_suffix = " [同期ブロック経由]" if rt_via_sync else ""
                    label = plain or href or str(item_page_id or "外部ページ")
                    found.append({
                        "block_id": str(b.get("id", "")),
                        "target_id": str(item_page_id or ""),
                        "target_title": label,
                        "href": href,
                        "kind": ref_kind,
                        "via_synced_block": rt_via_sync,
                        "message": rt.get("_external_page_warning")
                        or f"移行対象外（ZIP未収録）の外部ページ '{label}' への文中リンクを含んでいます{rt_via_suffix}",
                    })

        if children:
            found.extend(
                collect_unresolved_external_pages_from_blocks(
                    children, known_node_ids=known_node_ids, known_node_titles=known_node_titles
                )
            )
    return found


def get_node_unresolved_external_pages(node: Any) -> List[Dict[str, Any]]:
    """PageNodeに記録された未解決の外部ページ参照一覧を取得する。"""
    recorded = getattr(node, "unresolved_external_pages", None)
    if recorded:
        return list(recorded)
    return collect_unresolved_external_pages_from_blocks(getattr(node, "blocks", None) or [])


def collect_unresolved_external_dbs_from_blocks(
    blocks: List[Dict[str, Any]],
    known_db_ids: Optional[set] = None,
    known_db_titles: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """ブロックツリーから移行対象外（ZIP/ツリー未収録）の外部データベース参照を再帰的に収集する。"""
    found: List[Dict[str, Any]] = []
    norm_known_titles = {normalize_db_title(t) for t in (known_db_titles or set()) if t}
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        b_type = b.get("type")
        children = b.get("children_blocks") or []
        if b_type == "child_database" and b.get("is_linked_database"):
            data_sources = b.get("data_sources") or []
            source_name = ""
            if data_sources and isinstance(data_sources[0], dict):
                source_name = str(data_sources[0].get("name") or "").strip()
            candidate_ids = []
            for cid in (
                b.get("database_id"),
                b.get("id"),
                data_sources[0].get("id") if data_sources and isinstance(data_sources[0], dict) else None,
            ):
                if cid:
                    candidate_ids.append(str(cid).replace("-", "").lower())

            is_unresolved = False
            if known_db_ids is not None or known_db_titles is not None:
                id_matched = any(cid in (known_db_ids or set()) for cid in candidate_ids)
                norm_source = normalize_db_title(source_name)
                title_matched = bool(
                    norm_source
                    and any(
                        kt == norm_source or kt.endswith(norm_source) or norm_source.endswith(kt)
                        for kt in norm_known_titles
                    )
                )
                is_unresolved = not (id_matched or title_matched)
            else:
                is_unresolved = bool(b.get("_is_unresolved_external_db"))

            if is_unresolved:
                db_id = str(b.get("database_id") or b.get("id") or "")
                db_label = source_name or db_id or "Unknown DB"
                via_sync = bool(b.get("_via_synced_block"))
                via_suffix = " [同期ブロック経由]" if via_sync else ""
                default_msg = (
                    f"移行対象外（ZIP未収録）の外部データベース '{db_label}' を参照しています"
                    f"{via_suffix}"
                    + (f" (ID: {db_id})" if db_id else "")
                )
                found.append({
                    "block_id": str(b.get("id", "")),
                    "database_id": db_id,
                    "database_name": db_label,
                    "via_synced_block": via_sync,
                    "message": b.get("_external_db_warning") or default_msg,
                })
        elif b_type == "link_to_page" and (b.get("link_to_page") or {}).get("database_id"):
            db_id = str((b.get("link_to_page") or {}).get("database_id") or "")
            norm_db_id = db_id.replace("-", "").lower()
            if known_db_ids is not None:
                is_unresolved = norm_db_id not in known_db_ids
            else:
                is_unresolved = bool(b.get("_is_unresolved_external_db"))
            if is_unresolved:
                via_sync = bool(b.get("_via_synced_block"))
                via_suffix = " [同期ブロック経由]" if via_sync else ""
                found.append({
                    "block_id": str(b.get("id", "")),
                    "database_id": db_id,
                    "database_name": db_id,
                    "via_synced_block": via_sync,
                    "message": b.get("_external_db_warning")
                    or f"移行対象外（ZIP未収録）の外部データベース (ID: {db_id}) を参照しています{via_suffix}",
                })
        if children:
            found.extend(
                collect_unresolved_external_dbs_from_blocks(
                    children, known_db_ids=known_db_ids, known_db_titles=known_db_titles
                )
            )
    return found


def mark_unresolved_external_references(root_node: Any) -> int:
    """PageNodeツリー全体を走査し、未解決の外部DB・外部ページ・ZIP未収録添付を統一マーキングする。"""
    if not root_node:
        return 0

    known_db_ids = set()
    known_db_titles = set()
    db_title_to_id: Dict[str, str] = {}
    known_node_ids = set()
    known_node_titles = set()

    def _collect_nodes(node: Any) -> None:
        nid = getattr(node, "id", None)
        ntitle = getattr(node, "title", None)
        ntype = getattr(node, "node_type", None)
        if nid:
            known_node_ids.add(str(nid).replace("-", "").lower())
        if ntitle:
            known_node_titles.add(str(ntitle).strip())
        if ntype == "database":
            if nid:
                known_db_ids.add(str(nid).replace("-", "").lower())
            if ntitle:
                s_title = str(ntitle).strip()
                known_db_titles.add(s_title)
                norm_t = normalize_db_title(s_title)
                if norm_t and nid and norm_t not in db_title_to_id:
                    db_title_to_id[norm_t] = str(nid)
        for c in getattr(node, "children", None) or []:
            _collect_nodes(c)

    _collect_nodes(root_node)

    total_unresolved = 0

    def _mark_node(node: Any) -> None:
        nonlocal total_unresolved
        blocks = getattr(node, "blocks", None) or []

        def _update_flags(blks: List[Dict[str, Any]]) -> None:
            for b in blks:
                if not isinstance(b, dict):
                    continue
                b_type = b.get("type")
                via_sync = bool(b.get("_via_synced_block"))
                via_suffix = " [同期ブロック経由]" if via_sync else ""

                # 1. 外部DB参照フラグの更新
                if b_type == "child_database" and b.get("is_linked_database"):
                    data_sources = b.get("data_sources") or []
                    source_name = ""
                    if data_sources and isinstance(data_sources[0], dict):
                        source_name = str(data_sources[0].get("name") or "").strip()
                    candidate_ids = []
                    for cid in (
                        b.get("database_id"),
                        b.get("id"),
                        data_sources[0].get("id") if data_sources and isinstance(data_sources[0], dict) else None,
                    ):
                        if cid:
                            candidate_ids.append(str(cid).replace("-", "").lower())
                    id_matched = any(cid in known_db_ids for cid in candidate_ids)
                    norm_src = normalize_db_title(source_name)
                    matched_db_id = None
                    if norm_src in db_title_to_id:
                        matched_db_id = db_title_to_id[norm_src]
                    else:
                        for kt, k_id in db_title_to_id.items():
                            if kt.endswith(norm_src) or norm_src.endswith(kt):
                                matched_db_id = k_id
                                break

                    title_matched = matched_db_id is not None
                    if id_matched or title_matched:
                        b.pop("_is_unresolved_external_db", None)
                        b.pop("_external_db_warning", None)
                        # ID一致していない場合のみ、タイトル一致で特定した実DB IDを補完する
                        if not id_matched and matched_db_id:
                            norm_cur_db_id = str(b.get("database_id") or "").replace("-", "").lower()
                            if not norm_cur_db_id or norm_cur_db_id not in known_db_ids:
                                b["database_id"] = matched_db_id
                            if data_sources and isinstance(data_sources[0], dict):
                                norm_ds_id = str(data_sources[0].get("id") or "").replace("-", "").lower()
                                if not norm_ds_id or norm_ds_id not in known_db_ids:
                                    data_sources[0]["id"] = matched_db_id
                    else:
                        db_id = str(b.get("database_id") or b.get("id") or "")
                        db_label = source_name or db_id or "Unknown DB"
                        b["_is_unresolved_external_db"] = True
                        b["_external_db_warning"] = (
                            f"移行対象外（ZIP未収録）の外部データベース '{db_label}' を参照しています"
                            f"{via_suffix}"
                            + (f" (ID: {db_id})" if db_id else "")
                        )
                elif b_type == "link_to_page" and (b.get("link_to_page") or {}).get("database_id"):
                    db_id = str((b.get("link_to_page") or {}).get("database_id") or "")
                    if db_id.replace("-", "").lower() in known_db_ids:
                        b.pop("_is_unresolved_external_db", None)
                        b.pop("_external_db_warning", None)
                    else:
                        b["_is_unresolved_external_db"] = True
                        b["_external_db_warning"] = (
                            f"移行対象外（ZIP未収録）の外部データベース (ID: {db_id}) を参照しています{via_suffix}"
                        )

                # 2. 外部ページ参照フラグの更新 (ブロック型: child_page / link_to_page)
                if b_type == "child_page":
                    title = str((b.get("child_page") or {}).get("title") or "").strip()
                    raw_id = str(b.get("id") or "")
                    c_ids = [raw_id.replace("-", "").lower()] if _INLINE_UUID_RE.search(raw_id) else []
                    if _match_node_ref(c_ids, title, known_node_ids, known_node_titles):
                        b.pop("_is_unresolved_external_page", None)
                        b.pop("_external_page_warning", None)
                    else:
                        label = title or raw_id or "外部ページ"
                        b["_is_unresolved_external_page"] = True
                        b["_external_page_warning"] = (
                            f"移行対象外（ZIP未収録）の外部ページ '{label}' を参照しています{via_suffix}"
                        )
                elif b_type == "link_to_page" and (b.get("link_to_page") or {}).get("page_id"):
                    page_id = str((b.get("link_to_page") or {}).get("page_id") or "")
                    if page_id.replace("-", "").lower() in known_node_ids:
                        b.pop("_is_unresolved_external_page", None)
                        b.pop("_external_page_warning", None)
                    else:
                        b["_is_unresolved_external_page"] = True
                        b["_external_page_warning"] = (
                            f"移行対象外（ZIP未収録）の外部ページリンク (ID: {page_id}) を参照しています{via_suffix}"
                        )

                # 3. 外部ページ参照フラグの更新 (文中インラインリンク・ページメンション)
                for rt_list in _iter_block_rich_text_lists(b):
                    for rt in rt_list:
                        if not isinstance(rt, dict):
                            continue
                        href, item_page_id, plain, _ = _extract_rich_text_page_ref(rt)
                        if not is_notion_page_link_target(href, item_page_id):
                            rt.pop("_is_unresolved_external_page", None)
                            rt.pop("_external_page_warning", None)
                            continue
                        c_ids = []
                        if item_page_id:
                            c_ids.append(str(item_page_id).replace("-", "").lower())
                        if href:
                            for m in _INLINE_UUID_RE.findall(href):
                                c_ids.append(m.replace("-", "").lower())
                        if _match_node_ref(
                            c_ids,
                            plain if not c_ids else "",
                            known_node_ids,
                            known_node_titles,
                            allow_substring_title=False,
                        ):
                            rt.pop("_is_unresolved_external_page", None)
                            rt.pop("_external_page_warning", None)
                        else:
                            rt_via_sync = bool(rt.get("_via_synced_block") or via_sync)
                            rt_via_suffix = " [同期ブロック経由]" if rt_via_sync else ""
                            label = plain or href or str(item_page_id or "外部ページ")
                            rt["_is_unresolved_external_page"] = True
                            if rt_via_sync:
                                rt["_via_synced_block"] = True
                            rt["_external_page_warning"] = (
                                f"移行対象外（ZIP未収録）の外部ページ '{label}' への文中リンクを含んでいます{rt_via_suffix}"
                            )

                if b.get("children_blocks"):
                    _update_flags(b["children_blocks"])

        _update_flags(blocks)
        ext_dbs = collect_unresolved_external_dbs_from_blocks(
            blocks, known_db_ids=known_db_ids, known_db_titles=known_db_titles
        )
        ext_pages = collect_unresolved_external_pages_from_blocks(
            blocks, known_node_ids=known_node_ids, known_node_titles=known_node_titles
        )
        missing_atts = collect_missing_zip_attachments_from_blocks(blocks)
        node.unresolved_external_dbs = ext_dbs
        node.unresolved_external_pages = ext_pages
        node.missing_zip_attachments = missing_atts
        node_total = len(ext_dbs) + len(ext_pages) + len(missing_atts)
        if node_total > 0:
            total_unresolved += node_total
            if ext_dbs:
                logger.debug(
                    f"Unresolved external database reference detected in [{getattr(node, 'title', '')}] "
                    f"({getattr(node, 'id', '')}): {len(ext_dbs)} item(s)"
                )
            if ext_pages:
                logger.debug(
                    f"Unresolved external page reference detected in [{getattr(node, 'title', '')}] "
                    f"({getattr(node, 'id', '')}): {len(ext_pages)} item(s)"
                )
            if missing_atts:
                logger.warning(
                    f"Missing ZIP attachment detected in [{getattr(node, 'title', '')}] "
                    f"({getattr(node, 'id', '')}): {len(missing_atts)} item(s)"
                )
        for c in getattr(node, "children", None) or []:
            _mark_node(c)

    _mark_node(root_node)
    return total_unresolved


def mark_unresolved_external_dbs(root_node: Any) -> int:
    """後方互換エイリアス: PageNodeツリー全体の未解決参照（外部DB/外部ページ/ZIP欠落添付）を走査・マーキングする。"""
    return mark_unresolved_external_references(root_node)


def get_node_unresolved_external_dbs(node: Any) -> List[Dict[str, Any]]:
    """PageNodeに記録された未解決の外部データベース参照一覧を取得する。"""
    recorded = getattr(node, "unresolved_external_dbs", None)
    if recorded:
        return list(recorded)
    return collect_unresolved_external_dbs_from_blocks(getattr(node, "blocks", None) or [])


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


def is_individual_child_pages_database(
    db_node: Any,
    individual_db_child_threshold: int = 0,
) -> bool:
    """--individual-db-child-threshold 指定時、本文あり子ページ数が閾値以下(0件超)なら個別子ページ(従来方式)にするか判定する。"""
    if not individual_db_child_threshold or int(individual_db_child_threshold) <= 0:
        return False
    db_stats = analyze_database_child_page_stats(db_node)
    return bool(
        db_stats["total_rows"] > 0
        and 0 < db_stats["body_rows"] <= int(individual_db_child_threshold)
    )


def count_migratable_nodes(
    root_node: Any,
    individual_db_child_threshold: int = 0,
    db_mode_overrides: Optional[Dict[str, str]] = None,
) -> int:
    """Confluence上で実際に作成・移行されるノード数（ダッシュボード母数と一致）を算出する。"""
    if not root_node:
        return 0

    def _get_db_override(n: Any) -> Optional[str]:
        if not db_mode_overrides:
            return None
        nid = normalize_notion_id(getattr(n, "id", "") or "")
        return db_mode_overrides.get(nid) or db_mode_overrides.get(getattr(n, "id", "") or "")

    def _count(n: Any) -> int:
        ntype = getattr(n, "node_type", "")
        if ntype == "database_row" and is_empty_database_row(n):
            return 0
        if ntype == "database":
            override_mode = _get_db_override(n)
            if override_mode == "skip":
                return 0
            if analyze_database_child_page_stats(n)["skip_child_pages"]:
                return 1
            if override_mode == "individual" or (
                override_mode is None
                and is_individual_child_pages_database(
                    n,
                    individual_db_child_threshold=individual_db_child_threshold,
                )
            ):
                return 1 + sum(_count(c) for c in (getattr(n, "children", None) or []))
            # aggregated または static_table: 本体1枚 + 集約詳細ページ1枚 + レコード配下のネスト子ページ/DB
            nested_count = sum(
                _count(sub)
                for c in (getattr(n, "children", None) or [])
                for sub in (getattr(c, "children", None) or [])
            )
            return 2 + nested_count
        return 1 + sum(_count(c) for c in (getattr(n, "children", None) or []))

    return _count(root_node)
