"""
Tests for gvtest.runner — Runner, TestRun, test execution, stats, filtering, reporting.
"""

import os
import sys
import json
import pytest
import tempfile
import threading
from pathlib import Path
from io import StringIO
from unittest.mock import patch, MagicMock

from gvtest.runner import (
    Runner, TestRun, TestImpl, TestsetImpl, TestCommon,
    MakeTestImpl, Target, Worker,
    TestRunStats, TestStats, TestsetStats,
    table_dump_row,
)
from gvtest.testsuite import Shell, Call, Checker


# ---------------------------------------------------------------------------
# Runner initialization
# ---------------------------------------------------------------------------

class TestRunnerInit:
    """Tests for Runner construction and defaults."""

    def test_default_config(self):
        r = Runner(properties=[], flags=[])
        assert r.config == 'default'
        assert r.platform == 'gvsoc'
        assert r.load_average == 0.9
        assert r.max_timeout == -1
        assert r.stdout is False
        assert r.safe_stdout is False
        assert r.report_all is False

    def test_custom_config(self):
        r = Runner(config='debug', properties=[], flags=[], platform='rtl')
        assert r.config == 'debug'
        assert r.platform == 'rtl'

    def test_properties_parsing(self):
        r = Runner(properties=['arch=rv64', 'mode=sim'], flags=[])
        assert r.get_property('arch') == 'rv64'
        assert r.get_property('mode') == 'sim'
        assert r.get_property('nonexistent') is None

    def test_default_target_no_targets(self):
        r = Runner(properties=[], flags=[])
        assert r.target_names == ['default']
        assert r.default_target.name == 'default'

    def test_explicit_targets(self):
        r = Runner(properties=[], flags=[], targets=['rv64', 'pulp-open'])
        assert r.target_names == ['rv64', 'pulp-open']
        assert r.default_target.name == 'rv64'

    def test_get_platform(self):
        r = Runner(properties=[], flags=[], platform='fpga')
        assert r.get_platform() == 'fpga'


# ---------------------------------------------------------------------------
# Test selection and skipping
# ---------------------------------------------------------------------------

class TestFiltering:
    """Tests for test selection and skip logic."""

    def test_all_selected_when_no_filter(self):
        r = Runner(properties=[], flags=[])
        # Create a mock test
        mock_test = MagicMock()
        mock_test.get_full_name.return_value = 'suite:test_a'
        assert r.is_selected(mock_test) is True

    def test_selected_by_prefix(self):
        r = Runner(properties=[], flags=[], test_list=['suite:test_a'])
        mock_test = MagicMock()
        mock_test.get_full_name.return_value = 'suite:test_a'
        assert r.is_selected(mock_test) is True

    def test_not_selected(self):
        r = Runner(properties=[], flags=[], test_list=['suite:test_b'])
        mock_test = MagicMock()
        mock_test.get_full_name.return_value = 'suite:test_a'
        assert r.is_selected(mock_test) is False

    def test_selected_by_partial_prefix(self):
        """Test list uses prefix matching."""
        r = Runner(properties=[], flags=[], test_list=['suite'])
        mock_test = MagicMock()
        mock_test.get_full_name.return_value = 'suite:test_a'
        assert r.is_selected(mock_test) is True

    def test_skip_by_prefix(self):
        r = Runner(properties=[], flags=[], test_skip_list=['suite:skip_me'])
        assert r.is_skipped('suite:skip_me') is True
        assert r.is_skipped('suite:skip_me:subtest') is True
        assert r.is_skipped('suite:keep_me') is False

    def test_no_skip_list(self):
        r = Runner(properties=[], flags=[])
        assert r.is_skipped('anything') is False

    def test_multiple_skip_entries(self):
        r = Runner(properties=[], flags=[], test_skip_list=['a', 'b'])
        assert r.is_skipped('a:test') is True
        assert r.is_skipped('b:test') is True
        assert r.is_skipped('c:test') is False


# ---------------------------------------------------------------------------
# Test name tracking
# ---------------------------------------------------------------------------

class TestNameTracking:
    """Tests for max test name length tracking."""

    def test_declare_name_tracks_max(self):
        r = Runner(properties=[], flags=[])
        r.declare_name('short')
        assert r.get_max_testname_len() == 5
        r.declare_name('much_longer_name')
        assert r.get_max_testname_len() == 16
        r.declare_name('tiny')
        assert r.get_max_testname_len() == 16  # Still the max


# ---------------------------------------------------------------------------
# Testset loading and import
# ---------------------------------------------------------------------------

class TestTestsetImport:
    """Tests for loading testset.cfg files."""

    def test_load_simple_testset(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('basic')
    test = testset.new_test('echo_test')
    test.add_command(Shell('run', 'echo hello'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        assert len(r.testsets) == 1
        assert r.testsets[0].name == 'basic'
        assert len(r.testsets[0].tests) == 1
        assert r.testsets[0].tests[0].name == 'echo_test'

    def test_load_nonexistent_testset(self, tmp_path):
        r = Runner(properties=[], flags=[], nb_threads=1)
        with pytest.raises(RuntimeError, match='Unable to open'):
            r.add_testset(str(tmp_path / 'nonexistent.cfg'))

    def test_nested_testsets(self, tmp_path):
        sub_dir = tmp_path / 'sub'
        sub_dir.mkdir()
        
        sub_testset = sub_dir / 'testset.cfg'
        sub_testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('sub')
    test = testset.new_test('sub_test')
    test.add_command(Shell('run', 'echo sub'))
''')
        
        main_testset = tmp_path / 'testset.cfg'
        main_testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('main')
    testset.import_testset(file='sub/testset.cfg')
''')
        
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(main_testset))
        assert r.testsets[0].name == 'main'
        assert len(r.testsets[0].testsets) == 1
        assert r.testsets[0].testsets[0].name == 'sub'


# ---------------------------------------------------------------------------
# Test execution (end-to-end with real shell commands)
# ---------------------------------------------------------------------------

class TestExecution:
    """End-to-end tests running actual shell commands."""

    def _run_testset(self, tmp_path, testset_content, **runner_kwargs):
        """Helper to create, load, and run a testset."""
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(testset_content)
        
        defaults = {'properties': [], 'flags': [], 'nb_threads': 1}
        defaults.update(runner_kwargs)
        r = Runner(**defaults)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        return r

    def test_passing_test(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('pass_suite')
    test = testset.new_test('pass_test')
    test.add_command(Shell('run', 'echo hello'))
''')
        assert r.stats.stats['passed'] == 1
        assert r.stats.stats['failed'] == 0

    def test_failing_test(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('fail_suite')
    test = testset.new_test('fail_test')
    test.add_command(Shell('run', 'exit 1'))
''')
        assert r.stats.stats['failed'] == 1
        assert r.stats.stats['passed'] == 0

    def test_expected_nonzero_retval(self, tmp_path):
        """Shell command with expected non-zero retval should pass."""
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('retval_suite')
    test = testset.new_test('expected_fail')
    test.add_command(Shell('run', 'exit 1', retval=1))
''')
        assert r.stats.stats['passed'] == 1
        assert r.stats.stats['failed'] == 0

    def test_multiple_commands_stop_on_failure(self, tmp_path):
        """If a command fails, subsequent commands should not run."""
        marker = tmp_path / 'marker.txt'
        r = self._run_testset(tmp_path, f'''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('multi')
    test = testset.new_test('stop_on_fail')
    test.add_command(Shell('step1', 'exit 1'))
    test.add_command(Shell('step2', 'touch {marker}'))
''')
        assert r.stats.stats['failed'] == 1
        assert not marker.exists()  # step2 should not have run

    def test_multiple_tests(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('multi')
    for i in range(5):
        test = testset.new_test(f'test_{i}')
        test.add_command(Shell('run', 'echo ok'))
''')
        assert r.stats.stats['passed'] == 5
        assert r.stats.stats['failed'] == 0

    def test_mixed_pass_fail(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('mixed')
    t1 = testset.new_test('pass')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('fail')
    t2.add_command(Shell('run', 'exit 1'))
''')
        assert r.stats.stats['passed'] == 1
        assert r.stats.stats['failed'] == 1

    def test_test_output_captured(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('output')
    test = testset.new_test('echo_test')
    test.add_command(Shell('run', 'echo MAGIC_OUTPUT_STRING'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        
        test = r.testsets[0].tests[0]
        run = test.runs[0]
        assert 'MAGIC_OUTPUT_STRING' in run.output

    def test_checker_command_pass(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def my_checker(run, output):
    if 'SUCCESS' in output:
        return (True, None)
    return (False, "SUCCESS not found")

def testset_build(testset):
    testset.set_name('checker')
    test = testset.new_test('check_test')
    test.add_command(Shell('run', 'echo SUCCESS'))
    test.add_command(Checker('validate', my_checker))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1

    def test_checker_command_fail(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def my_checker(run, output):
    if 'SUCCESS' in output:
        return (True, None)
    return (False, "SUCCESS not found")

def testset_build(testset):
    testset.set_name('checker')
    test = testset.new_test('check_fail')
    test.add_command(Shell('run', 'echo FAILURE'))
    test.add_command(Checker('validate', my_checker))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['failed'] == 1

    def test_call_command(self, tmp_path):
        marker = tmp_path / 'call_marker.txt'
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(f'''
from gvtest.testsuite import *

def my_callback():
    with open("{marker}", "w") as f:
        f.write("called")
    return 0

def testset_build(testset):
    testset.set_name('call')
    test = testset.new_test('call_test')
    test.add_command(Call('step', my_callback))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert marker.exists()
        assert marker.read_text() == 'called'

    def test_skipped_test(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('skip')
    test = testset.new_test('skipped')
    test.skip('not ready')
    test.add_command(Shell('run', 'echo should not run'))
''')
        assert r.stats.stats['skipped'] == 1
        assert r.stats.stats['passed'] == 0

    def test_skip_from_command_line(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('suite')
    t1 = testset.new_test('keep')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('skip_me')
    t2.add_command(Shell('run', 'echo should not run'))
''', test_skip_list=['suite:skip_me'])
        assert r.stats.stats['passed'] == 1
        assert r.stats.stats['skipped'] == 1

    def test_select_specific_test(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('suite')
    t1 = testset.new_test('run_me')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('not_me')
    t2.add_command(Shell('run', 'echo should not run'))
''', test_list=['suite:run_me'])
        # Only run_me should be in the testset (not_me filtered at creation)
        assert r.stats.stats['passed'] == 1
        total = r.stats.stats['passed'] + r.stats.stats['failed']
        assert total == 1


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

class TestTimeout:
    """Tests for test timeout functionality."""

    def test_timeout_kills_test(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('timeout')
    test = testset.new_test('slow_test')
    test.add_command(Shell('run', 'sleep 60'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1, max_timeout=2)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['failed'] == 1
        run = r.testsets[0].tests[0].runs[0]
        assert 'Timeout reached' in run.output


# ---------------------------------------------------------------------------
# Benchmark extraction
# ---------------------------------------------------------------------------

class TestBenchmarks:
    """Tests for benchmark result extraction."""

    def test_bench_extraction(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('bench')
    test = testset.new_test('perf')
    test.add_command(Shell('run', 'echo "Cycles: 42"'))
    test.add_bench('cycles', r'Cycles: (\\d+)', 'CPU cycles')
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert len(r.bench_results) == 1
        result = r.bench_results[0]
        assert result['test'] == 'bench:perf'
        assert result['metric'] == 'cycles'
        assert result['value'] == 42.0
        assert result['description'] == 'CPU cycles'
        assert result['ref'] is None
        assert result['tol'] is None
        assert result['ref_type'] is None

    def test_bench_with_reference(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('bench')
    test = testset.new_test('perf')
    test.add_command(Shell('run', 'echo "Cycles: 105"'))
    test.add_bench('cycles', r'Cycles: (\\d+)',
                   ref=100, tol_pct=10, ref_type='rtl')
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        result = r.bench_results[0]
        assert result['value'] == 105.0
        assert result['ref'] == 100
        assert result['tol'] == 10.0  # tol_pct converted at declaration
        assert result['ref_type'] == 'rtl'
        assert result['description'] == 'cycles'  # defaults to metric name

    def test_register_bench_from_checker(self, tmp_path):
        # A checker that registers dynamic-metric bench results directly.
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def _checker(test, output, *a, **k):
    test.register_bench('filter.a', 12.0, description='fir')
    test.register_bench('filter.b', 34.0, description='mixer')
    return (True, 'registered 2 filters')

def testset_build(testset):
    testset.set_name('bench')
    test = testset.new_test('perf')
    test.add_command(Shell('run', 'echo hello'))
    test.add_command(Checker('check', _checker))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        metrics = {res['metric']: res['value'] for res in r.bench_results}
        assert metrics == {'filter.a': 12.0, 'filter.b': 34.0}
        assert all(res['test'] == 'bench:perf' for res in r.bench_results)

    def test_bench_db_export(self, tmp_path):
        db_file = tmp_path / 'bench.sqlite'
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('bench')
    test = testset.new_test('perf')
    test.add_command(Shell('run', 'echo "Cycles: 100"'))
    test.add_bench('cycles', r'Cycles: (\\d+)', 'CPU cycles',
                   ref=98, tol=5, ref_type='analytical')
''')
        r = Runner(properties=[], flags=[], nb_threads=1,
                   bench_db=str(db_file))
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert db_file.exists()
        import sqlite3
        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT test, metric, value, reference, tolerance, ref_type "
            "FROM results").fetchone()
        conn.close()
        assert row == ('bench:perf', 'cycles', 100.0, 98.0, 5.0, 'analytical')


class TestBenchCheck:
    """A bench with ref+tol gates the test via an auto-added command."""

    def _run(self, tmp_path, body, bench_check=True):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(
            'from gvtest.testsuite import *\n\n'
            'def testset_build(testset):\n'
            "    testset.set_name('bench')\n"
            '    test = testset.new_test(\'perf\')\n'
            + body)
        r = Runner(properties=[], flags=[], nb_threads=1,
                   bench_check=bench_check)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        return r

    def test_command_added_once(self, tmp_path):
        # Two enforceable benches -> exactly one bench_check command.
        r = self._run(tmp_path,
            "    test.add_command(Shell('run', 'echo x'))\n"
            "    test.add_bench('a', r'a=(\\d+)', ref=1, tol=1, ref_type='rtl')\n"
            "    test.add_bench('b', r'b=(\\d+)', ref=1, tol=1, ref_type='rtl')\n")
        test = r.testsets[0].tests[0]
        names = [c.name for c in test.commands]
        assert names.count('bench_check') == 1

    def test_in_tolerance_passes(self, tmp_path):
        r = self._run(tmp_path,
            "    test.add_command(Shell('run', 'echo \"Cycles: 102\"'))\n"
            "    test.add_bench('cycles', r'Cycles: (\\d+)', ref=100, tol=5, ref_type='rtl')\n")
        assert r.stats.stats['failed'] == 0
        assert r.testsets[0].tests[0].runs[0].status == 'passed'

    def test_out_of_tolerance_fails(self, tmp_path):
        r = self._run(tmp_path,
            "    test.add_command(Shell('run', 'echo \"Cycles: 130\"'))\n"
            "    test.add_bench('cycles', r'Cycles: (\\d+)', ref=100, tol=5, ref_type='rtl')\n")
        run = r.testsets[0].tests[0].runs[0]
        assert run.status == 'failed'
        assert 'cycles' in run.output and 'outside' in run.output

    def test_missing_measurement_fails(self, tmp_path):
        r = self._run(tmp_path,
            "    test.add_command(Shell('run', 'echo nothing'))\n"
            "    test.add_bench('cycles', r'Cycles: (\\d+)', ref=100, tol=5, ref_type='rtl')\n")
        run = r.testsets[0].tests[0].runs[0]
        assert run.status == 'failed'
        assert 'no measurement' in run.output

    def test_ref_without_tol_not_gated(self, tmp_path):
        # A ref alone is report-only: no bench_check command, never fails.
        r = self._run(tmp_path,
            "    test.add_command(Shell('run', 'echo \"Cycles: 130\"'))\n"
            "    test.add_bench('cycles', r'Cycles: (\\d+)', ref=100, ref_type='rtl')\n")
        test = r.testsets[0].tests[0]
        assert 'bench_check' not in [c.name for c in test.commands]
        assert test.runs[0].status == 'passed'

    def test_no_bench_check_flag_disables(self, tmp_path):
        r = self._run(tmp_path,
            "    test.add_command(Shell('run', 'echo \"Cycles: 130\"'))\n"
            "    test.add_bench('cycles', r'Cycles: (\\d+)', ref=100, tol=5, ref_type='rtl')\n",
            bench_check=False)
        # command is present but no-ops; the out-of-tolerance value is
        # still recorded, the test still passes.
        assert r.testsets[0].tests[0].runs[0].status == 'passed'
        assert r.bench_results[0]['value'] == 130.0


class TestBenchDeclaration:
    """Tests for the Bench dataclass validation."""

    def test_tol_pct_conversion(self):
        from gvtest.testsuite import Bench
        bench = Bench.make('cycles', r'(\d+)', ref=200, tol_pct=5,
                           ref_type='rtl')
        assert bench.tol == 10.0
        assert bench.desc == 'cycles'

    def test_tol_and_tol_pct_exclusive(self):
        from gvtest.testsuite import Bench
        with pytest.raises(ValueError):
            Bench.make('cycles', r'(\d+)', ref=200, tol=1, tol_pct=5,
                       ref_type='rtl')

    def test_tol_requires_ref(self):
        from gvtest.testsuite import Bench
        with pytest.raises(ValueError):
            Bench.make('cycles', r'(\d+)', tol=1)
        with pytest.raises(ValueError):
            Bench.make('cycles', r'(\d+)', tol_pct=5)
        with pytest.raises(ValueError):
            Bench.make('cycles', r'(\d+)', ref_type='rtl')

    def test_ref_requires_ref_type(self):
        from gvtest.testsuite import Bench
        with pytest.raises(ValueError):
            Bench.make('cycles', r'(\d+)', ref=200)

    def test_ref_type_validated(self):
        from gvtest.testsuite import Bench, REF_TYPES
        with pytest.raises(ValueError):
            Bench.make('cycles', r'(\d+)', ref=200, ref_type='guess')
        for ref_type in REF_TYPES:
            assert Bench.make('cycles', r'(\d+)', ref=200,
                              ref_type=ref_type).ref_type == ref_type


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

class TestStats:
    """Tests for statistics collection and aggregation."""

    def _make_mock_run(self, status='passed', duration=1.0, target=None, name='test'):
        """Create a mock test run with given status."""
        run = MagicMock()
        run.status = status
        run.duration = duration
        run.target = target
        run.config = 'default'
        run.test = MagicMock()
        run.test.get_full_name.return_value = name
        run.test.name = name
        run.get_target_name.return_value = 'default'
        run.get_stats = lambda stats: self._apply_stats(stats, status, duration)
        return run

    def _apply_stats(self, stats, status, duration):
        stats[status] += 1
        stats['duration'] = duration

    def test_run_stats_passed(self):
        run = self._make_mock_run('passed', 1.5)
        stats = TestRunStats(run)
        assert stats.stats['passed'] == 1
        assert stats.stats['failed'] == 0
        assert stats.stats['duration'] == 1.5

    def test_run_stats_failed(self):
        run = self._make_mock_run('failed', 0.5)
        stats = TestRunStats(run)
        assert stats.stats['failed'] == 1
        assert stats.stats['passed'] == 0

    def test_stats_propagate_to_parent(self):
        from gvtest.runner import TestStats as RealTestStats
        parent = RealTestStats()
        run = self._make_mock_run('passed', 1.0)
        TestRunStats(run, parent=parent)
        assert parent.stats['passed'] == 1


# ---------------------------------------------------------------------------
# JUnit report
# ---------------------------------------------------------------------------

class TestJunitReport:
    """Tests for JUnit XML report generation."""

    def test_junit_output(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('junit_suite')
    t1 = testset.new_test('pass_test')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('fail_test')
    t2.add_command(Shell('run', 'exit 1'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        
        report_path = tmp_path / 'junit-reports'
        r.dump_junit(str(report_path))
        
        assert report_path.exists()
        xml_files = list(report_path.glob('*.xml'))
        assert len(xml_files) >= 1
        
        content = xml_files[0].read_text()
        assert '<?xml version="1.0"' in content
        assert 'testsuite' in content
        assert 'testcase' in content
        assert 'pass_test' in content
        assert 'fail_test' in content
        assert '<failure>' in content

    def test_junit_skipped_test(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('junit_skip')
    test = testset.new_test('skipped')
    test.skip('not implemented')
    test.add_command(Shell('run', 'echo nope'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        
        report_path = tmp_path / 'junit-reports'
        r.dump_junit(str(report_path))
        
        xml_files = list(report_path.glob('*.xml'))
        content = xml_files[0].read_text()
        assert '<skipped' in content


# ---------------------------------------------------------------------------
# Testset hierarchy and naming
# ---------------------------------------------------------------------------

class TestTestsetHierarchy:
    """Tests for testset naming and nesting."""

    def test_full_name_single_level(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('suite')
    test = testset.new_test('test_a')
    test.add_command(Shell('run', 'echo ok'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        assert r.testsets[0].tests[0].get_full_name() == 'suite:test_a'

    def test_full_name_nested(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def sub_build(testset):
    testset.set_name('sub')
    test = testset.new_test('deep')
    test.add_command(Shell('run', 'echo ok'))

def testset_build(testset):
    testset.set_name('top')
    child = testset.new_testset('sub')
    sub_build(child)
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        # Navigate: top -> sub testset -> deep test
        sub = r.testsets[0].testsets[0]
        assert sub.get_full_name() == 'top:sub'
        assert sub.tests[0].get_full_name() == 'top:sub:deep'


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------

class TestParallelExecution:
    """Tests for multi-threaded test execution."""

    def test_parallel_tests(self, tmp_path):
        """Multiple tests should all complete with parallel workers."""
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('parallel')
    for i in range(10):
        test = testset.new_test(f'test_{i}')
        test.add_command(Shell('run', 'echo ok'))
''')
        r = Runner(properties=[], flags=[], nb_threads=4)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 10
        assert r.stats.stats['failed'] == 0


# ---------------------------------------------------------------------------
# Environment and working directory
# ---------------------------------------------------------------------------

class TestEnvironment:
    """Tests for test working directory and environment."""

    def test_working_directory(self, tmp_path):
        """Test runs in the testset's directory."""
        subdir = tmp_path / 'workdir'
        subdir.mkdir()
        testset_file = subdir / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('cwd')
    test = testset.new_test('pwd_test')
    test.add_command(Shell('run', 'pwd > cwd_output.txt'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        
        output = (subdir / 'cwd_output.txt').read_text().strip()
        assert output == str(subdir)


# ---------------------------------------------------------------------------
# Targets with testsets
# ---------------------------------------------------------------------------

class TestTargetsInTestset:
    """Tests for target-aware testset execution."""

    def test_testset_with_target(self, tmp_path):
        # Define target in gvtest.yaml
        config = tmp_path / 'gvtest.yaml'
        config.write_text('targets:\n  my_target: {}\n')

        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def target_tests(testset):
    test = testset.new_test('hello')
    test.add_command(Shell('run', 'echo ok'))

def testset_build(testset):
    testset.set_name('targeted')
    testset.add_testset(callback=target_tests)
''')
        r = Runner(properties=[], flags=[], nb_threads=1, targets=['my_target'])
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1


    def test_cli_target_skips_untargeted_tests(self, tmp_path):
        """When --target X is specified, tests without any
        target definition should be skipped entirely."""
        # No gvtest.yaml → no targets defined
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('no_target')
    test = testset.new_test('should_not_run')
    test.add_command(Shell('run', 'echo bad'))
''')
        r = Runner(
            properties=[], flags=[], nb_threads=1,
            targets=['some_target']
        )
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        # No tests should have run
        assert r.stats.stats['passed'] == 0
        assert r.stats.stats['failed'] == 0

    def test_no_cli_target_runs_all(self, tmp_path):
        """When no --target is specified, tests without
        targets should run normally."""
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('default_run')
    test = testset.new_test('should_run')
    test.add_command(Shell('run', 'echo ok'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1

    def test_cli_target_runs_only_matching(self, tmp_path):
        """When --target X is specified, only tests with
        target X should run, not untargeted tests."""
        config = tmp_path / 'gvtest.yaml'
        config.write_text(
            'targets:\n  target_a: {}\n  target_b: {}\n'
        )

        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('multi')
    test = testset.new_test('targeted_test')
    test.add_command(Shell('run', 'echo ok'))
''')
        # Request only target_a
        r = Runner(
            properties=[], flags=[], nb_threads=1,
            targets=['target_a']
        )
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        # Should run once (target_a only, not target_b)
        assert r.stats.stats['passed'] == 1

    def test_cli_target_with_sub_testset_targets(
        self, tmp_path
    ):
        """When --target X is specified, root has no targets
        but a sub-testset defines X, the sub's tests should
        run and root's direct tests should be skipped."""
        # Sub-testset with targets
        sub_dir = tmp_path / 'sub'
        sub_dir.mkdir()
        sub_config = sub_dir / 'gvtest.yaml'
        sub_config.write_text(
            'targets:\n  spatz_v2: {}\n  rv64: {}\n'
        )
        sub_testset = sub_dir / 'testset.cfg'
        sub_testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('sub')
    test = testset.new_test('targeted_test')
    test.add_command(Shell('run', 'echo targeted'))
''')
        # Root testset imports sub, also has its own test
        root_testset = tmp_path / 'testset.cfg'
        root_testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('root')
    test = testset.new_test('untargeted_test')
    test.add_command(Shell('run', 'echo untargeted'))
    testset.import_testset('sub/testset.cfg')
''')
        r = Runner(
            properties=[], flags=[], nb_threads=1,
            targets=['spatz_v2']
        )
        r.add_testset(str(root_testset))
        r.start()
        r.run()
        r.stop()
        # Only the sub's test for spatz_v2 should run,
        # not the root's untargeted test
        assert r.stats.stats['passed'] == 1

    def test_multi_target_across_yaml_files(
        self, tmp_path
    ):
        """When --target A --target B and A/B come from
        different gvtest.yaml files, both should run."""
        # Sub1 has target_a
        sub1 = tmp_path / 'sub1'
        sub1.mkdir()
        (sub1 / 'gvtest.yaml').write_text(
            'targets:\n  target_a: {}\n'
        )
        (sub1 / 'testset.cfg').write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('sub1')
    test = testset.new_test('test_a')
    test.add_command(Shell('run', 'echo a'))
''')
        # Sub2 has target_b
        sub2 = tmp_path / 'sub2'
        sub2.mkdir()
        (sub2 / 'gvtest.yaml').write_text(
            'targets:\n  target_b: {}\n'
        )
        (sub2 / 'testset.cfg').write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('sub2')
    test = testset.new_test('test_b')
    test.add_command(Shell('run', 'echo b'))
''')
        # Root imports both
        root = tmp_path / 'testset.cfg'
        root.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('root')
    testset.import_testset('sub1/testset.cfg')
    testset.import_testset('sub2/testset.cfg')
''')
        r = Runner(
            properties=[], flags=[], nb_threads=1,
            targets=['target_a', 'target_b']
        )
        r.add_testset(str(root))
        r.start()
        r.run()
        r.stop()
        # Both sub-testsets should run (1 test each)
        assert r.stats.stats['passed'] == 2

    def test_target_default_runs_only_untargeted(
        self, tmp_path
    ):
        """--target default should run only tests without
        a target definition, skipping targeted tests."""
        # Sub with real targets
        sub = tmp_path / 'sub'
        sub.mkdir()
        (sub / 'gvtest.yaml').write_text(
            'targets:\n  target_a: {}\n'
        )
        (sub / 'testset.cfg').write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('sub')
    test = testset.new_test('targeted')
    test.add_command(Shell('run', 'echo targeted'))
''')
        # Root with untargeted test + import of sub
        root = tmp_path / 'testset.cfg'
        root.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('root')
    test = testset.new_test('untargeted')
    test.add_command(Shell('run', 'echo untargeted'))
    testset.import_testset('sub/testset.cfg')
''')
        r = Runner(
            properties=[], flags=[], nb_threads=1,
            targets=['default']
        )
        r.add_testset(str(root))
        r.start()
        r.run()
        r.stop()
        # Only the untargeted test should run
        assert r.stats.stats['passed'] == 1

    def test_target_default_and_named_together(
        self, tmp_path
    ):
        """--target default --target X should run both
        untargeted tests and tests for target X."""
        sub = tmp_path / 'sub'
        sub.mkdir()
        (sub / 'gvtest.yaml').write_text(
            'targets:\n  target_a: {}\n'
        )
        (sub / 'testset.cfg').write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('sub')
    test = testset.new_test('targeted')
    test.add_command(Shell('run', 'echo targeted'))
''')
        root = tmp_path / 'testset.cfg'
        root.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('root')
    test = testset.new_test('untargeted')
    test.add_command(Shell('run', 'echo untargeted'))
    testset.import_testset('sub/testset.cfg')
''')
        r = Runner(
            properties=[], flags=[], nb_threads=1,
            targets=['default', 'target_a']
        )
        r.add_testset(str(root))
        r.start()
        r.run()
        r.stop()
        # Both should run
        assert r.stats.stats['passed'] == 2

    def test_no_cli_target_runs_all_yaml_targets(
        self, tmp_path
    ):
        """When no --target is specified and YAML defines
        targets, all targets should run."""
        config = tmp_path / 'gvtest.yaml'
        config.write_text(
            'targets:\n  target_a: {}\n  target_b: {}\n'
        )

        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('all_targets')
    test = testset.new_test('run_both')
    test.add_command(Shell('run', 'echo ok'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        # Should run twice (once per target)
        assert r.stats.stats['passed'] == 2


# ---------------------------------------------------------------------------
# Config integration (gvtest.yaml + testset loading)
# ---------------------------------------------------------------------------

class TestConfigIntegration:
    """Tests for gvtest.yaml python_paths integration during testset loading."""

    def test_python_paths_available_during_load(self, tmp_path):
        """Modules from gvtest.yaml python_paths should be importable during testset_build."""
        # Create a Python package to import
        lib_dir = tmp_path / 'mylib'
        lib_dir.mkdir()
        (lib_dir / '__init__.py').write_text('')
        (lib_dir / 'helpers.py').write_text('MAGIC = 42\n')
        
        # Create gvtest.yaml pointing to the lib
        config = tmp_path / 'gvtest.yaml'
        config.write_text(f'python_paths:\n  - {lib_dir.parent}\n')
        
        # Create testset that imports from the lib
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
from mylib.helpers import MAGIC

def testset_build(testset):
    testset.set_name('config_test')
    test = testset.new_test('import_test')
    test.add_command(Shell('run', f'echo {MAGIC}'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1

    def test_python_paths_available_during_testset_build(self, tmp_path):
        """Imports inside testset_build() should work (paths still in sys.path)."""
        lib_dir = tmp_path / 'buildlib'
        lib_dir.mkdir()
        (lib_dir / '__init__.py').write_text('')
        (lib_dir / 'tool.py').write_text('CMD = "echo from_buildlib"\n')
        
        config = tmp_path / 'gvtest.yaml'
        config.write_text(f'python_paths:\n  - {lib_dir.parent}\n')
        
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    # This import happens inside testset_build — paths must still be available
    from buildlib.tool import CMD
    testset.set_name('build_import')
    test = testset.new_test('test')
    test.add_command(Shell('run', CMD))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1

    def test_python_paths_isolated_between_testsets(self, tmp_path):
        """After loading a testset, its python_paths should be removed from sys.path."""
        lib_dir = tmp_path / 'isolated_lib'
        lib_dir.mkdir()
        
        config = tmp_path / 'gvtest.yaml'
        config.write_text(f'python_paths:\n  - {lib_dir}\n')
        
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('isolation')
    test = testset.new_test('test')
    test.add_command(Shell('run', 'echo ok'))
''')
        
        original_path = sys.path.copy()
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        
        # The isolated_lib should NOT be in sys.path after loading
        assert str(lib_dir) not in sys.path


# ---------------------------------------------------------------------------
# Module name collision
# ---------------------------------------------------------------------------

class TestModuleIsolation:
    """Tests for unique module naming during testset import."""

    def test_two_testsets_dont_collide(self, tmp_path):
        """Two different testset files should not overwrite each other's modules."""
        dir_a = tmp_path / 'a'
        dir_a.mkdir()
        (dir_a / 'testset.cfg').write_text('''
from gvtest.testsuite import *
MARKER_A = "from_a"

def testset_build(testset):
    testset.set_name('suite_a')
    test = testset.new_test('test_a')
    test.add_command(Shell('run', f'echo {MARKER_A}'))
''')
        
        dir_b = tmp_path / 'b'
        dir_b.mkdir()
        (dir_b / 'testset.cfg').write_text('''
from gvtest.testsuite import *
MARKER_B = "from_b"

def testset_build(testset):
    testset.set_name('suite_b')
    test = testset.new_test('test_b')
    test.add_command(Shell('run', f'echo {MARKER_B}'))
''')
        
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(dir_a / 'testset.cfg'))
        r.add_testset(str(dir_b / 'testset.cfg'))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 2
        assert r.stats.stats['failed'] == 0
        # Verify each test got the right output
        run_a = r.testsets[0].tests[0].runs[0]
        run_b = r.testsets[1].tests[0].runs[0]
        assert 'from_a' in run_a.output
        assert 'from_b' in run_b.output


# ---------------------------------------------------------------------------
# Max output length
# ---------------------------------------------------------------------------

class TestMaxOutputLen:
    """Tests for --max-output-len enforcement."""

    def _run_testset(self, tmp_path, testset_content, **runner_kwargs):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(testset_content)
        defaults = {'properties': [], 'flags': [], 'nb_threads': 1}
        defaults.update(runner_kwargs)
        r = Runner(**defaults)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        return r

    def test_output_truncated(self, tmp_path):
        """Output beyond max_output_len should be truncated."""
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('trunc')
    test = testset.new_test('big_output')
    test.add_command(Shell('run', 'seq 1 10000'))
''', max_output_len=200)
        run = r.testsets[0].tests[0].runs[0]
        # Output should contain truncation notice
        assert 'truncated' in run.output.lower() or 'Truncated' in run.output

    def test_no_truncation_by_default(self, tmp_path):
        """Without max_output_len, output is not truncated."""
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('notrunc')
    test = testset.new_test('output')
    test.add_command(Shell('run', 'seq 1 100'))
''')
        run = r.testsets[0].tests[0].runs[0]
        assert 'truncated' not in run.output.lower()
        assert '100' in run.output


# ---------------------------------------------------------------------------
# Command filtering (--cmd / --cmd-exclude)
# ---------------------------------------------------------------------------

class TestCommandFiltering:
    """Tests for --cmd and --cmd-exclude options."""

    def _run_testset(self, tmp_path, testset_content, **runner_kwargs):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(testset_content)
        defaults = {'properties': [], 'flags': [], 'nb_threads': 1}
        defaults.update(runner_kwargs)
        r = Runner(**defaults)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        return r

    def test_cmd_filter_runs_only_selected(self, tmp_path):
        """--cmd should run only the named commands."""
        marker = tmp_path / 'step2_ran.txt'
        r = self._run_testset(tmp_path, f'''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('cmdfilter')
    test = testset.new_test('test')
    test.add_command(Shell('step1', 'echo step1'))
    test.add_command(Shell('step2', 'touch {marker}'))
''', commands=['step1'])
        # step2 should NOT have run
        assert not marker.exists()

    def test_cmd_exclude_skips_command(self, tmp_path):
        """--cmd-exclude should skip the named commands."""
        marker = tmp_path / 'clean_ran.txt'
        r = self._run_testset(tmp_path, f'''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('cmdexclude')
    test = testset.new_test('test')
    test.add_command(Shell('clean', 'touch {marker}'))
    test.add_command(Shell('run', 'echo ok'))
''', commands_exclude=['clean'])
        # clean should NOT have run
        assert not marker.exists()
        assert r.stats.stats['passed'] == 1

    def test_no_cmd_filter_runs_all(self, tmp_path):
        """Without --cmd/--cmd-exclude, all commands run."""
        marker = tmp_path / 'all_ran.txt'
        r = self._run_testset(tmp_path, f'''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('allcmds')
    test = testset.new_test('test')
    test.add_command(Shell('step1', 'echo ok'))
    test.add_command(Shell('step2', 'touch {marker}'))
''')
        assert marker.exists()


# ---------------------------------------------------------------------------
# Graceful interrupt
# ---------------------------------------------------------------------------

class TestGracefulInterrupt:
    """Tests for signal handling."""

    def test_interrupted_flag_clears_pending(self, tmp_path):
        """Setting _interrupted should prevent pending tests from being dispatched."""
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('int')
    for i in range(5):
        test = testset.new_test(f'test_{i}')
        test.add_command(Shell('run', 'sleep 10'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        
        # Mark as interrupted before running — check_pending_tests should exit early
        r._interrupted = True
        
        # Enqueue tests
        for testset in r.testsets:
            testset.enqueue()
        
        # Check that pending tests get cleared
        r.check_pending_tests()
        r.stop()
        
        # All pending tests should have been dropped
        assert len(r.pending_tests) == 0
        assert r.nb_pending_tests == 0


# ---------------------------------------------------------------------------
# Resource locks (per-command serialization)
# ---------------------------------------------------------------------------

class TestResources:
    """Tests for Command.resources / Runner resource registry."""

    def test_command_resources_default_none(self):
        assert Shell('a', 'echo').resources is None
        assert Call('a', lambda: 0).resources is None
        assert Checker('a', lambda *a, **k: (True, None)).resources is None

    def test_command_resources_stored(self):
        s = Shell('a', 'echo', resources=['r1', 'r2'])
        assert s.resources == ['r1', 'r2']

    def test_declare_resource_default_capacity(self):
        r = Runner(properties=[], flags=[])
        r.declare_resource('build')
        entry = r._resources['build']
        assert entry.capacity == 1

    def test_declare_resource_custom_capacity(self):
        r = Runner(properties=[], flags=[])
        r.declare_resource('build', capacity=4)
        assert r._resources['build'].capacity == 4

    def test_declare_resource_idempotent(self):
        r = Runner(properties=[], flags=[])
        r.declare_resource('build', capacity=2)
        r.declare_resource('build', capacity=2)
        assert r._resources['build'].capacity == 2

    def test_declare_resource_mismatch_raises(self):
        r = Runner(properties=[], flags=[])
        r.declare_resource('build', capacity=1)
        with pytest.raises(ValueError):
            r.declare_resource('build', capacity=2)

    def test_declare_resource_invalid_capacity(self):
        r = Runner(properties=[], flags=[])
        with pytest.raises(ValueError):
            r.declare_resource('build', capacity=0)

    def test_acquire_undeclared_creates_resource(self):
        r = Runner(properties=[], flags=[])
        r.acquire_resource('adhoc')
        assert 'adhoc' in r._resources
        assert r._resources['adhoc'].capacity == 1
        r.release_resource('adhoc')

    def test_resource_serializes_acquirers(self):
        r = Runner(properties=[], flags=[])
        r.declare_resource('lock', capacity=1)
        r.acquire_resource('lock')
        # Second acquire from another thread must block until
        # the first releases.
        acquired = threading.Event()

        def grab():
            r.acquire_resource('lock')
            acquired.set()
            r.release_resource('lock')

        t = threading.Thread(target=grab, daemon=True)
        t.start()
        # Give the other thread a chance to reach acquire
        assert not acquired.wait(timeout=0.2)
        r.release_resource('lock')
        assert acquired.wait(timeout=2.0)
        t.join(timeout=2.0)

    def test_resource_capacity_n(self):
        r = Runner(properties=[], flags=[])
        r.declare_resource('pool', capacity=2)
        r.acquire_resource('pool')
        r.acquire_resource('pool')
        # Third acquire must block
        blocked = threading.Event()
        released = threading.Event()

        def grab():
            r.acquire_resource('pool')
            released.set()
            r.release_resource('pool')

        t = threading.Thread(target=grab, daemon=True)
        t.start()
        assert not released.wait(timeout=0.2)
        r.release_resource('pool')
        assert released.wait(timeout=2.0)
        r.release_resource('pool')
        t.join(timeout=2.0)

    def test_cli_resource_spec_parsing(self, tmp_path):
        r = Runner(properties=[], flags=[],
                   resources=['build', 'io:4'])
        assert r._resources['build'].capacity == 1
        assert r._resources['io'].capacity == 4

    def test_cli_resource_spec_invalid(self):
        with pytest.raises(ValueError):
            Runner(properties=[], flags=[],
                   resources=['bad:notanint'])

    def test_build_resource_sugar_make(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('make')
    testset.new_make_test('t1', flags="X=1",
                          build_resource='shared.build')
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        try:
            test = r.testsets[0].tests[0]
            names = [c.name for c in test.commands]
            assert names[0] == 'clean'
            assert names[1] == 'build'
            assert test.commands[0].resources == ['shared.build']
            assert test.commands[1].resources == ['shared.build']
            # run / check are untouched
            assert test.commands[2].name == 'run'
            assert test.commands[2].resources is None
        finally:
            r.stop()

    def test_no_clean_drops_clean_command(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('no_clean')
    testset.new_make_test('with_clean', flags="")
    testset.new_make_test('without_clean', flags="", no_clean=True)
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        try:
            tests = {t.name: t for t in r.testsets[0].tests}
            with_clean_cmds = [
                c.name for c in tests['with_clean'].commands
            ]
            assert with_clean_cmds[0] == 'clean'
            assert 'clean' in with_clean_cmds
            without_clean_cmds = [
                c.name for c in tests['without_clean'].commands
            ]
            assert 'clean' not in without_clean_cmds
            assert without_clean_cmds[0] == 'build'
            assert without_clean_cmds[1] == 'run'
        finally:
            r.stop()

    def test_declare_resource_shared_across_testsets(
            self, tmp_path):
        sub_dir = tmp_path / 'sub'
        sub_dir.mkdir()
        (sub_dir / 'testset.cfg').write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('sub')
    testset.new_make_test('t', flags="",
                          build_resource='global.build')
''')
        (tmp_path / 'testset.cfg').write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('top')
    testset.declare_resource('global.build', capacity=1)
    testset.import_testset('sub/testset.cfg')
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(tmp_path / 'testset.cfg'))
        r.start()
        try:
            # Resource declared at top is visible to sub's test
            assert 'global.build' in r._resources
            sub_test = r.testsets[0].testsets[0].tests[0]
            assert sub_test.commands[0].resources == [
                'global.build'
            ]
        finally:
            r.stop()

    def test_testrun_holds_and_releases(self, tmp_path):
        """End-to-end: two serialized commands must not overlap."""
        import time
        marker = tmp_path / 'marker'
        marker.write_text('0')
        testset_file = tmp_path / 'testset.cfg'
        # Two tests, each runs a shell that increments a
        # counter, sleeps, then checks the counter is still
        # its own value. If the resource is honored, the two
        # critical sections never overlap.
        testset_file.write_text(f'''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('ser')
    for i in range(2):
        t = testset.new_test(f'x{{i}}')
        t.add_command(Shell('run',
            'flock -xn {marker}.lock -c "sleep 0.5" '
            '|| (echo CONTENDED; exit 1)',
            resources=['lock']))
''')
        r = Runner(properties=[], flags=[], nb_threads=4)
        r.add_testset(str(testset_file))
        r.start()
        try:
            r.run()
        finally:
            r.stop()
        # Both tests must pass: the semaphore prevents two
        # workers from holding the flock concurrently
        passed = r.stats.stats['passed']
        failed = r.stats.stats['failed']
        assert passed == 2, (
            f"expected 2 passed, got passed={passed}, "
            f"failed={failed}"
        )

    def test_resource_released_on_failure(self, tmp_path):
        """A failing command must still release its resource."""
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('rel')
    t1 = testset.new_test('fail')
    t1.add_command(Shell('run', 'false', resources=['x']))
    t2 = testset.new_test('ok')
    t2.add_command(Shell('run', 'true', resources=['x']))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        try:
            r.run()
        finally:
            r.stop()
        # t1 fails, t2 passes; if the resource leaked t2
        # would deadlock and the whole run would hang.
        assert r.stats.stats['failed'] == 1
        assert r.stats.stats['passed'] == 1


# ---------------------------------------------------------------------------
# Resource scheduling — gating and ASAP hand-off
# ---------------------------------------------------------------------------

class TestResourceScheduling:
    """Tests for the dispatch-time resource gate and the
    release-time hand-off that promotes a parked test directly
    onto the worker queue."""

    def test_dispatcher_pre_claims_for_resource_test(self, tmp_path):
        """When a test is dispatched, the resources it needs are
        already claimed (in_use incremented) and recorded on the
        TestRun so the worker's first acquire is a no-op."""
        from gvtest.runner import _Resource
        testset_file = tmp_path / 'testset.cfg'
        # Single capacity-1 test that sleeps long enough for us to
        # observe the in-flight state.
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('s')
    t = testset.new_test('hold')
    t.add_command(Shell('run', 'sleep 0.5', resources=['lock']))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.declare_resource('lock', capacity=1)
        r.add_testset(str(testset_file))
        r.start()
        try:
            r.run()
        finally:
            r.stop()
        # After the test finishes, in_use must be back to 0 and no
        # waiters remain. (Mid-run we'd see in_use=1, but the test
        # has finished by the time r.run() returns.)
        assert r._resources['lock'].in_use == 0
        assert len(r._resources['lock'].waiters) == 0

    def test_blocked_test_parks_not_queues(self, tmp_path):
        """When a resource is full, the second test must park on
        the resource's waiters list rather than going to the worker
        queue (where it would block a worker)."""
        # Pre-claim 'lock' from the test thread so the dispatcher
        # finds it full when it tries to dispatch the test.
        r = Runner(properties=[], flags=[], nb_threads=0)
        r.declare_resource('lock', capacity=1)
        # Build two tests that both want 'lock'.
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('p')
    a = testset.new_test('a')
    a.add_command(Shell('run', 'true', resources=['lock']))
    b = testset.new_test('b')
    b.add_command(Shell('run', 'true', resources=['lock']))
''')
        r.add_testset(str(testset_file))
        # Manually pre-claim the resource so no slot is free.
        r._resources['lock'].in_use = 1
        # Run enqueue + dispatcher loop without workers.
        r._interrupted = False
        for testset in r.testsets:
            testset.enqueue()
        # check_pending_tests blocks while pending_tests > 0; after
        # parking both tests on the wait list, pending_tests is
        # empty and the loop exits cleanly.
        # Drain the loop in a thread with a watchdog so we can fail
        # the test if the loop hangs.
        done = threading.Event()

        def drive():
            r.check_pending_tests()
            done.set()

        threading.Thread(target=drive, daemon=True).start()
        # The dispatcher's "all blocked" branch sleeps 0.1s in a
        # tight loop until pending_tests is empty; with both tests
        # parked, it should drop out within one iteration.
        # Since pending_tests becomes empty as soon as the second
        # test is parked, the loop exits immediately on the next
        # iteration.
        assert done.wait(timeout=2.0)
        # Both tests must be on the waiters deque, not in the
        # worker queue, and not in pending_tests.
        assert len(r.pending_tests) == 0
        assert len(r._resources['lock'].waiters) == 2
        assert r.queue.qsize() == 0

    def test_release_promotes_waiter_to_queue(self, tmp_path):
        """Releasing a resource pops a waiter and pushes it
        directly onto the worker queue (ASAP hand-off)."""
        r = Runner(properties=[], flags=[], nb_threads=0)
        r.declare_resource('lock', capacity=1)
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('h')
    a = testset.new_test('a')
    a.add_command(Shell('run', 'true', resources=['lock']))
    b = testset.new_test('b')
    b.add_command(Shell('run', 'true', resources=['lock']))
''')
        r.add_testset(str(testset_file))
        # Simulate "test A is currently running with the lock":
        # the resource is fully in use, both tests are parked.
        r._resources['lock'].in_use = 1
        r._interrupted = False
        for testset in r.testsets:
            testset.enqueue()

        done = threading.Event()
        threading.Thread(
            target=lambda: (r.check_pending_tests(), done.set()),
            daemon=True,
        ).start()
        assert done.wait(timeout=2.0)
        assert len(r._resources['lock'].waiters) == 2

        # Release "test A's" claim. The first waiter should be
        # promoted directly onto the queue.
        r.release_resource('lock')
        # Now in_use = 1 again (claimed for the promoted waiter)
        # and the queue has exactly one test.
        assert r._resources['lock'].in_use == 1
        assert r.queue.qsize() == 1
        assert len(r._resources['lock'].waiters) == 1

        # Drain the promoted test (we have to release on its
        # behalf since we have no workers).
        promoted = r.queue.get_nowait()
        assert 'lock' in promoted.pre_claimed_resources
        r.release_resource('lock')
        # Second waiter promoted.
        assert r.queue.qsize() == 1
        assert len(r._resources['lock'].waiters) == 0

    def test_resource_tests_picked_before_non_resource(self, tmp_path):
        """When the dispatcher has both resource-using and
        non-resource candidates eligible, the resource-using ones
        must go to the worker queue first so their (serial) build
        phase overlaps with the (parallel) phase of non-resource
        tests instead of clustering at the tail of the run.
        """
        r = Runner(properties=[], flags=[], nb_threads=0)
        r.declare_resource('build', capacity=1)
        # Mimic the user's natural enqueue order: nested
        # build-resource tests first, sibling non-resource tests
        # after. Names are tagged so we can read them back from
        # the worker queue.
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('mix')
    for i in range(3):
        t = testset.new_test(f'res{i}')
        t.add_command(Shell('run', 'true', resources=['build']))
    for i in range(5):
        t = testset.new_test(f'free{i}')
        t.add_command(Shell('run', 'true'))
''')
        r.add_testset(str(testset_file))
        r._interrupted = False
        for testset in r.testsets:
            testset.enqueue()
        # Run the dispatcher with 0 workers; everything dispatched
        # ends up sitting on the worker queue in order.
        done = threading.Event()
        threading.Thread(
            target=lambda: (r.check_pending_tests(), done.set()),
            daemon=True,
        ).start()
        assert done.wait(timeout=2.0)
        # First test pushed to the queue must be a resource-using
        # one (capacity 1 → exactly one is dispatched, the other
        # two are parked).
        first = r.queue.get_nowait()
        assert first.test.name.startswith('res'), (
            f"first dispatched test should be a resource-using "
            f"one, got {first.test.name!r}"
        )
        # The other two resource-using tests must be parked, not
        # in the queue.
        assert len(r._resources['build'].waiters) == 2
        # Non-resource tests fill the remaining queue slots.
        rest = []
        while not r.queue.empty():
            rest.append(r.queue.get_nowait().test.name)
        assert all(n.startswith('free') for n in rest), rest
        assert len(rest) == 5

    def test_promoted_waiter_jumps_to_queue_front(self, tmp_path):
        """A waiter promoted on resource release must be picked
        up next, ahead of any non-resource tests already sitting
        in the dispatch queue."""
        r = Runner(properties=[], flags=[], nb_threads=0)
        r.declare_resource('build', capacity=1)
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('jump')
    for i in range(2):
        t = testset.new_test(f'res{i}')
        t.add_command(Shell('run', 'true', resources=['build']))
    for i in range(3):
        t = testset.new_test(f'free{i}')
        t.add_command(Shell('run', 'true'))
''')
        r.add_testset(str(testset_file))
        r._interrupted = False
        for testset in r.testsets:
            testset.enqueue()

        done = threading.Event()
        threading.Thread(
            target=lambda: (r.check_pending_tests(), done.set()),
            daemon=True,
        ).start()
        assert done.wait(timeout=2.0)

        # After the dispatcher runs: one resource test was
        # dispatched first, three non-resource tests follow it,
        # one resource test is parked.
        assert r.queue.qsize() == 4
        assert len(r._resources['build'].waiters) == 1

        # Drop the running resource-test (simulating it having
        # been picked by a worker) so we can release the lock.
        running = r.queue.get_nowait()
        assert running.test.name.startswith('res')

        # Queue now holds three non-resource tests in FIFO order.
        # Releasing 'build' must promote the parked resource-test
        # to the FRONT of the queue, not the back.
        r.release_resource('build')
        promoted = r.queue.get_nowait()
        assert promoted.test.name.startswith('res'), (
            f"promoted waiter should be picked next, but got "
            f"{promoted.test.name!r}"
        )

    def test_capacity_n_lets_n_run(self, tmp_path):
        """With capacity=N, the first N resource-locked tests
        must dispatch concurrently and the (N+1)'th must park."""
        r = Runner(properties=[], flags=[], nb_threads=0)
        r.declare_resource('pool', capacity=2)
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('cn')
    for i in range(3):
        t = testset.new_test(f't{i}')
        t.add_command(Shell('run', 'true', resources=['pool']))
''')
        r.add_testset(str(testset_file))
        r._interrupted = False
        for testset in r.testsets:
            testset.enqueue()

        done = threading.Event()
        threading.Thread(
            target=lambda: (r.check_pending_tests(), done.set()),
            daemon=True,
        ).start()
        assert done.wait(timeout=2.0)
        assert r._resources['pool'].in_use == 2
        assert r.queue.qsize() == 2
        assert len(r._resources['pool'].waiters) == 1

    def test_resource_locked_run_in_parallel_with_others(self, tmp_path):
        """End-to-end: with one capacity-1 build resource and a
        pool of non-resource tests, the build-resource tests
        serialize while non-resource tests run in parallel — and
        no worker dwells inside acquire_resource."""
        import time
        testset_file = tmp_path / 'testset.cfg'
        # 2 build-locked tests (each sleeps 0.3s) + 4 free tests
        # (each sleeps 0.3s). With 4 workers, ideal wall time is
        # ~max(2*0.3, 0.3) = 0.6s. The old behavior would block
        # workers and approach 2*0.3 + setup overhead.
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('mix')
    for i in range(2):
        t = testset.new_test(f'lock{i}')
        t.add_command(Shell('run', 'sleep 0.3', resources=['build']))
    for i in range(4):
        t = testset.new_test(f'free{i}')
        t.add_command(Shell('run', 'sleep 0.3'))
''')
        r = Runner(properties=[], flags=[], nb_threads=4)
        r.declare_resource('build', capacity=1)
        r.add_testset(str(testset_file))
        r.start()
        try:
            t0 = time.monotonic()
            r.run()
            elapsed = time.monotonic() - t0
        finally:
            r.stop()
        assert r.stats.stats['passed'] == 6
        assert r.stats.stats['failed'] == 0
        # Loose bound: well under the worst-case serial time.
        assert elapsed < 1.5, (
            f"expected ~0.6s, got {elapsed:.2f}s"
        )

    def test_interrupt_drains_waiters(self, tmp_path):
        """SIGINT must clear both pending_tests AND parked
        waiters, so nb_pending_tests can reach 0 and run() can
        return."""
        r = Runner(properties=[], flags=[], nb_threads=0)
        r.declare_resource('lock', capacity=1)
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('i')
    for i in range(3):
        t = testset.new_test(f't{i}')
        t.add_command(Shell('run', 'true', resources=['lock']))
''')
        r.add_testset(str(testset_file))
        r._resources['lock'].in_use = 1  # nothing can dispatch
        r._interrupted = False
        for testset in r.testsets:
            testset.enqueue()

        # Park all 3 tests on the lock's waiter list.
        done = threading.Event()
        threading.Thread(
            target=lambda: (r.check_pending_tests(), done.set()),
            daemon=True,
        ).start()
        assert done.wait(timeout=2.0)
        assert len(r._resources['lock'].waiters) == 3
        assert r.nb_pending_tests == 3

        # Drive the SIGINT handler directly. It must drain the
        # waiters and bring nb_pending_tests back to 0.
        r._handle_interrupt(0, None)
        assert len(r._resources['lock'].waiters) == 0
        assert r.nb_pending_tests == 0
        assert r.event.is_set()
