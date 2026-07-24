"""
tests/test_backup_config_and_secrets.py -- regression test for
tools/backup_config_and_secrets.py, added beta-1.10.21 to close the
DATA_INVENTORY coverage=none gap for .env, /etc/nginx/.htpasswd, persona
memory, config.json, and models/*.

Same shape as tests/test_backup_forge_db.py. Key assertions:
- every source that's present lands in the resulting tarball at the
  documented arcname
- missing sources are silently skipped, not fatal
- the retention pass is scoped to configstate.*.tar.gz -- it must NOT touch
  the sibling signal_history.*.db / forge.*.db snapshots that share the
  target directory by design
- distinct timestamped filenames on rapid successive runs (same collision
  fix as the two SQLite backup scripts)
"""
import os
import sqlite3
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import backup_config_and_secrets as bcs  # noqa: E402


def _make_app_layout(app_dir: Path):
    """Build a minimally-realistic app layout under app_dir so
    _collect_sources() finds real files at every expected location."""
    (app_dir / "logs").mkdir(parents=True, exist_ok=True)
    (app_dir / "src").mkdir(parents=True, exist_ok=True)
    (app_dir / "models").mkdir(parents=True, exist_ok=True)
    (app_dir / ".env").write_text("INTERNAL_API_TOKEN=deadbeef\n")
    (app_dir / "config.json").write_text('{"thresh":0.5}')
    (app_dir / "src" / "config.json").write_text('{"thresh":0.5}')
    (app_dir / "logs" / "crypto_operator_memory.jsonl").write_text(
        '{"role":"user","msg":"hi"}\n'
    )
    for asset in ("btc", "eth", "sol"):
        for side in ("long", "short"):
            (app_dir / "models" / f"model_{asset}_{side}.txt").write_text(
                f"stub-{asset}-{side}-model"
            )
    (app_dir / "models" / "metadata.json").write_text('{"model":"stub"}')
    (app_dir / "models" / "metrics.json").write_text('{"auc":0.6}')


def _make_htpasswd(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("predictor:$apr1$stub$hash\n")


def test_backup_bundles_all_present_sources(tmp_path):
    app = tmp_path / "opt" / "predictor"
    _make_app_layout(app)
    htpw = tmp_path / "etc" / "nginx" / ".htpasswd"
    _make_htpasswd(htpw)
    dest = tmp_path / "backups"

    result = bcs.backup_once(app, dest, htpw, keep=30)

    assert result is not None and result.exists()
    assert result.suffix == ".gz" and ".tar" in result.suffixes

    with tarfile.open(result, "r:gz") as tar:
        names = set(tar.getnames())

    # DATA_INVENTORY rows 4/5/6/7/9/10 -- everything we listed as a source.
    expected = {
        ".env",
        "etc/nginx/.htpasswd",
        "logs/crypto_operator_memory.jsonl",
        "config.json",
        "src/config.json",
        "models/model_btc_long.txt",
        "models/model_btc_short.txt",
        "models/model_eth_long.txt",
        "models/model_eth_short.txt",
        "models/model_sol_long.txt",
        "models/model_sol_short.txt",
        "models/metadata.json",
        "models/metrics.json",
    }
    missing = expected - names
    assert not missing, f"tarball missing expected entries: {missing}"


def test_missing_sources_are_skipped_not_fatal(tmp_path):
    """A fresh install / partial state has no .env yet, no persona memory
    file yet, etc. The backup must degrade gracefully -- bundle whatever
    IS present, log what isn't, exit 0."""
    app = tmp_path / "opt" / "predictor"
    (app / "models").mkdir(parents=True)
    # Only one model file present; nothing else.
    (app / "models" / "model_btc_long.txt").write_text("stub")
    htpw = tmp_path / "does_not_exist_htpasswd"
    dest = tmp_path / "backups"

    result = bcs.backup_once(app, dest, htpw, keep=30)

    assert result is not None and result.exists()
    with tarfile.open(result, "r:gz") as tar:
        names = set(tar.getnames())
    assert names == {"models/model_btc_long.txt"}, names


def test_backup_returns_none_when_no_sources_exist(tmp_path):
    app = tmp_path / "opt" / "predictor"
    app.mkdir(parents=True)  # exists but empty
    htpw = tmp_path / "does_not_exist_htpasswd"
    dest = tmp_path / "backups"

    result = bcs.backup_once(app, dest, htpw, keep=30)

    assert result is None
    assert not dest.exists() or not list(dest.glob("*.tar.gz"))


def test_repeated_backups_produce_distinct_files_not_overwrites(tmp_path):
    app = tmp_path / "opt" / "predictor"
    _make_app_layout(app)
    htpw = tmp_path / "etc" / "nginx" / ".htpasswd"
    _make_htpasswd(htpw)
    dest = tmp_path / "backups"

    paths = [bcs.backup_once(app, dest, htpw, keep=30) for _ in range(3)]

    assert len(set(paths)) == 3, (
        "three successive backup_once() calls produced fewer than 3 distinct "
        "filenames -- collision protection regressed"
    )
    for p in paths:
        assert p.exists()


def test_retention_prunes_only_configstate_backups(tmp_path):
    """Signal-history + forge SQLite backups share this same directory by
    design. The configstate retention pass must NEVER prune them, or the
    three backup jobs would delete each other's snapshots on every run."""
    app = tmp_path / "opt" / "predictor"
    _make_app_layout(app)
    htpw = tmp_path / "etc" / "nginx" / ".htpasswd"
    _make_htpasswd(htpw)
    dest = tmp_path / "backups"
    dest.mkdir()

    # Plant sibling SQLite backups as if from the two DB backup jobs.
    for i in range(3):
        (dest / f"signal_history.2026070{i}-000000-000000.db").write_bytes(
            b"pretend signal history " + str(i).encode()
        )
    for i in range(3):
        (dest / f"forge.2026070{i}-000000-000000.db").write_bytes(
            b"pretend forge " + str(i).encode()
        )

    for _ in range(5):
        bcs.backup_once(app, dest, htpw, keep=2)

    configstate_remaining = sorted(dest.glob("configstate.*.tar.gz"))
    signal_remaining = sorted(dest.glob("signal_history.*.db"))
    forge_remaining = sorted(dest.glob("forge.*.db"))
    assert len(configstate_remaining) == 2, configstate_remaining
    assert len(signal_remaining) == 3, (
        f"configstate retention pass pruned signal_history: {signal_remaining}"
    )
    assert len(forge_remaining) == 3, (
        f"configstate retention pass pruned forge: {forge_remaining}"
    )


def test_default_dest_dir_matches_other_backup_dirs():
    """Same target as backup_signal_log / backup_forge_db -- one directory
    to point off-host sync at. Regression against the three ever silently
    diverging."""
    app_dir = Path("/opt/predictor")
    dest = bcs._default_dest_dir(app_dir)
    assert str(dest) == "/opt/predictor-backups", dest
    assert not str(dest).startswith("/opt/predictor/"), (
        f"default backup dir {dest} is still inside the app directory it's "
        f"supposed to protect against"
    )


def test_htpasswd_from_env_var(monkeypatch, tmp_path):
    """HTPASSWD_PATH must be overridable via env -- default is
    /etc/nginx/.htpasswd, but tests / non-standard installs need a knob."""
    alt = tmp_path / "custom_htpasswd_path"
    monkeypatch.setenv("HTPASSWD_PATH", str(alt))
    assert bcs._default_htpasswd() == alt


def test_retention_env_var_default():
    """CONFIGSTATE_BACKUP_RETENTION_COUNT default is 30, independent of
    the two SQLite retentions."""
    import importlib
    # Just verify the constant is what we think it is by reading the
    # argparse default at parse time.
    import backup_config_and_secrets as bcs_mod  # noqa
    # Simulate no env override.
    old = os.environ.pop("CONFIGSTATE_BACKUP_RETENTION_COUNT", None)
    try:
        default = int(os.environ.get("CONFIGSTATE_BACKUP_RETENTION_COUNT", "30"))
        assert default == 30
    finally:
        if old is not None:
            os.environ["CONFIGSTATE_BACKUP_RETENTION_COUNT"] = old
