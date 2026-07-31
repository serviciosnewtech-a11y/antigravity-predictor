# Antigravity Predictor — Release Index

**Canonical location for every release artifact.** One directory per tag.
Naming: `antigravity-predictor-<variant>-<TAG>.tar.gz` plus a `.sha256` sidecar.

Nothing is a release until it lives here with a checksum. `dist/` inside the repo is
build scratch only, is gitignored, and is emptied after every packaging run.

---

## CURRENT — deploy this

| | |
|---|---|
| **Tag** | `v1.11.1` |
| **File** | `v1.11.1/antigravity-predictor-v1.11.1-9264607.tar.gz` |
| **SHA256** | `ddb720ae79fc89fdc9f571ed5cf11b7027e3a382169c9e642863b56f025d0803` |
| **Commit** | `9264607` on `main` |
| **Models** | H-13 set, 65 features, `internal_count=49053` |
| **Config** | H-13 thresholds; **no** `execution` block |
| **Install** | `tools/bootstrap.sh` — no root, no sudo, no apt, no systemd |

Verified: checksum matches, executable bits preserved (`100755` on `bootstrap.sh` and
`run.sh`), all six boosters at 49,053 rows, `bash -n` clean.

Deploy:

```bash
scp v1.11.1/antigravity-predictor-v1.11.1-9264607.tar.gz{,.sha256} USER@TARGET:~/
# on target:
tar xzf antigravity-predictor-v1.11.1-9264607.tar.gz \
  antigravity-predictor-v1.11.1/tools/bootstrap.sh --strip-components=2
./bootstrap.sh ~/antigravity-predictor-v1.11.1-9264607.tar.gz
cd ~/antigravity-predictor && ./run.sh
```

---

## QUARANTINED — do not deploy

**`v1.11.0/`** — retracted. Contains 998-row mirrored boosters and the withdrawn
`signal_threshold: 0.56` execution config. Retained as evidence only; the analysis is in
`docs/GATE_C_VERDICT.md` and `docs/GATE_C_FINAL.md`. Source is preserved on branch
`quarantine/v1.11.0`.

---

## SUPERSEDED — build intermediates, safe to delete

**`_superseded/`** — v1.11.1 tarballs built from `ac1432d`, `dacb3bc`, `7b39003`, `56d98d1`, and `f9c4ba0` during iteration.
Never deployed anywhere. Superseded by `9264607`. Delete whenever.

---

## Convention

```
releases/antigravity-predictor/
  <TAG>/
    antigravity-predictor-<variant>-<TAG>.tar.gz
    antigravity-predictor-<variant>-<TAG>.tar.gz.sha256
```

Variants seen historically: `bare-metal`, `docker`, `monolith`. From v1.11.1 the variant
suffix is dropped — there is one artifact, and the install path is chosen by which script
you run inside it.

Rules:

1. Every tarball has a `.sha256` sidecar in the same directory. No exceptions.
2. Tarballs are never committed to the repo. `*.tar.gz`, `*.tar.gz.sha256` and `dist/` are
   gitignored.
3. `git archive` from a committed HEAD only — never `tar` a working tree.
4. Never resolve a version by lexical tag sort. `beta-1.11` sorts after `beta-1.10.32` but
   is chronologically older; that has already caused stacked retries once.

---

## Housekeeping candidates

Not touched, needs a human decision:

| Path | Size | Note |
|---|---|---|
| `beta-1.1/antigravity-predictor-beta1.1.tar.gz` | **192 MB** | Almost certainly packaged a venv or dataset by mistake. 130× larger than its neighbours. |
| `../../products/predictor_deploy.tar.gz` | **68 MB** | Outside this tree, 2026-07-17, unreferenced. |
| `../../products/antigravity-predictor-20260718-194551.tar.gz` | 5 MB | Outside this tree, unreferenced. |
| `beta-1.2` … `beta-1.10.15` | ~1.4 MB each | Pre-H-13 models. Historical only. |

Removing the first two recovers roughly 260 MB of the 322 MB this tree occupies.
