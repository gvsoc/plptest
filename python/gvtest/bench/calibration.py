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
Calibration HTML report generator — compares the benchmark values measured
by a gvtest run against reference numbers, per metric and per target.

References normally come from the testset declarations (`add_bench(...,
ref=, tol=/tol_pct=, ref_type=)`) and are stored next to each measured
value in the bench DB. Alternatively a baseline run already in the DB can
serve as the reference (run-vs-run mode), e.g. an RTL-platform run used as
the reference for a gvsoc-platform run of the same tests.

Usage:
    python -m gvtest.bench.calibration --db bench.sqlite --output calibration-report.html
    python -m gvtest.bench.calibration --db bench.sqlite --output r.html --target "spatz*"
    python -m gvtest.bench.calibration --db bench.sqlite --output r.html --ref-platform rtl

The output is a single self-contained HTML file (inline CSS, no external
resources, light/dark aware).
"""

from __future__ import annotations

import argparse
import html
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

# (test, target, metric)
Key = tuple[str, str, str]

# Reference types that stand for an external ground truth, as opposed to a
# 'measured' lock (a snapshot of our own past output). The accuracy score and
# the improvement summary score against every declared reference regardless of
# type; this split is used only by the ratchet, which tightens a ground-truth
# tolerance but re-baselines a drifted 'measured' lock.
GROUND_TRUTH = ('rtl', 'analytical')


# -------------------------------------------------------------------- query

def query_results(
    conn: sqlite3.Connection,
    run: int | None = None,
    platform: str | None = None,
    test: str | None = None,
    target: str | None = None,
    exclude_platform: str | None = None,
) -> dict[Key, dict[str, Any]]:
    """Latest result per (test, target, metric) matching the filters.

    Rows are scanned in run-timestamp order so that when the same metric
    appears in several runs (or several times in one run), the most recent
    value wins.
    """
    query = """
        SELECT r.test, r.target, r.metric, r.value, r.description,
               r.reference, r.tolerance, r.ref_type,
               ru.id, ru.timestamp, ru.git_commit, ru.platform,
               r.value_min, r.value_max
        FROM results r
        JOIN runs ru ON r.run_id = ru.id
        WHERE 1=1
    """
    params: list[Any] = []
    if run is not None:
        query += " AND ru.id = ?"
        params.append(run)
    if platform is not None:
        query += " AND ru.platform = ?"
        params.append(platform)
    if exclude_platform is not None:
        query += " AND ru.platform != ?"
        params.append(exclude_platform)
    if test is not None:
        query += " AND r.test LIKE ?"
        params.append(test.replace('*', '%'))
    if target is not None:
        query += " AND r.target LIKE ?"
        params.append(target.replace('*', '%'))
    query += " ORDER BY ru.timestamp ASC, ru.id ASC"

    latest: dict[Key, dict[str, Any]] = {}
    for row in _iter_cells(conn, query, params):
        latest[(row['test'], row['target'], row['metric'])] = row
    return latest


def query_history(
    conn: sqlite3.Connection,
    platform: str | None = None,
    test: str | None = None,
    target: str | None = None,
    exclude_platform: str | None = None,
) -> dict[Key, list[dict[str, Any]]]:
    """Every result per (test, target, metric), oldest run first.

    Same shape as query_results but keeps the whole series so the
    calibration of a metric can be followed across runs/commits.
    """
    query = """
        SELECT r.test, r.target, r.metric, r.value, r.description,
               r.reference, r.tolerance, r.ref_type,
               ru.id, ru.timestamp, ru.git_commit, ru.platform,
               r.value_min, r.value_max
        FROM results r
        JOIN runs ru ON r.run_id = ru.id
        WHERE 1=1
    """
    params: list[Any] = []
    if platform is not None:
        query += " AND ru.platform = ?"
        params.append(platform)
    if exclude_platform is not None:
        query += " AND ru.platform != ?"
        params.append(exclude_platform)
    if test is not None:
        query += " AND r.test LIKE ?"
        params.append(test.replace('*', '%'))
    if target is not None:
        query += " AND r.target LIKE ?"
        params.append(target.replace('*', '%'))
    query += " ORDER BY ru.timestamp ASC, ru.id ASC"

    series: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    for row in _iter_cells(conn, query, params):
        series[(row['test'], row['target'], row['metric'])].append(row)
    return dict(series)


def _iter_cells(conn: sqlite3.Connection, query: str, params: list[Any]):
    for row in conn.execute(query, params):
        (test_name, target_name, metric, value, desc,
         ref, tol, ref_type, run_id, timestamp, commit, run_platform,
         value_min, value_max) = row
        yield {
            'test': test_name,
            'target': target_name,
            'metric': metric,
            'value': value,
            'value_min': value_min,
            'value_max': value_max,
            'desc': desc or metric,
            'ref': ref,
            'ref_min': None,
            'ref_max': None,
            'tol': tol,
            'ref_type': ref_type,
            # The reference as declared in the testset. `ref`/`ref_type` above
            # may later be replaced by a baseline run (run-vs-run mode); these
            # two keep the original ground truth for the accuracy score.
            'declared_ref': ref,
            'declared_ref_type': ref_type,
            'run_id': run_id,
            'timestamp': timestamp,
            'git_commit': commit,
            'platform': run_platform,
        }


# -------------------------------------------------------------------- model

def _classify(cell: dict[str, Any], default_tol_pct: float) -> None:
    """Annotate a cell with delta/delta_pct/severity in place.

    severity: 'ok' (|delta| <= tol), 'warn' (<= 2*tol), 'bad' (beyond),
    or None when the cell has no reference (measured-only).

    For metrics carrying a min/max spread, the same is computed for each
    bound (delta_min_pct/severity_min, ...). Those are display-only: the
    headline average is what drives the aggregates, so a filter counts
    once rather than three times.
    """
    ref = cell['ref']
    if ref is None:
        cell['delta'] = None
        cell['delta_pct'] = None
        cell['severity'] = None
        for side in ('min', 'max'):
            cell[f'delta_{side}_pct'] = None
            cell[f'severity_{side}'] = None
        return

    value = cell['value']
    delta = value - ref
    cell['delta'] = delta
    cell['delta_pct'] = None if ref == 0 else delta / ref * 100

    declared_tol = cell['tol']
    if declared_tol is None and ref != 0:
        cell['tol_fallback'] = True

    cell['severity'] = _severity_of(value, ref, declared_tol, default_tol_pct)

    for side in ('min', 'max'):
        bound, bound_ref = cell[f'value_{side}'], cell[f'ref_{side}']
        cell[f'delta_{side}_pct'] = (
            None if bound is None or not bound_ref
            else (bound - bound_ref) / bound_ref * 100)
        cell[f'severity_{side}'] = _severity_of(
            bound, bound_ref, declared_tol, default_tol_pct)


def _severity_of(value: float | None, ref: float | None,
                 tol: float | None, default_tol_pct: float) -> str | None:
    """ok/warn/bad for one value against one reference."""
    if value is None or ref is None:
        return None
    if tol is None:
        if ref == 0:
            return 'ok' if value == 0 else 'bad'
        tol = abs(ref) * default_tol_pct / 100
    delta = abs(value - ref)
    if delta <= tol:
        return 'ok'
    return 'warn' if delta <= 2 * tol else 'bad'


def _accuracy_error_pct(cell: dict[str, Any]) -> float | None:
    """|measured - reference| / |reference|, in %, against the declared
    reference of any type (rtl, analytical, or a measured lock). Returns None
    only when the cell has no usable declared reference to score against.
    """
    ref = cell.get('declared_ref')
    if ref not in (None, 0) and cell['value'] is not None:
        return abs(cell['value'] - ref) / abs(ref) * 100
    return None


def _aggregate(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary statistics over a list of cells."""
    referenced = [c for c in cells if c['severity'] is not None]
    ok = [c for c in referenced if c['severity'] == 'ok']
    pcts = [c for c in referenced if c['delta_pct'] is not None]
    worst = max(pcts, key=lambda c: abs(c['delta_pct']), default=None)
    # Accuracy score: the mean absolute % error against the declared
    # reference (any type). Lower is more accurate; it is the single number a
    # change should drive down.
    acc = [c['acc_err_pct'] for c in cells if c.get('acc_err_pct') is not None]
    return {
        'n_total': len(cells),
        'n_referenced': len(referenced),
        'n_ok': len(ok),
        'pct_ok': 100 * len(ok) / len(referenced) if referenced else None,
        'median_pct': (statistics.median(c['delta_pct'] for c in pcts)
                       if pcts else None),
        'worst': worst,
        'accuracy': statistics.mean(acc) if acc else None,
        'n_accuracy': len(acc),
        'ref_types': sorted({c['ref_type'] or 'declared'
                             for c in referenced}),
    }


def build_model(
    rows: dict[Key, dict[str, Any]],
    baseline: dict[Key, dict[str, Any]] | None = None,
    baseline_label: str | None = None,
    default_tol_pct: float = 5.0,
) -> dict[str, Any]:
    """Assemble the report model: cells, clusters, per-target and global stats."""
    cells = [dict(row) for row in rows.values()]
    # Trends are filled in by annotate_trends() when history is available;
    # default them so rendering works on a single-run database too.
    for cell in cells:
        cell.setdefault('trend_prev_pct', None)
        cell.setdefault('trend_window_pct', None)
        cell.setdefault('trend_window_days', 30)
        # Filled in by annotate_improvement() when a baseline is given.
        cell.setdefault('improve_pp', None)
        cell.setdefault('improve_err_base', None)
        cell.setdefault('improve_err_now', None)

    if baseline:
        for cell in cells:
            base = baseline.get((cell['test'], cell['target'], cell['metric']))
            if base is not None:
                cell['ref'] = base['value']
                cell['ref_min'] = base['value_min']
                cell['ref_max'] = base['value_max']
                cell['ref_type'] = baseline_label
                # Embedded tolerance (if any) still applies; otherwise the
                # default_tol_pct fallback kicks in during classification.

    for cell in cells:
        _classify(cell, default_tol_pct)
        # Accuracy error keys off the declared ground-truth reference, which
        # survives baseline substitution, so the score means the same in both
        # the plain report and run-vs-run mode.
        cell['acc_err_pct'] = _accuracy_error_pct(cell)

    # One section per target. Targets that happen to share tests (spatz /
    # spatz_v2 / spatz_v3) each get their own, so every section has the same
    # simple shape and carries the per-metric trend columns.
    clusters = []
    for target in sorted({c['target'] for c in cells}):
        target_cells = [c for c in cells if c['target'] == target]
        rows_out = [{
            'test': c['test'],
            'metric': c['metric'],
            'desc': c['desc'],
            'cells': [c],
            'worst_pct': (abs(c['delta_pct'])
                          if c['delta_pct'] is not None else None),
            'referenced': c['severity'] is not None,
        } for c in target_cells]
        # Worst deviations first; measured-only rows last.
        rows_out.sort(key=lambda r: (not r['referenced'],
                                     -(r['worst_pct'] if r['worst_pct'] is not None else -1),
                                     r['test'], r['metric']))
        clusters.append({
            'targets': [target],
            'rows': rows_out,
            'stats': _aggregate(target_cells),
            'n_tests': len({c['test'] for c in target_cells}),
        })

    target_stats = []
    for target_name in sorted({c['target'] for c in cells}):
        target_cells = [c for c in cells if c['target'] == target_name]
        target_stats.append({'target': target_name,
                             **_aggregate(target_cells)})

    runs = sorted({(c['run_id'], c['platform'], c['timestamp'],
                    c['git_commit']) for c in cells})

    return {
        'cells': cells,
        'clusters': clusters,
        'targets': target_stats,
        'global': _aggregate(cells),
        'runs': runs,
        'default_tol_pct': default_tol_pct,
        'baseline_label': baseline_label,
    }


# ------------------------------------------------------------------- render

_CSS = """
:root {
  color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --border:rgba(11,11,11,0.10);
  --accent:#2a78d6;
  --ok:#0ca30c; --warn:#fab219; --bad:#d03b3b;
  --ok-text:#006300; --warn-text:#7a5200; --bad-text:#b02f2f;
  --ok-bg:#e9f4e9; --warn-bg:#faf0d8; --bad-bg:#f9e7e5;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,0.10);
    --accent:#3987e5;
    --ok-text:#0ca30c; --warn-text:#fab219; --bad-text:#e66767;
    --ok-bg:#1c2b1c; --warn-bg:#2e2716; --bad-bg:#321d1b;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,0.10);
  --accent:#3987e5;
  --ok-text:#0ca30c; --warn-text:#fab219; --bad-text:#e66767;
  --ok-bg:#1c2b1c; --warn-bg:#2e2716; --bad-bg:#321d1b;
}
* { box-sizing:border-box }
body { background:var(--page); color:var(--ink); margin:0;
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
/* Wide enough for the widest detail table (per-filter deltas + both
   sides' avg/min/max) without an inner scrollbar on a normal screen.
   Prose blocks keep their own max-width so lines stay readable. */
main { max-width:1680px; margin:0 auto; padding:40px 28px 80px; }
.eyebrow { font-size:12px; letter-spacing:.14em; text-transform:uppercase;
  color:var(--accent); font-weight:600; }
h1 { font-size:30px; line-height:1.15; margin:6px 0 4px; font-weight:650; }
.sub { color:var(--ink-2); max-width:78ch; margin:0 0 10px; }
.runs { color:var(--muted); font-size:12.5px; margin:0 0 26px;
  font-variant-numeric:tabular-nums; }
.strip { display:flex; flex-wrap:wrap; gap:14px; margin:0 0 22px; }
.stat { background:var(--surface); border:1px solid var(--border);
  border-radius:6px; padding:14px 18px; min-width:150px; flex:1; }
.stat .v { font-size:26px; font-weight:650; }
.stat .v small { font-size:15px; color:var(--muted); font-weight:500; }
/* Metric ids (e.g. "sdk:examples:x:filter.y.max") have no spaces to break
   on, so let them wrap anywhere rather than overflow the tile. */
.stat .k { font-size:12.5px; color:var(--ink-2); margin-top:2px;
  overflow-wrap:anywhere; }
.legend { display:flex; gap:18px; flex-wrap:wrap; margin:0 0 28px;
  font-size:13px; color:var(--ink-2); }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:6px; vertical-align:-1px; }
.filters { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 18px; }
.filters button { font:13px system-ui,-apple-system,"Segoe UI",sans-serif;
  padding:5px 14px; border-radius:16px; border:1px solid var(--grid);
  background:var(--surface); color:var(--ink-2); cursor:pointer; }
.filters button:hover { border-color:var(--accent); color:var(--accent); }
.filters button[aria-pressed="true"] { background:var(--accent);
  border-color:var(--accent); color:#fff; font-weight:600; }
.hidden { display:none !important; }
.histo { background:var(--surface); border:1px solid var(--border);
  border-radius:6px; padding:18px 20px 10px; margin-bottom:36px; }
.histo h3 { margin:0 0 14px; font-size:14px; font-weight:600;
  color:var(--ink-2); }
.hrow { display:flex; align-items:flex-end; gap:3px; height:112px; }
.hcol { flex:1; display:flex; flex-direction:column; align-items:center;
  justify-content:flex-end; gap:4px; height:100%; }
.hbar { width:100%; border-radius:3px 3px 0 0; min-height:0; }
.hbar.ok { background:var(--ok) } .hbar.warn { background:var(--warn) }
.hbar.bad { background:var(--bad) }
.hcol span { font:10.5px ui-monospace,Menlo,Consolas,monospace;
  color:var(--muted); font-variant-numeric:tabular-nums; }
.note { color:var(--muted); font-size:12.5px; margin:8px 0 0; }
section { margin-bottom:44px; }
.sec-head { display:flex; flex-wrap:wrap; align-items:baseline; gap:12px;
  margin-bottom:10px; }
h2 { font-size:18px; margin:0; font-weight:650; }
h2::before { content:""; display:inline-block; width:8px; height:8px;
  border-radius:2px; background:var(--accent); margin-right:9px;
  vertical-align:2px; }
.sec-meta { color:var(--ink-2); font-size:13px; }
.scroll { overflow-x:auto; border:1px solid var(--border); border-radius:6px;
  background:var(--surface); }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
th { font-size:11.5px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); text-align:right; padding:9px 10px;
  border-bottom:1px solid var(--grid); white-space:nowrap;
  position:sticky; top:0; background:var(--surface); z-index:1; }
th.txt { text-align:left; }
th.grp { border-left:1px solid var(--grid); }
td { padding:6px 10px; border-bottom:1px solid var(--grid);
  vertical-align:top; }
tbody tr:last-child td { border-bottom:none; }
td.test { font-weight:600; white-space:nowrap; }
td.txt { white-space:nowrap; }
.mdesc { color:var(--muted); font-size:11.5px; margin-left:7px; }
td.num { text-align:right;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12.5px; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.grp { border-left:1px solid var(--grid); }
td.err.ok { color:var(--ok-text); background:var(--ok-bg); }
td.err.warn { color:var(--warn-text); background:var(--warn-bg);
  font-weight:600; }
td.err.bad { color:var(--bad-text); background:var(--bad-bg);
  font-weight:650; }
tr.mo td { color:var(--muted); }
.src { display:inline-block; font-size:11px; color:var(--ink-2);
  border:1px solid var(--grid); border-radius:10px; padding:0 8px;
  white-space:nowrap; }
.chart-td { min-width:150px; }
th.chart-td { text-align:left; }
.spark { color:var(--accent); display:block; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
footer { color:var(--muted); font-size:12.5px; margin-top:40px;
  max-width:80ch; }
"""


def _esc(text: Any) -> str:
    return html.escape(str(text))


def _fmt_value(value: float | None) -> str:
    if value is None:
        return '—'
    if value == int(value):
        return str(int(value))
    return f'{value:.1f}'


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return '—'
    return f'{pct:+.1f}%'


def _cell_title(cell: dict[str, Any]) -> str:
    parts = [f"measured avg {_fmt_value(cell['value'])}"]
    if cell['value_min'] is not None or cell['value_max'] is not None:
        parts[0] += (f" / min {_fmt_value(cell['value_min'])}"
                     f" / max {_fmt_value(cell['value_max'])}")
    if cell['ref'] is not None:
        ref = f"ref avg {_fmt_value(cell['ref'])}"
        if cell['ref_min'] is not None or cell['ref_max'] is not None:
            ref += (f" / min {_fmt_value(cell['ref_min'])}"
                    f" / max {_fmt_value(cell['ref_max'])}")
        if cell['tol'] is not None:
            ref += f" ± {_fmt_value(cell['tol'])}"
        elif cell.get('tol_fallback'):
            ref += " (default tolerance)"
        parts.append(ref)
        if cell['ref_type']:
            parts.append(f"type {cell['ref_type']}")
    return ', '.join(parts)


def _delta_cell(cell: dict[str, Any] | None, grp: bool = False) -> str:
    cls = 'num grp' if grp else 'num'
    if cell is None:
        return f'<td class="{cls}">—</td>'
    if cell['severity'] is None:
        return (f'<td class="{cls}" '
                f'title="{_esc(_cell_title(cell))}">—</td>')
    if cell['delta_pct'] is None:
        # ref == 0: absolute delta only
        label = f"{cell['delta']:+g} abs"
    else:
        label = _fmt_pct(cell['delta_pct'])
    return (f'<td class="{cls} err {cell["severity"]}" '
            f'title="{_esc(_cell_title(cell))}">{_esc(label)}</td>')


def _spread_delta_cell(cell: dict[str, Any] | None, side: str) -> str:
    """Δ of a min/max bound against its reference counterpart.

    Display-only: coloured like the headline Δ but never counted in the
    aggregates.
    """
    if cell is None:
        return '<td class="num">—</td>'
    pct = cell.get(f'delta_{side}_pct')
    severity = cell.get(f'severity_{side}')
    if pct is None or severity is None:
        return '<td class="num">—</td>'
    return (f'<td class="num err {severity}">{_esc(_fmt_pct(pct))}</td>')


def _value_cell(cell: dict[str, Any] | None, grp: bool = True) -> str:
    cls = 'num grp' if grp else 'num'
    if cell is None:
        return f'<td class="{cls}">—</td>'
    return (f'<td class="{cls}">{_esc(_fmt_value(cell["value"]))}</td>')


def _trend_cell(cell: dict[str, Any] | None, key: str,
                grp: bool = False) -> str:
    """Improvement of |Δ| (positive = closer to the reference than before)."""
    cls = 'num grp' if grp else 'num'
    pct = cell.get(key) if cell else None
    if pct is None:
        return f'<td class="{cls}">—</td>'
    # A metric can only improve by 100% (Δ -> 0) but can worsen without
    # bound; clamp the display so one outlier does not dominate the column.
    shown = max(-999.0, pct)
    sev = 'ok' if pct > 1 else ('bad' if pct < -1 else '')
    label = f'{shown:+.0f}%' if abs(shown) >= 10 else f'{shown:+.1f}%'
    title = ('closer to the reference than before'
             if pct > 0 else 'further from the reference than before')
    return (f'<td class="{cls}{" err " + sev if sev else ""}" '
            f'title="{_esc(title)}">{_esc(label)}</td>')


def _histogram(cells: list[dict[str, Any]], default_tol_pct: float) -> str:
    pcts = [c['delta_pct'] for c in cells
            if c['severity'] is not None and c['delta_pct'] is not None]
    if not pcts:
        return ''
    bin_width = 5.0
    max_abs = max(abs(p) for p in pcts)
    limit = min(50.0, max(10.0, -(-max_abs // bin_width) * bin_width))
    nb_bins = int(2 * limit / bin_width)
    counts = [0] * nb_bins
    for pct in pcts:
        idx = int((min(max(pct, -limit), limit - 1e-9) + limit) // bin_width)
        counts[min(max(idx, 0), nb_bins - 1)] += 1
    peak = max(counts)
    cols = []
    for i, count in enumerate(counts):
        low = -limit + i * bin_width
        mid = abs(low + bin_width / 2)
        sev = ('ok' if mid <= default_tol_pct
               else 'warn' if mid <= 2 * default_tol_pct else 'bad')
        height = 4 + 96 * count / peak if count else 0
        cols.append(
            f'<div class="hcol"><div class="hbar {sev}" '
            f'style="height:{height:.0f}px" '
            f'title="{low:+g}% to {low + bin_width:+g}%: {count} metric(s)">'
            f'</div><span>{low:+g}</span></div>')
    note = ''
    if max_abs > limit:
        note = (f'Deviations beyond ±{limit:g}% are '
                f'clamped into the edge bins.')
    return (f'<div class="histo" id="histo"><h3 id="histo-title">'
            f'Distribution of Δ across '
            f'{len(pcts)} referenced metrics ({bin_width:g}% bins, '
            f'colored with the ±{default_tol_pct:g}% default bands; '
            f'tables use each metric\'s own tolerance)</h3>'
            f'<div class="hrow" id="histo-row">{"".join(cols)}</div>'
            f'<p class="note" id="histo-note">{note}</p></div>')


def _anchor(name: str) -> str:
    return ''.join(ch if ch.isalnum() else '-' for ch in name)


def _history_section(history: dict[str, Any], default_tol_pct: float) -> str:
    """Per-target calibration over runs: % within tolerance and median |Δ|.

    Drawn as inline SVG so the page stays self-contained.
    """
    runs = history['runs']
    if len(runs) < 2:
        return ''

    # target -> run_id -> [delta_pct...]
    per: dict[str, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    for (_test, target, _metric), points in history['metrics'].items():
        for p in points:
            per[target][p['run_id']].append(p['delta_pct'])

    order = [r['run_id'] for r in runs]
    labels = {r['run_id']: (r['git_commit'] or '')[:8] or f"run {r['run_id']}"
              for r in runs}

    rows = []
    with_history = []
    for target in sorted(per):
        pts = [(rid, per[target][rid]) for rid in order if per[target].get(rid)]
        if len(pts) < 2:
            continue
        with_history.append(target)
        ok_series = [100 * sum(1 for d in ds if abs(d) <= default_tol_pct)
                     / len(ds) for _, ds in pts]
        med_series = [statistics.median(map(abs, ds)) for _, ds in pts]
        first, last = ok_series[0], ok_series[-1]
        trend = last - first
        cls = 'ok' if trend > 1 else ('bad' if trend < -1 else 'warn')
        rows.append(
            f'<tr data-target="{_esc(target)}">'
            f'<td class="test">{_esc(target)}</td>'
            f'<td class="num">{len(pts)}</td>'
            f'<td class="chart-td">{_sparkline(ok_series, 0, 100)}</td>'
            f'<td class="num">{first:.0f}% → {last:.0f}%</td>'
            f'<td class="num err {cls}">{trend:+.0f} pp</td>'
            f'<td class="chart-td">{_sparkline(med_series, 0, None)}</td>'
            f'<td class="num">{med_series[0]:.1f}% → {med_series[-1]:.1f}%</td>'
            '</tr>')
    if not rows:
        return ''

    span = f"{labels[order[0]]} → {labels[order[-1]]}"
    # Tagged with the targets that actually have history, so filtering to a
    # target without any hides the section instead of showing an empty table.
    return (
        f'<section id="history" data-targets="{_esc(" ".join(with_history))}">'
        '<div class="sec-head">'
        '<h2>Calibration over time</h2>'
        f'<span class="sec-meta">{len(runs)} runs · {_esc(span)} · '
        f'a metric counts as calibrated within ±{default_tol_pct:g}%'
        '</span></div>'
        '<div class="scroll"><table><thead><tr>'
        '<th class="txt">Target</th><th>Runs</th>'
        '<th class="chart-td">Within tolerance</th><th>First → last</th>'
        '<th>Trend</th>'
        '<th class="chart-td">Median |Δ|</th><th>First → last</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')


def _sparkline(values: list[float], lo: float | None,
               hi: float | None, width: int = 150, height: int = 22) -> str:
    """Minimal inline-SVG polyline; last point emphasised."""
    if len(values) < 2:
        return ''
    vmin = lo if lo is not None else min(values)
    vmax = hi if hi is not None else max(values)
    if vmax <= vmin:
        vmax = vmin + 1
    def x(i): return i * width / (len(values) - 1)
    def y(v): return height - (min(max(v, vmin), vmax) - vmin) / (vmax - vmin) * height
    pts = ' '.join(f'{x(i):.1f},{y(v):.1f}' for i, v in enumerate(values))
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="currentColor" '
            f'stroke-width="1.5"/>'
            f'<circle cx="{x(len(values)-1):.1f}" cy="{y(values[-1]):.1f}" '
            f'r="2.5" fill="currentColor"/></svg>')


def _improve_cell(cell: dict[str, Any] | None, grp: bool = False) -> str:
    """Per-metric accuracy improvement vs the baseline, in pp of |Δ| removed
    (positive = closer to the reference than the baseline was)."""
    cls = 'num grp' if grp else 'num'
    pp = cell.get('improve_pp') if cell else None
    if pp is None:
        return f'<td class="{cls}">—</td>'
    sev = 'ok' if pp > 0.05 else ('bad' if pp < -0.05 else '')
    eb, en = cell.get('improve_err_base'), cell.get('improve_err_now')
    title = (f'|Δ| {eb:.1f}% → {en:.1f}% vs baseline'
             if eb is not None and en is not None else 'vs baseline')
    return (f'<td class="{cls}{" err " + sev if sev else ""}" '
            f'title="{_esc(title)}">{pp:+.1f} pp</td>')


def _render_cluster(cluster: dict[str, Any], show_improve: bool = False) -> str:
    targets = cluster['targets']
    stats = cluster['stats']

    window_days = next((c['trend_window_days'] for row in cluster['rows']
                        for c in row['cells']
                        if c and c.get('trend_window_days')), 30)
    trend_prev_hdr = 'Impr. vs prev'
    trend_win_hdr = f'Impr. vs {window_days}d avg'

    head_meta = f"{cluster['n_tests']} test(s) · {stats['n_total']} metric(s)"
    if stats['n_referenced']:
        head_meta += (f" · {stats['pct_ok']:.0f}% within tolerance"
                      f" · ref types: {', '.join(stats['ref_types'])}")
    else:
        head_meta += " · measured-only (no references)"

    title = ' · '.join(targets)
    anchor = _anchor('sec-' + '-'.join(targets))

    # Metrics sampled over several activations (e.g. ACU per-filter cycles)
    # carry a min/max spread; show it for both sides when present. The
    # headline value stays the average, which is what Δ is computed on.
    spread = any(
        c is not None and (c['value_min'] is not None
                           or c['ref_min'] is not None)
        for row in cluster['rows'] for c in row['cells'])

    out = [f'<section id="{anchor}" '
           f'data-targets="{_esc(" ".join(targets))}"><div class="sec-head">'
           f'<h2>{_esc(title)}</h2>'
           f'<span class="sec-meta">{_esc(head_meta)}</span></div>'
           f'<div class="scroll"><table><thead><tr>'
           f'<th class="txt">Test</th><th class="txt">Metric</th>']
    # When a baseline was given, a per-metric "accuracy vs baseline" column
    # leads the movement group (so the trend columns drop their own border).
    impr_hdr = '<th class="grp">Δ acc vs base</th>' if show_improve else ''
    trend_prev_grp = '' if show_improve else ' class="grp"'
    if spread:
        # Deltas first: they are what the reader scans. Then how |Δ| is
        # moving, then the raw numbers behind it.
        out.append('<th class="grp">Δ avg</th><th>Δ min</th><th>Δ max</th>'
                   f'{impr_hdr}'
                   f'<th{trend_prev_grp}>{_esc(trend_prev_hdr)}</th>'
                   f'<th>{_esc(trend_win_hdr)}</th>'
                   '<th class="grp">Ref avg</th><th>Ref min</th>'
                   '<th>Ref max</th>'
                   '<th class="grp">Meas avg</th><th>Meas min</th>'
                   '<th>Meas max</th>'
                   '</tr></thead><tbody>')
    else:
        out.append(f'<th class="grp">Δ</th>'
                   f'{impr_hdr}'
                   f'<th{trend_prev_grp}>{_esc(trend_prev_hdr)}</th>'
                   f'<th>{_esc(trend_win_hdr)}</th>'
                   f'<th class="grp">Ref</th><th>Measured</th>'
                   '</tr></thead><tbody>')

    prev_test = None
    for row in cluster['rows']:
        cells = row['cells']
        cell0 = cells[0]
        ref_txt = ('—' if cell0 is None or cell0['ref'] is None
                   else _fmt_value(cell0['ref']))
        ref_title = ('' if cell0 is None
                     else f' title="{_esc(_cell_title(cell0))}"')
        row_class = '' if row['referenced'] else ' class="mo"'
        test_txt = '' if row['test'] == prev_test else _esc(row['test'])
        prev_test = row['test']
        out.append(f'<tr{row_class}><td class="test">{test_txt}</td>'
                   f'<td class="txt">{_esc(row["metric"])}'
                   f'<span class="mdesc">{_esc(row["desc"])}</span></td>')
        if spread:
            cell = cells[0]
            title = f' title="{_esc(_cell_title(cell))}"' if cell else ''
            # Δ avg (drives severity/stats), then the two spread bounds.
            out.append(_delta_cell(cell, grp=True))
            for side in ('min', 'max'):
                out.append(_spread_delta_cell(cell, side))
            if show_improve:
                out.append(_improve_cell(cell, grp=True))
            out.append(_trend_cell(cell, 'trend_prev_pct',
                                   grp=not show_improve))
            out.append(_trend_cell(cell, 'trend_window_pct'))
            for grp, key in ((' grp', 'ref'), ('', 'ref_min'), ('', 'ref_max'),
                             (' grp', 'value'), ('', 'value_min'),
                             ('', 'value_max')):
                value = cell[key] if cell else None
                out.append(f'<td class="num{grp}"{title}>'
                           f'{_esc(_fmt_value(value))}</td>')
        else:
            out.append(_delta_cell(cells[0], grp=True))
            if show_improve:
                out.append(_improve_cell(cells[0], grp=True))
            out.append(_trend_cell(cells[0], 'trend_prev_pct',
                                   grp=not show_improve))
            out.append(_trend_cell(cells[0], 'trend_window_pct'))
            out.append(f'<td class="num grp"{ref_title}>{_esc(ref_txt)}</td>')
            out.append(_value_cell(cells[0], grp=False))
        out.append('</tr>')
    out.append('</tbody></table></div></section>')
    return ''.join(out)


# Client-side target filter: chips at the top of the page narrow every
# section, the scoreboard, the summary tiles and the histogram down to one
# target. The aggregates are precomputed here; only the histogram is
# re-binned in the browser (same binning as _histogram above). Without
# JavaScript the page simply stays in its all-targets state.
_JS = """
(function () {
  'use strict';
  var dataEl = document.getElementById('calib-data');
  if (!dataEl) return;
  var data = JSON.parse(dataEl.textContent);
  var chips = Array.prototype.slice.call(
      document.querySelectorAll('.filters button'));

  function fmtPct(v) {
    if (v === null || v === undefined) return '\\u2014';
    return (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
  }

  function renderHisto(t) {
    var box = document.getElementById('histo');
    if (!box) return;
    var row = document.getElementById('histo-row');
    var title = document.getElementById('histo-title');
    var note = document.getElementById('histo-note');
    var pcts = data.metrics.filter(function (m) {
      return !t || m[0] === t;
    }).map(function (m) { return m[1]; });
    if (!pcts.length) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    var bw = 5;
    var maxAbs = Math.max.apply(null, pcts.map(Math.abs));
    var limit = Math.min(50, Math.max(10, Math.ceil(maxAbs / bw) * bw));
    var nb = Math.round(2 * limit / bw);
    var counts = new Array(nb).fill(0);
    pcts.forEach(function (p) {
      var i = Math.floor(
          (Math.min(Math.max(p, -limit), limit - 1e-9) + limit) / bw);
      counts[Math.min(Math.max(i, 0), nb - 1)] += 1;
    });
    var peak = Math.max.apply(null, counts);
    var out = '';
    for (var i = 0; i < nb; i++) {
      var low = -limit + i * bw;
      var mid = Math.abs(low + bw / 2);
      var sev = mid <= data.tolPct ? 'ok'
          : (mid <= 2 * data.tolPct ? 'warn' : 'bad');
      var h = counts[i] ? 4 + 96 * counts[i] / peak : 0;
      var lo = (low >= 0 ? '+' : '') + low;
      var hi = (low + bw >= 0 ? '+' : '') + (low + bw);
      out += '<div class="hcol"><div class="hbar ' + sev +
          '" style="height:' + h.toFixed(0) + 'px" title="' + lo +
          '% to ' + hi + '%: ' + counts[i] + ' metric(s)"></div><span>' +
          lo + '</span></div>';
    }
    row.innerHTML = out;
    title.textContent = 'Distribution of \\u0394 across ' + pcts.length +
        ' referenced metrics (' + bw + '% bins, colored with the \\u00b1' +
        data.tolPct + '% default bands; tables use each metric\\'s own ' +
        'tolerance)' + (t ? ' \\u2014 ' + t : '');
    if (note) {
      note.textContent = maxAbs > limit ?
          'Deviations beyond \\u00b1' + limit + '% are clamped into the ' +
          'edge bins.' : '';
    }
  }

  function apply(t) {
    chips.forEach(function (c) {
      c.setAttribute('aria-pressed',
          String((c.getAttribute('data-target') || '') === t));
    });
    document.querySelectorAll('section[data-targets]').forEach(
        function (s) {
          var ts = s.getAttribute('data-targets').split(' ');
          s.classList.toggle('hidden', !!t && ts.indexOf(t) < 0);
        });
    document.querySelectorAll('th[data-target], td[data-target]').forEach(
        function (el) {
          el.classList.toggle('hidden',
              !!t && el.getAttribute('data-target') !== t);
        });
    document.querySelectorAll('tr[data-target]').forEach(function (r) {
      r.classList.toggle('hidden',
          !!t && r.getAttribute('data-target') !== t);
    });
    var a = data.aggregates[t] || data.aggregates[''];
    document.getElementById('t-acc').innerHTML = a.accuracy === null ?
        '\\u2014' : a.accuracy.toFixed(1) + '<small>%</small>';
    document.getElementById('t-nacc').textContent = a.nAccuracy;
    document.getElementById('t-ref').textContent = a.referenced;
    document.getElementById('t-total').textContent = a.total;
    document.getElementById('t-ok').innerHTML = a.pctOk === null ?
        '\\u2014' : Math.round(a.pctOk) + '<small>%</small>';
    document.getElementById('t-median').textContent = fmtPct(a.median);
    document.getElementById('t-worst').textContent = fmtPct(a.worst);
    document.getElementById('t-worst-k').textContent =
        'worst \\u0394' + (a.worstLabel ? ' (' + a.worstLabel + ')' : '');
    renderHisto(t);
    if (history.replaceState) {
      history.replaceState(null, '', location.pathname + location.search +
          (t ? '#target=' + encodeURIComponent(t) : ''));
    }
  }

  chips.forEach(function (c) {
    c.addEventListener('click', function () {
      apply(c.getAttribute('data-target') || '');
    });
  });

  var m = /^#target=(.+)$/.exec(location.hash);
  if (m) {
    var t = decodeURIComponent(m[1]);
    if (data.aggregates[t]) apply(t);
  }
})();
"""


def build_history(
    series: dict[Key, list[dict[str, Any]]],
    baseline: dict[Key, dict[str, Any]] | None = None,
    default_tol_pct: float = 5.0,
) -> dict[str, Any]:
    """Δ of every metric across runs, so calibration drift is visible.

    Each run's value is compared against the reference that applies to it:
    the reference stored on that row, or — in baseline mode — the baseline
    run's value. (An RTL baseline is reused across the whole series: the
    RTL only moves when the RTL itself does, so pairing every gvsoc run
    against the latest RTL run is what you want.)

    Returns {'runs': [...], 'metrics': {key: [{run_id, delta_pct, ...}]}}.
    """
    runs: dict[int, dict[str, Any]] = {}
    metrics: dict[Key, list[dict[str, Any]]] = {}

    for key, entries in series.items():
        base = baseline.get(key) if baseline else None
        points = []
        for e in entries:
            ref = base['value'] if base is not None else e['ref']
            if ref is None or ref == 0:
                continue
            delta_pct = (e['value'] - ref) / ref * 100
            points.append({
                'run_id': e['run_id'],
                'timestamp': e['timestamp'],
                'git_commit': e['git_commit'],
                'value': e['value'],
                'ref': ref,
                'delta_pct': delta_pct,
                'severity': _severity_of(e['value'], ref, e['tol'],
                                         default_tol_pct),
            })
            runs.setdefault(e['run_id'], {
                'run_id': e['run_id'], 'timestamp': e['timestamp'],
                'git_commit': e['git_commit'], 'platform': e['platform']})
        if points:
            metrics[key] = points

    return {
        'runs': sorted(runs.values(), key=lambda r: (r['timestamp'], r['run_id'])),
        'metrics': metrics,
    }


def _improvement_pct(before: float | None, after: float) -> float | None:
    """How much |Δ| shrank, in percent of where it was.

    Positive = the metric moved closer to its reference. None when there
    is nothing to compare against, or when `before` was already zero (any
    change from a perfect match is unbounded in relative terms).
    """
    if before is None or before == 0:
        return None
    return (before - after) / before * 100


def annotate_trends(model: dict[str, Any], history: dict[str, Any],
                    window_days: int = 30) -> None:
    """Attach per-cell calibration trends in place.

    trend_prev_pct: improvement of |Δ| against the previous run.
    trend_window_pct: improvement of |Δ| against the mean |Δ| of the runs
    in the `window_days` before the current one (the recent norm).
    """
    for cell in model['cells']:
        cell['trend_prev_pct'] = None
        cell['trend_window_pct'] = None
        cell['trend_window_days'] = window_days
        points = history['metrics'].get(
            (cell['test'], cell['target'], cell['metric'])) or []
        if len(points) < 2 or cell['delta_pct'] is None:
            continue
        now_abs = abs(points[-1]['delta_pct'])
        cell['trend_prev_pct'] = _improvement_pct(
            abs(points[-2]['delta_pct']), now_abs)

        try:
            cutoff = (datetime.fromisoformat(points[-1]['timestamp'])
                      - timedelta(days=window_days))
        except (TypeError, ValueError):
            continue
        window = []
        for p in points[:-1]:
            try:
                if datetime.fromisoformat(p['timestamp']) >= cutoff:
                    window.append(abs(p['delta_pct']))
            except (TypeError, ValueError):
                continue
        if window:
            cell['trend_window_pct'] = _improvement_pct(
                statistics.mean(window), now_abs)


def find_regressions(history: dict[str, Any], threshold_pp: float = 2.0
                     ) -> list[dict[str, Any]]:
    """Metrics whose |Δ| grew by more than threshold_pp since the run before.

    Only compares the two most recent runs that carry the metric, so a
    metric absent from the latest run is not reported as a regression.
    """
    out = []
    for (test, target, metric), points in history['metrics'].items():
        if len(points) < 2:
            continue
        prev, last = points[-2], points[-1]
        drift = abs(last['delta_pct']) - abs(prev['delta_pct'])
        if drift > threshold_pp:
            out.append({
                'test': test, 'target': target, 'metric': metric,
                'from_pct': prev['delta_pct'], 'to_pct': last['delta_pct'],
                'drift_pp': drift,
                'from_commit': prev['git_commit'],
                'to_commit': last['git_commit'],
            })
    out.sort(key=lambda r: -r['drift_pp'])
    return out


def build_improvement(rows: dict[Key, dict[str, Any]],
                      baseline: dict[Key, dict[str, Any]],
                      flat_eps: float = 0.5) -> dict[str, Any] | None:
    """Per-metric accuracy change of `rows` relative to a `baseline` run.

    For every metric that carries a declared reference (any type) and appears
    in both runs, compares |Δ%| now against |Δ%| at the baseline. `improve_pp
    > 0` means the measured value moved closer to the reference — the change
    improved accuracy on that metric. Aggregates into a net score change and
    improved/regressed/flat counts. Returns None when nothing is comparable
    (no shared referenced metric).
    """
    items = []
    for key, cell in rows.items():
        ref = cell.get('declared_ref')
        if ref in (None, 0):
            continue
        base = baseline.get(key)
        if base is None or base.get('value') is None or cell.get('value') is None:
            continue
        err_now = abs(cell['value'] - ref) / abs(ref) * 100
        err_base = abs(base['value'] - ref) / abs(ref) * 100
        items.append({
            'test': key[0], 'target': key[1], 'metric': key[2], 'ref': ref,
            'value_base': base['value'], 'value_now': cell['value'],
            'err_base': err_base, 'err_now': err_now,
            'improve_pp': err_base - err_now,
        })
    if not items:
        return None
    return {
        'items': items,
        'n': len(items),
        'improved': [i for i in items if i['improve_pp'] > flat_eps],
        'regressed': [i for i in items if i['improve_pp'] < -flat_eps],
        'flat': [i for i in items if abs(i['improve_pp']) <= flat_eps],
        'mean_before': statistics.mean(i['err_base'] for i in items),
        'mean_after': statistics.mean(i['err_now'] for i in items),
        # + = mean absolute error shrank, i.e. accuracy improved overall.
        'net_pp': (statistics.mean(i['err_base'] for i in items)
                   - statistics.mean(i['err_now'] for i in items)),
        'gains': sorted(items, key=lambda i: -i['improve_pp']),
    }


def annotate_improvement(model: dict[str, Any],
                         improvement: dict[str, Any] | None) -> None:
    """Attach each metric's baseline improvement to its cell in place, so the
    detail tables can show a per-test "Δ acc vs base" column."""
    if not improvement:
        return
    by_key = {(i['test'], i['target'], i['metric']): i
              for i in improvement['items']}
    for cell in model['cells']:
        item = by_key.get((cell['test'], cell['target'], cell['metric']))
        if item is not None:
            cell['improve_pp'] = item['improve_pp']
            cell['improve_err_base'] = item['err_base']
            cell['improve_err_now'] = item['err_now']


def suggest_ratchet(cells: list[dict[str, Any]], headroom: float = 0.5,
                    margin: float = 2.0, drift_min_pct: float = 2.0
                    ) -> dict[str, Any]:
    """Proposals to lock in current accuracy so future regressions get caught.

    tighten: a ground-truth metric sitting well inside its tolerance
      (|Δ| < headroom·tol) can take a tighter tolerance; the suggestion keeps
      `margin`× the current |Δ| as slack.
    rebaseline: a 'measured' lock that drifted (|Δ| ≥ drift_min_pct of the
      reference) yet stays within tolerance can re-centre its reference on the
      current value, so the lock tracks the model instead of lagging it.

    Advisory only — returns the proposals; editing the testset.cfg files is a
    human decision.
    """
    tighten, rebase = [], []
    for c in cells:
        ref = c.get('declared_ref')
        rtype = c.get('declared_ref_type')
        val = c.get('value')
        tol = c.get('tol')
        if ref in (None, 0) or val is None:
            continue
        delta = abs(val - ref)
        if rtype in GROUND_TRUTH:
            if tol and tol > 0 and delta < headroom * tol:
                new_tol = max(int(math.ceil(margin * delta)), 1)
                if new_tol < tol:
                    tighten.append({
                        'test': c['test'], 'target': c['target'],
                        'metric': c['metric'], 'tol': tol, 'new_tol': new_tol,
                        'delta': delta, 'ref_type': rtype})
        elif rtype == 'measured' and tol is not None:
            if (delta <= tol and val != ref
                    and delta / abs(ref) * 100 >= drift_min_pct):
                rebase.append({
                    'test': c['test'], 'target': c['target'],
                    'metric': c['metric'], 'ref': ref, 'new_ref': val,
                    'drift_pct': (val - ref) / abs(ref) * 100})
    tighten.sort(key=lambda r: (r['tol'] - r['new_tol']) / r['tol'], reverse=True)
    rebase.sort(key=lambda r: -abs(r['drift_pct']))
    return {'tighten': tighten, 'rebaseline': rebase}


def _client_data(model: dict[str, Any]) -> dict[str, Any]:
    """Compact payload for the client-side target filter."""
    def agg(stats: dict[str, Any]) -> dict[str, Any]:
        worst = stats['worst']
        return {
            'total': stats['n_total'],
            'referenced': stats['n_referenced'],
            'pctOk': stats['pct_ok'],
            'median': stats['median_pct'],
            'worst': worst['delta_pct'] if worst else None,
            'worstLabel': (f"{worst['target']} "
                           f"{worst['test']}:{worst['metric']}"
                           if worst else ''),
            'accuracy': stats['accuracy'],
            'nAccuracy': stats['n_accuracy'],
        }

    aggregates = {'': agg(model['global'])}
    for ts in model['targets']:
        aggregates[ts['target']] = agg(ts)
    metrics = [[c['target'], round(c['delta_pct'], 3)]
               for c in model['cells']
               if c['severity'] is not None and c['delta_pct'] is not None]
    return {'tolPct': model['default_tol_pct'],
            'aggregates': aggregates,
            'metrics': metrics}


def _improvement_section(imp: dict[str, Any] | None,
                         baseline_label: str) -> str:
    """Accuracy change of this run vs a baseline run, over ground-truth refs.

    A headline net figure (mean |Δ| before → after) plus the metrics that
    moved most in each direction — the "did my change help, where?" view.
    """
    if not imp:
        return ''
    net = imp['net_pp']
    cls = 'ok' if net > 0.05 else ('bad' if net < -0.05 else 'warn')
    verdict = ('improved' if net > 0.05
               else 'regressed' if net < -0.05 else 'unchanged')
    color = {'ok': 'var(--ok-text)', 'bad': 'var(--bad-text)',
             'warn': 'var(--ink)'}[cls]

    gains = [i for i in imp['gains'] if i['improve_pp'] > 0.5][:15]
    losses = [i for i in reversed(imp['gains']) if i['improve_pp'] < -0.5][:15]
    shown = gains + losses

    def _rows(items: list[dict[str, Any]]) -> str:
        out = []
        for i in items:
            sev = 'ok' if i['improve_pp'] > 0 else 'bad'
            out.append(
                f'<tr><td class="test">{_esc(i["target"])}</td>'
                f'<td class="txt">{_esc(i["test"])}:{_esc(i["metric"])}</td>'
                f'<td class="num err {sev}">{i["improve_pp"]:+.1f} pp</td>'
                f'<td class="num">{i["err_base"]:.1f}% → {i["err_now"]:.1f}%</td>'
                f'<td class="num">{_esc(_fmt_value(i["value_base"]))} → '
                f'{_esc(_fmt_value(i["value_now"]))}</td>'
                f'<td class="num">{_esc(_fmt_value(i["ref"]))}</td></tr>')
        return ''.join(out)

    table = ''
    if shown:
        table = (
            '<div class="scroll"><table><thead><tr>'
            '<th class="txt">Target</th><th class="txt">Metric</th>'
            '<th>Δ accuracy</th><th>|Δ| before → after</th>'
            '<th>Measured before → after</th><th>Ref</th>'
            f'</tr></thead><tbody>{_rows(shown)}</tbody></table></div>')

    return (
        '<section id="improvement"><div class="sec-head">'
        '<h2>Accuracy vs baseline</h2>'
        f'<span class="sec-meta">baseline {_esc(baseline_label)} · '
        f'{imp["n"]} referenced metric(s) · positive Δ accuracy = closer '
        'to the reference</span></div>'
        '<div class="strip">'
        f'<div class="stat"><div class="v" style="color:{color}">{net:+.2f}'
        '<small> pp</small></div>'
        f'<div class="k">net accuracy — mean |Δ| {imp["mean_before"]:.1f}% → '
        f'{imp["mean_after"]:.1f}% ({verdict})</div></div>'
        f'<div class="stat"><div class="v">{len(imp["improved"])}</div>'
        '<div class="k">metrics improved</div></div>'
        f'<div class="stat"><div class="v">{len(imp["regressed"])}</div>'
        '<div class="k">metrics regressed</div></div>'
        f'<div class="stat"><div class="v">{len(imp["flat"])}</div>'
        '<div class="k">unchanged</div></div>'
        '</div>' + table + '</section>')


def render_html(model: dict[str, Any], title: str,
                history: dict[str, Any] | None = None,
                improvement: dict[str, Any] | None = None,
                improvement_baseline: str = '') -> str:
    g = model['global']
    default_tol_pct = model['default_tol_pct']

    runs_txt = ' · '.join(
        f"run {run_id} ({platform}, {timestamp[:19]}"
        + (f", {commit[:9]}" if commit else "") + ")"
        for run_id, platform, timestamp, commit in model['runs'])
    if model['baseline_label']:
        runs_txt += f" · reference: {model['baseline_label']}"

    worst = g['worst']
    worst_txt = '—'
    worst_sub = 'worst |Δ| deviation'
    if worst is not None:
        worst_txt = _fmt_pct(worst['delta_pct'])
        worst_sub = (f"worst Δ ({worst['target']} "
                     f"{worst['test']}:{worst['metric']})")

    target_names = [ts['target'] for ts in model['targets']]
    filters_html = (
        '<div class="filters" role="group" aria-label="Filter by target">'
        '<button type="button" data-target="" aria-pressed="true">'
        'All targets</button>'
        + ''.join(f'<button type="button" data-target="{_esc(t)}" '
                  f'aria-pressed="false">{_esc(t)}</button>'
                  for t in target_names)
        + '</div>')

    acc_txt = (f"{g['accuracy']:.1f}<small>%</small>"
               if g['accuracy'] is not None else '—')

    stats_html = f"""
{filters_html}
<div class="strip">
  <div class="stat"><div class="v" id="t-acc">{acc_txt}</div>
    <div class="k">accuracy — mean |Δ| vs reference
      (<span id="t-nacc">{g['n_accuracy']}</span> metrics; lower is better)</div></div>
  <div class="stat"><div class="v"><span id="t-ref">{g['n_referenced']}\
</span><small> / <span id="t-total">{g['n_total']}</span></small></div>
    <div class="k">metrics with a reference / total</div></div>
  <div class="stat"><div class="v" id="t-ok">{
    f"{g['pct_ok']:.0f}<small>%</small>" if g['pct_ok'] is not None
    else '—'}</div>
    <div class="k">within tolerance</div></div>
  <div class="stat"><div class="v" id="t-median">{_esc(_fmt_pct(g['median_pct']))}</div>
    <div class="k">median Δ (signed)</div></div>
  <div class="stat"><div class="v" id="t-worst">{_esc(worst_txt)}</div>
    <div class="k" id="t-worst-k">{_esc(worst_sub)}</div></div>
</div>
<div class="legend">
  <span><i style="background:var(--ok)"></i>within tolerance</span>
  <span><i style="background:var(--warn)"></i>≤ 2× tolerance</span>
  <span><i style="background:var(--bad)"></i>&gt; 2× tolerance</span>
  <span><i style="background:var(--muted)"></i>measured-only (no reference)</span>
</div>
"""

    scoreboard = ['<section><div class="sec-head"><h2>Per-target scoreboard'
                  '</h2></div><div class="scroll"><table><thead><tr>'
                  '<th class="txt">Target</th><th class="txt">Ref types</th>'
                  '<th>Metrics</th><th>Referenced</th><th>Within tol</th>'
                  '<th>Accuracy</th><th>Median Δ</th><th>Worst Δ</th>'
                  '</tr></thead><tbody>']
    coverage = []
    for ts in model['targets']:
        if ts['n_referenced'] == 0:
            coverage.append(ts)
            continue
        target_anchor = next(
            (_anchor('sec-' + '-'.join(c['targets']))
             for c in model['clusters'] if ts['target'] in c['targets']), '')
        worst = ts['worst']
        acc_cell = ('—' if ts['accuracy'] is None
                    else f"{ts['accuracy']:.1f}%")
        scoreboard.append(
            f'<tr data-target="{_esc(ts["target"])}">'
            f'<td class="test"><a href="#{target_anchor}">'
            f'{_esc(ts["target"])}</a></td>'
            f'<td class="txt">' + ' '.join(
                f'<span class="src">{_esc(s)}</span>'
                for s in ts['ref_types']) + '</td>'
            f'<td class="num">{ts["n_total"]}</td>'
            f'<td class="num">{ts["n_referenced"]}</td>'
            f'<td class="num">{ts["pct_ok"]:.0f}%</td>'
            f'<td class="num">{_esc(acc_cell)}</td>'
            f'<td class="num">{_esc(_fmt_pct(ts["median_pct"]))}</td>'
            f'<td class="num">'
            f'{_esc(_fmt_pct(worst["delta_pct"]) if worst else "—")}</td>'
            '</tr>')
    scoreboard.append('</tbody></table></div></section>')

    coverage_html = ''
    if coverage:
        rows = ''.join(
            f'<tr class="mo" data-target="{_esc(ts["target"])}">'
            f'<td class="test">{_esc(ts["target"])}</td>'
            f'<td class="num">{ts["n_total"]}</td></tr>' for ts in coverage)
        coverage_html = (
            '<section id="coverage" data-targets="'
            + _esc(' '.join(ts['target'] for ts in coverage)) + '">'
            '<div class="sec-head"><h2>Coverage gaps</h2>'
            '<span class="sec-meta">targets with measured values but no '
            'reference numbers yet</span></div>'
            '<div class="scroll"><table><thead><tr>'
            '<th class="txt">Target</th><th>Measured metrics</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div></section>')

    sections = ''.join(_render_cluster(c, show_improve=improvement is not None)
                       for c in model['clusters'])

    client_json = json.dumps(_client_data(model)).replace('</', '<\\/')

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style></head><body>
<main>
<p class="eyebrow">gvtest · calibration report</p>
<h1>{_esc(title)}</h1>
<p class="sub">Benchmark values measured by gvtest, compared against each
metric's reference number (provenance shown per section: RTL-measured,
analytical, or a baseline run). Δ is the deviation of the measured value
from its reference; each metric is judged against its own declared
tolerance (±{default_tol_pct:g}% default when none is declared).</p>
<p class="runs">{_esc(runs_txt)}</p>
{stats_html}
{_histogram(model['cells'], default_tol_pct)}
{''.join(scoreboard)}
{_improvement_section(improvement, improvement_baseline)}
{_history_section(history, default_tol_pct) if history else ''}
{sections}
{coverage_html}
<footer>Generated by <code>gvtest.bench.calibration</code> from the gvtest
bench database. References are declared in testset.cfg files via
<code>add_bench(..., ref=, tol=/tol_pct=, ref_type=)</code>, or taken
from a baseline run (<code>--ref-platform</code>/<code>--ref-run</code>).
</footer>
</main>
<script type="application/json" id="calib-data">{client_json}</script>
<script>{_JS}</script>
</body></html>
"""


# ---------------------------------------------------------------------- CLI

def _print_summary(model: dict[str, Any], output: str,
                   improvement: dict[str, Any] | None = None,
                   improvement_label: str = '') -> None:
    """Console summary printed on every run: the accuracy score front and
    centre, and — when a baseline was given — the accuracy change with it."""
    g = model['global']
    bar = '=' * 64
    print()
    print(bar)
    print('  CALIBRATION SUMMARY')
    print(bar)
    if g['accuracy'] is not None:
        print(f"  Accuracy score:   {g['accuracy']:7.2f} %   "
              f"(mean |Δ| vs reference over {g['n_accuracy']} metric(s), "
              f"lower is better)")
    else:
        print("  Accuracy score:       n/a   (no referenced metrics)")
    within = (f" · {g['pct_ok']:.0f}% within tolerance"
              if g['pct_ok'] is not None else '')
    print(f"  Coverage:         {g['n_referenced']:>7} / {g['n_total']} "
          f"metrics referenced{within}")
    if improvement is not None:
        net = improvement['net_pp']
        verdict = ('IMPROVED' if net > 0.05 else
                   'REGRESSED' if net < -0.05 else 'UNCHANGED')
        print('-' * 64)
        print(f"  Accuracy change:  {improvement['mean_before']:6.2f} % -> "
              f"{improvement['mean_after']:6.2f} %   "
              f"{verdict} by {abs(net):.2f} pp")
        print(f"  vs baseline {improvement_label}:  "
              f"{len(improvement['improved'])} improved · "
              f"{len(improvement['regressed'])} regressed · "
              f"{len(improvement['flat'])} flat")
    print(bar)
    print(f"  Report: {output}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Generate a calibration report (measured vs reference) '
                    'from a gvtest bench database')
    parser.add_argument('--db', required=True, help='SQLite bench database')
    parser.add_argument('--output', required=True,
                        help='Output HTML file path')
    parser.add_argument('--title', default='GVSoC calibration — measured vs reference',
                        help='Report title')
    parser.add_argument('--run', type=int, default=None,
                        help='Restrict to a single run id')
    parser.add_argument('--platform', default=None,
                        help='Restrict to runs of this platform')
    parser.add_argument('--ref-run', type=int, default=None,
                        help='Use this run id as the reference baseline')
    parser.add_argument('--ref-platform', default=None,
                        help='Use the latest run of this platform as the '
                             'reference baseline')
    parser.add_argument('--test', default=None,
                        help='Filter tests (glob, e.g. "spatz:*")')
    parser.add_argument('--target', default=None,
                        help='Filter targets (glob)')
    parser.add_argument('--default-tol-pct', type=float, default=5.0,
                        help='Tolerance band (%%) for metrics with a '
                             'reference but no declared tolerance '
                             '(default: %(default)s)')
    parser.add_argument('--no-history', action='store_true',
                        help='Skip the "calibration over time" section and '
                             'the per-metric trend columns even when the '
                             'database holds several runs')
    parser.add_argument('--trend-window-days', type=int, default=30,
                        help='Window (days) the per-metric "improvement vs '
                             'average" trend compares against '
                             '(default: %(default)s)')
    parser.add_argument('--check', action='store_true',
                        help='Also report metrics whose |delta| grew since '
                             'the previous run, and exit non-zero if any '
                             '(regression gate for CI)')
    parser.add_argument('--check-threshold-pp', type=float, default=2.0,
                        help='How much |delta| may grow (percentage points) '
                             'before --check calls it a regression '
                             '(default: %(default)s)')
    parser.add_argument('--baseline-run', type=int, default=None,
                        help='Compare accuracy against this run id (A/B): how '
                             'much each ground-truth metric moved toward its '
                             'reference vs this baseline. Adds an "Accuracy vs '
                             'baseline" section and prints a net summary')
    parser.add_argument('--baseline-platform', default=None,
                        help='Like --baseline-run but uses the latest run of '
                             'this platform as the accuracy baseline')
    parser.add_argument('--require-improvement', action='store_true',
                        help='Exit non-zero unless overall accuracy improved '
                             'vs the baseline (the symmetric counterpart of '
                             '--check; needs --baseline-run/-platform)')
    parser.add_argument('--suggest-ratchet', action='store_true',
                        help='Print advisory proposals to lock in the current '
                             'accuracy: tighten tolerances with spare headroom '
                             'and re-baseline drifted measured locks')
    args = parser.parse_args()

    from gvtest.bench.db import init_db
    conn = init_db(args.db)

    baseline = None
    baseline_label = None
    exclude_platform = None
    if args.ref_run is not None or args.ref_platform is not None:
        baseline = query_results(conn, run=args.ref_run,
                                 platform=args.ref_platform,
                                 test=args.test, target=args.target)
        baseline_label = (f'run:{args.ref_run}' if args.ref_run is not None
                          else f'platform:{args.ref_platform}')
        # Keep the baseline out of the measured set unless the user
        # explicitly selected a platform to report on.
        if args.ref_platform is not None and args.platform is None:
            exclude_platform = args.ref_platform

    rows = query_results(conn, run=args.run, platform=args.platform,
                         test=args.test, target=args.target,
                         exclude_platform=exclude_platform)

    # A/B accuracy baseline: an earlier run to measure improvement against.
    # Independent of --ref-*: the references stay the declared ground truth;
    # this only supplies the "before" values.
    improvement_rows = None
    improvement_label = ''
    if args.baseline_run is not None or args.baseline_platform is not None:
        improvement_rows = query_results(
            conn, run=args.baseline_run, platform=args.baseline_platform,
            test=args.test, target=args.target)
        improvement_label = (f'run {args.baseline_run}'
                             if args.baseline_run is not None
                             else f'latest {args.baseline_platform}')

    history = None
    if not args.no_history and args.run is None:
        history = build_history(
            query_history(conn, platform=args.platform, test=args.test,
                          target=args.target,
                          exclude_platform=exclude_platform),
            baseline=baseline, default_tol_pct=args.default_tol_pct)
    conn.close()

    if not rows:
        print('No benchmark results match the given filters', file=sys.stderr)
        return 1

    model = build_model(rows, baseline=baseline,
                        baseline_label=baseline_label,
                        default_tol_pct=args.default_tol_pct)
    if history is not None:
        annotate_trends(model, history, window_days=args.trend_window_days)

    improvement = (build_improvement(rows, improvement_rows)
                   if improvement_rows else None)
    annotate_improvement(model, improvement)

    with open(args.output, 'w') as output_file:
        output_file.write(render_html(
            model, args.title, history=history,
            improvement=improvement, improvement_baseline=improvement_label))

    _print_summary(model, args.output, improvement, improvement_label)

    rc = 0

    if improvement is not None:
        movers = sorted((i for i in improvement['gains']
                         if abs(i['improve_pp']) > 0.5),
                        key=lambda i: -abs(i['improve_pp']))[:12]
        if movers:
            print('  Biggest movers (Δ accuracy · |Δ| before → after):')
            for i in movers:
                print(f"    {i['improve_pp']:+7.1f} pp  {i['err_base']:6.1f}% "
                      f"-> {i['err_now']:6.1f}%  {i['target']} "
                      f"{i['test']}:{i['metric']}")
            print()

    if args.suggest_ratchet:
        ratchet = suggest_ratchet(model['cells'])
        tighten, rebase = ratchet['tighten'], ratchet['rebaseline']
        if not tighten and not rebase:
            print('\nRatchet: nothing to tighten or re-baseline — every '
                  'metric already sits near its tolerance/reference.')
        else:
            print(f'\nRatchet: {len(tighten)} tolerance(s) to tighten, '
                  f'{len(rebase)} measured lock(s) to re-baseline:')
            for t in tighten[:40]:
                print(f"  tighten     tol {t['tol']:g} -> {t['new_tol']:g}  "
                      f"(|Δ|={t['delta']:g}, {t['ref_type']})  {t['target']} "
                      f"{t['test']}:{t['metric']}")
            for r in rebase[:40]:
                print(f"  re-baseline ref {r['ref']:g} -> {r['new_ref']:g}  "
                      f"({r['drift_pct']:+.1f}%)  {r['target']} "
                      f"{r['test']}:{r['metric']}")

    if args.check:
        if history is None or len(history['runs']) < 2:
            print('\nRegression check: not enough history in the database '
                  '(need at least two runs) — skipped.')
        else:
            regressions = find_regressions(history, args.check_threshold_pp)
            prev, last = history['runs'][-2], history['runs'][-1]
            span = (f"{(prev['git_commit'] or '?')[:8]} -> "
                    f"{(last['git_commit'] or '?')[:8]}")
            if not regressions:
                print(f'\nRegression check ({span}): no metric drifted more '
                      f'than {args.check_threshold_pp:g} pp.')
            else:
                print(f'\nRegression check ({span}): {len(regressions)} '
                      f'metric(s) drifted more than '
                      f'{args.check_threshold_pp:g} pp:')
                for r in regressions[:40]:
                    print(f"  {r['drift_pp']:+7.1f} pp  {r['from_pct']:+7.1f}% "
                          f"-> {r['to_pct']:+7.1f}%  {r['target']} "
                          f"{r['test']}:{r['metric']}")
                if len(regressions) > 40:
                    print(f'  ... and {len(regressions) - 40} more')
                rc = 1

    if args.require_improvement:
        if improvement is None:
            print('\nImprovement gate: no baseline '
                  '(--baseline-run/--baseline-platform) or no shared '
                  'ground-truth metric — skipped.')
        elif improvement['net_pp'] > 0.05:
            print(f"\nImprovement gate: accuracy improved "
                  f"({improvement['net_pp']:+.2f} pp).")
        else:
            print(f"\nImprovement gate: accuracy did not improve "
                  f"({improvement['net_pp']:+.2f} pp).")
            rc = 1

    return rc


if __name__ == '__main__':
    sys.exit(main())
