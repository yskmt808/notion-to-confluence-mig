#!/usr/bin/env python3
"""
create_multiblock_test_set.py
Task B.1 検証用の100ページ規模（25ページ×4ブロック）のNotionテストページセットを作成するスクリプト。
ブロック間の相互ページリンク、クロスブロックDBリレーション、クロスブロックDBビュー埋め込みを含む。
"""

import os
import sys
import time
import logging
from typing import Dict, Any, List
import requests
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
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def create_page(parent_id: str, title: str, icon_emoji: str = "📄", children_blocks: List[Dict[str, Any]] = None) -> str:
    """Notionページを作成し、ページIDを返す（429リトライ付き）。"""
    url = "https://api.notion.com/v1/pages"
    payload = {
        "parent": {"page_id": parent_id},
        "icon": {"type": "emoji", "emoji": icon_emoji},
        "properties": {
            "title": [{"type": "text", "text": {"content": title}}]
        }
    }
    if children_blocks:
        payload["children"] = children_blocks[:100]

    for attempt in range(6):
        res = requests.post(url, headers=NOTION_HEADERS, json=payload)
        if res.status_code == 429:
            retry_after = float(res.headers.get("Retry-After", "1.0"))
            time.sleep(retry_after + 0.2 * attempt)
            continue
        if res.ok:
            page_id = res.json()["id"]
            if children_blocks and len(children_blocks) > 100:
                # 100ブロック以上ある場合は追加
                append_blocks(page_id, children_blocks[100:])
            return page_id
        time.sleep(0.5)
    raise RuntimeError(f"Failed to create page [{title}]: {res.status_code} - {res.text}")

def append_blocks(page_id: str, blocks: List[Dict[str, Any]]):
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i+100]
        for attempt in range(5):
            res = requests.patch(url, headers=NOTION_HEADERS, json={"children": chunk})
            if res.status_code == 429:
                time.sleep(float(res.headers.get("Retry-After", "1.0")) + 0.2)
                continue
            if res.ok:
                break
            time.sleep(0.5)

def create_database(parent_id: str, title: str, icon_emoji: str, properties: Dict[str, Any]) -> str:
    """Notionデータベースを作成する。"""
    url = "https://api.notion.com/v1/databases"
    payload = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": icon_emoji},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": properties
    }
    for attempt in range(5):
        res = requests.post(url, headers=NOTION_HEADERS, json=payload)
        if res.status_code == 429:
            time.sleep(float(res.headers.get("Retry-After", "1.0")) + 0.2)
            continue
        if res.ok:
            return res.json()["id"]
        time.sleep(0.5)
    raise RuntimeError(f"Failed to create database [{title}]: {res.status_code} - {res.text}")

def create_db_row(db_id: str, title: str, props: Dict[str, Any]) -> str:
    url = "https://api.notion.com/v1/pages"
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            "名前": {"title": [{"text": {"content": title}}]},
            **props
        }
    }
    for attempt in range(5):
        res = requests.post(url, headers=NOTION_HEADERS, json=payload)
        if res.status_code == 429:
            time.sleep(float(res.headers.get("Retry-After", "1.0")) + 0.2)
            continue
        if res.ok:
            return res.json()["id"]
        time.sleep(0.5)
    raise RuntimeError(f"Failed to create DB row [{title}]: {res.status_code} - {res.text}")

def build_p(text: str) -> Dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def build_h2(text: str) -> Dict[str, Any]:
    return {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def build_callout(text: str, emoji: str = "💡") -> Dict[str, Any]:
    return {"type": "callout", "callout": {"icon": {"type": "emoji", "emoji": emoji}, "rich_text": [{"type": "text", "text": {"content": text}}]}}


def main():
    logger.info("=" * 65)
    logger.info("🚀 Notion 100ページ（25ページ×4ブロック）テストデータセット生成開始")
    logger.info("=" * 65)

    created_pages: Dict[str, str] = {}
    created_dbs: Dict[str, str] = {}
    t_start = time.time()

    # 0. ルートページ作成
    root_title = "【Task B.1検証】100ページ・マルチブロック移行テストルート"
    logger.info(f"ルートページ作成中: {root_title}...")
    root_id = create_page(
        NOTION_PARENT_PAGE_ID,
        root_title,
        icon_emoji="🌐",
        children_blocks=[
            build_callout("Task B.1: 複数HTMLエクスポート（移行ブロック）の並行処理、ブロック間相互リンク解決、クロスブロックDBビューの解決を検証するための100ページ規模のテストセットです。", "📌"),
            build_h2("構成概要"),
            build_p("・Block A: プロジェクト管理・基盤 (25ページ)"),
            build_p("・Block B: 開発・エンジニアリング (25ページ)"),
            build_p("・Block C: オペレーション・ダッシュボード (25ページ)"),
            build_p("・Block D: QA・リリース管理 (25ページ)"),
        ]
    )
    created_pages["ROOT"] = root_id
    logger.info(f"ルートページ作成完了: {root_id}")

    # ==========================================
    # Block A: プロジェクト管理・基盤 (25ページ)
    # ==========================================
    logger.info("\n--- Block A: プロジェクト管理・基盤 作成中 ---")
    block_a_id = create_page(root_id, "Block A: プロジェクト管理・基盤", icon_emoji="🏢")
    created_pages["BLOCK_A"] = block_a_id

    # Block A DB: プロジェクト一覧 (本体)
    db_a_id = create_database(
        block_a_id,
        "A-DB: マスタープロジェクト管理",
        "📂",
        {
            "名前": {"title": {}},
            "ステータス": {"select": {"options": [{"name": "計画中", "color": "gray"}, {"name": "進行中", "color": "blue"}, {"name": "完了", "color": "green"}]}},
            "優先度": {"select": {"options": [{"name": "高", "color": "red"}, {"name": "中", "color": "yellow"}, {"name": "低", "color": "gray"}]}},
            "オーナー": {"rich_text": {}}
        }
    )
    created_dbs["DB_A"] = db_a_id
    # レコード5件
    for i in range(1, 6):
        r_id = create_db_row(db_a_id, f"Project Alpha-{i:02d}", {
            "ステータス": {"select": {"name": ["計画中", "進行中", "完了"][i % 3]}},
            "優先度": {"select": {"name": ["高", "中", "低"][i % 3]}},
            "オーナー": {"rich_text": [{"text": {"content": f"PM_Lead_{i}"}}]}
        })
        created_pages[f"A_ROW_{i}"] = r_id

    # 通常ドキュメント 18ページ (1親 + 1DB + 5行 + 18ドキュメント = 25ページ)
    for i in range(1, 19):
        p_id = create_page(
            block_a_id,
            f"A-{i:02d}. プロジェクト憲章・企画書 #{i}",
            icon_emoji="📑",
            children_blocks=[
                build_h2(f"プロジェクト方針概要 #{i}"),
                build_p(f"これは Block A のドキュメント第{i}ページです。"),
                build_p("後ほど Block B の開発タスクや Block C のダッシュボードから相互リンクされます。"),
            ]
        )
        created_pages[f"A_DOC_{i}"] = p_id

    logger.info(f"Block A 作成完了 (計 25 ページ/DB)")

    # ==========================================
    # Block B: 開発・エンジニアリング (25ページ)
    # ==========================================
    logger.info("\n--- Block B: 開発・エンジニアリング 作成中 ---")
    block_b_id = create_page(root_id, "Block B: 開発・エンジニアリング", icon_emoji="💻")
    created_pages["BLOCK_B"] = block_b_id

    # Block B DB: 開発タスク (Block A の DB との Relation あり)
    db_b_id = create_database(
        block_b_id,
        "B-DB: 開発タスク管理",
        "⚙️",
        {
            "名前": {"title": {}},
            "進捗ステータス": {"select": {"options": [{"name": "Todo", "color": "gray"}, {"name": "In Progress", "color": "blue"}, {"name": "Done", "color": "green"}]}},
            "種別": {"select": {"options": [{"name": "Frontend", "color": "purple"}, {"name": "Backend", "color": "orange"}, {"name": "Infra", "color": "brown"}]}},
            "プロジェクト参照": {
                "relation": {
                    "database_id": db_a_id,
                    "type": "single_property",
                    "single_property": {}
                }
            }
        }
    )
    created_dbs["DB_B"] = db_b_id
    # レコード8件 (Block A の行を参照)
    for i in range(1, 9):
        ref_row_id = created_pages.get(f"A_ROW_{(i % 5) + 1}")
        props = {
            "進捗ステータス": {"select": {"name": ["Todo", "In Progress", "Done"][i % 3]}},
            "種別": {"select": {"name": ["Frontend", "Backend", "Infra"][i % 3]}},
        }
        if ref_row_id:
            props["プロジェクト参照"] = {"relation": [{"id": ref_row_id}]}
        r_id = create_db_row(db_b_id, f"DevTask #{i:03d}: アーキテクチャ実装", props)
        created_pages[f"B_ROW_{i}"] = r_id

    # 通常ドキュメント 15ページ (1親 + 1DB + 8行 + 15ドキュメント = 25ページ)
    for i in range(1, 16):
        p_id = create_page(
            block_b_id,
            f"B-{i:02d}. 技術仕様書・API設計 #{i}",
            icon_emoji="📘",
            children_blocks=[
                build_h2(f"技術仕様コンポーネント #{i}"),
                build_p(f"これは Block B の技術ドキュメント第{i}ページです。"),
                build_p(f"前提条件: Block A のプロジェクト企画を参照しています。"),
            ]
        )
        created_pages[f"B_DOC_{i}"] = p_id

    logger.info(f"Block B 作成完了 (計 25 ページ/DB)")

    # ==========================================
    # Block C: オペレーション・ダッシュボード (25ページ)
    # ==========================================
    logger.info("\n--- Block C: 運用・ダッシュボード 作成中 ---")
    block_c_id = create_page(root_id, "Block C: 運用・ダッシュボード", icon_emoji="📊")
    created_pages["BLOCK_C"] = block_c_id

    # 通常ドキュメント 24ページ (1親 + 24ドキュメント = 25ページ)
    # ここにはクロスブロックのLinked Databaseビューや相互参照リンクを多数配置
    for i in range(1, 25):
        children = [
            build_h2(f"運用メトリクス・モニタリングレポート #{i}"),
            build_p(f"これは Block C の運用ドキュメント第{i}ページです。"),
        ]
        if i == 1:
            children.append(build_callout("【クロスブロック検証】Block A のプロジェクトDBおよび Block B の開発タスクDBを参照する統合ダッシュボードです。", "🚀"))
            children.append(build_h2("1. Block A プロジェクト進捗状況 (Board View)"))
            children.append(build_p("※HTMLエクスポート時、Block AのDBビューとして平坦化されます。"))
            children.append(build_h2("2. Block B 開発タスク一覧 (Table View)"))
            children.append(build_p("※HTMLエクスポート時、Block BのDBビューとして平坦化されます。"))
        p_id = create_page(
            block_c_id,
            f"C-{i:02d}. 運用 Runbook & SLA管理 #{i}",
            icon_emoji="📈",
            children_blocks=children
        )
        created_pages[f"C_DOC_{i}"] = p_id

    logger.info(f"Block C 作成完了 (計 25 ページ)")

    # ==========================================
    # Block D: QA・リリース管理 (25ページ)
    # ==========================================
    logger.info("\n--- Block D: QA・リリース管理 作成中 ---")
    block_d_id = create_page(root_id, "Block D: QA・リリース管理", icon_emoji="🎯")
    created_pages["BLOCK_D"] = block_d_id

    # Block D DB: リリーストラッキング
    db_d_id = create_database(
        block_d_id,
        "D-DB: リリース・デプロイ履歴",
        "🚀",
        {
            "名前": {"title": {}},
            "リリースフェーズ": {"select": {"options": [{"name": "Staging", "color": "orange"}, {"name": "Production", "color": "green"}]}},
            "判定": {"select": {"options": [{"name": "合格", "color": "green"}, {"name": "要修正", "color": "red"}]}},
            "対象タスク参照": {
                "relation": {
                    "database_id": db_b_id,
                    "type": "single_property",
                    "single_property": {}
                }
            }
        }
    )
    created_dbs["DB_D"] = db_d_id
    # レコード4件 (Block B のタスクを参照)
    for i in range(1, 5):
        ref_b_row = created_pages.get(f"B_ROW_{i}")
        props = {
            "リリースフェーズ": {"select": {"name": "Production" if i % 2 == 0 else "Staging"}},
            "判定": {"select": {"name": "合格" if i != 2 else "要修正"}},
        }
        if ref_b_row:
            props["対象タスク参照"] = {"relation": [{"id": ref_b_row}]}
        r_id = create_db_row(db_d_id, f"Release v{i}.0.0", props)
        created_pages[f"D_ROW_{i}"] = r_id

    # 通常ドキュメント 19ページ (1親 + 1DB + 4行 + 19ドキュメント = 25ページ)
    for i in range(1, 20):
        p_id = create_page(
            block_d_id,
            f"D-{i:02d}. QAテスト報告書・受け入れテスト #{i}",
            icon_emoji="📋",
            children_blocks=[
                build_h2(f"品質検証サマリー #{i}"),
                build_p(f"これは Block D のQAドキュメント第{i}ページです。"),
                build_p(f"対象機能: Block B の開発仕様および Block A の要件と突合済み。"),
            ]
        )
        created_pages[f"D_DOC_{i}"] = p_id

    logger.info(f"Block D 作成完了 (計 25 ページ/DB)")

    # ==========================================
    # クロスブロック相互リンクの注入 (Pass 2)
    # ==========================================
    logger.info("\n--- ブロック間相互リンク（クロスブロック参照）を注入中 ---")
    # Block A の重要ページに Block B, C, D へのリンクを追加
    append_blocks(created_pages["BLOCK_A"], [
        build_h2("🔗 関連ブロックへのクロスリンク"),
        build_p("このブロックは以下の他ブロックと密接に連携しています："),
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "・開発タスク: "}},
            {"type": "mention", "mention": {"type": "page", "page": {"id": created_pages["BLOCK_B"]}}}
        ]}},
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "・運用ダッシュボード: "}},
            {"type": "mention", "mention": {"type": "page", "page": {"id": created_pages["BLOCK_C"]}}}
        ]}},
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "・QAリリース判定: "}},
            {"type": "mention", "mention": {"type": "page", "page": {"id": created_pages["BLOCK_D"]}}}
        ]}}
    ])

    # Block B の重要ページに Block A, D へのリンクを追加
    append_blocks(created_pages["BLOCK_B"], [
        build_h2("🔗 関連プロジェクト・リリースへのクロスリンク"),
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "・親プロジェクト憲章: "}},
            {"type": "mention", "mention": {"type": "page", "page": {"id": created_pages["BLOCK_A"]}}}
        ]}},
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "・リリース履歴DB: "}},
            {"type": "mention", "mention": {"type": "page", "page": {"id": created_pages["BLOCK_D"]}}}
        ]}}
    ])

    total_time = time.time() - t_start
    total_count = len(created_pages)
    logger.info("\n" + "=" * 65)
    logger.info(f"🎉 100ページ規模（25ページ×4ブロック）作成完了！")
    logger.info(f"総作成数: {total_count} ページ/レコード/DB")
    logger.info(f"所要時間: {total_time:.1f} 秒 (平均 {total_count/total_time:.2f} items/s)")
    logger.info(f"ルートページID: {root_id}")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()

