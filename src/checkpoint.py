"""
移行処理のチェックポイント保存・レジューム（Resume from Checkpoint）マネージャー。
各ノードのPass 1（作成）およびPass 2（リンク解決）の完了状態を逐次アトミックに永続化し、
障害や手動中断からの安全な再開を実現します。
"""

import os
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_PATH = "migration_checkpoint.json"


class MigrationCheckpoint:
    """移行進捗のチェックポイント状態を管理するクラス。"""

    def __init__(self, checkpoint_path: str = DEFAULT_CHECKPOINT_PATH):
        self.checkpoint_path = checkpoint_path
        self._lock = threading.RLock()
        self.session_id: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.last_updated_at: str = self.started_at
        self.status: str = "IN_PROGRESS"  # "IN_PROGRESS", "INTERRUPTED", "COMPLETED"
        self.source_info: Dict[str, Any] = {}
        self.space_key: str = ""
        self.parent_id: Optional[str] = None
        self.nodes: Dict[str, Dict[str, Any]] = {}

    def load(self) -> bool:
        """既存のチェックポイントファイルが存在する場合に読み込む。成功時 True を返す。"""
        with self._lock:
            if not os.path.exists(self.checkpoint_path):
                return False
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.session_id = data.get("session_id", self.session_id)
                self.started_at = data.get("started_at", self.started_at)
                self.last_updated_at = data.get("last_updated_at", self.last_updated_at)
                self.status = data.get("status", "IN_PROGRESS")
                self.source_info = data.get("source_info", {})
                self.space_key = data.get("space_key", "")
                self.parent_id = data.get("parent_id")
                self.nodes = data.get("nodes", {})
                logger.info(f"Loaded migration checkpoint from [{self.checkpoint_path}]: {len(self.nodes)} nodes recorded (Status: {self.status})")
                return True
            except Exception as e:
                logger.warning(f"Could not load checkpoint from [{self.checkpoint_path}]: {e}")
                return False

    def save(self):
        """現在のチェックポイント状態を一時ファイル経由で安全（アトミック）に保存する。"""
        with self._lock:
            self.last_updated_at = datetime.now(timezone.utc).isoformat()
            data = {
                "version": "1.0",
                "session_id": self.session_id,
                "started_at": self.started_at,
                "last_updated_at": self.last_updated_at,
                "status": self.status,
                "space_key": self.space_key,
                "parent_id": self.parent_id,
                "source_info": self.source_info,
                "stats": {
                    "total_recorded_nodes": len(self.nodes),
                    "pass1_completed": sum(1 for n in self.nodes.values() if n.get("pass1_done")),
                    "pass2_completed": sum(1 for n in self.nodes.values() if n.get("pass2_done")),
                    "databases_pending_indexing": sum(1 for n in self.nodes.values() if n.get("pending_row_views")),
                },
                "nodes": self.nodes,
            }

            tmp_path = f"{self.checkpoint_path}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                try:
                    os.chmod(tmp_path, 0o600)
                except Exception:
                    pass
                os.replace(tmp_path, self.checkpoint_path)
            except Exception as e:
                logger.warning(f"Failed to write checkpoint atomically: {e}")
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

    def is_pass1_done(self, node_id: str) -> bool:
        """指定ノードがPass 1（Confluenceページ/DB作成）を完了しているか判定。"""
        with self._lock:
            node_entry = self.nodes.get(node_id)
            return bool(node_entry and node_entry.get("pass1_done") and node_entry.get("confluence_id"))

    def is_pass2_done(self, node_id: str) -> bool:
        """指定ノードがPass 2（リンク解決・更新）を完了しているか判定。"""
        with self._lock:
            node_entry = self.nodes.get(node_id)
            return bool(node_entry and node_entry.get("pass2_done"))

    def record_pass1(
        self,
        node_id: str,
        confluence_id: str,
        title: str,
        url: str,
        node_type: str,
        views: Optional[Dict[str, str]] = None,
        views_info: Optional[list] = None,
        pending_row_views: Optional[List[str]] = None,
    ):
        """ノードのPass 1完了を記録し保存する。"""
        with self._lock:
            existing = self.nodes.get(node_id, {})
            existing.update({
                "notion_id": node_id,
                "confluence_id": str(confluence_id),
                "title": title,
                "url": url,
                "node_type": node_type,
                "pass1_done": True,
                "pass1_timestamp": datetime.now(timezone.utc).isoformat(),
            })
            if views:
                existing["views"] = views
            if views_info:
                existing["views_info"] = views_info
            if pending_row_views is not None:
                existing["pending_row_views"] = pending_row_views
            self.nodes[node_id] = existing
            self.save()

    def record_pass2(self, node_id: str):
        """ノードのPass 2完了を記録し保存する。"""
        with self._lock:
            if node_id in self.nodes:
                self.nodes[node_id]["pass2_done"] = True
                self.nodes[node_id]["pass2_timestamp"] = datetime.now(timezone.utc).isoformat()
                self.save()

    def record_pending_row_views(self, node_id: str, pending_child_ids: List[str]):
        """データベースノードに対する未完了の行ビュー解決子ページIDリストを記録する。"""
        with self._lock:
            if node_id in self.nodes:
                self.nodes[node_id]["pending_row_views"] = pending_child_ids
                self.save()

    def get_pending_row_views(self, node_id: str) -> List[str]:
        """指定データベースノードの未完了行ビュー子ページIDリストを取得する。"""
        with self._lock:
            node_entry = self.nodes.get(node_id, {})
            return list(node_entry.get("pending_row_views", []))

    def get_databases_with_pending_row_views(self) -> Dict[str, Dict[str, Any]]:
        """行ビュー解決が未完了の全データベースノード情報を返す。"""
        with self._lock:
            results = {}
            for nid, item in self.nodes.items():
                if item.get("node_type") == "database" and item.get("pending_row_views"):
                    results[nid] = item
            return results

    def mark_row_views_resolved(self, node_id: str, resolved_child_ids: List[str]):
        """解決済みの行ビュー子ページIDをリストから除去し、全て完了した場合は空にする。"""
        with self._lock:
            if node_id in self.nodes:
                current_pending = set(self.nodes[node_id].get("pending_row_views", []))
                current_pending.difference_update(resolved_child_ids)
                self.nodes[node_id]["pending_row_views"] = sorted(list(current_pending))
                self.save()

    def get_node_info(self, node_id: str) -> Optional[Dict[str, Any]]:
        """記録されているノード情報を返す。"""
        with self._lock:
            return self.nodes.get(node_id)

    def get_migration_map(self) -> Dict[str, Dict[str, Any]]:
        """ConfluenceImporterで利用可能な migration_map 形式の辞書を生成して返す。"""
        with self._lock:
            result = {}
            for nid, item in self.nodes.items():
                if item.get("pass1_done") and item.get("confluence_id"):
                    entry = {
                        "confluence_id": item["confluence_id"],
                        "title": item["title"],
                        "url": item["url"],
                        "node_type": item["node_type"],
                    }
                    if "views" in item:
                        entry["views"] = item["views"]
                    if "views_info" in item:
                        entry["views_info"] = item["views_info"]
                    if "pending_row_views" in item:
                        entry["pending_row_views"] = item["pending_row_views"]
                    result[nid] = entry
            return result

    def mark_interrupted(self):
        """中断状態を明示的に保存する。"""
        with self._lock:
            self.status = "INTERRUPTED"
            self.save()
            logger.info(f"Checkpoint status set to INTERRUPTED ({len(self.nodes)} nodes saved).")

    def mark_completed(self):
        """完了状態を明示的に保存する。"""
        with self._lock:
            self.status = "COMPLETED"
            self.save()
            logger.info(f"Checkpoint status set to COMPLETED ({len(self.nodes)} nodes saved).")

    def remove_checkpoint(self):
        """完了後などにチェックポイントファイルをクリーンアップする。"""
        with self._lock:
            if os.path.exists(self.checkpoint_path):
                try:
                    os.remove(self.checkpoint_path)
                    logger.info(f"Removed completed checkpoint file: {self.checkpoint_path}")
                except Exception as e:
                    logger.warning(f"Could not remove checkpoint file: {e}")

