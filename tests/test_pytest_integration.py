"""
Tests for gvtest.pytest_integration — discovery options
(markers, strict mode, container wrapping), batch command
construction, and result mapping.
"""

import io
import os
import re
import subprocess
import threading
import time

import pytest

from gvtest.pytest_integration import (
    PytestTestRun,
    PytestTestset,
    _shell_join,
    _to_text,
    discover_pytest_tests,
)
from gvtest.runner import Runner, Target


class FakeCompleted:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestShellJoin:
    def test_quotes_spaces(self):
        joined = _shell_join(
            ['pytest', '-m', 'siracusa and kernels']
        )
        assert joined == (
            "pytest -m 'siracusa and kernels'"
        )

    def test_plain_args_untouched(self):
        assert _shell_join(['pytest', '-v']) == 'pytest -v'


class TestToText:
    def test_none(self):
        assert _to_text(None) == ''

    def test_bytes(self):
        assert _to_text(b'abc') == 'abc'

    def test_str(self):
        assert _to_text('abc') == 'abc'


class TestDiscovery:
    def test_markers_in_collect_cmd(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            return FakeCompleted(
                stdout='test_a.py::test_x\n'
            )

        monkeypatch.setattr(subprocess, 'run', fake_run)
        node_ids, exe, rc, stderr = discover_pytest_tests(
            str(tmp_path), 'pytest',
            markers='siracusa and kernels'
        )
        assert node_ids == ['test_a.py::test_x']
        assert rc == 0
        idx = captured['cmd'].index('-m')
        assert captured['cmd'][idx + 1] == \
            'siracusa and kernels'

    def test_container_wraps_collect_cmd(
        self, monkeypatch, tmp_path
    ):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            return FakeCompleted(
                stdout='test_a.py::test_x\n'
            )

        monkeypatch.setattr(subprocess, 'run', fake_run)
        target = Target(
            'deeploy',
            '{"container": {"image": "img"}}'
        )
        target.config_dir = str(tmp_path)
        node_ids, _, _, _ = discover_pytest_tests(
            str(tmp_path), 'pytest', target=target
        )
        assert node_ids == ['test_a.py::test_x']
        assert captured['cmd'][0] == 'podman'
        assert 'img' in captured['cmd']
        # pytest command is the bash -c payload
        assert 'pytest' in captured['cmd'][-1]

    def test_collect_failure_returns_rc(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            subprocess, 'run',
            lambda cmd, **kwargs: FakeCompleted(
                stderr='boom', returncode=2
            )
        )
        node_ids, _, rc, stderr = discover_pytest_tests(
            str(tmp_path), 'pytest'
        )
        assert node_ids == []
        assert rc == 2
        assert stderr == 'boom'


def _make_testset(runner, tmp_path, **kwargs):
    return PytestTestset(
        runner, None, 'pt', None, str(tmp_path),
        str(tmp_path), 'pytest', **kwargs
    )


class TestStrictMode:
    def test_strict_raises_on_failure(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            subprocess, 'run',
            lambda cmd, **kwargs: FakeCompleted(
                stderr='import error', returncode=2
            )
        )
        runner = Runner(properties=[], flags=[])
        ts = _make_testset(runner, tmp_path, strict=True)
        with pytest.raises(
            RuntimeError, match='collection failed'
        ):
            ts.discover()

    def test_strict_raises_on_empty(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            subprocess, 'run',
            lambda cmd, **kwargs: FakeCompleted(stdout='')
        )
        runner = Runner(properties=[], flags=[])
        ts = _make_testset(runner, tmp_path, strict=True)
        with pytest.raises(
            RuntimeError, match='collection failed'
        ):
            ts.discover()

    def test_non_strict_silent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, 'run',
            lambda cmd, **kwargs: FakeCompleted(
                returncode=2
            )
        )
        runner = Runner(properties=[], flags=[])
        ts = _make_testset(runner, tmp_path)
        ts.discover()
        assert ts.tests == []


class FakePopen:
    """Stub for the batch pytest process: replays canned
    stdout lines and writes a canned JUnit XML."""

    stdout_text = ''
    xml_text = '<testsuites></testsuites>'

    def __init__(self, cmd, **kwargs):
        FakePopen.captured['cmd'] = cmd
        match = re.search(r'--junit-xml=(\S+)', cmd[-1])
        if match:
            with open(match.group(1), 'w') as f:
                f.write(FakePopen.xml_text)
        self.stdout = io.StringIO(FakePopen.stdout_text)
        self.pid = os.getpid()
        self.returncode = 0

    def wait(self):
        return 0


class TestBatchCommand:
    """The batch pytest command must include markers, xdist
    and extra args, and report results live."""

    def _run_batch_capture(
        self, monkeypatch, tmp_path,
        batch_stdout='',
        batch_xml='<testsuites></testsuites>',
        nb_threads=0, **kwargs
    ):
        FakePopen.captured = {}
        FakePopen.stdout_text = batch_stdout
        FakePopen.xml_text = batch_xml

        runner = Runner(
            properties=[], flags=[], nb_threads=nb_threads
        )
        # Normally set by Runner.start(); we avoid spawning
        # worker threads in this unit test.
        runner._interrupted = False
        ts = _make_testset(runner, tmp_path, **kwargs)

        monkeypatch.setattr(
            subprocess, 'run',
            lambda cmd, **k: FakeCompleted(
                stdout='test_a.py::test_x\n'
            )
        )
        ts.discover()
        assert len(ts.tests) == 1

        monkeypatch.setattr(subprocess, 'Popen', FakePopen)

        timers = []
        real_timer = threading.Timer

        def fake_timer(interval, fn):
            timers.append(interval)
            return real_timer(interval, fn)

        monkeypatch.setattr(threading, 'Timer', fake_timer)

        ts.enqueue()
        # enqueue launches a daemon thread; wait for the
        # batch to fully finish (pending count back to 0 —
        # results may be set live before the XML is parsed)
        run = ts.tests[0].runs[-1]
        assert run._result_set.wait(timeout=10)
        for _ in range(1000):
            if runner.nb_pending_tests == 0:
                break
            time.sleep(0.01)
        assert runner.nb_pending_tests == 0
        FakePopen.captured['timers'] = timers
        return FakePopen.captured, run

    def test_markers_xdist_args(
        self, monkeypatch, tmp_path
    ):
        captured, _ = self._run_batch_capture(
            monkeypatch, tmp_path,
            markers='siracusa and kernels',
            xdist=4,
            pytest_args=['--toolchain', 'LLVM'],
        )
        # Non-container: argv is ['bash', '-c', cmdline]
        assert captured['cmd'][0] == 'bash'
        cmdline = captured['cmd'][2]
        assert "-m 'siracusa and kernels'" in cmdline
        assert '-n 4' in cmdline
        assert '--toolchain LLVM' in cmdline

    def test_batch_exe_used_for_run_only(
        self, monkeypatch, tmp_path
    ):
        # batch_exe wraps the run phase (e.g. a lock) while
        # discovery keeps using pytest_exe
        captured, _ = self._run_batch_capture(
            monkeypatch, tmp_path,
            batch_exe='flock /tmp/x.lock pytest',
        )
        assert captured['cmd'][2].startswith(
            'flock /tmp/x.lock pytest '
        )

    def test_xdist_follows_runner_threads(
        self, monkeypatch, tmp_path
    ):
        # xdist=-1 takes the worker count from the gvtest
        # --threads option
        captured, _ = self._run_batch_capture(
            monkeypatch, tmp_path,
            nb_threads=7, xdist=-1,
        )
        assert '-n 7' in captured['cmd'][2]

    def test_xdist_minus_one_defaults_to_cpus(
        self, monkeypatch, tmp_path
    ):
        captured, _ = self._run_batch_capture(
            monkeypatch, tmp_path, xdist=-1,
        )
        assert f"-n {os.cpu_count()}" in captured['cmd'][2]

    def test_batch_timeout_overrides_runner(
        self, monkeypatch, tmp_path
    ):
        captured, _ = self._run_batch_capture(
            monkeypatch, tmp_path, batch_timeout=1234
        )
        assert 1234 in captured['timers']

    def test_missing_result_reported_failed(
        self, monkeypatch, tmp_path
    ):
        # Batch produced no JUnit entry for the test: it
        # must be reported failed without raising
        # (regression test for the undefined _result_set
        # attribute).
        _, run = self._run_batch_capture(
            monkeypatch, tmp_path
        )
        assert run.status == 'failed'
        assert 'not found in pytest results' in run.output

    def test_live_result_reported(
        self, monkeypatch, tmp_path
    ):
        # A verbose result line in the batch output reports
        # the test before the JUnit XML is parsed; the test
        # keeps its live status even though the XML has no
        # entry for it.
        _, run = self._run_batch_capture(
            monkeypatch, tmp_path,
            batch_stdout=(
                'collecting ...\n'
                'test_a.py::test_x PASSED [100%]\n'
            ),
        )
        assert run.status == 'passed'

    def test_live_result_xdist_format(
        self, monkeypatch, tmp_path
    ):
        _, run = self._run_batch_capture(
            monkeypatch, tmp_path,
            batch_stdout=(
                '4 workers [1 item]\n'
                '[gw2] [100%] FAILED test_a.py::test_x \n'
            ),
        )
        assert run.status == 'failed'

    def test_live_result_still_gets_xml_output(
        self, monkeypatch, tmp_path
    ):
        # A live-reported test must still receive its output
        # and duration from the JUnit XML parsed at the end
        # of the batch (regression: terminating at live time
        # let the runner exit before the XML parse)
        _, run = self._run_batch_capture(
            monkeypatch, tmp_path,
            batch_stdout=(
                'test_a.py::test_x PASSED [100%]\n'
            ),
            batch_xml=(
                '<testsuite><testcase classname="test_a" '
                'name="test_x" time="2.5">'
                '<system-out>the gvsoc command line'
                '</system-out></testcase></testsuite>'
            ),
        )
        assert run.status == 'passed'
        assert 'the gvsoc command line' in run.output
        assert run.duration == 2.5
