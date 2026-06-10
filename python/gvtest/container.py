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
Container execution support — run test commands inside a
container (podman/docker) declared per target in gvtest.yaml:

    targets:
      my_target:
        container:
          image: ghcr.io/org/image:tag
          cmd: podman                # optional
          args: ['--rm', ...]        # optional, replaces defaults
          volumes:                   # optional
            - /abs/host/path         # same-path mount
            - rel/path               # resolved vs the yaml dir
            - /host:/cont:opts       # full spec

The default volume (when none is given) mounts the directory of
the gvtest.yaml that defined the target at the identical path
inside the container, so no path translation is needed: test
cwd, node ids and report paths are valid on both sides.
"""

from __future__ import annotations

import os
import subprocess
import uuid

from gvtest.targets import _expand_env


DEFAULT_CONTAINER_CMD = 'podman'
DEFAULT_CONTAINER_ARGS = ['--rm', '--security-opt', 'label=disable']


def _resolve_volume(volume: str, config_dir: str | None) -> str:
    """Normalize a volume spec to 'host:container[:opts]'.

    A bare path becomes a same-path mount. Relative host paths
    are resolved against the gvtest.yaml directory.
    """
    volume = _expand_env(volume)
    parts = volume.split(':')
    host = parts[0]
    if not os.path.isabs(host):
        if config_dir is None:
            raise RuntimeError(
                f"Relative container volume '{volume}' "
                "cannot be resolved (no config directory)"
            )
        host = os.path.abspath(
            os.path.join(config_dir, host)
        )
    rest = parts[1:]
    if not rest:
        rest = [host]
    return ':'.join([host] + rest)


def normalize_container_config(
    cfg: dict, config_dir: str | None
) -> dict:
    """Validate and normalize a target's container config.

    Returns a dict with keys: image, cmd, args, volumes —
    volumes fully resolved to 'host:container[:opts]' specs.
    """
    if not isinstance(cfg, dict):
        raise RuntimeError(
            "Invalid 'container' config: expected a mapping, "
            f"got {type(cfg).__name__}"
        )

    image = cfg.get('image')
    if not isinstance(image, str) or image == '':
        raise RuntimeError(
            "Invalid 'container' config: 'image' is required "
            "and must be a string"
        )

    cmd = cfg.get('cmd', DEFAULT_CONTAINER_CMD)
    if not isinstance(cmd, str):
        raise RuntimeError(
            "Invalid 'container' config: 'cmd' must be a string"
        )

    args = cfg.get('args', list(DEFAULT_CONTAINER_ARGS))
    if not isinstance(args, list) or \
            not all(isinstance(a, str) for a in args):
        raise RuntimeError(
            "Invalid 'container' config: 'args' must be a "
            "list of strings"
        )

    volumes = cfg.get('volumes')
    if volumes is None:
        volumes = [config_dir] if config_dir is not None else []
    if not isinstance(volumes, list) or \
            not all(isinstance(v, str) for v in volumes):
        raise RuntimeError(
            "Invalid 'container' config: 'volumes' must be a "
            "list of strings"
        )

    return {
        'image': _expand_env(image),
        'cmd': cmd,
        'args': [_expand_env(a) for a in args],
        'volumes': [
            _resolve_volume(v, config_dir) for v in volumes
        ],
    }


def build_container_shell_cmd(
    container: dict, inner_cmd: str, cwd: str,
    sourceme: str | None = None,
    envvars: dict[str, str] | None = None
) -> tuple[list[str], str]:
    """Build the argv to run a shell command in a container.

    The container is named so it can be force-removed on
    timeout/interrupt. sourceme is inlined into the bash -c
    payload (gvtest_cmd_stub may not exist in the image) and
    envvars are passed with -e (the host environment does not
    cross the container boundary).

    Returns (argv, container_name).
    """
    name = f'gvtest-{uuid.uuid4().hex[:12]}'

    payload = inner_cmd
    if sourceme is not None:
        payload = f'source {sourceme} && {payload}'

    argv = [container['cmd'], 'run', '--name', name]
    argv += container['args']
    for volume in container['volumes']:
        argv += ['-v', volume]
    for key, value in (envvars or {}).items():
        argv += ['-e', f'{key}={value}']
    argv += ['-w', cwd, container['image'],
             'bash', '-c', payload]

    return argv, name


def kill_container(cmd: str, name: str) -> None:
    """Force-remove a named container, ignoring errors."""
    try:
        subprocess.run(
            [cmd, 'rm', '-f', '-t', '0', name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        pass
