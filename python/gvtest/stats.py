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
Statistics collection and aggregation — TestRunStats, TestStats, TestsetStats.
"""

from __future__ import annotations

import os
import re
from typing import Any, TextIO
from xml.sax.saxutils import escape

from rich.table import Table

from gvtest.reporting import table_dump_row


class TestRunStats(object):

    def __init__(self, run: Any, parent: TestStats | None = None) -> None:
        self.run: Any = run
        self.parent: TestStats | None = parent
        self.stats: dict[str, int | float] = {
            'passed': 0, 'failed': 0, 'skipped': 0,
            'excluded': 0, 'duration': 0
        }
        run.get_stats(self.stats)
        if parent:
            parent.add_stats(self.stats)

    def dump_table(self, table: Table, dump_name: bool, report_all: bool) -> None:
        if self.stats['failed'] > 0 or report_all:
            table_dump_row(table,
                self.run.test.get_full_name() if dump_name else '',
                self.run.config,
                self.stats['duration'],
                self.stats['passed'],
                self.stats['failed'],
                self.stats['skipped'],
                self.stats['excluded']
            )

    def dump_junit(self, test_file: TextIO) -> None:
        if self.run.status != 'excluded':
            target_label = self.run.get_target_name()
            platform = self.run.runner.platform
            if platform:
                target_label = f'{target_label}:{platform}'
            fullname = self.run.test.get_full_name()
            if fullname.count(':') == 0:
                name = fullname
                classname = target_label
            elif fullname.count(':') == 1:
                testsuite, name = fullname.split(':', 1)
                classname = f'{target_label}.{testsuite}'
            else:
                testset, testsuite, name = fullname.split(':', 2)
                classname = f'{target_label}.{testsuite}'
            test_file.write(
                '  <testcase classname="%s" name="%s"'
                ' time="%f">\n'
                % (classname, name, self.run.duration)
            )
            if self.run.status == 'skipped':
                test_file.write('    <skipped message="%s"/>\n' % self.run.skip_message)
            else:
                if self.run.status == 'passed':
                    test_file.write('    <success/>\n')
                else:
                    test_file.write('    <failure>\n')
                    for line in self.run.output:
                        RE_XML_ILLEGAL = (
                            u'([\u0000-\u0008\u000b-\u000c'
                            u'\u000e-\u001f\ufffe-\uffff])'
                            u'|'
                            u'([%s-%s][^%s-%s])'
                            u'|([^%s-%s][%s-%s])'
                            u'|([%s-%s]$)'
                            u'|(^[%s-%s])'
                        ) % \
                                        (chr(0xd800),chr(0xdbff),chr(0xdc00),chr(0xdfff),
                                        chr(0xd800),chr(0xdbff),chr(0xdc00),chr(0xdfff),
                                        chr(0xd800),chr(0xdbff),chr(0xdc00),chr(0xdfff))
                        xml_line: str = re.sub(RE_XML_ILLEGAL, "", escape(line))
                        test_file.write(xml_line)
                    test_file.write('</failure>\n')
            test_file.write('  </testcase>\n')


class TestStats(object):

    def __init__(self, parent: TestsetStats | None = None, test: Any = None) -> None:
        self.parent: TestsetStats | None = parent
        self.test: Any = test
        self.child_runs_dict: dict[Any, TestRunStats] = {}
        self.child_runs: list[TestRunStats] = []
        self.stats: dict[str, int | float] = {
            'passed': 0, 'failed': 0, 'skipped': 0,
            'excluded': 0, 'duration': 0
        }

    def add_child_run(self, run: Any) -> None:
        child_run_stats = self.child_runs_dict.get(run.target)
        if child_run_stats is None:
            child_run_stats = TestRunStats(run=run, parent=self)
            self.child_runs_dict[run.target] = child_run_stats
            self.child_runs.append(child_run_stats)

    def add_stats(self, stats: dict[str, int | float]) -> None:
        for key in stats.keys():
            self.stats[key] += stats[key]
        if self.parent:
            self.parent.add_stats(stats)

    def dump_table(self, table: Table, report_all: bool) -> None:
        if len(self.child_runs) == 0:
            return

        if len(self.child_runs) == 1:
            self.child_runs[0].dump_table(table, True, report_all)
        else:
            if self.stats['failed'] > 0 or report_all:
                table_dump_row(table,
                    self.test.get_full_name(),
                    '',
                    self.stats['duration'],
                    self.stats['passed'],
                    self.stats['failed'],
                    self.stats['skipped'],
                    self.stats['excluded']
                )

            for run in self.child_runs:
                run.dump_table(table, False, report_all)

    def dump_junit(self, test_file: TextIO) -> None:
        for run in self.child_runs:
            run.dump_junit(test_file)


class TestsetStats(object):

    def __init__(self, testset: Any = None, parent: TestsetStats | None = None) -> None:
        self.child_tests: dict[str, TestStats] = {}
        self.child_testsets: dict[str, TestsetStats] = {}

        self.parent: TestsetStats | None = parent
        self.testset: Any = testset
        self.stats: dict[str, int | float] = {
            'passed': 0, 'failed': 0, 'skipped': 0,
            'excluded': 0, 'duration': 0
        }

    def add_stats(self, stats: dict[str, int | float]) -> None:
        for key in stats.keys():
            self.stats[key] += stats[key]
        if self.parent:
            self.parent.add_stats(stats)

    def add_child_testset(self, testset: Any) -> None:
        child_testset_stats = self.child_testsets.get(testset.name)
        if child_testset_stats is None:
            child_testset_stats = TestsetStats(testset=testset, parent=self)
            self.child_testsets[testset.name] = child_testset_stats

        for child_testset in testset.testsets:
            child_testset_stats.add_child_testset(child_testset)

        for test in testset.tests:
            child_testset_stats.add_child_test(test)

    def add_child_test(self, test: Any) -> None:
        child_test_stats = self.child_tests.get(test.name)
        if child_test_stats is None:
            child_test_stats = TestStats(self, test)
            self.child_tests[test.name] = child_test_stats

        for run in test.runs:
            child_test_stats.add_child_run(run)

    def dump_table(self, table: Table, report_all: bool) -> None:
        is_empty: bool = True
        for stat in self.stats.values():
            if stat != 0:
                is_empty = False
        if is_empty:
            return

        if self.testset is not None and self.testset.name is not None:
            if self.stats['failed'] > 0 or report_all:
                table_dump_row(table,
                    self.testset.get_full_name(),
                    '',
                    self.stats['duration'],
                    self.stats['passed'],
                    self.stats['failed'],
                    self.stats['skipped'],
                    self.stats['excluded']
                )

        for child in self.child_tests.values():
            child.dump_table(table, report_all)

        for child in self.child_testsets.values():
            child.dump_table(table, report_all)

    def dump_junit(self, test_file: TextIO) -> None:
        for child in self.child_testsets.values():
            child.dump_junit(test_file)

        for child in self.child_tests.values():
            child.dump_junit(test_file)

    def dump_junit_files(self, report_path: str) -> None:
        os.makedirs(report_path, exist_ok=True)

        for stats in self.child_testsets.values():
            testset = stats.testset
            filename: str = '%s/TEST-%s.xml' % (report_path, testset.name)
            with open(filename, 'w') as test_file:
                test_file.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                test_file.write(
                    '<testsuite skipped="%d" errors="%d"'
                    ' failures="%d" name="%s"'
                    ' tests="%d" time="%f">\n' % (
                        stats.stats['skipped'],
                        stats.stats['failed'],
                        stats.stats['failed'],
                        testset.name,
                        stats.stats['failed']
                        + stats.stats['passed'],
                        stats.stats['duration']
                    )
                )
                stats.dump_junit(test_file)
                test_file.write('</testsuite>\n')
