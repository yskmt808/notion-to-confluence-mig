import unittest
import threading
import time
from src.dashboard import MigrationDashboard


class TestMigrationDashboard(unittest.TestCase):
    def test_dashboard_initialization(self):
        d_enabled = MigrationDashboard(enabled=True, page_workers=4, space_key="TEST")
        self.assertTrue(d_enabled.enabled)
        self.assertEqual(d_enabled.page_workers, 4)

        d_disabled = MigrationDashboard(enabled=False, page_workers=2, space_key="TEST")
        self.assertFalse(d_disabled.enabled)

    def test_dashboard_lifecycle(self):
        d = MigrationDashboard(enabled=False, page_workers=2, space_key="TEST")
        d.start(total_nodes=10)
        self.assertEqual(d.pass1_total, 10)
        self.assertEqual(d.pass2_total, 10)

        d.set_worker_busy("Page 1", node_type="page")
        d.record_pass1_done("Page 1", node_type="page")
        d.set_worker_idle()

        d.record_pass1_skip("Page 2")
        d.record_warning("Warning test")
        d.record_error("Error test")

        d.record_pass2_done("Page 1")
        d.record_pass2_skip("Page 2")

        self.assertEqual(d.pass1_completed, 1)
        self.assertEqual(d.pass1_skipped, 1)
        self.assertEqual(d.pass2_completed, 1)
        self.assertEqual(d.pass2_skipped, 1)
        self.assertEqual(d.warnings_count, 1)
        self.assertEqual(d.errors_count, 1)

        d.finish()

    def test_dashboard_multithreaded_concurrency(self):
        d = MigrationDashboard(enabled=True, page_workers=4, space_key="TEST")
        d.start(total_nodes=40)

        def worker_task(worker_id: int):
            for i in range(10):
                title = f"Worker_{worker_id}_Item_{i}"
                d.set_worker_busy(title, node_type="database_row")
                time.sleep(0.001)
                d.record_pass1_done(title, node_type="database_row")
                d.set_worker_idle()
                d.record_pass2_done(title)

        threads = [threading.Thread(target=worker_task, args=(w,)) for w in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(d.pass1_completed, 40)
        self.assertEqual(d.pass2_completed, 40)
        d.finish()

    def test_dashboard_slot_reuse_and_no_collision(self):
        """スレッドが終了した際にスロットが返却され、次のスレッドが安全に再利用できることを検証"""
        d = MigrationDashboard(enabled=False, page_workers=2, space_key="TEST")
        d.start(total_nodes=10)

        # Thread 1 starts
        def run_thread_a():
            d.set_worker_busy("Task A", node_type="page")
            self.assertIn("[page] Task A", d.worker_slots.values())
            d.set_worker_idle()

        t1 = threading.Thread(target=run_thread_a)
        t1.start()
        t1.join()

        # Both slots should now be idle
        self.assertEqual(d.worker_slots[1], "待機中 (Idle)")
        self.assertEqual(d.worker_slots[2], "待機中 (Idle)")
        self.assertEqual(sorted(d._available_slots), [1, 2])

        # New thread should get slot 1
        def run_thread_b():
            d.set_worker_busy("Task B", node_type="page")
            self.assertEqual(d.worker_slots[1], "[page] Task B")
            d.set_worker_idle()

        t2 = threading.Thread(target=run_thread_b)
        t2.start()
        t2.join()

        self.assertEqual(sorted(d._available_slots), [1, 2])
        d.finish()

    def test_main_thread_task_display_and_renderable(self):
        """メインスレッドのタスク表示行がダッシュボード上で正しく記録・描画されること"""
        from rich.console import Console

        d = MigrationDashboard(enabled=True, page_workers=4, space_key="TEST")
        d.start(total_nodes=20)
        self.assertEqual(d.main_task, "Pass 1 開始準備中...")

        d.set_main_task("Pass 2: データベース最新ビュー再取得中 (1/2: 無題)")
        self.assertEqual(d.main_task, "Pass 2: データベース最新ビュー再取得中 (1/2: 無題)")

        renderable = d._build_renderable()
        c = Console(record=True, width=100)
        c.print(renderable)
        text = c.export_text()

        self.assertIn("Main Thread:", text)
        self.assertIn("Pass 2: データベース最新ビュー再取得中", text)
        self.assertIn("スレッド並行稼働状況 (Main + 4 workers)", text)

        d.finish()
        self.assertEqual(d.main_task, "完了 (Completed)")

    def test_main_thread_set_worker_busy_delegates_safely(self):
        """メインスレッドから set_worker_busy が呼ばれた場合、ワーカー枠を消費せずメインスレッドタスクとして記録されること"""
        d = MigrationDashboard(enabled=False, page_workers=4, space_key="TEST")
        d.start(total_nodes=10)

        # メインスレッドから set_worker_busy を呼び出し
        d.set_worker_busy("相互リンク・ビュー更新準備中...", node_type="準備")
        self.assertEqual(d.main_task, "[準備] 相互リンク・ビュー更新準備中...")
        # ワーカースロットはすべて待機中 (Idle) のままであること
        for slot in range(1, 5):
            self.assertEqual(d.worker_slots[slot], "待機中 (Idle)")

        d.set_worker_idle()
        for slot in range(1, 5):
            self.assertEqual(d.worker_slots[slot], "待機中 (Idle)")
        d.finish()

    def test_main_thread_task_in_single_worker_mode(self):
        """page_workers=1 の場合でもメインスレッド稼働状況が正しく描画されること"""
        from rich.console import Console

        d = MigrationDashboard(enabled=True, page_workers=1, space_key="TEST")
        d.start(total_nodes=5)
        d.set_main_task("Pass 1: ルートノード移行中")

        renderable = d._build_renderable()
        c = Console(record=True, width=100)
        c.print(renderable)
        text = c.export_text()

        self.assertIn("Main Thread:", text)
        self.assertIn("Pass 1: ルートノード移行中", text)
        self.assertIn("スレッド稼働状況 (Main)", text)
        d.finish()


if __name__ == "__main__":
    unittest.main()

