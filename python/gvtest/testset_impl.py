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
TestsetImpl — concrete implementation of the Testset abstract class.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from rich.table import Table

import gvtest.testsuite as testsuite
from gvtest.targets import Target
from gvtest.pytest_integration import PytestTestset
from gvtest.tests import (
    TestCommon, TestImpl, MakeTestImpl, GvrunTestImpl,
    SdkTestImpl, NetlistPowerSdkTestImpl,
)


class TestsetImpl(testsuite.Testset):

    def __init__(
        self, runner: Any, target: Any | None,
        parent: TestsetImpl | None = None,
        path: str | None = None
    ) -> None:
        self.runner: Any = runner
        self.name: str | None = None
        self.tests: list[TestCommon] = []
        self.testsets: list[TestsetImpl] = []
        self.parent: TestsetImpl | None = parent
        self.path: str | None = path
        self.target: Any | None = target
        self.components: list[str] | None = None

    def get_target(self) -> Any | None:
        return self.target

    def get_path(self) -> str | None:
        return self.path

    def get_property(self, name: str) -> Any | None:
        return self.runner.get_property(name)

    def get_platform(self) -> str | None:
        return self.runner.get_platform()

    def set_name(self, name: str) -> None:
        self.name = name

    def set_components(self, components: list[str]) -> None:
        """Tag all tests in this testset with the given components.

        Components are identified by their dotted vp_model name
        (e.g. "interco.router"). Inherited by child tests and
        nested testsets unless overridden.
        """
        self.components = list(components)

    def get_components(self) -> list[str]:
        """Return declared components, falling back to parent testset."""
        if self.components is not None:
            return self.components
        if self.parent is not None:
            getter = getattr(self.parent, 'get_components', None)
            if getter is not None:
                return getter()
        return []

    def get_full_name(self) -> str | None:
        if self.parent is not None:
            parent_name: str | None = self.parent.get_full_name()
            if parent_name is not None:
                if self.name is None:
                    return parent_name
                else:
                    return f'{parent_name}:{self.name}'

        return self.name

    def import_testset(self, file: str) -> None:
        filepath: str = file
        if self.path is not None:
            filepath = os.path.join(self.path, file)

        sub_dir = os.path.dirname(filepath)

        if self.runner._has_own_targets(sub_dir):
            # Sub-dir defines its own targets — it owns
            # its target scope independently of the parent.
            # Use _fanned_out to ensure we only fan out
            # once per sub-testset file (avoids duplication
            # when multiple parent targets import the same
            # sub-testset).
            real_path = os.path.realpath(filepath)
            if real_path not in self.runner._fanned_out:
                self.runner._fanned_out.add(real_path)
                sub_targets = (
                    self.runner._resolve_targets_for_dir(
                        sub_dir
                    )
                )
                for target in sub_targets:
                    self.testsets.append(
                        self.runner.import_testset(
                            filepath, target, self
                        )
                    )
        else:
            # Inherit parent's target. If parent is
            # "default" and --target was specified,
            # the tests created inside will be filtered
            # at enqueue time.
            self.testsets.append(
                self.runner.import_testset(
                    filepath, self.target, self
                )
            )

    def add_testset(self, callback: Callable[[TestsetImpl], None]) -> None:
        # No fan-out here: the testset already has its
        # target assigned by import_testset / add_testset
        # in Runner
        self.__new_testset(callback, self.target)

    def __new_testset(self, callback: Callable[[TestsetImpl], None], target: Any) -> TestsetImpl:
        testset: TestsetImpl = TestsetImpl(self.runner, target, self, path=self.path)
        self.testsets.append(testset)
        callback(testset)
        return testset

    def new_testset(self, testset_name: str) -> TestsetImpl:
        testset: TestsetImpl = TestsetImpl(self.runner, self.target, self, path=self.path)
        testset.set_name(testset_name)
        self.testsets.append(testset)

        return testset

    def _has_visible_descendants(self) -> bool:
        """True if any test in this subtree survives --target filtering."""
        for testset in self.testsets:
            if testset._has_visible_descendants():
                return True
        for test in self.tests:
            if not test._is_filtered_by_cli_target():
                return True
        return False

    def _display_target(self) -> str:
        return self.target.name if self.target is not None else ''

    def dump_tests(self, rows: dict[str, dict], indent_level: int = 0) -> None:
        """Collect this testset's rows into ``rows``.

        Rows are keyed by full_name so fan-out instances of the same
        logical testset/test merge: their target names accumulate in
        one row's targets list instead of producing duplicate rows.
        """
        if not self._has_visible_descendants():
            return

        if self.name is not None:
            key = self.get_full_name() or self.name
            entry = rows.get(key)
            if entry is None:
                entry = {
                    'name': self.name,
                    'full_name': self.get_full_name() or '',
                    'indent_level': indent_level,
                    'targets': [],
                    'components': list(self.get_components()),
                    'description': '',
                }
                rows[key] = entry
            own_target = self._display_target()
            if own_target and own_target not in entry['targets']:
                entry['targets'].append(own_target)
            indent_level += 1

        for testset in self.testsets:
            testset.dump_tests(rows, indent_level)

        for test in self.tests:
            test.dump_tests(rows, indent_level)


    def enqueue(self) -> None:

        for testset in self.testsets:
            testset.enqueue()

        for test in self.tests:
            test.enqueue()


    def import_pytest(
        self, path: str, pytest_exe: str = 'pytest',
        markers: str | None = None,
        pytest_args: list | None = None,
        xdist: int = 0,
        batch_timeout: int | None = None,
        strict: bool = False,
        batch_exe: str | None = None
    ) -> None:
        """Import pytest tests from a directory or file.

        Discovers all pytest tests at build time and creates
        gvtest test entries. At run time, selected tests are
        executed as a single batched pytest invocation.

        Args:
            path: Directory or file containing pytest tests.
            pytest_exe: Pytest executable (default: pytest).
            markers: ``-m`` marker expression applied to both
                collection and the batch run.
            pytest_args: Extra arguments appended to the
                batch pytest command.
            xdist: Parallel workers for the batch
                (``pytest-xdist`` ``-n``). -1 follows the
                gvtest ``--threads`` option (one worker per
                CPU by default); 0 disables xdist; a
                positive value is used as-is.
            batch_timeout: Timeout in seconds for the whole
                batch; overrides the runner ``--max-timeout``.
            strict: Raise an error when collection fails or
                discovers no tests, instead of silently
                creating an empty testset.
            batch_exe: Executable for the run phase only
                (e.g. ``flock <file> pytest`` to serialize
                concurrent batches); discovery keeps using
                pytest_exe. Defaults to pytest_exe.
        """
        if not os.path.isabs(path):
            if self.path is not None:
                pytest_path = os.path.join(self.path, path)
            else:
                pytest_path = os.path.join(
                    os.getcwd(), path
                )
        else:
            pytest_path = path

        # Derive a name from the path
        name = os.path.basename(
            pytest_path.rstrip('/')
        )

        pt = PytestTestset(
            self.runner, self, name, self.target,
            os.path.dirname(pytest_path) or os.getcwd(),
            pytest_path, pytest_exe,
            markers=markers, pytest_args=pytest_args,
            xdist=xdist, batch_timeout=batch_timeout,
            strict=strict, batch_exe=batch_exe
        )
        pt.discover()
        self.testsets.append(pt)

    def declare_resource(
        self, name: str, capacity: int = 1
    ) -> None:
        """Declare a shared resource usable by this testset's commands.

        Thin pass-through to `Runner.declare_resource`. Resources live
        on the Runner (one registry for the whole run), so a top-level
        testset may declare once and sub-testsets can freely reference
        the same name.
        """
        self.runner.declare_resource(name, capacity)

    def _apply_no_clean(
        self, test: TestCommon, no_clean: bool
    ) -> None:
        """Drop the auto-generated `clean` command from a test.

        Useful for shared-build tests where a legacy Makefile's clean
        recipe would wipe the shared tree, and where the per-run work
        dir is re-created on each run anyway.
        """
        if not no_clean:
            return
        test.commands = [
            c for c in test.commands
            if getattr(c, 'name', None) != 'clean'
        ]

    def _annotate_build_resource(
        self, test: TestCommon, build_resource: str | None
    ) -> None:
        """Attach a resource lock to the auto-generated clean/build
        commands of a test factory.

        The `run` and `check` commands (if any) are left untouched so
        they remain free to execute in parallel across tests.
        """
        if build_resource is None:
            return
        for cmd in test.commands:
            if getattr(cmd, 'name', None) in ('clean', 'build'):
                existing = list(cmd.resources or [])
                if build_resource not in existing:
                    existing.append(build_resource)
                cmd.resources = existing

    def new_test(self, name: str) -> TestImpl:
        test: TestImpl = TestImpl(self.runner, self, name, self.target, self.path)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test


    def new_gvrun_test(
        self, name: str, flags: str = '',
        checker: Callable[..., Any] | None = None,
        retval: int = 0,
        build_resource: str | None = None,
        no_clean: bool = False
    ) -> GvrunTestImpl:
        test: GvrunTestImpl = GvrunTestImpl(
            self.runner, self, name, self.target,
            self.path, flags, checker=checker,
            retval=retval
        )
        self._apply_no_clean(test, no_clean)
        self._annotate_build_resource(test, build_resource)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_make_test(
        self, name: str, flags: str = '',
        checker: Callable[..., Any] | None = None,
        retval: int = 0, path: str | None = None,
        build_resource: str | None = None,
        no_clean: bool = False
    ) -> MakeTestImpl:
        test: MakeTestImpl = MakeTestImpl(
            self.runner, self, name, self.target,
            self.path if path is None else path,
            flags, checker=checker, retval=retval
        )
        self._apply_no_clean(test, no_clean)
        self._annotate_build_resource(test, build_resource)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_test(
        self, name: str, flags: str | None = None,
        checker: Callable[..., Any] | None = None,
        retval: int = 0,
        build_resource: str | None = None,
        no_clean: bool = False
    ) -> SdkTestImpl:
        test: SdkTestImpl = SdkTestImpl(
            self.runner, self, name, self.target,
            self.path, flags, checker=checker,
            retval=retval
        )
        self._apply_no_clean(test, no_clean)
        self._annotate_build_resource(test, build_resource)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_netlist_power_test(
        self, name: str, flags: str | None = None
    ) -> NetlistPowerSdkTestImpl:
        test: NetlistPowerSdkTestImpl = (
            NetlistPowerSdkTestImpl(
                self.runner, self, name,
                self.target, self.path, flags
            )
        )
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test
