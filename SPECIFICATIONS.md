# GVTEST Specifications

## Overview

**gvtest** is a Python-based test framework designed for running and managing test suites for the GVSOC simulator. It provides a comprehensive infrastructure for defining, executing, organizing, and reporting test results across multiple configurations and targets.

## Project Information

- **License**: Apache License 2.0
- **Copyright**: ETH Zurich, University of Bologna
- **Language**: Python 3
- **Primary Dependencies**: 
  - `psutil` - Process and system utilities
  - `prettytable` - Table formatting
  - `rich` - Rich terminal output with progress bars and tables

## Architecture

### Core Components

The project is structured around three main Python modules:

1. **`__main__.py`** - Command-line interface and entry point
2. **`runner.py`** - Test execution engine and orchestration (~1,129 lines)
3. **`testsuite.py`** - Abstract base classes defining the test framework API

### Directory Structure

```
gvtest/
├── bin/
│   ├── gvtest              # Main executable wrapper script
│   └── gvtest_cmd_stub     # Helper script for sourcing environment files
├── python/
│   └── gvtest/
│       ├── __main__.py     # CLI entry point
│       ├── runner.py       # Test runner implementation
│       └── testsuite.py    # API definitions
├── CMakeLists.txt          # Build configuration
└── requirements.txt        # Python dependencies
```

## Hierarchical Configuration System

### Overview

gvtest implements a hierarchical configuration file discovery and merging mechanism inspired by pytest. When a testset is loaded, gvtest automatically searches for configuration files starting from the testset's directory and traversing up the directory tree to the filesystem root. All discovered configuration files are merged together, with child (closer to testset) configurations extending parent (higher in the tree) configurations. Each testset gets its own set of python_paths based on its location in the directory hierarchy.

**Important**: The python_paths are added to `sys.path` only during the testset loading phase (when `testset_build()` is called) and are removed immediately after to maintain isolation between testsets. All imports must happen during this phase. Imported modules remain available via Python's module cache (`sys.modules`).

This approach enables:

- **Per-project configurations**: Each test project can have its own config file
- **Multi-project support**: Upper-level projects can aggregate multiple sub-projects with shared defaults
- **Configuration inheritance**: Child project configs inherit and override parent configs
- **Automatic discovery**: Configs are automatically found without explicit specification
- **Simplified usage**: Fewer command-line arguments needed for common scenarios

### Configuration File Name

The configuration file must be named `gvtest.yaml`.

### Discovery Algorithm

When a testset is loaded, gvtest searches for configuration files using the following algorithm:

1. **Start**: Begin in the testset file's directory
2. **Search**: Look for `gvtest.yaml` in the current directory
3. **Collect**: If found, add it to the collection
4. **Traverse**: Move up one directory level (parent directory)
5. **Repeat**: Continue steps 2-4 until reaching the filesystem root
6. **Order**: Return all found `gvtest.yaml` files in hierarchical order (root → testset)
7. **Apply**: Add all python_paths to sys.path before loading the testset module
8. **Load**: Import and execute the testset module (calling `testset_build()`)
9. **Cleanup**: Remove the added paths from sys.path to maintain isolation

**Example Directory Hierarchy:**

```
/home/user/
├── gvtest.yaml              # Root workspace config (found 3rd, applied 1st)
└── projects/
    ├── gvtest.yaml          # Project-level config (found 2nd, applied 2nd)
    └── my-chip/
        ├── gvtest.yaml      # Chip-specific config (found 1st, applied 3rd)
        └── tests/
            └── basic/
                └── testset.cfg  # ← Testset location determines config discovery
```

**Discovery Order**: 
1. `/home/user/projects/my-chip/gvtest.yaml` (found first)
2. `/home/user/projects/gvtest.yaml` (found second)
3. `/home/user/gvtest.yaml` (found third)

**Application Order** (merging):
1. `/home/user/gvtest.yaml` (base)
2. `/home/user/projects/gvtest.yaml` (merged on top)
3. `/home/user/projects/my-chip/gvtest.yaml` (merged last, has highest priority)

### Configuration Merging Strategy

After discovery, configuration files are merged in hierarchical order (root → leaf):

1. **Base**: Start with the topmost (root-level) configuration
2. **Extend**: Each child configuration's directories are appended to the parent's directories
3. **Order Preserved**: Directories from parent configs come first, then child config directories

**Merge Rules:**

- **Directory Lists**: Child directories are appended to parent directories (order: root → leaf)
- The resulting list maintains the hierarchical order, allowing more specific (child) paths to override more general (parent) paths when Python searches `sys.path`

**Example:**

Parent config (`/home/user/gvtest.yaml`):
```yaml
python_paths:
  - /opt/gvsoc/python
  - /usr/share/gvsoc/python
```

Child config (`/home/user/projects/my-chip/gvtest.yaml`):
```yaml
python_paths:
  - ./python
  - ./lib
```

**Merged Result:**
```yaml
python_paths:
  - /opt/gvsoc/python         # From parent (root)
  - /usr/share/gvsoc/python   # From parent (root)
  - ./python                  # From child
  - ./lib                     # From child
```

All these directories are temporarily appended to `sys.path` when the testset is loaded, allowing the testset to import Python packages from any of these locations during the `testset_build()` call. After loading, the paths are removed to prevent them from affecting other testsets. Each testset's python_paths are determined by the testset's location in the directory hierarchy.

### Configuration File Format

The `gvtest.yaml` file contains a simple list of directories to be added to Python's `sys.path` when a testset in that directory hierarchy is loaded. This allows testsets to import Python packages from these directories.

#### YAML Format

```yaml
# gvtest.yaml

# Directories to add to sys.path (for Python package imports)
python_paths:
  - /opt/gvsoc/python
  - /usr/share/gvsoc/python
  - ./python
  - ./lib
  - ../common/python
```

**Notes:**
- Paths can be absolute or relative
- Relative paths are resolved relative to the directory containing the `gvtest.yaml` file
- All paths from all discovered `gvtest.yaml` files are merged (parent directories first, then child directories)
- The merged paths are temporarily appended to `sys.path` before loading each testset
- Each testset gets paths based on its own location in the directory hierarchy
- Paths are removed after testset loading to maintain isolation between testsets
- All imports must happen during `testset_build()` execution

### Configuration Schema

| Key | Type | Description |
|-----|------|-------------|
| `python_paths` | array of strings | List of directories to add to `sys.path` |

**Example:**
```yaml
python_paths:
  - /absolute/path/to/python/modules
  - ./relative/path/to/modules
  - ../shared/python
```

### Configuration Validation

The configuration loader validates:

- **File syntax**: YAML parsing errors
- **Schema compliance**: Correct structure (top-level `python_paths` key with array value)
- **Type checking**: All items in `python_paths` must be strings
- **Path existence**: Warning if directories don't exist (but not an error)

**Validation Errors** are reported with:
- File path and line number (when available)
- Clear error message describing the issue

### Configuration Per Testset

Configuration discovery and loading happens **per testset** based on the testset's location:

- Testset at `/workspace/project1/tests/testset.cfg` uses configs from `/workspace/` and `/workspace/project1/`
- Testset at `/workspace/project2/tests/testset.cfg` uses configs from `/workspace/` and `/workspace/project2/`
- Each testset only sees python_paths relevant to its location in the directory hierarchy
- Paths are added to `sys.path` only during testset loading and removed afterward

This ensures that:
- Projects remain isolated from each other
- Shared workspace-level utilities are available to all projects
- Each project can have its own specific dependencies
- Testsets cannot accidentally access modules from unrelated projects

### Import Timing Requirements

**Critical**: All Python imports from configured paths must happen during the `testset_build()` function execution:

```python
# testset.cfg

# ✓ CORRECT - Import at module level (executed when testset loads)
from my_project_lib import test_helpers
from shared_utils import validation

def testset_build(testset):
    # ✓ CORRECT - Import inside testset_build
    from my_project_lib import specific_tool
    
    testset.set_name('my_tests')
    test = testset.new_test('test_1')
    test.add_command(Shell('run', './app'))

# ✗ INCORRECT - Import in callback executed later
def my_callback(test):
    # This will fail - paths are no longer in sys.path
    from my_project_lib import late_import  # ImportError!
```

Once the testset is loaded, the configured paths are removed from `sys.path`. However, modules already imported remain available through Python's module cache, so they can be used in callbacks and test execution.

### Configuration Precedence

**Path Merging Order** (for `python_paths`):

All discovered `gvtest.yaml` files are merged in hierarchical order:
1. Root-level config directories are added first
2. Each level down adds its directories to the end
3. Result: `sys.path` contains all directories from root → leaf order

This ensures that more specific (child) packages can override more general (parent) packages when Python searches for imports.

### Multi-Project Setup Examples

#### Workspace Level Configuration

**File**: `/workspace/gvtest.yaml`

```yaml
# Organization-wide Python paths
python_paths:
  - /opt/company/shared-python
  - /usr/local/lib/gvsoc/python
```

#### Project Level Configuration

**File**: `/workspace/chips/gvtest.yaml`

```yaml
# Chip family Python modules
python_paths:
  - ./common/python
  - ./shared/lib
```

#### Component Level Configuration

**File**: `/workspace/chips/chip_a/gvtest.yaml`

```yaml
# Chip A specific Python modules
python_paths:
  - ./python
  - ./testlibs
```

**Usage with testset at** `/workspace/chips/chip_a/tests/testset.cfg`:

```bash
# Run the testset
gvtest --testset /workspace/chips/chip_a/tests/testset.cfg

# When loading this testset, sys.path will have (in order):
# 1. /opt/company/shared-python           (from workspace)
# 2. /usr/local/lib/gvsoc/python          (from workspace)
# 3. /workspace/chips/common/python       (from project, resolved)
# 4. /workspace/chips/shared/lib          (from project, resolved)
# 5. /workspace/chips/chip_a/python       (from component, resolved)
# 6. /workspace/chips/chip_a/testlibs     (from component, resolved)

# The testset can import from any of these locations during testset_build()
```

**Important**: After the testset loads, these paths are removed from `sys.path` to maintain isolation. A different testset in `/workspace/chips/chip_b/` would get different paths based on its own hierarchy.

### Benefits of Hierarchical Configuration

1. **Code Organization**: Python packages organized alongside projects
2. **Shared Libraries**: Common test utilities at workspace level available to all testsets
3. **Project-Specific Modules**: Each project brings its own Python packages
4. **No Explicit Path Management**: Testsets automatically have access to packages in their hierarchy
5. **Hierarchical Override**: More specific packages naturally override general ones
6. **Strong Project Isolation**: Each testset only sees python_paths from its own hierarchy during loading
7. **Clean Separation**: Each project directory is self-contained
8. **No Cross-contamination**: Paths from one testset don't leak to other testsets

## Functionality

### 1. Test Organization

gvtest uses a hierarchical test organization model:

- **Testsets**: Top-level containers that can import other testsets
- **Tests**: Individual test cases within a testset
- **Targets**: Different configurations/platforms for which tests can run
- **Commands**: Actions to execute within a test (Shell commands, Python callbacks, checkers)

### 2. Test Definition

Tests are defined in configuration files (typically `testset.cfg`) using a Python DSL:

```python
def testset_build(testset):
    testset.set_name('my_tests')
    testset.add_target('target_name')
    
    test = testset.new_test('test_name')
    test.add_command(Shell('run', 'command to execute'))
```

#### Command Types

- **Shell**: Execute shell commands
  - `Shell(name, cmd, retval=0)`
- **Call**: Execute Python callbacks
  - `Call(name, callback)`
- **Checker**: Execute validation callbacks
  - `Checker(name, callback, *kargs, **kwargs)`

### 3. Test Execution

The runner provides parallel test execution with:

- **Multi-threading**: Configurable number of worker threads (`--threads`)
- **Load balancing**: Respects system load average (`--load-average`)
- **Timeout management**: Per-test timeout limits (`--max-timeout`)
- **Process management**: Graceful process termination and cleanup
- **Environment isolation**: Support for target-specific environment variables

### 4. Test Status

Each test can have one of the following statuses:

- **passed**: Test completed successfully
- **failed**: Test failed (non-zero return code or timeout)
- **skipped**: Test was skipped based on conditions
- **excluded**: Test was excluded from execution

### 5. Reporting

gvtest provides multiple reporting formats:

#### Table Report
Visual table showing test results with:
- Test names and configurations
- Duration
- Pass/fail counts
- Skipped and excluded counts
- Color-coded output (green for pass, red for fail, etc.)

#### Summary Report
Aggregate statistics across all tests

#### JUnit XML Report
Standard JUnit XML format for CI/CD integration:
- Per-testset XML files
- Compatible with Jenkins, GitLab CI, and other tools
- Located in `junit-reports/` by default

### 6. Benchmark Support

The framework can extract benchmark results from test output:

- **Pattern matching**: `add_bench(name, extract, desc)` declares a regex
  whose group(1) is extracted from stdout as the metric value
- **References**: optional `ref=`, `tol=`/`tol_pct=` record a reference
  value and tolerance next to each measured value; a reference requires a
  `ref_type=` describing what it is — one of `rtl`, `analytical`,
  `measured` (validated against `testsuite.REF_TYPES`)
- **SQLite export**: `--bench-db FILE` stores results per run/test/target
- **Reports**: `python -m gvtest.bench.report` (trends over time) and
  `python -m gvtest.bench.calibration` (measured vs reference)

### 7. Command Line Interface

#### Available Commands

- **`tests`**: List all tests in the testset
- **`run`**: Execute the tests
- **`table`**: Display results in table format
- **`summary`**: Show summary statistics
- **`junit`**: Generate JUnit XML reports
- **`all`**: Execute run, table, summary, and junit commands

#### Key Options

| Option | Description |
|--------|-------------|
| `--testset PATH` | Path to testset configuration file |
| `--target TARGETS` | Specify target platform(s) to test |
| `--config CONFIG` | Configuration name (default: 'default') |
| `--threads THREADS` | Number of worker threads (0 = auto) |
| `--load-average LOAD` | Target system load (0.0-1.0) |
| `--stdout` | Stream test output to stdout in real-time |
| `--safe-stdout` | Dump test output after test completion |
| `--max-timeout MAX` | Maximum test timeout in seconds |
| `--test TEST_LIST` | Run specific tests (can be repeated) |
| `--skip TEST_SKIP_LIST` | Skip specific tests |
| `--verbose` | Enable verbose logging |
| `--dump-all` | Report all tests (not just failed ones) |
| `--no-fail` | Exit with error code if any test fails |

### 8. Environment Management

#### Target-Specific Environments

Targets can specify:
- **`sourceme`**: Script to source before running tests
- **`envvars`**: Environment variables to set
- **`properties`**: Custom properties accessible in test definitions

The `gvtest_cmd_stub` helper script sources environment files before executing commands.

### 9. Test Filtering

Multiple mechanisms for filtering tests:

- **By test name**: `--test test_name`
- **By target**: `--target target_name`
- **By command**: `--cmd command_name` or `--cmd-exclude command_name`
- **By skip list**: `--skip test_name`
- **By flags**: `--flags flag_name`

### 10. Output Control

- **Max output length**: Limit test output size (`--max-output-len`)
- **Real-time output**: Stream output during execution (`--stdout`)
- **Buffered output**: Show output after completion (`--safe-stdout`)
- **Progress indicators**: Rich terminal UI with progress bars
- **Color coding**: Terminal colors for different test states

## Integration with GVSOC

gvtest is specifically designed to work with GVSOC simulator tests:

- Tests execute `gvsoc` commands with various targets
- Supports multiple RISC-V and PULP platform targets
- Integrates with SDK test infrastructure
- Can run netlist and power simulation tests

Example platforms supported:
- rv64 (RISC-V 64-bit)
- pulp-open
- spatz
- snitch
- occamy
- siracusa
- mempool

## Usage Examples

### Basic Usage

```bash
# Run tests with auto-discovered config and testset
gvtest

# Run specific testset
gvtest --testset path/to/testset.cfg

# Run with 4 worker threads (overrides config)
gvtest --threads 4

# Run specific test
gvtest --test my_test_name

# Show all tests without running
gvtest tests
```

### Advanced Usage

```bash
# Run tests for specific target with custom config
gvtest --target rv64 --config debug

# Generate JUnit report only
gvtest junit --junit-report-path ./reports

# Run with benchmark extraction into the bench DB
gvtest --bench-db bench.sqlite

# Run with verbose output and no failure tolerance
gvtest --verbose --no-fail --stdout
```

### Multi-Project Workflow

```bash
# Project structure:
# /workspace/
# ├── gvtest.yaml           # Workspace Python paths
# └── chips/
#     ├── gvtest.yaml       # Chip family Python paths
#     ├── chip_a/
#     │   ├── gvtest.yaml   # Chip A Python paths
#     │   └── tests/
#     │       └── testset.cfg
#     └── chip_b/
#         ├── gvtest.yaml   # Chip B Python paths
#         └── tests/
#             └── testset.cfg

# Run chip_a tests - uses workspace + chips + chip_a configs
gvtest --testset /workspace/chips/chip_a/tests/testset.cfg

# Run chip_b tests - uses workspace + chips + chip_b configs
gvtest --testset /workspace/chips/chip_b/tests/testset.cfg

# Each testset gets python_paths based on its location:
# - chip_a testset can import from chip_a_lib but not chip_b_lib
# - chip_b testset can import from chip_b_lib but not chip_a_lib
# - Both can import from workspace-level and chips-level shared modules
```

## API for Test Developers

### Testset Definition

```python
from gvtest import *

def testset_build(testset):
    # Set testset name
    testset.set_name('my_testset')
    
    # Add target configurations
    testset.add_target('target_name', config_json)
    
    # Import other testsets
    testset.import_testset(file='other/testset.cfg')
    
    # Create nested testset
    testset.new_testset(name)
    
    # Create new test
    test = testset.new_test('test_name')
    
    # Create SDK test (convenience method)
    testset.new_sdk_test('sdk_test', flags='--flag')
    
    # Access properties
    prop = testset.get_property('property_name')
    platform = testset.get_platform()
```

### Test Configuration

```python
# Create test
test = testset.new_test('my_test')

# Add shell command
test.add_command(Shell('step1', 'echo "Hello"'))

# Add Python callback
test.add_command(Call('step2', my_callback_function))

# Add checker
test.add_command(Checker('validate', validation_func, arg1, arg2))

# Add benchmark extraction (optionally with a reference for calibration)
test.add_bench('cycles', r'Cycles: (\d+)', 'CPU cycles count')
test.add_bench('cycles', r'Cycles: (\d+)', ref=100, tol_pct=5, ref_type='rtl')
```

## Installation

The project uses CMake for installation:

```bash
cmake -S . -B build
cmake --install build --prefix /path/to/install
```

This installs:
- Executables to `bin/`
- Python modules to `python/`

## Technical Details

### Concurrency Model

- Uses Python threading for parallel execution
- Queue-based work distribution
- Process monitoring with `psutil`
- Timeout handling with threading timers
- Graceful shutdown with signal handling

### Process Management

- Spawns subprocesses for shell commands
- Captures stdout/stderr (merged stream)
- Kills process trees on timeout
- Handles Unicode decoding errors gracefully

### Output Formatting

Uses the `rich` library for:
- Progress bars during test execution
- Formatted tables for results
- Color-coded status messages
- Tree views for hierarchical test organization

### Statistics Collection

Tracks at multiple levels:
- Per-test run statistics
- Per-test aggregates (across targets)
- Per-testset aggregates
- Global statistics

## Design Patterns

1. **Abstract Factory**: `Testset` creates `Test` objects
2. **Command Pattern**: `Command` hierarchy for test actions
3. **Observer Pattern**: Statistics collection
4. **Template Method**: Test execution flow
5. **Strategy Pattern**: Different reporting formats

## Extensibility

The framework is designed for extension:

- Abstract base classes in `testsuite.py` define contracts
- Callback mechanisms for custom test logic
- Property system for configuration
- Plugin-style testset imports
- Customizable benchmark extraction patterns

## Limitations and Constraints

- Python 3 required
- Shell commands execute in test-specific working directory
- Each tool invocation spawns fresh shell (no state persistence)
- Output limited to stdout/stderr (no GUI interaction)
- Timeout applies to entire test, not individual commands

## Conclusion

gvtest provides a robust, feature-rich test execution framework tailored for hardware simulation and embedded systems development. Its hierarchical organization, parallel execution, comprehensive reporting, and extensible architecture make it well-suited for managing complex test suites in CI/CD environments.
