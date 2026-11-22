"""Store an instrumented batch of OCEL events for Eye to observe."""

from eye.intervals import GRACE, HOUR


def publish_window(db, start, objects, events, links, manifest, *, staged=False):
    expected = sum(manifest.values())
    window = db.execute(
        """insert into windows (start_at, end_at, expected_events, observation_snapshot, published_through)
           values (%s, %s, %s, %s, %s)
           on conflict (start_at) do nothing returning start_at""",
        (start, start + HOUR, expected, start + HOUR + GRACE, start if staged else None),
    ).fetchone()
    if not window:
        return
    with db.cursor() as cursor:
        cursor.executemany("insert into objects values (%s, %s, %s) on conflict (id) do nothing", objects)
        for table, data in (("events", sorted(events, key=lambda row: row[2])), ("event_objects", links)):
            with cursor.copy(f"copy {table} from stdin") as copy:
                for row in data:
                    copy.write_row(row)
        with cursor.copy("copy source_manifests from stdin") as copy:
            for (minute, source, activity), count in manifest.items():
                copy.write_row((start, minute, source, activity, count))
