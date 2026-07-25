---
name: Deploy Blocker
about: Structured report when a deploy runner (Hermes Agent, CI, operator) hits an environmental blocker
title: "[DEPLOY-BLOCKER] "
labels: deploy, blocker
---

## Runtime context (fill in ALL fields — one blank field wastes a triage round)

- **host:** (hostname)
- **OS:** (`lsb_release -a` one-liner, e.g. `Linux Mint 22.3 (Ubuntu Noble)`)
- **repo tag being deployed:** (e.g. `beta-1.10.27`)
- **script attempted:** (`install.sh` | `hermes_deploy.sh` | `deploy.sh` | `verify.sh` | `rollback.sh`)
- **executor role:** (`hermes` | `ci` | `manual-operator`)

## Sudo state (the #1 root cause of deploy blockers)

- **EUID at invocation:** (`echo $EUID` — 0 means root, else the user's uid)
- **`sudo -n true` exit code:** (`sudo -n true; echo $?` — 0 = passwordless sudo works)
- **NOPASSWD sudoers.d entry present:** (`ls /etc/sudoers.d/ | grep -i hermes || echo none`)

## Environment probes

- **disk free at `$APP_DIR` parent:** (`df -h $(dirname $APP_DIR) | tail -1`)
- **ports 80/443 status:** (`ss -ltn | grep -E ':(80|443)\b' || echo free`)
- **prior artifacts in /tmp:** (`ls /tmp/deploy-*.log /tmp/install-*.log /tmp/deploy-*-report.txt 2>/dev/null || echo none`)
- **last log path** (if the runner produced one): (e.g. `/tmp/deploy-beta-1.10.27-12345.log`)

## Blocker(s)

Number each. For each: what happened, what was tried, why it failed.

1.
2.

## Requested changes (if any code/doc changes needed)

A.
B.

## Constraints on the fix

- [ ] Do NOT change the existing shipped install path (`install.sh`, `hermes_deploy.sh`) — backward compat required.
- [ ] Preflight failures should surface with a single actionable message, not a stack of retries.
- [ ] New scripts should be additive; parallel to existing paths.

## Full environment dump (paste output)

```
uname -a:
lsb_release -a:
free -h:
df -h:
sudo -n true 2>&1; echo "sudo -n exit: $?"
id:
```
