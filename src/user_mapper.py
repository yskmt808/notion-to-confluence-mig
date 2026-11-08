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
            self._by_name[m.notion_name.lower()] = m

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

    @classmethod
    def collect_users_from_tree(cls, root_node: Any) -> List[Dict[str, str]]:
        """PageNode ツリー全体から登場する Notion ユーザー（作成者、担当者、コメント発言者等）を収集する。"""
        users_map: Dict[str, Dict[str, str]] = {}

        def _record(uid: str = "", name: str = "", email: str = ""):
            key = (uid.replace("-", "").lower() if uid else "") or (email.lower() if email else "") or (name.lower() if name else "")
            if not key:
                return
            if key not in users_map:
                users_map[key] = {"notion_id": uid, "notion_name": name, "notion_email": email}
            else:
                if uid and not users_map[key]["notion_id"]:
                    users_map[key]["notion_id"] = uid
                if name and not users_map[key]["notion_name"]:
                    users_map[key]["notion_name"] = name
                if email and not users_map[key]["notion_email"]:
                    users_map[key]["notion_email"] = email

        def _scan_node(n: Any):
            # ページ作成者
            cb = getattr(n, "created_by", None) or (n.properties.get("created_by") if hasattr(n, "properties") else None)
            if isinstance(cb, dict):
                _record(cb.get("id", ""), cb.get("name", ""), cb.get("person", {}).get("email", ""))

            # ページコメント発言者
            comments = getattr(n, "comments", []) or []
            for c in comments:
                c_by = c.get("created_by") or {}
                if isinstance(c_by, dict):
                    _record(c_by.get("id", ""), c_by.get("name", ""), c_by.get("person", {}).get("email", ""))

            # データベース行の people プロパティ
            if hasattr(n, "properties") and isinstance(n.properties, dict):
                for p_name, p_val in n.properties.items():
                    if isinstance(p_val, dict) and p_val.get("type") == "people":
                        for u in p_val.get("users", []):
                            _record(u.get("id", ""), u.get("name", ""), u.get("person", {}).get("email", ""))

            for child in getattr(n, "children", []):
                _scan_node(child)

        _scan_node(root_node)
        return list(users_map.values())

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

