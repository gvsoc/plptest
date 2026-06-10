#!/usr/bin/env python3

#
# Copyright (C) 2023 ETH Zurich, University of Bologna
#     and GreenWaves Technologies
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.
#

"""
Target configuration — platform targets with properties,
env vars, and sourceme scripts.

Env var expansion: Use ``${VAR}`` in sourceme and envvars
values to expand environment variables at runtime.
Missing variables expand to an empty string.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


_ENV_VAR_RE = re.compile(r'\$\{([^}]+)\}')


def _expand_env(value: str) -> str:
    """Replace all ``${VAR}`` references with their env value."""
    return _ENV_VAR_RE.sub(
        lambda m: os.environ.get(m.group(1), ''), value
    )


class Target(object):

    def __init__(
        self, name: str, config: str | None = None
    ) -> None:
        self.name: str = name
        if config is None:
            config = '{}'
        self.config: dict[str, Any] = json.loads(config)
        self.config_dir: str | None = None

    @classmethod
    def from_dict(
        cls, name: str, config: dict[str, Any]
    ) -> Target:
        """Create a Target from a YAML-parsed dict."""
        t = cls.__new__(cls)
        t.name = name
        t.config = dict(config)
        t.config_dir: str | None = None
        return t

    def get_name(self) -> str:
        return self.name

    def get_sourceme(self) -> str | None:
        sourceme = self.config.get('sourceme')
        if sourceme is not None:
            sourceme = _expand_env(sourceme)
            if not os.path.isabs(sourceme):
                config_dir = getattr(
                    self, 'config_dir', None
                )
                if config_dir is not None:
                    sourceme = os.path.join(
                        config_dir, sourceme
                    )
            return sourceme
        return None

    def get_envvars(self) -> dict[str, str] | None:
        envvars = self.config.get('envvars')
        if envvars is not None:
            result: dict[str, str] = {}
            for key, value in envvars.items():
                expanded = _expand_env(value)
                result[key] = expanded
            return result
        return None

    def get_container(self) -> dict | None:
        """Return the normalized container config, or None.

        Declared per target in gvtest.yaml under the
        ``container`` key; when set, every test command of
        this target runs inside that container.
        """
        cfg = self.config.get('container')
        if cfg is None:
            return None
        from gvtest.container import normalize_container_config
        return normalize_container_config(
            cfg, getattr(self, 'config_dir', None)
        )

    def format_properties(self, str: str) -> str:
        properties = self.config.get('properties')
        if properties is None:
            return str
        return str.format(**properties)

    def get_property(self, name: str) -> Any | None:
        properties = self.config.get('properties')
        if properties is None:
            return None
        return properties.get(name)
