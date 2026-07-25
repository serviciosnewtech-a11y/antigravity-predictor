---
name: Deploy Blocker
about: Structured report when a deploy runner (Hermes Agent, CI, operator) hits an environmental blocker
title: "[DEPLOY-BLOCKER] "
labels: deploy, blocker
---

## Context

- **Target host:** (hostname, OS, version — e.g. NTS1, Linux Mint 22.3 Ubuntu Noble-based)
- **Repo:** https://github.com/serviciosnewtech-a11y/antigravity-predictor.git
- **Tag being deployed:** (e.g. `beta-1.10.27`)
- **Deploy runner:** (Hermes Agent / manual operator / CI job)
- **Script attempted:** (`install.sh` / `hermes_deploy.sh` / `deploy.sh`)

## Blockers

Number each. For each: what happened, what was tried, why it failed.

1.
2.

## What was verified anyway

Environmental facts confirmed BEFORE the blocker fired. Helps triage.

- OS check:
- Disk free:
- Ports 80/443 status:
- Sudo available:
- Tarball present + sha256 match:

## Requested changes

If the fix requires code/doc changes, list them here with rationale.

A.
B.
C.

## Constraints for the fix

- [ ] Do NOT change the existing shipped install path (`install.sh`, `hermes_deploy.sh`) — backward compat is required.
- [ ] Preflight failures should surface with a single actionable message, not a stack of retries.
- [ ] New scripts should be additive; parallel to existing paths, not replacements.

## Environment info dump

```
uname -a:
lsb_release -a:
free -h:
df -h:
sudo -n true 2>&1; echo "sudo -n exit: $?"
```
