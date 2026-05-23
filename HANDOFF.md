# Handoff — Moving Convis to a New Machine

Step-by-step for setting up Convis on a new development PC where you (or another developer) will continue work. Assumes the new PC will use the same AWS account and the same production deployment.

## Phase 1 — What to copy

### From the old machine

```
Convis-main/                           # the whole repo
├── convis-api/.env                    # ⚠️ HAS SECRETS — copy securely (1Password / scp / USB)
├── convis-api/.env.production.example # template (no secrets)
├── convis-web/.env.local              # local dev config (NEXT_PUBLIC_API_URL etc)
├── convis-web/.env.production         # prod config
└── (everything else — code, docs)
```

**Skip:**
- `convis-api/venv/`, `convis-web/node_modules/`, `convis-web/.next/` — regenerate on new machine
- `convis-api/__pycache__`, `convis-api/.pytest_cache`
- Any `.bak`, `.backup` files unless explicitly needed

### Recommended packaging

```bash
cd /Users/psitech/Desktop/Psitech/
tar --exclude='Convis-main/convis-api/venv' \
    --exclude='Convis-main/convis-web/node_modules' \
    --exclude='Convis-main/convis-web/.next' \
    --exclude='Convis-main/**/__pycache__' \
    --exclude='Convis-main/**/.pytest_cache' \
    -czf convis-handoff.tar.gz Convis-main/
```

Result: ~50-100 MB tarball. Transfer over USB or Drive (NOT email — has secrets).

### Out-of-band: things to share securely

These aren't in the repo — share via 1Password, signal, etc:

- AWS access keys (or IAM user creds) for account `942617679452`
- The `MONGODB_URI` if not already in `.env`
- API keys (OpenAI, Deepgram, ElevenLabs, Twilio, LiveKit, Google) — already in `.env`
- LiveKit `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`
- SMTP credentials if used

## Phase 2 — On the new machine

### 1. Install prerequisites

```bash
# macOS (Homebrew)
brew install --cask docker
brew install awscli node@20 miniconda
# Docker Desktop must be running before you build any image

# Or Linux:
sudo apt install docker.io awscli nodejs npm
```

Python via miniconda (matches the old setup):
```bash
conda create -n gen python=3.12
conda activate gen
```

### 2. Configure AWS

```bash
aws configure
# Region: us-east-1
# Account: 942617679452
# Verify:
aws sts get-caller-identity
aws ecr describe-repositories --query 'repositories[].repositoryName'
# Should list: convis-api, convis-web
```

### 3. Unpack the repo

```bash
mkdir -p ~/Desktop/Psitech && cd ~/Desktop/Psitech
tar -xzf ~/Downloads/convis-handoff.tar.gz
cd Convis-main
```

### 4. Drop in the env files

If `.env` came in the tarball, you're set. If not:
```bash
cp convis-api/.env.production.example convis-api/.env
# Edit and fill in: MONGODB_URI, OPENAI_API_KEY, DEEPGRAM_API_KEY, ELEVEN_LABS_API_KEY,
# CARTESIA_API_KEY, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, TWILIO_*, etc.
```

For the webapp:
```bash
cat > convis-web/.env.local << 'EOF'
NEXT_PUBLIC_API_URL=https://c8nkbpp2n4.us-east-1.awsapprunner.com
EOF
```

### 5. Install dependencies

```bash
# API
cd convis-api && pip install -r requirements.txt && cd ..
# Webapp
cd convis-web && npm install && cd ..
```

### 6. Verify everything works

```bash
# API health (against production):
curl https://c8nkbpp2n4.us-east-1.awsapprunner.com/health
# Web (production via CloudFront):
curl -I https://d2slsogs01yy9r.cloudfront.net/

# Local API:
cd convis-api && uvicorn app.main:app --reload --port 8000
# In another terminal:
cd convis-web && npm run dev
# Visit http://localhost:3000
```

### 7. Test ECR + Docker buildx

```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 942617679452.dkr.ecr.us-east-1.amazonaws.com
# Should print "Login Succeeded"
docker buildx ls
# Should show a builder with "linux/amd64" platform support
```

If buildx isn't available:
```bash
docker buildx create --name multiarch --use
docker buildx inspect --bootstrap
```

### 8. Set up Claude Code (if using AI assistance)

Open the repo in your IDE. If using Claude Code:
- Claude Code will auto-load **`CLAUDE.md`** from the repo root — that's intentional.
- For Anthropic API access: set `ANTHROPIC_API_KEY` env var.

The `CLAUDE.md` includes critical pitfalls, locked production defaults, and operational tasks. Read it before making changes.

## Phase 3 — First production deploy from the new machine

(Only if you're going to roll an image; full guide in [`deployment-docs/aws/AWS_APP_RUNNER.md`](./deployment-docs/aws/AWS_APP_RUNNER.md).)

### Build + push API image
```bash
TS=handoff-test-$(date +%s)
cd convis-api
docker buildx build --platform linux/amd64 --provenance=false \
  -t 942617679452.dkr.ecr.us-east-1.amazonaws.com/convis-api:$TS \
  -t 942617679452.dkr.ecr.us-east-1.amazonaws.com/convis-api:latest \
  --push .

# ⚠️ ALWAYS verify the image actually landed:
aws ecr describe-images --repository-name convis-api --image-ids imageTag=$TS \
  --query 'imageDetails[0].imageTags'
# If empty: re-run the buildx push (silent-push bug).
```

Don't roll prod for a "test" image — just push it to verify your toolchain works, then delete the tag:
```bash
aws ecr batch-delete-image --repository-name convis-api --image-ids imageTag=$TS
```

## Recovery cheat sheet — common operational tasks

### Tail agent logs
```bash
aws logs tail /ecs/convis-livekit-agent --since 5m --format short \
  | grep -E 'Job received|LLMMetrics|TTSMetrics|EOUMetrics|ERROR|WARN' \
  | grep -v VADMetrics | tail -40
```

### Tail API logs (cache warmer, exceptions)
```bash
aws logs tail "/aws/apprunner/convis-api/c76a998b086b43a4b3468fa0ad0c93e4/application" \
  --since 10m --format short | grep -E 'LLM_CACHE_WARMER|ERROR|WARN'
```

### Rollback a deploy
```bash
# App Runner — point image identifier back at previous tag
# (use `aws apprunner list-operations` to find the prior tag)
# OR for ECS agent — register prior task def revision and update-service:
aws ecs update-service --cluster convis --service convis-livekit-agent \
  --task-definition convis-livekit-agent:<PRIOR_REV> --force-new-deployment
```

### Edit assistant config in Mongo (when webapp form is unsafe to use)
```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate gen
python << 'PY'
from dotenv import load_dotenv; load_dotenv('convis-api/.env')
import os
from pymongo import MongoClient
from bson import ObjectId
db = MongoClient(os.environ['MONGODB_URI'])[os.environ['DATABASE_NAME']]
db.assistants.update_one(
    {'_id': ObjectId('<id>')},
    {'$set': {'llm_model': 'gpt-4o-mini', 'llm_max_tokens': 80, 'expressive_mode': False}}
)
PY
```

## Latency debugging cheat-sheet

If a call feels slow, pull the last call's metrics and check:

| Symptom | Where to look | Likely cause |
|---|---|---|
| `prompt_cached_tokens: 0` on every turn | LLMMetrics in agent log | Cache key mismatch — verify `prompt_cache_key=<assistant_id>` is being sent. Or assistant's `system_message` recently changed and cache hasn't repopulated yet. |
| `LLMMetrics duration > 2s` consistently | LLMMetrics + check `llm_model` in Mongo | Likely on `gpt-4-turbo` instead of `gpt-4o-mini` (form-clobber regression). |
| `transcription_delay > 1s` | EOUMetrics | Deepgram slow on this audio — caller's network/mic, not our config. Intermittent. |
| `audio_duration > 25s` per turn | TTSMetrics | LLM is monologuing. Reduce `llm_max_tokens` (typical good value: 80). |
| TTS ttfb > 0.3s | TTSMetrics | ElevenLabs slow / WSS reconnecting. Check for `websocket closed unexpectedly`. |
| Long gap from `EOU` to first audio | timestamps between EOUMetrics and TTSMetrics | LLM TTFT high — usually cache miss or large prompt. |

## Known issues / unfinished work

These were in flight as of 2026-04-30 and may need follow-up:

1. **`webapp.convis.ai` returns 502** — points to a Hostinger VPS (72.60.203.40, NL) running nginx that can't reach upstream. Either fix the VPS proxy config, or move DNS to point at CloudFront via an ACM certificate (preferred — simpler architecture).

2. **`/ai-agent` page is 1.68 MB JS** — `convis-web/app/ai-agent/page.tsx` is 2,800 lines as a single client component. Code-split refactor (lazy-load the edit modal, split tabs) hasn't shipped yet. Effort: ~4-8 hours. Net result: initial bundle ~400 KB instead of 1.68 MB.

3. **App Runner web instance is 2 vCPU / 4 GB** (bumped from 1 / 2). Didn't materially help TTFB — the bottleneck on `/ai-agent` is JS hydration on the client, not server SSR. Could downgrade back to save cost (~$45/mo) without losing perceived speed.

4. **CloudFront origin request policy** is `AllViewerExceptHostHeader` — set this way because App Runner's Envoy returns 404 if it sees an unrecognized Host header. Don't switch back to `AllViewer` without re-checking that App Runner accepts the CloudFront domain.

5. **OpenAI prompt cache** is keyed by `prompt_cache_key=<assistant_id>`. The 4-min API warmer fires for ALL assistants in Mongo. If you onboard many more assistants, watch the OpenAI bill — each warmed prompt is ~$0.20/day.

## Contact / escalation

- **AWS account owner:** see PsiTech root user
- **LiveKit project:** see LiveKit Cloud dashboard (project key in `.env`)
- **Twilio account:** owner per Twilio console
- **Domain registrar:** convis.ai DNS — check current registrar before any DNS change
