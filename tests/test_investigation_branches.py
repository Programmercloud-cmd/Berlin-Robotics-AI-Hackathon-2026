import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from psycopg.types.json import Jsonb

from eye.db import connect, set_state, setup
from eye.investigation import finish_next_branch
from eye.investigation_branches import search_cohort

try:
    from pgserver import get_server
except ImportError:
    get_server = None


START = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)


def event(identifier, activity, offset, *, ingest=None, **attrs):
    occurred = START + timedelta(seconds=offset)
    return {"id": identifier, "activity": activity, "occurred_at": occurred,
            "ingested_at": occurred if ingest is None else START + timedelta(seconds=ingest), "attrs": attrs}


class EvidenceBranchesTest(unittest.TestCase):
    def test_search_cohorts_check_session_order_and_impression_timing(self):
        rows = [
            event("search-a", "search_requested", 0, session_id="a", experiment_arm="holdout",
                  search_release="v1", response_ms=600),
            event("feed-a", "feed_impression", .3, session_id="a"),
            event("search-b", "search_requested", 0, session_id="b", experiment_arm="rollout",
                  search_release="v2", response_ms=100),
            event("order-a", "order_confirmed", 2, session_id="a", order_id="a",
                  order_path="recommendation", recommendation_impression_id="feed-a"),
            event("order-b", "order_confirmed", 2, session_id="b", order_id="b", order_path="direct"),
            event("order-mismatched", "order_confirmed", 2, session_id="b", order_id="other",
                  order_path="recommendation", recommendation_impression_id="feed-a"),
            event("feed-late", "feed_impression", 3, session_id="b"),
        ]
        cohorts = search_cohort(rows)
        self.assertEqual(cohorts["holdout"]["recommendation_orders"], 1)
        self.assertEqual(cohorts["holdout"]["before_response"], 1)
        self.assertEqual(cohorts["rollout"]["direct_orders"], 1)
        self.assertEqual(cohorts["rollout"]["unclassified_orders"], 1)
        self.assertEqual(cohorts["rollout"]["exposed"], 0)

    @unittest.skipUnless(get_server, "Install requirements-local.txt for PostgreSQL tests.")
    def test_late_success_branch_updates_the_recovered_incident(self):
        hour = timedelta(hours=1)
        with TemporaryDirectory(prefix="eye-evidence-branches-") as directory:
            pg = get_server(Path(directory) / "data")
            try:
                dsn = pg.get_uri()
                with connect(dsn) as db:
                    setup(db)
                    for at in (START - hour, START, START + hour):
                        db.execute("""insert into windows(start_at,end_at,expected_events,observation_snapshot)
                                      values (%s,%s,0,%s)""", (at, at + hour, at + hour))
                    db.execute("""insert into metrics(id,name,definition,calculation)
                                  values ('payment_success_rate','Payment success','', '{}')""")
                    payments = [
                        event("a", "payment_attempted", 10, payment_id="a"),
                        event("late-a", "payment_succeeded", 11, ingest=4000, payment_id="a"),
                        event("order-a", "order_confirmed", 12, payment_id="a"),
                        event("b", "payment_attempted", 20, payment_id="b"),
                        event("timeout-b", "payment_timed_out", 21, payment_id="b", response="gateway_timeout"),
                        event("unrelated", "payment_succeeded", 31, ingest=4000, payment_id="other-attempt"),
                    ]
                    with db.cursor() as cursor:
                        cursor.executemany("""insert into events(id,activity,window_start,occurred_at,ingested_at,attrs)
                                             values (%s,%s,%s,%s,%s,%s)""",
                                           [(row["id"], row["activity"], START, row["occurred_at"], row["ingested_at"],
                                             Jsonb(row["attrs"])) for row in payments])
                    db.execute("""insert into incidents(metric_id, scope, opened_at, updated_at, report)
                                  values ('payment_success_rate','{}',%s,%s,%s)""",
                               (START, START, Jsonb({"deep_investigation": "pending"})))
                    set_state(db, "baseline_through", (START - hour).isoformat())
                    set_state(db, "last_window", START.isoformat())

                stop = Event()
                self.assertTrue(finish_next_branch(dsn, stop))
                with connect(dsn) as db:
                    first = db.execute("select report from incidents").fetchone()["report"]
                    self.assertTrue(first["branch_waits_for_receipts"])
                    details = db.execute("select payload from evidence where id=%s",
                                         (first["evidence_branches"][1]["evidence_id"],)).fetchone()["payload"]
                    self.assertEqual(details["attempts"]["late_successes"], 0)
                    self.assertEqual(details["attempts"]["initial_gateway_timeouts"], 1)
                    self.assertEqual(details["attempts"]["orders_while_missing"], 1)
                    db.execute("update incidents set recovered_at=%s, report=report || %s",
                               (START + hour, Jsonb({"recovery": "Preserved recovery."})))
                    set_state(db, "last_window", (START + hour).isoformat())
                self.assertTrue(finish_next_branch(dsn, stop))
                with connect(dsn) as db:
                    last = db.execute("select report, recovered_at from incidents").fetchone()
                    details = db.execute(
                        "select payload from evidence where id=%s",
                        (last["report"]["evidence_branches"][1]["evidence_id"],),
                    ).fetchone()["payload"]
                    self.assertEqual(details["attempts"]["late_successes"], 1)
                    self.assertEqual(details["attempts"]["missing_now"], 0)
                    self.assertEqual(last["recovered_at"], START + hour)
                    self.assertEqual(last["report"]["recovery"], "Preserved recovery.")
                self.assertFalse(finish_next_branch(dsn, stop))
            finally:
                pg.cleanup()
