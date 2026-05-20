#!/usr/bin/env python3

#
# Copyright (C) 2023 ETH Zurich, University of Bologna and GreenWaves Technologies
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
Test runner — orchestration, worker threads, and CLI-facing API.

This module was split from a monolithic runner.py. The other pieces now live in:
  - targets.py      — Target class
  - tests.py        — TestRun, TestCommon, TestImpl, and specialized test types
  - stats.py        — TestRunStats, TestStats, TestsetStats
  - reporting.py    — bcolors, table_dump_row
  - testset_impl.py — TestsetImpl
  - config.py       — Hierarchical gvtest.yaml config loader
"""

from __future__ import annotations

import os
import logging
import signal
import subprocess
import sys
import queue
import threading
import time
import importlib
import importlib.util
from collections import deque
from dataclasses import dataclass, field
from importlib.machinery import SourceFileLoader
from types import FrameType
from typing import Any

import psutil
import rich.table
from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.progress import Progress, BarColumn, TextColumn, TaskProgressColumn
from rich.align import Align

from pathlib import Path
from gvtest.config import get_python_paths_for_dir, ConfigLoader
from gvtest.targets import Target
from gvtest.stats import TestsetStats
from gvtest.testset_impl import TestsetImpl

# Re-export classes that external code (tests, __main__) may import from runner
from gvtest.targets import Target
from gvtest.tests import (
    TestRun, TestCommon, TestImpl, MakeTestImpl,
    GvrunTestImpl, SdkTestImpl, NetlistPowerSdkTestImpl,
)
from gvtest.stats import TestRunStats, TestStats, TestsetStats
from gvtest.reporting import table_dump_row


@dataclass
class _Resource:
    """Per-resource scheduling state.

    A resource is a named slot with bounded `capacity`; tests whose
    commands list the resource via `Command.resources` count against
    `in_use` while they hold it. When `in_use == capacity`, further
    candidates are parked on `waiters` instead of being dispatched
    to a worker. Releases pop a waiter and dispatch it directly.
    """
    capacity: int
    in_use: int = 0
    waiters: 'deque[TestRun]' = field(default_factory=deque)


class _DispatchQueue:
    """Worker-facing dispatch queue with priority front-insertion.

    Behaves like a `queue.Queue` (`put`/`get`/`get_nowait`/`empty`/
    `qsize`) but `put(item, front=True)` inserts at the head so the
    next worker pickup is that item. Used to hand off a freshly
    promoted waiter ahead of the regular FIFO of dispatched tests.
    """

    def __init__(self) -> None:
        self._deque: 'deque[TestRun | None]' = deque()
        self._cond: threading.Condition = threading.Condition()

    def put(self, item: TestRun | None, front: bool = False) -> None:
        with self._cond:
            if front:
                self._deque.appendleft(item)
            else:
                self._deque.append(item)
            self._cond.notify()

    def get(self) -> TestRun | None:
        with self._cond:
            while not self._deque:
                self._cond.wait()
            return self._deque.popleft()

    def get_nowait(self) -> TestRun | None:
        with self._cond:
            if not self._deque:
                raise queue.Empty
            return self._deque.popleft()

    def empty(self) -> bool:
        with self._cond:
            return not self._deque

    def qsize(self) -> int:
        with self._cond:
            return len(self._deque)


class Worker(threading.Thread):

    def __init__(self, runner: Runner) -> None:
        super().__init__(daemon=True)

        self.runner: Runner = runner

    def run(self) -> None:
        while True:
            test: TestRun | None = self.runner.pop_test()
            if test is None:
                return
            test.run()


class Runner():

    def __init__(
            self, config: str = 'default',
            load_average: float = 0.9,
            nb_threads: int = 0,
            properties: list[str] | None = None,
            stdout: bool = False,
            safe_stdout: bool = False,
            max_output_len: int = -1,
            max_timeout: int = -1,
            test_list: list[str] | None = None,
            test_skip_list: list[str] | None = None,
            commands: list[str] | None = None,
            commands_exclude: list[str] | None = None,
            flags: list[str] | None = None,
            bench_db: str | None = None,
            bench_regexp: str | None = None,
            targets: list[str] | None = None,
            platform: str = 'gvsoc',
            report_all: bool = False,
            progress: bool = False,
            tolerate_missing: bool = False,
            resources: list[str] | None = None
    ) -> None:
        self.nb_threads: int = nb_threads
        self.queue: _DispatchQueue = _DispatchQueue()
        self.testsets: list[TestsetImpl] = []
        self.pending_tests: list[TestRun] = []
        self.active_runs: list[TestRun] = []
        self.max_testname_len: int = 0
        self.config: str = config
        self.event: threading.Event = threading.Event()
        self.lock: threading.Lock = threading.Lock()
        self.load_average: float = load_average
        self.stdout: bool = stdout
        self.safe_stdout: bool = safe_stdout
        self.nb_pending_tests: int = 0
        self.test_skip_list: list[str] | None = test_skip_list
        self.max_timeout: int = max_timeout
        self.max_output_len: int = max_output_len
        self.commands_filter: list[str] | None = commands
        self.commands_exclude: list[str] | None = commands_exclude
        self.flags: list[str] = flags if flags is not None else []
        self.bench_results: list[dict[str, Any]] = []
        self.bench_db: str | None = bench_db
        self.properties: dict[str, str] = {}
        self.test_list: list[str] | None = test_list
        self.target_names: list[str] = targets if targets is not None else ['default']
        self._cli_targets_explicit: bool = targets is not None
        self.platform: str | None = platform
        # Fallback target for tests that aren't attached to any
        # gvtest.yaml-declared target. Always named 'default' so
        # they report under a neutral label; using the first
        # --target would falsely label them as belonging to it.
        self.default_target: Target = Target('default')
        self.default_target._is_fallback = True
        # Track sub-testset files that have already been
        # fanned out to their own targets, to prevent
        # duplication when multiple parent targets import
        # the same sub-testset.
        self._fanned_out: set[str] = set()
        self.cpu_poll_interval: float = 0.1
        self.report_all: bool = report_all
        self.progress: bool = progress
        self.tolerate_missing: bool = tolerate_missing
        self.live_display: Any = None
        self.tui: Any = None
        self.stats: TestsetStats = TestsetStats()
        self.nb_total_tests: int = 0
        self._module_cache: dict[str, Any] = {}
        if properties is not None:
            for prop in properties:
              name, value = prop.split('=')
              self.properties[name] = value

        # Resource registry. Each named resource carries
        # capacity, current in_use count, and a FIFO of
        # parked TestRuns. The dispatcher claims a resource
        # at dispatch time (incrementing in_use) and parks
        # the candidate on the resource's waiters deque
        # when the resource is full. Releases pop a waiter
        # and dispatch it directly to the worker queue.
        # Lazy creation: the first acquire of an undeclared
        # resource creates a capacity-1 entry on the fly.
        self._resources: dict[str, _Resource] = {}
        self._resources_cond: threading.Condition = (
            threading.Condition(threading.Lock())
        )
        if resources is not None:
            for spec in resources:
                if ':' in spec:
                    name, _, cap_str = spec.partition(':')
                    try:
                        capacity = int(cap_str)
                    except ValueError:
                        raise ValueError(
                            f"Invalid --resource capacity: {spec!r}"
                        )
                else:
                    name, capacity = spec, 1
                self.declare_resource(name, capacity)



    def get_active_targets(self) -> list[str]:
        return self.target_names

    def get_platform(self) -> str | None:
        return self.platform

    def get_property(self, name: str) -> str | None:
        return self.properties.get(name)

    def is_selected(self, test: TestCommon) -> bool:
        if self.test_list is None:
            return True

        for selected_test in self.test_list:
            full_name = test.get_full_name()
            if full_name is not None and full_name.find(selected_test) == 0:
                return True

        return False

    def is_skipped(self, name: str) -> bool:
        if self.test_skip_list is not None:
            for skip in self.test_skip_list:
                if name.find(skip) == 0:
                    return True

        return False

    def tests(self) -> None:
        from collections import OrderedDict
        rows: OrderedDict[str, dict] = OrderedDict()
        for testset in self.testsets:
            testset.dump_tests(rows)

        table = rich.table.Table(title=f'tests', title_justify="left")
        table.add_column('Name')
        table.add_column('Path')
        table.add_column('Targets')
        table.add_column('Components')
        table.add_column('Description')

        for entry in rows.values():
            table.add_row(
                '  ' * entry['indent_level'] + entry['name'],
                entry['full_name'],
                ', '.join(entry['targets']),
                ', '.join(entry['components']),
                entry['description'],
            )

        print()
        rich.print(table)

    def summary(self) -> None:
        failed: int | float = self.stats.stats['failed']
        passed: int | float = self.stats.stats['passed']
        skipped: int | float = self.stats.stats['skipped']
        excluded: int | float = self.stats.stats['excluded']
        total: int | float = failed + passed

        console = Console()
        table = Table(show_header=False)
        table.add_column("Status", justify="center")
        table.add_column("Count", justify="right")
        table.add_row("Test Summary", "", style="bold", end_section=True)
        table.add_row("Total", str(total))
        table.add_row("Passed", str(passed))
        table.add_row("Failed", str(failed))
        table.add_row("Skipped", str(skipped))
        table.add_row("Excluded", str(excluded))

        console.print(table)

        success_ratio: float = passed / total if total > 0 else 0
        percent: int = int(success_ratio * 100)

        if passed == total:
            msg: str = "[bold green]All tests passed[/bold green]"
        else:
            msg = f"[bold red]{passed}/{total} tests passed ({percent}%).[/bold red]"

        final_bar = Progress(
            BarColumn(bar_width=len(msg))
        )

        task = final_bar.add_task("", total=100, completed=percent)

        if passed == total:
            content: Any = msg
        else:
            content = Group(
                Align.center(msg, vertical="middle"),
                Align.center(final_bar, vertical="middle")
            )

        console.print(Panel.fit(
            content,
            border_style="green" if passed == total else "red",
            padding=(1, 2)
        ))

    def run(self) -> None:
        self.event.clear()
        self.nb_total_tests = 0

        # Start live display before enqueue so it catches
        # skipped/excluded tests too
        if self.progress and self.tui is None:
            from gvtest.live_display import LiveDisplay
            from rich.console import Console
            self.live_display = LiveDisplay(
                Console(highlight=False, stderr=True)
            )
            # Start with 0, update total after enqueue
            self.live_display.start(0)

        for testset in self.testsets:
            testset.enqueue()

        # Update totals now that all tests are counted
        if self.live_display is not None:
            self.live_display.set_total(
                self.nb_total_tests
            )

        # Notify TUI of total test count
        if self.tui is not None:
            self.tui.set_total(self.nb_total_tests)

        if len(self.pending_tests) > 0:
            self.check_pending_tests()

        # Wait if there are still tests running
        # (includes both regular and pytest batch tests)
        self.lock.acquire()
        should_wait: bool = self.nb_pending_tests > 0
        self.lock.release()
        if should_wait:
            # Use a timeout loop so SIGINT can be delivered
            # (event.wait() without timeout can block signal
            # handling on some platforms)
            while not self.event.is_set():
                self.event.wait(timeout=0.5)

        # Stop live display
        if self.live_display is not None:
            self.live_display.stop()
            self.live_display = None

        self.stats: TestsetStats = TestsetStats()
        for testset in self.testsets:
            self.stats.add_child_testset(testset)

        if self.bench_db is not None and self.bench_results:
            from datetime import datetime as _dt, timezone as _tz
            report = {
                'timestamp': _dt.now(_tz.utc).isoformat(),
                'git_commit': self._get_git_info('rev-parse', 'HEAD'),
                'git_branch': self._get_git_info('rev-parse', '--abbrev-ref', 'HEAD'),
                'platform': self.platform or 'gvsoc',
                'results': self.bench_results,
            }
            self._write_bench_db(report)



    def declare_name(self, name: str) -> None:
        name_len: int = len(name)
        if self.max_testname_len < name_len:
            self.max_testname_len = name_len


    def dump_table(self) -> None:
        console = Console()
        table = Table(show_header=True, header_style="bold")
        table.add_column("test", justify="left", no_wrap=True)
        table.add_column("config", justify="left", no_wrap=True)
        table.add_column("time", justify="right")
        table.add_column("passed/total", justify="right")
        table.add_column("failed", justify="right")
        table.add_column("skipped", justify="right")
        table.add_column("excluded", justify="right")
        self.stats.dump_table(table, self.report_all)
        print()
        console.print(table)


    def dump_junit(self, report_path: str) -> None:
        os.makedirs(report_path, exist_ok=True)

        self.stats.dump_junit_files(report_path)



    def get_config(self) -> str:
        return self.config

    def pop_test(self) -> TestRun | None:
        return self.queue.get()

    def start(self) -> None:
        if self.nb_threads == 0:
            self.nb_threads = psutil.cpu_count(logical=True) or 1

        self._interrupted: bool = False
        import threading as _threading
        if _threading.current_thread() is _threading.main_thread():
            self._orig_sigint: Any = signal.getsignal(
                signal.SIGINT
            )
            signal.signal(
                signal.SIGINT, self._handle_interrupt
            )
        else:
            self._orig_sigint = signal.SIG_DFL

        for thread_id in range(0, self.nb_threads):
            Worker(self).start()

    def _handle_interrupt(
        self, signum: int, frame: FrameType | None
    ) -> None:
        """Graceful Ctrl+C: stop everything."""
        if self._interrupted:
            # Second Ctrl+C: force exit
            signal.signal(signal.SIGINT, self._orig_sigint)
            raise KeyboardInterrupt
        self._interrupted = True
        print('\n--- Interrupted, killing running tests ---')
        sys.stdout.flush()

        self.lock.acquire()
        # Clear pending tests
        dropped: int = len(self.pending_tests)
        self.pending_tests.clear()
        self.nb_pending_tests -= dropped

        # Drain tests parked on resource waiters
        with self._resources_cond:
            for res in self._resources.values():
                n = len(res.waiters)
                if n:
                    res.waiters.clear()
                    self.nb_pending_tests -= n

        # Drain the queue so workers don't pick up more
        while not self.queue.empty():
            try:
                item = self.queue.get_nowait()
                if item is not None:
                    self.nb_pending_tests -= 1
            except Exception:
                break

        # Kill all currently running test processes
        for run in list(self.active_runs):
            run.kill()

        if self.nb_pending_tests <= 0:
            self.nb_pending_tests = 0
            self.event.set()
        self.lock.release()

    def stop(self) -> None:
        for thread_id in range(0, self.nb_threads):
            self.queue.put(None)
        # Restore original signal handler
        import threading as _threading
        if (hasattr(self, '_orig_sigint')
                and self._orig_sigint is not None
                and _threading.current_thread()
                is _threading.main_thread()):
            signal.signal(signal.SIGINT, self._orig_sigint)
            self._orig_sigint = None

    @property
    def _cli_targets_specified(self) -> bool:
        """True when the user explicitly passed --target."""
        return self._cli_targets_explicit

    def add_testset(self, file: str) -> None:
        if not os.path.isabs(file):
            file = os.path.join(os.getcwd(), file)

        # Resolve targets for the root testset's directory
        testset_dir = os.path.dirname(file)
        targets = self._resolve_targets_for_dir(testset_dir)

        if targets:
            for target in targets:
                self.testsets.append(
                    self.import_testset(file, target, None)
                )
        else:
            # No YAML targets at this level — load with
            # default target. The testset may import
            # sub-testsets that DO define targets.
            # Filtering of untargeted tests happens at
            # enqueue time (see TestCommon.enqueue).
            self.testsets.append(
                self.import_testset(
                    file, self.default_target, None
                )
            )

    def _has_own_targets(self, directory: str) -> bool:
        """Check if directory has its own gvtest.yaml with
        a targets section (not inherited from parent)."""
        config_file = os.path.join(
            directory, 'gvtest.yaml'
        )
        if not os.path.exists(config_file):
            return False
        try:
            loader = ConfigLoader(directory)
            config = loader.load_config(Path(config_file))
            return 'targets' in config
        except Exception:
            return False

    def _resolve_targets_for_dir(
        self, directory: str
    ) -> list[Target]:
        """Resolve targets for a specific directory from
        gvtest.yaml hierarchy. Returns list of Target objects
        applicable to this directory, filtered by CLI --target
        if specified."""
        loader = ConfigLoader(directory)
        loader.config_files = loader.discover_configs()
        yaml_targets = loader.resolve_targets(
            loader.config_files
        )

        if not yaml_targets:
            return []

        # Build Target objects
        config_dir: str | None = getattr(
            loader, '_targets_config_dir', None
        )
        targets: list[Target] = []
        # Filter by --target names, but only filter out
        # YAML targets that aren't requested. 'default'
        # in target_names means "untargeted tests" and
        # doesn't affect YAML target resolution.
        cli_real_targets = (
            [n for n in self.target_names if n != 'default']
            if self._cli_targets_specified
            else []
        )
        for name, cfg in yaml_targets.items():
            if (cli_real_targets
                    and name not in cli_real_targets):
                continue
            t = Target.from_dict(name, cfg)
            t.config_dir = config_dir
            targets.append(t)

        return targets


    def import_testset(
        self, file: str, target: Target,
        parent: TestsetImpl | None = None
    ) -> TestsetImpl:
        logging.debug(f"Parsing file (path: {file})")

        # Get the directory of the testset file
        testset_dir: str = os.path.dirname(file)
        
        # Discover and load gvtest.yaml configs for this testset's directory hierarchy
        # This will find all gvtest.yaml files from testset_dir up to filesystem root
        python_paths: list[str] = get_python_paths_for_dir(testset_dir)
        
        # Save the current sys.path to restore it later
        # This ensures complete isolation between testsets
        saved_sys_path: list[str] = sys.path.copy()
        
        try:
            # Add the discovered paths to sys.path
            # This allows the testset to import from configured paths during loading
            for path in python_paths:
                if path not in sys.path:
                    sys.path.insert(0, path)
                    logging.debug(f"Added to sys.path for testset: {path}")
            
            # Cache modules: import once, call testset_build per target.
            # This preserves global state across targets.
            module_name: str = f"gvtest_testset_{hash(file)}"
            if module_name in self._module_cache:
                module = self._module_cache[module_name]
            else:
                spec = importlib.util.spec_from_loader(module_name, SourceFileLoader(module_name, file))
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self._module_cache[module_name] = module

            # testset_build() must run while python_paths are still in sys.path,
            # since it may import modules from configured paths
            testset: TestsetImpl = TestsetImpl(self, target, parent, path=os.path.dirname(file))
            module.testset_build(testset)
        except FileNotFoundError as exc:
            if self.tolerate_missing:
                logging.warning(
                    f"skipping missing testset file: {file}"
                )
                # Return an empty placeholder so parents don't crash on
                # later attribute access; it carries no tests.
                return TestsetImpl(self, target, parent,
                                   path=os.path.dirname(file))
            raise RuntimeError('Unable to open test configuration file: ' + file)
        except Exception as exc:
            if self.tolerate_missing:
                logging.warning(
                    f"skipping testset {file}: {type(exc).__name__}: {exc}"
                )
                return TestsetImpl(self, target, parent,
                                   path=os.path.dirname(file))
            raise
        finally:
            # Restore original sys.path to maintain isolation between testsets
            # Imported modules remain available via sys.modules cache
            sys.path = saved_sys_path
            logging.debug(f"Restored sys.path after loading testset")

        return testset


    def count_test(self) -> None:
        """Increment total test count (incl. skipped)."""
        self.nb_total_tests += 1

    def enqueue_test(self, test: TestRun) -> None:
        self.lock.acquire()
        self.nb_pending_tests += 1
        self.pending_tests.append(test)
        self.lock.release()



    def check_pending_tests(self) -> None:
        while True:
            self.lock.acquire()
            if len(self.pending_tests) == 0:
                self.lock.release()
                break

            if self._interrupted:
                # Drop all remaining pending tests AND any
                # tests parked on resource wait lists, so
                # nb_pending_tests can reach zero.
                dropped: int = len(self.pending_tests)
                self.pending_tests.clear()
                with self._resources_cond:
                    for res in self._resources.values():
                        dropped += len(res.waiters)
                        res.waiters.clear()
                self.nb_pending_tests -= dropped
                if self.nb_pending_tests <= 0:
                    self.nb_pending_tests = 0
                    self.event.set()
                self.lock.release()
                break

            # Two-pass backward scan over pending_tests.
            #
            # Pass 1 looks only at resource-using candidates so
            # the (serial) build phase of build_resource tests
            # ramps up at the start of the run, overlapping with
            # the (parallel) phase of non-resource tests. Pass 2
            # picks any remaining dispatchable test.
            #
            # In each pass: skip tests with unmet deps; try to
            # claim resources atomically — on success dispatch,
            # on failure park on the first full resource and
            # keep scanning.
            test: TestRun | None = self._scan_and_pick(
                prefer_resource=True
            )
            if test is None:
                test = self._scan_and_pick(
                    prefer_resource=False
                )

            if test is None:
                # All pending tests are blocked (parked on
                # waiters lists or unmet deps). Wait for a
                # running test to finish or release a resource.
                self.lock.release()
                time.sleep(0.1)
                continue

            self.lock.release()

            while not self.check_cpu_load():
                time.sleep(self.cpu_poll_interval)

            self.queue.put(test)


    def check_cpu_load(self) -> bool:
        if self.load_average == 1.0:
            return True

        load: float = psutil.cpu_percent(interval=self.cpu_poll_interval)

        return load < self.load_average * 100


    def get_max_testname_len(self) -> int:
        return self.max_testname_len


    def register_active(self, test: TestRun) -> None:
        self.lock.acquire()
        self.active_runs.append(test)
        self.lock.release()

    def unregister_active(self, test: TestRun) -> None:
        self.lock.acquire()
        if test in self.active_runs:
            self.active_runs.remove(test)
        self.lock.release()

    def terminate(self, test: TestRun) -> None:
        self.unregister_active(test)
        self.lock.acquire()
        self.nb_pending_tests -= 1

        if self.nb_pending_tests == 0:
            self.event.set()

        self.lock.release()

    def declare_resource(
        self, name: str, capacity: int = 1
    ) -> None:
        """Declare a named resource with a given capacity.

        Re-declaring with the same capacity is a no-op. Re-declaring
        with a different capacity raises, since capacities are fixed
        once any test has acquired the resource.
        """
        if capacity < 1:
            raise ValueError(
                f"Resource {name!r} capacity must be >= 1, "
                f"got {capacity}"
            )
        with self._resources_cond:
            existing = self._resources.get(name)
            if existing is not None:
                if existing.capacity != capacity:
                    raise ValueError(
                        f"Resource {name!r} already declared with "
                        f"capacity {existing.capacity}, cannot "
                        f"redeclare with capacity {capacity}"
                    )
                return
            self._resources[name] = _Resource(capacity=capacity)

    @staticmethod
    def _needed_resources(test: TestRun) -> set[str]:
        """Union of resource names listed by any of the test's
        commands."""
        needed: set[str] = set()
        for cmd in test.test.commands:
            cmd_res = getattr(cmd, 'resources', None)
            if cmd_res:
                needed.update(cmd_res)
        return needed

    def _get_or_create_resource(self, name: str) -> _Resource:
        """Caller must hold _resources_cond. Lazy-creates a
        capacity-1 entry if needed."""
        res = self._resources.get(name)
        if res is None:
            res = _Resource(capacity=1)
            self._resources[name] = res
        return res

    def _try_claim(self, test: TestRun) -> str | None:
        """Caller must hold _resources_cond.

        Try to atomically claim every resource the test needs. On
        success, increments in_use for each and stamps
        test.pre_claimed_resources, returning None. On failure,
        returns the first resource name that was full (so the caller
        can park the test on it)."""
        needed = self._needed_resources(test)
        if not needed:
            test.pre_claimed_resources = set()
            return None
        for name in needed:
            res = self._get_or_create_resource(name)
            if res.in_use >= res.capacity:
                return name
        for name in needed:
            self._resources[name].in_use += 1
        test.pre_claimed_resources = set(needed)
        return None

    def _scan_and_pick(
        self, prefer_resource: bool
    ) -> TestRun | None:
        """Walk pending_tests backward (LIFO) and try to dispatch
        one test.

        When `prefer_resource` is True, only resource-using
        candidates are considered (the others are left in place for
        the second pass). For each candidate with satisfied deps,
        attempt to claim its resources. On success the test is
        popped and returned. On failure the test is popped and
        appended to the first full resource's wait list, and the
        scan continues. Returns None when nothing was dispatched.
        Caller must hold self.lock.
        """
        i = len(self.pending_tests) - 1
        while i >= 0:
            candidate = self.pending_tests[i]
            if not candidate.test.deps_satisfied():
                i -= 1
                continue
            if prefer_resource and not self._needed_resources(
                candidate
            ):
                i -= 1
                continue
            with self._resources_cond:
                blocker = self._try_claim(candidate)
                if blocker is None:
                    return self.pending_tests.pop(i)
                self._resources[blocker].waiters.append(
                    candidate
                )
                self.pending_tests.pop(i)
            # The list shrank by one; restart from the new tail
            # since indices below i are unchanged but i itself
            # may now point past the end.
            i = len(self.pending_tests) - 1
        return None

    def acquire_resource(
        self, name: str, test_run: TestRun | None = None
    ) -> None:
        """Claim a named resource for the caller.

        Fast path: if `test_run` is given and `name` is in
        `test_run.pre_claimed_resources`, the dispatcher already
        accounted for this claim — return immediately and remove the
        name from the pre-claim set so subsequent releases match
        exactly one claim.

        Slow path (legacy / non-monotonic patterns): block until
        `in_use < capacity`, then increment. Lazily creates a
        capacity-1 resource if undeclared.
        """
        if (test_run is not None
                and name in test_run.pre_claimed_resources):
            test_run.pre_claimed_resources.discard(name)
            return
        with self._resources_cond:
            res = self._get_or_create_resource(name)
            while res.in_use >= res.capacity:
                self._resources_cond.wait()
            res.in_use += 1

    def release_resource(self, name: str) -> None:
        """Release a previously-claimed named resource.

        Decrements in_use and tries to promote a waiter: scans the
        resource's waiters deque, picks the first one whose entire
        resource set can now be claimed, claims it, and dispatches
        it directly to the worker queue. Waiters that still can't
        be fully claimed are re-parked on whichever resource of
        theirs is still full.
        """
        promoted: TestRun | None = None
        with self._resources_cond:
            res = self._resources.get(name)
            if res is None:
                # Should not happen in well-formed code; be
                # defensive.
                return
            res.in_use -= 1

            # Try to promote one waiter. Limit iterations to the
            # current waiter count to bound work in the (unusual)
            # case where every waiter has to be re-parked.
            attempts = len(res.waiters)
            while promoted is None and attempts > 0 and res.waiters:
                attempts -= 1
                waiter = res.waiters.popleft()
                blocker = self._try_claim(waiter)
                if blocker is None:
                    promoted = waiter
                else:
                    # waiter still can't run — re-park on the
                    # blocker (which is by definition not `name`,
                    # since `name` just freed a slot).
                    self._resources[blocker].waiters.append(waiter)

            self._resources_cond.notify_all()

        if promoted is not None:
            # Front of the dispatch queue so the next idle worker
            # picks the promoted test up immediately, ahead of any
            # already-queued non-resource tests.
            self.queue.put(promoted, front=True)

    def register_bench_result(
        self, name: str, value: float, desc: str,
        test_name: str = '', target: str = ''
    ) -> None:
        self.bench_results.append({
            'name': name,
            'value': value,
            'desc': desc,
            'test': test_name,
            'target': target,
        })

    def _get_git_info(self, *args: str) -> str | None:
        try:
            return subprocess.check_output(
                ['git'] + list(args),
                stderr=subprocess.DEVNULL
            ).decode().strip()
        except Exception:
            return None

    def _write_bench_db(self, report: dict[str, Any]) -> None:
        from gvtest.bench.db import init_db
        conn = init_db(self.bench_db)
        cursor = conn.execute(
            "INSERT INTO runs (timestamp, git_commit, git_branch, platform) "
            "VALUES (?, ?, ?, ?)",
            (
                report['timestamp'],
                report.get('git_commit'),
                report.get('git_branch'),
                report.get('platform', 'unknown'),
            )
        )
        run_id = cursor.lastrowid
        for r in report.get('results', []):
            conn.execute(
                "INSERT OR IGNORE INTO results "
                "(run_id, test, target, metric, value, description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, r.get('test', ''), r.get('target', ''),
                 r.get('name', ''), r.get('value', 0), r.get('desc', ''))
            )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM results WHERE run_id = ?",
            (run_id,)
        ).fetchone()[0]
        logging.info(f"Bench: inserted {count} result(s) into {self.bench_db}")
        conn.close()
