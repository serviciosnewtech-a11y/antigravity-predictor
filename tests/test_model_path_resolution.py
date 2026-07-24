"""
tests/test_model_path_resolution.py — regression test for the exact bug found
during a real bare-metal beta-1.9 install: predictor.service sets
WorkingDirectory=<APP_DIR>/src (so local imports like `import signal_log`
resolve without a package install step), but config.json's model_long_path/
model_short_path (e.g. "models/model_btc_long.txt") are repo-root-relative
strings that used to be opened raw against cwd. That only ever worked by
coincidence in Docker (WORKDIR=/app is the repo root there) -- on bare-metal
it silently looked for src/models/... instead of the real models/... one
level up, and predictor.service crash-looped (exit status 3) on every fresh
install as a result.

predictor_server.resolve_model_path() must return the correct file
regardless of the caller's cwd.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("lightgbm")  # predictor_server imports it at module scope

import predictor_server as ps  # noqa: E402


def test_resolves_correctly_regardless_of_cwd(tmp_path, monkeypatch):
    # Simulate predictor.service's actual WorkingDirectory=<APP_DIR>/src by
    # chdir-ing into an unrelated directory -- resolution must not depend on
    # cwd at all, only on this module's own __file__ location (or MODELS_DIR).
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODELS_DIR", raising=False)

    resolved = ps.resolve_model_path("models/model_btc_long.txt")

    assert os.path.isabs(resolved)
    assert os.path.exists(resolved), f"resolved path does not exist: {resolved}"
    assert resolved.endswith(os.path.join("models", "model_btc_long.txt"))


def test_models_dir_env_override_takes_priority(tmp_path, monkeypatch):
    fake_models_dir = tmp_path / "custom_models"
    fake_models_dir.mkdir()
    fake_model = fake_models_dir / "model_btc_long.txt"
    fake_model.write_text("fake booster contents")

    monkeypatch.setenv("MODELS_DIR", str(fake_models_dir))
    # Reload so the module-level _MODELS_DIR picks up the new env var, same
    # as a real process restart would.
    import importlib
    importlib.reload(ps)

    resolved = ps.resolve_model_path("models/model_btc_long.txt")
    assert resolved == str(fake_model)

    # Restore for any tests that run after this one in the same session.
    monkeypatch.delenv("MODELS_DIR", raising=False)
    importlib.reload(ps)


def test_falls_back_to_raw_path_if_nothing_found(monkeypatch):
    monkeypatch.delenv("MODELS_DIR", raising=False)
    resolved = ps.resolve_model_path("models/definitely_does_not_exist.txt")
    # Neither MODELS_DIR nor the __file__-relative default has this file --
    # falls back to returning the cfg value unchanged rather than raising,
    # so the caller's own error message (LightGBMError) stays informative.
    assert resolved == "models/definitely_does_not_exist.txt"
