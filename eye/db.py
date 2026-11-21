import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def connect(url=None):
    if not url and not os.environ.get("DATABASE_URL"):
        load_dotenv()
    dsn = url or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL connection string.")
    db = psycopg.connect(dsn, row_factory=dict_row, prepare_threshold=None)
    db.execute("set search_path to eye; set timezone to 'UTC'")
    return db


def setup(db):
    db.execute(Path(__file__).with_name("schema.sql").read_text())


def state(db, key):
    row = db.execute("select value from runtime where key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(db, key, value):
    db.execute(
        "insert into runtime values (%s, %s) on conflict (key) do update set value = excluded.value",
        (key, Jsonb(value)),
    )
