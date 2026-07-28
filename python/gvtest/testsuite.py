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

from __future__ import annotations

import abc
import dataclasses
from typing import Any, Callable


class Target(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def get_name(self) -> str: pass


# Allowed reference provenances: 'rtl' (measured on the RTL design),
# 'analytical' (derived from the timing model's equations), 'measured'
# (locked-in value measured on the current model — a regression lock).
REF_TYPES = ('rtl', 'analytical', 'measured')


@dataclasses.dataclass(frozen=True)
class Bench:
    """A benchmark metric extracted from a test's output.

    `extract` is a regex matched against each output line (start-anchored);
    its group(1) is the metric value. An optional reference value with an
    absolute tolerance and a mandatory type (one of REF_TYPES) turns the
    metric into a calibration point for the bench DB and calibration
    report.
    """
    name: str
    extract: str
    desc: str
    ref: float | None = None
    tol: float | None = None
    ref_type: str | None = None

    @staticmethod
    def make(
        name: str, extract: str, desc: str | None = None, *,
        ref: float | None = None,
        tol: float | None = None,
        tol_pct: float | None = None,
        ref_type: str | None = None,
    ) -> Bench:
        if tol is not None and tol_pct is not None:
            raise ValueError(
                f'bench {name}: tol and tol_pct are mutually exclusive')
        if ref is None and (tol is not None or tol_pct is not None
                            or ref_type is not None):
            raise ValueError(
                f'bench {name}: tol/tol_pct/ref_type require ref')
        if ref is not None and ref_type is None:
            raise ValueError(
                f'bench {name}: a reference requires a ref_type '
                f'(one of {", ".join(REF_TYPES)})')
        if ref_type is not None and ref_type not in REF_TYPES:
            raise ValueError(
                f'bench {name}: unknown ref_type {ref_type!r} '
                f'(allowed: {", ".join(REF_TYPES)})')
        if tol_pct is not None:
            tol = abs(ref) * tol_pct / 100
        return Bench(name=name, extract=extract,
                     desc=desc if desc is not None else name,
                     ref=ref, tol=tol, ref_type=ref_type)


class Command:
  # Optional list of named resources this command needs to hold
  # while it executes. The runner serializes commands against
  # each other through a per-resource semaphore (default
  # capacity 1). Consecutive commands in the same test listing
  # the same resource hand off the lock without releasing.
  resources: list[str] | None = None

class Shell(Command):

  def __init__(
      self, name: str, cmd: str, retval: int = 0,
      resources: list[str] | None = None
  ) -> None:
    self.name: str = name
    self.cmd: str = cmd
    self.retval: int = retval
    self.resources = list(resources) if resources else None


class Call(Command):

  def __init__(
      self, name: str, callback: Callable[[], int],
      resources: list[str] | None = None
  ) -> None:
    self.name: str = name
    self.callback: Callable[[], int] = callback
    self.resources = list(resources) if resources else None


class Checker(Command):

  def __init__(
      self, name: str, callback: Callable[..., Any],
      *kargs: Any,
      resources: list[str] | None = None,
      **kwargs: Any
  ) -> None:
    self.name: str = name
    self.callback: tuple[
        Callable[..., Any], tuple[Any, ...],
        dict[str, Any]
    ] = callback, kargs, kwargs
    self.resources = list(resources) if resources else None


class Test(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def add_bench(
        self, name: str, extract: str, desc: str | None = None, *,
        ref: float | None = None,
        tol: float | None = None,
        tol_pct: float | None = None,
        ref_type: str | None = None,
    ) -> None: pass

    @abc.abstractmethod
    def get_path(self) -> str: pass

    @abc.abstractmethod
    def add_command(self, command: Command) -> None: pass

    @abc.abstractmethod
    def depends_on(self, *tests: Test) -> None: pass

    # Note: set_components() is intentionally not declared here (abstract
    # or concrete). It lives on TestCommon only, so concrete subclasses
    # using multiple inheritance like GvrunTestImpl(SdkTest, TestCommon)
    # resolve the method via TestCommon rather than shadowing it with a
    # no-op stub earlier in the MRO.



class Testset(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def set_name(self, name: str) -> None: pass

    @abc.abstractmethod
    def get_target(self) -> Target | None: pass

    @abc.abstractmethod
    def import_testset(self, file: str) -> None: pass

    @abc.abstractmethod
    def import_pytest(
        self, path: str, pytest_exe: str = 'pytest',
        markers: str | None = None,
        pytest_args: list | None = None,
        xdist: int = 0,
        batch_timeout: int | None = None,
        strict: bool = False,
        batch_exe: str | None = None
    ) -> None: pass

    @abc.abstractmethod
    def new_testset(self, testset_name: str) -> Testset: pass

    @abc.abstractmethod
    def new_test(self, name: str) -> Test: pass

    @abc.abstractmethod
    def new_sdk_test(
        self, name: str, flags: str | None = None,
        no_clean: bool = False
    ) -> SdkTest: pass

    @abc.abstractmethod
    def new_sdk_netlist_power_test(self, name: str, flags: str | None = None) -> SdkTest: pass

    @abc.abstractmethod
    def declare_resource(self, name: str, capacity: int = 1) -> None:
        """Declare a named resource with a given capacity.

        Resources are referenced from `Command.resources` (or via
        the `build_resource=` kwarg on test factories) to serialize
        specific commands across parallel TestRuns. A resource with
        capacity N allows at most N concurrent holders. If the
        resource already exists with a different capacity, this
        raises; re-declaring with the same capacity is a no-op.
        """
        pass

    @abc.abstractmethod
    def get_property(self, name: str) -> Any: pass

    @abc.abstractmethod
    def get_platform(self) -> str | None: pass

    @abc.abstractmethod
    def get_path(self) -> str | None: pass

    # Note: set_components() lives on TestsetImpl; see Test.set_components
    # note above.



class SdkTest(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def add_bench(
        self, name: str, extract: str, desc: str | None = None, *,
        ref: float | None = None,
        tol: float | None = None,
        tol_pct: float | None = None,
        ref_type: str | None = None,
    ) -> None: pass

    # Note: set_components() lives on TestCommon; see Test.set_components
    # note above.
