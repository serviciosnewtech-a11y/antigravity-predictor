# Antigravity Predictor Deployment Log

## 2026-07-18 — Self-deployable repo candidate

- Promoted verified Hermes-proxy deployment files from Testing release candidate.
- Added README.md, DEPLOYMENT.md, deploy.sh, diagnose.sh.
- Default inference path is Hermes proxy / OpenAI-compatible.
- Anthropic and Ollama are optional fallbacks only.
- No GitHub push performed from this environment.

## 2026-07-18 — Client-agnostic default correction

- Changed default `SA_INFERENCE_BACKEND` to `disabled`.
- Core Docker deploy no longer requires Hermes, Ollama, Anthropic, or any LLM.
- Hermes/OpenAI-compatible enrichment remains optional.
- Verified bash syntax, Python compile, compose config, and disabled-backend smoke.
- Fresh Docker rebuild still blocked in this shell by Docker socket permission.
