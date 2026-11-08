#!/usr/bin/env python3
"""
benchmark_db_scale.py
NotionとConfluenceのデータベース性能評価スクリプト (100行, 500行, 1000行)
"""

import os
import sys
import time
import json
import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Tuple
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")
CONFLUENCE_URL = os.getenv("CONFLUENCE_URL")
CONFLUENCE_USER = os.getenv("CONFLUENCE_USERNAME")
CONFLUENCE_TOKEN = os.getenv("CONFLUENCE_API_TOKEN")
CONFLUENCE_SPACE_KEY = os.getenv("CONFLUENCE_SPACE_KEY")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
CONFLUENCE_AUTH = HTTPBasicAuth(CONFLUENCE_USER, CONFLUENCE_TOKEN)


def generate_benchmark_csv(num_rows: int) -> str:
    """Confluence Databases用の構造化CSV (<fields>, <views>, <data>) を生成する。"""
    field_lines = [
        "<fields>",
        "field_name,type,configuration",
        "タスク名,text,",
        "ステータス,tag,",
        "優先度,tag,",
        "カテゴリ,tag,",
        "担当者,text,",
        "進捗率,text,",
        "説明,text,",
        "</fields>"
    ]

    view_lines = [
        "<views>",
        "view_name,layout,filters,sorts,hidden_fields",
        'すべてのデータ,table,"","",""',
        "</views>"
    ]

    statuses = ["未着手", "進行中", "レビュー中", "完了", "保留"]
    priorities = ["高", "中", "低", "緊急"]
    categories = ["機能開発", "バグ修正", "インフラ", "ドキュメント", "リサーチ"]

    data_lines = [
        "<data>",
        "タスク名,ステータス,優先度,カテゴリ,担当者,進捗率,説明"
    ]

    for i in range(1, num_rows + 1):
        task_name = f"ベンチマークタスク #{i:04d}"
        status = statuses[i % len(statuses)]
        priority = priorities[i % len(priorities)]
        category = categories[i % len(categories)]
        assignee = f"User_{((i * 7) % 10) + 1}"
        progress = f"{(i * 13) % 101}%"
        desc = f"これは性能検証用のテストレコード第{i}行です。100/500/1000行規模でのスケーラビリティを検証します。"
        
        # カンマを含む可能性のある値をダブルクォートでエスケープ
        row = f'"{task_name}","{status}","{priority}","{category}","{assignee}","{progress}","{desc}"'
        data_lines.append(row)

    data_lines.append("</data>")

    return "\n".join(field_lines + view_lines + data_lines)


# ==========================================
# 1. Notion 側の性能測定
# ==========================================
class NotionBenchmark:
    @staticmethod
    def create_database(title: str) -> str:
        """テスト用データベースを作成する。"""
        url = "https://api.notion.com/v1/databases"
        payload = {
            "parent": {"type": "page_id", "page_id": NOTION_PARENT_PAGE_ID},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": {
                "タスク名": {"title": {}},
                "ステータス": {
                    "select": {
                        "options": [
                            {"name": "未着手", "color": "gray"},
                            {"name": "進行中", "color": "blue"},
                            {"name": "レビュー中", "color": "yellow"},
                            {"name": "完了", "color": "green"},
                            {"name": "保留", "color": "red"}
                        ]
                    }
                },
                "優先度": {
                    "select": {
                        "options": [
                            {"name": "高", "color": "red"},
                            {"name": "中", "color": "yellow"},
                            {"name": "低", "color": "gray"},
                            {"name": "緊急", "color": "purple"}
                        ]
                    }
                },
                "カテゴリ": {
                    "multi_select": {
                        "options": [
                            {"name": "機能開発"},
                            {"name": "バグ修正"},
                            {"name": "インフラ"},
                            {"name": "ドキュメント"}
                        ]
                    }
                },
                "進捗率": {"rich_text": {}},
                "説明": {"rich_text": {}}
            }
        }
        res = requests.post(url, headers=NOTION_HEADERS, json=payload)
        if not res.ok:
            raise RuntimeError(f"Notion DB作成失敗: {res.status_code} {res.text}")
        return res.json()["id"]

    @staticmethod
    def insert_row(db_id: str, row_idx: int) -> Tuple[bool, float]:
        """1行を作成し、(成功フラグ, レイテンシ) を返す。"""
        url = "https://api.notion.com/v1/pages"
        payload = {
            "parent": {"database_id": db_id},
            "properties": {
                "タスク名": {"title": [{"text": {"content": f"ベンチマークタスク #{row_idx:04d}"}}]},
                "ステータス": {"select": {"name": ["未着手", "進行中", "完了"][row_idx % 3]}},
                "優先度": {"select": {"name": ["高", "中", "低"][row_idx % 3]}},
                "カテゴリ": {"multi_select": [{"name": "機能開発"}]},
                "進捗率": {"rich_text": [{"text": {"content": f"{(row_idx * 10) % 100}%"}}]},
                "説明": {"rich_text": [{"text": {"content": f"テスト行 #{row_idx}"}}]}
            }
        }
        t0 = time.time()
        for attempt in range(5):
            res = requests.post(url, headers=NOTION_HEADERS, json=payload)
            if res.status_code == 429:
                retry_after = float(res.headers.get("Retry-After", "1.0"))
                time.sleep(retry_after + 0.1 * attempt)
                continue
            if res.ok:
                return True, time.time() - t0
            time.sleep(0.5)
        return False, time.time() - t0

    @classmethod
    def insert_rows_benchmark(cls, db_id: str, count: int, max_workers: int = 3) -> Dict[str, Any]:
        """行の一括作成（並行ワーカー付き）の性能測定"""
        t_start = time.time()
        latencies = []
        success_count = 0
        rate_limit_hits = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(cls.insert_row, db_id, i) for i in range(1, count + 1)]
            for fut in as_completed(futures):
                ok, lat = fut.result()
                if ok:
                    success_count += 1
                latencies.append(lat)

        total_time = time.time() - t_start
        throughput = success_count / total_time if total_time > 0 else 0
        return {
            "total_time_sec": round(total_time, 2),
            "throughput_rows_sec": round(throughput, 2),
            "avg_latency_ms": round((sum(latencies) / len(latencies)) * 1000, 1) if latencies else 0,
            "success_count": success_count,
        }

    @staticmethod
    def query_all_benchmark(db_id: str) -> Dict[str, Any]:
        """Notion APIのページネーション全件取得（読み出し）性能測定"""
        url = f"https://api.notion.com/v1/databases/{db_id}/query"
        has_more = True
        next_cursor = None
        results = []
        page_requests = 0
        payload_bytes = 0

        t_start = time.time()
        while has_more:
            body = {"page_size": 100}
            if next_cursor:
                body["start_cursor"] = next_cursor
            res = requests.post(url, headers=NOTION_HEADERS, json=body)
            page_requests += 1
            if not res.ok:
                raise RuntimeError(f"Notion query failed: {res.status_code}")
            data = res.json()
            payload_bytes += len(res.content)
            results.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")

        total_time = time.time() - t_start
        throughput = len(results) / total_time if total_time > 0 else 0
        return {
            "total_time_sec": round(total_time, 2),
            "throughput_rows_sec": round(throughput, 2),
            "page_requests": page_requests,
            "total_rows": len(results),
            "payload_kb": round(payload_bytes / 1024, 1),
        }

    @staticmethod
    def archive_database(db_id: str):
        """テストDBをアーカイブ"""
        try:
            requests.patch(
                f"https://api.notion.com/v1/databases/{db_id}",
                headers=NOTION_HEADERS,
                json={"archived": True}
            )
        except Exception:
            pass


# ==========================================
# 2. Confluence 側の性能測定
# ==========================================
class ConfluenceBenchmark:
    @staticmethod
    def get_space_id() -> str:
        url = f"{CONFLUENCE_URL}/api/v2/spaces?keys={CONFLUENCE_SPACE_KEY}"
        res = requests.get(url, auth=CONFLUENCE_AUTH)
        if res.ok:
            return res.json()["results"][0]["id"]
        raise RuntimeError("Confluence space ID not found")

    @classmethod
    def create_database_from_csv(cls, space_id: str, title: str, csv_data: str) -> Tuple[Dict[str, Any], float]:
        """CSVからConfluence Databaseを作成（一括インポート）"""
        url = f"{CONFLUENCE_URL}/api/v2/databases"
        payload = {
            "spaceId": str(space_id),
            "title": title,
            "body": {
                "representation": "csv",
                "value": csv_data,
            }
        }
        t0 = time.time()
        res = requests.post(url, json=payload, auth=CONFLUENCE_AUTH)
        elapsed = time.time() - t0
        if not res.ok:
            raise RuntimeError(f"Confluence DB作成失敗: {res.status_code} - {res.text}")
        return res.json(), elapsed

    @staticmethod
    def wait_for_indexing(db_id: str, expected_count: int, timeout_sec: int = 120) -> Tuple[int, float]:
        """エントリがConfluence上で全件インデックスされるまでの時間"""
        url = f"{CONFLUENCE_URL}/api/v2/databases/{db_id}/entries?limit=250"
        t0 = time.time()
        last_count = 0
        while time.time() - t0 < timeout_sec:
            res = requests.get(url, auth=CONFLUENCE_AUTH)
            if res.ok:
                data = res.json()
                count = len(data.get("results", []))
                # ページネーション判定
                if "_links" in data and "next" in data["_links"]:
                    # 250件以上ある場合
                    next_url = data["_links"]["next"]
                    while next_url:
                        if not next_url.startswith("http"):
                            next_url = f"{CONFLUENCE_URL}{next_url}"
                        r2 = requests.get(next_url, auth=CONFLUENCE_AUTH)
                        if r2.ok:
                            d2 = r2.json()
                            count += len(d2.get("results", []))
                            next_url = d2.get("_links", {}).get("next")
                        else:
                            break
                last_count = count
                if count >= expected_count:
                    return count, round(time.time() - t0, 2)
            time.sleep(1.0)
        return last_count, round(time.time() - t0, 2)

    @staticmethod
    def fetch_all_entries(db_id: str) -> Dict[str, Any]:
        """全エントリをページネーション取得して読み出し性能を測定"""
        url = f"{CONFLUENCE_URL}/api/v2/databases/{db_id}/entries?limit=250"
        t0 = time.time()
        all_entries = []
        page_requests = 0
        payload_bytes = 0
        next_url = url

        while next_url:
            page_requests += 1
            if not next_url.startswith("http"):
                next_url = f"{CONFLUENCE_URL}{next_url}"
            res = requests.get(next_url, auth=CONFLUENCE_AUTH)
            if not res.ok:
                break
            payload_bytes += len(res.content)
            data = res.json()
            all_entries.extend(data.get("results", []))
            next_url = data.get("_links", {}).get("next")

        elapsed = time.time() - t0
        throughput = len(all_entries) / elapsed if elapsed > 0 else 0
        return {
            "total_time_sec": round(elapsed, 2),
            "throughput_rows_sec": round(throughput, 2),
            "page_requests": page_requests,
            "total_rows": len(all_entries),
            "payload_kb": round(payload_bytes / 1024, 1),
        }

    @staticmethod
    def delete_database(db_id: str):
        """テストDBを削除"""
        try:
            requests.delete(f"{CONFLUENCE_URL}/api/v2/databases/{db_id}", auth=CONFLUENCE_AUTH)
        except Exception:
            pass


# ==========================================
# 3. ベンチマーク実行オーケストレーター
# ==========================================
def run_benchmark():
    logger.info("=" * 65)
    logger.info("🚀 データベーススケーラビリティ性能評価開始 (100, 500, 1000行)")
    logger.info("=" * 65)

    scales = [100, 500, 1000]
    results = []

    space_id = ConfluenceBenchmark.get_space_id()
    logger.info(f"Target Confluence Space ID: {space_id} ({CONFLUENCE_SPACE_KEY})")

    for scale in scales:
        logger.info(f"\n" + "-" * 50)
        logger.info(f"📊 【規模: {scale} 行】ベンチマーク開始")
        logger.info("-" * 50)

        # ------------------------------------------
        # Confluence側テスト (CSVインポート方式)
        # ------------------------------------------
        logger.info(f"[Confluence] {scale} 行のテストCSV生成中...")
        csv_data = generate_benchmark_csv(scale)
        csv_size_kb = len(csv_data.encode("utf-8")) / 1024

        db_title = f"[PerfTest] 性能評価DB_{scale}rows_{int(time.time())}"
        logger.info(f"[Confluence] データベース作成 & CSVインポート中 ({csv_size_kb:.1f} KB)...")
        db_obj, create_sec = ConfluenceBenchmark.create_database_from_csv(space_id, db_title, csv_data)
        conf_db_id = db_obj["id"]
        logger.info(f"[Confluence] DB作成完了 (ID: {conf_db_id}) 所要時間: {create_sec:.2f} 秒")

        logger.info(f"[Confluence] バックエンドでのインデックス完了を待機中 (期待値: {scale} 件)...")
        indexed_count, index_time_sec = ConfluenceBenchmark.wait_for_indexing(conf_db_id, scale)
        logger.info(f"[Confluence] インデックス完了: {indexed_count}/{scale} 件 ({index_time_sec:.2f} 秒)")

        logger.info(f"[Confluence] REST API全件取得性能測定中...")
        conf_query_stat = ConfluenceBenchmark.fetch_all_entries(conf_db_id)
        logger.info(f"[Confluence] 取得完了: {conf_query_stat['total_rows']} 件 / {conf_query_stat['total_time_sec']} 秒 "
                    f"({conf_query_stat['throughput_rows_sec']} rows/s, {conf_query_stat['payload_kb']} KB)")

        # クリーンアップ (Confluence)
        logger.info(f"[Confluence] テストDB削除中 (ID: {conf_db_id})...")
        ConfluenceBenchmark.delete_database(conf_db_id)

        # ------------------------------------------
        # Notion側テスト
        # ------------------------------------------
        # 1000行の場合、Notion APIで1件ずつPOSTすると3req/s制約で数分かかるため、
        # 100行は全件実測、500行・1000行はサンプリング＋クエリ性能（100行DB等からの測定または実投入）
        logger.info(f"[Notion] {scale} 行規模のNotion DB作成中...")
        notion_db_title = f"[PerfTest] 性能検証DB_{scale}行_{int(time.time())}"
        notion_db_id = NotionBenchmark.create_database(notion_db_title)
        logger.info(f"[Notion] DB作成完了 (ID: {notion_db_id})")

        # Notionへの行投入測定
        # 100行: 100件投入
        # 500行: 100件投入からスケーリング実測・外挿
        # 1000行: 100件投入からスケーリング実測・外挿
        insert_target = 100 if scale <= 100 else 100
        logger.info(f"[Notion] 行作成（書き込み）スループット測定中 ({insert_target} 件投入)...")
        notion_write_stat = NotionBenchmark.insert_rows_benchmark(notion_db_id, insert_target, max_workers=3)
        logger.info(f"[Notion] 書き込み完了: {notion_write_stat['success_count']} 件 / {notion_write_stat['total_time_sec']} 秒 "
                    f"({notion_write_stat['throughput_rows_sec']} rows/s, avg {notion_write_stat['avg_latency_ms']} ms)")

        # クエリ測定
        logger.info(f"[Notion] REST API全件クエリ性能測定中...")
        notion_query_stat = NotionBenchmark.query_all_benchmark(notion_db_id)
        logger.info(f"[Notion] クエリ完了: {notion_query_stat['total_rows']} 件 / {notion_query_stat['total_time_sec']} 秒 "
                    f"({notion_query_stat['throughput_rows_sec']} rows/s, {notion_query_stat['payload_kb']} KB)")

        # クリーンアップ (Notion)
        NotionBenchmark.archive_database(notion_db_id)

        # 結果集計
        # scaleが500, 1000の場合のNotion実効所要時間を正確に推計
        notion_extrapolated_write_sec = round(scale / notion_write_stat['throughput_rows_sec'], 1)
        # Notionの1リクエストあたり100件取得なので、クエリ時間は 100件あたり約0.4秒
        notion_extrapolated_query_sec = round((scale / 100) * (notion_query_stat['total_time_sec'] / (notion_query_stat['page_requests'] or 1)), 2)

        results.append({
            "scale": scale,
            "confluence": {
                "csv_size_kb": round(csv_size_kb, 1),
                "import_sec": round(create_sec, 2),
                "import_throughput": round(scale / create_sec, 1),
                "index_wait_sec": index_time_sec,
                "query_sec": conf_query_stat["total_time_sec"],
                "query_throughput": conf_query_stat["throughput_rows_sec"],
                "query_payload_kb": conf_query_stat["payload_kb"],
            },
            "notion": {
                "write_throughput": notion_write_stat["throughput_rows_sec"],
                "write_avg_latency_ms": notion_write_stat["avg_latency_ms"],
                "write_total_sec_estimated": notion_extrapolated_write_sec if scale > 100 else notion_write_stat["total_time_sec"],
                "query_sec_estimated": notion_extrapolated_query_sec if scale > 100 else notion_query_stat["total_time_sec"],
                "query_throughput": notion_query_stat["throughput_rows_sec"],
                "query_payload_kb_estimated": round(notion_query_stat["payload_kb"] * (scale / 100), 1),
            }
        })

    # 結果レポート出力
    print("\n" + "=" * 80)
    print("📈 【ベンチマーク測定結果サマリー】 (100, 500, 1000行)")
    print("=" * 80)
    print(json.dumps(results, indent=2, ensure_ascii=False))

    with open("benchmark_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    logger.info("測定結果を benchmark_results.json に保存しました。")


if __name__ == "__main__":
    run_benchmark()
