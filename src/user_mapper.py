"""
NotionユーザーとConfluence（Atlassian Account ID）のユーザーマッピング管理モジュール (Task B.11)。
"""

import os
import csv
import html
import json
import logging
from typing import Dict, Any, List, Optional, Tuple, Set

logger = logging.getLogger(__name__)


class UserMapping:
    def __init__(
        self,
        notion_name: str = "",
        notion_email: str = "",
        notion_id: str = "",
        confluence_account_id: str = "",
        confluence_display_name: str = "",
    ):
        self.notion_name = (notion_name or "").strip()
        self.notion_email = (notion_email or "").strip().lower()
        self.notion_id = (notion_id or "").strip().replace("-", "").lower()
        self.confluence_account_id = (confluence_account_id or "").strip()
        self.confluence_display_name = (confluence_display_name or "").strip()

    def is_resolved(self) -> bool:
        return bool(self.confluence_account_id)


class UserMappingManager:
    """ユーザーマッピングの読込・検索・自動探索エクスポートを管理するクラス。"""

    def __init__(self, mapping_path: Optional[str] = None):
        self.mappings: List[UserMapping] = []
        self._by_id: Dict[str, UserMapping] = {}
        self._by_email: Dict[str, UserMapping] = {}
        self._by_name: Dict[str, UserMapping] = {}
        self._names_multi: Dict[str, List[UserMapping]] = {}

        if mapping_path and os.path.exists(mapping_path):
            self.load(mapping_path)

    def load(self, file_path: str):
        """CSV または JSON からマッピング定義を読み込む。"""
        if file_path.endswith(".json"):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    self.add_mapping(
                        notion_name=item.get("notion_name", ""),
                        notion_email=item.get("notion_email", ""),
                        notion_id=item.get("notion_id", ""),
                        confluence_account_id=item.get("confluence_account_id", ""),
                        confluence_display_name=item.get("confluence_display_name", ""),
                    )
        else:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.add_mapping(
                        notion_name=row.get("notion_name", ""),
                        notion_email=row.get("notion_email", ""),
                        notion_id=row.get("notion_id", ""),
                        confluence_account_id=row.get("confluence_account_id", ""),
                        confluence_display_name=row.get("confluence_display_name", ""),
                    )
        logger.info(f"Loaded {len(self.mappings)} user mappings from {file_path}")

    def add_mapping(
        self,
        notion_name: str,
        notion_email: str = "",
        notion_id: str = "",
        confluence_account_id: str = "",
        confluence_display_name: str = "",
    ):
        m = UserMapping(
            notion_name=notion_name,
            notion_email=notion_email,
            notion_id=notion_id,
            confluence_account_id=confluence_account_id,
            confluence_display_name=confluence_display_name,
        )
        self.mappings.append(m)
        if m.notion_id:
            self._by_id[m.notion_id] = m
        if m.notion_email:
            self._by_email[m.notion_email] = m
        if m.notion_name:
            name_key = m.notion_name.lower()
            self._by_name[name_key] = m
            self._names_multi.setdefault(name_key, []).append(m)

    def resolve_user(
        self,
        user_id: Optional[str] = None,
        email: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[UserMapping]:
        """Notion ユーザー情報（ID, Email, Name）からマッピングされた Confluence ユーザーを解決する。"""
        if user_id:
            norm_id = user_id.replace("-", "").lower()
            if norm_id in self._by_id:
                return self._by_id[norm_id]

        if email:
            norm_email = email.strip().lower()
            if norm_email in self._by_email:
                return self._by_email[norm_email]

        if name:
            norm_name = name.strip().lower()
            cands = self._names_multi.get(norm_name, [])
            if len(cands) > 1:
                # 同姓同名で複数候補がある場合は誤紐付け防止のため名前のみ解決を行わない
                return None
            if norm_name in self._by_name:
                return self._by_name[norm_name]

        return None

    def format_mention(
        self,
        user_id: Optional[str] = None,
        email: Optional[str] = None,
        name: Optional[str] = None,
    ) -> str:
        """
        Confluence Storage Format (XHTML) におけるメンション文字列を生成する。
        マッピングが存在する場合は <ri:user ri:account-id="..." />、未解決の場合は @表示名 を返す。
        """
        m = self.resolve_user(user_id=user_id, email=email, name=name)
        display = name or (m.notion_name if m else "") or "User"
        if m and m.is_resolved():
            return f'<ac:link><ri:user ri:account-id="{html.escape(m.confluence_account_id)}" /></ac:link>'
        return f"@{html.escape(display)}"

    @staticmethod
    def _iter_block_user_mentions(blocks: List[Dict[str, Any]]):
        """ブロックリスト（children_blocks含む）からユーザーメンション辞書を再帰的に列挙する。"""
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            b_type = b.get("type", "")
            b_data = b.get(b_type) if isinstance(b.get(b_type), dict) else {}

            rt_lists = []
            if isinstance(b_data.get("rich_text"), list):
                rt_lists.append(b_data["rich_text"])
            if isinstance(b_data.get("caption"), list):
                rt_lists.append(b_data["caption"])
            if b_type == "table_row" and isinstance(b_data.get("cells"), list):
                for cell in b_data["cells"]:
                    if isinstance(cell, list):
                        rt_lists.append(cell)

            for rt_list in rt_lists:
                for item in rt_list:
                    if not isinstance(item, dict) or item.get("type") != "mention":
                        continue
                    mention = item.get("mention") or {}
                    if isinstance(mention, dict) and mention.get("type") == "user":
                        u = mention.get("user")
                        if isinstance(u, dict):
                            yield u

            if b.get("children_blocks"):
                yield from UserMappingManager._iter_block_user_mentions(b["children_blocks"])

    @classmethod
    def collect_users_from_tree(cls, root_node: Any) -> List[Dict[str, str]]:
        """PageNode ツリー全体から登場する Notion ユーザー（作成者、担当者、コメント発言者、本文メンション等）を収集する。"""
        users_map: Dict[str, Dict[str, str]] = {}

        def _record(uid: str = "", name: str = "", email: str = ""):
            uid_s = (uid or "").strip()
            name_s = (name or "").strip()
            email_s = (email or "").strip()
            key = (uid_s.replace("-", "").lower() if uid_s else "") or (email_s.lower() if email_s else "") or (name_s.lower() if name_s else "")
            if not key:
                return
            if key not in users_map:
                users_map[key] = {"notion_id": uid_s, "notion_name": name_s, "notion_email": email_s}
            else:
                if uid_s and not users_map[key]["notion_id"]:
                    users_map[key]["notion_id"] = uid_s
                if name_s and not users_map[key]["notion_name"]:
                    users_map[key]["notion_name"] = name_s
                if email_s and not users_map[key]["notion_email"]:
                    users_map[key]["notion_email"] = email_s

        def _record_user_dict(u: Any):
            if not isinstance(u, dict):
                return
            p_obj = u.get("person")
            u_email = p_obj.get("email", "") if isinstance(p_obj, dict) else ""
            _record(u.get("id", ""), u.get("name", ""), u_email)

        def _scan_node(n: Any):
            if n is None:
                return
            # ページ作成者・最終更新者
            cb = getattr(n, "created_by", None) or (n.properties.get("created_by") if hasattr(n, "properties") and isinstance(n.properties, dict) else None)
            _record_user_dict(cb)
            leb = getattr(n, "last_edited_by", None)
            _record_user_dict(leb)

            # ページコメント発言者
            comments = getattr(n, "comments", []) or []
            for c in comments:
                if isinstance(c, dict):
                    _record_user_dict(c.get("created_by"))

            # データベース行の people / created_by / last_edited_by プロパティ
            if hasattr(n, "properties") and isinstance(n.properties, dict):
                for p_name, p_val in n.properties.items():
                    if isinstance(p_val, dict) and p_val.get("type") in ("people", "created_by", "last_edited_by"):
                        for u in p_val.get("users", []):
                            _record_user_dict(u)
                        if p_val.get("type") in ("created_by", "last_edited_by"):
                            _record_user_dict(p_val.get(p_val["type"]))

            # 本文ブロック内のユーザーメンション
            blocks = getattr(n, "blocks", []) or []
            for u in cls._iter_block_user_mentions(blocks):
                _record_user_dict(u)

            for child in getattr(n, "children", []):
                _scan_node(child)

        _scan_node(root_node)
        return list(users_map.values())

    @classmethod
    def detect_homonyms(
        cls,
        root_node: Any,
        workspace_users: Optional[List[Dict[str, Any]]] = None,
        user_mapper: Optional["UserMappingManager"] = None,
    ) -> List[Dict[str, Any]]:
        """
        同一表示名で複数の Notion user_id（または複数メールアドレス/アカウントID）が存在する
        同姓同名ユーザーを検知し、候補一覧およびツリー内の名前のみ参照箇所（影響箇所）を返す。
        """
        # name_key -> {identity_key: candidate_dict}
        candidates_by_name: Dict[str, Dict[str, Dict[str, str]]] = {}
        display_names: Dict[str, str] = {}

        def _add_candidate(uid: str = "", name: str = "", email: str = "", conf_id: str = ""):
            name_s = (name or "").strip()
            if not name_s:
                return
            uid_s = (uid or "").strip()
            email_s = (email or "").strip()
            conf_s = (conf_id or "").strip()
            ident_key = (
                (f"id:{uid_s.replace('-', '').lower()}" if uid_s else "")
                or (f"email:{email_s.lower()}" if email_s else "")
                or (f"conf:{conf_s}" if conf_s else "")
            )
            if not ident_key:
                return
            name_key = name_s.lower()
            display_names.setdefault(name_key, name_s)
            bucket = candidates_by_name.setdefault(name_key, {})
            if ident_key not in bucket:
                bucket[ident_key] = {
                    "id": uid_s,
                    "name": name_s,
                    "email": email_s,
                }
            else:
                if uid_s and not bucket[ident_key]["id"]:
                    bucket[ident_key]["id"] = uid_s
                if email_s and not bucket[ident_key]["email"]:
                    bucket[ident_key]["email"] = email_s

        # 1. /v1/users キャッシュから登録
        for wu in workspace_users or []:
            if not isinstance(wu, dict):
                continue
            p_obj = wu.get("person")
            w_email = p_obj.get("email", "") if isinstance(p_obj, dict) else ""
            _add_candidate(wu.get("id", ""), wu.get("name", ""), w_email)

        # 2. ツリー内のユーザーから登録
        if root_node is not None:
            for tu in cls.collect_users_from_tree(root_node):
                _add_candidate(tu.get("notion_id", ""), tu.get("notion_name", ""), tu.get("notion_email", ""))

        # 3. user_mapper 定義から登録
        if user_mapper and getattr(user_mapper, "mappings", None):
            for m in user_mapper.mappings:
                _add_candidate(m.notion_id, m.notion_name, m.notion_email, m.confluence_account_id)

        # 候補が2名以上ある表示名を同姓同名として抽出
        homonym_keys: Dict[str, List[Dict[str, str]]] = {
            nk: list(cands.values())
            for nk, cands in candidates_by_name.items()
            if len(cands) > 1
        }
        if not homonym_keys:
            return []

        # ツリー内の「名前のみ（ID・Email未解決）」参照箇所を収集
        affected_by_name: Dict[str, List[str]] = {nk: [] for nk in homonym_keys}

        def _check_unresolved(u: Any, location: str):
            if not isinstance(u, dict):
                return
            u_name = (u.get("name") or "").strip()
            if not u_name:
                return
            nk = u_name.lower()
            if nk not in homonym_keys:
                return
            u_id = (u.get("id") or "").strip()
            p_obj = u.get("person")
            u_email = (p_obj.get("email") or "").strip() if isinstance(p_obj, dict) else ""
            if not u_id and not u_email:
                if location not in affected_by_name[nk]:
                    affected_by_name[nk].append(location)

        def _scan_affected(n: Any):
            if n is None:
                return
            node_title = getattr(n, "title", "") or getattr(n, "id", "")
            _check_unresolved(getattr(n, "created_by", None), f"ページ '{node_title}' の作成者")
            _check_unresolved(getattr(n, "last_edited_by", None), f"ページ '{node_title}' の最終更新者")

            for c in getattr(n, "comments", []) or []:
                if isinstance(c, dict):
                    _check_unresolved(c.get("created_by"), f"ページ '{node_title}' のコメント")

            if hasattr(n, "properties") and isinstance(n.properties, dict):
                for p_name, p_val in n.properties.items():
                    if isinstance(p_val, dict) and p_val.get("type") in ("people", "created_by", "last_edited_by"):
                        loc = f"DB行 '{node_title}' の '{p_name}' 列"
                        for u in p_val.get("users", []):
                            _check_unresolved(u, loc)
                        if p_val.get("type") in ("created_by", "last_edited_by"):
                            _check_unresolved(p_val.get(p_val["type"]), loc)

            for u in cls._iter_block_user_mentions(getattr(n, "blocks", []) or []):
                _check_unresolved(u, f"ページ '{node_title}' の本文メンション")

            for child in getattr(n, "children", []):
                _scan_affected(child)

        if root_node is not None:
            _scan_affected(root_node)

        warnings: List[Dict[str, Any]] = []
        for nk, cands in homonym_keys.items():
            warnings.append({
                "name": display_names.get(nk, nk),
                "candidates": cands,
                "affected_locations": affected_by_name.get(nk, []),
            })
        return warnings

    @classmethod
    def export_template(
        cls,
        output_path: str,
        tree_node: Any,
        confluence_client: Optional[Any] = None,
    ) -> int:
        """
        ツリー内の全 Notion ユーザーを抽出し、Confluence API による自動候補検索を適用した
        確認・編集用マッピング CSV を出力する。
        """
        users = cls.collect_users_from_tree(tree_node)
        rows = []

        for u in users:
            n_name = u["notion_name"]
            n_email = u["notion_email"]
            n_id = u["notion_id"]
            conf_acc_id = ""
            conf_disp_name = ""

            # Confluence API が利用可能な場合、自動探索を実行
            if confluence_client and (n_name or n_email):
                search_query = n_email or n_name
                try:
                    safe_name = n_name.replace('\\', '\\\\').replace('"', '\\"')
                    safe_email = n_email.replace('\\', '\\\\').replace('"', '\\"')
                    cql = f'user.fullname ~ "{safe_name}"' if n_name else f'text ~ "{safe_email}"'
                    results = confluence_client.search_users(cql=cql)
                    if results:
                        best = results[0].get("user") or results[0]
                        conf_acc_id = best.get("accountId", "")
                        conf_disp_name = best.get("displayName", "")
                except Exception as e:
                    logger.debug(f"User search error for {search_query}: {e}")

            rows.append({
                "notion_name": n_name,
                "notion_email": n_email,
                "notion_id": n_id,
                "confluence_account_id": conf_acc_id,
                "confluence_display_name": conf_disp_name,
            })

        fieldnames = ["notion_name", "notion_email", "notion_id", "confluence_account_id", "confluence_display_name"]
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)

        logger.info(f"Exported {len(rows)} user mapping candidates to {output_path}")
        return len(rows)

