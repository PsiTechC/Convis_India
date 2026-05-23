# Convis — Claude Code Project Context

This document orients Claude Code (and any AI coding assistant) when opening this repo on a new machine. Read this first.

## What is Convis?

Convis is an AI voice agent platform — businesses deploy "lifelike" conversational voice agents to handle inbound and outbound phone calls. Use cases: dental clinic receptionist, real-estate lead qualification, ecommerce customer support, B2B sales follow-up. Marketing site: https://convis.ai/

The voice pipeline is: **Twilio (PSTN) → LiveKit Cloud (SIP/media) → our agent worker (Fargate) → Deepgram (ASR) + OpenAI (LLM) + ElevenLabs or Cartesia (TTS) → back to caller.** All providers are cloud APIs.

## Repo layout

| Path | What |
|---|---|
| `convis-api/` | **FastAPI** backend. CRUD for assistants, calls, users; webhook receivers; campaign scheduler; LLM cache warmer. Deployed on AWS App Runner. |
| `convis-api/app/services/livekit/agent_worker.py` | The actual voice agent (LiveKit Agents framework). Runs as a separate ECS Fargate task — NOT inside the API. Joins rooms, runs the ASR→LLM→TTS pipeline. |
| `convis-api/app/services/livekit/assistant_config.py` | Loads an assistant's config from MongoDB into the agent runtime config. Whitelists/coerces unsupported provider values. |
| `convis-api/app/services/llm_cache_warmer.py` | Background loop in API process — every 4 minutes fires a 1-token completion per unique assistant prompt, keeping OpenAI's prompt cache warm. |
| `convis-web/` | **Next.js 14** frontend (App Router, `'use client'` heavy). Dashboard for managing assistants, voices, calls. Deployed on App Runner. |
| `convis-web/app/ai-agent/page.tsx` | Assistant CRUD page (~2,800 lines, single client component — known too-big-to-be-fast issue). |
| `deployment-docs/` | Existing AWS + LiveKit setup docs. Read `aws/AWS_APP_RUNNER.md` first. |
| `bolna-master/`, `n8n-custom-nodes/` | Adjacent / older code — not part of the live system. Ignore. |

## Production stack (LOCKED — do not regress)

| Component | Value | Why |
|---|---|---|
| ASR | Deepgram `nova-2-phonecall`, lang=en | Best WER on 8 kHz PSTN audio |
| LLM | OpenAI `gpt-4o-mini` | 3-5× faster than `gpt-4-turbo`; supports prompt caching |
| TTS | ElevenLabs `eleven_flash_v2_5` (default) or Cartesia Sonic-2 (cheaper) | Streaming, ~150ms TTFB |
| VAD | Silero (prewarmed in `prewarm_fnc`) | |
| Telephony | Twilio Programmable Voice + LiveKit SIP | |

**Knobs in `agent_worker.py` — these are tuned, do not casually change:**

```python
DEFAULT_DG_ENDPOINTING_MS = 130       # Deepgram silence threshold
DEFAULT_MIN_ENDPOINTING_DELAY = 0.08  # AgentSession turn-end debounce
DEFAULT_MIN_INTERRUPTION_DURATION = 0.4  # min user speech for barge-in
DEFAULT_LLM_MAX_TOKENS = 120  # response cap (per-assistant override available)
```

Per-assistant overrides come through `assistant_config.py` and live in Mongo on the assistant doc:
- `min_interruption_duration` (typical: `0.25` for snappy barge-in)
- `min_endpointing_delay` (typical: `0.15` — aggressive 0.06 cuts off slow speakers)
- `asr_endpointing_ms` (typical: `200` — reliable; `130` is faster but riskier)
- `llm_model`, `llm_max_tokens`, `expressive_mode`, `tts_stability`, `tts_speed` etc.

## Critical pitfalls (learned the hard way)

### 1. ElevenLabs `eleven_v3` is NOT supported by livekit-plugins-elevenlabs streaming WSS
The `multi-stream-input` endpoint returns **403** for `model_id=eleven_v3`. v3 is alpha and only available via non-streaming HTTP. **`assistant_config.py` defensively coerces `eleven_v3` → `eleven_flash_v2_5`** with a warning log. Don't undo that coercion unless the plugin gains v3 streaming support.

### 2. `docker buildx ... --push` can silently fail to write the manifest
Build reports success, image is "pushed", but `aws ecr describe-images --image-ids imageTag=<TAG>` returns `ImageNotFoundException`. App Runner / ECS will then crash-loop on `CannotPullContainerError`.

**Always verify:**
```bash
aws ecr describe-images --repository-name convis-api --image-ids imageTag=$TAG --query 'imageDetails[0].imageTags'
```
**before** rolling. If missing, just `docker buildx build ... --push` again — layers cache, it's fast.

### 3. The assistant edit form has a stale-cache form-clobber risk (now fixed but watch for regressions)
`convis-web/app/ai-agent/page.tsx` PUT/POST payloads previously **hardcoded** `llm_model: 'gpt-4-turbo'` and `llm_max_tokens: 150` regardless of `formData`. Every save reverted Mongo to the slow path. **Fixed (2026-04-29):** payloads now use `formData.llm_model || 'gpt-4o-mini'` and `formData.llm_max_tokens ?? 80`. If you see Mongo regressing to gpt-4-turbo, check whether someone reverted these edits.

### 4. OpenAI prompt cache requires explicit `prompt_cache_key`
Auto-cache inference was unreliable across the bare-SDK warmer vs livekit's wrapped chat() format → cache misses. **Both** the agent's `openai.LLM(...)` and the `_warm_llm_cache` / `llm_cache_warmer` use `prompt_cache_key=<assistant_id>` so they hit the same bucket. If you change the warmer, keep the cache key consistent or cache will silently miss.

### 5. ECS agent task model
Each call spawns a fresh **worker process** within the ECS task (not a fresh container). `WorkerOptions(num_idle_processes=1)` keeps one process always loaded so call-accept skips Silero/module/client init. The `prewarm_fnc` runs once per process and loads VAD + warms TLS/DNS. The per-job `_warm_llm_cache` fires concurrent with the greeting to populate OpenAI cache before the user's first turn.

## Common operational tasks

### Update one assistant's config in Mongo (when the form is unsafe to use)
```python
# Inside conda env "gen" or your local Python with pymongo + python-dotenv
from dotenv import load_dotenv; load_dotenv('convis-api/.env')
import os
from pymongo import MongoClient
from bson import ObjectId
db = MongoClient(os.environ['MONGODB_URI'])[os.environ['DATABASE_NAME']]
db.assistants.update_one(
    {'_id': ObjectId('<assistant_id>')},
    {'$set': {'llm_model': 'gpt-4o-mini', 'llm_max_tokens': 80}}
)
```

### Build + roll the agent (single-line summary; full docs in `deployment-docs/aws/`)
```bash
TS=feat-$(date +%s)
cd convis-api && docker buildx build --platform linux/amd64 --provenance=false \
  -t 942617679452.dkr.ecr.us-east-1.amazonaws.com/convis-api:$TS \
  -t 942617679452.dkr.ecr.us-east-1.amazonaws.com/convis-api:latest \
  --push .
# verify image is in ECR (silent-push bug):
aws ecr describe-images --repository-name convis-api --image-ids imageTag=$TS
# register new task def + roll service (see existing td json or AWS_APP_RUNNER.md)
```

### Tail agent logs
```bash
aws logs tail /ecs/convis-livekit-agent --since 5m --format short \
  | grep -E 'Job received|LLMMetrics|TTSMetrics|EOUMetrics|ERROR|WARN' \
  | grep -v VADMetrics | tail -40
```

### Verify cache warmer is firing
```bash
aws logs tail "/aws/apprunner/convis-api/c76a998b086b43a4b3468fa0ad0c93e4/application" --since 10m \
  | grep LLM_CACHE_WARMER
```

## Streaming pipeline — confirm everything streams

In `agent_worker.py`:
- STT: `deepgram.STT(interim_results=True, no_delay=True, ...)` ✓
- LLM: `openai.LLM(...)` defaults to streaming ✓
- TTS: `elevenlabs.TTS(...)` uses `multi-stream-input` WSS ✓

If you see audible robotic gaps mid-response, suspect either (a) a 39-second monologue exceeding TTS buffer, or (b) ElevenLabs WSS dropped mid-call (look for `websocket closed unexpectedly` in agent logs).

## What's deployed where

| Service | Infra | Image / Task | Public URL |
|---|---|---|---|
| `convis-api` | App Runner (us-east-1) | latest tag in ECR | `https://c8nkbpp2n4.us-east-1.awsapprunner.com` |
| `convis-web` | App Runner (us-east-1) | latest tag in ECR | `https://dvniu3gjgr.us-east-1.awsapprunner.com` |
| CloudFront in front of `convis-web` | edge global | dist `EPQNX2WRK7963` | `https://d2slsogs01yy9r.cloudfront.net` |
| `convis-livekit-agent` | ECS Fargate (cluster `convis`, us-east-1) | task def `convis-livekit-agent:NN` | n/a (joins LiveKit rooms outbound) |
| Mongo | MongoDB Atlas | shared with other projects | URI in `.env` |

`webapp.convis.ai` currently points to a Hostinger VPS in NL (72.60.203.40) that proxies to App Runner via nginx — proxy is broken (502). Fix is either repair the VPS nginx or move DNS to point at CloudFront via ACM certificate (TODO).

## Conventions

- **Don't** create new `*.md` files in this repo unless explicitly asked.
- **Don't** edit `convis-web/app/ai-agent/page.tsx` lightly — it's 2,800+ lines, single client component; small changes can have wide blast radius.
- **Verify ECR push** every time before rolling (silent-push bug).
- **Refresh the dashboard browser tab** before opening the assistant edit form (the form caches stale state).
- For voice-call latency tuning, the user's threshold is roughly: any single turn taking >2.5s post-stop is "too slow" and worth investigating.

## Where to look first

- "Why is the call slow?" → tail agent logs for LLMMetrics. Check `prompt_cached_tokens`, `duration`, `transcription_delay`. See `HANDOFF.md` § "Latency debugging cheat-sheet".
- "Why is the call silent / disconnecting?" → search agent logs for `WSServerHandshakeError`, `websocket closed unexpectedly`, `eleven_v3` in URL.
- "Why did my Mongo edit revert?" → `convis-web/app/ai-agent/page.tsx` form save payloads. Should NOT contain hardcoded `llm_model: 'gpt-4-turbo'`.
- "Where do I deploy a new image?" → `deployment-docs/aws/AWS_APP_RUNNER.md`.
- "Where is the LiveKit / Twilio infrastructure setup?" → `deployment-docs/LIVEKIT_SIP_SETUP.md`.
