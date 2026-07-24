"""
tests/test_sync_backups_offsite.py -- regression test for
tools/sync_backups_offsite.py, added beta-1.10.22.

The whole point of this script is that OFFSITE_BACKUP_CMD is a seam, not a
hardcoded destination -- so the tests focus on the seam's contract:

- Unset OFFSITE_BACKUP_CMD -> exit 0 with a clear "not configured" log.
- Set OFFSITE_BACKUP_CMD to a successful command -> exit 0.
- Set OFFSITE_BACKUP_CMD to a failing command -> propagate its exit code.
- Parsing is argv-style (shlex.split), NOT shell -- verify that a command
  containing shell metacharacters gets parsed as literal argv rather than
  interpreted by /bin/sh (injection-safe by design).
- Missing BACKUP_DIR -> exit 0 (nothing to push yet; expected on a fresh
  install before any local backup timer has run).
- The child process's env receives BACKUP_DIR (so custom wrapper scripts
  can read it there).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import sync_backups_offsite as sbo  # noqa: E402


def _make_backup_dir(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    (d / "signal_history.stub.db").write_bytes(b"stub")
    return d


def test_unset_env_exits_zero_and_skips(tmp_path, capsys):
    backup_dir = _make_backup_dir(tmp_path)
    code = sbo.run_once(backup_dir, cmd_str="")
    assert code == 0
    out = capsys.readouterr().out
    assert "not configured" in out.lower() or "skipping" in out.lower()


def test_whitespace_only_env_treated_as_unset(tmp_path, capsys):
    backup_dir = _make_backup_dir(tmp_path)
    code = sbo.run_once(backup_dir, cmd_str="   \t  ")
    assert code == 0


def test_successful_command_returns_zero(tmp_path):
    backup_dir = _make_backup_dir(tmp_path)
    # `true` always exits 0 -- most portable "successful command" available.
    code = sbo.run_once(backup_dir, cmd_str="/bin/true")
    assert code == 0


def test_failing_command_propagates_exit_code(tmp_path):
    backup_dir = _make_backup_dir(tmp_path)
    # `false` always exits 1.
    code = sbo.run_once(backup_dir, cmd_str="/bin/false")
    assert code == 1


def test_specific_nonzero_exit_code_propagates(tmp_path):
    backup_dir = _make_backup_dir(tmp_path)
    code = sbo.run_once(
        backup_dir, cmd_str="/bin/sh -c 'exit 42'"
    )
    assert code == 42


def test_missing_command_returns_127(tmp_path):
    backup_dir = _make_backup_dir(tmp_path)
    code = sbo.run_once(
        backup_dir, cmd_str="/nonexistent/binary-nobody-would-name-this"
    )
    assert code == 127


def test_missing_backup_dir_skips_gracefully(tmp_path):
    """Fresh install before any local backup timer has fired -- nothing to
    push. Must exit 0, not error, or the timer will fail-loop."""
    missing = tmp_path / "does-not-exist"
    # Even with OFFSITE_BACKUP_CMD set, if there's nothing to push we skip.
    code = sbo.run_once(missing, cmd_str="/bin/true")
    assert code == 0


def test_shell_metacharacters_are_not_interpreted(tmp_path):
    """The whole design promise is: pass argv, not a shell string. If we
    ever regress to shell=True, a malicious/misconfigured OFFSITE_BACKUP_CMD
    could execute arbitrary side-effect commands via `;` or `&&`. Guard
    against that by trying to trigger a side effect via metacharacters and
    asserting the side effect never happens.

    We use a marker file the "malicious" side of the command would create
    if the shell parsed our string. With shell=False + shlex.split, the
    metacharacters become literal argv entries, `/bin/true` gets extra
    args it ignores, and nothing gets written."""
    backup_dir = _make_backup_dir(tmp_path)
    marker = tmp_path / "shell_metachar_side_effect"
    # If this string were shell-interpreted, `touch <marker>` would run
    # after true. With shlex.split -> argv, it's just extra args to /bin/true.
    malicious = f"/bin/true ; touch {marker}"

    code = sbo.run_once(backup_dir, cmd_str=malicious)
    # true ignores extra args and exits 0
    assert code == 0
    assert not marker.exists(), (
        f"shell metacharacters were interpreted -- {marker} was created, "
        f"meaning subprocess ran via a shell instead of as argv. This is a "
        f"real injection vulnerability if it regresses."
    )


def test_backup_dir_exposed_to_child_env(tmp_path):
    """BACKUP_DIR must appear in the child process's env so custom wrapper
    scripts can read it there. Verify by having the child print $BACKUP_DIR
    into a marker file we then read back."""
    backup_dir = _make_backup_dir(tmp_path)
    marker = tmp_path / "child_env_capture"
    # Use sh -c with a single-quoted script so the parent doesn't expand it
    # -- the child shell dereferences $BACKUP_DIR from ITS env, which is
    # what sync_backups_offsite.py should have populated.
    cmd = f"/bin/sh -c 'echo -n \"$BACKUP_DIR\" > {marker}'"
    code = sbo.run_once(backup_dir, cmd_str=cmd)
    assert code == 0
    assert marker.exists(), "child did not run"
    assert marker.read_text() == str(backup_dir), (
        f"child saw BACKUP_DIR={marker.read_text()!r}, expected {str(backup_dir)!r}"
    )


def test_default_backup_dir_matches_local_backup_dirs():
    """Regression against BACKUP_DIR default silently diverging from the
    three local backup scripts (backup_signal_log, backup_forge_db,
    backup_config_and_secrets)."""
    d = sbo._resolve_backup_dir()
    assert str(d) == "/opt/predictor-backups", d


def test_env_var_override_for_backup_dir(monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", "/some/other/backups")
    d = sbo._resolve_backup_dir()
    assert str(d) == "/some/other/backups", d
