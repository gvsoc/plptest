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

import os
import logging
import plptest.testsuite as testsuite
import psutil
import threading
import queue
import sys
import os.path
import subprocess
import io
import select
from prettytable import PrettyTable
from datetime import datetime
import re
from xml.sax.saxutils import escape
from threading import Timer
import signal
import csv
import importlib
from importlib.machinery import SourceFileLoader
import json
import time


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    BG_HEADER =  '\033[105m'
    BG_OKBLUE =  '\033[104m'
    BG_OKGREEN = '\033[102m'
    BG_WARNING = '\033[103m'
    BG_FAIL =    '\033[101m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def table_dump_row(table, name, config, duration, passed, failed, skipped, excluded):

    total = passed + failed

    if skipped == 0:
        skipped_str = ''
    else:
        skipped_str = skipped

    if excluded == 0:
        excluded_str = ''
    else:
        excluded_str = excluded

    if failed == 0:
        failed_str = ''
        name_str = name
        config_str = config
    else:
        failed_str = failed
        name_str = bcolors.FAIL + name + bcolors.ENDC
        config_str = bcolors.FAIL + config + bcolors.ENDC

    table.add_row([
        name_str, config_str, f"{duration:.2f}", '%d/%d' % (passed, total), failed_str,
        skipped_str, excluded_str
    ])



class Target(object):

    def __init__(self, name, config=None):
        self.name = name
        if config is None:
            config = '{}'
        self.config = json.loads(config)

    def get_name(self):
        return self.name

    def get_sourceme(self):
        sourceme = self.config.get('sourceme')

        if sourceme is not None:
            return eval(sourceme)

        return None

    def format_properties(self, str):
        properties = self.config.get('properties')
        if properties is None:
            return str
        return str.format(**properties)

    def get_property(self, name):
        properties = self.config.get('properties')
        if properties is None:
            return None
        return properties.get(name)



class TestRunStats(object):

    def __init__(self, run, parent=None):
        self.run = run
        self.parent = parent
        self.stats = {'passed': 0, 'failed': 0, 'skipped': 0, 'excluded': 0, 'duration': 0}
        run.get_stats(self.stats)
        if parent:
            parent.add_stats(self.stats)

    def dump_table(self, table, dump_name):
        table_dump_row(table,
            self.run.test.get_full_name() if dump_name else '',
            self.run.config,
            self.stats['duration'],
            self.stats['passed'],
            self.stats['failed'],
            self.stats['skipped'],
            self.stats['excluded']
        )

    def dump_junit(self, test_file):
        if self.run.status != 'excluded':
            fullname = self.run.test.get_full_name()
            if fullname.count(':') == 0:
                name = fullname
                classname = self.run.get_target_name()
            elif fullname.count(':') == 1:
                testsuite, name = fullname.split(':', 1)
                classname = f'{self.run.get_target_name()}.{testsuite}'
            else:
                testset, testsuite, name = fullname.split(':', 2)
                classname = f'{self.run.get_target_name()}.{testsuite}'
            test_file.write('  <testcase classname="%s" name="%s" time="%f">\n' % (classname, name, self.run.duration))
            if self.run.status == 'skipped':
                test_file.write('    <skipped message="%s"/>\n' % self.run.skip_message)
            else:
                if self.run.status == 'passed':
                    test_file.write('    <success/>\n')
                else:
                    test_file.write('    <failure>\n')
                    for line in self.run.output:
                        RE_XML_ILLEGAL = u'([\u0000-\u0008\u000b-\u000c\u000e-\u001f\ufffe-\uffff])' + \
                                        u'|' + \
                                        u'([%s-%s][^%s-%s])|([^%s-%s][%s-%s])|([%s-%s]$)|(^[%s-%s])' % \
                                        (chr(0xd800),chr(0xdbff),chr(0xdc00),chr(0xdfff),
                                        chr(0xd800),chr(0xdbff),chr(0xdc00),chr(0xdfff),
                                        chr(0xd800),chr(0xdbff),chr(0xdc00),chr(0xdfff))
                        xml_line = re.sub(RE_XML_ILLEGAL, "", escape(line))
                        test_file.write(xml_line)
                    test_file.write('</failure>\n')
            test_file.write('  </testcase>\n')


class TestStats(object):

    def __init__(self, parent=None, test=None):
        self.parent = parent
        self.test = test
        self.child_runs_dict = {}
        self.child_runs = []
        self.stats = {'passed': 0, 'failed': 0, 'skipped': 0, 'excluded': 0, 'duration': 0}

    def add_child_run(self, run):
        child_run_stats = self.child_runs_dict.get(run.target)
        if child_run_stats is None:
            child_run_stats = TestRunStats(run=run, parent=self)
            self.child_runs_dict[run.target] = child_run_stats
            self.child_runs.append(child_run_stats)

    def add_stats(self, stats):
        for key in stats.keys():
            self.stats[key] += stats[key]
        if self.parent:
            self.parent.add_stats(stats)

    def dump_table(self, table):
        if len(self.child_runs) == 0:
            return

        if len(self.child_runs) == 1:
            self.child_runs[0].dump_table(table, True)
        else:
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
                run.dump_table(table, False)

    def dump_junit(self, test_file):
        for run in self.child_runs:
            run.dump_junit(test_file)

class TestsetStats(object):

    def __init__(self, testset=None, parent=None):
        self.child_tests = {}
        self.child_testsets = {}

        self.parent = parent
        self.testset = testset
        self.stats = {'passed': 0, 'failed': 0, 'skipped': 0, 'excluded': 0, 'duration': 0}

    def add_stats(self, stats):
        for key in stats.keys():
            self.stats[key] += stats[key]
        if self.parent:
            self.parent.add_stats(stats)

    def add_child_testset(self, testset):
        child_testset_stats = self.child_testsets.get(testset.name)
        if child_testset_stats is None:
            child_testset_stats = TestsetStats(testset=testset, parent=self)
            self.child_testsets[testset.name] = child_testset_stats

        for child_testset in testset.testsets:
            child_testset_stats.add_child_testset(child_testset)

        for test in testset.tests:
            child_testset_stats.add_child_test(test)

    def add_child_test(self, test):
        child_test_stats = self.child_tests.get(test.name)
        if child_test_stats is None:
            child_test_stats = TestStats(self, test)
            self.child_tests[test.name] = child_test_stats

        for run in test.runs:
            child_test_stats.add_child_run(run)

    def dump_table(self, table):
        is_empty = True
        for stat in self.stats.values():
            if stat != 0:
                is_empty = False
        if is_empty:
            return

        if self.testset is not None and self.testset.name is not None:
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
            child.dump_table(table)

        for child in self.child_testsets.values():
            child.dump_table(table)

    def dump_junit(self, test_file):
        for child in self.child_testsets.values():
            child.dump_junit(test_file)

        for child in self.child_tests.values():
            child.dump_junit(test_file)

    def dump_junit_files(self, report_path):
        os.makedirs(report_path, exist_ok=True)

        for stats in self.child_testsets.values():
            testset = stats.testset
            filename = '%s/TEST-%s.xml' % (report_path, testset.name)
            with open(filename, 'w') as test_file:
                test_file.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                test_file.write('<testsuite skipped="%d" errors="%d" failures="%d" name="%s" tests="%d" time="%f">\n' % \
                    (stats.stats['skipped'], stats.stats['failed'], stats.stats['failed'], testset.name,
                    stats.stats['failed'] + stats.stats['passed'], stats.stats['duration']))
                stats.dump_junit(test_file)
                test_file.write('</testsuite>\n')






class TestRun(object):

    def __init__(self, test, target):
        self.target = target
        self.test = test
        self.runner = test.runner
        self.lock = threading.Lock()
        self.duration = 0
        if target is not None:
            self.config = target.name
        else:
            self.config = self.runner.config

        self.sourceme = None

        if self.target is not None:
            self.sourceme = self.target.get_sourceme()

    def get_target_name(self):
        if self.target is None:
            return self.config

        return self.target.name

    def get_stats(self, stats):
        stats[self.status] += 1
        stats['duration'] = self.duration

    # Called by worker to execute the test
    def run(self):

        self.__print_start_message()

        self.output = ''
        self.status = "passed"

        start_time = datetime.now()

        timeout = self.runner.max_timeout
        self.timeout_reached = False

        if timeout != -1:
            timer = Timer(timeout, self.kill)
            timer.start()

        for command in self.test.commands:

            retval = self.__exec_command(command, self.target, self.sourceme)

            if retval != 0 or self.timeout_reached:
                if self.timeout_reached:
                    self.__dump_test_msg('--- Timeout reached ---\n')
                self.status = "failed"
                break

        if timeout != -1:
            timer.cancel()

        duration = datetime.now() - start_time
        self.duration = \
            (duration.microseconds +
                (duration.seconds + duration.days * 24 * 3600) * 10**6) / 10**6

        if self.runner.safe_stdout:
            print (self.output)

        for bench in self.test.benchs:
            pattern = re.compile(bench[0])
            for line in self.output.splitlines():
                result = pattern.match(line)
                if result is not None:
                    value = float(result.group(1))
                    name = bench[1]
                    desc = bench[2]
                    self.runner.register_bench_result(name, value, desc)


        self.print_end_message()

        self.runner.terminate(self)

    def kill(self):
        self.lock.acquire()
        self.timeout_reached = True
        if self.current_proc is not None:
            try:
                process = psutil.Process(pid=self.current_proc.pid)

                for children in process.children(recursive=True):
                    os.kill(children.pid, signal.SIGKILL)
            except:
                pass
        self.lock.release()

    # Print start bannier
    def __print_start_message(self):
        testname = self.test.get_full_name().ljust(self.runner.get_max_testname_len() + 5)
        if self.target is not None:
            config = self.target.name
        else:
            config = self.runner.get_config()
        print (bcolors.OKBLUE + 'START'.ljust(8) + bcolors.ENDC + bcolors.BOLD + testname + bcolors.ENDC + ' %s' % (config))
        sys.stdout.flush()

    # Print end bannier
    def print_end_message(self):
        testname = self.test.get_full_name().ljust(self.runner.get_max_testname_len() + 5)
        if self.target is not None:
            config = self.target.name
        else:
            config = self.runner.get_config()

        if self.status == 'passed':
            test_result_str = bcolors.OKGREEN + 'OK '.ljust(8) + bcolors.ENDC
        elif self.status == 'failed':
            test_result_str = bcolors.FAIL + 'KO '.ljust(8) + bcolors.ENDC
        elif self.status == 'skipped':
            test_result_str = bcolors.WARNING + 'SKIP '.ljust(8) + bcolors.ENDC
        elif self.status == 'excluded':
            test_result_str = bcolors.HEADER + 'EXCLUDE '.ljust(8) + bcolors.ENDC

        print (test_result_str + bcolors.BOLD + testname + bcolors.ENDC + ' %s' % (config))
        sys.stdout.flush()

    def __exec_process(self, command):
        self.lock.acquire()
        if self.timeout_reached:
            return ['', -1]

        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True,
            cwd=self.test.path)

        self.current_proc = proc

        self.lock.release()

        for line in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors='replace'):
            self.__dump_test_msg(line)

        retval = proc.wait()
        self.current_proc = None

        return retval


    def __dump_test_msg(self, msg):
        self.output += msg
        if self.runner.stdout:
            print (msg[:-1])


    # Called by run method to execute specific command
    def __exec_command(self, command, target, sourceme):

        if type(command) == testsuite.Shell:
            cmd = command.cmd
            if self.target is not None:
                cmd = self.target.format_properties(cmd)

            self.__dump_test_msg(f'--- Shell command: {cmd} ---\n')

            if sourceme is not None:
                cmd = f'plptest_cmd_stub {sourceme} {cmd}'

            retval = 0 if self.__exec_process(cmd) == command.retval else 1

        elif type(command) == testsuite.Checker:
            self.__dump_test_msg(f'--- Checker command ---\n')
            result = command.callback[0](self, self.output, *command.callback[1], **command.callback[2])
            if result[1] is not None:
                self.__dump_test_msg(result[1])

            retval = 0 if result[0] else -1

        elif type(command) == testsuite.Call:
            self.__dump_test_msg(f'--- Call command ---\n')
            return command.callback()

        else:
            print ('Unsupported command type: ' + str(type(command)))
            retval = -1

        return retval

class TestCommon(object):

    def __init__(self, runner, parent, name, target, path):
        self.runner = runner
        self.target = target
        self.name = name
        self.parent = parent
        self.full_name = None
        self.commands = []
        self.path = path
        self.status = None
        self.skipped = None
        if self.path == '':
            self.path = os.getcwd()
        self.current_proc = None

        self.full_name = self.name

        if self.parent is not None:
            parent_name = self.parent.get_full_name()
            if parent_name is not None:
                self.full_name =  f'{parent_name}:{self.name}'

        self.runner.declare_name(self.full_name)
        self.benchs = []
        self.runs = []

    def skip(self, msg):
        self.skipped = msg
        return self

    def get_target(self):
        return self.target

    # Called by user to add commands
    def add_command(self, command):
        self.commands.append(command)


    # Called by runner to enqueue this test to the list of tests ready to be executed
    def enqueue(self):
        run = TestRun(self, self.target)
        if self.runner.is_skipped(self.get_full_name()) or self.skipped is not None:
            if self.skipped is not None:
                run.skip_message = self.skipped
            else:
                run.skip_message = "Skipped from command line"
            run.status = "skipped"
            self.runs.append(run)
            run.print_end_message()

        else:
            self.runs.append(run)
            self.runner.enqueue_test(run)


    # Can be called to get full name including hierarchy path
    def get_full_name(self):
        return self.full_name



class TestImpl(testsuite.Test, TestCommon):

    def __init__(self, runner, parent, name, target, path):
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class MakeTestImpl(testsuite.Test, TestCommon):

    def __init__(self, runner, parent, name, target, path, flags, checker=None, retval=0):
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' platform=%s' % platform

        self.flags += f' build=build/{runner.get_config()}/{self.name}'

        self.add_command(testsuite.Shell('clean', 'make clean %s' % (self.flags)))
        self.add_command(testsuite.Shell('build', 'make build %s' % (self.flags)))
        self.add_command(testsuite.Shell('run', 'make run %s' % (self.flags), retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class GvrunTestImpl(testsuite.SdkTest, TestCommon):

    def __init__(self, runner, parent, name, target, path, flags, checker=None, retval=0):
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' --platform=%s' % platform

        target = target.get_name()
        self.flags += f' --build-dir=build/{target}/{self.name}'

        cmd = f'gvrun --target {target} {self.flags}'
        self.add_command(testsuite.Shell('clean', f'{cmd} clean'))
        self.add_command(testsuite.Shell('build', f'{cmd} build'))
        self.add_command(testsuite.Shell('run', f'{cmd} run', retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class SdkTestImpl(testsuite.SdkTest, TestCommon):

    def __init__(self, runner, parent, name, target, path, flags, checker=None, retval=0):
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' --platform=%s' % platform

        self.flags += f' --build=build/{runner.get_config()}/{self.name}'

        self.add_command(testsuite.Shell('clean', 'posbuild clean %s' % (self.flags)))
        self.add_command(testsuite.Shell('build', 'posbuild build %s' % (self.flags)))
        self.add_command(testsuite.Shell('run', 'posbuild run %s' % (self.flags), retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class NetlistPowerSdkTestImpl(SdkTestImpl):

    def __init__(self, runner, parent, name, target, path, flags):
        SdkTestImpl.__init__(self, runner, parent, name, target, path, flags)

        self.add_command(testsuite.Shell('power_gen', 'make power_gen %s' % (self.flags)))
        self.add_command(testsuite.Shell('power_copy', 'make power_copy %s' % (self.flags)))




class TestsetImpl(testsuite.Testset):

    def __init__(self, runner, target, parent=None, path=None):
        self.runner = runner
        self.name = None
        self.tests = []
        self.testsets = []
        self.parent = parent
        self.path = path
        self.targets = {}
        self.active_targets = []
        self.target = target

    def get_target(self):
        return self.target

    def get_path(self):
        return self.path

    def get_property(self, name):
        return self.runner.get_property(name)

    def set_name(self, name):
        self.name = name

    def add_target(self, name, config=None):
        if config is None:
            config = '{}'
        self.targets[name] = Target(name, config)

    def get_full_name(self):
        if self.parent is not None:
            parent_name = self.parent.get_full_name()
            if parent_name is not None:
                if self.name is None:
                    return parent_name
                else:
                    return f'{parent_name}:{self.name}'

        return self.name

    def import_testset(self, file):
        active_targets = self.runner.get_active_targets()
        filepath = file
        if self.path is not None:
            filepath = os.path.join(self.path, file)

        if len(self.targets) == 0 or len(active_targets) == 1 and active_targets[0] == 'default':
            self.testsets.append(self.runner.import_testset(filepath, self.target, self))
        else:
            for target_name in active_targets:
                target = self.targets.get(target_name)
                if target is not None:
                    self.testsets.append(self.runner.import_testset(filepath, target, self))

    def add_testset(self, callback):
        active_targets = self.runner.get_active_targets()
        if len(self.targets) == 0 or len(active_targets) == 1 and active_targets[0] == 'default':
            self.__new_testset(callback, self.target)
        else:
            for target_name in self.runner.get_active_targets():
                target = self.targets.get(target_name)
                if target is not None:
                    self.__new_testset(callback, target)

    def __new_testset(self, callback, target):
        testset = TestsetImpl(self.runner, target, self, path=self.path)
        self.testsets.append(testset)
        callback(testset)
        return testset

    def new_testset(self, testset_name):
        testset = TestsetImpl(self.runner, self.target, self, path=self.path)
        testset.set_name(testset_name)
        self.testsets.append(testset)

        return testset


    def enqueue(self):

        for testset in self.testsets:
            testset.enqueue()

        for test in self.tests:
            test.enqueue()


    def new_test(self, name):
        test = TestImpl(self.runner, self, name, self.target, self.path)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test


    def new_gvrun_test(self, name, flags='', checker=None, retval=0):
        test = GvrunTestImpl(self.runner, self, name, self.target, self.path, flags, checker=checker, retval=retval)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_make_test(self, name, flags='', checker=None, retval=0):
        test = MakeTestImpl(self.runner, self, name, self.target, self.path, flags, checker=checker, retval=retval)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_test(self, name, flags='', checker=None, retval=0):
        test = SdkTestImpl(self.runner, self, name, self.target, self.path, flags, checker=checker, retval=retval)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_netlist_power_test(self, name, flags=''):
        test = NetlistPowerSdkTestImpl(self.runner, self, name, self.target, self.path, flags)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test


class Worker(threading.Thread):

    def __init__(self, runner):
        super().__init__()

        self.runner = runner

    def run(self):
        while True:
            test = self.runner.pop_test()
            if test is None:
                return
            test.run()


class Runner():

    def __init__(self, config='default', load_average=0.9, nb_threads=0, properties=None,
            stdout=False, safe_stdout=False, max_output_len=-1, max_timeout=-1,
            test_list=None, test_skip_list=None, commands=None, commands_exclude=None,
            flags=None, bench_csv_file=None, bench_regexp=None, targets=None):
        self.nb_threads = nb_threads
        self.queue = queue.Queue()
        self.testsets = []
        self.pending_tests = []
        self.max_testname_len = 0
        self.config = config
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.load_average = load_average
        self.stdout = stdout
        self.safe_stdout = safe_stdout
        self.nb_pending_tests = 0
        self.test_skip_list = test_skip_list
        self.max_timeout = max_timeout
        self.flags = flags
        self.bench_results = {}
        self.bench_csv_file = bench_csv_file
        self.properties = {}
        self.test_list = test_list
        self.targets = targets
        self.default_target = Target('default')
        if self.targets is None:
            self.targets = [ 'default' ]
        self.cpu_poll_interval = 0.1
        for prop in properties:
          name, value = prop.split('=')
          self.properties[name] = value


        if bench_csv_file is not None:
            if os.path.exists(bench_csv_file):
                with open(bench_csv_file, 'r') as file:
                    csv_reader = csv.reader(file)
                    for row in csv_reader:
                        self.bench_results[row[0]] = row[1:]

    def get_active_targets(self):
        return self.targets

    def get_property(self, name):
        return self.properties.get(name)

    def is_selected(self, test):
        if self.test_list is None:
            return True

        for selected_test in self.test_list:
            if test.get_full_name().find(selected_test) == 0:
                return True

        return False

    def is_skipped(self, name):
        if self.test_skip_list is not None:
            for skip in self.test_skip_list:
                if name.find(skip) == 0:
                    return True

        return False


    def run(self):
        self.event.clear()

        for testset in self.testsets:
            testset.enqueue()

        if len(self.pending_tests) > 0:

            self.check_pending_tests()

            self.event.wait()

        self.stats = TestsetStats()
        for testset in self.testsets:
            self.stats.add_child_testset(testset)

        if self.bench_csv_file is not None:
            with open(self.bench_csv_file, 'w') as file:
                csv_writer = csv.writer(file)
                for key, value in self.bench_results.items():
                    csv_writer.writerow([key] + value)



    def declare_name(self, name):
        name_len = len(name)
        if self.max_testname_len < name_len:
            self.max_testname_len = name_len


    def dump_table(self):
        x = PrettyTable(['test', 'config', 'time', 'passed/total', 'failed', 'skipped', 'excluded'])
        x.align = "r"
        x.align["test"] = "l"
        x.align["config"] = "l"
        self.stats.dump_table(x)
        print()
        print(x)


    def dump_junit(self, report_path):
        os.makedirs(report_path, exist_ok=True)

        self.stats.dump_junit_files(report_path)



    def get_config(self):
        return self.config

    def pop_test(self):
        return self.queue.get()

    def start(self):
        if self.nb_threads == 0:
            self.nb_threads = psutil.cpu_count(logical=True)

        for thread_id in range(0, self.nb_threads):
            Worker(self).start()


    def stop(self):
        for thread_id in range(0, self.nb_threads):
            self.queue.put(None)

    def add_testset(self, file):
        if not os.path.isabs(file):
            file = os.path.join(os.getcwd(), file)
        self.testsets.append(self.import_testset(file, self.default_target))


    def import_testset(self, file, target, parent=None):
        logging.debug(f"Parsing file (path: {file})")

        try:
            spec = importlib.util.spec_from_loader("module.name", SourceFileLoader("module.name", file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except FileNotFoundError as exc:
            raise RuntimeError(bcolors.FAIL + 'Unable to open test configuration file: ' + file + bcolors.ENDC)

        testset = TestsetImpl(self, target, parent, path=os.path.dirname(file))

        module.testset_build(testset)

        return testset


    def enqueue_test(self, test):
        self.nb_pending_tests += 1

        self.pending_tests.append(test)



    def check_pending_tests(self):
        while len(self.pending_tests) > 0:

            while not self.check_cpu_load():
                time.sleep(self.cpu_poll_interval)

            test = self.pending_tests.pop()
            self.queue.put(test)


    def check_cpu_load(self):
        if self.load_average == 1.0:
            return True

        load = psutil.cpu_percent(interval=self.cpu_poll_interval)

        return load < self.load_average * 100


    def get_max_testname_len(self):
        return self.max_testname_len


    def terminate(self, test):
        self.lock.acquire()
        self.nb_pending_tests -= 1

        if self.nb_pending_tests == 0:
            self.event.set()

        self.lock.release()

    def register_bench_result(self, name, value, desc):
        self.bench_results[name] = [value, desc]
