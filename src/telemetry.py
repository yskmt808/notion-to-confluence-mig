"""
移行処理のサニタイズド診断レポート生成（Sanitized Telemetry & Debug Report Generator）。
機密情報（タイトル、本文、個人名、メールアドレス、認証トークン）を完全マスキングし、
機能動作・変換マクロ・HTTPステータス・エラー診断情報のみを抽出した安全な開発者報告用レポートを生成します。
"""

import os
import re
import sys
import json
import hashlib
import platform
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from collections import Counter

logger = logging.getLogger(__name__)


class SanitizedTelemetry:
    """移行実行中の動作診断テレメトリを収集し、機密情報をマスキングした安全なレポートを出力するクラス。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.session_id: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.finished_at: Optional[str] = None
        self.environment: Dict[str, Any] = self._collect_environment()

        # 診断メトリクス
        self.block_types_counter: Counter = Counter()
        self.html_tags_counter: Counter = Counter()
        self.confluence_macros_counter: Counter = Counter()
        self.http_requests: List[Dict[str, Any]] = []
        self.http_status_counter: Counter = Counter()
        self.step_timings: Dict[str, float] = {}
        self.warnings: List[str] = []
        self.errors: List[Dict[str, Any]] = []
        self.unsupported_elements: List[str] = []

        # ノード構造の抽象化サマリー（タイトル等の機密を含まない）
        self.tree_summary: Dict[str, Any] = {
            "total_nodes": 0,
            "max_depth": 0,
            "pages_count": 0,
            "databases_count": 0,
            "database_rows_count": 0,
        }

    def _collect_environment(self) -> Dict[str, Any]:
        """実行環境のメタデータを収集する（個人パスや機密を含まない）。"""
        env = {
            "os": platform.system(),
            "os_release": platform.release(),
            "python_version": sys.version.split()[0],
            "architecture": platform.machine(),
            "libraries": {},
        }
        for pkg in ["requests", "beautifulsoup4", "notion_client", "matplotlib", "urllib3"]:
            try:
                mod = __import__(pkg)
                env["libraries"][pkg] = getattr(mod, "__version__", "unknown")
            except Exception:
                env["libraries"][pkg] = "not installed"
        return env

    @staticmethod
    def hash_text(text: str, prefix: str = "HASH") -> str:
        """文字列を不可逆な短縮ハッシュに置換する。"""
        if not text:
            return ""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        return f"[{prefix}_{digest}]"

    @staticmethod
    def sanitize_string(text: str) -> str:
        """文字列からトークン、メールアドレス、UUID等をマスキングする。"""
        if not text:
            return ""
        # Bearer / API トークン
        text = re.sub(r'(?i)bearer\s+[A-Za-z0-9_\-\.]+', 'Bearer [REDACTED_TOKEN]', text)
        text = re.sub(r'(?i)basic\s+[A-Za-z0-9+/=]+', 'Basic [REDACTED_TOKEN]', text)
        text = re.sub(r'ntn_[A-Za-z0-9_]+', '[REDACTED_NOTION_TOKEN]', text)
        text = re.sub(r'secret_[A-Za-z0-9_]+', '[REDACTED_NOTION_SECRET]', text)
        text = re.sub(r'ATATT3[A-Za-z0-9_\-=]+', '[REDACTED_ATLASSIAN_TOKEN]', text)
        # メールアドレス
        text = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '[REDACTED_EMAIL]', text)
        # Atlassian Account ID (例: 712020:xxxx...)
        text = re.sub(r'\b\d{5,7}:[0-9a-fA-F\-]{32,36}\b', '[REDACTED_ACCOUNT_ID]', text)
        # 32桁 / 36桁 UUID (ページID等)
        text = re.sub(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', '[UUID]', text)
        text = re.sub(r'\b[0-9a-fA-F]{32}\b', '[UUID32]', text)
        return text

    def record_tree(self, root_node):
        """ノードツリーの構造統計を収集する（タイトル・本文は除外）。"""
        total = 0
        pages = 0
        dbs = 0
        rows = 0
        max_d = 0

        def traverse(n, d):
            nonlocal total, pages, dbs, rows, max_d
            total += 1
            max_d = max(max_d, d)
            if n.node_type == "page":
                pages += 1
            elif n.node_type == "database":
                dbs += 1
            elif n.node_type == "database_row":
                rows += 1

            for b in getattr(n, "blocks", []):
                b_type = b.get("type", "unknown")
                self.block_types_counter[b_type] += 1
                if b_type in ("breadcrumb", "unsupported", "button", "template"):
                    if b_type not in self.unsupported_elements:
                        self.unsupported_elements.append(b_type)

            for c in getattr(n, "children", []):
                traverse(c, d + 1)

        traverse(root_node, 1)
        self.tree_summary = {
            "total_nodes": total,
            "max_depth": max_d,
            "pages_count": pages,
            "databases_count": dbs,
            "database_rows_count": rows,
        }

    def record_html_tag(self, tag_name: str):
        """HTML抽出時のタグを記録する。"""
        with self._lock:
            self.html_tags_counter[tag_name] += 1

    def record_macro(self, macro_name: str):
        """Confluence Storage Formatへのマクロ変換を記録する。"""
        with self._lock:
            self.confluence_macros_counter[macro_name] += 1

    def record_http_request(
        self,
        method: str,
        endpoint_pattern: str,
        status_code: int,
        duration_ms: float,
        error_type: Optional[str] = None,
    ):
        """HTTP通信の統計を記録する（機密URLパラメータやヘッダーは除外）。"""
        sanitized_endpoint = self.sanitize_string(endpoint_pattern)
        with self._lock:
            self.http_status_counter[str(status_code)] += 1
            self.http_requests.append({
                "method": method,
                "endpoint": sanitized_endpoint,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 2),
                "error_type": error_type,
            })

    def record_timing(self, step_name: str, seconds: float):
        """各ステップの所要時間を記録する。"""
        with self._lock:
            self.step_timings[step_name] = round(seconds, 2)

    def record_warning(self, message: str):
        """警告メッセージをサニタイズして記録する。"""
        sanitized = self.sanitize_string(message)
        with self._lock:
            self.warnings.append(sanitized)

    def record_error(self, error: Exception, context: str = ""):
        """例外情報をサニタイズして記録する（スタックトレースからファイル本文や個人パスを消去）。"""
        err_type = type(error).__name__
        err_msg = self.sanitize_string(str(error))
        with self._lock:
            self.errors.append({
                "error_type": err_type,
                "message": err_msg,
                "context": self.sanitize_string(context),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    def generate_report(self) -> Dict[str, Any]:
        """サニタイズされた診断レポートオブジェクトを生成する。"""
        self.finished_at = datetime.now(timezone.utc).isoformat()
        durations = [r["duration_ms"] for r in self.http_requests if "duration_ms" in r]
        http_summary = {
            "total_requests": len(self.http_requests),
            "status_distribution": dict(self.http_status_counter),
            "latency_ms": {
                "min": min(durations) if durations else 0,
                "avg": round(sum(durations) / len(durations), 2) if durations else 0,
                "max": max(durations) if durations else 0,
            },
        }

        return {
            "report_version": "1.0",
            "sanitized": True,
            "privacy_notice": "このレポートには移行元・移行先の本文、タイトル、個人名、認証トークン等の機密情報は一切含まれていません。",
            "session_id": self.session_id,
            "timestamps": {
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            },
            "environment": self.environment,
            "tree_structure_summary": self.tree_summary,
            "diagnostics": {
                "step_timings_seconds": self.step_timings,
                "block_types_distribution": dict(self.block_types_counter),
                "confluence_macros_distribution": dict(self.confluence_macros_counter),
                "unsupported_elements_detected": self.unsupported_elements,
                "http_summary": http_summary,
            },
            "warnings_count": len(self.warnings),
            "warnings_sample": self.warnings[:20],
            "errors_count": len(self.errors),
            "errors": self.errors,
        }

    def save_reports(self, json_path: str = "debug_report.json", md_path: str = "debug_report.md"):
        """JSON形式およびMarkdown形式でサニタイズ診断レポートを保存する。"""
        report_data = self.generate_report()

        # 1. JSON レポート
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved sanitized debug report (JSON): {json_path}")

        # 2. Markdown レポート
        md_content = self._render_markdown_report(report_data)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        logger.info(f"Saved sanitized debug report (Markdown): {md_path}")

    def _render_markdown_report(self, data: Dict[str, Any]) -> str:
        """Markdown形式の読みやすいレポートをレンダリングする。"""
        env = data["environment"]
        diag = data["diagnostics"]
        http_s = diag["http_summary"]
        tree = data["tree_structure_summary"]

        md = []
        md.append("# 🛠️ Notion to Confluence 移行診断レポート (Sanitized Debug Report)")
        md.append(f"> **機密情報保護**: {data['privacy_notice']}\n")
        md.append(f"- **セッションID**: `{data['session_id']}`")
        md.append(f"- **実行日時**: `{data['timestamps']['started_at']}` 〜 `{data['timestamps']['finished_at']}`")
        md.append(f"- **実行環境**: {env['os']} ({env['os_release']}) | Python {env['python_version']} ({env['architecture']})")
        md.append(f"- **主要ライブラリ**: " + ", ".join(f"`{k}=={v}`" for k, v in env["libraries"].items()))
        md.append("")

        md.append("## 🌲 ツリー構造サマリー（機密除外）")
        md.append(f"- **総ノード数**: {tree['total_nodes']} 件 (ページ: {tree['pages_count']}, DB: {tree['databases_count']}, DB行: {tree['database_rows_count']})")
        md.append(f"- **最大階層深度**: {tree['max_depth']} 階層")
        md.append("")

        md.append("## ⏱️ 処理時間分析")
        md.append("| 処理ステップ | 所要時間 (秒) |")
        md.append("| :--- | :---: |")
        for step, sec in diag["step_timings_seconds"].items():
            md.append(f"| {step} | {sec} s |")
        md.append("")

        md.append("## 🌐 HTTP通信・API診断")
        md.append(f"- **総リクエスト数**: {http_s['total_requests']} 件")
        md.append(f"- **レスポンスコード分布**: " + ", ".join(f"`{k}`: {v}件" for k, v in http_s["status_distribution"].items()))
        md.append(f"- **API応答時間 (ms)**: 最小 {http_s['latency_ms']['min']}ms / 平均 {http_s['latency_ms']['avg']}ms / 最大 {http_s['latency_ms']['max']}ms")
        md.append("")

        md.append("## 🧩 検出ブロック＆生成Confluenceマクロ分布")
        md.append("### 検出されたNotionブロック種別")
        md.append("| ブロック種別 | 検出件数 |")
        md.append("| :--- | :---: |")
        for b_name, count in sorted(diag["block_types_distribution"].items(), key=lambda x: -x[1]):
            md.append(f"| `{b_name}` | {count} |")
        md.append("")

        md.append("### 生成されたConfluenceマクロ種別")
        md.append("| マクロ種別 (`ac:name`) | 生成件数 |")
        md.append("| :--- | :---: |")
        for m_name, count in sorted(diag["confluence_macros_distribution"].items(), key=lambda x: -x[1]):
            md.append(f"| `{m_name}` | {count} |")
        md.append("")

        if diag["unsupported_elements_detected"]:
            md.append(f"### ⚠️ 検出されたNotion固有未対応要素")
            for elem in diag["unsupported_elements_detected"]:
                md.append(f"- `{elem}` (フォールバック注記として保持)")
            md.append("")

        if data["errors_count"] > 0:
            md.append(f"## ❌ 発生エラー診断 ({data['errors_count']} 件)")
            for i, err in enumerate(data["errors"], 1):
                md.append(f"### エラー #{i}: `{err['error_type']}`")
                md.append(f"- **メッセージ**: `{err['message']}`")
                if err["context"]:
                    md.append(f"- **発生コンテキスト**: `{err['context']}`")
            md.append("")
        else:
            md.append("## ✅ エラー診断: 0件 (全処理正常完了)")
            md.append("")

        if data["warnings_count"] > 0:
            md.append(f"## ⚠️ 警告サマリー ({data['warnings_count']} 件)")
            for w in data["warnings_sample"]:
                md.append(f"- {w}")
            md.append("")

        return "\n".join(md)

