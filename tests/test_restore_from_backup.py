"""
tests/test_restore_from_backup.py -- end-to-end regression for
tools/restore_from_backup.sh, added beta-1.10.23.

Seeds a scratch backup dir with synthetic signal_history/forge SQLite
snapshots + a configstate tarball, runs the restore script into a scratch
target dir with --force (bypassing the "predictor.service is active"
safety), and asserts the restored files exist at the right paths with the
right contents.

Two goals:
1. The restore script actually works end-to-end (data-integrity).
2. It's safe against the scenarios that could bite an operator running
   against a live app dir -- dry-run mode really is read-only, receipt is
   written, --timestamp point-in-time recovery picks the right snapshot,
   partial restores (--only) don't overwrite the other families.

Deliberately does NOT run against /opt/predictor -- everything is in a
tmp_path.
"""
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESTORE_TOOL = REPO_ROOT / "tools" / "restore_from_backup.sh"


def _make_sqlite(path: Path, table: str, cols: str, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE {table} ({cols})")
    conn.executemany(
        f"INSERT INTO {table} ({','.join(c.split()[0] for c in cols.split(',')[1:])}) "
        f"VALUES ({','.join(['?'] * (len(cols.split(',')) - 1))})",
        rows,
    )
    conn.commit()
    conn.close()


def _make_configstate_tarball(dest: Path, entries: dict):
    """entries: {arcname: bytes}. Writes a gzipped tar to dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for arcname, content in entries.items():
            info = tarfile.TarInfo(name=arcname)
            info.size = len(content)
            import io
            tar.addfile(info, io.BytesIO(content))


def _seed_backups(source_dir: Path, timestamp: str = "20260724-120000-000000"):
    """Populate a scratch backup directory with one snapshot of each family."""
    source_dir.mkdir(parents=True, exist_ok=True)

    _make_sqlite(
        source_dir / f"signal_history.{timestamp}.db",
        "signal_events",
        "id INTEGER PRIMARY KEY, symbol TEXT, price REAL",
        [("BTC/USDT", 60000.0), ("ETH/USDT", 3500.0)],
    )
    _make_sqlite(
        source_dir / f"forge.{timestamp}.db",
        "trades",
        "id INTEGER PRIMARY KEY, strategy_id TEXT, pnl_pct REAL",
        [("btc_baseline", 0.5), ("sol_hi_conf", -1.2)],
    )
    _make_configstate_tarball(
        source_dir / f"configstate.{timestamp}.tar.gz",
        {
            ".env": b"INTERNAL_API_TOKEN=deadbeef\n",
            "config.json": b'{"thresh": 0.5}',
            "src/config.json": b'{"thresh": 0.5}',
            "logs/crypto_operator_memory.jsonl": b'{"role":"user","msg":"hi"}\n',
            "models/metadata.json": b'{"model":"stub"}',
            "models/model_btc_long.txt": b"stub-btc-long-model",
            "models/model_eth_short.txt": b"stub-eth-short-model",
            # etc/nginx/.htpasswd deliberately omitted here -- separate test
        },
    )


def _run(*args, expect_ok=True):
    proc = subprocess.run(
        ["bash", str(RESTORE_TOOL), *args],
        capture_output=True, text=True,
    )
    if expect_ok:
        assert proc.returncode == 0, (
            f"restore tool exited {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def test_full_restore_end_to_end(tmp_path):
    src = tmp_path / "backups"
    tgt = tmp_path / "target"
    _seed_backups(src)

    _run("--source-dir", str(src), "--target-dir", str(tgt), "--force")

    # signal_history
    sh = sqlite3.connect(str(tgt / "logs" / "signal_history.db"))
    rows = sh.execute("SELECT symbol, price FROM signal_events ORDER BY id").fetchall()
    sh.close()
    assert rows == [("BTC/USDT", 60000.0), ("ETH/USDT", 3500.0)]

    # forge
    fg = sqlite3.connect(str(tgt / "forge_data" / "forge.db"))
    rows = fg.execute("SELECT strategy_id, pnl_pct FROM trades ORDER BY id").fetchall()
    fg.close()
    assert rows == [("btc_baseline", 0.5), ("sol_hi_conf", -1.2)]

    # configstate contents
    assert (tgt / ".env").read_bytes() == b"INTERNAL_API_TOKEN=deadbeef\n"
    assert (tgt / "config.json").read_bytes() == b'{"thresh": 0.5}'
    assert (tgt / "src" / "config.json").read_bytes() == b'{"thresh": 0.5}'
    assert (tgt / "logs" / "crypto_operator_memory.jsonl").read_bytes() == \
        b'{"role":"user","msg":"hi"}\n'
    assert (tgt / "models" / "metadata.json").read_bytes() == b'{"model":"stub"}'
    assert (tgt / "models" / "model_btc_long.txt").read_bytes() == b"stub-btc-long-model"
    assert (tgt / "models" / "model_eth_short.txt").read_bytes() == b"stub-eth-short-model"

    # receipt was written
    receipt = (tgt / "logs" / "restore_applied.log").read_text()
    assert "signal_history:" in receipt
    assert "forge:" in receipt
    assert "configstate:" in receipt
    assert "sha256=" in receipt


def test_dry_run_does_not_touch_target(tmp_path):
    src = tmp_path / "backups"
    tgt = tmp_path / "target"
    _seed_backups(src)

    proc = _run("--source-dir", str(src), "--target-dir", str(tgt),
                "--dry-run", "--force")

    assert not tgt.exists() or not any(tgt.iterdir()), (
        "dry-run left files in target: {list(tgt.iterdir())}"
    )
    # Plan lines still printed:
    assert "signal_history:" in proc.stdout
    assert "forge:" in proc.stdout
    assert "configstate:" in proc.stdout
    assert "DRY RUN" in proc.stdout


def test_timestamp_picks_at_or_before_cutoff(tmp_path):
    """Point-in-time recovery: with three snapshots at 10:00, 11:00, 12:00,
    a --timestamp of 20260724-110000 must pick the 11:00 snapshot, not
    12:00 (which is after) and not 10:00 (which is older-than-necessary)."""
    src = tmp_path / "backups"
    tgt = tmp_path / "target"
    src.mkdir()

    for stamp, price in [
        ("20260724-100000-000000", 60000.0),
        ("20260724-110000-000000", 61000.0),
        ("20260724-120000-000000", 62000.0),
    ]:
        _make_sqlite(
            src / f"signal_history.{stamp}.db",
            "signal_events",
            "id INTEGER PRIMARY KEY, price REAL",
            [(price,)],
        )

    _run("--source-dir", str(src), "--target-dir", str(tgt),
         "--timestamp", "20260724-110000", "--force",
         "--only", "signal_history")

    conn = sqlite3.connect(str(tgt / "logs" / "signal_history.db"))
    price = conn.execute("SELECT price FROM signal_events").fetchone()[0]
    conn.close()
    assert price == 61000.0, f"picked wrong snapshot (got price={price})"


def test_only_flag_restricts_to_one_family(tmp_path):
    """--only forge means signal_history and configstate must NOT be
    touched, even though the source dir contains all three."""
    src = tmp_path / "backups"
    tgt = tmp_path / "target"
    _seed_backups(src)

    _run("--source-dir", str(src), "--target-dir", str(tgt),
         "--force", "--only", "forge")

    assert (tgt / "forge_data" / "forge.db").exists()
    assert not (tgt / "logs" / "signal_history.db").exists()
    assert not (tgt / ".env").exists()
    assert not (tgt / "config.json").exists()


def test_missing_family_is_skipped_not_fatal(tmp_path):
    """A backup dir with only configstate (no SQLite backups) still restores
    what it has."""
    src = tmp_path / "backups"
    tgt = tmp_path / "target"
    src.mkdir()
    _make_configstate_tarball(
        src / "configstate.20260724-120000-000000.tar.gz",
        {"config.json": b'{"only_thing":"here"}'},
    )

    _run("--source-dir", str(src), "--target-dir", str(tgt), "--force")

    assert (tgt / "config.json").read_bytes() == b'{"only_thing":"here"}'
    assert not (tgt / "forge_data" / "forge.db").exists()
    assert not (tgt / "logs" / "signal_history.db").exists()


def test_empty_source_dir_exits_zero(tmp_path):
    src = tmp_path / "backups"
    tgt = tmp_path / "target"
    src.mkdir()

    proc = _run("--source-dir", str(src), "--target-dir", str(tgt), "--force")
    assert proc.returncode == 0
    assert "Nothing to restore" in proc.stdout


def test_missing_source_dir_fails(tmp_path):
    tgt = tmp_path / "target"
    proc = _run("--source-dir", str(tmp_path / "does-not-exist"),
                "--target-dir", str(tgt), "--force",
                expect_ok=False)
    assert proc.returncode != 0


def test_missing_required_args_fails(tmp_path):
    proc = _run("--target-dir", str(tmp_path / "t"), expect_ok=False)
    assert proc.returncode != 0
    assert "source-dir" in (proc.stderr + proc.stdout).lower()


def test_receipt_contains_selected_snapshot_filenames(tmp_path):
    src = tmp_path / "backups"
    tgt = tmp_path / "target"
    _seed_backups(src, timestamp="20260724-131415-987654")

    _run("--source-dir", str(src), "--target-dir", str(tgt), "--force")

    receipt = (tgt / "logs" / "restore_applied.log").read_text()
    assert "signal_history.20260724-131415-987654.db" in receipt
    assert "forge.20260724-131415-987654.db" in receipt
    assert "configstate.20260724-131415-987654.tar.gz" in receipt
