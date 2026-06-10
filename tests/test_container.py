"""
Tests for gvtest.container — container config normalization,
command construction, and target/config wiring.
"""

import json
import pytest

from gvtest.container import (
    DEFAULT_CONTAINER_ARGS,
    DEFAULT_CONTAINER_CMD,
    build_container_shell_cmd,
    normalize_container_config,
)
from gvtest.runner import Target


class TestNormalizeContainerConfig:
    """Normalization and validation of the container config."""

    def test_minimal(self):
        cfg = normalize_container_config(
            {'image': 'ubuntu:22.04'}, '/work'
        )
        assert cfg['image'] == 'ubuntu:22.04'
        assert cfg['cmd'] == DEFAULT_CONTAINER_CMD
        assert cfg['args'] == DEFAULT_CONTAINER_ARGS
        # Default volume: config dir, same-path mount
        assert cfg['volumes'] == ['/work:/work']

    def test_missing_image_raises(self):
        with pytest.raises(RuntimeError, match='image'):
            normalize_container_config({}, '/work')

    def test_non_mapping_raises(self):
        with pytest.raises(RuntimeError, match='mapping'):
            normalize_container_config('ubuntu', '/work')

    def test_custom_cmd_and_args(self):
        cfg = normalize_container_config(
            {
                'image': 'img',
                'cmd': 'docker',
                'args': ['--rm'],
            },
            '/work',
        )
        assert cfg['cmd'] == 'docker'
        assert cfg['args'] == ['--rm']

    def test_bad_args_raises(self):
        with pytest.raises(RuntimeError, match='args'):
            normalize_container_config(
                {'image': 'img', 'args': 'not-a-list'},
                '/work',
            )

    def test_bare_volume_same_path(self):
        cfg = normalize_container_config(
            {'image': 'img', 'volumes': ['/data']},
            '/work',
        )
        assert cfg['volumes'] == ['/data:/data']

    def test_relative_volume_resolved_vs_config_dir(self):
        cfg = normalize_container_config(
            {'image': 'img', 'volumes': ['../sdk']},
            '/work/tests',
        )
        assert cfg['volumes'] == ['/work/sdk:/work/sdk']

    def test_relative_volume_without_config_dir_raises(self):
        with pytest.raises(RuntimeError, match='volume'):
            normalize_container_config(
                {'image': 'img', 'volumes': ['rel']},
                None,
            )

    def test_full_volume_spec_kept(self):
        cfg = normalize_container_config(
            {'image': 'img', 'volumes': ['/h:/c:ro']},
            '/work',
        )
        assert cfg['volumes'] == ['/h:/c:ro']

    def test_no_volumes_without_config_dir(self):
        cfg = normalize_container_config(
            {'image': 'img'}, None
        )
        assert cfg['volumes'] == []

    def test_env_expansion(self, monkeypatch):
        monkeypatch.setenv('SDK_ROOT', '/opt/sdk')
        cfg = normalize_container_config(
            {
                'image': 'registry/img:${SDK_ROOT}',
                'volumes': ['${SDK_ROOT}'],
            },
            '/work',
        )
        assert cfg['image'] == 'registry/img:/opt/sdk'
        assert cfg['volumes'] == ['/opt/sdk:/opt/sdk']


class TestBuildContainerShellCmd:
    """Construction of the podman run argv."""

    @pytest.fixture
    def container(self):
        return normalize_container_config(
            {'image': 'ubuntu:22.04'}, '/work'
        )

    def test_basic_shape(self, container):
        argv, name = build_container_shell_cmd(
            container, 'make test', '/work/tests'
        )
        assert argv[0] == DEFAULT_CONTAINER_CMD
        assert argv[1] == 'run'
        assert argv[2:4] == ['--name', name]
        assert name.startswith('gvtest-')
        assert '-v' in argv
        assert argv[argv.index('-v') + 1] == '/work:/work'
        assert argv[argv.index('-w') + 1] == '/work/tests'
        assert argv[-3] == 'bash'
        assert argv[-2] == '-c'
        assert argv[-1] == 'make test'

    def test_unique_names(self, container):
        _, n1 = build_container_shell_cmd(
            container, 'true', '/work'
        )
        _, n2 = build_container_shell_cmd(
            container, 'true', '/work'
        )
        assert n1 != n2

    def test_sourceme_inlined(self, container):
        argv, _ = build_container_shell_cmd(
            container, 'make test', '/work',
            sourceme='/work/sourceme.sh',
        )
        assert argv[-1] == (
            'source /work/sourceme.sh && make test'
        )

    def test_envvars_passed_with_e(self, container):
        argv, _ = build_container_shell_cmd(
            container, 'true', '/work',
            envvars={'FOO': 'bar', 'X': 'y z'},
        )
        assert 'FOO=bar' in argv
        assert argv[argv.index('FOO=bar') - 1] == '-e'
        assert 'X=y z' in argv

    def test_image_before_bash(self, container):
        argv, _ = build_container_shell_cmd(
            container, 'true', '/work'
        )
        assert argv[-4] == 'ubuntu:22.04'


class TestTargetContainer:
    """Target.get_container() wiring."""

    def test_no_container(self):
        t = Target('rv64')
        assert t.get_container() is None

    def test_with_container(self):
        config = json.dumps(
            {'container': {'image': 'img'}}
        )
        t = Target('rv64', config)
        t.config_dir = '/work'
        cfg = t.get_container()
        assert cfg is not None
        assert cfg['image'] == 'img'
        assert cfg['volumes'] == ['/work:/work']
