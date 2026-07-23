# Live deploy notes: getting /api/chat working on real bare-metal

Written 2026-07-23 after a real live deploy spent several hours chasing
`/api/chat` returning `agent_unavailable`, one layer at a time. Every fix
below is now baked into `install.sh`/`predictor.service`/`agent_relay.service`
as of beta-1.10.4/1.10.5 — a fresh install shouldn't hit any of these. This
file exists for two reasons: (1) if a live host is running an *older*
install and being patched by hand rather than reinstalled, this is the exact
sequence to work through, and (2) so the next time chat-backend wiring
breaks in some new way, whoever's debugging it isn't starting from zero.

## The chain, in the order it actually gets exercised

`predictor.service` reads `/opt/predictor/.env` → resolves a backend via
`CHAT_BACKEND`/`HERMES_PROXY_URL`/etc. (`src/llm_backend.py`) → for the
default local-relay pattern, POSTs to `agent_relay.service`
(`tools/agent_chat_relay.py`) → which shells out to whatever
`AGENT_RELAY_CMD` names → reply flows back the same path to the dashboard.
Every failure found live was a break somewhere in that chain, not in the
application logic at either end (`predictor_server.py`'s prompt assembly and
`agent_chat_relay.py`'s HTTP contract were both correct throughout).

## 1. `predictor.service` never loaded `.env` at all

Symptom: `.env` edited, `predictor.service` restarted, nothing changed —
`/api/chat` still unconfigured no matter what went into `.env`.

Cause: the unit had no `EnvironmentFile=` directive. `predictor_server.py`
does no dotenv loading of its own (`os.environ.get()` only), so whatever
wasn't already in systemd's own environment simply never arrived. Every
other unit (`signal_agent.service`) already had `EnvironmentFile=-/opt/predictor/.env`
— this one was just missed originally.

Fixed in the shipped `predictor.service` (beta-1.10.4). If patching an older
live install by hand: add `EnvironmentFile=-/opt/predictor/.env` under
`[Service]`, `systemctl daemon-reload`, restart.

## 2. `.env` itself can silently be empty/wrong-owned

Once `EnvironmentFile=` was added, the very next symptom looked identical
(still unconfigured) because `/opt/predictor/.env` turned out to be 0 bytes
and `root:root` — probably from an earlier `sudo` edit that created a fresh
empty file instead of editing the existing one, or a template regeneration
that didn't preserve prior content.

Always confirm directly before assuming a `.env` edit "didn't take":
```
ls -la /opt/predictor/.env
cat /opt/predictor/.env
```

## 3. Local agent relay was never wired into the systemd product at all

`tools/agent_chat_relay.py` (the no-API-key local CLI-agent backend) only
ever had a home in `run_monolith.sh` (the standalone launcher) — no service
unit, not even copied to `$APP_DIR` by `install.sh`, for the actual
systemd-based bare-metal product.

Fixed in beta-1.10.4: new `deploy/bare-metal/agent_relay.service`,
`install.sh` now copies `tools/` and installs/enables/starts the unit.

## 4. systemd doesn't inherit your interactive shell's PATH

Symptom: `/health` reports `agent_binary_exists: false` even for a binary
that works fine when Hermes (or whoever) runs it by hand.

Cause: a bare command name in `AGENT_RELAY_CMD` (e.g. `hermes --profile
metis chat -q {prompt}`) relies on PATH resolution, and systemd services get
a minimal default PATH — no `.bashrc`/`.profile` sourcing, no user-level
`~/.local/bin` unless it happens to already be on that minimal PATH.

Fix: always use an absolute path in `AGENT_RELAY_CMD`. Find it first with
`command -v <binary>` run as whichever user/session it already works under,
then use that full path, not the bare name.

## 5. `ProtectSystem=strict` blocks traversal into locked-down directories

Symptom: `agent_binary_exists: true` (absolute path found), but `live_ok:
false` with `exited with code 126: ... Permission denied`.

Cause (in the specific incident this file documents): the real agent
binary's wrapper `exec`'d into a venv under a directory that was `d---------`
(zero permissions, including for its own owner) — almost certainly
deliberate hardening on that agent's own private credential/session
directory. `agent_relay.service`'s `ProtectSystem=strict` only allows writes
to `ReadWritePaths` (`logs/`, `agent_state/`), but this was a *read/execute*
permission problem one layer below that, on a directory outside this
service's control entirely.

This is not a bug to patch reflexively by loosening the target directory's
permissions — that directory being locked down is very likely intentional,
and granting a public-dashboard-facing service account (`predictor`) access
into another agent's live credentialed session is a real security decision,
not a plumbing fix. See "What we actually did" below for how this incident
resolved it.

## 6. The `{prompt}` quoting trap — passes health check, fails on real chat

Symptom: `/health` shows `agent_binary_exists: true` AND `live_ok: true` —
looks fully verified — but a real `/api/chat` call still returns
`agent_unavailable`.

Cause: `AGENT_RELAY_CMD` had `{prompt}` wrapped in its own quotes, e.g.
`/bin/echo "[reply] {prompt}"`. The relay's substitution already applies a
safe `shlex.quote()` around the value — that's only safe when `{prompt}` is
the *outermost* quoting context in the template. Nested inside another
quote, the safety guarantee breaks the moment the real prompt contains an
embedded quote character. The health check's probe string is the literal
word `"ping"` — no quotes in it, sails through clean. A real chat request's
system prompt is `_CRYPTO_OPERATOR_SYSTEM_PROMPT` plus live context, and it
contains actual embedded quotes (e.g. `predictor_server.py`'s own boundary
line: `No directive financial instructions ("comprá ahora")`). That quote
prematurely closes the template's wrapping quote, the resulting shell
command is a syntax error, the subprocess exits non-zero, the relay reports
a real failure, and `/api/chat` correctly (if confusingly) reports
`agent_unavailable` — while `/health` keeps reporting green, because it
never tested with a prompt that looks like a real one.

Fixed defensively in beta-1.10.5: `agent_chat_relay.py` now prints a loud
startup warning if a quote character sits immediately against either side
of `{prompt}` in `AGENT_RELAY_CMD`. It can't catch every variant (a quote
elsewhere in the template that still encloses the placeholder), so the rule
of thumb is: **never put quotes around `{prompt}` in the template at all.**

```
Correct: AGENT_RELAY_CMD=/path/to/binary --profile metis chat -q {prompt}
Wrong:   AGENT_RELAY_CMD=/bin/echo "[reply] {prompt}"
```

## What we actually did, this incident

Given #5 (locked-down private agent directory) and #6 (quoting trap) both
needed resolving to prove the interface actually worked, and reaching into
another agent's live credentialed session wasn't something to decide on the
fly mid-incident: the interface/control chain was proven end-to-end using a
harmless stub (`AGENT_RELAY_CMD=/bin/echo [relay-test reply] {prompt}`, no
wrapping quotes) instead of the real agent binary. `/health` showed
`true`/`true`, and a real `/api/chat` call returned a genuine (if canned)
reply instead of `agent_unavailable` — confirming every link in the chain
(`predictor` → `.env` → `agent_relay.service` → relay subprocess → reply
back through `/api/chat`) works correctly.

Wiring the *real* agent back in (a properly scoped, credentialed profile
that doesn't require reaching into another agent's private session
directory) is tracked separately — see the repo's task list / commit history
around "de-scope agent_relay from full Hermes session to isolated profile."
Swapping the stub for a real, correctly-scoped `AGENT_RELAY_CMD` is a
one-line `.env` change once that profile exists; it does not require
re-proving any of the plumbing above.
