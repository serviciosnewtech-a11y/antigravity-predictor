# Non-Interactive Deploy (Hermes-friendly)

The default `install.sh` and even `hermes_deploy.sh` assume some form of
interactivity is possible — either running as root, or sudo can prompt for
a password. Hermes Agent runtimes on locked-down hosts frequently CAN'T do
either. This doc is the workaround.

## The fundamental blocker

`install.sh` writes to `/etc/systemd/system`, invokes `apt-get`, configures
`ufw`, restarts services. All require root. There's no way around that
without rearchitecting the install as a series of systemd user-scope units,
which is a much larger change.

The only sustainable non-interactive path is: **grant the deploy user
passwordless sudo, once, via `/etc/sudoers.d/`**. After that, every
subsequent deploy runs without prompts.

## One-time setup (run once as root, then never again)

Pick the user Hermes runs as (typically `luis` or `predictor`), then in a
root shell (SSH as root, or a one-time `sudo -i` interactive session):

```bash
DEPLOY_USER="luis"    # or whatever your Hermes runtime uses
echo "$DEPLOY_USER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/hermes-deploy
chmod 440 /etc/sudoers.d/hermes-deploy
visudo -c   # sanity-check syntax; must print "parsed OK"
```

Verify from the deploy user's shell:

```bash
sudo -n true && echo "PASSWORDLESS SUDO OK"
```

Only after this returns `PASSWORDLESS SUDO OK` will `deploy.sh` run
non-interactively.

## Deploy config

Copy the template and fill in values:

```bash
cp deploy/bare-metal/hermes_deploy.conf.example ~/deploy.conf
$EDITOR ~/deploy.conf
```

Required fields (all others optional):
- `TAG` — e.g. `beta-1.10.27`
- `TARBALL_PATH` — where you SCP'd the tarball (e.g. `/tmp/foo.tar.gz`)
- `TARBALL_SHA256` — from the tarball's `.sha256` file (verified before extraction)
- `APP_DIR` — install target (e.g. `/opt/predictor`)
- `APP_USER` — system user to run services (e.g. `predictor`)

## The paste-ready root-shell block

Paste this into a root shell (or any shell where the deploy user has
passwordless sudo). All output goes to `/tmp` so you can walk away — the
report will still be there when your terminal dies:

```bash
export DEBIAN_FRONTEND=noninteractive
export CONF=~/deploy.conf

# Extract deploy.sh location from the tarball if you haven't extracted yet:
TAG=$(grep '^TAG=' $CONF | cut -d= -f2 | tr -d '"')
TARBALL=$(grep '^TARBALL_PATH=' $CONF | cut -d= -f2 | tr -d '"')
tar -xzf $TARBALL -C /tmp
DEPLOY_ROOT=/tmp/antigravity-predictor-bare-metal-$TAG

# Run — writes /tmp/deploy-<TAG>-<pid>.log AND /tmp/deploy-<TAG>-report.txt
bash $DEPLOY_ROOT/deploy/bare-metal/deploy.sh $CONF
echo "Exit code: $?"
echo "Report at: /tmp/deploy-$TAG-report.txt"
```

After this returns (or after your terminal closes), Hermes can read
`/tmp/deploy-$TAG-report.txt` for a structured summary:

```
TAG=beta-1.10.27
HOST=nts1
TIMESTAMP=2026-07-25T...
APP_DIR=/opt/predictor
APP_USER=predictor
LOG=/tmp/deploy-beta-1.10.27-12345.log
INSTALL_LOG=/tmp/install-beta-1.10.27-12345.log
[BASIC_AUTH] user=predictor password=XXXX
VERIFY_FAILS=0
STATUS=deploy-ok
── verify output ──
  [PASS] 3.1 predictor.service active
  ...
```

## Post-deploy verification (any time, any state)

Standalone; doesn't touch anything, just reports:

```bash
sudo bash /opt/predictor/deploy/bare-metal/verify.sh ~/deploy.conf
echo "Exit code = number of failed checks"
```

## Rolling back

```bash
# Basic rollback: stops services, removes app dir, PRESERVES backups + htpasswd
sudo bash $DEPLOY_ROOT/deploy/bare-metal/rollback.sh ~/deploy.conf

# Full nuke: removes backups, htpasswd, and ufw allow rules too (SSH kept)
sudo bash $DEPLOY_ROOT/deploy/bare-metal/rollback.sh ~/deploy.conf \
    --wipe-backups --wipe-htpasswd --wipe-ufw
```

## What to do when Hermes reports a blocker

File a structured report using `docs/ISSUE_TEMPLATE/deploy-blocker.md`.
Fill in EVERY field — one blank field wastes a triage round.
