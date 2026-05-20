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


import argparse
import logging
import os
import sys

from gvtest.runner import Runner




def summary(runner, args):
    runner.summary()

def tests(runner, args):
    runner.tests()

def run(runner, args):
    runner.run()

def table(runner, args):
    runner.dump_table()

def junit(runner, args):
    runner.dump_junit(args.junit_report_path)

def catalog(runner, args):
    from gvtest.catalog import dump_catalog
    dump_catalog(runner, args.catalog_output,
                 show_unclassified=args.show_unclassified)

def all(runner, args):
    run(runner, args)
    table(runner, args)
    summary(runner, args)
    junit(runner, args)

commands = {
  'tests'  : ['Show the tests', tests],
  'run'    : ['Run the tests', run],
  'table'  : ['Dump a report using a table', table],
  'summary' : ['Dump summary', summary],
  'junit'  : ['Dump junit report', junit],
  'catalog': ['Export test metadata as JSON', catalog],
  'all'  : ['Execute commands run table summary junit', all],
}

commandHelp = """Available commands:
"""

for name, cmd in commands.items():
	commandHelp += '  %-10s %s\n' % (name, cmd[0])

parser = argparse.ArgumentParser(
    description='Run a testset',
    epilog=commandHelp,
    formatter_class=argparse.RawDescriptionHelpFormatter
)

parser.add_argument('command', metavar='CMD', type=str, nargs='*',
                   help='a command to be executed (see the command help afterwards)')

parser.add_argument(
    "--testset", dest="testset", action="append",
    default=None, metavar="PATH",
    help="Path to the testset. Default: %(default)s"
)
parser.add_argument(
    "--property", dest="properties", action="append",
    default=[], help="Specifies property"
)
parser.add_argument(
    '--py-stack', dest='py_stack', action="store_true",
    help='Show python exception stack.'
)
parser.add_argument('--verbose', dest='verbose', action="store_true", help='Enable verbose mode.')
parser.add_argument('--dump-all', dest='all', action="store_true", help='Report all tests.')
parser.add_argument(
    "--target", dest="targets", action="append",
    help="Specify a target for which the tests must be run"
)
parser.add_argument(
    "--threads", dest="threads", default=0, type=int,
    help="Specify the number of worker threads"
)
parser.add_argument("--config", dest="config", default='default', help="Specify config name")
parser.add_argument(
    "--load-average", dest="load_average", default=1.0,
    type=float,
    help="Specify the system average load that this "
    "tool should try to respect, from 0 to 1"
)
parser.add_argument(
    "--stdout", dest="stdout", action="store_true",
    help="Dumps test output to stdout"
)
parser.add_argument(
    "--safe-stdout", dest="safe_stdout",
    action="store_true",
    help="Dumps test output to stdout once the test is done"
)
parser.add_argument(
    "--no-progress", dest="no_progress",
    action="store_true",
    help="Disable live progress bar"
)
parser.add_argument(
    "--tui", dest="tui",
    action="store_true",
    help="Launch full-screen TUI with split-pane display"
)
parser.add_argument(
    "--max-output-len", dest="max_output_len", type=int,
    default=-1,
    help="Maximum length of a test output. "
    "Default: %(default)s bytes"
)
parser.add_argument(
    "--max-timeout", dest="max_timeout", default=-1,
    type=int,
    help="Sets maximum timeout allowed for a test"
)
parser.add_argument(
    "--test", dest="test_list", default=None,
    action="append", help="Specify a test to be run"
)
parser.add_argument(
    "--skip", dest="test_skip_list", default=None,
    action="append",
    help="Specify a test to be skipped"
)
parser.add_argument(
    "--cmd", dest="commands", action="append",
    default=None, metavar="PATH",
    help="Add command to be executed. Default: %(default)s"
)
parser.add_argument(
    "--cmd-exclude", dest="commands_exclude",
    action="append", default=None, metavar="PATH",
    help="Add command to be excluded. Default: %(default)s"
)
parser.add_argument("--flags", dest="flags", action="append", default=[], help="Specifies flags")
parser.add_argument("--platform", dest="platform", default='gvsoc', help="Specifies platform")
parser.add_argument(
    "--junit-report-path", dest="junit_report_path",
    default='junit-reports', help="Specifies flags"
)
parser.add_argument(
    "--bench-regexp", dest="bench_regexp",
    default='.*@BENCH@(.*)@DESC@(.*)@',
    help="Specify regexp for extracting benchmark results"
)
parser.add_argument(
    "--bench-db", dest="bench_db",
    default=None,
    help="Specify SQLite database for benchmark results"
)
parser.add_argument(
    "--no-fail", dest="no_fail", action="store_true",
    help="Return an error if there is any test failure"
)
parser.add_argument(
    "--catalog-output", dest="catalog_output", default=None,
    metavar="PATH",
    help="Path to write the catalog JSON "
    "(default: stdout). Used by the `catalog` command."
)
parser.add_argument(
    "--show-unclassified", dest="show_unclassified",
    action="store_true",
    help="When exporting the catalog, list tests with no declared "
    "components so coverage gaps are visible."
)
parser.add_argument(
    "--tolerate-missing", dest="tolerate_missing",
    action="store_true",
    help="Warn and continue when an imported testset file is missing "
    "(useful with partial checkouts when exporting the catalog)."
)
parser.add_argument(
    "--resource", dest="resources", action="append",
    default=None, metavar="NAME[:CAPACITY]",
    help="Declare a shared resource with the given capacity "
    "(default 1). Commands in testset.cfg referencing this "
    "resource are serialized up to that many concurrent "
    "holders. Repeatable."
)


args = parser.parse_args()

if args.verbose:
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

if len(args.command) == 0:
  args.command.append('run')
  args.command.append('table')
  args.command.append('summary')

if args.testset is None:
  args.testset = [os.getcwd() + '/testset.cfg']

runner = None

try:
    runner = Runner(
        config=args.config, load_average=args.load_average, nb_threads=args.threads,
        properties=args.properties, stdout=args.stdout, safe_stdout=args.safe_stdout,
        max_output_len=args.max_output_len, max_timeout=args.max_timeout, test_list=args.test_list,
        test_skip_list=args.test_skip_list,
        commands=args.commands,
        commands_exclude=args.commands_exclude,
        flags=args.flags,
        bench_db=args.bench_db,
        bench_regexp=args.bench_regexp,
        targets=args.targets,
        platform=args.platform, report_all=args.all,
        progress=not args.no_progress,
        tolerate_missing=args.tolerate_missing,
        resources=args.resources,
    )

    for testset in args.testset:
        runner.add_testset(testset)

    if args.tui:
        from gvtest.tui import run_tui
        run_tui(runner)
        # Print summary after TUI exits
        runner.dump_table()
        runner.summary()
    else:
        runner.start()

        for command in args.command:
            if commands.get(command) == None:
                raise RuntimeError(
                    'Invalid command: ' + command
                )

            cmd_entry = commands.get(command)
            assert cmd_entry is not None
            cmd_entry[1](runner, args)

        runner.stop()

except RuntimeError as e:
    if runner is not None:
        runner.stop()

    if args.py_stack:
        raise

    print('Input error: ' + str(e), file = sys.stderr)
    sys.exit(1)

except:
    if runner is not None:
        runner.stop()
    raise

if args.no_fail and runner is not None and runner.stats.stats['failed'] != 0:
  exit(1)
