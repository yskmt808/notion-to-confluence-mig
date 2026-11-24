#!/usr/bin/env python3
"""
移行計画作成用の Notion ページツリー調査ツール。

- API 調査モード (--api): POST /v1/search + レートリミット制御 + キャッシュ再開で最小 API コールでツリーを構築
- ZIP 分析モード (--zip / --zip-dir): Notion HTML エクスポート ZIP から API コールなしでツリーを構築
- 出力 (方式B): <output-dir>/pages.csv (1ページ1行のUTF-8-BOM CSV) と <output-dir>/summary.md
"""

import argparse
import glob
import logging
import os
import sys
import time
from typing import List

from dotenv import load_dotenv

from src.tree_planner import (
    ApiTreeScanner,
    build_summary_markdown,
    export_pages_csv,
    export_summary_markdown,
    flatten_plan_trees,
    scan_zips_to_plan_trees,
)


def parse_args(argv: List[str] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="移行計画作成用のNotionページツリー調査ツール (1ページ1行CSV + Markdownサマリー出力)",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--api",
        action="store_true",
        help="Notion API (POST /v1/search) を使用してツリーを調査するモード",
    )
    mode_group.add_argument(
        "--zip",
        nargs="+",
        dest="zip_paths",
        help="Notion HTMLエクスポートZIPファイルのパス（複数指定可）",
    )
    mode_group.add_argument(
        "--zip-dir",
        help="複数のNotion HTMLエクスポートZIPが格納されたディレクトリ",
    )

    parser.add_argument(
        "--notion-token",
        default="",
        help="Notion Integration Token（未指定時は環境変数 NOTION_TOKEN / NOTION_API_KEY を参照）",
    )
    parser.add_argument(
        "--root-page-id",
        default="",
        help="(--api時) 調査対象とするルートページ/DBのUUID（未指定時はアクセス可能な全体）",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=2.8,
        help="(--api時) 1秒あたりの最大APIリクエスト数 (デフォルト: 2.8)",
    )
    parser.add_argument(
        "--cache-file",
        default="",
        help="(--api時) 検索結果の中間キャッシュJSONパス (デフォルト: <output-dir>/api_search_cache.json)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="(--api時) キャッシュファイルから中断箇所を再開する",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/tree_plan",
        help="pages.csv と summary.md の出力先ディレクトリ (デフォルト: reports/tree_plan)",
    )
    parser.add_argument(
        "--exclude-db-rows",
        action="store_true",
        help="pages.csv の明細行から DB行レコード (database_row) を除外する（サマリーとdb_row_countは維持）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="詳細ログを出力する",
    )
    return parser.parse_args(argv)


def main(argv: List[str] = None) -> int:
    load_dotenv()
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    t0 = time.monotonic()
    api_calls = 0

    if args.api:
        token = args.notion_token or os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY") or ""
        if not token:
            print("エラー: --api モードには --notion-token または環境変数 NOTION_TOKEN が必要です。", file=sys.stderr)
            return 1
        cache_path = args.cache_file or os.path.join(args.output_dir, "api_search_cache.json")
        scanner = ApiTreeScanner(
            token=token,
            rate_limit=args.rate_limit,
            cache_file=cache_path,
            resume=args.resume,
        )
        print("🔍 Notion API (POST /v1/search) でページツリーを探索中...")
        roots = scanner.scan(
            root_page_id=args.root_page_id or None,
            progress_cb=lambda cnt: print(f"  ... {cnt} 件取得済み (API呼出: {scanner.api_calls}回)", flush=True),
        )
        api_calls = scanner.api_calls
        source_label = "Notion API (POST /v1/search)"
        if args.root_page_id:
            source_label += f" [root={args.root_page_id}]"
    else:
        zip_files: List[str] = []
        if args.zip_paths:
            zip_files = list(args.zip_paths)
        elif args.zip_dir:
            zip_files = sorted(glob.glob(os.path.join(args.zip_dir, "*.zip")))
            if not zip_files:
                print(f"エラー: ディレクトリ '{args.zip_dir}' にZIPファイルが見つかりません。", file=sys.stderr)
                return 1
        print(f"📦 エクスポートZIP ({len(zip_files)}件) からページツリーを解析中...")
        roots = scan_zips_to_plan_trees(zip_files)
        source_label = f"Export ZIP x {len(zip_files)} ({os.path.basename(zip_files[0])})"

    include_db_rows = not args.exclude_db_rows
    rows = flatten_plan_trees(roots, include_db_rows=include_db_rows)
    elapsed = time.monotonic() - t0

    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = os.path.join(args.output_dir, "pages.csv")
    md_path = os.path.join(args.output_dir, "summary.md")

    export_pages_csv(rows, csv_path)
    summary_md = build_summary_markdown(
        roots,
        rows,
        source_label=source_label,
        api_calls=api_calls,
        elapsed_seconds=elapsed,
    )
    export_summary_markdown(summary_md, md_path)

    print("")
    print(summary_md)
    print(f"✅ 出力完了:")
    print(f"   - 明細CSV (1ページ1行): {csv_path}")
    print(f"   - サマリー (Markdown)  : {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
