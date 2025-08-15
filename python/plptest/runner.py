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

    def __init__(self, name, config):
        self.name = name
        self.config = json.loads(config)

    def get_sourceme(self):
        sourceme = self.config.get('sourceme')

        if sourceme is not None:
            return eval(sourceme)

        return None


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

    def get_stats(self, parent_stats):
        self.stats = {'passed': 0, 'failed': 0, 'skipped': 0, 'excluded': 0, 'duration': 0}
        self.stats[self.status] += 1
        self.stats['duration'] = self.duration

        if parent_stats is not None:
            for key in parent_stats.keys():
                parent_stats[key] += self.stats[key]

    def dump_table(self, table, dump_name):
        table_dump_row(table,
            self.test.get_full_name() if dump_name else '',
            self.config,
            self.duration,
            self.stats['passed'],
            self.stats['failed'],
            self.stats['skipped'],
            self.stats['excluded']
        )

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
            retval = self.__exec_command(command, self.sourceme)

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


        self.__print_end_message()

        self.runner.terminate(self)

    def dump_junit(self, test_file):
        if self.status != 'excluded':
            test_file.write('  <testcase classname="%s" name="%s" time="%f">\n' % (self.config, self.test.get_full_name(), self.duration))
            if self.status == 'skipped':
                test_file.write('    <skipped message="%s"/>\n' % self.skip_message)
            else:
                if self.status == 'passed':
                    test_file.write('    <success/>\n')
                else:
                    test_file.write('    <failure>\n')
                    for line in self.output:
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
    def __print_end_message(self):
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
    def __exec_command(self, command, sourceme):

        if type(command) == testsuite.Shell:
            self.__dump_test_msg(f'--- Shell command: {command.cmd} ---\n')

            if sourceme is not None:
                cmd = f'plptest_cmd_stub {sourceme} {command.cmd}'
            else:
                cmd = command.cmd

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

    def __init__(self, runner, parent, name, path):
        self.runner = runner
        self.name = name
        self.parent = parent
        self.full_name = None
        self.commands = []
        self.path = path
        self.status = None
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

    def get_target(self, target_name):
        if self.parent is not None:
            return self.parent.get_target(target_name)
        return None

    # Called by user to add commands
    def add_command(self, command):
        self.commands.append(command)


    # Called by runner to enqueue this test to the list of tests ready to be executed
    def enqueue(self, targets, target=None):
        run = TestRun(self, target)
        if self.runner.is_skipped(self.get_full_name()):
            run.skip_message = "Skipped from command line"
            run.status = "skipped"
            run.__print_end_message()
        self.runs.append(run)
        self.runner.enqueue_test(run)


    # Can be called to get full name including hierarchy path
    def get_full_name(self):
        return self.full_name






    def get_stats(self, parent_stats):
        if len(self.runs) == 1:
            return self.runs[0].get_stats(parent_stats)
        else:
            self.stats = {'passed': 0, 'failed': 0, 'skipped': 0, 'excluded': 0, 'duration': 0}
            for run in self.runs:
                run.get_stats(self.stats)

            if parent_stats is not None:
                for key in parent_stats.keys():
                    parent_stats[key] += self.stats[key]


    def dump_table(self, table):
        if len(self.runs) == 1:
            self.runs[0].dump_table(table, True)
        else:
            table_dump_row(table,
                self.get_full_name(),
                '',
                self.stats['duration'],
                self.stats['passed'],
                self.stats['failed'],
                self.stats['skipped'],
                self.stats['excluded']
            )

            for run in self.runs:
                run.dump_table(table, False)


    def dump_junit(self, test_file):
        for run in self.runs:
            run.dump_junit(test_file)



class TestImpl(testsuite.Test, TestCommon):

    def __init__(self, runner, parent, name, path):
        TestCommon.__init__(self, runner, parent, name, path)
        self.runner = runner
        self.name = name

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class MakeTestImpl(testsuite.Test, TestCommon):

    def __init__(self, runner, parent, name, path, flags, checker=None, retval=0):
        TestCommon.__init__(self, runner, parent, name, path)
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


class SdkTestImpl(testsuite.SdkTest, TestCommon):

    def __init__(self, runner, parent, name, path, flags, checker=None, retval=0):
        TestCommon.__init__(self, runner, parent, name, path)
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

    def __init__(self, runner, parent, name, path, flags):
        SdkTestImpl.__init__(self, runner, parent, name, path, flags)

        self.add_command(testsuite.Shell('power_gen', 'make power_gen %s' % (self.flags)))
        self.add_command(testsuite.Shell('power_copy', 'make power_copy %s' % (self.flags)))




class TestsetImpl(testsuite.Testset):

    def __init__(self, runner, parent=None, path=None):
        self.runner = runner
        self.name = None
        self.tests = []
        self.testsets = []
        self.parent = parent
        self.path = path
        self.targets = {}

    def get_target(self, target_name):
        target = self.targets.get(target_name)
        if target is not None:
            return target
        if self.parent is not None:
            return self.parent.get_target(target_name)
        return None

    def get_path(self):
        return self.path

    def get_property(self, name):
        return self.runner.get_property(name)

    def set_name(self, name):
        self.name = name

    def add_target(self, name, config):
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
        filepath = file
        if self.path is not None:
            filepath = os.path.join(self.path, file)

        self.testsets.append(self.runner.import_testset(filepath, self))


    def new_testset(self, testset):
        print ('new testset')


    def enqueue(self, targets, target=None):

        if targets is not None and len(self.targets) > 0:
            for target_name in targets:
                target = self.targets.get(target_name)
                if target is not None:
                    for testset in self.testsets:
                        testset.enqueue(None, target)

                    for test in self.tests:
                        test.enqueue(None, target)
        else:
            for testset in self.testsets:
                testset.enqueue(targets, target)

            for test in self.tests:
                test.enqueue(targets, target)


    def new_test(self, name):
        test = TestImpl(self.runner, self, name, self.path)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test


    def new_make_test(self, name, flags='', checker=None, retval=0):
        test = MakeTestImpl(self.runner, self, name, self.path, flags, checker=checker, retval=retval)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_test(self, name, flags='', checker=None, retval=0):
        test = SdkTestImpl(self.runner, self, name, self.path, flags, checker=checker, retval=retval)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_netlist_power_test(self, name, flags=''):
        test = NetlistPowerSdkTestImpl(self.runner, self, name, self.path, flags)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test


    def dump_table(self, table):
        if self.name is not None:
            table_dump_row(table,
                self.get_full_name(),
                '',
                self.stats['duration'],
                self.stats['passed'],
                self.stats['failed'],
                self.stats['skipped'],
                self.stats['excluded']
            )

        for child in self.tests + self.testsets:
            child.dump_table(table)


    def get_stats(self, parent_stats):

        self.stats = {'passed': 0, 'failed': 0, 'skipped': 0, 'excluded': 0, 'duration': 0}

        for child in self.tests + self.testsets:
            child.get_stats(self.stats)

        if parent_stats is not None:
            for key in parent_stats.keys():
                parent_stats[key] += self.stats[key]


    def dump_junit(self, test_file):
        for child in self.testsets + self.tests:
            child.dump_junit(test_file)


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
            testset.enqueue(self.targets)

        self.check_pending_tests()

        self.event.wait()

        self.stats = {'passed': 0, 'failed': 0, 'skipped': 0, 'excluded': 0, 'duration': 0}
        for testset in self.testsets:
            testset.get_stats(self.stats)

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
        for testset in self.testsets:
            testset.dump_table(x)
        print()
        print(x)


    def dump_junit(self, report_path):
        os.makedirs(report_path, exist_ok=True)

        for testset in self.testsets:
            filename = '%s/TEST-%s.xml' % (report_path, testset.name)
            with open(filename, 'w') as test_file:
                test_file.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                test_file.write('<testsuite skipped="%d" errors="%d" failures="%d" name="%s" tests="%d" time="%f">\n' % \
                    (testset.stats['skipped'], testset.stats['failed'], testset.stats['failed'], testset.name,
                    testset.stats['failed'] + testset.stats['passed'], testset.stats['duration']))
                testset.dump_junit(test_file)
                test_file.write('</testsuite>\n')



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
        self.testsets.append(self.import_testset(file))


    def import_testset(self, file, parent=None):
        logging.debug(f"Parsing file (path: {file})")

        try:
            spec = importlib.util.spec_from_loader("module.name", SourceFileLoader("module.name", file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except FileNotFoundError as exc:
            raise RuntimeError(bcolors.FAIL + 'Unable to open test configuration file: ' + file + bcolors.ENDC)

        testset = TestsetImpl(self, parent, path=os.path.dirname(file))

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
