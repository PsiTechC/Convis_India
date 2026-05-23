# Convis

AI voice agent platform — businesses deploy lifelike conversational agents to handle inbound and outbound phone calls. Marketing site: https://convis.ai/

```
PSTN ─► Twilio ─► LiveKit Cloud (SIP) ─► Agent (ECS Fargate) ─┬─► Deepgram (ASR)
                                                                ├─► OpenAI (LLM)
                                                                └─► ElevenLabs / Cartesia (TTS)
```

## Repo layout

| Path | What |
|---|---|
| [`convis-api/`](./convis-api/) | FastAPI backend (assistants CRUD, webhooks, campaigns) — App Runner |
| [`convis-api/app/services/livekit/agent_worker.py`](./convis-api/app/services/livekit/agent_worker.py) | LiveKit Agent voice pipeline — runs as ECS Fargate task, NOT inside the API |
| [`convis-web/`](./convis-web/) | Next.js 14 dashboard — App Runner (also fronted by CloudFront) |
| [`deployment-docs/`](./deployment-docs/) | AWS + LiveKit setup guides |
| [`CLAUDE.md`](./CLAUDE.md) | Context for Claude Code / AI assistants — read first if using AI tooling |
| [`HANDOFF.md`](./HANDOFF.md) | What to copy / set up when moving the repo to a new machine |

## Quick start (local dev)

### Prereqs
- macOS or Linux
- Python 3.12 (recommend conda env, e.g. `conda create -n gen python=3.12`)
- Node 18+ for the webapp
- Docker Desktop (for building images, deploying)
- AWS CLI v2 configured with admin credentials for the Convis AWS account
- A `convis-api/.env` file (copy from `.env.production.example`, fill in real values — see HANDOFF.md)
- A `convis-web/.env.local` file with `NEXT_PUBLIC_API_URL`

### Run the API locally
```bash
cd convis-api
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Run the dashboard locally
```bash
cd convis-web
npm install
npm run dev    # http://localhost:3000
```

### Run the LiveKit agent worker locally
The agent connects out to LiveKit Cloud — you don't need to expose any ports.
```bash
cd convis-api
python -m app.services.livekit.agent_worker dev
```
For inbound PSTN calls to actually route to your local worker, register a separate dev `LIVEKIT_AGENT_NAME` so prod traffic isn't affected.

## Deploying

The full deploy guide is **[`deployment-docs/aws/AWS_APP_RUNNER.md`](./deployment-docs/aws/AWS_APP_RUNNER.md)** — read it end-to-end the first time. Summary:

| Service | Infra | How to roll |
|---|---|---|
| `convis-api` | App Runner | `docker buildx build --push` to ECR → `aws apprunner update-service --source-configuration ...` |
| `convis-web` | App Runner | same pattern, separate ECR repo |
| `convis-livekit-agent` | ECS Fargate (cluster `convis`) | `docker build --push` (uses convis-api image) → `register-task-definition` → `update-service --force-new-deployment` |

**⚠️ Always verify the image is actually in ECR** before rolling — `docker buildx --push` can silently fail to write the manifest:
```bash
aws ecr describe-images --repository-name convis-api --image-ids imageTag=$TAG
```

## Production stack (locked — see [CLAUDE.md](./CLAUDE.md) for rationale)

- **ASR:** Deepgram nova-2-phonecall
- **LLM:** OpenAI gpt-4o-mini (NOT gpt-4-turbo — 3-5× slower)
- **TTS:** ElevenLabs eleven_flash_v2_5 (default) or Cartesia Sonic-2
- **VAD:** Silero (prewarmed at worker boot)
- **Telephony:** Twilio + LiveKit Cloud SIP

## Where to find things

| Question | File |
|---|---|
| How are calls dispatched? | `convis-api/app/services/livekit/sip_service.py` + Twilio webhooks under `convis-api/app/routes/twilio_webhooks/` |
| Why is my LLM slow / how does prompt caching work? | `convis-api/app/services/llm_cache_warmer.py` and `agent_worker.py:_warm_llm_cache` |
| How does the agent join a LiveKit room? | `convis-api/app/services/livekit/agent_worker.py` `entrypoint()` |
| How is an assistant's config loaded into the agent? | `convis-api/app/services/livekit/assistant_config.py` |
| How does barge-in (interruption) work? | `agent_worker.py` — `min_interruption_duration` + Silero VAD events |
| How do I add CORS for a new domain? | App Runner env var `CORS_ORIGINS` on `convis-api` service |
| How do I trim webapp bundle size? | `convis-web/app/ai-agent/page.tsx` is 2,800 lines — code-splitting refactor not yet done |

## Important docs

- **[CLAUDE.md](./CLAUDE.md)** — full project context for AI assistants (also useful for humans). Includes critical pitfalls + tuning rationale.
- **[HANDOFF.md](./HANDOFF.md)** — PC migration: what to copy, where to put credentials, first-run checklist.
- **[deployment-docs/aws/AWS_APP_RUNNER.md](./deployment-docs/aws/AWS_APP_RUNNER.md)** — full AWS deploy guide.
- **[deployment-docs/LIVEKIT_SIP_SETUP.md](./deployment-docs/LIVEKIT_SIP_SETUP.md)** — LiveKit + Twilio SIP trunk setup.

## Support

- AWS account: 942617679452 (us-east-1)
- ECS cluster: `convis`
- ECR repos: `convis-api`, `convis-web`
- Mongo: Atlas (URI in `.env`)
