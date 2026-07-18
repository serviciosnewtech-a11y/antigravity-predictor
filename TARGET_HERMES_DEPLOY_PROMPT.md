# Target Hermes Deploy Prompt — Antigravity Predictor

Paste this into Hermes on the test PC after placing the release tarball on that machine.

```text
You are the execution worker on this test PC. Deploy the Antigravity Predictor package from the provided tarball. Do not redesign the app. Do not enable live trading. Keep DRY_RUN=true. Treat Hermes/Ollama inference as optional: core dashboard must deploy even if no LLM backend is available.

Package path: <ABSOLUTE_PATH_TO_TARBALL>
Expected SHA256: <SHA256_FROM_SHA256SUMS>
Install root: ~/antigravity-predictor-test

Steps:
1. Create/reuse ~/antigravity-predictor-test.
2. Extract the tarball there so docker-compose.yml, deploy.sh, diagnose.sh, and README.md are at the install root.
3. Verify SHA256 against SHA256SUMS.txt if present.
4. Run: bash deploy.sh
5. If deploy.sh blocks, run: bash diagnose.sh
6. Report only concrete evidence:
   - pwd
   - sha256 verification result
   - docker compose ps
   - curl http://localhost/api/status result
   - curl http://localhost:18910/api/status result
   - dashboard URL shown by deploy.sh
   - whether Spanish/English UI toggle is visible
   - whether chat shows honest unavailable state or a real backend reply

Stop conditions:
- DRY_RUN is not true.
- Exchange API keys are non-empty.
- Required ports are held by unrelated processes.
- Docker is unavailable and cannot be accessed via direct user or sg docker.
- Any command would require sudo or destructive host changes.
```

## Optional LLM/Hermes enhancement

Only after the core app is deployed, the operator may choose to enable chat/enrichment by editing `.env`:

```env
SA_INFERENCE_BACKEND=openai_compatible
HERMES_PROXY_URL=http://host.docker.internal:8645/v1
HERMES_INFERENCE_MODEL=<operator-approved-model>
HERMES_PROXY_API_KEY=local
```

Then rerun:

```bash
bash deploy.sh
```

If no backend is configured, the app must remain usable and show an honest unavailable/disabled chat state; it must not fake replies.
