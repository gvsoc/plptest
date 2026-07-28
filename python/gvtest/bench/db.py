#!/usr/bin/env python3

#
# Copyright (C) 2026 ETH Zurich, University of Bologna and GreenWaves Technologies
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Benchmark database — ingest JSON results into SQLite.

Usage:
    python -m gvtest.bench.db init   --db bench.sqlite
    python -m gvtest.bench.db insert --json results.json --db bench.sqlite
    python -m gvtest.bench.db list   --db bench.sqlite [--test PATTERN]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    git_commit TEXT,
    git_branch TEXT,
    platform   TEXT NOT NULL,
    json_file  TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    test        TEXT NOT NULL,
    target      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    value_min   REAL,
    value_max   REAL,
    description TEXT,
    reference   REAL,
    tolerance   REAL,
    ref_type  TEXT,
    UNIQUE(run_id, test, target, metric)
);

CREATE INDEX IF NOT EXISTS idx_results_test_metric
    ON results(test, metric);
CREATE INDEX IF NOT EXISTS idx_results_target
    ON results(target);
CREATE INDEX IF NOT EXISTS idx_runs_timestamp
    ON runs(timestamp);
"""


_SCHEMA_VERSION = 3


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing database up to the current schema.

    Version 2 added the reference/tolerance/ref_type columns; version 3
    added value_min/value_max (the spread of a metric measured over
    several activations). Older rows read back NULL, i.e. measured-only
    and/or a single measurement with no spread.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
    for col, decl in (('reference', 'REAL'), ('tolerance', 'REAL'),
                      ('ref_type', 'TEXT'),
                      ('value_min', 'REAL'), ('value_max', 'REAL')):
        if col not in cols:
            conn.execute(f"ALTER TABLE results ADD COLUMN {col} {decl}")
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables if they don't exist. Returns connection."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def insert_json(db_path: str, json_path: str) -> int | None:
    """Insert a JSON results file into the database.

    Returns the run_id of the inserted run, or None if
    the file was already ingested (idempotent).
    """
    json_path = os.path.abspath(json_path)

    with open(json_path, 'r') as f:
        data = json.load(f)

    conn = init_db(db_path)

    # Check if already ingested
    row = conn.execute(
        "SELECT id FROM runs WHERE json_file = ?",
        (json_path,)
    ).fetchone()
    if row is not None:
        print(f"Already ingested: {json_path} (run_id={row[0]})")
        conn.close()
        return None

    cursor = conn.execute(
        "INSERT INTO runs (timestamp, git_commit, git_branch, platform, json_file) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            data.get('timestamp', ''),
            data.get('git_commit'),
            data.get('git_branch'),
            data.get('platform', 'unknown'),
            json_path,
        )
    )
    run_id = cursor.lastrowid

    for result in data.get('results', []):
        conn.execute(
            "INSERT OR IGNORE INTO results "
            "(run_id, test, target, metric, value, value_min, value_max, "
            "description, reference, tolerance, ref_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                result.get('test', ''),
                result.get('target', ''),
                result.get('metric', ''),
                result.get('value', 0),
                result.get('value_min'),
                result.get('value_max'),
                result.get('description', ''),
                result.get('ref'),
                result.get('tol'),
                result.get('ref_type'),
            )
        )

    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM results WHERE run_id = ?",
        (run_id,)
    ).fetchone()[0]
    print(f"Inserted run_id={run_id}: {count} result(s) from {json_path}")
    conn.close()
    return run_id


def list_tests(db_path: str, pattern: str | None = None) -> None:
    """List distinct test/metric combinations in the database."""
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT DISTINCT r.test, r.metric, r.target, r.description,
               COUNT(*) as num_runs,
               MIN(ru.timestamp) as first_run,
               MAX(ru.timestamp) as last_run
        FROM results r
        JOIN runs ru ON r.run_id = ru.id
    """
    params: list[str] = []
    if pattern is not None:
        query += " WHERE r.test LIKE ?"
        params.append(pattern.replace('*', '%'))
    query += " GROUP BY r.test, r.metric, r.target ORDER BY r.test, r.metric, r.target"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        print("No benchmark results found.")
        conn.close()
        return

    # Simple table output
    fmt = "  {:<50s} {:<35s} {:<20s} {:>5s}  {:<20s}  {:<20s}"
    print(fmt.format("TEST", "METRIC", "TARGET", "RUNS", "FIRST", "LAST"))
    print("  " + "-" * 155)
    for row in rows:
        print(fmt.format(
            row['test'], row['metric'], row['target'],
            str(row['num_runs']), row['first_run'][:19], row['last_run'][:19],
        ))

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Benchmark database tool'
    )
    subparsers = parser.add_subparsers(dest='command')

    p_init = subparsers.add_parser('init', help='Initialize database')
    p_init.add_argument('--db', required=True, help='SQLite database path')

    p_insert = subparsers.add_parser('insert', help='Insert JSON results')
    p_insert.add_argument('--json', required=True, dest='json_file',
                          help='JSON results file')
    p_insert.add_argument('--db', required=True, help='SQLite database path')

    p_list = subparsers.add_parser('list', help='List benchmarks')
    p_list.add_argument('--db', required=True, help='SQLite database path')
    p_list.add_argument('--test', default=None,
                        help='Filter by test name (supports * wildcard)')

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == 'init':
        conn = init_db(args.db)
        print(f"Database initialized: {args.db}")
        conn.close()

    elif args.command == 'insert':
        insert_json(args.db, args.json_file)

    elif args.command == 'list':
        list_tests(args.db, args.test)


if __name__ == '__main__':
    main()
