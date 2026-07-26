# DEPLOY RUNBOOK — Antigravity Predictor v1.11.1

**Artifact:** `antigravity-predictor-v1.11.1-9ef00fa-full.tar.gz`  
**SHA256:** `d05bc763a9d7ae573c318f98177fcb5a5a7f448d7c760502cb6ad7d6b82ca3a8`  
**Size:** 92 MB (extracts to 104 MB)  
**Source:** `main` @ `9ef00fa`  

---

## 1. Pre-Deployment Cleanup & Process Termination (Clean Slate)

Before installing, stop any existing daemon and clean leftover state to avoid port or state conflicts:

```bash
# 1. Kill running predictor daemon by PID file
if [[ -f $HOME/predictor/run.pid ]]; then
  kill $(cat $HOME/predictor/run.pid) 2>/dev/null || true
fi
if [[ -f $HOME/antigravity-predictor/run.pid ]]; then
  kill $(cat $HOME/antigravity-predictor/run.pid) 2>/dev/null || true
fi

# 2. Terminate any lingering predictor_server.py processes
pkill -f "predictor_server.py" 2>/dev/null || true
fuser -k 18910/tcp 2>/dev/null || true

# 3. Remove existing installation directory for clean install verification
rm -rf $HOME/predictor $HOME/antigravity-predictor
```

---

## 2. Fresh Installation & Bootstrap

```bash
# 1. Extract bootstrap installer from full package
tar xzf antigravity-predictor-v1.11.1-9ef00fa-full.tar.gz \
  antigravity-predictor-v1.11.1/tools/bootstrap.sh --strip-components=2

# 2. Run user-space bootstrap installer
./bootstrap.sh /path/to/antigravity-predictor-v1.11.1-9ef00fa-full.tar.gz

# 3. Launch application daemon
cd $HOME/antigravity-predictor && mkdir -p logs
nohup ./run.sh > logs/run.out 2>&1 & echo $! > run.pid
```

> **Note:** Hard-refresh your browser (`Ctrl+F5` or `Cmd+Shift+R`) to clear cached `app.js` and render the updated AUD-01 ATR geometry (R:R `1.50:1`) and active symbol UI guard.

---

## 3. Post-Deploy Audit Verification

Run the verification audit script to validate system health, symbol isolation, and ATR geometry:

```bash
cd $HOME/antigravity-predictor
python3 tools/eval_stage1.py
```

Check assertions:
- **Section 5 (Symbol Isolation)**: `sym === state.activeSymbol` guard active. Non-active symbol snapshots do not paint active Agent Report UI.
- **Section 6 (ATR Geometry)**: SL distance = $1.0 \times \text{ATR}$, TP1 distance = $1.5 \times \text{ATR}$, TP2 distance = $2.5 \times \text{ATR}$. R:R = **`1.50:1`**.

---

## 4. Emergency Rollback Protocol

If the installation fails or rollback is required:

```bash
# 1. Terminate current daemon
if [[ -f $HOME/antigravity-predictor/run.pid ]]; then
  kill $(cat $HOME/antigravity-predictor/run.pid) 2>/dev/null || true
fi
pkill -f "predictor_server.py" 2>/dev/null || true

# 2. Delete failed installation directory
rm -rf $HOME/antigravity-predictor

# 3. Restore previous release package if needed
# ./bootstrap.sh /path/to/previous-tarball.tar.gz
```
