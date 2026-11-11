"""
CLI インタラクティブ進捗UI・移行ダッシュボードモジュール (Task B.10)。
大規模移行時に、端末上で進行状況・ワーカースレッド稼働状況・推定残り時間をリアルタイム表示します。
"""

import sys
import time
import threading
from typing import Optional, Dict, Any

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        Progress,
        BarColumn,
        TextColumn,
        SpinnerColumn,
        MofNCompleteColumn,
        TimeRemainingColumn,
        TimeElapsedColumn,
    )
    from rich.table import Table
    from rich.markup import escape
    RICH_AVAILABLE = True
except ImportError:
    escape = lambda x: str(x)
    RICH_AVAILABLE = False


class MigrationDashboard:
    """
    移行処理のリアルタイム進捗を表示するダッシュボード。
    Richが利用可能かつTTY環境の場合はリッチなインタラクティブUIを表示し、
    非TTY環境（CIやパイプ出力）または--no-dashboard指定時は簡潔なテキスト行ログにフォールバックする。
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        page_workers: int = 8,
        space_key: str = "",
        title_prefix: str = "",
    ):
        self.is_tty = sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False
        if enabled is None:
            # TTYかつRich利用可能ならデフォルト有効
            self.enabled = RICH_AVAILABLE and self.is_tty
        else:
            self.enabled = enabled and RICH_AVAILABLE

        self.page_workers = page_workers
        self.space_key = space_key
        self.title_prefix = title_prefix

        self._lock = threading.RLock()
        self.start_time = 0.0

        # メトリクス
        self.pass1_total = 0
        self.pass1_completed = 0
        self.pass1_skipped = 0
        self.pass2_total = 0
        self.pass2_completed = 0
        self.pass2_skipped = 0
        self.errors_count = 0
        self.warnings_count = 0

        # メインスレッド実行タスク状態
        self.main_task: str = "待機中 (Idle)"
        self._last_plain_main_task: str = ""

        # ワーカースレッド状態
        self.worker_slots: Dict[int, str] = {}
        for i in range(1, max(2, page_workers) + 1):
            self.worker_slots[i] = "待機中 (Idle)"
        self._available_slots: List[int] = list(range(1, max(2, page_workers) + 1))
        self._thread_to_slot: Dict[int, int] = {}

        # 最後のログ出力時刻（非TTYモード時のレートリミット用）
        self._last_plain_log_time = 0.0

        # Richコンポーネント
        self.console = Console() if RICH_AVAILABLE else None
        self.live: Optional[Any] = None
        self.progress: Optional[Any] = None
        self.p1_task_id: Optional[int] = None
        self.p2_task_id: Optional[int] = None

    def _get_worker_slot(self) -> int:
        """呼び出し元スレッドに一意のワーカー番号 (1..N) を動的プールから割り当てる。"""
        tid = threading.get_ident()
        with self._lock:
            if tid not in self._thread_to_slot:
                if self._available_slots:
                    slot = self._available_slots.pop(0)
                else:
                    slot = (len(self._thread_to_slot) % max(1, self.page_workers)) + 1
                self._thread_to_slot[tid] = slot
            return self._thread_to_slot[tid]

    def start(self, total_nodes: int):
        """ダッシュボードを開始する。"""
        with self._lock:
            self.start_time = time.time()
            self.pass1_total = total_nodes
            self.pass2_total = total_nodes
            self.main_task = "Pass 1 開始準備中..."

            if not self.enabled:
                print(f"📊 移行タスク開始: 合計 {total_nodes} ノード (ワーカースレッド数: {self.page_workers})")
                return

            self.progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(bar_width=32),
                MofNCompleteColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                TextColumn("ETA:"),
                TimeRemainingColumn(),
                console=self.console,
            )
            self.p1_task_id = self.progress.add_task("Pass 1: ページ・DB作成", total=total_nodes)
            self.p2_task_id = self.progress.add_task("Pass 2: リンク・ビュー解決", total=total_nodes)

            self.live = Live(
                self._build_renderable(),
                console=self.console,
                refresh_per_second=4,
                transient=False,
                auto_refresh=True,
            )
            self.live.start()

    def _build_renderable(self):
        """Richダッシュボードのレイアウトを構築する。"""
        grid = Table.grid(expand=True)
        if self.progress:
            grid.add_row(self.progress)

        # スレッド稼働状況（メインスレッド + ワーカースレッド）
        t_table = Table.grid(padding=(0, 1), expand=False)
        t_table.add_column("Thread", style="bold cyan", min_width=16, max_width=16, no_wrap=True)
        t_table.add_column("Task", style="none", min_width=50, max_width=58, no_wrap=True, overflow="ellipsis")

        # 1. メインスレッドの実行タスク表示行（常に最上部に強調表示）
        main_style = "bold yellow" if self.main_task not in ("待機中 (Idle)", "完了 (Completed)") else "dim"
        t_table.add_row("🧵 Main Thread:", f"[{main_style}]{escape(self.main_task)}[/{main_style}]")

        # 2. ワーカースレッド並行稼働状況（ワーカー数が複数の場合のみ表示）
        if self.page_workers > 1:
            visible_slots = sorted(self.worker_slots.keys())[:min(8, self.page_workers)]
            for slot in visible_slots:
                status = self.worker_slots[slot]
                worker_style = "dim" if status == "待機中 (Idle)" else "green"
                t_table.add_row(f"⚡ Worker #{slot}:", f"[{worker_style}]{escape(status)}[/{worker_style}]")
            panel_title = f"スレッド並行稼働状況 (Main + {self.page_workers} workers)"
        else:
            panel_title = "スレッド稼働状況 (Main)"

        t_panel = Panel(
            t_table,
            title=panel_title,
            border_style="dim",
            padding=(0, 1),
        )
        grid.add_row(t_panel)

        # メトリクスフッター
        now = time.time()
        elapsed = max(0.001, now - self.start_time)
        processed = self.pass1_completed + self.pass1_skipped
        speed = processed / elapsed

        stats_str = (
            f"[bold]進捗:[/bold] 作成 {self.pass1_completed} | "
            f"スキップ {self.pass1_skipped} | "
            f"[yellow]警告 {self.warnings_count}[/yellow] | "
            f"[red]エラー {self.errors_count}[/red] | "
            f"速度: [cyan]{speed:.1f} 件/秒[/cyan]"
        )
        grid.add_row(Panel(stats_str, border_style="cyan", padding=(0, 1)))

        return Panel(
            grid,
            title=f"🚀 Notion to Confluence 移行ダッシュボード [Space: {self.space_key or 'Default'}]",
            border_style="blue",
            padding=(1, 1),
        )

    def _refresh(self):
        """画面表示オブジェクトを更新する（描画はLiveのauto_refreshループに任せる）。"""
        if self.enabled and self.live:
            try:
                self.live.update(self._build_renderable(), refresh=False)
            except Exception:
                pass

    def record_pass1_done(self, title: str, node_type: str = "page"):
        """Pass 1 でノードの作成が完了したことを記録する。"""
        with self._lock:
            self.pass1_completed += 1
            if self.enabled and self.progress and self.p1_task_id is not None:
                self.progress.update(self.p1_task_id, advance=1)
                self._refresh()
            else:
                self._maybe_print_plain_log("Pass 1 作成", title)

    def record_pass1_skip(self, title: str):
        """Pass 1 でチェックポイントによりスキップされたことを記録する。"""
        with self._lock:
            self.pass1_skipped += 1
            if self.enabled and self.progress and self.p1_task_id is not None:
                self.progress.update(self.p1_task_id, advance=1)
                self._refresh()
            else:
                self._maybe_print_plain_log("Pass 1 スキップ", title)

    def record_pass2_done(self, title: str):
        """Pass 2 でリンク/ビュー更新が完了したことを記録する。"""
        with self._lock:
            self.pass2_completed += 1
            if self.enabled and self.progress and self.p2_task_id is not None:
                self.progress.update(self.p2_task_id, advance=1)
                self._refresh()
            else:
                self._maybe_print_plain_log("Pass 2 更新", title)

    def record_pass2_skip(self, title: str):
        """Pass 2 でスキップされたことを記録する。"""
        with self._lock:
            self.pass2_skipped += 1
            if self.enabled and self.progress and self.p2_task_id is not None:
                self.progress.update(self.p2_task_id, advance=1)
                self._refresh()

    def record_error(self, message: str):
        """エラー発生を記録する。"""
        with self._lock:
            self.errors_count += 1
            self._refresh()

    def record_warning(self, message: str):
        """警告発生を記録する。"""
        with self._lock:
            self.warnings_count += 1
            self._refresh()

    def set_main_task(self, task_name: str):
        """メインスレッドで実行中のシーケンシャル処理タスク表示を更新する。"""
        with self._lock:
            # 日本語全角文字の端末幅を考慮し、最大50文字程度に安全に切り詰めて行折り返しを防ぐ
            short_task = task_name if len(task_name) <= 50 else task_name[:47] + "..."
            self.main_task = short_task
            if not self.enabled:
                if self._last_plain_main_task != short_task:
                    self._last_plain_main_task = short_task
                    print(f"🧵 [Main Thread] {short_task}")
            self._refresh()

    def set_worker_busy(self, title: str, node_type: str = "page"):
        """現在のスレッドの作業内容を更新する。メインスレッドから呼ばれた場合は set_main_task に委譲する。"""
        if self.page_workers > 1 and threading.current_thread() is threading.main_thread():
            self.set_main_task(f"[{node_type}] {title}")
            return

        slot = self._get_worker_slot()
        with self._lock:
            # 日本語全角文字の端末幅を考慮し、最大26文字程度に安全に切り詰めて行折り返しを防ぐ
            short_title = title if len(title) <= 26 else title[:23] + "..."
            self.worker_slots[slot] = f"[{node_type}] {short_title}"
            self._refresh()

    def set_worker_idle(self):
        """現在のスレッドを待機状態に戻し、スロット番号を返却する。"""
        if self.page_workers > 1 and threading.current_thread() is threading.main_thread():
            return

        tid = threading.get_ident()
        with self._lock:
            slot = self._thread_to_slot.pop(tid, None)
            if slot is not None:
                if slot not in self._available_slots:
                    self._available_slots.append(slot)
                    self._available_slots.sort()
                self.worker_slots[slot] = "待機中 (Idle)"
            self._refresh()

    def _maybe_print_plain_log(self, action: str, title: str):
        """非TTYモード時のレートリミットされたプレーンテキストログ。"""
        now = time.time()
        # 1秒に1回、または最後のノードで出力
        if now - self._last_plain_log_time >= 1.0 or (self.pass1_completed + self.pass1_skipped) >= self.pass1_total:
            self._last_plain_log_time = now
            total = max(1, self.pass1_total)
            done = self.pass1_completed + self.pass1_skipped
            pct = (done / total) * 100
            print(f"[{action}] {done}/{total} ({pct:.1f}%) - 最新: {title[:35]}")

    def finish(self):
        """ダッシュボードの表示を終了し、最終サマリーを出力する。"""
        with self._lock:
            self.main_task = "完了 (Completed)"
            if self.enabled and self.live:
                try:
                    self._refresh()
                    self.live.stop()
                except Exception:
                    pass

            elapsed = max(0.001, time.time() - self.start_time)
            total_pass1 = self.pass1_completed + self.pass1_skipped
            total_pass2 = self.pass2_completed + self.pass2_skipped
            speed = total_pass1 / elapsed

            print("\n" + "=" * 50)
            print("🏁 Notion to Confluence 移行処理完了 (Summary)")
            print(f"  • Pass 1 (ページ・DB作成) : {self.pass1_completed} 作成 / {self.pass1_skipped} スキップ (計 {total_pass1} 件)")
            print(f"  • Pass 2 (リンク・ビュー) : {self.pass2_completed} 更新 / {self.pass2_skipped} スキップ (計 {total_pass2} 件)")
            print(f"  • 所要時間                : {elapsed:.2f} 秒 (平均 {speed:.1f} 件/秒)")
            if self.warnings_count > 0:
                print(f"  • ⚠️ 警告件数             : {self.warnings_count} 件")
            if self.errors_count > 0:
                print(f"  • ❌ エラー件数           : {self.errors_count} 件")
            print("=" * 50 + "\n")

