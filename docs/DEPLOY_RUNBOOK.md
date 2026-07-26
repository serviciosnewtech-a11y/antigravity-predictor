# DEPLOY RUNBOOK — Linux Mint test machine

**Artifact:** `antigravity-predictor-v1.11.1.tar.gz`
**SHA256:** `c08595c1d26d35d81c9544d49968a13fffbee2617f3e5d4b0201f7e7748db93a`
**Source:** `main` @ `ac1432d`
**Path:** `run.sh` (proven), NOT `install.sh` (never completed anywhere)

Six stages. Each has a checkpoint and a failure action. **Do not continue past a failed
checkpoint** — every past abort came from stacking a second problem on an unresolved first.

Set these once per SSH session on the target:

```bash
export APP=$HOME/antigravity-predictor
export TARBALL=$HOME/antigravity-predictor-v1.11.1.tar.gz
```

---

## STAGE 0 — Preflight

Copy `tools/preflight.sh` to the target and run it there. Not here. The whole point is to
measure that machine.

```bash
bash ~/preflight.sh; echo "BLOCKERS: $?"
cat ~/reports/preflight_*.json 2>/dev/null || ls reports/
```

**Checkpoint:** blocker count is 0, or every blocker is understood and deliberately accepted.
**On failure:** fix the named blocker. Do not proceed with "it's probably fine."

---

## STAGE 1 — Prerequisites

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

## STAGE 2 — Transfer and verify

From this workstation:

```bash
scp /media/hermes/Storage/git/antigravity-predictor/antigravity-predictor-v1.11.1.tar.gz \
    /media/hermes/Storage/git/antigravity-predictor/antigravity-predictor-v1.11.1.tar.gz.sha256 \
    /media/hermes/Storage/git/antigravity-predictor/tools/preflight.sh \
    USER@TARGET:~/
```

On the target:

```bash
cd ~ && sha256sum -c antigravity-predictor-v1.11.1.tar.gz.sha256
```

**Checkpoint:** prints `OK`.
**On failure:** re-transfer. Never extract an unverified tarball — silent truncation is how
you spend two hours debugging a "code bug" that is a bad copy.

---

## STAGE 3 — Extract and bootstrap

```bash
cd ~ && tar xzf $TARBALL
mv ~/antigravity-predictor-v1.11.1 $APP 2>/dev/null || true
cd $APP && ls
```

Confirm the right models landed **before** starting anything:

```bash
for f in models/model_{btc,eth,sol}_{long,short}.txt; do
  echo "$f: $(grep -m1 -o 'internal_count=[0-9]*' $f)"
done
```

**Checkpoint:** all six report `internal_count=49053`. If any says `998`, the wrong artifact
was built — stop and rebuild from `ac1432d`.

Then bootstrap. Takes 1–2 minutes, longer on first LightGBM wheel fetch:

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
