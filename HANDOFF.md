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

**Latest, current tag: `beta-1.10.9`, commit `6934b7b`.** This is what's
actually deployed/being deployed on the live host as of this handoff.

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

## 8. How to pick this back up

1. Confirm what state the live host is actually in — don't assume
   beta-1.10.9 is deployed just because it's the latest packaged tag; check
   `git -C /opt/predictor rev-parse HEAD` (or equivalent) on the live host
   against `6934b7b`.
2. If the live `/api/chat` relay is still pointed at anything other than the
   isolated `/opt/predictor-metis` install, that's the highest-priority
   open item (§4, task #65).
3. Check the live `.env` for a stray `AGENT_RELAY_TIMEOUT_S=10` (§4).
4. Anything new gets the full treatment from §5 — tag, package, verify by
   execution, archive, README entry. Don't skip steps under time pressure;
   that's exactly how the `beta-1.11` numbering trap in §2 happened.
