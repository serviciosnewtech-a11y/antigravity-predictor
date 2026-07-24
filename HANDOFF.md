# Handoff Dossier — Antigravity Predictor

Written 2026-07-23 to close out a long session before starting a fresh one.
Read this top to bottom before touching anything — it front-loads the state,
gotchas, and conventions the old session accumulated so you don't have to
re-derive them from git log archaeology or, worse, repeat a mistake that was
already found and fixed once.

Owner/operator: Luis E. Wilson (serviciosnewtech@gmail.com). Luis relays
messages between you (Hermes, running on the live VPS with real shell/file
access) and a separate Claude session (Cowork) that has the git repo mounted
but no access to the live host. That Claude session did the code/test/package
work described below and is who wrote this file.

## 1. What this system is

Antigravity Predictor: a self-hosted crypto futures **signal** system —
advisory only, no autonomous trade execution. Bybit linear perpetuals,
BTC/ETH/SOL, 15-minute timeframe. Two LightGBM models per asset (long/short).
TP/SL are ATR-based: TP1 = 1.5×ATR, TP2 = 2.5×ATR, SL = 1.0×ATR (see §7 for a
subtlety about displayed vs. raw ATR distance — validated today, not a bug).
FastAPI backend (`src/predictor_server.py`) serves a REST+WebSocket API and a
static dashboard (`dashboard/`). Two deployment products, packaged from the
same source tree: bare-metal (systemd units, `deploy/bare-metal/`) and Docker
(`deploy/docker/`).

## 2. Current state — READ THIS BEFORE TRUSTING ANY TAG NUMBER

**Latest, current tag: `beta-1.10.15`.** Cut 2026-07-24 with the Forge
scoring/scorecard work (§7.10). Prior tags `beta-1.10.10` through
`beta-1.10.14` shipped in the same session batch (§7.6/§7.8). All older
references in this doc to "beta-1.10.9 is latest" pre-date those and are
stale — don't trust them, trust the git tag list.

**Gotcha:** tag `beta-1.11` exists and sorts *after* `beta-1.10.9`
alphanumerically, but it is chronologically **older** —
`git merge-base --is-ancestor beta-1.11 HEAD` confirms it's an ancestor of
current HEAD, not a sibling or descendant. Mid-session the numbering scheme
changed from `beta-1.N` to `beta-1.10.N` and the old `beta-1.11` tag was never
retired or renamed. **Ignore `beta-1.11` — treat `beta-1.10.9` as latest.**
If you cut a new tag next, `beta-1.10.10` is the correct next number, not
`beta-1.12` (though renaming the whole scheme to avoid this trap again —
e.g. jumping straight to `beta-1.11.0` — might be worth raising with Luis).

Full chronological version history with detailed per-release notes lives in
`../releases/README.md` (outside this repo, sibling directory — see §5).
That file is append-only and is the actual source of truth for "what shipped
when and why." Skim its last ~10 entries (beta-1.10.4 through beta-1.10.9)
for the freshest context; going back further gets you the beta-1.8/1.9
history (durable trade log, model-path-resolution crash fix, docker/bare-metal
split).

## 3. What shipped in the last few versions (freshest first)

- **beta-1.10.9** (dashboard only, no backend changes): fixed a real bug
  where switching from XAU/USD (Gold, forced onto a 1d timeframe) to a real
  trading symbol kept showing Gold's stale candles — a single shared
  `state.displayCandles` was reused across all symbols regardless of which
  one it was fetched for. Also fixed the trade-history panel never actually
  scrolling on its own (no bounded height for its `.scrollable` region to
  clamp against), and added a second, independent side-by-side chart
  (`dashboard/app.js`: `initChart2`/`switchAsset2`/`fetchCandlesForSymbol2`).
- **beta-1.10.8**: relay startup warm-up. A real CLI agent's first
  invocation after a fresh process start can take real cold-start time
  (~15s measured live); the relay now absorbs that once at startup instead
  of on the first real user request or after every restart.
  `AGENT_RELAY_WARMUP_TIMEOUT_S` / `AGENT_RELAY_SKIP_WARMUP` env vars.
- **beta-1.10.6/1.10.7**: fixed a real `h11` protocol error under live
  traffic (`Too much data for declared Content-Length`). Root cause:
  `/api/chat`'s route handler was `async def` but called a synchronous
  blocking `requests.post()` directly, stalling uvicorn's whole event loop
  and corrupting concurrent connection interleaving. Fixed via
  `asyncio.to_thread()`. (1.10.7 is a small test-portability follow-up —
  `deploy/bare-metal`-only tests now skip cleanly against docker-only
  tarballs instead of hard-failing.)
- **beta-1.10.4/1.10.5**: real bare-metal deploy plumbing bugs found live —
  `predictor.service` never had `EnvironmentFile=`, so nothing in `.env`
  ever reached the process serving `/api/chat`; the local no-API-key CLI
  agent relay pattern (`tools/agent_chat_relay.py`) existed for the
  monolith launcher but was never wired into the systemd product at all.
  Also found and defended against a `{prompt}`-quoting trap in
  `AGENT_RELAY_CMD` templates (see `deploy/bare-metal/LIVE_DEPLOY_NOTES.md`
  for the full story — worth reading once, it's the most detailed record of
  live-deploy gotchas in this repo).

## 4. Architecture note: what "Hermes" means in this codebase's chat feature

The dashboard's chat box (`/api/chat`) is designed to be backed by **you** —
a real agent with tools, memory, and reasoning — not a plain hosted LLM API
call. It was explicitly rejected earlier in the session to make `/api/chat`
"open and agnostic" with a pluggable backend registry; that proposal is
documented as rejected in code comments already, don't re-propose it.

The actual wiring: `predictor_server.py` calls out to `HERMES_PROXY_URL`
(OpenAI-chat-style contract). That can point at either (a) a remote hosted
API, or (b) — the actually-used, no-API-key default — a local relay,
`tools/agent_chat_relay.py`, running as `agent_relay.service`
(`deploy/bare-metal/agent_relay.service`), which shells out to a configured
local CLI agent (`AGENT_RELAY_CMD`, with a `{prompt}` placeholder). During
this session that CLI agent was, for testing, temporarily pointed at the
live admin Hermes session itself (explicitly authorized as a *temporary*
test — "wire it to this session so we know it works, we will refine the
relay later"). A properly isolated install was then set up at
`/opt/predictor-metis` (Nous Research's open-source Hermes Agent,
`https://hermes-agent.nousresearch.com/`, installed per-user, data at
`~/.hermes`, binary via `~/.local/bin/hermes`, invoked with `hermes -z
<prompt>` — the "purest one-shot" CLI entry point, no TTY/interactive-layer
requirements).

**Open item, not yet closed (tracked as task #65 in the old session's task
list):** fully de-scope `agent_relay` off the live admin session and confirm
`/opt/predictor-metis` has no host-admin tool access — i.e. verify it
actually matches the product's own documented guarantee (search this repo
for `HERMES_CORE_IDENTITY` — the predictor's own system prompt for the
*trading* signal agent, not to be confused with the CLI agent backing the
chat relay) that the chat feature has no tool-calling wired to it beyond
read/write memory. If the live `agent_relay.service`'s `AGENT_RELAY_CMD`
still points at anything other than the isolated `/opt/predictor-metis`
install, that's the first thing to check and fix.

**Update, confirmed live 2026-07-23 (same day, later in the day):** the live
host's `AGENT_RELAY_CMD` was still on the shipped fallback default,
`hermes --profile metis chat -q {prompt}` — never actually a verified value
on any host, just an unfixed leftover from an older predecessor tool (full
story: `deploy/bare-metal/LIVE_DEPLOY_NOTES.md` #7, added this same session
after this exact failure was reported). `/health` fails because no Hermes
CLI profile named `metis` exists on that host; `/api/chat` returns 503.
Fixed the misleading docs/examples (`.env.example`, `install.sh`,
`tools/agent_chat_relay.py`'s docstring, this file's own §6 example) so they
no longer present that string as a verified "Correct:" value. **Still
needs doing on the live host itself:** find the real working invocation
(most likely `hermes -z "<prompt>"` against the `/opt/predictor-metis`
install, absolute path, per LIVE_DEPLOY_NOTES.md #7's fix steps), verify it
by hand first, then set it in `.env`, restart `agent_relay`, recheck
`/health` → `/api/chat/status` → a real `/api/chat` call.

**Update, same incident, later same day:** progress since the above —
`agent_relay.service` was replaced with the canonical unit and given a
dedicated OS user (not `predictor`) so the internet-facing dashboard
process never gains a path to Hermes's own credentials at
`/opt/predictor-metis/.hermes/.env` (that access boundary was deliberately
kept locked down — this was a real security decision, explicitly confirmed
with Luis, not a plumbing fix; see `LIVE_DEPLOY_NOTES.md` for the pattern
if this needs repeating on another host). A real Hermes CLI invocation
under the relay's own environment then worked, ~5.8s. Next symptom:
`/api/chat/status` still reported `relay unreachable: ... Read timed out
(read timeout=12)`. Root cause found and fixed in beta-1.10.11: `chat_
status()`'s local-relay `/health` probe in `src/predictor_server.py` had a
**bare hardcoded `timeout=12`** that silently ignored
`AGENT_RELAY_HEALTHCHECK_TIMEOUT_S` entirely, despite a comment claiming it
tracked that env var. Live `.env` had that var raised to 15 (reasonable, to
give a real agent more health-check headroom), so predictor's client-side
wait was giving up *before* the relay's own longer, actually-configured
budget could finish. Fixed to read the env var properly
(`relay_budget + 2s`). **Not yet applied to the live host as of this
writing** — pull beta-1.10.11 (or just the one-line fix in `src/
predictor_server.py`), restart `predictor.service`, recheck
`/api/chat/status` → a real `/api/chat` call.

**Also still open:** whether the relay timeout that was originally observed
at a hard "10.0s" was `AGENT_RELAY_TIMEOUT_S` explicitly set to 10 somewhere
in the live `.env` (worth reverting to the 120s default now that warm-up
handles cold start separately) or `AGENT_RELAY_HEALTHCHECK_TIMEOUT_S` (also
defaults to 10s, meant only for the lightweight `/health` probe) being hit
by an unexpected code path. Non-blocking — beta-1.10.8's warm-up fix should
resolve the practical symptom either way — but worth a `grep` of the live
`.env` for closure.

## 5. Conventions this project follows — keep following them

- **Every real fix gets a permanent git tag** (never moved or reused once
  created — see the `beta-1.11` numbering trap in §2 for why this matters),
  packaged via `tools/package_release.sh {docker|bare-metal} <tag>
  <output-dir>`, archived with `.sha256` checksums to
  `../releases/antigravity-predictor/<tag>/` (a directory **outside** this
  git repo, sibling to it — release tarballs are build output, not source),
  and logged as a new entry in `../releases/README.md` (append-only, never
  edit past entries except to mark them "Superseded by X").
- **Verify by execution, not static review.** Extract the actual packaged
  tarball and run `pytest` / reproduce the deploy logic against it before
  calling anything shipped. Don't trust that a fix "looks right."
- **`git commit -F /tmp/commit_msg.txt`** (heredoc into a temp file, then
  `-F`) — avoids a known bash quoting bug with embedded double-quoted
  substrings in `-m` messages.
- Every meaningful lesson learned from a live-host debugging session gets
  written into a durable file in the repo (e.g.
  `deploy/bare-metal/LIVE_DEPLOY_NOTES.md`), not left in chat — this
  standing instruction from Luis is why this handoff doc exists at all.
- Prefer a scoped, root-cause fix over a broader knob-turn (e.g. beta-1.10.8
  rejected "just raise the timeout" in favor of an actual warm-up
  mechanism — this pattern of pushback is something Luis actively wants,
  not just tolerates).

## 6. Key file map

- `src/predictor_server.py` — FastAPI backend, all REST/WS routes, the
  signal engines, `/api/chat`, price-level computation.
- `dashboard/{index.html,app.js,style.css}` — the whole frontend, no build
  step, static files served directly by FastAPI's `StaticFiles` mount.
- `tools/agent_chat_relay.py` — the local no-API-key chat backend relay.
- `tools/package_release.sh` — packaging script, see §5.
- `deploy/bare-metal/` — systemd units + `install.sh` for the bare-metal
  product; `LIVE_DEPLOY_NOTES.md` here is the single best "gotchas" file.
- `deploy/docker/` — Dockerfiles + compose + `PRE_TEST_CHECKLIST.md` (docker
  has had less live-testing than bare-metal this whole project — treat with
  more suspicion).
- `../releases/README.md` and `../releases/antigravity-predictor/<tag>/` —
  the full shipped-version history and archived tarballs, outside this repo.
- `tests/` — pytest suite, 52 tests as of beta-1.10.9. No headless test
  harness exists for `dashboard/`'s JS — frontend changes get statically
  verified (`node --check`, DOM id checks) but real interaction/visual
  verification needs an actual browser on the live host.

## 7. Today's validated finding: the "R:R looks backwards" critique

Luis relayed an external (GPT) critique of a live dashboard screenshot
claiming the displayed R:R ratio was mislabeled/reversed and that TP price
distances didn't match their advertised ATR multiples. Investigated against
actual code (`dashboard/app.js:2046-2076`, `src/predictor_server.py:1364-
1393`, `dashboard/index.html:486,488,1004`). Verdict, so this doesn't get
re-litigated from scratch:

- **The numbers are internally consistent and correctly computed** — every
  figure in the screenshot reconciles exactly against the actual formulas
  (verified by hand: SL, TP1, TP2, and R:R all recompute to the exact
  displayed values). No sign bug, no wrong-reference-point bug — short-trade
  direction is handled correctly (`Math.abs()` on both risk and reward, SL
  placed above entry, TPs below, for a short).
- **Real, legitimate point:** TP1/TP2 labels say "1.5×ATR"/"2.5×ATR" but the
  actual displayed price distance is smaller than that raw multiple, because
  a fee-drag adjustment (`close * 0.0015`) is baked into the final price
  before display. This is a deliberate design choice (fee accounting), not a
  bug, but the label doesn't disclose it — worth a UI tooltip or label
  tweak if it keeps causing confusion, not worth a numerical fix.
- **Overstated / not a bug:** the "R:R is backwards" claim. The UI only ever
  displays the bare abbreviation "R:R" — it never spells out "Risk:Reward"
  vs "Reward:Risk" in the HTML, so there's no literal text mislabel to
  point at. The formula used is `reward / risk`, which is a legitimate,
  common retail-trading convention (reward earned per unit of risk taken;
  <1 correctly flags an unfavorable setup, which is exactly what 0.43
  signals for that screenshot's trade). It is not "almost certainly using
  the wrong reference point" — that specific claim is wrong, verified by
  reproducing the arithmetic by hand.
- **One real, minor code-hygiene item, not user-facing:** the server-side
  field is literally named `risk_reward_tp1` (`predictor_server.py:1393`)
  but holds a `reward/risk` value — the name and the formula disagree with
  each other internally, which is exactly the kind of thing that causes
  someone to later "fix" it incorrectly. Worth a rename (e.g. to
  `reward_risk_tp1`) next time that function is touched, but not urgent and
  not something that changes any displayed number.

## 7.5. Rollback executed 2026-07-23 — live host reset to a clean beta-1.10.11 install

The hand-edit chain in §7/the update above (canonical unit swap → dedicated
relay user → timeout bug fix) got deep enough into live-only drift that
Luis called for a clean reset rather than continuing to hand-patch forward.
Executed: `/opt/predictor` restored from the beta-1.10.11 bare-metal
tarball, all three systemd units (`predictor.service`, `agent_relay.service`,
`signal_agent.service`) reinstalled from the version-controlled source
(wiping every hand-edit made to any of them this incident), `.env` reset to
a fresh template. Verified clean: `predictor.service` active, `/api/status`
responds, `/` serves the dashboard, `/ws` is available.

**Chat/relay is deliberately left unconfigured.** Nothing points
`AGENT_RELAY_CMD` at anything real right now — `/api/chat` will report
unavailable, and that's intentional, not a bug to chase. Do not re-approach
this by hand-patching the live host again. If/when it gets revisited:

- The two real code/doc fixes from this incident (beta-1.10.10's misleading
  `--profile metis` example, beta-1.10.11's hardcoded healthcheck timeout)
  are still in the codebase and the current package — those don't need
  redoing.
- The dedicated-relay-user pattern (run `agent_relay.service` as an account
  that isn't `predictor`, so the internet-facing dashboard process never
  gains a path to Hermes's own credentials) was proven to work live but
  reverts on every plain `install.sh` run (it always writes `User=predictor`
  — no two-user support exists in the installer yet). Re-apply it by hand
  each time, or treat "add proper two-user support to `install.sh`" as real
  future work if this is going to be a permanent pattern.
- Go slowly, one verified step at a time, with evidence before each edit —
  the same discipline this whole file has been trying to model. The prior
  attempt's actual failure mode wasn't any single wrong fix (every
  individual fix found this incident was real and correct) — it was
  stacking live hand-edits faster than they could be tracked.

## 7.6. Exposing the dashboard to the internet — beta-1.10.12/1.10.13

Luis asked for the best route to put this on the public internet for a
couple of testers. Found real gaps before recommending anything: `config.
json`'s `server.host` was `"0.0.0.0"` (correct for Docker, but on bare-
metal this meant `predictor_server.py` was listening on the VPS's real
public interface directly, bypassing nginx entirely, with zero
authentication anywhere in the app), and no host firewall existed at all.
Fixed in beta-1.10.12: `install.sh` now forces the bare-metal `config.json`
copy to `127.0.0.1` and configures `ufw` (SSH/80/443 only). Fixed in
beta-1.10.13: nginx basic auth (one shared username/password, generated by
`install.sh`, printed once in the install log) — sized for "a few known
testers," not a real per-user login system; `ENABLE_BASIC_AUTH=false` opts
out. HTTPS still needs a manual `certbot --nginx -d <domain>` once a domain
is pointed at the host — not automated, since `install.sh` doesn't collect
a domain interactively.

None of this has been applied to the live host yet as of this writing —
it needs a fresh `install.sh` run (or at minimum: patch `config.json`,
add the `ufw` rules, add `auth_basic` to the live `nginx.conf` and generate
`/etc/nginx/.htpasswd` by hand) before actually opening 80/443 to the
internet. If this session already did a clean reinstall per §7.5, doing
that reinstall from beta-1.10.13 instead of beta-1.10.11 gets all of this
for free.

## 7.7. First clean rehearsal, backend side confirmed healthy (2026-07-23/24)

Per Luis's call to get this fully working in a rehearsal environment before
ever touching a real paid VPS (§7.5's rollback made clear why): a genuinely
fresh `install.sh` run hit one real bug — `/opt/predictor/.venv` never got
created, `predictor.service` crash-looped on `203/EXEC` (systemd's
"`execve()` itself failed" code) with the restart counter past 200. Root
cause was never fully pinned down (the `drwx------` permission theory Hermes
proposed doesn't actually hold — root bypasses Linux DAC permission checks
entirely, and `install.sh`'s own `[[ $EUID -eq 0 ]]` guard means the venv
creation step already runs as root, so that directory's `0700` bit
shouldn't have blocked it). Recreating the venv by hand (as root, `pip
install -r requirements.txt`, `chown -R predictor:predictor`) fixed it
immediately. **This might recur on the next fresh install** since the real
cause is still unknown — if it does, capture `install.sh`'s actual output
this time (does it reach the final "installed successfully" banner or stop
silently before the venv step?) rather than just recreating the venv again.

Once running: full backend smoke passed — `/api/status`, `/api/trades`,
`/api/orderbook`, `/api/market-tickers`, `/api/news`, `/api/calendar`,
`/api/assets` all 200 with live Bybit data, `/ws` verified with a real
WebSocket handshake (not just a plain `curl`, which always 404s against a
websocket-only Starlette route by design — that's not a bug, don't chase it
again if it comes up). `predictor.service` stable for 200+ minutes.

**Recurring friction, same root cause each time:** `/opt/predictor` (the
`predictor` system user's home directory) is `0700` — only `predictor`
itself and root can read/write/traverse into it at all. This has now
blocked three different things run by a different account: a WS-probe
script write, and a monitoring job's `tail` of `predictor.log`. None of
these are bugs in the app — they're a real, structural side effect of a
sensibly-private home directory conflicting with wanting other tooling to
inspect the service from outside. Two real options, not yet decided:
(a) narrow ACL grants (`setfacl`) for whichever specific account needs
access, scoped to exactly what it needs — matches the pattern already used
for the agent-relay credential-access decision earlier this incident; or
(b) switch `predictor.service`'s `StandardOutput=`/`StandardError=` from
`append:/opt/predictor/logs/predictor.log` to systemd's default (`journal`)
so log access becomes `journalctl -u predictor`, gated by the standard
`systemd-journal` group instead of raw filesystem permissions on a private
home directory — cleaner long-term, but changes what `tail -f .../
predictor.log` (already documented in `install.sh`'s own status banner)
actually shows, so it's a real, if small, decision rather than a pure
bugfix.

**Still open:** the dashboard visual/toggle UI pass needs Hermes's Chrome
CDP connection (`127.0.0.1:9222`) restored — that's Hermes-side tooling,
not something fixable from this session. Everything backend-testable is
confirmed healthy in the meantime.

## 7.8. Data preservation + chart history depth — beta-1.10.14

From the live dashboard screenshot the user shared: "we need to preserve
data at all costs" and "we only have like 1 day or less" of chart history.
Both were real, found and fixed:

- `signal_history.db` was restart-safe but not accident-safe -- a single
  copy inside `/opt/predictor`, gone the moment that directory is wiped,
  reinstalled, or (what actually happened) simply not present on a fresh
  rehearsal host with no migration path from wherever the old data was.
  New `tools/backup_signal_log.py` + `predictor_backup.service`/`.timer`:
  safe SQLite-backup-API snapshots every 6h to `/opt/predictor-backups`
  (outside the app dir on purpose), with retention pruning. This is
  local-disk redundancy against reinstall/wipe accidents only -- NOT
  off-host backup against losing the whole disk/host. That's real,
  separate work, still undecided, not solved by this.
- The dashboard's default (15m) chart view was capped at ~1.5 days of
  history on every fresh start because `AssetEngine.fetch_initial_candles()`
  hardcoded a 150-candle Bybit request. Bumped to 1000 (Bybit's own
  per-request max), ~10.4 days now.

Neither has been applied to the rehearsal host yet as of this writing --
needs a fresh `install.sh` run (or at minimum: pull `src/predictor_server.py`
+ `tools/backup_signal_log.py`, restart `predictor.service`, and manually
set up the backup timer/directory) from beta-1.10.14.

## 7.9. Forge "improving loop" — discussion-only, 2026-07-24, NO CODE WRITTEN YET

Luis opened this explicitly as discussion, not implementation: **"do not code
it right away i have a couple of questions."** Everything in this section is
agreed direction, not shipped work — the next session should implement it,
not re-litigate it, unless new evidence changes the picture.

**Context:** `forge/` (`server.py`, `db.py`, `simulator.py`, `strategies.py`,
`collector.py`) is a separate paper-trading/comparison engine, philosophically
consistent with the rest of the project (its own docstring: "NOTHING promotes
automatically... pull results, compare, decide"). It runs 16 `StrategySimulator`
instances concurrently in one process. It is currently **fully disconnected**
from `retrain_all.sh` / the retraining pipeline — no shared data at all.

**Ground-truth findings from this session (verified by reading the actual
code, not assumed):**

- **Not a memory/capacity problem.** `LiveCollector._history` is a bounded
  `deque(maxlen=ATR_PERIOD + 5)` per symbol (`forge/collector.py`); each
  `StrategySimulator` holds only its param set plus at most one open position.
  16-18 of these in one process costs kilobytes, not megabytes. Luis's
  original framing ("memory management for potentially 18 agents") does not
  need architectural change on the memory side — flag this to him directly if
  it resurfaces, the concern doesn't hold up against the actual code.
- **Logging is already durable, not memory-buffered.** `forge/db.py`:
  `insert_candle()`, `open_trade()`, `close_trade()` all commit immediately
  inside `with _lock, _conn() as c:` blocks. `trades` already logs strategy_id,
  symbol, direction, entry/exit price, pnl_pct, exit_reason, candles_held per
  closed position. `candles` already logs raw model output per tick
  (long_prob, short_prob, atr, trend), pruned to last 5000 rows/symbol. **The
  "log it" half of Luis's ask is essentially already solved.**
- **Real, confirmed bug: `strategy_registry` has no stable identity across
  restarts.** `forge/strategies.py`: `Strategy.id` is
  `field(default_factory=lambda: uuid.uuid4().hex[:8])` — a fresh random ID
  every time the process (re)starts, not loaded or derived deterministically.
  `db.upsert_strategy()` does `INSERT OR REPLACE ... VALUES (:id, ...)`, so a
  new ID means a new row, not an update. Live `forge_data/forge.db` observed
  with 144 `strategy_registry` rows for what should be 16 strategies (16 ×
  ~9 restarts). **This must be fixed before any scoring step means anything**
  — otherwise a strategy's history fragments across every restart. Fix
  direction agreed but not written: derive `id` deterministically from
  `name + symbol + direction + params`, or have startup look up existing
  registry rows by name instead of blindly inserting fresh ones.
- **What's actually missing is a *comparison* step, not more logging.**
  Nothing in the codebase currently reads `trades`/`candles` and produces a
  verdict — Forge's own stated philosophy ("pull results, compare, decide")
  has no code behind the "compare" part yet.

**Luis's stated constraint, important for how this gets built:** he set out
gathering this data to be analyzed but says plainly "I know little to
nothing about crypto so often times I don't know what number means, I just
know it goes there." **This is a hard design requirement, not a nice-to-
have:** the comparison step must not just surface raw numbers (win rate,
avg pnl_pct) for Luis to interpret — it needs to output a plain-language
verdict per strategy he can act on without crypto expertise.

**Agreed direction (discussion-stage, ready to scope into real work next
session):**

1. Fix the `Strategy.id` stability bug first (above) — a scorecard built on
   fragmented history is worse than no scorecard.
2. Add one small, scheduled scoring job — same pattern as
   `predictor_backup.service`/`.timer` (§7.8): no LLM, no autonomy, pure
   arithmetic over already-durable SQLite rows, on a timer.
3. The job groups `trades` by `strategy_id`, computes win rate / average
   pnl_pct / sample size, and — critically — applies a **minimum sample size
   gate before rendering any verdict** (e.g. ~30 closed trades) so 3 lucky
   trades can't produce a false "this strategy is great" signal. Below the
   threshold: explicit "not enough data yet," not a hidden/absent row.
4. Above the threshold, output a plain label per strategy, not raw stats:
   e.g. "profitable, keep running" / "losing money, consider disabling" /
   "not enough trades yet." Luis should be able to read the output and act
   without knowing what any underlying number means.
5. **Open, unresolved question — needs Luis's decision before implementing:**
   should the scoring job be allowed to auto-flip a losing strategy's
   `active` flag to 0 in `strategy_registry` (stops it from paper-trading
   further, fully reversible, nothing "live" touched), or should it only
   ever report and leave every state change to Luis by hand? Leaning toward
   "report only" to match the project's consistent "advisory only, nothing
   promotes/executes automatically" stance elsewhere, but this wasn't
   explicitly confirmed — ask before writing it either way.

**Separately flagged, smaller scope, not yet needed:** if a future goal is
comparing *model retrains* through Forge (not just strategy param variants),
that needs a new `model_version` field somewhere in the logging path — no
such concept exists in the schema today. Distinct, later piece of work, not
part of the 16-strategy scorecard above.

## 7.10. Forge improving loop — implemented in beta-1.10.15, 2026-07-24

Followed §7.9's agreed direction after one round of design pushback from
Luis, then shipped. Everything below is what actually landed, not what was
discussed. Full 85-test suite green.

**Design decisions Luis's rebuttal changed vs. the initial audit:**
- Strategy.id is **not** `self.name` — it's `uuid5(FORGE_STRATEGY_NAMESPACE,
  canonical(symbol + direction + sorted params))[:12]`. Cosmetic rename of
  the display name (`"EMA Cross"` → `"EMA Cross v2"`) does NOT create a new
  identity; a real param change DOES. Fixed namespace UUID + a
  `STRATEGY_ID_SCHEMA_VERSION` baked into the canonical string protect
  against future identity-fields-set drift.
- Migration is idempotent with a printable report (no silent deletes):
  remaps `trades.strategy_id` by `strategy_name` for anything not on a
  canonical id, deletes non-canonical `strategy_registry` rows, scrubs any
  stale `id` inside `params` JSON. Orphan trades from removed strategies
  are left in place, not silently deleted. Verified against a synthetic
  144-row registry — collapses to 16, second run is a no-op.
- Scoring is a **monolith** (`forge/scoring.py`), one file, three sections:
  metrics / evaluators / recommendations. Splits into three modules the
  moment a second evaluator (Bayesian, drift detection, ML ranking) shows
  up — section boundaries in the file map 1:1 to that future split.
- Full metric set: `trade_count`, `win_rate_pct`, `expectancy_pct`,
  `profit_factor`, `avg_R`, `max_drawdown_pct`, `max_consec_losses`,
  `avg_candles_held`, `total_pnl_pct`. All persisted. All computed
  underneath the plain-language verdict.
- Four v1 verdict labels — nothing trend-based yet (needs
  `evaluation_history` depth to compute against, which won't exist until
  v2 has run for a while):
  * `not_enough_data` (below MIN_TRADES=50)
  * `healthy` (all-of: PF ≥ 1.3, expectancy > 0, DD < 15%, streak < 10)
  * `losing_money_consider_disabling` (any-of: PF < 1.0, total pnl < -3%,
    DD > 25%)
  * `inconclusive` (sample met, neither branch triggered)
- Losing check runs BEFORE healthy check — a high-WR strategy with tiny
  wins vs big losses (net negative) must flag losing, not healthy.
- All thresholds env-overridable via `FORGE_SCORECARD_MIN_TRADES` /
  `FORGE_SCORECARD_HEALTHY_PF` / `FORGE_SCORECARD_HEALTHY_MAX_DD` /
  `FORGE_SCORECARD_HEALTHY_MAX_CONSEC_LOSS` / `FORGE_SCORECARD_LOSING_PF`
  / `FORGE_SCORECARD_LOSING_TOTAL_PNL` / `FORGE_SCORECARD_LOSING_MAX_DD`.
  Defaults calibrated for 15-minute scalping (higher noise ratio → 50-trade
  gate vs. §7.9's initial 30, tighter DD tolerance, PF ≥ 1.3 as the
  "actually edge, not noise" line).
- **Report-only** on the write side. `POST /recommendations/apply` is a
  separate human-approved endpoint — same pattern as every other engine in
  the project. Applying a healthy verdict is a no-op; applying a losing
  verdict sets `active=0` in `strategy_registry` AND removes from the
  live `simulators` list so it stops paper-trading without waiting for a
  restart. Recommendation stays advisory in the write direction; the
  operator clicks once to apply.
- **Nullable `model_version` + `strategy_version`** columns added to
  `candles` and `trades` now, while the schema is young. Zero behavior
  change today; saves a future migration when `retrain_all.sh` learns to
  emit a manifest.

**Additional real bugs found + fixed same PR (both flagged during audit,
worth doing while everything was open):**
- `forge/server.py` `remove_strategy` used to only filter the in-memory
  `simulators` list — `strategy_registry.active` stayed 1, so state
  silently diverged. Now writes `active = 0` too.
- SQLite `journal_mode=WAL` + `synchronous=NORMAL` set on every connection
  — the scorecard's aggregate reader was going to serialize against the
  candle-insert writer under default DELETE/FULL. Confirmed live: WAL is
  persistent in the file header; `synchronous` is per-connection, set in
  `_conn()`, verified by regression test.

**Files shipped:**
- Modified: `forge/strategies.py` (canonical id), `forge/db.py` (WAL +
  schema + `cleanup_registry`), `forge/server.py` (lifespan cleanup, three
  new routes, `remove_strategy` DB fix), `deploy/bare-metal/install.sh`
  (scorecard timer + directory creation).
- New: `forge/scoring.py`, `tools/forge_scorecard.py`,
  `deploy/bare-metal/forge_scorecard.service`,
  `deploy/bare-metal/forge_scorecard.timer`,
  4 test files (25 new tests, all pass; full 85-test suite green).

**Not yet applied to any host as of tag time.** Fresh `install.sh` from
beta-1.10.15 gets everything at once. The registry cleanup runs on every
Forge startup — safe on a clean DB (no-op), collapses the fragmented
144-row registry to 16 canonical rows on a host that carried the pre-fix
state forward.

**Deliberately deferred to v2** (not in beta-1.10.15):
- Trend-based verdicts (`recovering`, `degrading`, `unstable`) — need
  `evaluation_history` to accumulate real depth first.
- Splitting `scoring.py` into three modules — do it the moment a second
  evaluator appears; splitting now is layering for its own sake.
- Reading strategies from `strategy_registry` on startup instead of always
  from `DEFAULT_STRATEGIES` — orthogonal to this work; the registry is now
  actually useful (canonical ids, current scorecard, evaluation history)
  but the code path that reads it back at startup is future work.

## 8. How to pick this back up

1. **Latest tag is `beta-1.10.15`** as of this writing — Forge improving
   loop (§7.10). Ignore the older references in this doc to "latest is
   1.10.9" — those pre-date §7.6 through §7.10. Ignore `beta-1.11` (see §2
   for the numbering trap).
2. Confirm what state the live host is actually in — don't assume the
   latest tag is deployed just because it's tagged. `git -C /opt/predictor
   rev-parse HEAD` on the live host against `beta-1.10.15`.
3. If the live `/api/chat` relay is still pointed at anything other than
   the isolated `/opt/predictor-metis` install, that's the next open item
   (§4, task #65).
4. Check the live `.env` for a stray `AGENT_RELAY_TIMEOUT_S=10` (§4).
5. beta-1.10.12/1.10.13/1.10.14/1.10.15 (loopback bind + firewall, nginx
   basic auth, data backup + candle history depth, Forge scoring loop —
   §7.6/§7.8/§7.10) are shipped/packaged but **not yet applied to the
   rehearsal host** as of this writing — a fresh `install.sh` run from
   beta-1.10.15 gets all of it at once.
6. Watch the first live scorecard runs on the rehearsal host once real
   trades accumulate — the 50-trade gate is a first-guess default. If most
   strategies stay at `not_enough_data` for longer than expected, either
   lower `FORGE_SCORECARD_MIN_TRADES` in `.env` or wait. If everything
   flags `losing` on real data, the threshold constants are wrong for
   this signal density — tune via env vars, don't re-code.
7. Anything new gets the full treatment from §5 — tag, package, verify by
   execution, archive, README entry. Don't skip steps under time pressure;
   that's exactly how the `beta-1.11` numbering trap in §2 happened.
