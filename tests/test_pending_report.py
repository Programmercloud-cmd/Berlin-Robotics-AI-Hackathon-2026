import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from threading import Event

from psycopg.types.json import Jsonb

from eye import investigation
from eye.db import connect, set_state, setup

try:
    from pgserver import get_server
except ImportError:
    get_server = None


class PendingReportTest(unittest.TestCase):
    @unittest.skipUnless(get_server, "Install requirements-local.txt for PostgreSQL tests.")
    def test_report_preserves_recovery_recorded_while_provider_waits(self):
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory(prefix="eye-report-test-"))
            server = get_server(directory + "/data")
            cleanup.callback(server.cleanup)
            dsn = server.get_uri()
            start = datetime(2026, 11, 21, tzinfo=timezone.utc)
            hours = [start + timedelta(hours=i) for i in range(4)]
            with connect(dsn) as db:
                setup(db)
                set_state(db, "baseline_through", start.isoformat())
                db.execute("""insert into metrics (id, name, definition, calculation)
                              values ('success', 'Success', 'Payment success', '{"op":"ratio"}')""")
                db.execute("insert into graph_nodes (id, kind, label) values ('metric:success', 'metric', 'Success')")
                for beginning, end in zip(hours, hours[1:]):
                    db.execute("insert into windows values (%s, %s, 100)", (beginning, end))
                    db.execute("insert into observations values ('success', %s, '{}', 0.98, 100)", (beginning,))
                incident = db.execute(
                    """insert into incidents (metric_id, scope, opened_at, updated_at)
                       values ('success', '{}', %s, %s) returning id""",
                    (start, start),
                ).fetchone()["id"]
                db.execute("insert into pending_reports values (%s, '{}', %s)",
                           (incident, Jsonb({"summary": "Fallback.", "reasoning": "statistical"})))

            requested, release, stop = Event(), Event(), Event()

            def provider(system, context):
                requested.set()
                if not release.wait(10):
                    raise TimeoutError("Test provider was not released.")
                return {"summary": "The delayed report arrived."}

            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(investigation.finish_next_report, dsn, provider, stop)
                try:
                    self.assertTrue(requested.wait(5))
                    for window in hours[1:3]:
                        with connect(dsn) as db:
                            investigation.track(db, window, [])
                    with connect(dsn) as db:
                        before = db.execute("select * from incidents where id = %s", (incident,)).fetchone()
                    self.assertEqual(before["recovered_at"], hours[2])
                    self.assertEqual(before["healthy_windows"], 2)
                finally:
                    release.set()
                self.assertTrue(result.result(timeout=5))
            with connect(dsn) as db:
                after = db.execute("select * from incidents where id = %s", (incident,)).fetchone()
                self.assertIsNone(db.execute("select 1 from pending_reports").fetchone())
            self.assertEqual(after["recovered_at"], before["recovered_at"])
            self.assertEqual(after["report"]["recovery"], before["report"]["recovery"])
            self.assertEqual(after["report"]["summary"], "The delayed report arrived.")
            self.assertEqual(after["report"]["reasoning"], "model")

    def test_unusable_provider_output_preserves_statistical_report(self):
        fallback = {"summary": "Fallback.", "reasoning": "statistical"}
        result = investigation.resolve_report(lambda *_: {"summary": "", "likely_causes": []}, {}, fallback)
        self.assertEqual(result, fallback)
