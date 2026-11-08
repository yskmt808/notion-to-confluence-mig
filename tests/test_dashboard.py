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


if __name__ == "__main__":
    unittest.main()

