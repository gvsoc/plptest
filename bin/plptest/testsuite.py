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

import abc


class Testset(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def set_name(self, name): pass

    @abc.abstractmethod
    def import_testset(self, file): pass

    @abc.abstractmethod
    def new_testset(self, testset): pass

    @abc.abstractmethod
    def new_test(self, name): pass

    @abc.abstractmethod
    def new_sdk_test(self, name, flags=None): pass



class Test(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def add_bench(self, extract, name, desc): pass




class SdkTest(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def add_bench(self, extract, name, desc): pass



class Shell(object):

  def __init__(self, name, cmd):
    self.name = name
    self.cmd = cmd
