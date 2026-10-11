"""Shared test bootstrap.

Ensure the repo root is importable and install the config mock BEFORE any test
module imports application code. Pytest imports conftest.py before collecting
test modules, so doing this here (instead of a per-module preamble) makes the
suite order-independent regardless of collection order or invocation directory.
"""
import os
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tests.mock_config as _mock_config

sys.modules['config'] = _mock_config

# The media DB path is ./data/media_file_ids.db, and ./data of a working copy holds the bridge's
# real state. api_server and post_parser import DB_PATH by value, so point it at a throwaway
# directory before any test module imports them; the directory is removed when the run ends.
import file_io

_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix='pyrogram-bridge-tests-')
file_io.DB_PATH = os.path.join(_TEST_DB_DIR.name, 'media_file_ids.db')

# The Telegram session directory is a constant (telegram_client.SESSION_PATH = "data"). Point it
# at the test data directory before any test module builds a TelegramClient, so a test run
# from a working copy never touches the real session in ./data.
import telegram_client

telegram_client.SESSION_PATH = os.path.join(ROOT, 'tests', 'test_data')

# Pin the runner timezone to UTC (golden determinism).
# Naive kurigram dates are interpreted by datetime.timestamp() in the LOCAL tz, so
# without this pin RSS <pubDate> values drift between machines (this sandbox is MSK).
os.environ['TZ'] = 'UTC'
time.tzset()

import pytest


@pytest.fixture(autouse=True)
def _reset_media_mime_cache():
    """Clear the process-lifetime MIME dict before each test.

    ``api_server._mime_types`` persists for the whole process, so an entry populated by one
    test would otherwise leak into another and mask a get/magic call the next test asserts on.
    """
    try:
        import api_server
        api_server._mime_types.clear()
    except Exception:
        pass
    yield
