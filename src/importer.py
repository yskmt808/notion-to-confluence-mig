"""
Confluence REST API (v1) を利用してページツリーを再現・作成・更新するモジュール。
"""

import html
import io
import json
import logging
import mimetypes
import os
import random
import re
import time
import unicodedata
import urllib.parse
import concurrent.futures
import threading
import datetime
import ipaddress
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests
from src.extractor import (
    PageNode,
    analyze_database_child_page_stats,
    classify_external_app_url,
    count_migratable_nodes,
    database_row_has_substantive_body,
    is_broken_or_placeholder_url,
    is_empty_database_row as _shared_is_empty_database_row,
    is_individual_child_pages_database,
    is_unsupported_page_element_block,
    normalize_db_title,
)
from src.converter import NotionToConfluenceConverter
from src.checkpoint import CheckpointWriteError
from src.db_triage import resolve_db_migration_mode

logger = logging.getLogger(__name__)

ATTACHMENT_MIME_MAP = {
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
}


def detect_attachment_mime_type(filename: str) -> str:
    """ファイル名から適切なMIMEタイプを判定する。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in ATTACHMENT_MIME_MAP:
        return ATTACHMENT_MIME_MAP[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _is_safe_external_url(url: str) -> bool:
    """外部URLの安全性（SSRF防止）を検証する。http/httpsスキームのみを許可し、ループバック・リンクローカル・プライベートIPを拒否する。"""
    if not url or not isinstance(url, str):
        return False
    if is_broken_or_placeholder_url(url, allow_notion_page_url=True):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower()
        if not hostname or hostname in ("localhost", "ip6-localhost", "ip6-loopback"):
            return False
        if hostname.endswith(".local") or hostname.endswith(".internal"):
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False



def render_latex_to_png(expr: str, dpi: int = 300, fontsize: int = 18) -> bytes:
    """LaTeX式をローカルPC上で完全オフラインで高解像度PNG画像にレンダリングする。
    情報セキュリティ要件に準拠し、外部サービスへの通信は一切行わない。"""
    # 式の前後の $ マークを除去（fig.text内でラップするため）
    clean_expr = expr.strip()
    if clean_expr.startswith("$$") and clean_expr.endswith("$$"):
        clean_expr = clean_expr[2:-2].strip()
    elif clean_expr.startswith("$") and clean_expr.endswith("$"):
        clean_expr = clean_expr[1:-1].strip()

    fig = plt.figure(figsize=(0.01, 0.01))
    fig.text(0, 0, f"${clean_expr}$", fontsize=fontsize)
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", transparent=False, dpi=dpi, pad_inches=0.05)
    plt.close(fig)
    return buffer.getvalue()


class ConfluenceClient:
    """Confluence Cloud REST API の直接ラッパー"""

    def __init__(self, base_url: str, username: str, token: str):
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url.endswith("/wiki"):
            self.base_url = f"{self.base_url}/wiki"
        self.auth = (username, token)
        self.headers = {"Content-Type": "application/json"}
        self._space_title_cache: Dict[Tuple[str, str, str], Optional[Dict[str, Any]]] = {}

    def get_page_by_title(
        self, space_key: str, title: str, title_prefix: str = "", cql_fallback: bool = False, use_cache: bool = False
    ) -> Optional[Dict[str, Any]]:
        cache_key = (space_key, "page", title, title_prefix)
        if use_cache and cache_key in self._space_title_cache:
            return self._space_title_cache[cache_key]

        if title and len(title) > 255:
            title = title[:252] + "..."
        url = f"{self.base_url}/rest/api/content"
        params = {
            "spaceKey": space_key,
            "title": title,
            "expand": "version,ancestors",
        }
        res = requests.get(url, params=params, auth=self.auth, timeout=30)
        if res.status_code == 200:
            results = res.json().get("results", [])
            if results:
                if use_cache:
                    self._space_title_cache[cache_key] = results[0]
                return results[0]

        # title_prefix がある場合のフォールバック検索
        if title_prefix and not title.startswith(title_prefix):
            prefixed = f"{title_prefix}{title}"
            if len(prefixed) > 255:
                prefixed = prefixed[:252] + "..."
            res_pref = requests.get(url, params={"spaceKey": space_key, "title": prefixed, "expand": "version,ancestors"}, auth=self.auth, timeout=30)
            if res_pref.status_code == 200:
                results_pref = res_pref.json().get("results", [])
                if results_pref:
                    if use_cache:
                        self._space_title_cache[cache_key] = results_pref[0]
                    return results_pref[0]

        # CQLによる柔軟なタイトル検索フォールバック (オプトイン)
        if cql_fallback:
            safe_title = title.replace('\\', '\\\\').replace('"', '\\"')
            safe_space = space_key.replace('\\', '\\\\').replace('"', '\\"')
            cql = f'type = page AND space = "{safe_space}" AND title ~ "{safe_title}"'
            try:
                cql_url = f"{self.base_url}/rest/api/content/search"
                cql_res = requests.get(cql_url, params={"cql": cql}, auth=self.auth, timeout=30)
                if cql_res.status_code == 200:
                    results = cql_res.json().get("results", [])
                    norm_target = normalize_db_title(title)
                    expected_trunc = title[:252] + "..." if len(title) > 255 else title
                    for r in results:
                        r_title = r.get("title", "")
                        if r_title == title or r_title == expected_trunc:
                            if use_cache:
                                self._space_title_cache[cache_key] = r
                            return r
                        if norm_target and normalize_db_title(r_title) == norm_target:
                            if use_cache:
                                self._space_title_cache[cache_key] = r
                            return r
                        if title_prefix:
                            norm_pref = normalize_db_title(f"{title_prefix}{title}")
                            if norm_pref and normalize_db_title(r_title) == norm_pref:
                                if use_cache:
                                    self._space_title_cache[cache_key] = r
                                return r
            except Exception as e:
                logger.debug(f"CQL page search error for [{title}]: {e}")

        if use_cache:
            self._space_title_cache[cache_key] = None
        return None

    def get_page_by_id(self, page_id: str, expand: str = "version,ancestors") -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/rest/api/content/{page_id}"
        params = {"expand": expand}
        res = requests.get(url, params=params, auth=self.auth, timeout=30)
        if res.status_code == 200:
            return res.json()
        return None

    def get_database_by_id(self, database_id: str) -> Optional[Dict[str, Any]]:
        """Confluence Databases (v2) を ID から取得する。存在しない場合は None を返す。"""
        url = f"{self.base_url}/api/v2/databases/{database_id}"
        try:
            res = requests.get(url, auth=self.auth, timeout=30)
            if res.status_code == 200:
                return res.json()
            # v1 content API でもフォールバック確認
            url_v1 = f"{self.base_url}/rest/api/content/{database_id}"
            res_v1 = requests.get(url_v1, auth=self.auth, timeout=30)
            if res_v1.status_code == 200:
                return res_v1.json()
        except Exception as e:
            logger.warning(f"Error checking database {database_id} existence: {e}")
        return None

    def get_page_attachments(self, page_id: str) -> List[Dict[str, Any]]:
        """指定されたConfluenceページの添付ファイル一覧を取得する。"""
        url = f"{self.base_url}/rest/api/content/{page_id}/child/attachment"
        try:
            res = requests.get(url, auth=self.auth, timeout=30)
            if res.status_code == 200:
                return res.json().get("results", [])
        except Exception as e:
            logger.warning(f"Failed to get attachments for page {page_id}: {e}")
        return []

    def move_page_position(self, page_id: str, position: str, target_id: str) -> bool:
        """ページをターゲットページに対して相対配置（before / after / append）で並び替える。"""
        url = f"{self.base_url}/rest/api/content/{page_id}/move/{position}/{target_id}"
        try:
            res = requests.put(url, auth=self.auth, timeout=15)
            return res.ok
        except Exception as e:
            logger.warning(f"Failed to move page {page_id} {position} {target_id}: {e}")
            return False

    def create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        parent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if title and len(title) > 255:
            title = title[:252] + "..."
        url = f"{self.base_url}/rest/api/content"
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": body,
                    "representation": "storage",
                }
            },
        }
        if parent_id:
            payload["ancestors"] = [{"id": str(parent_id)}]

        max_retries = 3
        for attempt in range(max_retries):
            res = requests.post(url, json=payload, auth=self.auth, headers=self.headers, timeout=30)
            if res.status_code in (200, 201):
                return res.json()
            elif res.status_code == 409 and attempt < max_retries - 1:
                # 409 Conflict: 親コンテナのロック競合・同時作成衝突時はジッター付き指数バックオフで再試行 (WARNING)
                backoff = (2 ** attempt) + random.uniform(0.5, 1.5)
                logger.warning(
                    f"[409 Conflict] Retrying create_page for [{title}] under parent {parent_id} "
                    f"(attempt {attempt + 1}/{max_retries}) after {backoff:.2f}s..."
                )
                time.sleep(backoff)
                continue
            else:
                err_msg = f"Failed to create page [{title}] under parent {parent_id}: {res.status_code} - {res.text}"
                logger.error(err_msg)
                raise RuntimeError(err_msg)

    def create_comment(self, page_id: str, body_html: str) -> Dict[str, Any]:
        """ページに対して最上位コメントを追加する。"""
        url = f"{self.base_url}/rest/api/content"
        payload = {
            "type": "comment",
            "container": {"id": str(page_id), "type": "page"},
            "body": {
                "storage": {
                    "value": body_html,
                    "representation": "storage",
                }
            },
        }
        res = requests.post(url, json=payload, auth=self.auth, headers=self.headers, timeout=30)
        if res.status_code not in (200, 201):
            logger.warning(f"Failed to add comment to page {page_id}: {res.status_code} - {res.text}")
            return {}
        return res.json()

    def update_page(
        self,
        page_id: str,
        space_key: str,
        title: str,
        body: str,
        current_version: int,
        parent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if title and len(title) > 255:
            title = title[:252] + "..."
        url = f"{self.base_url}/rest/api/content/{page_id}"
        payload = {
            "id": page_id,
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": body,
                    "representation": "storage",
                }
            },
            "version": {"number": current_version + 1},
        }
        if parent_id:
            payload["ancestors"] = [{"id": str(parent_id)}]

        max_retries = 3
        for attempt in range(max_retries):
            if attempt > 0:
                try:
                    latest = self.get_page_by_id(page_id, expand="version")
                    if latest and "version" in latest:
                        current_version = latest["version"]["number"]
                        payload["version"]["number"] = current_version + 1
                except Exception as ve:
                    logger.warning(f"Could not fetch latest page version for [{title}] ({page_id}): {ve}")

            res = requests.put(url, json=payload, auth=self.auth, headers=self.headers, timeout=30)
            if res.status_code == 200:
                return res.json()
            elif res.status_code == 409 and attempt < max_retries - 1:
                backoff = (2 ** attempt) + random.uniform(0.5, 1.5)
                logger.warning(
                    f"[409 Conflict] Retrying update_page for [{title}] ({page_id}) "
                    f"(attempt {attempt + 1}/{max_retries}) after {backoff:.2f}s..."
                )
                time.sleep(backoff)
                continue
            else:
                err_msg = f"Failed to update page [{title}] ({page_id}): {res.status_code} - {res.text}"
                logger.error(err_msg)
                raise RuntimeError(err_msg)

    def upload_attachment(self, page_id: str, filename: str, file_bytes: bytes, mime_type: Optional[str] = None):
        """Confluenceページにファイルを添付アップロードする。"""
        if not mime_type or mime_type == "application/octet-stream":
            mime_type = detect_attachment_mime_type(filename)
        url = f"{self.base_url}/rest/api/content/{page_id}/child/attachment"
        headers = {"X-Atlassian-Token": "nocheck"}
        files = {"file": (filename, file_bytes, mime_type)}

        # 既存添付ファイルがあるか確認
        try:
            get_res = requests.get(url, params={"filename": filename}, auth=self.auth, timeout=30)
            if get_res.status_code == 200:
                results = get_res.json().get("results", [])
                if results:
                    att_id = results[0]["id"]
                    # 既存添付ファイルを更新アップロード
                    update_url = f"{url}/{att_id}/data"
                    res = requests.post(update_url, auth=self.auth, headers=headers, files=files, timeout=60)
                    if res.status_code == 200:
                        return res.json()
        except Exception as e:
            logger.warning(f"Error checking existing attachment {filename}: {e}")

        res = requests.post(url, auth=self.auth, headers=headers, files=files, timeout=60)
        if res.status_code not in (200, 201):
            logger.warning(f"Failed to upload attachment [{filename}] to page {page_id}: {res.status_code} - {res.text}")
        return res.json() if res.status_code in (200, 201) else None

    def set_page_emoji(self, page_id: str, emoji_char: str):
        """Notionの絵文字をConfluenceのページタイトル絵文字プロパティに設定する。"""
        if not emoji_char:
            return
        from src.icon_mapper import resolve_notion_icon_to_emoji, is_unicode_emoji
        resolved = resolve_notion_icon_to_emoji(emoji_char)
        if not resolved or not is_unicode_emoji(resolved):
            return
        cps = [f"{ord(c):x}" for c in resolved if ord(c) != 0xfe0f]
        if not cps:
            return
        emoji_hex = "-".join(cps)

        for prop_key in ("emoji-title-published", "emoji-title-draft"):
            url = f"{self.base_url}/rest/api/content/{page_id}/property/{prop_key}"
            for attempt in range(3):
                try:
                    get_res = requests.get(url, auth=self.auth, timeout=15)
                    if get_res.status_code == 200:
                        prop_data = get_res.json()
                        version = prop_data.get("version", {}).get("number", 1) + 1
                        res = requests.put(
                            url,
                            json={"key": prop_key, "value": emoji_hex, "version": {"number": version}},
                            auth=self.auth,
                            headers=self.headers,
                            timeout=15,
                        )
                    else:
                        post_url = f"{self.base_url}/rest/api/content/{page_id}/property"
                        res = requests.post(
                            post_url,
                            json={"key": prop_key, "value": emoji_hex},
                            auth=self.auth,
                            headers=self.headers,
                            timeout=15,
                        )
                    if res.status_code == 429:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    if res.status_code in (200, 201):
                        break
                    else:
                        logger.warning(f"Failed setting {prop_key} on page {page_id}: HTTP {res.status_code}")
                        break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(1.0)
                    else:
                        logger.warning(f"Exception setting page emoji property for {page_id}: {e}")

    def set_page_cover(self, page_id: str, cover_id: str, position: int = 50) -> bool:
        """Confluenceのページヘッダーカバー画像プロパティ（cover-picture-id-published / cover-picture-id-draft）を設定する。"""
        if not cover_id:
            return False
        try:
            pos_val = max(0, min(100, int(position if position is not None else 50)))
        except Exception:
            pos_val = 50
        val_str = json.dumps({"id": str(cover_id), "position": pos_val}, separators=(",", ":"))
        success_any = False

        for prop_key in ("cover-picture-id-published", "cover-picture-id-draft"):
            url = f"{self.base_url}/rest/api/content/{page_id}/property/{prop_key}"
            for attempt in range(3):
                try:
                    get_res = requests.get(url, auth=self.auth, timeout=15)
                    if get_res.status_code == 200:
                        prop_data = get_res.json()
                        version = prop_data.get("version", {}).get("number", 1) + 1
                        res = requests.put(
                            url,
                            json={"key": prop_key, "value": val_str, "version": {"number": version}},
                            auth=self.auth,
                            headers=self.headers,
                            timeout=15,
                        )
                    else:
                        post_url = f"{self.base_url}/rest/api/content/{page_id}/property"
                        res = requests.post(
                            post_url,
                            json={"key": prop_key, "value": val_str},
                            auth=self.auth,
                            headers=self.headers,
                            timeout=15,
                        )
                    if res.status_code == 429:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    if res.status_code in (200, 201):
                        success_any = True
                        break
                    else:
                        logger.warning(f"Failed setting {prop_key} on page {page_id}: HTTP {res.status_code}")
                        break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(1.0)
                    else:
                        logger.warning(f"Exception setting page cover property {prop_key} for {page_id}: {e}")
        return success_any

    def get_page_property(self, page_id: str, prop_key: str) -> Optional[Dict[str, Any]]:
        """指定したConfluenceページのContent Propertyを取得する。"""
        url = f"{self.base_url}/rest/api/content/{page_id}/property/{prop_key}"
        try:
            res = requests.get(url, auth=self.auth, timeout=15)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.debug(f"Error fetching page property {prop_key} for {page_id}: {e}")
        return None

    def delete_page(self, page_id: str) -> bool:
        """Confluenceページを削除する。"""
        url = f"{self.base_url}/rest/api/content/{page_id}"
        res = requests.delete(url, auth=self.auth, timeout=30)
        return res.status_code in (200, 204)

    def get_database_by_title(
        self,
        space_key: str,
        title: str,
        title_prefix: str = "",
        use_cache: bool = False,
        allow_fuzzy_suffix: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Confluence Databases (type: database) をタイトルとスペースから検索する。"""
        cache_key = (space_key, "database", title, title_prefix, allow_fuzzy_suffix)
        if use_cache and cache_key in self._space_title_cache:
            return self._space_title_cache[cache_key]

        if title and len(title) > 255:
            title = title[:252] + "..."
        url = f"{self.base_url}/rest/api/content/search"
        safe_title = title.replace('\\', '\\\\').replace('"', '\\"')
        safe_space = space_key.replace('\\', '\\\\').replace('"', '\\"')
        cql = f'type = database AND space = "{safe_space}" AND title ~ "{safe_title}"'
        res = requests.get(url, params={"cql": cql}, auth=self.auth, timeout=30)
        results = []
        if res.status_code == 200:
            results = res.json().get("results", [])

        # もし見つからず title_prefix がある場合、prefix 付き CQL も試行
        if not results and title_prefix and not title.startswith(title_prefix):
            pref_title = f"{title_prefix}{title}"
            safe_pref_title = pref_title.replace('\\', '\\\\').replace('"', '\\"')
            cql_pref = f'type = database AND space = "{safe_space}" AND title ~ "{safe_pref_title}"'
            res_pref = requests.get(url, params={"cql": cql_pref}, auth=self.auth, timeout=30)
            if res_pref.status_code == 200:
                results = res_pref.json().get("results", [])

        if results:
            expected_trunc = title[:252] + "..." if len(title) > 255 else title
            norm_target = normalize_db_title(title)
            pref_trunc = (f"{title_prefix}{title}"[:252] + "...") if title_prefix else ""
            norm_pref = normalize_db_title(f"{title_prefix}{title}") if title_prefix else ""

            # title_prefix 指定時はプレフィックス付き完全一致・正規化一致を最優先
            if title_prefix and not title.startswith(title_prefix):
                for r in results:
                    r_title = r.get("title", "")
                    if r_title == f"{title_prefix}{title}" or (pref_trunc and r_title == pref_trunc):
                        if use_cache:
                            self._space_title_cache[cache_key] = r
                        return r
                if norm_pref:
                    for r in results:
                        r_title = r.get("title", "")
                        if normalize_db_title(r_title) == norm_pref:
                            if use_cache:
                                self._space_title_cache[cache_key] = r
                            return r

            # 1. 完全一致
            for r in results:
                r_title = r.get("title", "")
                if r_title == title or r_title == expected_trunc:
                    if use_cache:
                        self._space_title_cache[cache_key] = r
                    return r

            # 2. プレフィックス付き完全一致
            if title_prefix:
                for r in results:
                    r_title = r.get("title", "")
                    if r_title == f"{title_prefix}{title}" or (pref_trunc and r_title == pref_trunc):
                        if use_cache:
                            self._space_title_cache[cache_key] = r
                        return r

            # 3. タイトル正規化一致（記号・コロン・空白差分）
            if norm_target:
                for r in results:
                    r_title = r.get("title", "")
                    if normalize_db_title(r_title) == norm_target:
                        if use_cache:
                            self._space_title_cache[cache_key] = r
                        return r

            # 4. プレフィックス付きタイトル正規化一致
            if norm_pref:
                for r in results:
                    r_title = r.get("title", "")
                    if normalize_db_title(r_title) == norm_pref:
                        if use_cache:
                            self._space_title_cache[cache_key] = r
                        return r

            # 5. 接尾辞一致（allow_fuzzy_suffix=True かつ 異プレフィックス誤爆しない場合のみ）
            if allow_fuzzy_suffix and norm_target and len(norm_target) >= 3 and not title_prefix:
                for r in results:
                    r_title = r.get("title", "")
                    norm_r = normalize_db_title(r_title)
                    if norm_r and norm_r.endswith(norm_target):
                        if use_cache:
                            self._space_title_cache[cache_key] = r
                        return r

        if use_cache:
            self._space_title_cache[cache_key] = None
        return None

    def get_space_id(self, space_key: str) -> Optional[str]:
        """スペースキーからSpace IDを取得する。"""
        url = f"{self.base_url}/rest/api/space/{space_key}"
        res = requests.get(url, auth=self.auth, timeout=30)
        if res.status_code == 200:
            s_id = res.json().get("id")
            return str(s_id) if s_id is not None else None
        return None

    def create_database(
        self,
        space_id: str,
        title: str,
        parent_id: Optional[str],
        csv_body: str,
    ) -> Dict[str, Any]:
        """Confluence Databases (type: database) を構造化CSVから新規作成する。"""
        if title and len(title) > 255:
            title = title[:252] + "..."
        url = f"{self.base_url}/api/v2/databases"
        payload = {
            "spaceId": str(space_id),
            "title": title,
            "body": {
                "representation": "csv",
                "value": csv_body,
            },
        }
        if parent_id:
            payload["parentId"] = str(parent_id)

        res = requests.post(url, json=payload, auth=self.auth, timeout=60)
        if res.status_code in (200, 201):
            return res.json()
        raise RuntimeError(f"Failed to create Confluence database [{title}]: {res.status_code} - {res.text}")

    def delete_database(self, database_id: str) -> bool:
        """Confluence Databases を削除する。"""
        url = f"{self.base_url}/api/v2/databases/{database_id}"
        res = requests.delete(url, auth=self.auth, timeout=30)
        return res.status_code in (200, 204)

    def update_database_field_options(
        self,
        database_id: str,
        single_select_fields: List[str],
        multi_select_fields: List[str],
    ) -> bool:
        """
        Confluence Databases のフィールド設定（単一選択/複数選択）を更新する。
        - single_select_fields: allow_multiple_values: false
        - multi_select_fields: allow_multiple_values: true
        """
        if not single_select_fields and not multi_select_fields:
            return True

        url_get = f"{self.base_url}/api/v2/databases/{database_id}"
        r_get = requests.get(url_get, auth=self.auth, timeout=30)
        if not r_get.ok:
            logger.warning(f"Failed to fetch database {database_id} for field option update: {r_get.status_code}")
            return False
        cur_version = r_get.json().get("version", {}).get("number", 1)

        metadata_lines = [
            "<metadata>",
            "operation_type,target,property,value"
        ]
        for f in single_select_fields:
            metadata_lines.append(f"edit_field,{f},allow_multiple_values,false")
        for f in multi_select_fields:
            metadata_lines.append(f"edit_field,{f},allow_multiple_values,true")
        metadata_lines.append("</metadata>")

        all_fields = single_select_fields + multi_select_fields
        data_lines = [
            "<data>",
            f"operation_type,entry_index,{','.join(all_fields)}",
            "</data>"
        ]
        update_csv = "\n".join(metadata_lines + data_lines)

        url_put = f"{self.base_url}/api/v2/databases/{database_id}"
        payload = {
            "id": str(database_id),
            "version": {
                "number": cur_version + 1
            },
            "body": {
                "representation": "csv",
                "value": update_csv
            }
        }
        r_put = requests.put(url_put, json=payload, auth=self.auth, timeout=30)
        if r_put.ok:
            logger.info(f"Successfully configured field select options for database [{database_id}]")
            return True
        else:
            logger.warning(f"Failed to update database field select options: {r_put.status_code} - {r_put.text}")
            return False

    def get_database_entries(
        self,
        database_id: str,
        expected_count: int = 1,
        timeout_sec: int = 45,
    ) -> List[Dict[str, Any]]:
        """Confluence Databases のエントリ一覧を取得する（インデックス完了までポーリング待機・ページネーション対応）。"""
        base_entries_url = f"{self.base_url}/api/v2/databases/{database_id}/entries"
        start_time = time.time()
        last_results: List[Dict[str, Any]] = []
        while time.time() - start_time < timeout_sec:
            try:
                all_results: List[Dict[str, Any]] = []
                next_url: Optional[str] = base_entries_url
                is_first = True
                while next_url:
                    if is_first:
                        res = requests.get(next_url, params={"limit": 100}, auth=self.auth, timeout=15)
                        is_first = False
                    else:
                        res = requests.get(next_url, auth=self.auth, timeout=15)
                    if not res.ok:
                        break
                    data = res.json()
                    page_results = data.get("results") or []
                    if not isinstance(page_results, list):
                        page_results = []
                    all_results.extend(page_results)
                    next_link = (data.get("_links") or {}).get("next")
                    if next_link and page_results:
                        if next_link.startswith(("http://", "https://")):
                            next_url = next_link
                        elif next_link.startswith("/api/") and self.base_url.rstrip("/").endswith("/wiki"):
                            next_url = f"{self.base_url.rstrip('/')}{next_link}"
                        else:
                            next_url = urllib.parse.urljoin(self.base_url, next_link)
                    else:
                        next_url = None
                if all_results:
                    last_results = all_results
                if len(all_results) >= expected_count:
                    return all_results
            except Exception:
                pass
            time.sleep(2)
        return last_results

    def get_database_views(self, database_id: str, retries: int = 3, delay: float = 2.0) -> List[Dict[str, Any]]:
        """Confluence Databases の定義済みビュー一覧を取得する。"""
        url = f"{self.base_url}/api/v2/databases/{database_id}/views"
        for attempt in range(retries):
            try:
                res = requests.get(url, auth=self.auth, timeout=15)
                if res.ok:
                    results = res.json().get("results") or []
                    if not isinstance(results, list):
                        results = []
                    if results or attempt == retries - 1:
                        return results
                elif res.status_code == 404 and attempt < retries - 1:
                    time.sleep(delay)
            except Exception as e:
                logger.warning(f"Failed to get database views for {database_id} (attempt {attempt + 1}): {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
        return []

    def get_database_fields(self, database_id: str) -> List[Dict[str, Any]]:
        """Confluence Databases のフィールド定義一覧 (/api/v2/databases/{id}/fields) を取得する。"""
        url = f"{self.base_url}/api/v2/databases/{database_id}/fields"
        try:
            res = requests.get(url, auth=self.auth, timeout=15)
            if res.ok:
                results = res.json().get("results", [])
                return results if isinstance(results, list) else []
        except Exception as e:
            logger.debug(f"Failed to get database fields for {database_id}: {e}")
        return []

    def create_database_view(
        self,
        database_id: str,
        view_name: str,
        layout: str = "table",
        filters: Optional[List[Dict[str, Any]]] = None,
        sorts: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Confluence Databases に新しいビュー（フィルタ・ソート付き）を自動作成する。
        - layout: "table", "card", "board", "list"
        - filters: [{"field": "ステータス", "operator": "!=", "value": "完了"}]
        - sorts: [{"field": "期日", "direction": "asc"}]
        """
        existing_views = self.get_database_views(database_id)
        for v in existing_views:
            if v.get("name") == view_name:
                return v

        url_get = f"{self.base_url}/api/v2/databases/{database_id}"
        r_get = requests.get(url_get, auth=self.auth, timeout=15)
        if not r_get.ok:
            return None
        cur_version = r_get.json().get("version", {}).get("number", 1)

        metadata_lines = [
            "<metadata>",
            f"add_view,{view_name},{layout}",
        ]
        if filters:
            for f in filters:
                field = f["field"]
                op = f.get("operator", "=")
                val = f["value"]
                metadata_lines.append(f'add_filter,{view_name},{field},"[\'{field}\' {op} \'{val}\']"')

        if sorts:
            for s in sorts:
                field = s["field"]
                direction = s.get("direction", "asc")
                metadata_lines.append(f'add_sort,{view_name},{field},"[\'{field}\' {direction}]"')

        metadata_lines.append("</metadata>")
        data_lines = [
            "<data>",
            "operation_type,entry_index",
            "</data>",
        ]
        csv_body = "\n".join(metadata_lines + data_lines)
        payload = {
            "id": str(database_id),
            "version": {"number": cur_version + 1},
            "body": {"representation": "csv", "value": csv_body},
        }
        r_put = requests.put(url_get, json=payload, auth=self.auth, timeout=30)
        if not r_put.ok:
            logger.warning(f"Failed to create view [{view_name}] on database [{database_id}]: {r_put.status_code} - {r_put.text}")
            return None

        new_views = self.get_database_views(database_id)
        for v in new_views:
            if v.get("name") == view_name:
                logger.info(f"Successfully created view [{view_name}] (ID: {v.get('id')}) on database [{database_id}]")
                return v
        return None

    def setup_grouped_database_views(
        self,
        database_id: str,
        group_field: str,
        group_values: List[str],
    ) -> List[Dict[str, Any]]:
        """
        データベースのGroup by対象列の各グループ値に対応するフィルタービューを一括作成し、
        [{"label": val, "saved_view_id": view_id}, ...] のリストを返す。
        """
        existing_views = {v["name"]: v for v in self.get_database_views(database_id)}
        results = []
        views_to_create = []

        for val in group_values:
            vname = f"{group_field}: {val}"
            if vname in existing_views:
                results.append({"label": val, "saved_view_id": existing_views[vname]["id"]})
            else:
                views_to_create.append(val)

        if views_to_create:
            url_get = f"{self.base_url}/api/v2/databases/{database_id}"
            r_get = requests.get(url_get, auth=self.auth, timeout=15)
            if r_get.ok:
                cur_version = r_get.json().get("version", {}).get("number", 1)
                metadata_lines = ["<metadata>"]
                for val in views_to_create:
                    vname = f"{group_field}: {val}"
                    metadata_lines.append(f"add_view,{vname},table")
                    metadata_lines.append(f'add_filter,{vname},{group_field},"[\'{group_field}\' = \'{val}\']"')
                metadata_lines.append("</metadata>")
                data_lines = ["<data>", "operation_type,entry_index", "</data>"]
                csv_body = "\n".join(metadata_lines + data_lines)
                payload = {
                    "id": str(database_id),
                    "version": {"number": cur_version + 1},
                    "body": {"representation": "csv", "value": csv_body},
                }
                r_put = requests.put(url_get, json=payload, auth=self.auth, timeout=30)
                if r_put.ok:
                    time.sleep(2)
                    updated_views = {v["name"]: v for v in self.get_database_views(database_id)}
                    for val in views_to_create:
                        vname = f"{group_field}: {val}"
                        if vname in updated_views:
                            results.append({"label": val, "saved_view_id": updated_views[vname]["id"]})

        return results

    def add_labels(self, page_id: str, labels: List[str]):
        """Confluenceページにラベル（タグ）を付与する。"""
        if not labels:
            return
        url = f"{self.base_url}/rest/api/content/{page_id}/label"
        payload = [{"prefix": "global", "name": re.sub(r'[^a-zA-Z0-9_\-]', '', lbl.lower().replace(' ', '-'))} for lbl in labels]
        try:
            res = requests.post(url, json=payload, auth=self.auth, headers=self.headers, timeout=15)
            if res.status_code == 200:
                logger.debug(f"Added labels {labels} to page {page_id}")
            else:
                logger.warning(f"Failed to add labels {labels} to page {page_id}: {res.status_code}")
        except Exception as e:
            logger.warning(f"Error adding labels to page {page_id}: {e}")

    def search_users(self, cql: str) -> List[Dict[str, Any]]:
        """CQLクエリでConfluenceユーザーを検索する (Task B.11)。"""
        url = f"{self.base_url}/rest/api/search/user"
        try:
            res = requests.get(url, params={"cql": cql}, auth=self.auth, timeout=15)
            if res.ok:
                return res.json().get("results", [])
        except Exception as e:
            logger.debug(f"search_users error with cql '{cql}': {e}")
        return []

    def get_current_user(self) -> Optional[Dict[str, Any]]:
        """現在のAPI認証ユーザー情報を取得する。"""
        url = f"{self.base_url}/rest/api/user/current"
        try:
            res = requests.get(url, auth=self.auth, timeout=15)
            if res.ok:
                return res.json()
            logger.warning(f"Failed to get current user: {res.status_code} - {res.text}")
        except Exception as e:
            logger.warning(f"Failed to get current user: {e}")
        return None

    def set_page_private(self, page_id: str, account_id: Optional[str] = None) -> bool:
        """ページを移行実行者のみが閲覧・編集可能なプライベート権限（read, update制限）に設定する。"""
        if not account_id:
            user_info = self.get_current_user()
            if user_info:
                account_id = user_info.get("accountId")
        if not account_id:
            logger.warning(f"Could not determine current user accountId. Skipping private restriction for page {page_id}.")
            return False

        url = f"{self.base_url}/rest/api/content/{page_id}/restriction"
        payload = [
            {
                "operation": "read",
                "restrictions": {
                    "user": [{"type": "known", "accountId": account_id}],
                    "group": [],
                },
            },
            {
                "operation": "update",
                "restrictions": {
                    "user": [{"type": "known", "accountId": account_id}],
                    "group": [],
                },
            },
        ]
        try:
            res = requests.put(url, json=payload, auth=self.auth, headers=self.headers, timeout=15)
            if res.ok:
                logger.info(f"Successfully set private restriction (read/update) for page {page_id} to user {account_id}")
                return True
            else:
                logger.warning(f"Failed to set private restriction for page {page_id}: {res.status_code} - {res.text}")
                return False
        except Exception as e:
            logger.warning(f"Error setting private restriction for page {page_id}: {e}")
            return False


class ConfluenceImporter:
    def __init__(
        self,
        url: str,
        username: str,
        token: str,
        space_key: str,
        checkpoint: Optional[Any] = None,
        cancel_manager: Optional[Any] = None,
        telemetry: Optional[Any] = None,
        page_workers: int = 8,
        timezone: Optional[Union[str, datetime.tzinfo]] = None,
        dashboard: Optional[Any] = None,
        user_mapper: Optional[Any] = None,
        metadata_template: Optional[str] = None,
        private_mode: bool = False,
        prune_blocks_on_pass2: bool = False,
        title_prefix: Optional[str] = None,
        external_mapping: Optional[Union[str, List[str]]] = None,
        individual_db_child_threshold: int = 0,
        db_mode_overrides: Optional[Dict[str, str]] = None,
        verify_resume_remote: bool = True,
    ):
        self.space_key = space_key
        self.checkpoint = checkpoint
        self.cancel_manager = cancel_manager
        self.telemetry = telemetry
        self.page_workers = max(1, page_workers)
        self.dashboard = dashboard
        self.user_mapper = user_mapper
        self.metadata_template = metadata_template
        self.private_mode = private_mode
        self.prune_blocks_on_pass2 = prune_blocks_on_pass2
        self.title_prefix = title_prefix or ""
        self.individual_db_child_threshold = max(0, int(individual_db_child_threshold or 0))
        self.db_mode_overrides: Dict[str, str] = dict(db_mode_overrides) if db_mode_overrides else {}
        self.verify_resume_remote = bool(verify_resume_remote)
        self._current_user_account_id: Optional[str] = None
        from src.timezone_util import resolve_timezone, format_iso_datetime
        self.timezone = resolve_timezone(timezone)
        self._lock = threading.RLock()
        self._parent_locks: Dict[str, threading.Lock] = {}
        self._reserved_titles: Set[str] = set()
        self._node_to_title: Dict[str, str] = {}
        self._db_view_requirements: Dict[str, Dict[str, Any]] = {}
        self.client = ConfluenceClient(url, username, token)
        if self.private_mode:
            current_user = self.client.get_current_user()
            if current_user:
                self._current_user_account_id = current_user.get("accountId")
            if self._current_user_account_id:
                logger.info(f"Private mode active: Root page will be restricted to accountId: {self._current_user_account_id}")
            else:
                warn_msg = "Private mode active, but could not determine current user accountId. Will retry at root node creation."
                logger.warning(warn_msg)
                if self.telemetry:
                    self.telemetry.record_warning(warn_msg)
        conv_kwargs = {"timezone": self.timezone, "user_mapper": self.user_mapper}
        if self.metadata_template is not None:
            conv_kwargs["metadata_template"] = self.metadata_template
        self.converter = NotionToConfluenceConverter(**conv_kwargs)
        self.converter.confluence_base_url = self.client.base_url
        self.converter.space_key = self.space_key
        self.migration_map: Dict[str, Dict[str, Any]] = {}

        if self.checkpoint:
            existing_map = self.checkpoint.get_migration_map()
            if existing_map:
                self.migration_map = existing_map
                for n_id, info in existing_map.items():
                    c_title = info.get("title")
                    if c_title:
                        self._reserved_titles.add(c_title)
                        self._node_to_title[n_id] = c_title
                logger.info(f"Initialized migration map with {len(existing_map)} nodes from checkpoint ({len(self._reserved_titles)} titles reserved).")

        if external_mapping:
            self._preload_external_mapping(external_mapping)

    def _preload_external_mapping(self, external_mapping_paths: Union[str, List[str]]) -> int:
        """先行移行の実行結果マッピングファイル (migration_result.json 等) を読み込み、初期マップへ反映する。"""
        if not external_mapping_paths:
            return 0
        if isinstance(external_mapping_paths, str):
            paths = [p.strip() for p in external_mapping_paths.split(",") if p.strip()]
        else:
            paths = list(external_mapping_paths)

        loaded_count = 0
        for p in paths:
            if not os.path.exists(p):
                logger.warning(f"External mapping file not found: {p}")
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                mapping_dict = data.get("mapping", data) if isinstance(data, dict) else {}
                if not isinstance(mapping_dict, dict):
                    continue

                for nid, ninfo in mapping_dict.items():
                    if not isinstance(ninfo, dict):
                        continue
                    norm_id = str(nid).replace("-", "").lower()
                    title = ninfo.get("title") or ""
                    conf_id = str(ninfo.get("confluence_id")) if ninfo.get("confluence_id") is not None else ""
                    url = ninfo.get("url") or (f"{self.client.base_url}/spaces/{self.space_key}/pages/{conf_id}" if conf_id else "")
                    node_type = ninfo.get("node_type", "page")

                    entry = {
                        "confluence_id": conf_id,
                        "title": title,
                        "url": url,
                        "node_type": node_type,
                    }
                    if "views" in ninfo and isinstance(ninfo["views"], dict):
                        entry["views"] = ninfo["views"]
                    if "views_info" in ninfo and isinstance(ninfo["views_info"], list):
                        entry["views_info"] = ninfo["views_info"]

                    self.migration_map[nid] = entry
                    if norm_id != nid:
                        self.migration_map[norm_id] = entry
                    if title:
                        self._reserved_titles.add(title)
                        self._node_to_title[nid] = title
                        self._node_to_title[norm_id] = title
                        if hasattr(self, "converter"):
                            if not hasattr(self.converter, "id_to_title_map") or self.converter.id_to_title_map is None:
                                self.converter.id_to_title_map = {}
                            self.converter.id_to_title_map[nid] = title
                            self.converter.id_to_title_map[norm_id] = title

                    loaded_count += 1
                logger.info(f"Loaded {loaded_count} entries from external mapping: {p}")
            except Exception as e:
                logger.warning(f"Failed to load external mapping {p}: {e}")

        if hasattr(self, "converter"):
            self.converter.migration_map = self.migration_map
        return loaded_count

    def _resolve_remote_database(
        self, candidate_ids: Optional[List[str]] = None, candidate_title: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """未登録のデータベース参照について、Confluence API 経由で既存DB（およびそのビュー一覧）を検索・解決して migration_map に動的登録する。"""
        with self._lock:
            candidate_ids = [cid for cid in (candidate_ids or []) if cid]
            for cid in candidate_ids:
                norm = cid.replace("-", "").lower()
                if cid in self.migration_map and self.migration_map[cid].get("node_type") == "database":
                    return self.migration_map[cid]
                if norm in self.migration_map and self.migration_map[norm].get("node_type") == "database":
                    return self.migration_map[norm]

            if candidate_title:
                clean_t = candidate_title.strip()
                norm_t = normalize_db_title(clean_t)
                for mid, minfo in self.migration_map.items():
                    if minfo.get("node_type") == "database":
                        m_title = minfo.get("title", "")
                        norm_m = normalize_db_title(m_title)
                        if m_title == clean_t or (norm_t and norm_m == norm_t):
                            return minfo

            # Confluence API による検索
            found_db = None
            if candidate_title:
                found_db = self.client.get_database_by_title(
                    self.space_key, candidate_title, title_prefix=getattr(self, "title_prefix", ""), use_cache=True
                )

            if not found_db and candidate_ids:
                for cid in candidate_ids:
                    known_title = getattr(self, "_node_to_title", {}).get(cid) or (
                        getattr(self.converter, "id_to_title_map", {}).get(cid) if hasattr(self, "converter") else None
                    )
                    if known_title:
                        found_db = self.client.get_database_by_title(
                            self.space_key, known_title, title_prefix=getattr(self, "title_prefix", ""), use_cache=True
                        )
                        if found_db:
                            break

            if not found_db:
                return None

            conf_id = str(found_db.get("id"))
            real_title = found_db.get("title") or candidate_title or "Remote Database"
            db_url = f"{self.client.base_url}/spaces/{self.space_key}/database/{conf_id}"

            views_list = self.client.get_database_views(conf_id)
            views_dict = {v["name"]: v["id"] for v in views_list if v.get("name") and v.get("id")}

            db_entry = {
                "confluence_id": conf_id,
                "title": real_title,
                "url": db_url,
                "node_type": "database",
                "views": views_dict,
                "views_info": views_list,
                "_is_remote_resolved": True,
            }

            for cid in candidate_ids:
                self.migration_map[cid] = db_entry
                norm = cid.replace("-", "").lower()
                self.migration_map[norm] = db_entry
                self._node_to_title[cid] = real_title
                self._node_to_title[norm] = real_title
                if hasattr(self, "converter"):
                    if not hasattr(self.converter, "id_to_title_map") or self.converter.id_to_title_map is None:
                        self.converter.id_to_title_map = {}
                    self.converter.id_to_title_map[cid] = real_title
                    self.converter.id_to_title_map[norm] = real_title

            self._reserved_titles.add(real_title)
            if hasattr(self, "converter"):
                self.converter.migration_map = self.migration_map

            logger.info(f"Dynamically resolved remote database [{real_title}] (ID: {conf_id}, views: {list(views_dict.keys())})")
            return db_entry

    def _resolve_remote_page(
        self, candidate_ids: Optional[List[str]] = None, candidate_title: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """未登録のページ参照について、Confluence API 経由で既存ページを検索・解決して migration_map に動的登録する。"""
        with self._lock:
            candidate_ids = [cid for cid in (candidate_ids or []) if cid]
            for cid in candidate_ids:
                norm = cid.replace("-", "").lower()
                if cid in self.migration_map and self.migration_map[cid].get("node_type") == "page":
                    return self.migration_map[cid]
                if norm in self.migration_map and self.migration_map[norm].get("node_type") == "page":
                    return self.migration_map[norm]

            if candidate_title:
                clean_t = candidate_title.strip()
                norm_t = normalize_db_title(clean_t)
                for mid, minfo in self.migration_map.items():
                    if minfo.get("node_type") == "page":
                        m_title = minfo.get("title", "")
                        norm_m = normalize_db_title(m_title)
                        if m_title == clean_t or (norm_t and norm_m == norm_t):
                            return minfo

            found_p = None
            if candidate_title:
                found_p = self.client.get_page_by_title(
                    self.space_key, candidate_title, title_prefix=getattr(self, "title_prefix", ""), cql_fallback=True, use_cache=True
                )

            if not found_p and candidate_ids:
                for cid in candidate_ids:
                    known_title = getattr(self, "_node_to_title", {}).get(cid) or (
                        getattr(self.converter, "id_to_title_map", {}).get(cid) if hasattr(self, "converter") else None
                    )
                    if known_title:
                        found_p = self.client.get_page_by_title(
                            self.space_key, known_title, title_prefix=getattr(self, "title_prefix", ""), cql_fallback=True, use_cache=True
                        )
                        if found_p:
                            break

            if not found_p:
                return None

            conf_id = str(found_p.get("id"))
            real_title = found_p.get("title") or candidate_title or ""
            p_url = f"{self.client.base_url}/spaces/{self.space_key}/pages/{conf_id}"

            page_entry = {
                "confluence_id": conf_id,
                "title": real_title,
                "url": p_url,
                "node_type": "page",
                "_is_remote_resolved": True,
            }

            for cid in candidate_ids:
                self.migration_map[cid] = page_entry
                norm = cid.replace("-", "").lower()
                self.migration_map[norm] = page_entry
                self._node_to_title[cid] = real_title
                self._node_to_title[norm] = real_title
                if hasattr(self, "converter"):
                    if not hasattr(self.converter, "id_to_title_map") or self.converter.id_to_title_map is None:
                        self.converter.id_to_title_map = {}
                    self.converter.id_to_title_map[cid] = real_title
                    self.converter.id_to_title_map[norm] = real_title

            self._reserved_titles.add(real_title)
            if hasattr(self, "converter"):
                self.converter.migration_map = self.migration_map

            logger.info(f"Dynamically resolved remote page [{real_title}] (ID: {conf_id})")
            return page_entry

    def _resolve_external_and_remote_references(self, root_node: PageNode):
        """ツリー内の未解決 Linked Database View や外部参照について、Confluence上の既存リソースを検索して動的解決する。"""
        from src.converter import extract_uuids

        # 現在移行中のローカルツリー内に存在するDB・ページはリモート検索対象から除外する
        # (Pass 1 前に過去の別プレフィックス移行DBで _node_to_title / migration_map が上書きされるのを防止)
        local_db_ids: Set[str] = set()
        local_db_norm_titles: Set[str] = set()
        local_node_ids: Set[str] = set()
        local_node_norm_titles: Set[str] = set()
        prefix_str = getattr(self, "title_prefix", "") or ""

        def _collect_local_tree(n: PageNode):
            if not n:
                return
            if n.id:
                norm_nid = str(n.id).replace("-", "").lower()
                local_node_ids.add(n.id)
                local_node_ids.add(norm_nid)
                if n.node_type == "database":
                    local_db_ids.add(n.id)
                    local_db_ids.add(norm_nid)
            if n.title:
                raw_t = n.title.strip()
                unpref_t = raw_t[len(prefix_str):].strip() if (prefix_str and raw_t.startswith(prefix_str)) else raw_t
                for t_cand in (raw_t, unpref_t):
                    nt = normalize_db_title(t_cand)
                    if nt:
                        local_node_norm_titles.add(nt)
                        if n.node_type == "database":
                            local_db_norm_titles.add(nt)
            for c in n.children or []:
                _collect_local_tree(c)

        _collect_local_tree(root_node)

        def _is_local_db(c_ids: List[str], title_cand: Optional[str]) -> bool:
            for cid in c_ids or []:
                if cid and (cid in local_db_ids or str(cid).replace("-", "").lower() in local_db_ids):
                    return True
            if title_cand:
                nt = normalize_db_title(title_cand)
                if nt and nt in local_db_norm_titles:
                    return True
            return False

        def _is_local_node(c_ids: List[str], title_cand: Optional[str]) -> bool:
            for cid in c_ids or []:
                if cid and (cid in local_node_ids or str(cid).replace("-", "").lower() in local_node_ids):
                    return True
            if title_cand:
                nt = normalize_db_title(title_cand)
                if nt and nt in local_node_norm_titles:
                    return True
            return False

        def _scan_blocks(blocks: List[Dict[str, Any]]):
            for b in blocks or []:
                b_type = b.get("type", "")
                if b_type == "child_database":
                    c_ids = []
                    if b.get("database_id"):
                        c_ids.append(b["database_id"])
                    if b.get("id"):
                        c_ids.append(b["id"])
                    src_name = b.get("database_title") or (b.get("child_database") or {}).get("title", "")
                    for ds in b.get("data_sources", []):
                        if isinstance(ds, dict):
                            if ds.get("id"):
                                c_ids.append(ds["id"])
                            if not src_name and ds.get("name"):
                                src_name = ds["name"]

                    if _is_local_db(c_ids, src_name):
                        b.pop("_is_unresolved_external_db", None)
                        continue
                    resolved = self._resolve_remote_database(candidate_ids=c_ids, candidate_title=src_name)
                    if resolved:
                        b.pop("_is_unresolved_external_db", None)

                elif b_type == "child_page":
                    c_title = (b.get("child_page") or {}).get("title")
                    c_id = b.get("id")
                    if b.get("_is_unresolved_external_page"):
                        if _is_local_node([c_id] if c_id else [], c_title):
                            b.pop("_is_unresolved_external_page", None)
                            continue
                        resolved_p = self._resolve_remote_page(candidate_ids=[c_id] if c_id else [], candidate_title=c_title)
                        if resolved_p:
                            b.pop("_is_unresolved_external_page", None)

                elif b_type == "link_to_page":
                    lp = b.get("link_to_page", {}) or {}
                    t_id = lp.get("database_id") or lp.get("page_id")
                    t_title = lp.get("title") or b.get("link_title") or b.get("title")
                    if t_id:
                        if _is_local_db([t_id], t_title) or _is_local_node([t_id], t_title):
                            b.pop("_is_unresolved_external_db", None)
                            b.pop("_is_unresolved_external_page", None)
                            continue
                        resolved_db = self._resolve_remote_database(candidate_ids=[t_id], candidate_title=t_title)
                        if resolved_db:
                            b.pop("_is_unresolved_external_db", None)
                        else:
                            resolved_p = self._resolve_remote_page(candidate_ids=[t_id], candidate_title=t_title)
                            if resolved_p:
                                b.pop("_is_unresolved_external_page", None)

                # rich_text 内のリンク・参照の動的解決
                data = b.get(b_type, {})
                if isinstance(data, dict):
                    rts = data.get("rich_text", [])
                    if isinstance(rts, list):
                        for rt in rts:
                            if not isinstance(rt, dict):
                                continue
                            if rt.get("_is_unresolved_external_page"):
                                p_id = rt.get("page_id")
                                plain = str(rt.get("plain_text") or (rt.get("text") or {}).get("content", "") or "").strip()
                                link_obj = (rt.get("text") or {}).get("link") if isinstance(rt.get("text"), dict) else None
                                href = (link_obj.get("url") if isinstance(link_obj, dict) else None) or rt.get("href") or ""
                                uuids = extract_uuids(href) if href else []
                                candidate_ids = ([p_id] if p_id else []) + uuids
                                if _is_local_node(candidate_ids, plain):
                                    rt.pop("_is_unresolved_external_page", None)
                                    continue
                                resolved_p = self._resolve_remote_page(candidate_ids=candidate_ids, candidate_title=plain)
                                if resolved_p:
                                    rt.pop("_is_unresolved_external_page", None)

                if b.get("children_blocks"):
                    _scan_blocks(b["children_blocks"])

        def _traverse(n: PageNode):
            if n.blocks:
                # 2パス走査: rich_text等のタイトルから解決された後、IDのみのlink_to_pageも確実に解消
                _scan_blocks(n.blocks)
                _scan_blocks(n.blocks)
            for c in n.children:
                _traverse(c)

        _traverse(root_node)

    @staticmethod
    def _extract_db_field_values(db_node: PageNode, field_name: str) -> List[str]:
        """データベースノードの子行から指定フィールドのユニーク値一覧をソート済みで抽出する。"""
        field_vals: Set[str] = set()
        for child in getattr(db_node, "children", []) or []:
            p_val = getattr(child, "properties", {}).get(field_name)
            if isinstance(p_val, dict):
                val = p_val.get("value")
                if isinstance(val, list):
                    for item in val:
                        lbl = (item.get("label") or item.get("name")) if isinstance(item, dict) else str(item)
                        if lbl:
                            field_vals.add(str(lbl).strip())
                elif val is not None and str(val).strip():
                    field_vals.add(str(val).strip())
            elif p_val is not None and str(p_val).strip():
                field_vals.add(str(p_val).strip())
        return sorted(v for v in field_vals if v)

    @staticmethod
    def _analyze_database_view_requirements(root_node: PageNode) -> Dict[str, Dict[str, Any]]:
        """ツリー全体を事前スキャンし、各データベースに対して他ページから参照されているビュー要件を収集する。"""
        db_by_norm_id: Dict[str, str] = {}
        db_by_title: Dict[str, str] = {}

        def _collect_dbs(n: PageNode):
            if n.node_type == "database":
                norm_id = n.id.replace("-", "").lower()
                db_by_norm_id[norm_id] = norm_id
                if n.title:
                    s_title = str(n.title).strip()
                    db_by_title[s_title] = norm_id
                    norm_t = normalize_db_title(s_title)
                    if norm_t and norm_t not in db_by_title:
                        db_by_title[norm_t] = norm_id
            for c in n.children:
                _collect_dbs(c)

        _collect_dbs(root_node)
        if not db_by_norm_id:
            return {}

        reqs: Dict[str, Dict[str, Any]] = {}

        def _ensure_req(db_norm_id: str) -> Dict[str, Any]:
            if db_norm_id not in reqs:
                reqs[db_norm_id] = {
                    "needs_board": False,
                    "board_fields": set(),
                    "needs_gallery": False,
                    "group_by_fields": set(),
                    "requested_contexts": set(),
                }
            return reqs[db_norm_id]

        def _resolve_db_norm_id(candidate_ids: List[str], candidate_title: str = "") -> Optional[str]:
            for cid in candidate_ids:
                if cid:
                    nid = str(cid).replace("-", "").lower()
                    if nid in db_by_norm_id:
                        return nid
            if candidate_title:
                if candidate_title in db_by_title:
                    return db_by_title[candidate_title]
                norm_c = normalize_db_title(candidate_title)
                if norm_c and norm_c in db_by_title:
                    return db_by_title[norm_c]
                for m_title, nid in db_by_title.items():
                    norm_m = normalize_db_title(m_title)
                    if norm_m and (norm_m.endswith(norm_c) or norm_c.endswith(norm_m)):
                        return nid
            return None

        def _extract_plain(rt_list: List[Dict[str, Any]]) -> str:
            return "".join(t.get("plain_text", "") or t.get("text", {}).get("content", "") for t in (rt_list or [])).strip()

        def _scan_blocks(blocks: List[Dict[str, Any]], page_title: str, inherited_heading: str = ""):
            current_heading = inherited_heading
            for b in blocks or []:
                b_type = b.get("type", "")
                if b_type in ("heading_1", "heading_2", "heading_3"):
                    h_txt = _extract_plain(b.get(b_type, {}).get("rich_text", []))
                    if h_txt:
                        current_heading = h_txt
                elif b_type == "toggle":
                    t_txt = _extract_plain(b.get("toggle", {}).get("rich_text", []))
                    if b.get("children_blocks"):
                        _scan_blocks(b["children_blocks"], page_title, inherited_heading=(t_txt or current_heading))
                    continue

                if b_type == "child_database" and b.get("is_linked_database"):
                    c_ids = []
                    if b.get("database_id"):
                        c_ids.append(b["database_id"])
                    if b.get("id"):
                        c_ids.append(b["id"])
                    data_sources = b.get("data_sources", [])
                    src_name = ""
                    for ds in data_sources:
                        if isinstance(ds, dict):
                            if ds.get("id"):
                                c_ids.append(ds["id"])
                            if not src_name and ds.get("name"):
                                src_name = ds["name"]
                    target_db = _resolve_db_norm_id(c_ids, src_name)
                    if target_db:
                        r = _ensure_req(target_db)
                        if page_title:
                            r["requested_contexts"].add(page_title)
                        if current_heading:
                            r["requested_contexts"].add(current_heading)
                        for v in b.get("views", []) or []:
                            v_name = v.get("name", "") or ""
                            v_type = (v.get("type") or "").lower()
                            v_cfg = v.get("configuration", {}) or {}
                            gb = v_cfg.get("group_by")
                            gb_prop = gb.get("property_name") if isinstance(gb, dict) else None
                            if v_name:
                                r["requested_contexts"].add(v_name)
                            if v_type == "board" or "ボード" in v_name or "board" in v_name.lower() or "カンバン" in v_name:
                                r["needs_board"] = True
                                if gb_prop:
                                    r["board_fields"].add(gb_prop)
                            elif v_type == "gallery" or "ギャラリー" in v_name or "gallery" in v_name.lower():
                                r["needs_gallery"] = True
                            if gb_prop and v_type != "board":
                                r["group_by_fields"].add(gb_prop)

                elif b_type == "link_to_page":
                    lp = b.get("link_to_page", {}) or {}
                    target_id = lp.get("page_id") or lp.get("database_id")
                    target_db = _resolve_db_norm_id([target_id] if target_id else [])
                    if target_db:
                        r = _ensure_req(target_db)
                        if page_title:
                            r["requested_contexts"].add(page_title)
                        if current_heading:
                            r["requested_contexts"].add(current_heading)
                        ctx_lower = f"{current_heading} {page_title}".lower()
                        if any(kw in ctx_lower for kw in ("ボード", "カンバン", "board")):
                            r["needs_board"] = True
                        if any(kw in ctx_lower for kw in ("ギャラリー", "カード", "gallery")):
                            r["needs_gallery"] = True

                if b.get("children_blocks"):
                    _scan_blocks(b["children_blocks"], page_title, inherited_heading=current_heading)

        def _traverse_pages(n: PageNode):
            if n.blocks:
                _scan_blocks(n.blocks, n.title or "")
            for c in n.children:
                _traverse_pages(c)

        _traverse_pages(root_node)
        return reqs

    def import_tree(
        self,
        root_node: PageNode,
        parent_id: Optional[str] = None,
        overwrite: bool = True,
    ) -> Dict[str, Dict[str, str]]:
        """PageNodeのツリーをConfluenceへ階層構造を維持して再帰的にインポートする。"""
        root_node._is_root = True
        # 全ノードのNotion ID -> Title マップを構築（リレーションのスマートリンク解決用）
        id_title_map = {}
        def collect_titles(n: PageNode):
            id_title_map[n.id] = n.title
            for c in n.children:
                collect_titles(c)
        collect_titles(root_node)

        # 外部マッピングや事前解決済みのタイトルをマージ
        for mid, mtitle in getattr(self, "_node_to_title", {}).items():
            if mid not in id_title_map:
                id_title_map[mid] = mtitle

        self.converter.id_to_title_map = id_title_map
        self.converter.migration_map = self.migration_map

        # 未解決の外部参照・クロスZIP参照についてConfluenceリモートリソースを動的解決
        self._resolve_external_and_remote_references(root_node)

        # ツリー全体を事前スキャンし、他ページから参照されているDBビュー要件（ボード/ギャラリー/カテゴリ等）を収集
        self._db_view_requirements = self._analyze_database_view_requirements(root_node)

        # リモート解決されたDBに対して必要なビュー（board / gallery等）が存在しなければ自動作成
        for db_norm_id, req in (self._db_view_requirements or {}).items():
            for mid, minfo in list(self.migration_map.items()):
                if minfo.get("node_type") == "database" and str(mid).replace("-", "").lower() == db_norm_id:
                    views = minfo.get("views") or {}
                    db_conf_id = minfo.get("confluence_id")
                    if req.get("needs_board") and not any("ボード" in vn or "board" in vn.lower() for vn in views):
                        try:
                            created_v = self.client.create_database_view(db_conf_id, "ボード", layout="board")
                            if created_v and created_v.get("id"):
                                minfo.setdefault("views", {})["ボード"] = created_v["id"]
                                logger.info(f"Created missing board view for remote database [{minfo.get('title')}]")
                        except Exception as e:
                            logger.debug(f"Could not auto-create board view for DB {db_conf_id}: {e}")
                    if req.get("needs_gallery") and not any("ギャラリー" in vn or "gallery" in vn.lower() for vn in views):
                        try:
                            created_v = self.client.create_database_view(db_conf_id, "ギャラリー", layout="gallery")
                            if created_v and created_v.get("id"):
                                minfo.setdefault("views", {})["ギャラリー"] = created_v["id"]
                                logger.info(f"Created missing gallery view for remote database [{minfo.get('title')}]")
                        except Exception as e:
                            logger.debug(f"Could not auto-create gallery view for DB {db_conf_id}: {e}")

        # ダッシュボードの開始 (Task B.10)
        # 実際に作成されるノード数（skip_child_pages / 1ページ集約 / 個別上書き / 空DB行を反映）を母数とする
        total_nodes = count_migratable_nodes(
            root_node,
            individual_db_child_threshold=self.individual_db_child_threshold,
            db_mode_overrides=self.db_mode_overrides,
        )
        if self.dashboard:
            self.dashboard.start(total_nodes)
            self.dashboard.set_main_task(f"Pass 1: 事前マッピング・タイトル初期化中...")

        try:
            if self.dashboard:
                self.dashboard.set_main_task(f"Pass 1: ルートノード移行中 [{root_node.title}]")
            self._import_node_recursive(root_node, parent_id=parent_id, overwrite=overwrite, allow_sub_parallel=True)

            # Pass 2: 全ページのリンク解決と更新 (データベースビュー・相互リンクの完全同期)
            logger.info("Executing Pass 2: Resolving embedded database views, pending row views and cross-page links...")
            if self.dashboard:
                self.dashboard.set_main_task("Pass 1 完了: 未解決DB行ビューの非同期解決中...")
            # 1. 未解決のデータベース行ビューを非同期解決 (Approach B / Resume)
            self.resolve_pending_database_row_views(root_node)

            # Pass 2 直前にも未解決参照を再度動的解決
            self._resolve_external_and_remote_references(root_node)

            # 2. データベースの最新ビュー一覧を再取得して確実に migration_map を補完
            db_items = [(nid, ninfo) for nid, ninfo in self.migration_map.items() if ninfo.get("node_type") == "database"]
            for i, (nid, ninfo) in enumerate(db_items, 1):
                db_conf_id = ninfo["confluence_id"]
                if self.dashboard:
                    self.dashboard.set_main_task(f"Pass 2: データベース最新ビュー再取得中 ({i}/{len(db_items)}: [{ninfo['title']}])")
                views = self.client.get_database_views(db_conf_id)
                ninfo["views"] = {v["name"]: v["id"] for v in views}
                logger.info(f"Refreshed views for database [{ninfo['title']}]: {list(ninfo['views'].keys())}")

            self.converter.migration_map = self.migration_map

            if self.dashboard:
                self.dashboard.set_main_task("Pass 2: 相互リンク・バックリンク構造を解析中...")
            # Task B.9: 受信リンク（バックリンク）関係を解析・構築
            self.incoming_links_map = self._build_incoming_links_map(root_node)
            logger.info(f"Built incoming links map for {len(self.incoming_links_map)} target pages.")

            self._resolve_and_update_page_links(root_node)

            # ページ内アンカー欠落リンクの検知レポートを出力
            self.save_inpage_repair_candidates()

            if self.dashboard:
                self.dashboard.set_main_task("全ノードの移行完了")
                self.dashboard.finish()

            return self.migration_map
        except Exception:
            if self.dashboard and getattr(self.dashboard, "live", None):
                try:
                    self.dashboard.live.stop()
                except Exception:
                    pass
            raise

    def save_inpage_repair_candidates(self, output_path: str = "reports/inpage_link_repair_candidates.json") -> Optional[str]:
        """移行中に検出されたアンカー欠落の可能性のある自ページリンク一覧をJSONレポートとして出力する。"""
        candidates = getattr(self.converter, "inpage_repair_candidates", [])
        if not candidates:
            return None

        with self._lock:
            for c in candidates:
                p_id = c.get("page_id")
                if p_id and p_id in self.migration_map:
                    c["confluence_page_id"] = self.migration_map[p_id].get("confluence_id")
                    if not c.get("page_title"):
                        c["page_title"] = self.migration_map[p_id].get("title", "")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(candidates, f, ensure_ascii=False, indent=2)
            logger.info(f"📋 アンカー欠落の可能性のある自ページリンク修復候補リスト ({len(candidates)}件) を出力しました: {output_path}")
            print(f"\n💡 [In-Page Links] アンカー欠落の可能性のある自ページリンクを {len(candidates)} 件検出しました。")
            print(f"   修復候補リスト: {output_path}")
            print(f"   Notion API による自動修復を実行する場合: python migrate.py --repair-inpage-links --notion-token <YOUR_NOTION_TOKEN>\n")
            return output_path
        except Exception as e:
            logger.warning(f"修復候補リスト保存失敗 ({output_path}): {e}")
            return None

    def _extract_referenced_node_ids(self, blocks: List[Dict[str, Any]]) -> Set[str]:
        """ブロックリストから参照されている他のNotionノードID（UUID正規化形式）を抽出する。"""
        referenced_ids = set()
        uuid_pattern = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")

        def _scan_rich_text(rts: List[Dict[str, Any]]):
            for t in rts:
                if not isinstance(t, dict):
                    continue
                text_obj = t.get("text") or {}
                link_obj = text_obj.get("link") or {}
                href = t.get("href") or link_obj.get("url") or ""
                if href:
                    matches = uuid_pattern.findall(href)
                    for m in matches:
                        referenced_ids.add(m.replace("-", "").lower())
                mention = t.get("mention") or {}
                m_type = mention.get("type")
                if m_type == "page":
                    p_id = (mention.get("page") or {}).get("id")
                    if p_id:
                        referenced_ids.add(p_id.replace("-", "").lower())
                elif m_type == "database":
                    d_id = (mention.get("database") or {}).get("id")
                    if d_id:
                        referenced_ids.add(d_id.replace("-", "").lower())

        for b in blocks:
            b_type = b.get("type", "")
            data = b.get(b_type, {})
            if isinstance(data, dict):
                rts = data.get("rich_text", [])
                if isinstance(rts, list):
                    _scan_rich_text(rts)

            if b_type == "link_to_page":
                link_data = b.get("link_to_page", {})
                t_id = link_data.get("page_id") or link_data.get("database_id")
                if t_id:
                    referenced_ids.add(t_id.replace("-", "").lower())

            if b.get("children_blocks"):
                referenced_ids.update(self._extract_referenced_node_ids(b["children_blocks"]))

        return referenced_ids

    def _build_incoming_links_map(self, root_node: PageNode) -> Dict[str, List[Dict[str, Any]]]:
        """全ノードを走査し、各ターゲットノードに対する受信リンク（リンク元一覧）を構築する (Task B.9)。"""
        incoming_map: Dict[str, List[Dict[str, Any]]] = {}

        norm_to_node_id: Dict[str, str] = {}
        def _index_node_ids(n: PageNode):
            norm = n.id.replace("-", "").lower()
            norm_to_node_id[norm] = n.id
            norm_to_node_id[n.id] = n.id
            for c in n.children:
                _index_node_ids(c)
        _index_node_ids(root_node)

        def _traverse(n: PageNode):
            if n.blocks:
                ref_norms = self._extract_referenced_node_ids(n.blocks)
                source_info = self.migration_map.get(n.id) or {}
                source_entry = {
                    "id": n.id,
                    "title": n.title,
                    "url": source_info.get("url"),
                }
                for ref in ref_norms:
                    target_id = norm_to_node_id.get(ref)
                    if target_id and target_id != n.id:
                        if target_id not in incoming_map:
                            incoming_map[target_id] = []
                        if not any(item["id"] == n.id for item in incoming_map[target_id]):
                            incoming_map[target_id].append(source_entry)

            for c in n.children:
                _traverse(c)

        _traverse(root_node)
        return incoming_map

    def _resolve_and_update_single_node(self, node: PageNode):
        """パス2: 単一ノードの相互リンクおよび埋め込みDBビューを解決して更新する。"""
        if self.cancel_manager:
            self.cancel_manager.check_cancelled()

        if self.checkpoint and self.checkpoint.is_pass2_done(node.id):
            logger.debug(f"[RESUME] Skipping Pass 2 update for [{node.title}] (already completed)")
            if self.dashboard:
                self.dashboard.record_pass2_skip(node.title)
            if self.prune_blocks_on_pass2:
                node.blocks = []
                node.properties = {}
            return

        if node.node_type in ("page", "database_row") and (node.blocks or node.properties):
            with self._lock:
                page_info = self.migration_map.get(node.id)
            if page_info:
                conf_id = page_info["confluence_id"]
                try:
                    if self.dashboard:
                        self.dashboard.set_worker_busy(node.title, node_type=node.node_type)
                    incoming = getattr(self, "incoming_links_map", {}).get(node.id, [])
                    db_url = getattr(node, "parent_db_url", None)
                    entry_id = getattr(node, "entry_id", None)
                    new_body = self.converter.generate_page_content(
                        node, db_url=db_url, entry_id=entry_id, incoming_links=incoming
                    )
                    if self.telemetry:
                        for m in re.findall(r'<ac:structured-macro ac:name="([^"]+)"', new_body):
                            self.telemetry.record_macro(m)

                    latest = self.client.get_page_by_id(conf_id, expand="version")
                    if latest:
                        current_ver = latest.get("version", {}).get("number", 1)
                        title_to_update = page_info.get("title") or node.title
                        self.client.update_page(
                            page_id=conf_id,
                            space_key=self.space_key,
                            title=title_to_update,
                            body=new_body,
                            current_version=current_ver,
                        )
                        # アイコンが未設定またはPass 1で失敗していた場合の確実な再設定
                        if node.icon:
                            self.client.set_page_emoji(conf_id, node.icon)
                        logger.info(f"Pass 2 updated page [{node.title}] (ID: {conf_id}, v{current_ver + 1}) with resolved views & links")
                        if self.checkpoint:
                            self.checkpoint.record_pass2(node.id)
                        if self.dashboard:
                            self.dashboard.record_pass2_done(node.title)
                except CheckpointWriteError as ce:
                    if self.cancel_manager:
                        self.cancel_manager.trigger_error_halt(f"Checkpoint write error during Pass 2 for [{node.title}]: {ce}")
                    raise
                except Exception as e:
                    logger.warning(f"Pass 2 update error for page {node.title} ({conf_id}): {e}")
                    if self.telemetry:
                        self.telemetry.record_warning(f"Pass 2 update error for page {node.title}: {e}")
                    if self.dashboard:
                        self.dashboard.record_warning(f"Pass 2 update error: {node.title}")
                finally:
                    if self.dashboard:
                        self.dashboard.set_worker_idle()
        else:
            if self.checkpoint:
                try:
                    self.checkpoint.record_pass2(node.id)
                except CheckpointWriteError as ce:
                    if self.cancel_manager:
                        self.cancel_manager.trigger_error_halt(f"Checkpoint write error during Pass 2 for [{node.title}]: {ce}")
                    raise
            if self.dashboard:
                self.dashboard.record_pass2_skip(node.title)

        if self.prune_blocks_on_pass2:
            node.blocks = []
            node.properties = {}

    def _get_db_migration_mode(self, db_node: PageNode) -> str:
        """DBノードの移行方式 ('aggregated' / 'individual' / 'static_table' / 'skip') を返す。"""
        return resolve_db_migration_mode(
            db_node,
            individual_db_child_threshold=self.individual_db_child_threshold,
            mode_overrides=self.db_mode_overrides,
        )

    def _is_aggregated_db(self, db_node: PageNode) -> bool:
        """DBが1ページ集約方式（本文あり行あり、かつ aggregated または static_table）かを判定する。"""
        if analyze_database_child_page_stats(db_node)["skip_child_pages"]:
            return False
        return self._get_db_migration_mode(db_node) in ("aggregated", "static_table")

    def _update_aggregated_detail_page_pass2(self, db_node: PageNode):
        """Pass 2: 1ページ集約方式のDB詳細まとめページに解決済みリンクと?entryId=戻りリンクを反映する。"""
        with self._lock:
            db_info = self.migration_map.get(db_node.id)
        if not db_info:
            return
        agg_id = db_info.get("aggregated_detail_page_id") or getattr(db_node, "_aggregated_detail_page_id", None)
        if not agg_id:
            return
        agg_node_id = f"{db_node.id}__details"
        if self.checkpoint and self.checkpoint.is_pass2_done(agg_node_id):
            if self.dashboard:
                self.dashboard.record_pass2_skip(db_info.get("aggregated_detail_page_title") or db_node.title)
            return

        db_conf_id = str(db_info["confluence_id"])
        db_web_url = f"{self.client.base_url}/spaces/{self.space_key}/database/{db_conf_id}"
        body_children = [
            c for c in (db_node.children or [])
            if not self.is_empty_database_row(c) and database_row_has_substantive_body(c)
        ]
        if not body_children:
            return

        # entry_id が未解決の行があれば Pass 2 時点で再取得を試みる
        if any(not getattr(c, "entry_id", None) for c in body_children):
            try:
                entries = self.client.get_database_entries(db_conf_id, expected_count=len(db_node.children or []), timeout_sec=10)
                text_to_entry = self._build_text_key_to_entry_map(entries)
                for c in body_children:
                    if not getattr(c, "entry_id", None):
                        eid = text_to_entry.get((c.title or "").strip())
                        if eid:
                            c.entry_id = eid
            except Exception as e:
                logger.debug(f"Pass 2 entryId lookup skipped for aggregated DB [{db_node.title}]: {e}")

        row_entries = [(c, getattr(c, "entry_id", None)) for c in body_children]
        agg_title = db_info.get("aggregated_detail_page_title") or getattr(db_node, "_aggregated_detail_page_title", f"{db_node.title} - 詳細ページ一覧")
        try:
            if self.dashboard:
                self.dashboard.set_worker_busy(agg_title, node_type="page")
            agg_body = self.converter.generate_aggregated_database_details_html(db_node, db_web_url, row_entries)
            latest = self.client.get_page_by_id(str(agg_id), expand="version")
            if latest:
                current_ver = latest.get("version", {}).get("number", 1)
                self.client.update_page(
                    page_id=str(agg_id),
                    space_key=self.space_key,
                    title=agg_title,
                    body=agg_body,
                    current_version=current_ver,
                )
                logger.info(f"Pass 2 updated aggregated detail page [{agg_title}] (ID: {agg_id}, v{current_ver + 1})")
                if self.checkpoint:
                    self.checkpoint.record_pass2(agg_node_id)
                if self.dashboard:
                    self.dashboard.record_pass2_done(agg_title)
        except CheckpointWriteError as ce:
            if self.cancel_manager:
                self.cancel_manager.trigger_error_halt(f"Checkpoint write error during Pass 2 for [{agg_title}]: {ce}")
            raise
        except Exception as e:
            logger.warning(f"Pass 2 update error for aggregated detail page {agg_title} ({agg_id}): {e}")
            if self.telemetry:
                self.telemetry.record_warning(f"Pass 2 update error for aggregated detail page {agg_title}: {e}")
        finally:
            if self.dashboard:
                self.dashboard.set_worker_idle()

    def _resolve_and_update_page_links(self, root_node: PageNode):
        """パス2: 全データベースおよびページの作成完了後、相互リンクおよび埋め込みDBビューを完全に解決して更新する（並行実行）。"""
        all_nodes: List[PageNode] = []
        aggregated_dbs: List[PageNode] = []
        def _collect(n: PageNode):
            if n.node_type == "database_row" and self.is_empty_database_row(n):
                return
            if n.node_type == "database" and self._get_db_migration_mode(n) == "skip":
                return
            all_nodes.append(n)
            if n.node_type == "database":
                if (
                    getattr(n, "_skipped_row_child_pages", False)
                    or analyze_database_child_page_stats(n)["skip_child_pages"]
                ):
                    return
                if getattr(n, "_aggregated_mode", False) or self._is_aggregated_db(n):
                    aggregated_dbs.append(n)
                    # 1ページ集約方式: 行個別のupdateは行わず、レコード配下のネスト子ページ/DBのみ収集
                    for c in n.children:
                        for sub in (getattr(c, "children", None) or []):
                            _collect(sub)
                    return
            for c in n.children:
                _collect(c)
        _collect(root_node)

        workers = min(self.page_workers, len(all_nodes))
        if workers > 1:
            logger.info(f"Pass 2: Concurrently resolving links for {len(all_nodes)} nodes with {workers} worker threads...")
            if self.dashboard:
                self.dashboard.set_main_task(f"Pass 2: リンク・ビュー並行更新中 (ワーカー待機中: {len(all_nodes)} 件)")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(self._resolve_and_update_single_node, n) for n in all_nodes]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        else:
            if self.dashboard:
                self.dashboard.set_main_task(f"Pass 2: リンク・ビュー更新中 ({len(all_nodes)} 件)")
            for n in all_nodes:
                self._resolve_and_update_single_node(n)

        for db_n in aggregated_dbs:
            self._update_aggregated_detail_page_pass2(db_n)

    @staticmethod
    def _build_text_key_to_entry_map(entries: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
        """Confluence DB エントリ一覧から text キー値(plain) -> entryId マップを構築する。"""
        text_to_entry: Dict[str, str] = {}
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            entry_id = e.get("id")
            if not entry_id:
                continue
            values = e.get("values") or {}
            if not isinstance(values, dict):
                continue
            for fval in values.values():
                if not isinstance(fval, dict) or fval.get("type") != "text":
                    continue
                plain = fval.get("plain")
                if not isinstance(plain, str):
                    plain = fval.get("value") if isinstance(fval.get("value"), str) else ""
                key = plain.strip()
                if key and key not in text_to_entry:
                    text_to_entry[key] = str(entry_id)
        return text_to_entry

    @staticmethod
    def _build_page_to_entry_map(entries: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
        """Confluence DB エントリ一覧から pageLink の contentId -> entryId マップを構築する。"""
        page_to_entry_map: Dict[str, str] = {}
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            entry_id = e.get("id")
            if not entry_id:
                continue
            values = e.get("values") or {}
            if not isinstance(values, dict):
                continue
            for fval in values.values():
                if not isinstance(fval, dict) or fval.get("type") != "pageLink":
                    continue
                items = fval.get("value") or []
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    cid = item.get("contentId")
                    if cid:
                        page_to_entry_map[str(cid)] = entry_id
        return page_to_entry_map

    def _embed_row_views_concurrently(
        self,
        indexed_items: List[Tuple[PageNode, str]],
        db_web_url: str,
        confluence_page_id: str,
    ):
        """インデックスが完了した子ページに対して行ビューを並行または順次埋め込み、親IDを更新する。"""
        def _update_single_child(child_node: PageNode, entry_id: str):
            if self.cancel_manager:
                self.cancel_manager.check_cancelled()
            if self.dashboard:
                self.dashboard.set_worker_busy(child_node.title, node_type="row_view")
            try:
                with self._lock:
                    c_info = self.migration_map.get(child_node.id)
                if not c_info:
                    return
                c_id = c_info["confluence_id"]
                c_latest = self.client.get_page_by_id(c_id, expand="version")
                c_ver = c_latest.get("version", {}).get("number", 1) if c_latest else 1
                c_title = c_info.get("title") or child_node.title
                child_node.parent_db_url = db_web_url
                child_node.entry_id = entry_id
                child_body = self.converter.generate_page_content(
                    child_node,
                    db_url=db_web_url,
                    entry_id=entry_id,
                )
                self.client.update_page(
                    page_id=c_id,
                    space_key=self.space_key,
                    title=c_title,
                    body=child_body,
                    current_version=c_ver,
                    parent_id=confluence_page_id,
                )
                if child_node.icon:
                    self.client.set_page_emoji(c_id, child_node.icon)
                logger.info(f"Embedded native database row view into child page [{c_title}] (entry: {entry_id})")
            except Exception as e:
                logger.warning(f"Could not reparent and embed row view for child page {c_id}: {e}")
                if self.telemetry:
                    self.telemetry.record_warning(f"Could not embed row view for {child_node.title} ({c_id}): {e}")
            finally:
                if self.dashboard:
                    self.dashboard.set_worker_idle()

        workers = min(self.page_workers, len(indexed_items))
        if workers > 1:
            logger.info(f"Embedding row views concurrently for {len(indexed_items)} pages using {workers} worker threads...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_update_single_child, c, eid) for c, eid in indexed_items]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.warning(f"Worker error in embedding row view: {e}")
        else:
            for c, eid in indexed_items:
                _update_single_child(c, eid)

    def _reparent_pending_children_concurrently(
        self,
        pending_children: List[PageNode],
        db_web_url: str,
        confluence_page_id: str,
    ):
        """まだConfluence DBのインデックス待ちである子ページの親IDをDB IDに変更し階層不一致を防ぐ。"""
        def _reparent_single(child_node: PageNode):
            if self.cancel_manager:
                self.cancel_manager.check_cancelled()
            try:
                with self._lock:
                    c_info = self.migration_map.get(child_node.id)
                if not c_info:
                    return
                c_id = c_info["confluence_id"]
                c_latest = self.client.get_page_by_id(c_id, expand="version")
                c_ver = c_latest.get("version", {}).get("number", 1) if c_latest else 1
                c_title = c_info.get("title") or child_node.title
                child_node.parent_db_url = db_web_url
                child_body = self.converter.generate_page_content(
                    child_node,
                    db_url=db_web_url,
                    entry_id=None,
                )
                self.client.update_page(
                    page_id=c_id,
                    space_key=self.space_key,
                    title=c_title,
                    body=child_body,
                    current_version=c_ver,
                    parent_id=confluence_page_id,
                )
                if child_node.icon:
                    self.client.set_page_emoji(c_id, child_node.icon)
                logger.info(f"Reparented pending child page [{c_title}] to parent DB [{confluence_page_id}]")
            except Exception as e:
                logger.warning(f"Could not reparent pending child page {child_node.title}: {e}")

        workers = min(self.page_workers, len(pending_children))
        if workers > 1:
            logger.info(f"Reparenting {len(pending_children)} pending child pages to DB [{confluence_page_id}]...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_reparent_single, c) for c in pending_children]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.warning(f"Worker error in reparenting pending child: {e}")
        else:
            for c in pending_children:
                _reparent_single(c)

    def resolve_pending_database_row_views(self, root_node: PageNode):
        """Pass 2 または --resume 時に、インデックス待ちだったデータベースの子ページ行ビューを並行して非同期解決する。"""
        db_nodes: List[PageNode] = []
        def _collect_dbs(n: PageNode):
            if n.node_type == "database":
                db_nodes.append(n)
            for c in n.children:
                _collect_dbs(c)
        _collect_dbs(root_node)

        if not db_nodes:
            return

        def _resolve_single_db(node: PageNode):
            with self._lock:
                db_info = self.migration_map.get(node.id)
            pending_ids = set()
            if db_info and "pending_row_views" in db_info:
                pending_ids = set(db_info["pending_row_views"])
            elif self.checkpoint:
                pending_ids = set(self.checkpoint.get_pending_row_views(node.id))

            if not pending_ids:
                return

            if self.dashboard:
                self.dashboard.set_main_task(f"Pass 2: DB同期中 [{node.title}] (残り {len(pending_ids)} 件)")
            try:
                db_conf_id = db_info["confluence_id"] if db_info else self.checkpoint.get_node_info(node.id).get("confluence_id")
                expected = len(node.children)
                logger.info(f"[Resume] Checking indexed entries for database [{node.title}] (ID: {db_conf_id}, pending: {len(pending_ids)} pages)...")
                # In resume / Pass 2, allow up to 25 seconds for Confluence background indexing
                entries = self.client.get_database_entries(db_conf_id, expected_count=expected, timeout_sec=25)
                page_to_entry_map = self._build_page_to_entry_map(entries)

                db_web_url = f"{self.client.base_url}/spaces/{self.space_key}/database/{db_conf_id}"
                resolved_items = []
                resolved_child_ids = []
                for child in node.children:
                    child.parent_db_title = node.title
                    child.parent_db_url = db_web_url
                    if child.id in pending_ids:
                        with self._lock:
                            c_info = self.migration_map.get(child.id) or (self.checkpoint.get_node_info(child.id) if self.checkpoint else None)
                        if c_info:
                            c_cid = str(c_info["confluence_id"])
                            if c_cid in page_to_entry_map:
                                resolved_items.append((child, page_to_entry_map[c_cid]))
                                resolved_child_ids.append(child.id)

                if resolved_items:
                    logger.info(f"[Resume] Resolving {len(resolved_items)} newly indexed row views for [{node.title}]...")
                    self._embed_row_views_concurrently(resolved_items, db_web_url, db_conf_id)
                    if self.checkpoint:
                        try:
                            self.checkpoint.mark_row_views_resolved(node.id, resolved_child_ids)
                        except CheckpointWriteError as ce:
                            if self.cancel_manager:
                                self.cancel_manager.trigger_error_halt(
                                    f"Checkpoint write error resolving row views for [{node.title}]: {ce}"
                                )
                            raise
                    if db_info:
                        db_info["pending_row_views"] = [cid for cid in db_info.get("pending_row_views", []) if cid not in resolved_child_ids]
                    remaining = len(pending_ids) - len(resolved_child_ids)
                    if remaining > 0:
                        logger.info(f"[Resume] Database [{node.title}]: {remaining} child pages are still indexing in background.")
                    else:
                        logger.info(f"[Resume] Database [{node.title}]: All {len(node.children)} row views successfully embedded!")
                else:
                    logger.info(f"[Resume] Database [{node.title}]: No new entries indexed yet ({len(pending_ids)} pages still pending).")
            finally:
                if self.dashboard:
                    self.dashboard.set_worker_idle()

        workers = min(self.page_workers, len(db_nodes))
        if workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_resolve_single_db, db) for db in db_nodes]
                for f in concurrent.futures.as_completed(futures):
                    f.result()
        else:
            for db in db_nodes:
                _resolve_single_db(db)

    @staticmethod
    def _extract_attachment_file_id(upload_res: Any) -> Optional[str]:
        """upload_attachment のレスポンス辞書から Atlassian Media Service の fileId (UUID) を抽出する。"""
        if not isinstance(upload_res, dict):
            return None
        ext = upload_res.get("extensions")
        if isinstance(ext, dict) and ext.get("fileId"):
            return str(ext["fileId"])
        results = upload_res.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            r_ext = results[0].get("extensions")
            if isinstance(r_ext, dict) and r_ext.get("fileId"):
                return str(r_ext["fileId"])
            if results[0].get("id"):
                return str(results[0]["id"])
        if upload_res.get("id"):
            return str(upload_res["id"])
        return None

    def _process_cover(self, node: PageNode, page_id: str):
        """ページのカバー画像をConfluenceへ添付アップロードし、ネイティブカバー画像プロパティ（cover-picture-id-published / draft）を設定する。"""
        if not getattr(node, "cover", None):
            return

        cover_data = node.cover if isinstance(node.cover, dict) else {}
        position = cover_data.get("position", 50)
        f_obj = cover_data.get("file") or {}
        e_obj = cover_data.get("external") or {}
        cover_url = f_obj.get("url") or e_obj.get("url")
        local_bytes = cover_data.get("_local_bytes")

        filename = (
            cover_data.get("_filename")
            or getattr(node, "_cover_filename", None)
            or f"cover_{node.id.replace('-', '')[:8]}.jpg"
        )
        node._cover_filename = filename

        if isinstance(local_bytes, (bytes, bytearray)):
            mime_type = detect_attachment_mime_type(filename)
            try:
                upload_res = self.client.upload_attachment(
                    page_id=page_id,
                    filename=filename,
                    file_bytes=bytes(local_bytes),
                    mime_type=mime_type,
                )
                file_id = self._extract_attachment_file_id(upload_res)
                if file_id:
                    self.client.set_page_cover(page_id, file_id, position=position)
                    node._cover_uploaded = True
                    node._native_cover_set = True
                    logger.info(f"Successfully set native cover image from ZIP: {filename} (fileId={file_id}, position={position})")
                else:
                    node._cover_uploaded = False
            except Exception as e:
                node._cover_uploaded = False
                logger.warning(f"Failed to upload/set local ZIP cover image {filename}: {e}")
            return

        if not cover_url:
            return

        if not _is_safe_external_url(cover_url):
            logger.warning(f"Skipped unsafe or non-HTTP cover URL for page {page_id}: {cover_url}")
            node._cover_uploaded = False
            return

        logger.info(f"Downloading cover image for page {page_id}: {filename}")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        try:
            resp = requests.get(cover_url, headers=headers, timeout=30)
            if resp.status_code == 200:
                content_type = resp.headers.get("Content-Type") or detect_attachment_mime_type(filename)
                upload_res = self.client.upload_attachment(
                    page_id=page_id,
                    filename=filename,
                    file_bytes=resp.content,
                    mime_type=content_type,
                )
                file_id = self._extract_attachment_file_id(upload_res) or cover_url
                self.client.set_page_cover(page_id, file_id, position=position)
                node._cover_uploaded = True
                node._native_cover_set = True
                logger.info(f"Successfully uploaded and set native cover image: {filename} ({len(resp.content)} bytes, fileId={file_id}, position={position})")
            else:
                node._cover_uploaded = False
                logger.warning(f"Failed to download cover image {filename}: HTTP {resp.status_code}")
        except Exception as e:
            node._cover_uploaded = False
            logger.warning(f"Failed to download/upload cover image {filename}: {e}")

    def _disambiguate_row_attachment_filenames(
        self,
        blocks: List[Dict[str, Any]],
        row_idx: int,
        used_filenames: Set[str],
    ):
        """1ページ集約ページ上で複数行が同名ファイル(例: Untitled.png)を持つ場合にファイル名を重複回避する。"""
        for b in (blocks or []):
            if not isinstance(b, dict):
                continue
            b_type = b.get("type")
            if b_type in ("image", "file", "pdf", "video", "audio") or b.get("_attachment_bytes") or b.get("_attachment_zip_path"):
                base_fn = unicodedata.normalize("NFC", self.converter._get_attachment_filename(b))
                if base_fn and base_fn in used_filenames:
                    candidate = f"r{row_idx}_{base_fn}"
                    suffix = 2
                    while candidate in used_filenames:
                        candidate = f"r{row_idx}_{suffix}_{base_fn}"
                        suffix += 1
                    b["_attachment_filename"] = candidate
            if b.get("children_blocks"):
                self._disambiguate_row_attachment_filenames(b["children_blocks"], row_idx, used_filenames)

    def _process_aggregated_attachments(self, body_children: List[PageNode], agg_page_id: str):
        """1ページ集約詳細ページ配下の全レコード本文に含まれる画像・添付ファイルを一括アップロードする。"""
        agg_uploaded_filenames: Set[str] = set()
        for idx, child in enumerate(body_children, start=1):
            if not getattr(child, "blocks", None):
                continue
            self._disambiguate_row_attachment_filenames(child.blocks, idx, agg_uploaded_filenames)
            self._process_attachments(child.blocks, agg_page_id, uploaded_filenames=agg_uploaded_filenames)

    def _process_attachments(self, blocks: List[Dict[str, Any]], page_id: str, uploaded_filenames: Optional[Set[str]] = None):
        """ブロックツリー内の画像・添付ファイル・メディアをダウンロードし、Confluenceにアップロードする。"""
        if uploaded_filenames is None:
            uploaded_filenames = set()

        for b in blocks:
            b_type = b.get("type")
            if (
                b.get("_is_unsupported_external_embed")
                or b.get("_attachment_missing_in_zip")
                or is_unsupported_page_element_block(b)
            ):
                b["_attachment_uploaded"] = False
                if b.get("children_blocks"):
                    self._process_attachments(b["children_blocks"], page_id, uploaded_filenames=uploaded_filenames)
                continue

            file_url = None
            is_embed = False

            if b_type == "image":
                img_data = b.get("image", {})
                f_obj = img_data.get("file") or {}
                e_obj = img_data.get("external") or {}
                file_url = f_obj.get("url") or e_obj.get("url")
            elif b_type in ("file", "pdf"):
                file_data = b.get(b_type, {})
                f_obj = file_data.get("file") or {}
                e_obj = file_data.get("external") or {}
                file_url = f_obj.get("url") or e_obj.get("url")
            elif b_type == "video":
                v_data = b.get("video", {})
                f_obj = v_data.get("file") or {}
                e_obj = v_data.get("external") or {}
                file_url = f_obj.get("url") or e_obj.get("url")
                if file_url and any(h in file_url.lower() for h in ("youtube.com", "youtu.be", "vimeo.com", "loom.com", "twitch.tv", "wistia.com")):
                    is_embed = True
            elif b_type == "audio":
                a_data = b.get("audio", {})
                f_obj = a_data.get("file") or {}
                e_obj = a_data.get("external") or {}
                file_url = f_obj.get("url") or e_obj.get("url")
                if file_url and any(h in file_url.lower() for h in ("spotify.com", "soundcloud.com", "apple.com")):
                    is_embed = True
            elif b_type == "equation":
                # 数式をローカルPC環境でPNG画像としてレンダリングしてアップロード（外部通信ゼロ）
                expr = b.get("equation", {}).get("expression", "")
                if expr:
                    block_id_short = b.get("id", "").replace("-", "")[:8]
                    filename = f"equation_{block_id_short}.png"
                    b["_attachment_filename"] = filename
                    if filename in uploaded_filenames:
                        b["_attachment_uploaded"] = True
                    else:
                        logger.info(f"Locally rendering LaTeX equation for page {page_id}: {filename}")
                        try:
                            png_bytes = render_latex_to_png(expr)
                            self.client.upload_attachment(
                                page_id=page_id,
                                filename=filename,
                                file_bytes=png_bytes,
                                mime_type="image/png",
                            )
                            uploaded_filenames.add(filename)
                            b["_attachment_uploaded"] = True
                            logger.info(f"Successfully uploaded locally-rendered equation image: {filename}")
                        except Exception as e:
                            b["_attachment_uploaded"] = False
                            logger.warning(f"Failed to locally render/upload equation {filename}: {e}")

            if b.get("_attachment_bytes"):
                filename = unicodedata.normalize("NFC", self.converter._get_attachment_filename(b))
                b["_attachment_filename"] = filename
                if filename in uploaded_filenames:
                    b["_attachment_uploaded"] = True
                    b.pop("_attachment_bytes", None)
                    b.pop("_attachment_zip_entry", None)
                    b.pop("_attachment_zip_path", None)
                else:
                    logger.info(f"Uploading attachment for page {page_id}: {filename}")
                    try:
                        mime_type = detect_attachment_mime_type(filename)
                        upload_res = self.client.upload_attachment(
                            page_id=page_id,
                            filename=filename,
                            file_bytes=b["_attachment_bytes"],
                            mime_type=mime_type,
                        )
                        if upload_res is not None:
                            uploaded_filenames.add(filename)
                            b["_attachment_uploaded"] = True
                            logger.info(f"Successfully uploaded attachment: {filename}")
                        else:
                            b["_attachment_uploaded"] = False
                            logger.warning(f"Attachment upload returned no result for {filename}")
                    except Exception as e:
                        b["_attachment_uploaded"] = False
                        logger.warning(f"Failed to upload attachment {filename}: {e}")
                    finally:
                        b.pop("_attachment_bytes", None)
                        b.pop("_attachment_zip_entry", None)
                        b.pop("_attachment_zip_path", None)
            elif file_url and not is_embed:
                if is_broken_or_placeholder_url(file_url):
                    b["_attachment_uploaded"] = False
                    b["_is_unsupported_external_embed"] = True
                    b["_unsupported_reason"] = "broken_or_placeholder_url"
                    logger.info(f"Skipped downloading broken/placeholder URL for page {page_id}: {file_url}")
                elif (ext_app := classify_external_app_url(file_url)) is not None:
                    svc_name, _ = ext_app
                    b["_attachment_uploaded"] = False
                    b["_is_unsupported_external_embed"] = True
                    b["service_name"] = b.get("service_name") or svc_name
                    b["_unsupported_reason"] = "external_app_auth_required"
                    logger.info(f"Skipped downloading external app link ({svc_name}) for page {page_id}: {file_url}")
                else:
                    filename = unicodedata.normalize("NFC", self.converter._get_attachment_filename(b))
                    b["_attachment_filename"] = filename

                    if not _is_safe_external_url(file_url):
                        logger.warning(f"Skipped unsafe or non-HTTP attachment URL for page {page_id}: {file_url}")
                        b["_attachment_uploaded"] = False
                    elif filename in uploaded_filenames:
                        b["_attachment_uploaded"] = True
                    else:
                        logger.info(f"Downloading attachment for page {page_id}: {filename}")
                        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
                        try:
                            resp = requests.get(file_url, headers=headers, timeout=30)
                            if resp.status_code == 200:
                                content_type = resp.headers.get("Content-Type", "application/octet-stream")
                                upload_res = self.client.upload_attachment(
                                    page_id=page_id,
                                    filename=filename,
                                    file_bytes=resp.content,
                                    mime_type=content_type,
                                )
                                if upload_res is not None:
                                    uploaded_filenames.add(filename)
                                    b["_attachment_uploaded"] = True
                                    logger.info(f"Successfully uploaded attachment: {filename} ({len(resp.content)} bytes)")
                                else:
                                    b["_attachment_uploaded"] = False
                                    logger.warning(f"Attachment upload returned no result for {filename}")
                            else:
                                b["_attachment_uploaded"] = False
                                logger.warning(f"Download failed for {filename}: HTTP {resp.status_code}")
                        except Exception as e:
                            b["_attachment_uploaded"] = False
                            logger.warning(f"Failed to download/upload attachment {filename}: {e}")
            elif b_type in ("image", "file", "pdf", "video", "audio") and not file_url:
                b["_attachment_uploaded"] = False

            if b.get("children_blocks"):
                self._process_attachments(b["children_blocks"], page_id, uploaded_filenames=uploaded_filenames)

    def _resolve_unique_page_title(
        self, base_title: str, notion_node_id: str, allow_existing_overwrite: bool = True, is_database: bool = False
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Notionノード（ページまたはデータベース）のタイトルがConfluenceスペース内で重複している場合、安全にユニークなタイトルを採番する。
        - ページとデータベースの間、および同種ノード間のいずれのコンフリクトも検出してユニーク化する。
        - 異種ノード（DBが既存Pageを上書き、またはPageが既存DBを上書き）の上書き・削除は絶対に禁止する。
        """
        with self._lock:
            if notion_node_id in self._node_to_title:
                assigned = self._node_to_title[notion_node_id]
                existing = (
                    self.client.get_database_by_title(space_key=self.space_key, title=assigned)
                    if is_database
                    else self.client.get_page_by_title(space_key=self.space_key, title=assigned)
                )
                return assigned, existing

            # Confluenceのページ/データベースタイトル最大文字数は255文字。サフィックス付与（例: " (2)"）を考慮し240文字に安全に切り詰め
            default_t = "Untitled Database" if is_database else "Untitled"
            cleaned_title = (base_title or default_t).strip().replace("\n", " ").replace("\r", "")
            if len(cleaned_title) > 240:
                cleaned_title = cleaned_title[:237] + "..."

            def _is_valid_item(item: Any) -> bool:
                if item is None:
                    return False
                if isinstance(item, dict):
                    return bool(item.get("id"))
                return False

            def _inspect_existing(title_to_check: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
                """指定タイトルについて、Confluence上の既存Pageおよび既存Databaseの両方を検索する。"""
                p = self.client.get_page_by_title(space_key=self.space_key, title=title_to_check)
                d = self.client.get_database_by_title(space_key=self.space_key, title=title_to_check)
                valid_p = p if _is_valid_item(p) else None
                valid_d = d if _is_valid_item(d) else None
                return valid_p, valid_d

            target_title = cleaned_title
            existing_page, existing_db = _inspect_existing(target_title)

            # 同種コンテンツの既存オブジェクトを取得
            target_same_type_existing = existing_db if is_database else existing_page
            # 異種コンテンツの既存オブジェクト（コンフリクト対象: DBならPage、PageならDB）
            target_diff_type_existing = existing_page if is_database else existing_db

            # 既存同種オブジェクトが migration_map で既に別の notion_node_id にマッピングされているか判定
            claimed_by_other = False
            if target_same_type_existing:
                eid = str(target_same_type_existing.get("id"))
                for mid, minfo in self.migration_map.items():
                    if mid != notion_node_id and str(minfo.get("confluence_id")) == eid:
                        claimed_by_other = True
                        break

            # 上書き可能かの判定:
            # 1. 異種コンテンツが同名で存在しないこと（PageがあるのにDBで上書き、またはその逆は不可）
            # 2. 同種コンテンツが存在し、上書きが許可されており、同一実行内でまだ予約されておらず、他ノードに専有されていないこと
            can_overwrite = (
                target_diff_type_existing is None
                and target_same_type_existing is not None
                and allow_existing_overwrite
                and target_title not in self._reserved_titles
                and not claimed_by_other
            )

            # コンフリクト判定:
            # - 異種コンテンツが同名で存在する場合（Page vs Database 衝突）
            # - 同種コンテンツが存在するが上書き不可の場合（同一スペース内の既存コンテンツ）
            # - 同一実行内で既に他のノードによって予約されている場合
            has_conflict = (
                target_diff_type_existing is not None
                or target_title in self._reserved_titles
                or (target_same_type_existing is not None and not can_overwrite)
            )

            existing_item = target_same_type_existing if can_overwrite else None

            if has_conflict:
                suffix = 2
                while suffix < 500:
                    candidate = f"{cleaned_title} ({suffix})"
                    if len(candidate) > 255:
                        candidate = candidate[:255]
                    if candidate not in self._reserved_titles:
                        cand_p, cand_d = _inspect_existing(candidate)
                        cand_same = cand_d if is_database else cand_p
                        cand_diff = cand_p if is_database else cand_d

                        # 異種コンテンツが存在しない場合のみ候補とする
                        if cand_diff is None:
                            if cand_same is None:
                                target_title = candidate
                                existing_item = None
                                break
                            elif allow_existing_overwrite:
                                cand_eid = str(cand_same.get("id"))
                                cand_claimed = any(
                                    mid != notion_node_id and str(minfo.get("confluence_id")) == cand_eid
                                    for mid, minfo in self.migration_map.items()
                                )
                                if not cand_claimed:
                                    target_title = candidate
                                    existing_item = cand_same
                                    break
                    suffix += 1

                conflict_reason = (
                    f"conflict with existing {'page' if is_database else 'database'}"
                    if target_diff_type_existing is not None
                    else f"duplicate {'database' if is_database else 'page'} title"
                )
                logger.info(
                    f"Resolved title conflict in Confluence ({conflict_reason}): "
                    f"[{base_title}] -> [{target_title}] (Node ID: {notion_node_id})"
                )

            self._reserved_titles.add(target_title)
            self._node_to_title[notion_node_id] = target_title
            if hasattr(self, "converter") and hasattr(self.converter, "id_to_title_map"):
                self.converter.id_to_title_map[notion_node_id] = target_title
            return target_title, existing_item

    @staticmethod
    def is_empty_database_row(row_node: PageNode) -> bool:
        """データベース行が完全に空レコード（タイトル無題、本文なし、プロパティ値なし）であるかを判定する。"""
        return _shared_is_empty_database_row(row_node)

    def _register_skipped_db_rows(
        self,
        db_node: PageNode,
        db_url: str,
        skip_only_bodyless: bool = False,
    ) -> None:
        """子ページ作成をスキップしたDB行の参照フォールバック情報を converter へ登録する。"""
        if not hasattr(self, "converter") or not hasattr(self.converter, "skipped_db_rows"):
            return
        with self._lock:
            for child in (db_node.children or []):
                if skip_only_bodyless and database_row_has_substantive_body(child):
                    continue
                info = {
                    "title": child.title,
                    "db_id": db_node.id,
                    "db_title": db_node.title,
                    "url": db_url,
                }
                self.converter.skipped_db_rows[child.id] = info
                norm_id = str(child.id).replace("-", "").lower()
                self.converter.skipped_db_rows[norm_id] = info

    def _apply_root_private_restriction(self, node: PageNode, confluence_page_id: str) -> None:
        """private_mode 有効かつルートノードの場合、作成直後にプライベート閲覧・編集制限を適用する。"""
        if not (self.private_mode and getattr(node, "_is_root", False)):
            return
        success = self.client.set_page_private(confluence_page_id, account_id=self._current_user_account_id)
        if not success:
            warn_msg = (
                f"Failed to apply private restriction to root node [{node.title}] "
                f"(Confluence ID: {confluence_page_id}). Continuing migration with default space permissions."
            )
            logger.warning(warn_msg)
            if self.telemetry:
                self.telemetry.record_warning(warn_msg)
            if self.dashboard:
                self.dashboard.record_warning(f"Private restriction failed: {node.title}")

    def _import_node_recursive(
        self,
        node: PageNode,
        parent_id: Optional[str],
        overwrite: bool,
        allow_sub_parallel: bool = True,
    ) -> str:
        """単一ノードをConfluenceに作成・更新し、子ノードも再帰処理する。"""
        if self.cancel_manager:
            self.cancel_manager.check_cancelled()

        # 空のデータベースレコード行は子ページ作成をスキップ
        if self.is_empty_database_row(node):
            logger.info(f"Skipping empty database row page creation: [{node.title}] (ID: {node.id})")
            return None

        # レジューム判定: 既にPass 1完了済みノードの場合はConfluence作成をスキップ
        if self.checkpoint and self.checkpoint.is_pass1_done(node.id):
            cached = self.checkpoint.get_node_info(node.id)
            confluence_page_id = cached["confluence_id"]
            saved_title = cached.get("title") or node.title

            # Confluenceサーバー上に実在するか確認 (verify_resume_remote=False 時はチェックポイントを信頼してAPI確認を省略)
            if self.verify_resume_remote:
                remote_item = (
                    self.client.get_database_by_id(confluence_page_id)
                    if node.node_type == "database"
                    else self.client.get_page_by_id(confluence_page_id)
                )
            else:
                remote_item = {"id": confluence_page_id}
            if not remote_item:
                logger.warning(
                    f"[RESUME] Cached Confluence ID {confluence_page_id} for [{node.title}] (Node ID: {node.id}) "
                    f"does not exist on Confluence server. Unmarking checkpoint and re-creating..."
                )
                self.checkpoint.unmark_pass1(node.id)
            else:
                node.title = saved_title
                with self._lock:
                    self._reserved_titles.add(saved_title)
                    self._node_to_title[node.id] = saved_title
                confluence_url = cached.get("url", f"{self.client.base_url}/spaces/{self.space_key}/pages/{confluence_page_id}")
                logger.info(f"[RESUME] Skipping creation for [{node.title}] (already migrated to ID {confluence_page_id})")
                if self.dashboard:
                    self.dashboard.record_pass1_skip(node.title)

                self.migration_map[node.id] = {
                    "confluence_id": confluence_page_id,
                    "title": saved_title,
                    "url": confluence_url,
                    "node_type": node.node_type,
                }
                if "views" in cached:
                    self.migration_map[node.id]["views"] = cached["views"]
                if "views_info" in cached:
                    self.migration_map[node.id]["views_info"] = cached["views_info"]

                if node.node_type == "database":
                    resume_db_web_url = f"{self.client.base_url}/spaces/{self.space_key}/database/{confluence_page_id}"
                    for child in node.children:
                        child.parent_db_title = node.title
                        child.parent_db_url = resume_db_web_url

                if node.node_type == "database" and analyze_database_child_page_stats(node)["skip_child_pages"]:
                    node._skipped_row_child_pages = True
                    self._register_skipped_db_rows(node, confluence_url)
                elif node.node_type == "database" and self._is_aggregated_db(node):
                    node._aggregated_mode = True
                    self._register_skipped_db_rows(node, confluence_url, skip_only_bodyless=True)
                    agg_node_id = f"{node.id}__details"
                    agg_info = self.checkpoint.get_node_info(agg_node_id) if self.checkpoint else None
                    if agg_info and agg_info.get("confluence_id"):
                        agg_id = str(agg_info["confluence_id"])
                        agg_title = agg_info.get("title") or f"{node.title} - 詳細ページ一覧"
                        agg_url = agg_info.get("url") or f"{self.client.base_url}/spaces/{self.space_key}/pages/{agg_id}"
                        node._aggregated_detail_page_id = agg_id
                        node._aggregated_detail_page_title = agg_title
                        node._aggregated_detail_page_url = agg_url
                        self.migration_map[node.id]["aggregated_detail_page_id"] = agg_id
                        self.migration_map[node.id]["aggregated_detail_page_title"] = agg_title
                        self.migration_map[node.id]["aggregated_detail_page_url"] = agg_url
                        body_children = [
                            c for c in (node.children or [])
                            if not self.is_empty_database_row(c) and database_row_has_substantive_body(c)
                        ]
                        if body_children and not self.checkpoint.is_pass2_done(agg_node_id):
                            self._process_aggregated_attachments(body_children, agg_id)
                        with self._lock:
                            for c in body_children:
                                anchor = urllib.parse.quote((c.title or "").strip(), safe="")
                                row_anchor_url = f"{agg_url}#{anchor}" if anchor else agg_url
                                self.migration_map[c.id] = {
                                    "confluence_id": agg_id,
                                    "title": c.title,
                                    "url": row_anchor_url,
                                    "node_type": c.node_type,
                                }
                        nested_children = [
                            sub
                            for c in (node.children or [])
                            for sub in (getattr(c, "children", None) or [])
                        ]
                        if nested_children:
                            self._import_children_concurrently(
                                parent_node=node,
                                children=nested_children,
                                parent_id=agg_id,
                                overwrite=overwrite,
                                allow_sub_parallel=allow_sub_parallel,
                            )
                elif node.children:
                    self._import_children_concurrently(
                        parent_node=node,
                        children=node.children,
                        parent_id=confluence_page_id,
                        overwrite=overwrite,
                        allow_sub_parallel=allow_sub_parallel,
                    )
                return confluence_page_id

        if self.dashboard:
            self.dashboard.set_worker_busy(node.title, node_type=node.node_type)

        if node.node_type == "database":
            db_mode = self._get_db_migration_mode(node)
            if db_mode == "skip":
                logger.info(f"[DB Triage] Skipping database [{node.title}] (ID: {node.id}) as requested (mode=skip).")
                return None

            if db_mode == "static_table":
                target_title, existing_page = self._resolve_unique_page_title(
                    base_title=node.title,
                    notion_node_id=node.id,
                    allow_existing_overwrite=overwrite,
                )
                node.title = target_title
                overview_body = self.converter.generate_page_content(node)
                if existing_page and overwrite:
                    confluence_page_id = str(existing_page["id"])
                    self._apply_root_private_restriction(node, confluence_page_id)
                    latest = self.client.get_page_by_id(confluence_page_id, expand="version")
                    ver = latest.get("version", {}).get("number", 1) if latest else 1
                    self.client.update_page(
                        page_id=confluence_page_id,
                        space_key=self.space_key,
                        title=node.title,
                        body=overview_body,
                        current_version=ver,
                    )
                    webui = existing_page.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{confluence_page_id}")
                else:
                    with self._get_parent_lock(parent_id):
                        res = self.client.create_page(
                            space_key=self.space_key,
                            title=node.title,
                            body=overview_body,
                            parent_id=parent_id,
                        )
                    confluence_page_id = str(res["id"])
                    webui = res.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{confluence_page_id}")
                    self._apply_root_private_restriction(node, confluence_page_id)
                confluence_url = f"{self.client.base_url}{webui}"
                if node.icon:
                    self.client.set_page_emoji(confluence_page_id, node.icon)

                db_map_entry = {
                    "confluence_id": confluence_page_id,
                    "title": node.title,
                    "url": confluence_url,
                    "node_type": "page",
                }
                body_children = [
                    c for c in (node.children or [])
                    if not self.is_empty_database_row(c) and database_row_has_substantive_body(c)
                ]
                if body_children:
                    node._aggregated_mode = True
                    agg_node_id = f"{node.id}__details"
                    agg_title, existing_agg = self._resolve_unique_page_title(
                        base_title=f"{node.title} - 詳細ページ一覧",
                        notion_node_id=agg_node_id,
                        allow_existing_overwrite=overwrite,
                    )
                    if existing_agg and overwrite:
                        agg_page_id = str(existing_agg["id"])
                        agg_webui = existing_agg.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{agg_page_id}")
                    else:
                        agg_res = self.client.create_page(
                            space_key=self.space_key,
                            title=agg_title,
                            body="<p>詳細ページを生成中...</p>",
                            parent_id=confluence_page_id,
                        )
                        agg_page_id = str(agg_res["id"])
                        agg_webui = agg_res.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{agg_page_id}")
                    agg_page_url = f"{self.client.base_url}{agg_webui}"
                    node._aggregated_detail_page_id = agg_page_id
                    node._aggregated_detail_page_title = agg_title
                    node._aggregated_detail_page_url = agg_page_url
                    db_map_entry["aggregated_detail_page_id"] = agg_page_id
                    db_map_entry["aggregated_detail_page_title"] = agg_title
                    db_map_entry["aggregated_detail_page_url"] = agg_page_url
                    with self._lock:
                        self.migration_map[agg_node_id] = {
                            "confluence_id": agg_page_id,
                            "title": agg_title,
                            "url": agg_page_url,
                            "node_type": "page",
                        }
                        for c in body_children:
                            anchor = urllib.parse.quote((c.title or "").strip(), safe="")
                            row_anchor_url = f"{agg_page_url}#{anchor}" if anchor else agg_page_url
                            self.migration_map[c.id] = {
                                "confluence_id": agg_page_id,
                                "title": c.title,
                                "url": row_anchor_url,
                                "node_type": c.node_type,
                            }
                    if self.checkpoint:
                        self.checkpoint.record_pass1(
                            node_id=agg_node_id,
                            confluence_id=agg_page_id,
                            title=agg_title,
                            url=agg_page_url,
                            node_type="page",
                        )
                    if self.dashboard:
                        self.dashboard.record_pass1_done(agg_title, node_type="page")
                    self._process_aggregated_attachments(body_children, agg_page_id)
                    nested_children = [
                        sub for c in (node.children or []) for sub in (getattr(c, "children", None) or [])
                    ]
                    if nested_children:
                        self._import_children_concurrently(
                            parent_node=node,
                            children=nested_children,
                            parent_id=agg_page_id,
                            overwrite=overwrite,
                            allow_sub_parallel=allow_sub_parallel,
                        )
                self._register_skipped_db_rows(node, confluence_url, skip_only_bodyless=True)
                with self._lock:
                    self.migration_map[node.id] = db_map_entry
                if self.checkpoint:
                    self.checkpoint.record_pass1(
                        node_id=node.id,
                        confluence_id=confluence_page_id,
                        title=node.title,
                        url=confluence_url,
                        node_type="page",
                    )
                if self.dashboard:
                    self.dashboard.record_pass1_done(node.title, node_type="page")
                return confluence_page_id

            # 重複タイトル解決（同名DBが存在する場合に安全にユニークタイトルを採番）
            target_title, existing_db = self._resolve_unique_page_title(
                base_title=node.title,
                notion_node_id=node.id,
                allow_existing_overwrite=overwrite,
                is_database=True,
            )
            node.title = target_title

            # Confluence Databases (type: database) をネイティブ作成
            legacy_page = self.client.get_page_by_title(
                space_key=self.space_key,
                title=node.title,
            )
            if legacy_page and overwrite:
                # 既存ページが今回のマイグレーションツリーのアクティブノードに割り当てられている場合は絶対に削除しない
                is_active_page = any(
                    str(minfo.get("confluence_id")) == str(legacy_page.get("id"))
                    for minfo in self.migration_map.values()
                )
                if not is_active_page:
                    logger.info(f"Removing unmapped legacy page for database overwrite: [{node.title}] (ID: {legacy_page['id']})")
                    self.client.delete_page(legacy_page["id"])
                else:
                    logger.warning(f"Protected active migrated page [{node.title}] (ID: {legacy_page['id']}) from database deletion.")

            if existing_db and overwrite:
                logger.info(f"Removing existing Confluence Database for fresh recreation: [{node.title}] (ID: {existing_db['id']})")
                self.client.delete_database(existing_db["id"])
                existing_db = None

            existing_db_url = (
                f"{self.client.base_url}/spaces/{self.space_key}/database/{existing_db['id']}"
                if existing_db else None
            )

            db_stats = analyze_database_child_page_stats(node)
            skip_row_pages = bool(db_stats["skip_child_pages"])
            is_individual_mode = (not skip_row_pages) and (db_mode == "individual")
            is_aggregated_mode = (not skip_row_pages) and (db_mode == "aggregated")
            use_text_title = skip_row_pages or is_aggregated_mode
            node._skipped_row_child_pages = skip_row_pages
            node._aggregated_mode = is_aggregated_mode

            # 1. 子ページまたは1ページ集約詳細ページを作成してリンクURLとIDを確定
            child_link_map = {}
            unmigrated_children = []
            agg_page_id: Optional[str] = None
            agg_page_title: Optional[str] = None
            agg_page_url: Optional[str] = None
            if skip_row_pages:
                logger.info(
                    f"Skipping row child page creation for database [{node.title}] "
                    f"(0/{db_stats['total_rows']} rows have substantive body -> title column uses 'text')."
                )
            elif is_aggregated_mode:
                body_children = [
                    c for c in (node.children or [])
                    if not self.is_empty_database_row(c) and database_row_has_substantive_body(c)
                ]
                logger.info(
                    f"Aggregated 1-page mode DB [{node.title}] ({len(body_children)}/{db_stats['total_rows']} rows with body): "
                    f"creating 1 detail page with 'text' key column and hidden 'hyperlink' column."
                )
                agg_node_id = f"{node.id}__details"
                if self.checkpoint and self.checkpoint.is_pass1_done(agg_node_id):
                    cached_agg = self.checkpoint.get_node_info(agg_node_id)
                    agg_page_id = str(cached_agg["confluence_id"])
                    agg_page_title = cached_agg.get("title") or f"{node.title} - 詳細ページ一覧"
                    agg_page_url = cached_agg.get("url") or f"{self.client.base_url}/spaces/{self.space_key}/pages/{agg_page_id}"
                else:
                    agg_page_title, existing_agg = self._resolve_unique_page_title(
                        base_title=f"{node.title} - 詳細ページ一覧",
                        notion_node_id=agg_node_id,
                        allow_existing_overwrite=overwrite,
                    )
                    if existing_agg and overwrite:
                        agg_page_id = str(existing_agg["id"])
                        agg_webui = existing_agg.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{agg_page_id}")
                        agg_page_url = f"{self.client.base_url}{agg_webui}"
                    else:
                        agg_res = self.client.create_page(
                            space_key=self.space_key,
                            title=agg_page_title,
                            body="<p>詳細ページを生成中...</p>",
                            parent_id=parent_id,
                        )
                        agg_page_id = str(agg_res["id"])
                        agg_webui = agg_res.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{agg_page_id}")
                        agg_page_url = f"{self.client.base_url}{agg_webui}"
                    if self.checkpoint:
                        self.checkpoint.record_pass1(
                            node_id=agg_node_id,
                            confluence_id=agg_page_id,
                            title=agg_page_title,
                            url=agg_page_url,
                            node_type="page",
                        )
                    if self.dashboard:
                        self.dashboard.record_pass1_done(agg_page_title, node_type="page")

                node._aggregated_detail_page_id = agg_page_id
                node._aggregated_detail_page_title = agg_page_title
                node._aggregated_detail_page_url = agg_page_url
                self._process_aggregated_attachments(body_children, agg_page_id)
                with self._lock:
                    self.migration_map[agg_node_id] = {
                        "confluence_id": agg_page_id,
                        "title": agg_page_title,
                        "url": agg_page_url,
                        "node_type": "page",
                    }
                    for child in body_children:
                        anchor = urllib.parse.quote((child.title or "").strip(), safe="")
                        row_anchor_url = f"{agg_page_url}#{anchor}" if anchor else agg_page_url
                        child_link_map[child.id] = row_anchor_url
                        self.migration_map[child.id] = {
                            "confluence_id": agg_page_id,
                            "title": child.title,
                            "url": row_anchor_url,
                            "node_type": child.node_type,
                        }
                nested_children = [
                    sub
                    for c in (node.children or [])
                    for sub in (getattr(c, "children", None) or [])
                ]
                if nested_children:
                    self._import_children_concurrently(
                        parent_node=node,
                        children=nested_children,
                        parent_id=agg_page_id,
                        overwrite=overwrite,
                        allow_sub_parallel=allow_sub_parallel,
                    )
            else:
                logger.info(
                    f"Individual child-page mode DB [{node.title}] ({db_stats['body_rows']}/{db_stats['total_rows']} rows with body <= {self.individual_db_child_threshold})"
                )
                for child in node.children:
                    child.parent_db_title = node.title
                    if existing_db_url:
                        child.parent_db_url = existing_db_url
                    if self.is_empty_database_row(child):
                        logger.info(f"Skipping empty database row child page: [{child.title}] (ID: {child.id})")
                        continue
                    if self.checkpoint and self.checkpoint.is_pass1_done(child.id):
                        cached = self.checkpoint.get_node_info(child.id)
                        cid = cached["confluence_id"]
                        with self._lock:
                            self.migration_map[child.id] = {
                                "confluence_id": cid,
                                "title": child.title,
                                "url": cached.get("url", f"{self.client.base_url}/spaces/{self.space_key}/pages/{cid}"),
                                "node_type": child.node_type,
                            }
                        child_link_map[child.id] = f"{self.client.base_url}/spaces/{self.space_key}/pages/{cid}"
                    else:
                        unmigrated_children.append(child)

            if unmigrated_children:
                def _migrate_child_task(child_node):
                    if self.cancel_manager:
                        self.cancel_manager.check_cancelled()
                    if self.dashboard:
                        self.dashboard.set_worker_busy(child_node.title, node_type="db_row")
                    try:
                        child_node.parent_db_title = node.title
                        cid = self._import_node_recursive(
                            child_node,
                            parent_id=parent_id,
                            overwrite=overwrite,
                            allow_sub_parallel=False,
                        )
                        return child_node.id, cid
                    except Exception as e:
                        err_msg = f"Failed to create database row page [{child_node.title}] for database [{node.title}]: {e}"
                        logger.error(err_msg)
                        if self.telemetry:
                            self.telemetry.record_warning(err_msg)
                        if self.cancel_manager:
                            self.cancel_manager.trigger_error_halt(err_msg)
                        raise
                    finally:
                        if self.dashboard:
                            self.dashboard.set_worker_idle()

                # DB行レコードは同一の parent_id を共有するため、親ロック競合 (409 Conflict) を防止するため逐次作成する
                if self.dashboard:
                    self.dashboard.set_main_task(f"Pass 1: DB子レコード作成中 (DB: [{node.title}], {len(unmigrated_children)} 件)")
                for c in unmigrated_children:
                    chid, cid = _migrate_child_task(c)
                    child_link_map[chid] = f"{self.client.base_url}/spaces/{self.space_key}/pages/{cid}"

            if existing_db:
                logger.info(f"Reusing existing Confluence Database: [{node.title}] (ID: {existing_db['id']})")
                confluence_page_id = existing_db["id"]
                webui = existing_db.get("_links", {}).get("webui", f"/spaces/{self.space_key}/database/{confluence_page_id}")
                confluence_url = f"{self.client.base_url}{webui}"
            else:
                # 2. Confluence Database 用の構造化CSVを生成
                csv_body = self.converter.generate_database_csv(
                    node,
                    child_link_map,
                    use_text_title=use_text_title,
                    add_hidden_link_col=is_aggregated_mode,
                )

                # 3. Confluence Database を新規作成
                space_id = self.client.get_space_id(self.space_key)
                if not space_id:
                    raise RuntimeError(f"Could not resolve Confluence Space ID for space key '{self.space_key}'.")
                logger.info(f"Creating native Confluence Database: [{node.title}] under parent {parent_id}")
                try:
                    with self._get_parent_lock(parent_id):
                        db_res = self.client.create_database(
                            space_id=space_id,
                            title=node.title,
                            parent_id=parent_id,
                            csv_body=csv_body,
                        )
                    confluence_page_id = db_res["id"]
                    webui = db_res.get("_links", {}).get("webui", f"/spaces/{self.space_key}/database/{confluence_page_id}")
                    confluence_url = f"{self.client.base_url}{webui}"
                except Exception as e:
                    logger.warning(f"Failed to create native Confluence Database for [{node.title}] ({e}). Falling back to standard Confluence page with overview table.")
                    fallback_body = self.converter.generate_page_content(node)
                    res = self.client.create_page(
                        space_key=self.space_key,
                        title=node.title,
                        body=fallback_body,
                        parent_id=parent_id,
                    )
                    confluence_page_id = res["id"]
                    webui = res.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{confluence_page_id}")
                    confluence_url = f"{self.client.base_url}{webui}"

            if skip_row_pages:
                self._register_skipped_db_rows(node, confluence_url)
            elif is_aggregated_mode:
                self._register_skipped_db_rows(node, confluence_url, skip_only_bodyless=True)

            # プライベート移行モード（DBルート作成直後の即時ロック）
            self._apply_root_private_restriction(node, confluence_page_id)

            # 3.5. 単一選択・複数選択プロパティの設定を反映（ステータス等はsingle-select、カテゴリ等はmulti-select）
            single_props, multi_props = self.converter.get_select_field_configurations(node)
            if single_props or multi_props:
                logger.info(f"Applying select/multi-select field options for [{node.title}]: single={single_props}, multi={multi_props}")
                self.client.update_database_field_options(
                    confluence_page_id,
                    single_select_fields=single_props,
                    multi_select_fields=multi_props,
                )

            # 3.6. データベースの追加ビュー（Board/カンバン、Gallery/カード、Group byフィルタービュー）を自動プロビジョニング
            view_id_map = {}
            views_info_list = []
            try:
                existing_views = self.client.get_database_views(confluence_page_id)
                existing_board_names = {v.get("name") for v in existing_views if v.get("type") == "board"}
                has_gallery_view = any(v.get("type") == "gallery" or "ギャラリー" in v.get("name", "") for v in existing_views)

                confirmed_views = node.properties.get("confirmed_views")
                if confirmed_views:
                    logger.info(f"Applying {len(confirmed_views)} confirmed views from worksheet for [{node.title}]")
                    for cv in confirmed_views:
                        v_name = cv.get("name") or "Custom View"
                        v_layout = cv.get("layout", "table")
                        v_group = cv.get("group_by")
                        if v_layout == "board" and v_group:
                            if v_name not in existing_board_names:
                                url_get = f"{self.client.base_url}/api/v2/databases/{confluence_page_id}"
                                r_get = requests.get(url_get, auth=self.client.auth, timeout=15)
                                if r_get.ok:
                                    cur_v = r_get.json().get("version", {}).get("number", 1)
                                    b_csv = f"<metadata>\nadd_view,{v_name},board,{v_group}\n</metadata>\n<data>\noperation_type,entry_index\n</data>"
                                    requests.put(
                                        url_get,
                                        json={"id": str(confluence_page_id), "version": {"number": cur_v + 1}, "body": {"representation": "csv", "value": b_csv}},
                                        auth=self.client.auth,
                                        timeout=30,
                                    )
                                    logger.info(f"Created confirmed board view [{v_name}] on database [{node.title}]")
                        elif v_layout == "gallery":
                            if not has_gallery_view:
                                self.client.create_database_view(confluence_page_id, view_name=v_name, layout="gallery")
                else:
                    norm_db_id = node.id.replace("-", "").lower()
                    db_req = getattr(self, "_db_view_requirements", {}).get(norm_db_id, {})
                    STATUS_FIELD_NAMES = ("ステータス", "Status", "進捗", "状態", "優先度", "Priority")
                    primary_status_field = next((f for f in single_props if f in STATUS_FIELD_NAMES), None)
                    primary_status_vals = (
                        self._extract_db_field_values(node, primary_status_field)
                        if primary_status_field
                        else []
                    )
                    is_smart_status = 2 <= len(primary_status_vals) <= 7

                    # 3.6-a. ボードビュー (Board): 他ページから参照されている軸、または厳選ステータス列(値2〜7件)のみ生成
                    board_fields_to_create: List[str] = []
                    for bf in sorted(db_req.get("board_fields", set())):
                        if bf in single_props and bf not in board_fields_to_create:
                            board_fields_to_create.append(bf)
                    if db_req.get("needs_board") and not board_fields_to_create:
                        fallback_bf = primary_status_field or (single_props[0] if single_props else None)
                        if fallback_bf:
                            board_fields_to_create.append(fallback_bf)
                    if is_smart_status and primary_status_field and primary_status_field not in board_fields_to_create:
                        board_fields_to_create.append(primary_status_field)

                    for s_field in board_fields_to_create:
                        board_view_name = f"{s_field}別ボード"
                        if board_view_name in existing_board_names:
                            logger.info(f"Board view [{board_view_name}] already exists on database [{node.title}], reusing it.")
                            continue

                        url_get = f"{self.client.base_url}/api/v2/databases/{confluence_page_id}"
                        r_get = requests.get(url_get, auth=self.client.auth, timeout=15)
                        if r_get.ok:
                            cur_v = r_get.json().get("version", {}).get("number", 1)
                            b_csv = f"<metadata>\nadd_view,{board_view_name},board,{s_field}\n</metadata>\n<data>\noperation_type,entry_index\n</data>"
                            requests.put(
                                url_get,
                                json={"id": str(confluence_page_id), "version": {"number": cur_v + 1}, "body": {"representation": "csv", "value": b_csv}},
                                auth=self.client.auth,
                                timeout=30,
                            )
                            logger.info(f"Created board view [{board_view_name}] on database [{node.title}]")

                    # 3.6-b. ギャラリービュー (Gallery): 他ページから明示的に参照されている場合のみ生成
                    if db_req.get("needs_gallery"):
                        if not has_gallery_view:
                            self.client.create_database_view(confluence_page_id, view_name="ギャラリービュー", layout="gallery")
                        else:
                            logger.info(f"Gallery view already exists on database [{node.title}], reusing it.")

                    # 3.6-c. フィルタービュー:
                    #   1) 参照元で Group by 指定された列の全ユニーク値
                    #   2) 参照元のページタイトル・見出し・ビュー名に一致するカテゴリ値等のピンポイント生成（件数上限なし）
                    #   3) 代表ステータス列かつユニーク値 2〜7件のスマート厳選自動生成
                    filter_views_to_create: Dict[str, Set[str]] = {}
                    for gb_field in sorted(db_req.get("group_by_fields", set())):
                        vals = self._extract_db_field_values(node, gb_field)
                        if vals:
                            filter_views_to_create.setdefault(gb_field, set()).update(vals)

                    requested_contexts = db_req.get("requested_contexts", set())
                    if requested_contexts:
                        all_candidate_props = list(dict.fromkeys(single_props + multi_props))
                        for prop_name in all_candidate_props:
                            vals = self._extract_db_field_values(node, prop_name)
                            for v in vals:
                                if any((v == ctx or (len(v) >= 2 and v in ctx)) for ctx in requested_contexts):
                                    filter_views_to_create.setdefault(prop_name, set()).add(v)

                    if is_smart_status and primary_status_field:
                        filter_views_to_create.setdefault(primary_status_field, set()).update(primary_status_vals)

                    for f_field, f_vals_set in filter_views_to_create.items():
                        if f_vals_set:
                            sorted_vals = sorted(list(f_vals_set))
                            logger.info(f"Setting up filtered views for [{node.title}] on field [{f_field}]: {sorted_vals}")
                            self.client.setup_grouped_database_views(confluence_page_id, f_field, sorted_vals)

                # 最新のビュー一覧を取得してマッピング
                # 重複ビューがある場合、適切な type (board/gallery/table) を最優先でマッピングする
                latest_views = self.client.get_database_views(confluence_page_id)
                views_info_list = [
                    {"id": v["id"], "name": v.get("name", ""), "type": v.get("type", "")}
                    for v in latest_views
                ]

                # 1. 通常のマッピング
                for v in latest_views:
                    v_name = v.get("name", "")
                    v_id = v.get("id", "")
                    v_type = v.get("type", "")
                    if v_name not in view_id_map:
                        view_id_map[v_name] = v_id
                    else:
                        # 既存キーがある場合、ボードなら type == 'board'、ギャラリーなら type == 'gallery' を優先上書き
                        if "ボード" in v_name and v_type == "board":
                            view_id_map[v_name] = v_id
                        elif "ギャラリー" in v_name and v_type == "gallery":
                            view_id_map[v_name] = v_id

                # 2. ボードビューについては必ず type == 'board' の ID を優先確保
                for v in latest_views:
                    if v.get("type") == "board":
                        view_id_map[v.get("name", "")] = v.get("id")
                    elif v.get("type") == "gallery":
                        view_id_map[v.get("name", "")] = v.get("id")

            except Exception as e:
                logger.warning(f"Error provisioning additional database views for [{node.title}]: {e}")

            # 4. データベースのエントリ一覧を取得し、各子ページに対応する entryId をマッピング
            expected = len(node.children)
            logger.info(f"Checking database [{node.title}] entries index (expecting {expected} entries, non-blocking check)...")
            entries = self.client.get_database_entries(confluence_page_id, expected_count=expected, timeout_sec=3)
            logger.info(f"Retrieved {len(entries)} indexed entries from database [{node.title}] (ID: {confluence_page_id})")
            page_to_entry_map = self._build_page_to_entry_map(entries)
            text_to_entry_map = self._build_text_key_to_entry_map(entries) if is_aggregated_mode else {}

            db_web_url = f"{self.client.base_url}/spaces/{self.space_key}/database/{confluence_page_id}"

            indexed_children = []
            pending_children = []
            pending_child_ids = []
            for child in node.children:
                child.parent_db_title = node.title
                child.parent_db_url = db_web_url
                if child.id not in child_link_map:
                    continue
                if is_aggregated_mode:
                    eid = text_to_entry_map.get((child.title or "").strip())
                    if eid:
                        child.entry_id = eid
                    continue
                with self._lock:
                    c_info = self.migration_map.get(child.id)
                if c_info:
                    c_id = str(c_info["confluence_id"])
                    if c_id in page_to_entry_map:
                        indexed_children.append((child, page_to_entry_map[c_id]))
                    else:
                        pending_children.append(child)
                        pending_child_ids.append(child.id)

            if indexed_children:
                self._embed_row_views_concurrently(indexed_children, db_web_url, confluence_page_id)

            if pending_children:
                # 親階層の不一致（Parent hierarchy mismatch）を防ぐため、インデックス待ちの子ページも即座に親をDBに移動
                self._reparent_pending_children_concurrently(pending_children, db_web_url, confluence_page_id)
                logger.info(f"[Approach B] Deferred {len(pending_child_ids)} row views for database [{node.title}] (reparented; index resolve via --resume)")

            with self._lock:
                db_map_entry = {
                    "confluence_id": confluence_page_id,
                    "title": node.title,
                    "url": confluence_url,
                    "node_type": "database",
                    "views": view_id_map,
                    "views_info": views_info_list,
                    "pending_row_views": pending_child_ids,
                }
                if is_aggregated_mode and agg_page_id:
                    db_map_entry["aggregated_detail_page_id"] = agg_page_id
                    db_map_entry["aggregated_detail_page_title"] = agg_page_title
                    db_map_entry["aggregated_detail_page_url"] = agg_page_url
                self.migration_map[node.id] = db_map_entry
                if self.checkpoint:
                    self.checkpoint.record_pass1(
                        node_id=node.id,
                        confluence_id=confluence_page_id,
                        title=node.title,
                        url=confluence_url,
                        node_type="database",
                        views=view_id_map,
                        views_info=views_info_list,
                        pending_row_views=pending_child_ids,
                    )
            if self.dashboard:
                self.dashboard.record_pass1_done(node.title, node_type="database")
            return confluence_page_id

        target_title, existing_page = self._resolve_unique_page_title(
            base_title=node.title,
            notion_node_id=node.id,
            allow_existing_overwrite=overwrite,
        )
        node.title = target_title

        if existing_page and overwrite:
            confluence_page_id = existing_page["id"]
            # プライベート移行モード（既存ルート上書き前の即時ロック）
            self._apply_root_private_restriction(node, confluence_page_id)
            # カバー画像・添付ファイル処理（既存ページIDを利用）
            self._process_cover(node, confluence_page_id)
            if node.blocks:
                self._process_attachments(node.blocks, confluence_page_id)

            # 本文生成
            body_xhtml = self.converter.generate_page_content(node)
            if not body_xhtml.strip():
                body_xhtml = "<p>（本文なし）</p>"
            if self.telemetry:
                for m in re.findall(r'<ac:structured-macro ac:name="([^"]+)"', body_xhtml):
                    self.telemetry.record_macro(m)

            # 最新バージョンを再取得して確実に更新
            latest = self.client.get_page_by_id(confluence_page_id, expand="version")
            current_version = latest.get("version", {}).get("number", 1) if latest else 1

            logger.info(f"Updating existing Confluence page: [{target_title}] (ID: {confluence_page_id}, v{current_version + 1})")
            res = self.client.update_page(
                page_id=confluence_page_id,
                space_key=self.space_key,
                title=target_title,
                body=body_xhtml,
                current_version=current_version,
                parent_id=parent_id,
            )
            webui = res.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{confluence_page_id}")
            confluence_url = f"{self.client.base_url}{webui}"

        else:
            # 新規作成フロー
            dummy_body = "<p>読み込み中...</p>"
            confluence_page_id, confluence_url = self._create_or_update_page(
                title=target_title,
                body=dummy_body,
                parent_id=parent_id,
                overwrite=False,
            )
            # プライベート移行モード（dummy_body 作成直後・添付/本文更新前の即時ロック）
            self._apply_root_private_restriction(node, confluence_page_id)
            self._process_cover(node, confluence_page_id)
            if node.blocks:
                self._process_attachments(node.blocks, confluence_page_id)

            body_xhtml = self.converter.generate_page_content(node)
            if not body_xhtml.strip():
                body_xhtml = "<p>（本文なし）</p>"
            if self.telemetry:
                for m in re.findall(r'<ac:structured-macro ac:name="([^"]+)"', body_xhtml):
                    self.telemetry.record_macro(m)

            latest = self.client.get_page_by_id(confluence_page_id, expand="version")
            current_version = latest.get("version", {}).get("number", 1) if latest else 1

            res = self.client.update_page(
                page_id=confluence_page_id,
                space_key=self.space_key,
                title=target_title,
                body=body_xhtml,
                current_version=current_version,
                parent_id=parent_id,
            )
            webui = res.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{confluence_page_id}")
            confluence_url = f"{self.client.base_url}{webui}"

        # ページ絵文字アイコンの設定
        if node.icon:
            self.client.set_page_emoji(confluence_page_id, node.icon)

        # データベース行の場合、Page Properties Reportマクロ連動用のラベルを付与
        if node.node_type == "database_row":
            labels = ["notion-db-row"]
            if parent_id:
                labels.append(f"db-{parent_id}")
            self.client.add_labels(confluence_page_id, labels)

        with self._lock:
            self.migration_map[node.id] = {
                "confluence_id": confluence_page_id,
                "title": node.title,
                "url": confluence_url,
                "node_type": node.node_type,
            }
            if self.checkpoint:
                self.checkpoint.record_pass1(
                    node_id=node.id,
                    confluence_id=confluence_page_id,
                    title=node.title,
                    url=confluence_url,
                    node_type=node.node_type,
                )

        if self.dashboard:
            self.dashboard.record_pass1_done(node.title, node_type=node.node_type)

        # ページ最上位コメントの移行 (Parity with Atlassian official importer)
        comments = getattr(node, "comments", [])
        if comments:
            logger.info(f"Importing {len(comments)} top-level comments for page [{node.title}] (ID: {confluence_page_id})")
            for c in comments:
                rich_texts = c.get("rich_text", [])
                c_text = "".join(t.get("plain_text", "") for t in rich_texts)
                if c_text:
                    c_by = c.get("created_by") or {}
                    u_id = c_by.get("id")
                    u_name = (
                        c.get("display_name", {}).get("resolved_name")
                        or c_by.get("name")
                        or "Notion User"
                    )
                    u_email = c_by.get("person", {}).get("email") if isinstance(c_by.get("person"), dict) else None
                    if self.user_mapper:
                        author_html = self.user_mapper.format_mention(user_id=u_id, email=u_email, name=u_name)
                    else:
                        author_html = f"<strong>{html.escape(u_name)}</strong>"

                    created_time = c.get("created_time") or ""
                    # タイムゾーン変換を適用して可読日時に整形
                    if created_time:
                        from src.timezone_util import format_iso_datetime
                        clean_time = format_iso_datetime(created_time, tz=self.timezone)
                    else:
                        clean_time = ""
                    time_meta = f" | 🕒 {clean_time}" if clean_time else ""

                    header_html = (
                        f'<p style="color: #6B778C; font-size: 12px; margin-bottom: 4px;">'
                        f'<strong>💬 Notion元コメント</strong> — {author_html}{html.escape(time_meta)}'
                        f'</p>'
                    )
                    c_html = f"{header_html}\n<p>{html.escape(c_text)}</p>"
                    try:
                        self.client.create_comment(confluence_page_id, c_html)
                    except Exception as ce:
                        logger.warning(f"Could not migrate comment to page {confluence_page_id}: {ce}")
            # コメントのインポート完了後に即時メモリ解放
            node.comments = []

        # 6. 子ノードのインポート（今回作成したページを親として深さ優先・ブランチ並行実行）
        if node.children:
            self._import_children_concurrently(
                parent_node=node,
                children=node.children,
                parent_id=confluence_page_id,
                overwrite=overwrite,
                allow_sub_parallel=allow_sub_parallel,
            )

        return confluence_page_id

    def _import_children_concurrently(
        self,
        parent_node: PageNode,
        children: List[PageNode],
        parent_id: Optional[str],
        overwrite: bool,
        allow_sub_parallel: bool = True,
    ):
        """親ノード直下の兄弟ノード群（ページおよびデータベース）をインポートする。
        同一親に対する同時作成による 409 Conflict を防ぐため、ブランチ単位で並行化し、
        ブランチ内部は完全な深さ優先（DFS）で逐次処理する。
        """
        if not children:
            return

        def _child_task(child_node: PageNode, sub_parallel: bool):
            if self.cancel_manager:
                self.cancel_manager.check_cancelled()
            if self.dashboard:
                self.dashboard.set_worker_busy(child_node.title, node_type=child_node.node_type)
            try:
                if parent_node.node_type == "database":
                    child_node.parent_db_title = parent_node.title
                return self._import_node_recursive(
                    child_node,
                    parent_id=parent_id,
                    overwrite=overwrite,
                    allow_sub_parallel=sub_parallel,
                )
            except Exception as e:
                err_msg = f"Failed to import child node [{child_node.title}]: {e}"
                logger.error(err_msg)
                if self.telemetry:
                    self.telemetry.record_warning(err_msg)
                if self.cancel_manager:
                    self.cancel_manager.trigger_error_halt(err_msg)
                raise
            finally:
                if self.dashboard:
                    self.dashboard.set_worker_idle()

        workers = min(self.page_workers, len(children))
        if allow_sub_parallel and workers > 1:
            logger.info(
                f"Concurrently importing {len(children)} branch subtrees under [{parent_node.title}] "
                f"(Parent ID: {parent_id}) with {workers} worker threads (DFS per branch)..."
            )
            if self.dashboard:
                self.dashboard.set_main_task(f"Pass 1: ブランチ並行作成中 (親: [{parent_node.title}], {len(children)} 本)")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_child = {executor.submit(_child_task, c, False): c for c in children}
                for future in concurrent.futures.as_completed(future_to_child):
                    c = future_to_child[future]
                    try:
                        future.result()
                    except Exception as e:
                        err_msg = f"Worker failed importing branch [{c.title}]: {e}"
                        logger.error(err_msg)
                        if self.telemetry:
                            self.telemetry.record_warning(err_msg)
                        if self.cancel_manager:
                            self.cancel_manager.trigger_error_halt(err_msg)
                        raise
        else:
            # 深さ優先（DFS）逐次実行
            sub_par = (len(children) == 1 and allow_sub_parallel)
            for child in children:
                _child_task(child, sub_par)

        # 並行作成完了後、Notion元の定義順序に合わせてConfluence左ペインの並び順を整列
        if len(children) > 1:
            try:
                valid_cids = []
                for c in children:
                    with self._lock:
                        info = self.migration_map.get(c.id)
                    if info and info.get("confluence_id"):
                        valid_cids.append(str(info["confluence_id"]))

                if len(valid_cids) > 1:
                    if self.dashboard:
                        self.dashboard.set_main_task(f"Pass 1: 子ノード並び順を整列中 (親: [{parent_node.title}])")
                    for i in range(1, len(valid_cids)):
                        prev_cid = valid_cids[i - 1]
                        curr_cid = valid_cids[i]
                        self.client.move_page_position(curr_cid, "after", prev_cid)
                    logger.info(f"Reordered {len(valid_cids)} sibling pages under [{parent_node.title}] to match Notion order.")
            except Exception as e:
                logger.warning(f"Could not reorder sibling pages under [{parent_node.title}]: {e}")

    def _get_parent_lock(self, parent_id: Optional[str]) -> threading.Lock:
        """同一親コンテナに対する並行作成による Confluence 409 Conflict を防ぐための親単位ロックを取得する。"""
        key = str(parent_id) if parent_id else "__ROOT__"
        with self._lock:
            if key not in self._parent_locks:
                self._parent_locks[key] = threading.Lock()
            return self._parent_locks[key]

    def _create_or_update_page(
        self,
        title: str,
        body: str,
        parent_id: Optional[str],
        overwrite: bool,
    ) -> Tuple[str, str]:
        """Confluence上にページを作成する。タイトルはすでに _resolve_unique_page_title で予約・一元化されている。"""
        logger.info(f"Creating new Confluence page: [{title}] (Parent: {parent_id})")
        parent_lock = self._get_parent_lock(parent_id)
        with parent_lock:
            res = self.client.create_page(
                space_key=self.space_key,
                title=title,
                body=body,
                parent_id=parent_id,
            )
        page_id = res["id"]
        webui = res.get("_links", {}).get("webui", f"/spaces/{self.space_key}/pages/{page_id}")
        page_url = f"{self.client.base_url}{webui}"
        return page_id, page_url
