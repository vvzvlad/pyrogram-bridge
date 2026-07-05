"""Shared test bootstrap (issue #17).

Ensure the repo root is importable and install the config mock BEFORE any test
module imports application code. Pytest imports conftest.py before collecting
test modules, so doing this here (instead of a per-module preamble) makes the
suite order-independent regardless of collection order or invocation directory.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tests.mock_config as _mock_config

sys.modules['config'] = _mock_config
