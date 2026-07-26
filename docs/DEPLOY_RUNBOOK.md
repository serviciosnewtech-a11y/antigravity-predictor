# DEPLOY RUNBOOK — Linux Mint test machine

**Artifact:** `dist/antigravity-predictor-v1.11.1-dacb3bc.tar.gz`
**SHA256:** `6917b5696bdb06b7cfc216953dba00302a512fc8c020750448ecbe774e7f4570`
**Source:** `main` @ `dacb3bc` — 261 files
**Path:** `run.sh` (proven), NOT `install.sh` (never completed anywhere)

Independently verified in this tarball: all six boosters report `internal_count=49053`
(correct artifact, not the retracted 998-row set); `config.json` carries H-13 thresholds with
no `execution` block, so nothing from quarantined v1.11.0 is present; `tools/preflight.sh` is
included, so it no longer needs separate transfer.

Six stages. Each has a checkpoint and a failure action. **Do not continue past a failed
checkpoint** — every past abort came from stacking a second problem on an unresolved first.

Set these once per SSH session on the target:

```bash
export APP=$HOME/antigravity-predictor
export TARBALL=$HOME/antigravity-predictor-v1.11.1-dacb3bc.tar.gz
```

---

## STAGE 0 — Transfer, verify, extract

From this workstation:

```bash
D=<repo_root>/dist
scp $D/antigravity-predictor-v1.11.1-dacb3bc.tar.gz \
    $D/antigravity-predictor-v1.11.1-dacb3bc.tar.gz.sha256 \
    USER@TARGET:~/
```

On the target:

```bash
cd ~ && sha256sum -c antigravity-predictor-v1.11.1-dacb3bc.tar.gz.sha256
tar xzf antigravity-predictor-v1.11.1-dacb3bc.tar.gz
mv ~/antigravity-predictor-v1.11.1 $APP
cd $APP && ls
```

**Checkpoint:** `sha256sum -c` prints `OK`, and `$APP` contains `run.sh`, `src/`, `models/`,
`tools/`, `config.json`.
**On failure:** re-transfer. Never extract an unverified tarball — silent truncation costs
hours of debugging a "code bug" that is a bad copy.

Confirm the right models landed, before anything starts:

```bash
for f in $APP/models/model_{btc,eth,sol}_{long,short}.txt; do
  echo "$(basename $f): $(grep -m1 -o 'internal_count=[0-9]*' $f)"
done
```

**Checkpoint:** all six report `internal_count=49053`. Any `998` means the wrong artifact —
stop and rebuild from `dacb3bc`.

---

## STAGE 1 — Preflight, then prerequisites

Run the preflight from the extracted tree. On the target, not here — the whole point is to
measure that machine.

```bash
cd $APP && bash tools/preflight.sh; echo "BLOCKERS: $?"
cat $APP/reports/preflight_*.json | tail -60
```

**Checkpoint:** blocker count is 0, or every blocker is understood and deliberately accepted.
**On failure:** fix the named blocker before continuing. Do not proceed on "probably fine."

This is the single most likely point of failure. `python3` being present does **not** mean
`python3 -m venv` works — on Mint/Debian the venv module ships separately, and its absence is
what produced the `203/EXEC` crash loop that has never been root-caused.

```bash
python3 --version
python3 -m venv /tmp/venvtest && /tmp/venvtest/bin/python -c "print('venv OK')" && rm -rf /tmp/venvtest
```

**If that fails:**

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip build-essential
# Mint 22/Noble may need the versioned package instead:
# sudo apt install -y python3.12-venv
```

Then re-run the test above until it prints `venv OK`.

Also confirm outbound reachability — the server is useless without it:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.bybit.com/v5/market/time
curl -sS -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/
df -h $HOME | tail -1
```

**Checkpoint:** `venv OK`, both curls return `200`, ≥4 GB free.

---

## STAGE 3 — Bootstrap

Takes 1–2 minutes, longer on first LightGBM wheel fetch:

```bash
cd $APP && ./run.sh
```

**Checkpoint:** `[run.sh] .venv ready.` followed by uvicorn startup lines, no traceback.
**On failure:** capture the full traceback before doing anything else. If it is a
`ModuleNotFoundError`, it is the documented cwd-path class — run from `$APP`, never from
`$APP/src`.

---

## STAGE 4 — Validate running

Leave `run.sh` in one terminal. In a second SSH session:

```bash
curl -s localhost:18910/api/status | head -40
for s in BTC_USDT ETH_USDT SOL_USDT; do
  echo "== $s"; curl -s localhost:18910/api/feature-parity/$s
done
```

**Checkpoint, all three assets:**
- `populated_features: 65`, `expected_features: 65`
- `blocked_reason: null`
- `degraded: false`
- `latest_signal` is one of BUY / SELL / NEUTRAL — **not** `UNAVAILABLE`

`UNAVAILABLE` means the feature gate is blocking inference. Read `missing_features` in
`/api/status` — it names the exact family that failed, usually a data feed that did not
reach the target.

Then confirm it is stable rather than merely started:

```bash
sleep 600
curl -s localhost:18910/api/status | grep -o '"latest_signal":"[A-Z]*"'
```

**Checkpoint:** still responding after 10 minutes, no crash loop.

---

## STAGE 5 — Survive logout

Until this is done, the system dies when you close SSH. Simplest reliable option, no root:

```bash
cd $APP
nohup ./run.sh > $APP/logs/run.out 2>&1 &
echo $! > $APP/run.pid
disown
```

Verify it survives:

```bash
exit          # close SSH entirely
# reconnect
curl -s localhost:18910/api/status | head -5
tail -20 $APP/logs/run.out
```

**Checkpoint:** responds after a full disconnect/reconnect cycle.

A user-level systemd unit is the better long-term answer, but `nohup` is what gets you to a
demo tonight without touching root.

---

## STAGE 6 — Remote access (only after Stage 5 passes)

Do not start this until the system is confirmed running and stable locally on the target.

Three known issues will bite in this order:

1. **CORS** — `DASHBOARD_ORIGINS` defaults to localhost. The page will load and every API call
   will fail. Set it to the exact origin the client will type, scheme included.
2. **WebSocket** — the dashboard's live feed uses `/ws`. Any reverse proxy must forward
   `Upgrade` and `Connection` headers or the page renders and never updates, which looks
   identical to a broken model.
3. **Backend exposure** — 18910/18911/18912 stay bound to localhost; only the proxy is
   reachable.

Fastest route to a working URL is an SSH tunnel from the client side, or a Cloudflare/ngrok
tunnel from the target — neither needs DNS, certs, firewall changes, or root. Decide when you
get here.

---

## If a stage fails

Capture, then diagnose. In order:

```bash
tail -50 $APP/logs/run.out
curl -s localhost:18910/api/status
python3 --version; echo "$(lsb_release -ds 2>/dev/null || cat /etc/os-release | head -2)"
ls -la $APP/models/ | head
```

Send me those four outputs and nothing else. The failure is almost certainly one of the
twelve catalogued modes, and those four commands identify which one.
