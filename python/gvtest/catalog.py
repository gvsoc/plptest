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
Structured catalog export of discovered tests.

Walks the runner's testset tree and emits a flat JSON document with the
metadata needed by external consumers (doc generators, dashboards,
auditing scripts). Intentionally read-only on the runner — calling this
does not execute or schedule any test.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from gvtest import testsuite
from gvtest.testset_impl import TestsetImpl
from gvtest.tests import TestCommon


def _command_entry(cmd: testsuite.Command) -> dict[str, Any]:
    if isinstance(cmd, testsuite.Shell):
        return {'name': cmd.name, 'kind': 'shell',
                'cmd': cmd.cmd, 'retval': cmd.retval}
    if isinstance(cmd, testsuite.Checker):
        return {'name': cmd.name, 'kind': 'checker'}
    if isinstance(cmd, testsuite.Call):
        return {'name': cmd.name, 'kind': 'call'}
    return {'name': getattr(cmd, 'name', None), 'kind': 'unknown'}


def _test_entry(test: TestCommon, testset: TestsetImpl) -> dict[str, Any]:
    target = test.target
    target_name = target.get_name() if target is not None else None
    return {
        'full_name': test.get_full_name(),
        'path': test.path,
        'description': test.description,
        'components': test.get_components(),
        'target': target_name,
        'testset': testset.get_full_name(),
        'kind': type(test).__name__,
        'commands': [_command_entry(c) for c in test.commands],
        'benchs': [dataclasses.asdict(b) for b in test.benchs],
    }


def _walk(testset: Any, entries: list[dict[str, Any]]) -> None:
    if isinstance(testset, TestsetImpl):
        for child in testset.testsets:
            _walk(child, entries)
        for test in testset.tests:
            entries.append(_test_entry(test, testset))


def build_catalog(runner: Any) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for testset in runner.testsets:
        _walk(testset, entries)

    unclassified = [e['full_name'] for e in entries if not e['components']]
    components: dict[str, int] = {}
    for e in entries:
        for c in e['components']:
            components[c] = components.get(c, 0) + 1

    return {
        'tests': entries,
        'components': components,
        'unclassified_count': len(unclassified),
    }


def dump_catalog(runner: Any, output_path: str | None,
                 show_unclassified: bool = False) -> None:
    catalog = build_catalog(runner)
    text = json.dumps(catalog, indent=2, sort_keys=True)
    if output_path in (None, '', '-'):
        print(text)
    else:
        with open(output_path, 'w') as f:
            f.write(text)
            f.write('\n')
        print(f'Catalog written to {output_path} '
              f'({len(catalog["tests"])} tests, '
              f'{len(catalog["components"])} components, '
              f'{catalog["unclassified_count"]} unclassified)')

    if show_unclassified:
        names = [e['full_name'] for e in catalog['tests']
                 if not e['components']]
        if names:
            print('\nUnclassified tests (no components declared):')
            for n in names:
                print(f'  {n}')
