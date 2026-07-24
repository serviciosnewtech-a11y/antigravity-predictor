"""
tests/conftest.py — project-level pytest fixtures shared across the suite.

Kept intentionally small. Only fixtures that are (a) used by more than one
test file and (b) fiddly enough that duplicating them invites drift belong
here — otherwise a fixture should live next to the test that uses it.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def forge_db(monkeypatch):
    """Isolated forge SQLite DB in a fresh tempdir per test.

    Sets FORGE_DATA_DIR to a temp path, wipes any cached forge.* modules
    from sys.modules, then reimports forge.db so its module-level DB_PATH
    resolves against the fresh env. Also invalidates forge.scoring since
    it holds a reference to forge.db functions.

    Returns the reloaded db module. init_db() is NOT called — leave that
    to the test so it can inspect the pre-init state if desired.

    Usage:
        def test_thing(forge_db):
            forge_db.init_db()
            ...
    """
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setenv("FORGE_DATA_DIR", str(tmp))

    # Drop every forge.* submodule; the fresh import below rebuilds them
    # against the tempdir path. Order-independent (dict copy first, then
    # delete).
    for mod_name in [m for m in list(sys.modules) if m == "forge" or m.startswith("forge.")]:
        del sys.modules[mod_name]

    from forge import db as db_mod
    importlib.reload(db_mod)
    return db_mod
