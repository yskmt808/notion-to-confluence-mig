import unittest
import datetime
from src.timezone_util import (
    get_local_timezone,
    get_local_timezone_name,
    resolve_timezone,
    format_iso_datetime,
)
from src.converter import NotionToConfluenceConverter
from src.worksheet import WorksheetManager
from src.extractor import PageNode


class TestTimezoneAndCalendar(unittest.TestCase):
    def test_local_timezone_detection(self):
        tz = get_local_timezone()
        self.assertIsNotNone(tz)
        name = get_local_timezone_name()
        self.assertTrue(len(name) > 0)

    def test_resolve_timezone(self):
        tz_jst = resolve_timezone("JST")
        self.assertEqual(tz_jst.tzname(None), "JST")

        tz_utc = resolve_timezone("UTC")
        self.assertEqual(tz_utc.utcoffset(None), datetime.timedelta(0))

        # None resolves to local
        tz_none = resolve_timezone(None)
        self.assertIsNotNone(tz_none)

    def test_format_iso_datetime(self):
        # 2026-11-07 23:30:00 UTC == 2026-11-08 08:30:00 JST
        iso_str = "2026-11-07T23:30:00.000Z"
        formatted_jst = format_iso_datetime(iso_str, tz="JST")
        self.assertIn("2026-11-08 08:30", formatted_jst)
        self.assertIn("JST", formatted_jst)

        formatted_utc = format_iso_datetime(iso_str, tz="UTC")
        self.assertIn("2026-11-07 23:30", formatted_utc)
        self.assertIn("UTC", formatted_utc)

    def test_converter_date_property_with_time_and_range(self):
        conv = NotionToConfluenceConverter(timezone="JST")
        prop_range = {
            "type": "date",
            "start": "2026-11-07T23:30:00.000Z",
            "end": "2026-11-08T03:00:00.000Z",
        }
        rendered = conv.format_property_for_confluence(prop_range)
        # Should render start and end with lozenge and JST time
        self.assertIn('<time datetime="2026-11-07" />', rendered)
        self.assertIn("08:30 JST", rendered)
        self.assertIn('〜 <time datetime="2026-11-08" />', rendered)
        self.assertIn("12:00 JST", rendered)

    def test_worksheet_calendar_view_mapping(self):
        db_node = PageNode(
            id="test-db-1",
            title="スケジュール管理DB",
            node_type="database",
            properties={
                "schema": {"日時": {"type": "date"}},
                "views_config": [
                    {"name": "月別カレンダー", "type": "calendar"},
                    {"name": "タイムライン計画", "type": "timeline"},
                    {"name": "進行中ボード", "type": "board"},
                ],
            },
        )
        views = WorksheetManager._extract_db_views(db_node)
        self.assertEqual(len(views), 3)

        v_cal = next(v for v in views if v["view_name"] == "月別カレンダー")
        self.assertEqual(v_cal["inferred_layout"], "table")
        self.assertIn("カレンダー", v_cal["notes"])

        v_tl = next(v for v in views if v["view_name"] == "タイムライン計画")
        self.assertEqual(v_tl["inferred_layout"], "table")
        self.assertIn("timeline", v_tl["notes"])

        v_board = next(v for v in views if v["view_name"] == "進行中ボード")
        self.assertEqual(v_board["inferred_layout"], "board")

    def test_telemetry_respects_tz_env(self):
        """TZ環境変数設定時に SanitizedTelemetry の session_id, started_at, finished_at, error timestamp が反映されること"""
        import os
        import time
        from unittest.mock import patch
        from src.telemetry import SanitizedTelemetry

        orig_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Asia/Tokyo"
            if hasattr(time, "tzset"):
                time.tzset()

            telemetry = SanitizedTelemetry()
            self.assertIn("+09:00", telemetry.started_at)

            telemetry.record_error(RuntimeError("test error"), context="testing")
            report = telemetry.generate_report()
            self.assertIn("+09:00", report["timestamps"]["started_at"])
            self.assertIn("+09:00", report["timestamps"]["finished_at"])
            self.assertIn("+09:00", report["errors"][0]["timestamp"])
        finally:
            if orig_tz is not None:
                os.environ["TZ"] = orig_tz
            else:
                os.environ.pop("TZ", None)
            if hasattr(time, "tzset"):
                time.tzset()

    def test_checkpoint_respects_tz_env(self):
        """TZ環境変数設定時に MigrationCheckpoint の started_at, last_updated_at, pass1/pass2 timestamp が反映されること"""
        import os
        import time
        import tempfile
        from src.checkpoint import MigrationCheckpoint

        orig_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Asia/Tokyo"
            if hasattr(time, "tzset"):
                time.tzset()

            with tempfile.TemporaryDirectory() as tmpdir:
                cp_file = os.path.join(tmpdir, "checkpoint.json")
                cp = MigrationCheckpoint(checkpoint_path=cp_file)
                self.assertIn("+09:00", cp.started_at)
                self.assertIn("+09:00", cp.last_updated_at)

                cp.record_pass1("node-1", "101", "Page 1", "https://example.com/1", "page")
                self.assertIn("+09:00", cp.nodes["node-1"]["pass1_timestamp"])

                cp.record_pass2("node-1")
                self.assertIn("+09:00", cp.nodes["node-1"]["pass2_timestamp"])
                self.assertIn("+09:00", cp.last_updated_at)
        finally:
            if orig_tz is not None:
                os.environ["TZ"] = orig_tz
            else:
                os.environ.pop("TZ", None)
            if hasattr(time, "tzset"):
                time.tzset()

    def test_migrate_cli_respects_tz_and_custom_tz(self):
        """migrate.py CLI 実行時の Start Timestamp とステップ開始時刻に TZ (Asia/Tokyo) および --tz 指定が反映されること"""
        import os
        import io
        import sys
        import time
        import zipfile
        import tempfile
        import contextlib
        from unittest.mock import patch
        import migrate

        orig_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Asia/Tokyo"
            if hasattr(time, "tzset"):
                time.tzset()

            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = os.path.join(tmpdir, "test.zip")
                with zipfile.ZipFile(zip_path, "w") as zf:
                    zf.writestr(
                        "Test 11111111111111111111111111111111.html",
                        "<html><head><title>Test</title></head><body><h1>Test</h1></body></html>".encode("utf-8"),
                    )

                stdout_buf = io.StringIO()
                with patch.object(sys, "argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--checkpoint", os.path.join(tmpdir, "cp.json")]), \
                     contextlib.redirect_stdout(stdout_buf), \
                     patch("migrate.setup_logger"):
                    migrate.main()

                output = stdout_buf.getvalue()
                self.assertIn("Start Timestamp      :", output)
                self.assertIn("+09:00", output)
                self.assertIn("Timezone             : Asia/Tokyo", output)
        finally:
            if orig_tz is not None:
                os.environ["TZ"] = orig_tz
            else:
                os.environ.pop("TZ", None)
            if hasattr(time, "tzset"):
                time.tzset()


if __name__ == "__main__":
    unittest.main()

