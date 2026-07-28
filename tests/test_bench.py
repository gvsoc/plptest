"""
Tests for gvtest.bench — DB schema/migration and the calibration report.
"""

import json
import sqlite3

import pytest

from gvtest.bench.db import init_db, insert_json, _SCHEMA_VERSION
from gvtest.bench import calibration


# ---------------------------------------------------------------------------
# DB schema migration
# ---------------------------------------------------------------------------

_LEGACY_SCHEMA = """
CREATE TABLE runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    git_commit TEXT,
    git_branch TEXT,
    platform   TEXT NOT NULL,
    json_file  TEXT
);
CREATE TABLE results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    test        TEXT NOT NULL,
    target      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    description TEXT,
    UNIQUE(run_id, test, target, metric)
);
"""


class TestDbMigration:

    def test_legacy_db_gains_reference_columns(self, tmp_path):
        db_path = str(tmp_path / 'bench.sqlite')
        conn = sqlite3.connect(db_path)
        conn.executescript(_LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO runs (timestamp, platform) VALUES ('t0', 'gvsoc')")
        conn.execute(
            "INSERT INTO results (run_id, test, target, metric, value) "
            "VALUES (1, 'a', 'default', 'cycles', 42)")
        conn.commit()
        conn.close()

        conn = init_db(db_path)
        row = conn.execute(
            "SELECT value, reference, tolerance, ref_type, "
            "value_min, value_max FROM results").fetchone()
        assert row == (42.0, None, None, None, None, None)
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        conn.close()

        # Idempotent on re-open
        conn = init_db(db_path)
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        conn.close()

    def test_fresh_db_at_current_version(self, tmp_path):
        conn = init_db(str(tmp_path / 'bench.sqlite'))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
        assert {'reference', 'tolerance', 'ref_type',
                'value_min', 'value_max'} <= cols
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        conn.close()


# ---------------------------------------------------------------------------
# Calibration report
# ---------------------------------------------------------------------------

def _result(test, target, metric, value, ref=None, tol=None, src=None,
            value_min=None, value_max=None):
    return {'test': test, 'target': target, 'metric': metric, 'value': value,
            'description': metric, 'ref': ref, 'tol': tol, 'ref_type': src,
            'value_min': value_min, 'value_max': value_max}


def _make_db(tmp_path, runs):
    db_path = str(tmp_path / 'bench.sqlite')
    for i, run in enumerate(runs):
        json_path = tmp_path / f'run{i}.json'
        json_path.write_text(json.dumps(run))
        insert_json(db_path, str(json_path))
    return db_path


def _run(results, platform='gvsoc', timestamp='2026-07-16T10:00:00+00:00'):
    return {'timestamp': timestamp, 'git_commit': 'c0ffee0',
            'git_branch': 'main', 'platform': platform, 'results': results}


@pytest.fixture
def db_path(tmp_path):
    return _make_db(tmp_path, [_run([
        # ok (within tol), warn (<= 2x tol), bad (> 2x tol)
        _result('t:a', 'tgt1', 'm_ok', 102, ref=100, tol=5, src='rtl'),
        _result('t:a', 'tgt1', 'm_warn', 108, ref=100, tol=5, src='rtl'),
        _result('t:a', 'tgt1', 'm_bad', 150, ref=100, tol=5, src='rtl'),
        # no declared tolerance: default_tol_pct fallback
        _result('t:a', 'tgt1', 'm_notol', 104, ref=100, src='analytical'),
        # ref == 0 with absolute tolerance
        _result('t:a', 'tgt1', 'm_zero', 1, ref=0, tol=2, src='analytical'),
        # measured-only
        _result('t:a', 'tgt1', 'm_free', 7),
        # shared test on sibling targets
        _result('s:shared', 'sib1', 'cycles', 100, ref=100, tol=5, src='rtl'),
        _result('s:shared', 'sib2', 'cycles', 90, ref=100, tol=5, src='rtl'),
        # coverage-gap target: measured values, no references
        _result('c:t', 'gapless', 'lat', 12),
    ])])


class TestCalibrationModel:

    def test_severity_bands(self, db_path):
        conn = sqlite3.connect(db_path)
        rows = calibration.query_results(conn)
        conn.close()
        model = calibration.build_model(rows)
        severity = {c['metric']: c['severity'] for c in model['cells']
                    if c['target'] == 'tgt1'}
        assert severity == {'m_ok': 'ok', 'm_warn': 'warn', 'm_bad': 'bad',
                            'm_notol': 'ok', 'm_zero': 'ok', 'm_free': None}

    def test_one_section_per_target(self, db_path):
        # Targets sharing a test (sib1/sib2) each get their own section
        # rather than being merged into side-by-side columns.
        conn = sqlite3.connect(db_path)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        assert {tuple(c['targets']) for c in model['clusters']} == \
            {('tgt1',), ('sib1',), ('sib2',), ('gapless',)}
        for cluster in model['clusters']:
            for row in cluster['rows']:
                assert len(row['cells']) == 1
        sib1 = next(c for c in model['clusters'] if c['targets'] == ['sib1'])
        sib2 = next(c for c in model['clusters'] if c['targets'] == ['sib2'])
        assert sib1['rows'][0]['cells'][0]['severity'] == 'ok'
        # 90 vs 100 +/- 5 sits exactly on the 2x-tolerance boundary -> warn
        assert sib2['rows'][0]['cells'][0]['severity'] == 'warn'

    def test_aggregates(self, db_path):
        conn = sqlite3.connect(db_path)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        g = model['global']
        assert g['n_total'] == 9
        assert g['n_referenced'] == 7
        assert g['n_ok'] == 4  # m_ok, m_notol, m_zero, sib1
        gapless = next(t for t in model['targets']
                       if t['target'] == 'gapless')
        assert gapless['n_referenced'] == 0

    def test_latest_run_wins(self, tmp_path):
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'm', 100, ref=100, tol=5)],
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'm', 200, ref=100, tol=5)],
                 timestamp='2026-07-16T11:00:00+00:00'),
        ])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        conn.close()
        assert rows[('t:a', 'tgt', 'm')]['value'] == 200

    def test_baseline_mode(self, tmp_path):
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'm', 100)], platform='rtl',
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'm', 112)], platform='gvsoc',
                 timestamp='2026-07-16T11:00:00+00:00'),
        ])
        conn = sqlite3.connect(db)
        baseline = calibration.query_results(conn, platform='rtl')
        rows = calibration.query_results(conn, exclude_platform='rtl')
        conn.close()
        model = calibration.build_model(
            rows, baseline=baseline, baseline_label='platform:rtl',
            default_tol_pct=5.0)
        cell = model['cells'][0]
        assert cell['ref'] == 100
        assert cell['ref_type'] == 'platform:rtl'
        assert cell['severity'] == 'bad'  # +12% vs 5% default bands
        assert model['global']['n_referenced'] == 1

    def test_baseline_carries_spread(self, tmp_path):
        # A metric sampled over several activations: the headline value is
        # the average and min/max come along from both sides.
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'filter.f', 59.0,
                          value_min=50, value_max=70)], platform='rtl',
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'filter.f', 50.0,
                          value_min=40, value_max=90)], platform='gvsoc',
                 timestamp='2026-07-16T11:00:00+00:00'),
        ])
        conn = sqlite3.connect(db)
        baseline = calibration.query_results(conn, platform='rtl')
        rows = calibration.query_results(conn, exclude_platform='rtl')
        conn.close()
        model = calibration.build_model(
            rows, baseline=baseline, baseline_label='platform:rtl')
        cell = model['cells'][0]
        assert (cell['value'], cell['value_min'], cell['value_max']) == \
            (50.0, 40, 90)
        assert (cell['ref'], cell['ref_min'], cell['ref_max']) == (59.0, 50, 70)
        # Deltas: headline avg plus each spread bound against its own ref
        assert round(cell['delta_pct'], 1) == -15.3        # 50 vs 59
        assert round(cell['delta_min_pct'], 1) == -20.0    # 40 vs 50
        assert round(cell['delta_max_pct'], 1) == 28.6     # 90 vs 70
        # Only the headline avg drives the aggregates (one filter, one metric)
        assert model['global']['n_referenced'] == 1
        html_str = calibration.render_html(model, 'spread')
        # Columns are ordered avg / min / max on both sides and for the deltas
        for col in ('<th class="grp">Ref avg</th>', '<th>Ref min</th>',
                    '<th>Ref max</th>', '<th class="grp">Meas avg</th>',
                    '<th>Meas min</th>', '<th>Meas max</th>',
                    '<th class="grp">Δ avg</th>', '<th>Δ min</th>',
                    '<th>Δ max</th>'):
            assert col in html_str, col
        assert '-20.0%' in html_str and '+28.6%' in html_str
        # Deltas come first, then the trend, then the raw numbers
        assert html_str.index('<th class="grp">Δ avg</th>') < \
            html_str.index('Impr. vs prev') < \
            html_str.index('<th class="grp">Ref avg</th>') < \
            html_str.index('<th class="grp">Meas avg</th>')
        # The fixed-scale delta bar is gone
        assert 'dbar' not in html_str
        assert '⋯' not in html_str


class TestCalibrationHistory:
    """Following a metric's calibration across runs."""

    def _db(self, tmp_path, values):
        # One run per value, same declared reference of 100.
        runs = [_run([_result('t:a', 'tgt', 'm', v, ref=100, tol=5,
                              src='rtl')],
                     timestamp=f'2026-07-{16 + i}T10:00:00+00:00')
                for i, v in enumerate(values)]
        for i, r in enumerate(runs):
            r['git_commit'] = f'commit{i}'
        return _make_db(tmp_path, runs)

    def test_history_tracks_delta_per_run(self, tmp_path):
        db = self._db(tmp_path, [100, 104, 130])
        conn = sqlite3.connect(db)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        points = hist['metrics'][('t:a', 'tgt', 'm')]
        assert [round(p['delta_pct']) for p in points] == [0, 4, 30]
        assert [p['severity'] for p in points] == ['ok', 'ok', 'bad']
        assert [p['git_commit'] for p in points] == \
            ['commit0', 'commit1', 'commit2']
        assert len(hist['runs']) == 3

    def test_find_regressions(self, tmp_path):
        # 0% -> 4% -> 30%: the last step is a 26pp regression
        db = self._db(tmp_path, [100, 104, 130])
        conn = sqlite3.connect(db)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        regs = calibration.find_regressions(hist, threshold_pp=2.0)
        assert len(regs) == 1
        assert round(regs[0]['drift_pp']) == 26
        assert regs[0]['to_commit'] == 'commit2'
        # A model that got *better* is not a regression
        assert calibration.find_regressions(
            {'runs': hist['runs'],
             'metrics': {('t', 'x', 'm'): [
                 {'delta_pct': 30, 'git_commit': 'a', 'run_id': 1},
                 {'delta_pct': 2, 'git_commit': 'b', 'run_id': 2}]}},
            threshold_pp=2.0) == []

    def test_history_uses_baseline_when_given(self, tmp_path):
        # ACU-style: reference comes from an rtl run, reused across history
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'm', 100)], platform='rtl',
                 timestamp='2026-07-16T09:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'm', 110)], platform='gvsoc',
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'm', 150)], platform='gvsoc',
                 timestamp='2026-07-17T10:00:00+00:00'),
        ])
        conn = sqlite3.connect(db)
        baseline = calibration.query_results(conn, platform='rtl')
        hist = calibration.build_history(
            calibration.query_history(conn, exclude_platform='rtl'),
            baseline=baseline)
        conn.close()
        points = hist['metrics'][('t:a', 'tgt', 'm')]
        assert [round(p['delta_pct']) for p in points] == [10, 50]
        assert calibration.find_regressions(hist)[0]['drift_pp'] == 40

    def test_trend_columns(self, tmp_path):
        # |Δ| goes 20% -> 10% -> 5%: improving each run.
        db = self._db(tmp_path, [120, 110, 105])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        calibration.annotate_trends(model, hist, window_days=30)
        cell = model['cells'][0]
        # vs previous run: |Δ| 10% -> 5% == 50% improvement
        assert round(cell['trend_prev_pct']) == 50
        # vs the 30d average of the earlier runs (mean(20, 10) = 15) -> 5%
        assert round(cell['trend_window_pct']) == 67
        html_str = calibration.render_html(model, 'trend', history=hist)
        assert 'Impr. vs prev' in html_str
        assert 'Impr. vs 30d avg' in html_str

    def test_trend_marks_worsening(self, tmp_path):
        # |Δ| 5% -> 20%: got four times worse
        db = self._db(tmp_path, [105, 120])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        calibration.annotate_trends(model, hist)
        assert round(model['cells'][0]['trend_prev_pct']) == -300

    def test_trends_absent_without_history(self, tmp_path):
        # A single-run database still renders; trend columns show as "—"
        db = self._db(tmp_path, [110])
        conn = sqlite3.connect(db)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        assert model['cells'][0]['trend_prev_pct'] is None
        html_str = calibration.render_html(model, 'no history')
        assert 'Impr. vs prev' in html_str

    def test_history_section_rendered(self, tmp_path):
        db = self._db(tmp_path, [100, 104, 130])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        html_str = calibration.render_html(model, 'hist', history=hist)
        assert 'Calibration over time' in html_str
        assert '<svg class="spark"' in html_str
        # A single run has nothing to trend
        assert 'Calibration over time' not in calibration.render_html(
            model, 'hist', history={'runs': hist['runs'][:1], 'metrics': {}})


class TestCalibrationRender:

    def test_render_self_contained(self, db_path, tmp_path):
        conn = sqlite3.connect(db_path)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        html_str = calibration.render_html(model, 'test report')
        for cell in model['cells']:
            assert cell['metric'] in html_str
        # Self-contained: no external resources (inline script only)
        assert 'http://' not in html_str.replace(
            'http://www.apache.org', '')
        assert 'https://' not in html_str
        assert 'cdn.' not in html_str
        assert '<script src' not in html_str

    def test_render_target_filter(self, db_path):
        conn = sqlite3.connect(db_path)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        html_str = calibration.render_html(model, 'test report')
        # One chip per target plus the all-targets default
        for target in ('tgt1', 'sib1', 'sib2', 'gapless'):
            assert f'<button type="button" data-target="{target}"' \
                in html_str
        assert 'data-target=""' in html_str
        # Each target has its own section, tagged for client-side filtering
        for target in ('tgt1', 'sib1', 'sib2', 'gapless'):
            assert f'data-targets="{target}"' in html_str
        # The embedded payload parses and covers every target
        payload = json.loads(
            html_str.split('id="calib-data">')[1].split('</script>')[0]
            .replace('<\\/', '</'))
        assert set(payload['aggregates']) == \
            {'', 'tgt1', 'sib1', 'sib2', 'gapless'}
        assert payload['tolPct'] == 5.0
        assert all(m[1] is not None for m in payload['metrics'])

    def test_cli(self, db_path, tmp_path, capsys):
        output = tmp_path / 'report.html'
        argv = ['calibration', '--db', db_path, '--output', str(output)]
        import unittest.mock
        with unittest.mock.patch('sys.argv', argv):
            assert calibration.main() == 0
        assert output.exists()
        assert 'Per-target scoreboard' in output.read_text()
        # The console summary always prints, with the accuracy score.
        out = capsys.readouterr().out
        assert 'CALIBRATION SUMMARY' in out
        assert 'Accuracy score:' in out

    def test_cli_no_match(self, db_path, tmp_path):
        argv = ['calibration', '--db', db_path,
                '--output', str(tmp_path / 'r.html'),
                '--target', 'nonexistent*']
        import unittest.mock
        with unittest.mock.patch('sys.argv', argv):
            assert calibration.main() == 1


# ---------------------------------------------------------------------------
# Accuracy score, A/B improvement, and ratchet
# ---------------------------------------------------------------------------

def _two_run_db(tmp_path):
    """Baseline (run 1) then a change (run 2): cyc moves closer to its RTL
    reference, lat drifts a little further."""
    return _make_db(tmp_path, [
        _run([_result('t:a', 'tgt', 'cyc', 120, ref=100, tol=5, src='rtl'),
              _result('t:b', 'tgt', 'lat', 210, ref=200, tol=5, src='rtl')],
             timestamp='2026-07-16T10:00:00+00:00'),
        _run([_result('t:a', 'tgt', 'cyc', 103, ref=100, tol=5, src='rtl'),
              _result('t:b', 'tgt', 'lat', 214, ref=200, tol=5, src='rtl')],
             timestamp='2026-07-17T10:00:00+00:00'),
    ])


class TestAccuracyScore:

    def test_score_covers_all_references(self, tmp_path):
        db = _make_db(tmp_path, [_run([
            _result('t:a', 'tgt', 'm1', 110, ref=100, tol=5, src='rtl'),       # 10%
            _result('t:a', 'tgt', 'm2', 102, ref=100, tol=5, src='analytical'),  # 2%
            _result('t:a', 'tgt', 'm3', 130, ref=100, tol=5, src='measured'),  # 30%
            _result('t:a', 'tgt', 'm4', 7),                                     # no ref
        ])])
        conn = init_db(db)
        model = calibration.build_model(calibration.query_results(conn))
        g = model['global']
        assert g['n_accuracy'] == 3                       # rtl + analytical + measured
        assert g['accuracy'] == pytest.approx(14.0)       # mean(10, 2, 30)
        by_metric = {c['metric']: c['acc_err_pct'] for c in model['cells']}
        assert by_metric['m3'] == pytest.approx(30.0)     # measured now counts
        assert by_metric['m4'] is None                    # only the unreferenced
        conn.close()

    def test_score_survives_baseline_substitution(self, tmp_path):
        # Even in run-vs-run (baseline) mode, the accuracy score keeps using
        # the declared ground-truth reference, not the substituted baseline.
        conn = init_db(_two_run_db(tmp_path))
        rows = calibration.query_results(conn)
        base = calibration.query_results(conn, run=1)
        model = calibration.build_model(rows, baseline=base,
                                        baseline_label='run:1')
        # cyc |Δ|=3%, lat |Δ|=7% against declared refs -> mean 5%
        assert model['global']['accuracy'] == pytest.approx(5.0)
        conn.close()


class TestImprovement:

    def test_net_and_counts(self, tmp_path):
        conn = init_db(_two_run_db(tmp_path))
        rows = calibration.query_results(conn)            # latest = run 2
        base = calibration.query_results(conn, run=1)
        imp = calibration.build_improvement(rows, base)
        assert imp['n'] == 2
        assert imp['net_pp'] == pytest.approx(7.5)        # 12.5% -> 5.0%
        assert len(imp['improved']) == 1
        assert len(imp['regressed']) == 1
        assert imp['gains'][0]['metric'] == 'cyc'         # biggest gain first
        assert imp['gains'][0]['improve_pp'] == pytest.approx(17.0)
        conn.close()

    def test_measured_lock_included(self, tmp_path):
        db = _make_db(tmp_path, [
            _run([_result('t', 'tgt', 'm', 120, ref=100, tol=34, src='measured')],
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t', 'tgt', 'm', 105, ref=100, tol=34, src='measured')],
                 timestamp='2026-07-17T10:00:00+00:00'),
        ])
        conn = init_db(db)
        rows = calibration.query_results(conn)
        base = calibration.query_results(conn, run=1)
        imp = calibration.build_improvement(rows, base)
        assert imp is not None                            # measured now counts
        assert imp['n'] == 1
        assert imp['gains'][0]['improve_pp'] == pytest.approx(15.0)  # 20% -> 5%
        conn.close()

    def test_section_rendered(self, tmp_path):
        conn = init_db(_two_run_db(tmp_path))
        rows = calibration.query_results(conn)
        base = calibration.query_results(conn, run=1)
        model = calibration.build_model(rows)
        imp = calibration.build_improvement(rows, base)
        html_str = calibration.render_html(
            model, 't', improvement=imp, improvement_baseline='run 1')
        assert 'Accuracy vs baseline' in html_str
        assert 'net accuracy' in html_str
        assert 'Accuracy</th>' in html_str                # scoreboard column
        assert 'id="t-acc"' in html_str                   # headline tile
        conn.close()

    def test_per_metric_column(self, tmp_path):
        conn = init_db(_two_run_db(tmp_path))
        rows = calibration.query_results(conn)
        base = calibration.query_results(conn, run=1)
        model = calibration.build_model(rows)
        imp = calibration.build_improvement(rows, base)
        calibration.annotate_improvement(model, imp)
        # Each cell carries its own improvement (cyc +17 pp, lat -2 pp) ...
        by = {c['metric']: c['improve_pp'] for c in model['cells']}
        assert by['cyc'] == pytest.approx(17.0)
        assert by['lat'] == pytest.approx(-2.0)
        # ... and the detail table renders a per-metric column for them.
        html_str = calibration.render_html(
            model, 't', improvement=imp, improvement_baseline='run 1')
        assert 'Δ acc vs base' in html_str
        assert '+17.0 pp' in html_str and '-2.0 pp' in html_str

    def test_no_column_without_baseline(self, tmp_path):
        conn = init_db(_two_run_db(tmp_path))
        model = calibration.build_model(calibration.query_results(conn))
        html_str = calibration.render_html(model, 't')
        assert 'Δ acc vs base' not in html_str
        conn.close()


class TestRatchet:

    def test_tighten_with_headroom(self, tmp_path):
        # |Δ|=1 sits well inside tol=20 -> tighten (keeps 2x slack).
        db = _make_db(tmp_path, [_run([
            _result('t', 'tgt', 'm', 101, ref=100, tol=20, src='rtl')])])
        conn = init_db(db)
        cells = calibration.build_model(calibration.query_results(conn))['cells']
        rat = calibration.suggest_ratchet(cells)
        assert len(rat['tighten']) == 1
        assert rat['tighten'][0]['tol'] == 20
        assert rat['tighten'][0]['new_tol'] == 2          # ceil(2 * 1)
        assert rat['rebaseline'] == []
        conn.close()

    def test_no_tighten_near_tolerance(self, tmp_path):
        # |Δ|=4 against tol=5: no spare headroom, nothing suggested.
        db = _make_db(tmp_path, [_run([
            _result('t', 'tgt', 'm', 104, ref=100, tol=5, src='rtl')])])
        conn = init_db(db)
        cells = calibration.build_model(calibration.query_results(conn))['cells']
        assert calibration.suggest_ratchet(cells)['tighten'] == []
        conn.close()

    def test_rebaseline_drifted_measured_lock(self, tmp_path):
        db = _make_db(tmp_path, [_run([
            _result('t', 'tgt', 'm', 142, ref=135, tol=34, src='measured')])])
        conn = init_db(db)
        cells = calibration.build_model(calibration.query_results(conn))['cells']
        rat = calibration.suggest_ratchet(cells)
        assert len(rat['rebaseline']) == 1
        assert rat['rebaseline'][0]['ref'] == 135
        assert rat['rebaseline'][0]['new_ref'] == 142
        assert rat['tighten'] == []                       # not a ground truth
        conn.close()


class TestImprovementCli:

    def test_baseline_and_require_improvement(self, tmp_path, capsys):
        db = _two_run_db(tmp_path)
        out = tmp_path / 'r.html'
        argv = ['calibration', '--db', db, '--output', str(out),
                '--baseline-run', '1', '--require-improvement',
                '--suggest-ratchet']
        import unittest.mock
        with unittest.mock.patch('sys.argv', argv):
            assert calibration.main() == 0               # net +7.5pp -> pass
        text = capsys.readouterr().out
        assert 'CALIBRATION SUMMARY' in text
        assert 'Accuracy score:' in text
        assert 'Accuracy change:' in text
        assert 'IMPROVED by' in text
        assert 'vs baseline run 1' in text
        assert 'Improvement gate: accuracy improved' in text

    def test_require_improvement_fails_on_regression(self, tmp_path, capsys):
        # Swap the runs: run 2 is worse than run 1 -> gate fails.
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'cyc', 103, ref=100, tol=5, src='rtl')],
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'cyc', 130, ref=100, tol=5, src='rtl')],
                 timestamp='2026-07-17T10:00:00+00:00'),
        ])
        out = tmp_path / 'r.html'
        argv = ['calibration', '--db', db, '--output', str(out),
                '--baseline-run', '1', '--require-improvement']
        import unittest.mock
        with unittest.mock.patch('sys.argv', argv):
            assert calibration.main() == 1
        assert 'did not improve' in capsys.readouterr().out
