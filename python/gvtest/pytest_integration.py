#!/usr/bin/env python3

#
# Copyright (C) 2023 ETH Zurich, University of Bologna
# and GreenWaves Technologies
#
# Licensed under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of
# the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in
# writing, software distributed under the License is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing
# permissions and limitations under the License.
#

"""
Pytest integration — discover pytest tests and run them
as gvtest tests. Tests are discovered at build time and
executed as a single batched pytest invocation at run time.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from rich.console import Console

from gvtest.tests import TestCommon, TestRun

_console = Console(highlight=False)
logger = logging.getLogger(__name__)


def _build_pytest_cmd(
    pytest_exe: str, args: list[str]
) -> list[str]:
    """Build the command list for running pytest.

    Handles both direct executable (``pytest``) and module
    invocation (``python -m pytest``).
    """
    parts = pytest_exe.split()
    return parts + args


def _shell_join(cmd: list[str]) -> str:
    """Join an argv into a shell-safe command string."""
    return ' '.join(shlex.quote(c) for c in cmd)


# Per-test result lines in pytest verbose output, used to
# report results live while the batch is still running:
#   with xdist:  "[gw0] [ 20%] PASSED path::test[param]"
#   without:     "path::test[param] PASSED [ 20%]"
_LIVE_RESULT_RE = re.compile(
    r'^\[gw\d+\]\s+\[\s*\d+%\]\s+'
    r'(?P<status1>PASSED|FAILED|ERROR|SKIPPED|XPASS|XFAIL)'
    r'\s+(?P<node1>\S+)'
    r'|^(?P<node2>\S+::\S+)\s+'
    r'(?P<status2>PASSED|FAILED|ERROR|SKIPPED|XPASS|XFAIL)\b'
)

_LIVE_STATUS = {
    'PASSED': 'passed',
    'XPASS': 'passed',
    'FAILED': 'failed',
    'ERROR': 'failed',
    'SKIPPED': 'skipped',
    'XFAIL': 'skipped',
}


def _live_match_to_result(
    match: re.Match
) -> tuple[str, str]:
    """Extract (status, node_id) from a live result line."""
    status = match.group('status1') or \
        match.group('status2')
    node_id = match.group('node1') or match.group('node2')
    return _LIVE_STATUS[status], node_id


def _to_text(value: str | bytes | None) -> str:
    """Normalize subprocess output that may be str, bytes
    or None (TimeoutExpired yields either depending on the
    text mode)."""
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode(errors='replace')
    return value


def discover_pytest_tests(
    path: str, pytest_exe: str = 'pytest',
    markers: str | None = None,
    target: Any | None = None,
) -> tuple[list[str], str, int, str]:
    """Run pytest --collect-only to discover test node IDs.

    Args:
        path: Directory or file to discover tests in.
        pytest_exe: Pytest executable name/path.
        markers: Optional ``-m`` marker expression, applied
            at collection so node ids match the batch run.
        target: Optional Target; its container, sourceme and
            envvars wrap the collection command so discovery
            sees the same environment as the batch run.

    Returns:
        Tuple of (node_ids, resolved_exe, returncode,
        stderr). The resolved_exe may differ from pytest_exe
        if a fallback was used.
    """
    cwd = os.path.dirname(path) or '.'
    # Force the rootdir to the execution directory: pytest
    # prints node ids relative to its rootdir (e.g. where a
    # pytest.ini lives), but the batch run resolves them
    # relative to its cwd — pin both to the same directory.
    args = ['--collect-only', '-q', f'--rootdir={cwd}']
    if markers is not None:
        args += ['-m', markers]
    args.append(path)
    cmd = _build_pytest_cmd(pytest_exe, args)

    container = (
        target.get_container() if target is not None
        else None
    )
    if container is not None:
        from gvtest.container import (
            build_container_shell_cmd,
        )
        cmd, _ = build_container_shell_cmd(
            container, _shell_join(cmd), cwd,
            target.get_sourceme(), target.get_envvars()
        )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=300,
            cwd=cwd
        )
    except FileNotFoundError:
        # Try fallback: use current Python interpreter
        if pytest_exe == 'pytest' and container is None:
            fallback = f'{sys.executable} -m pytest'
            logger.debug(
                "'pytest' not found in PATH, trying "
                f"'{fallback}'"
            )
            return discover_pytest_tests(
                path, fallback, markers, target
            )
        logger.error(
            f"pytest executable '{pytest_exe}' not found"
        )
        return [], pytest_exe, -1, 'executable not found'
    except subprocess.TimeoutExpired:
        logger.error("pytest --collect-only timed out")
        return [], pytest_exe, -1, 'collection timed out'

    node_ids: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        # pytest -q output: "test_file.py::test_name"
        # Skip summary lines like "X tests collected"
        if '::' in line and not line.startswith('='):
            node_ids.append(line)

    logger.debug(
        f"Discovered {len(node_ids)} pytest tests in {path}"
    )
    return node_ids, pytest_exe, result.returncode, \
        result.stderr


class PytestTestRun(TestRun):
    """A test run for a pytest test. Does not execute
    individually — results are set by the batch runner."""

    def __init__(
        self, test: TestCommon, target: Any | None
    ) -> None:
        super().__init__(test, target)
        self._result_set = threading.Event()

    def run(self) -> None:
        """Not used — results are set directly by the
        batch runner."""
        pass

    def set_result(
        self, status: str, output: str, duration: float
    ) -> None:
        """Called by the batch runner to set this test's
        result."""
        self.status = status
        self.output = output
        self.duration = duration
        self._result_set.set()


class PytestTest(TestCommon):
    """A gvtest test backed by a single pytest test."""

    def __init__(
        self, runner: Any, parent: Any | None,
        name: str, target: Any | None,
        path: str | None, node_id: str
    ) -> None:
        super().__init__(runner, parent, name, target, path)
        self.node_id: str = node_id

    def enqueue(self) -> None:
        # PytestTestset handles enqueue for all its tests
        # as a batch. This should not be called directly.
        pass


class PytestTestset:
    """A testset that wraps a pytest test directory.

    Discovery happens at build time. Execution batches all
    selected tests into a single pytest invocation with
    JUnit XML output, then maps results back to individual
    gvtest tests.
    """

    def __init__(
        self, runner: Any, parent: Any | None,
        name: str, target: Any | None,
        path: str, pytest_path: str,
        pytest_exe: str = 'pytest',
        markers: str | None = None,
        pytest_args: list[str] | None = None,
        xdist: int = 0,
        batch_timeout: int | None = None,
        strict: bool = False,
        batch_exe: str | None = None
    ) -> None:
        self.runner: Any = runner
        self.parent: Any | None = parent
        self.name: str = name
        self.target: Any | None = target
        self.path: str = path
        self.pytest_path: str = pytest_path
        self.pytest_exe: str = pytest_exe
        # Executable for the run phase only (e.g. wrapped in
        # a lock); discovery keeps using pytest_exe
        self.batch_exe: str | None = batch_exe
        self.markers: str | None = markers
        self.pytest_args: list[str] = pytest_args or []
        self.xdist: int = xdist
        self.batch_timeout: int | None = batch_timeout
        self.strict: bool = strict
        self.tests: list[PytestTest] = []
        self.testsets: list[Any] = []
        # node ids whose result was already reported live
        # from the batch output (printed + terminated)
        self._finalized: set[str] = set()

    def get_full_name(self) -> str | None:
        if self.parent is not None:
            parent_name = self.parent.get_full_name()
            if parent_name is not None:
                return f'{parent_name}:{self.name}'
        return self.name

    def discover(self) -> None:
        """Discover pytest tests and create PytestTest
        entries."""
        node_ids, resolved_exe, returncode, stderr = \
            discover_pytest_tests(
                self.pytest_path, self.pytest_exe,
                self.markers, self.target
            )
        if self.strict and (returncode != 0 or not node_ids):
            raise RuntimeError(
                f"pytest collection failed in "
                f"{self.pytest_path} (exit {returncode}, "
                f"{len(node_ids)} tests collected):\n"
                f"{stderr}"
            )
        # Use the resolved executable (may be fallback)
        self.pytest_exe = resolved_exe
        for node_id in node_ids:
            # Convert node_id to a clean test name
            # e.g. "tests/test_foo.py::TestBar::test_baz"
            # → "test_foo:TestBar:test_baz"
            test_name = self._node_id_to_name(node_id)
            test = PytestTest(
                self.runner, self, test_name,
                self.target, self.path, node_id
            )
            if self.runner.is_selected(test):
                self.tests.append(test)

    def _node_id_to_name(self, node_id: str) -> str:
        """Convert pytest node ID to a gvtest test name.

        Example: ``tests/test_foo.py::TestBar::test_baz``
        → ``test_foo:TestBar:test_baz``
        """
        parts = node_id.split('::')
        file_part = parts[0]
        rest = parts[1:]

        # Get just the filename without .py
        basename = os.path.basename(file_part)
        basename = basename.replace('.py', '')

        components = [basename] + rest
        return ':'.join(components)

    def _effective_xdist(self) -> int:
        """Resolve the xdist worker count.

        -1 follows gvtest's --threads option (whose own
        0/auto default is one worker per CPU); 0 disables
        xdist; a positive value is used as-is.
        """
        if self.xdist >= 0:
            return self.xdist
        # Runner.start() resolves nb_threads=0 (auto) to the
        # CPU count before tests are enqueued
        nb_threads = getattr(self.runner, 'nb_threads', 0)
        if nb_threads > 0:
            return nb_threads
        return os.cpu_count() or 1

    def _has_visible_descendants(self) -> bool:
        """True if any test survives --target filtering."""
        for test in self.tests:
            if not test._is_filtered_by_cli_target():
                return True
        return False

    def dump_tests(
        self, rows: dict[str, dict], indent_level: int = 0
    ) -> None:
        if not self._has_visible_descendants():
            return
        key = self.get_full_name() or self.name
        entry = rows.get(key)
        if entry is None:
            entry = {
                'name': self.name,
                'full_name': self.get_full_name() or '',
                'indent_level': indent_level,
                'targets': [],
                'components': [],
                'description': '',
            }
            rows[key] = entry
        if self.target is not None and hasattr(self.target, 'name'):
            tname = self.target.name
            if tname and tname not in entry['targets']:
                entry['targets'].append(tname)
        for test in self.tests:
            test.dump_tests(rows, indent_level + 1)

    def enqueue(self) -> None:
        """Enqueue all pytest tests and run the batch."""
        if not self.tests:
            return

        # Collect active (non-skipped) tests
        active_tests: list[PytestTest] = []
        for test in self.tests:
            config = (
                test.target.name
                if test.target is not None
                else self.runner.get_config()
            )
            self.runner.count_test()
            if self.runner.tui is not None:
                self.runner.tui.count_target(config)

            if (self.runner.is_skipped(test.get_full_name())
                    or test.skipped is not None):
                # Handle skipped tests
                run = PytestTestRun(test, test.target)
                run.skip_message = (
                    test.skipped or
                    "Skipped from command line"
                )
                run.status = "skipped"
                test.runs.append(run)
                run.print_end_message()
            else:
                # Create the run and register as pending
                run = PytestTestRun(test, test.target)
                test.runs.append(run)
                self.runner.lock.acquire()
                self.runner.nb_pending_tests += 1
                self.runner.lock.release()
                active_tests.append(test)

        if not active_tests:
            return

        # Run the batch in a thread so we don't block
        # the main enqueue loop
        batch_thread = threading.Thread(
            target=self._run_batch,
            args=(active_tests,),
            daemon=True
        )
        batch_thread.start()

    def _run_batch(
        self, active_tests: list[PytestTest]
    ) -> None:
        """Run pytest once for all active tests, then
        distribute results."""
        if self.runner._interrupted:
            for test in active_tests:
                run = test.runs[-1]
                assert isinstance(run, PytestTestRun)
                run.set_result("failed", "", 0)
                run.print_end_message()
                self.runner.terminate(run)
            return

        # Collect node IDs
        node_ids = [t.node_id for t in active_tests]

        run0 = active_tests[0].runs[-1]
        assert isinstance(run0, PytestTestRun)
        container = run0.container

        # Create temp file for JUnit XML. With a container,
        # the file must live inside the workspace mount —
        # the host /tmp is not visible from the container.
        xml_fd, xml_path = tempfile.mkstemp(
            suffix='.xml', prefix='gvtest_pytest_',
            dir=self.path if container is not None else None
        )
        os.close(xml_fd)

        try:
            # Build pytest command
            # -o junit_logging=all captures print() output
            # in the JUnit XML system-out section
            args = [
                f'--junit-xml={xml_path}',
                f'--rootdir={self.path}',
                '-v', '--tb=short',
                '-o', 'junit_logging=all',
            ]
            if self.markers is not None:
                args += ['-m', self.markers]
            xdist = self._effective_xdist()
            if xdist > 0:
                args += ['-n', str(xdist)]
            args += self.pytest_args
            cmd = _build_pytest_cmd(
                self.batch_exe or self.pytest_exe,
                args + node_ids
            )

            # Build environment with target's sourceme
            # and envvars
            env = os.environ.copy()
            container_name: str | None = None

            if container is not None:
                # Containerized: sourceme is inlined and
                # envvars are passed with -e by the
                # container helper.
                from gvtest.container import (
                    build_container_shell_cmd,
                    kill_container,
                )
                argv, container_name = \
                    build_container_shell_cmd(
                        container, _shell_join(cmd),
                        self.path, run0.sourceme,
                        run0.envvars
                    )
            else:
                sourceme_prefix = ''
                if run0.sourceme:
                    sourceme_prefix = (
                        f'source {run0.sourceme} && '
                    )
                if run0.envvars:
                    env.update(run0.envvars)
                argv = [
                    'bash', '-c',
                    f'{sourceme_prefix}{_shell_join(cmd)}'
                ]

            # Print start messages
            for test in active_tests:
                run = test.runs[-1]
                assert isinstance(run, PytestTestRun)
                run.print_start()

            start_time = datetime.now()

            logger.debug(f"Running pytest batch: {argv}")

            timeout = self.batch_timeout
            if timeout is None:
                timeout = (
                    self.runner.max_timeout
                    if self.runner.max_timeout != -1
                    else None
                )

            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, errors='replace',
                env=env, cwd=self.path,
                start_new_session=True
            )

            timed_out = threading.Event()

            def _kill_batch() -> None:
                timed_out.set()
                if container_name is not None:
                    kill_container(
                        container['cmd'], container_name
                    )
                try:
                    os.killpg(
                        os.getpgid(proc.pid),
                        signal.SIGKILL
                    )
                except Exception:
                    pass

            timer: threading.Timer | None = None
            if timeout is not None:
                timer = threading.Timer(
                    timeout, _kill_batch
                )
                timer.start()

            # Stream pytest's verbose output: report each
            # test as soon as its PASSED/FAILED line shows
            # up instead of waiting for the whole batch.
            # The JUnit XML parsed afterwards remains the
            # authoritative source for status, output and
            # duration.
            node_to_run = {
                t.node_id: t.runs[-1] for t in active_tests
            }
            output_lines: list[str] = []
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    output_lines.append(line)
                    if self.runner.stdout:
                        print(line, end='')
                    match = _LIVE_RESULT_RE.search(line)
                    if match is None:
                        continue
                    status, node_id = (
                        _live_match_to_result(match)
                    )
                    run = node_to_run.get(node_id)
                    if (run is None or
                            node_id in self._finalized):
                        continue
                    assert isinstance(run, PytestTestRun)
                    # Report live but do NOT terminate yet:
                    # terminating the last pending test lets
                    # the runner exit while this thread is
                    # still parsing the JUnit XML for the
                    # output and duration
                    run.set_result(status, '', 0)
                    run.print_end_message()
                    self._finalized.add(node_id)
                proc.wait()
            finally:
                if timer is not None:
                    timer.cancel()

            batch_output = ''.join(output_lines)

            if timed_out.is_set():
                batch_output += '\n--- Timeout reached ---\n'
                for test in active_tests:
                    if test.node_id in self._finalized:
                        continue
                    run = test.runs[-1]
                    assert isinstance(run, PytestTestRun)
                    run.set_result(
                        "failed", batch_output, 0
                    )
                return  # the finally block finalizes

            total_duration = (
                datetime.now() - start_time
            ).total_seconds()

            # Parse JUnit XML results
            self._parse_results(
                xml_path, active_tests, batch_output,
                total_duration
            )

        finally:
            # Clean up XML file
            try:
                os.unlink(xml_path)
            except OSError:
                pass

            # Print results and finalize. Tests that got a
            # live result line were already announced; all
            # tests are terminated here, after the XML parse,
            # so the runner cannot exit before outputs and
            # durations are filled in.
            for test in active_tests:
                run = test.runs[-1]
                assert isinstance(run, PytestTestRun)
                if run.output and (
                    self.runner.stdout
                    or self.runner.safe_stdout
                ):
                    print(run.output)
                if test.node_id not in self._finalized:
                    run.print_end_message()
                self.runner.terminate(run)

    def _parse_results(
        self, xml_path: str,
        active_tests: list[PytestTest],
        batch_output: str, total_duration: float
    ) -> None:
        """Parse JUnit XML and set results on each test."""
        # Build a map from node_id to test
        node_to_test: dict[str, PytestTest] = {
            t.node_id: t for t in active_tests
        }

        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except Exception as e:
            logger.error(f"Failed to parse JUnit XML: {e}")
            # All tests without a live result failed
            for test in active_tests:
                if test.node_id in self._finalized:
                    continue
                run = test.runs[-1]
                assert isinstance(run, PytestTestRun)
                run.set_result(
                    "failed",
                    f"JUnit XML parse error: {e}\n"
                    f"{batch_output}",
                    0
                )
            return

        # Track which tests we've seen results for
        seen: set[str] = set()

        for testcase in root.iter('testcase'):
            classname = testcase.get('classname', '')
            name = testcase.get('name', '')
            time_str = testcase.get('time', '0')
            duration = float(time_str)

            # Reconstruct node_id from classname and name
            # JUnit format: classname="test_file.TestClass"
            #               name="test_method"
            node_id = self._junit_to_node_id(
                classname, name
            )

            test = node_to_test.get(node_id)
            if test is None:
                # Try fuzzy match by test name
                for nid, t in node_to_test.items():
                    if nid.endswith(
                        f'::{name}'
                    ) or nid.endswith(
                        f'::{classname.split(".")[-1]}'
                        f'::{name}'
                    ):
                        test = t
                        break

            if test is None:
                logger.debug(
                    f"No match for JUnit test: "
                    f"{classname}::{name}"
                )
                continue

            seen.add(test.node_id)

            # Determine status
            failure = testcase.find('failure')
            error = testcase.find('error')
            skipped_el = testcase.find('skipped')

            output_parts: list[str] = []
            system_out = testcase.find('system-out')
            system_err = testcase.find('system-err')
            if system_out is not None and system_out.text:
                output_parts.append(system_out.text)
            if system_err is not None and system_err.text:
                output_parts.append(system_err.text)

            if failure is not None:
                status = "failed"
                if failure.text:
                    output_parts.append(failure.text)
            elif error is not None:
                status = "failed"
                if error.text:
                    output_parts.append(error.text)
            elif skipped_el is not None:
                status = "skipped"
                msg = skipped_el.get('message', '')
                if msg:
                    output_parts.append(
                        f"Skipped: {msg}"
                    )
            else:
                status = "passed"

            output = '\n'.join(output_parts)

            run = test.runs[-1]
            assert isinstance(run, PytestTestRun)
            run.set_result(status, output, duration)

        # Any tests not in XML results → failed
        for test in active_tests:
            if test.node_id not in seen:
                run = test.runs[-1]
                assert isinstance(run, PytestTestRun)
                if not run._result_set.is_set():
                    run.set_result(
                        "failed",
                        "Test not found in pytest results\n"
                        + batch_output,
                        0
                    )

    def _junit_to_node_id(
        self, classname: str, name: str
    ) -> str:
        """Convert JUnit classname + name back to a pytest
        node ID.

        JUnit: classname="tests.test_foo.TestBar"
               name="test_baz"
        Node:  tests/test_foo.py::TestBar::test_baz

        JUnit: classname="tests.test_foo"
               name="test_baz"
        Node:  tests/test_foo.py::test_baz
        """
        parts = classname.split('.')
        # Find where the module ends and class begins
        # Module parts contain only lowercase/underscores
        # Class parts start with uppercase
        module_parts: list[str] = []
        class_parts: list[str] = []
        for part in parts:
            if not class_parts and (
                part[0:1].islower() or part.startswith('test')
            ):
                module_parts.append(part)
            else:
                class_parts.append(part)

        file_path = '/'.join(module_parts) + '.py'
        if class_parts:
            return (
                f'{file_path}::'
                f'{"::".join(class_parts)}::{name}'
            )
        return f'{file_path}::{name}'

    def get_stats(self, stats: Any) -> None:
        """Aggregate stats from all tests."""
        for test in self.tests:
            for run in test.runs:
                run.get_stats(stats.stats)
