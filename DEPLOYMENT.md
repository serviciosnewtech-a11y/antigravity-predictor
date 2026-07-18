# Antigravity Predictor — Self-contained Deployment

## Target-agent tasklist

1. Extract the release folder.
2. `cd` into the folder.
3. Review `.env.example`; for demo keep `DRY_RUN=true` and exchange keys empty.
4. Run `bash deploy.sh`.
5. If blocked, run `bash diagnose.sh` and return all output.

## Default inference

Default client deployment is LLM/agent agnostic. It does not require Hermes,
Anthropic, Ollama, or any target-side agent:

```env
SA_INFERENCE_BACKEND=disabled
```

Optional Hermes/OpenAI-compatible enrichment:

```env
SA_INFERENCE_BACKEND=openai_compatible
HERMES_PROXY_URL=http://host.docker.internal:8645/v1
HERMES_INFERENCE_MODEL=stepfun/step-3.7-flash:free
HERMES_PROXY_API_KEY=local
```

Anthropic and Ollama are optional advanced fallbacks only.

## Pass criteria

- Docker Compose config is valid.
- Dashboard, predictor, executor, forge, and signal_agent are up.
- `http://localhost/api/status` returns `status: online`.
- Signal-agent logs show `Backend: disabled` for default client mode, or the explicitly configured optional backend.
