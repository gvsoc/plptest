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


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'



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
        self.duration = 0
        self.current_proc = None
        self.lock = threading.Lock()

        self.full_name = self.name

        if self.parent is not None:
            parent_name = self.parent.get_full_name()
            if parent_name is not None:
                self.full_name =  f'{parent_name}:{self.name}'

        self.runner.declare_name(self.full_name)
        self.benchs = []


    # Called by user to add commands
    def add_command(self, command):
        self.commands.append(command)


    # Called by runner to enqueue this test to the list of tests ready to be executed
    def enqueue(self):
        if self.runner.is_skipped(self.get_full_name()):
            self.skip_message = "Skipped from command line"
            self.status = "skipped"
            self.__print_end_message()
        else:
            self.runner.enqueue_test(self)


    # Can be called to get full name including hierarchy path
    def get_full_name(self):
        return self.full_name


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

        for command in self.commands:
            retval = self.__exec_command(command)

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

        for bench in self.benchs:
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


    # Print start bannier
    def __print_start_message(self):
        testname = self.get_full_name().ljust(self.runner.get_max_testname_len() + 5)
        config = self.runner.get_config()
        print (bcolors.OKBLUE + 'START'.ljust(8) + bcolors.ENDC + bcolors.BOLD + testname + bcolors.ENDC + ' %s' % (config))
        sys.stdout.flush()


    # Print end bannier
    def __print_end_message(self):
        testname = self.get_full_name().ljust(self.runner.get_max_testname_len() + 5)
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
            cwd=self.path)

        self.current_proc = proc

        self.lock.release()

        for line in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors='replace'):
            self.__dump_test_msg(line)

        retval = proc.poll()
        self.current_proc = None

        return retval


    def __dump_test_msg(self, msg):
        self.output += msg
        if self.runner.stdout:
            print (msg[:-1])


    # Called by run method to execute specific command
    def __exec_command(self, command):

        if type(command) == testsuite.Shell:
            self.__dump_test_msg(f'--- Shell command: {command.cmd} ---\n')
            retval = self.__exec_process(command.cmd)

        elif type(command) == testsuite.Checker:
            self.__dump_test_msg(f'--- Checker command ---\n')
            result = command.callback(self.output)
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


    def get_stats(self, parent_stats):
        self.stats = {'passed': 0, 'failed': 0, 'skipped': 0, 'excluded': 0, 'duration': 0}
        self.stats[self.status] += 1
        self.stats['duration'] = self.duration

        if parent_stats is not None:
            for key in parent_stats.keys():
                parent_stats[key] += self.stats[key]


    def dump_table(self, table):
        total = self.stats['passed'] + self.stats['failed']

        table.add_row([
            self.get_full_name(), self.runner.config, self.duration, '%d/%d' % (self.stats['passed'], total), self.stats['failed'],
            self.stats['skipped'], self.stats['excluded']
        ])


    def dump_junit(self, test_file):
        if self.status != 'excluded':
            test_file.write('  <testcase classname="%s" name="%s" time="%f">\n' % (self.runner.config, self.get_full_name(), self.duration))
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



class TestImpl(testsuite.Test, TestCommon):

    def __init__(self, runner, parent, name, path):
        TestCommon.__init__(self, runner, parent, name, path)
        self.runner = runner
        self.name = name

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])



class SdkTestImpl(testsuite.SdkTest, TestCommon):

    def __init__(self, runner, parent, name, path, flags):
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

        self.flags += f'--build=build/{self.name}'

        self.add_command(testsuite.Shell('clean', 'posbuild clean %s' % (self.flags)))
        self.add_command(testsuite.Shell('all', 'posbuild build run %s' % (self.flags)))

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

    def get_property(self, name):
        return self.runner.get_property(name)

    def set_name(self, name):
        self.name = name


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


    def enqueue(self):
        for testset in self.testsets:
            testset.enqueue()

        for test in self.tests:
            test.enqueue()


    def new_test(self, name):
        test = TestImpl(self.runner, self, name, self.path)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test


    def new_sdk_test(self, name, flags=''):
        test = SdkTestImpl(self.runner, self, name, self.path, flags)
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
            total = self.stats['passed'] + self.stats['failed']

            table.add_row([
                self.get_full_name(), self.runner.config, 0, '%d/%d' % (self.stats['passed'], total), self.stats['failed'],
                self.stats['skipped'], self.stats['excluded']
            ])

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
            flags=None, bench_csv_file=None, bench_regexp=None):
        self.nb_threads = nb_threads
        self.queue = queue.Queue()
        self.testsets = []
        self.pending_tests = []
        self.nb_running_tests = 0
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
            testset.enqueue()

        self.lock.acquire()
        self.check_pending_tests()
        self.lock.release()

        self.event.wait()

        for testset in self.testsets:
            testset.get_stats(None)

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
        while len(self.pending_tests) > 0 and len(self.pending_tests) > 0 and self.check_cpu_load():
            test = self.pending_tests.pop()
            self.queue.put(test)


    def check_cpu_load(self):
        if self.nb_running_tests >= self.nb_threads:
            return False

        return psutil.cpu_percent(interval=0.1) < self.load_average * 100


    def get_max_testname_len(self):
        return self.max_testname_len


    def terminate(self, test):
        self.lock.acquire()

        self.nb_pending_tests -= 1

        if test in self.pending_tests:
            self.pending_tests.remove(test)

        if self.nb_pending_tests == 0:
            self.event.set()
        else:
            self.check_pending_tests()

        self.lock.release()

    def register_bench_result(self, name, value, desc):
        self.bench_results[name] = [value, desc]