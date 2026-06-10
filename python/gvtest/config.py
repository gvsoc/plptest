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
Hierarchical configuration file loader for gvtest.

This module implements pytest-style configuration discovery and merging.
It searches for gvtest.yaml files from the current directory up to the
filesystem root, merges them hierarchically, and adds the specified
python_paths to sys.path.
"""

from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from typing import List, Dict, Optional

try:
    import yaml
except ImportError:
    yaml = None


logger = logging.getLogger(__name__)


class ConfigLoader:
    """Loads and merges hierarchical gvtest.yaml configuration files."""
    
    CONFIG_FILENAME = 'gvtest.yaml'
    
    def __init__(self, start_dir: Optional[str] = None):
        """
        Initialize the configuration loader.
        
        Args:
            start_dir: Starting directory for config discovery. 
                      Defaults to current working directory.
        """
        self.start_dir = Path(start_dir or os.getcwd()).resolve()
        self.config_files: List[Path] = []
        self.python_paths: List[str] = []
    
    def discover_configs(self) -> List[Path]:
        """
        Discover all gvtest.yaml files from start_dir to filesystem root.
        
        Returns:
            List of Path objects in hierarchical order (root → leaf).
        """
        configs = []
        current = self.start_dir
        
        logger.debug(f"Starting config discovery from: {current}")
        
        # Traverse up to filesystem root
        while True:
            config_file = current / self.CONFIG_FILENAME
            
            if config_file.exists() and config_file.is_file():
                logger.debug(f"Found config file: {config_file}")
                configs.append(config_file)
            
            # Check if we've reached the root
            parent = current.parent
            if parent == current:
                break
            current = parent
        
        # Reverse to get root → leaf order
        configs.reverse()
        
        logger.debug(f"Discovered {len(configs)} config file(s)")
        return configs
    
    def load_config(self, config_file: Path) -> Dict:
        """
        Load and parse a single gvtest.yaml file.
        
        Args:
            config_file: Path to the config file.
            
        Returns:
            Dictionary containing the parsed configuration.
            
        Raises:
            RuntimeError: If YAML module is not available or parsing fails.
        """
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required to load gvtest.yaml files. "
                "Install it with: pip install pyyaml"
            )
        
        try:
            with open(config_file, 'r') as f:
                config = yaml.safe_load(f)
            
            if config is None:
                logger.warning(f"Config file is empty: {config_file}")
                return {}
            
            if not isinstance(config, dict):
                raise RuntimeError(
                    f"Invalid config format in {config_file}: "
                    f"expected a dictionary, got {type(config).__name__}"
                )
            
            logger.debug(f"Loaded config from: {config_file}")
            return config
            
        except yaml.YAMLError as e:
            raise RuntimeError(
                f"Failed to parse YAML file {config_file}: {e}"
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to load config file {config_file}: {e}"
            )
    
    def validate_config(self, config: Dict, config_file: Path) -> None:
        """
        Validate the structure of a configuration dictionary.
        
        Args:
            config: Configuration dictionary to validate.
            config_file: Path to the config file (for error messages).
            
        Raises:
            RuntimeError: If validation fails.
        """
        if not config:
            return  # Empty config is valid
        
        # Check for unknown keys
        known_keys = {'python_paths', 'targets'}
        unknown_keys = set(config.keys()) - known_keys
        if unknown_keys:
            logger.warning(
                f"Unknown keys in {config_file}: {', '.join(unknown_keys)}"
            )
        
        # Validate python_paths if present
        if 'python_paths' in config:
            python_paths = config['python_paths']
            
            if not isinstance(python_paths, list):
                raise RuntimeError(
                    f"Invalid 'python_paths' in {config_file}: "
                    f"expected a list, got {type(python_paths).__name__}"
                )
            
            for i, path in enumerate(python_paths):
                if not isinstance(path, str):
                    raise RuntimeError(
                        f"Invalid path at index {i} in {config_file}: "
                        f"expected a string, got {type(path).__name__}"
                    )
        
        # Validate targets if present
        if 'targets' in config:
            targets = config['targets']
            if not isinstance(targets, dict):
                raise RuntimeError(
                    f"Invalid 'targets' in {config_file}: "
                    f"expected a mapping, got "
                    f"{type(targets).__name__}"
                )
            for name, target_cfg in targets.items():
                if not isinstance(name, str):
                    raise RuntimeError(
                        f"Invalid target name in "
                        f"{config_file}: "
                        f"expected a string, got "
                        f"{type(name).__name__}"
                    )
                if target_cfg is not None and \
                        not isinstance(target_cfg, dict):
                    raise RuntimeError(
                        f"Invalid config for target "
                        f"'{name}' in {config_file}: "
                        f"expected a mapping or null, got "
                        f"{type(target_cfg).__name__}"
                    )
                if target_cfg is not None and \
                        'container' in target_cfg:
                    self._validate_container(
                        target_cfg['container'], name,
                        config_file
                    )

    def _validate_container(
        self, container, target_name: str,
        config_file: Path
    ) -> None:
        """Validate a target's ``container`` sub-config."""
        prefix = (
            f"Invalid 'container' config for target "
            f"'{target_name}' in {config_file}: "
        )
        if not isinstance(container, dict):
            raise RuntimeError(
                prefix + "expected a mapping, got "
                f"{type(container).__name__}"
            )
        image = container.get('image')
        if not isinstance(image, str) or image == '':
            raise RuntimeError(
                prefix + "'image' is required and must be "
                "a string"
            )
        if 'cmd' in container and \
                not isinstance(container['cmd'], str):
            raise RuntimeError(
                prefix + "'cmd' must be a string"
            )
        for key in ('args', 'volumes'):
            value = container.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or \
                    not all(isinstance(v, str)
                            for v in value):
                raise RuntimeError(
                    prefix + f"'{key}' must be a list "
                    "of strings"
                )
    
    def resolve_paths(self, paths: List[str], config_file: Path) -> List[str]:
        """
        Resolve relative paths relative to the config file directory.
        
        Args:
            paths: List of paths (absolute or relative).
            config_file: Path to the config file.
            
        Returns:
            List of resolved absolute paths.
        """
        resolved = []
        config_dir = config_file.parent
        
        for path in paths:
            if os.path.isabs(path):
                resolved_path = path
            else:
                # Resolve relative to config file directory
                resolved_path = str((config_dir / path).resolve())
            
            # Check if path exists (warning only)
            if not os.path.exists(resolved_path):
                logger.warning(
                    f"Path does not exist (from {config_file}): {resolved_path}"
                )
            elif not os.path.isdir(resolved_path):
                logger.warning(
                    f"Path is not a directory (from {config_file}): {resolved_path}"
                )
            
            resolved.append(resolved_path)
        
        return resolved
    
    def resolve_targets(
        self, config_files: List[Path]
    ) -> Dict[str, Dict]:
        """
        Resolve target definitions with scoping semantics.
        
        The most specific (leaf-most) config that defines a
        ``targets`` section determines which targets apply.
        If no config defines targets, returns empty dict.
        
        Target properties are inherited: a parent config
        defines the base properties, and a child config's
        targets section restricts which targets are active
        while inheriting/overriding their properties.
        
        Args:
            config_files: Config files in root → leaf order.
            
        Returns:
            Dict mapping target name to its config dict.
        """
        # First pass: collect all target definitions
        # from all levels (root → leaf)
        all_defined: Dict[str, Dict] = {}
        # Track which config was the last to define targets
        last_targets_file: Optional[Path] = None
        last_targets_names: Optional[List[str]] = None

        for config_file in config_files:
            try:
                config = self.load_config(config_file)
                self.validate_config(config, config_file)
                
                if 'targets' in config:
                    last_targets_file = config_file
                    last_targets_names = []
                    for name, target_cfg in \
                            config['targets'].items():
                        if target_cfg is None:
                            target_cfg = {}
                        else:
                            # Anchor relative sourceme paths to
                            # the YAML that defined them. Each
                            # property belongs to its own source
                            # file — without this, an inherited
                            # sourceme from a parent YAML would
                            # later be resolved against the leaf
                            # YAML's directory.
                            target_cfg = dict(target_cfg)
                            sm = target_cfg.get('sourceme')
                            if (isinstance(sm, str)
                                    and '${' not in sm
                                    and not os.path.isabs(sm)):
                                target_cfg['sourceme'] = str(
                                    (config_file.parent / sm)
                                    .resolve()
                                )
                        if name in all_defined:
                            all_defined[name].update(
                                target_cfg
                            )
                        else:
                            all_defined[name] = dict(
                                target_cfg
                            )
                        last_targets_names.append(name)
                        logger.debug(
                            f"Target '{name}' from "
                            f"{config_file}"
                        )
            except Exception as e:
                logger.error(
                    f"Error processing {config_file}: {e}"
                )
                raise

        if last_targets_names is None:
            # No config defined targets at all
            return {}

        # Return only the targets named in the most
        # specific (leaf) config that has a targets section
        result: Dict[str, Dict] = {}
        for name in last_targets_names:
            result[name] = all_defined[name]

        # Store the directory of the config that defined
        # the targets, for resolving relative paths
        self._targets_config_dir: str | None = (
            str(last_targets_file.parent)
            if last_targets_file is not None else None
        )
        return result
    
    def get_targets(self) -> Dict[str, Dict]:
        """
        Discover and resolve target definitions from configs.
        
        Returns:
            Dict mapping target name to its config dict.
        """
        if not self.config_files:
            self.config_files = self.discover_configs()
        return self.resolve_targets(self.config_files)
    
    def merge_configs(self, config_files: List[Path]) -> List[str]:
        """
        Load and merge all config files, collecting python_paths.
        
        Args:
            config_files: List of config files in hierarchical order (root → leaf).
            
        Returns:
            Merged list of python paths.
        """
        all_paths = []
        
        for config_file in config_files:
            try:
                config = self.load_config(config_file)
                self.validate_config(config, config_file)
                
                if 'python_paths' in config:
                    paths = config['python_paths']
                    resolved_paths = self.resolve_paths(paths, config_file)
                    all_paths.extend(resolved_paths)
                    logger.debug(
                        f"Added {len(resolved_paths)} path(s) from {config_file}"
                    )
            
            except Exception as e:
                logger.error(f"Error processing {config_file}: {e}")
                raise
        
        return all_paths
    
    def apply_to_sys_path(self, paths: List[str]) -> None:
        """
        Add paths to sys.path, avoiding duplicates.
        
        Args:
            paths: List of paths to add to sys.path.
        """
        added_count = 0
        
        for path in paths:
            if path not in sys.path:
                sys.path.append(path)
                added_count += 1
                logger.debug(f"Added to sys.path: {path}")
            else:
                logger.debug(f"Already in sys.path: {path}")
        
        logger.info(f"Added {added_count} path(s) to sys.path")
    
    def get_python_paths(self) -> List[str]:
        """
        Discover, load, and merge configurations without applying to sys.path.
        
        Returns:
            List of resolved python paths from merged configurations.
        """
        # Discover config files
        self.config_files = self.discover_configs()
        
        if not self.config_files:
            logger.debug("No gvtest.yaml files found")
            return []
        
        logger.debug(f"Found {len(self.config_files)} config file(s):")
        for config_file in self.config_files:
            logger.debug(f"  - {config_file}")
        
        # Merge configurations
        self.python_paths = self.merge_configs(self.config_files)
        
        if not self.python_paths:
            logger.debug("No python_paths specified in config files")
            return []
        
        return self.python_paths

    def load_and_apply(self) -> int:
        """
        Discover, load, merge, and apply configurations.
        
        This is the main entry point for loading configurations.
        
        Returns:
            Number of paths added to sys.path.
        """
        python_paths = self.get_python_paths()
        
        if not python_paths:
            return 0
        
        logger.info(f"Found {len(self.config_files)} config file(s):")
        for config_file in self.config_files:
            logger.info(f"  - {config_file}")
        
        # Apply to sys.path
        initial_len = len(sys.path)
        self.apply_to_sys_path(python_paths)
        return len(sys.path) - initial_len


def get_python_paths_for_dir(directory: str) -> List[str]:
    """
    Get python paths for a specific directory without modifying sys.path.
    
    This function discovers and merges gvtest.yaml files from the specified
    directory up to the filesystem root, returning the merged python_paths.
    
    Args:
        directory: Directory to start config discovery from.
    
    Returns:
        List of resolved python paths.
    """
    loader = ConfigLoader(directory)
    return loader.get_python_paths()


def load_and_apply_config(start_dir: Optional[str] = None) -> int:
    """
    Convenience function to load and apply gvtest configurations.
    
    Args:
        start_dir: Starting directory for config discovery.
                  Defaults to current working directory.
    
    Returns:
        Number of paths added to sys.path.
    """
    loader = ConfigLoader(start_dir)
    return loader.load_and_apply()