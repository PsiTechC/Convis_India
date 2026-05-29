# Convis on AWS — ECS Express Mode in ap-south-1

This doc replaces `AWS_APP_RUNNER.md` for new deployments. App Runner is in maintenance mode as of April 30, 2026, so we use **Amazon ECS Express Mode** instead. All production resources live in **ap-south-1 (Mumbai)** for an India-native deployment.

## What you'll end up with

| Service | Platform | Region | Purpose |
|---|---|---|---|
| `convis-api` | ECS Express Mode (Fargate + ALB) | ap-south-1 | FastAPI HTTP backend |
| `convis-web` | ECS Express Mode (Fargate + ALB) | ap-south-1 | Next.js dashboard |
| `convis-livekit-agent` | ECS Fargate (vanilla service) | ap-south-1 | Voice agent worker |

All three services share one Application Load Balancer (Express Mode pools them automatically within a VPC).

**Account:** `942617679452` · **Region:** `ap-south-1` · **Cluster:** `default`

---

## Conventions used in this doc

- All values shown as `<your value>` come from `convis-api/.env` or `convis-web/.env.local` — never check the real values into git.
- Console clicks are written as `Console → ECS → Express mode → Create service`.
- All CLI commands assume PowerShell on Windows.
- **Plain env vars only** — no Secrets Manager wiring in the task definitions. The team accepted this trade-off after Secrets Manager + cross-region IAM caused multiple rounds of debugging.

---

## Part 0 — Fix your `.env` files before pasting them anywhere

### convis-api/.env

| Variable | Current | Change to |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` |
| `API_BASE_URL` | ngrok URL | leave placeholder for first deploy; update with ECS URL in Part 3 |
| `BASE_URL` | ngrok URL | same |
| `OUTBOUND_TWIML_URL` | `${API_BASE_URL}/...` | resolved URL after Part 3 (ECS does not expand `${...}`) |
| `TW_STATUS_CALLBACK` | `${API_BASE_URL}/...` | resolved URL after Part 3 |
| `TW_RECORDING_CALLBACK` | `${API_BASE_URL}/...` | resolved URL after Part 3 |
| `LIVEKIT_SIP_INBOUND_HOST ` | trailing space in key | trim the trailing space |
| `LIVEKIT_SIP_OUTBOUND_TRUNK_ID ` | trailing space in key | trim the trailing space |

### convis-web/.env.production

This file is read by `npm run build` and the resulting `NEXT_PUBLIC_*` values are **baked into the JavaScript bundle**. Changing it requires a rebuild of the convis-web image.

Update **before** building the convis-web image:
```env
NEXT_PUBLIC_API_URL=<ap-south-1 convis-api URL — from Part 2>
NEXT_PUBLIC_API_BASE_URL=<same>
NEXT_PUBLIC_N8N_URL=/n8n
```

### convis-web/.env.local

This file is for local dev only. It is NOT used by the production build. Leave it alone.

---

## Part 1 — Confirm or push images to ap-south-1 ECR

```powershell
aws ecr describe-images --repository-name convis-api --region ap-south-1 --query 'imageDetails[*].imageTags' --output table
aws ecr describe-images --repository-name convis-web --region ap-south-1 --query 'imageDetails[*].imageTags' --output table
```

If both show `latest` (and ideally a git SHA tag), skip to Part 2.

If either is empty, push from the us-east-1 image:

```powershell
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin 942617679452.dkr.ecr.ap-south-1.amazonaws.com

docker pull 942617679452.dkr.ecr.us-east-1.amazonaws.com/convis-api:latest
docker tag 942617679452.dkr.ecr.us-east-1.amazonaws.com/convis-api:latest 942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-api:latest
docker push 942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-api:latest

docker pull 942617679452.dkr.ecr.us-east-1.amazonaws.com/convis-web:latest
docker tag 942617679452.dkr.ecr.us-east-1.amazonaws.com/convis-web:latest 942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-web:latest
docker push 942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-web:latest
```

Note: the convis-web image you just pushed was built with the **old us-east-1 `NEXT_PUBLIC_API_URL` baked in**. Part 4 below rebuilds it with the correct ap-south-1 URL.

---

## Part 2 — Deploy convis-api on Express Mode (first pass, placeholder URLs)

**Console → ECS (ap-south-1) → Express mode → Create service.**

| Field | Value |
|---|---|
| Service name | `convis-api` |
| Cluster | `default` |
| Image | `942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-api:latest` (use the Browse ECR picker) |
| Container port | `8000` |
| Health check path | `/health` |
| CPU | `1 vCPU` |
| Memory | `2 GB` |
| Command | leave blank (the Dockerfile CMD runs gunicorn — that's what you want) |
| Networking | default VPC, all subnets (multi-AZ), default SG, assign public IP **enabled** |
| Task execution role | `ecsTaskExecutionRole` |
| Task role | leave empty |
| Secrets section | **leave completely empty** |

### Environment variables for convis-api

Paste all of these as plain key/value. Most values come from `convis-api/.env`.

```env
ENVIRONMENT=production
MONGODB_URI=<your value>
DATABASE_NAME=convis_india
EMAIL_USER=no-reply@convis.ai
EMAIL_PASS=<your value>
SMTP_HOST=<your value>
SMTP_PORT=<your value>
SMTP_USE_SSL=true
FRONTEND_URL=https://webapp.convis.ai
CORS_ORIGINS=https://webapp.convis.ai,https://api.convis.ai
ENCRYPTION_KEY=<your value>
JWT_SECRET=<your value>
OPENAI_API_KEY=<your value>
REDIS_URL=redis://redis-10185.c258.us-east-1-4.ec2.redns.redis-cloud.com:10185
GOOGLE_CLIENT_ID=<your value>
GOOGLE_CLIENT_SECRET=<your value>
GOOGLE_REDIRECT_URI=https://api.convis.ai/api/calendar/google/callback
DEFAULT_TIMEZONE=Asia/Kolkata
DEFAULT_MAX_ATTEMPTS=3
DEFAULT_RETRY_DELAYS=15,60,1440
ENABLE_CALENDAR_BOOKING=true
ENABLE_POST_CALL_AI=true
ENABLE_AUTO_RETRY=true
CAMPAIGN_DISPATCH_INTERVAL_SECONDS=1
CELERY_BROKER_URL=redis://redis-10185.c258.us-east-1-4.ec2.redns.redis-cloud.com:10185/0
CELERY_RESULT_BACKEND=redis://redis-10185.c258.us-east-1-4.ec2.redns.redis-cloud.com:10185/0
N8N_ENABLED=true
N8N_API_URL=https://n8n-custom-1035304851064.europe-west1.run.app
N8N_WEBHOOK_URL=https://n8n-custom-1035304851064.europe-west1.run.app/webhook
N8N_EDITOR_BASE_URL=https://n8n-custom-1035304851064.europe-west1.run.app
N8N_API_KEY=<your value>
SARVAM_API_KEY=<your value>
LIVEKIT_URL=wss://test-n1vl6im4.livekit.cloud
LIVEKIT_API_KEY=<your value>
LIVEKIT_API_SECRET=<your value>
LIVEKIT_AGENT_NAME=convis-agent
LIVEKIT_SIP_OUTBOUND_TRUNK_ID=<your value, no trailing space>
LIVEKIT_SIP_INBOUND_HOST=<your value, no trailing space>
API_BASE_URL=https://placeholder.convis.ai
BASE_URL=https://placeholder.convis.ai
OUTBOUND_TWIML_URL=https://placeholder.convis.ai/api/twilio-webhooks/outbound-call
TW_STATUS_CALLBACK=https://placeholder.convis.ai/api/twilio-webhooks/call-status
TW_RECORDING_CALLBACK=https://placeholder.convis.ai/api/twilio-webhooks/recording
```

The placeholders let the app boot; the Twilio callback URLs only matter when calls happen.

### Create + verify

Click **Create**. Wait 5–10 minutes. Status reaches **Active** and you get a URL like:
```
https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws
```

Test:
```powershell
curl https://<api-url>/health
```
Expected: `{"status":"healthy"}`

**If it fails** — open the service → Logs tab and check the most recent log lines for the error. Common ones: missing required env var (e.g. `JWT_SECRET` blank), MongoDB connection refused (check `MONGODB_URI` IP allowlist in Atlas).

---

## Part 3 — Update the URL env vars and re-roll convis-api

Once you have the api URL, return to the service → **Update**.

Replace the placeholders with the real URLs:
```env
API_BASE_URL=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws
BASE_URL=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws
OUTBOUND_TWIML_URL=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws/api/twilio-webhooks/outbound-call
TW_STATUS_CALLBACK=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws/api/twilio-webhooks/call-status
TW_RECORDING_CALLBACK=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws/api/twilio-webhooks/recording
```

Save. Express Mode re-rolls the task automatically (~3-5 min). Verify `/health` still returns healthy after the re-roll.

---

## Part 4 — Rebuild convis-web with the correct API URL baked in

The Next.js bundle hardcodes `NEXT_PUBLIC_API_URL` at build time. Whatever image is in ap-south-1 ECR right now points the browser at the old us-east-1 App Runner URL. You must rebuild.

### 4a. Update convis-web/.env.production

Edit `convis-web/.env.production` to:
```env
NEXT_PUBLIC_API_URL=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws
NEXT_PUBLIC_API_BASE_URL=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws
NEXT_PUBLIC_N8N_URL=/n8n
```

(If you also need `NEXT_PUBLIC_LIVEKIT_URL` baked in — check `convis-web/.env.local` — add it here too.)

### 4b. Rebuild + push convis-web image

```powershell
$TAG = "deploy-" + (Get-Date -Format "yyyyMMdd-HHmm")
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin 942617679452.dkr.ecr.ap-south-1.amazonaws.com

docker buildx build --platform linux/amd64 --provenance=false `
  -t 942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-web:$TAG `
  -t 942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-web:latest `
  --push ./convis-web

aws ecr describe-images --repository-name convis-web --region ap-south-1 --image-ids imageTag=$TAG --query 'imageDetails[0].imageTags'
```

The verify step is critical — `docker buildx --push` has been known to silently fail to write the manifest. If the verify command throws `ImageNotFoundException`, re-run the build.

### 4c. (Optional but recommended) Fix the Dockerfile so build-args actually work

Currently `convis-web/Dockerfile` doesn't declare `ARG NEXT_PUBLIC_API_URL`, so the `--build-arg` line in `.github/workflows/deploy.yml` is silently ignored. To make build-args take effect (so you don't need to commit URLs to `.env.production`):

```dockerfile
# Add inside the `builder` stage, BEFORE `RUN npm run build`:
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
```

Then set the GitHub repo variable `NEXT_PUBLIC_API_URL` to the ap-south-1 URL.

This is a workflow improvement, not strictly required to ship — do it when you next touch the Dockerfile.

---

## Part 5 — Deploy convis-web on Express Mode

**Console → ECS (ap-south-1) → Express mode → Create service.**

| Field | Value |
|---|---|
| Service name | `convis-web` |
| Cluster | `default` (same as api → shared ALB) |
| Image | `942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-web:latest` |
| Container port | `3000` |
| Health check path | `/` |
| CPU | `1 vCPU` |
| Memory | `2 GB` |
| Command | leave blank |
| Networking | same VPC/subnets/SG as api |
| Task execution role | `ecsTaskExecutionRole` |
| Secrets | empty |

### Environment variables for convis-web

```env
NODE_ENV=production
NEXT_PUBLIC_API_URL=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws
NEXT_PUBLIC_API_BASE_URL=https://convis-api-xxxxxxxxxxxx.ap-south-1.on.aws
NEXT_PUBLIC_N8N_URL=/n8n
```

> Reminder: these runtime env vars don't change the browser bundle (that came from Part 4's rebuild). Setting them anyway for consistency, and so any server-side code in Next.js that reads them sees the right values.

Create → wait → you get the web URL: `https://convis-web-xxxxxxxxxxxx.ap-south-1.on.aws`. Open in browser, sign in, confirm the dashboard works.

---

## Part 6 — Smoke tests

```powershell
# API health
curl https://<api-url>/health

# LiveKit credentials reachable
curl https://<api-url>/api/livekit/

# Outbound transport configured
curl https://<api-url>/api/outbound-calls/
```

Expected JSON responses for each (no 5xx).

**Browser flow:**
1. Open the web URL.
2. Sign in.
3. Open Network tab → reload → confirm API calls go to the **ap-south-1** URL, not us-east-1.
4. Open an assistant → click the call/test button → speak → confirm a reply.

The voice call will currently route to the **us-east-1 agent** because the agent migration hasn't happened yet (Part 8). That's fine for this smoke test — confirms api + web + LiveKit work; calls work because the agent is still alive in us-east-1.

---

## Part 7 — Update GitHub Actions workflow

Edit `.github/workflows/deploy.yml`. Two changes:

### 7a. Change the GitHub repo variable

`AWS_REGION` is currently `us-east-1`. Set it to `ap-south-1`:
- GitHub repo → Settings → Secrets and variables → Actions → Variables → `AWS_REGION` → Edit → `ap-south-1`.

Also update `NEXT_PUBLIC_API_URL` to the new ap-south-1 api URL (used as a build-arg, will actually take effect once you fix the Dockerfile per Part 4c).

### 7b. Rewrite deploy-api and deploy-web jobs

Replace the App Runner `start-deployment` calls with ECS `update-service --force-new-deployment` calls:

```yaml
  deploy-api:
    needs: build-api
    runs-on: ubuntu-latest
    steps:
      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::${{ env.AWS_ACCOUNT_ID }}:role/GitHubActionsConvisDeployRole
          aws-region: ${{ env.AWS_REGION }}

      - name: Force-redeploy convis-api on ECS Express Mode
        run: |
          aws ecs update-service \
            --cluster default \
            --service convis-api \
            --force-new-deployment \
            --region ${{ env.AWS_REGION }} \
            --output table \
            --query 'service.{name:serviceName,desired:desiredCount,running:runningCount,status:status}'

  deploy-web:
    needs: build-web
    runs-on: ubuntu-latest
    steps:
      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::${{ env.AWS_ACCOUNT_ID }}:role/GitHubActionsConvisDeployRole
          aws-region: ${{ env.AWS_REGION }}

      - name: Force-redeploy convis-web on ECS Express Mode
        run: |
          aws ecs update-service \
            --cluster default \
            --service convis-web \
            --force-new-deployment \
            --region ${{ env.AWS_REGION }} \
            --output table \
            --query 'service.{name:serviceName,desired:desiredCount,running:runningCount,status:status}'
```

### 7c. (Until Part 8) Keep us-east-1 agent in sync

The agent is still running in us-east-1 and needs new images too. Until you migrate it, the workflow should push to **both** regions' ECR. Either:
- Run the GitHub Actions IAM role's region as `us-east-1` for the build/push step and `ap-south-1` for the deploy step (more complex), OR
- After Part 8 (agent migration) is done, this becomes moot.

Simplest approach: do Part 8 quickly so you only ever push to ap-south-1.

### 7d. Permissions

The deploy IAM role (`GitHubActionsConvisDeployRole`) currently has App Runner permissions. ECS `update-service` is already allowed (used by `deploy-agent`), so no permission changes are needed. You can clean up the `apprunner:*` permissions later.

---

## Part 8 — Migrate the agent to ap-south-1

### 8a. Confirm the agent image is in ap-south-1 ECR

The agent uses the same `convis-api` image (the CMD is overridden in the task def). If Part 1 succeeded, this is already done.

### 8b. Create the task definition

Save as `deployment-docs/aws/ecs-livekit-agent-task-ap-south-1.json`:

```json
{
  "family": "convis-livekit-agent",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "arn:aws:iam::942617679452:role/ecsTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::942617679452:role/convis-agent-task-role",
  "containerDefinitions": [
    {
      "name": "livekit-agent",
      "image": "942617679452.dkr.ecr.ap-south-1.amazonaws.com/convis-api:latest",
      "command": ["python", "-m", "app.services.livekit.agent_worker", "start"],
      "essential": true,
      "environment": [
        { "name": "ENVIRONMENT",                  "value": "production" },
        { "name": "DATABASE_NAME",                "value": "convis_india" },
        { "name": "LIVEKIT_AGENT_NAME",           "value": "convis-agent" },
        { "name": "MONGODB_URI",                  "value": "<your value>" },
        { "name": "OPENAI_API_KEY",               "value": "<your value>" },
        { "name": "SARVAM_API_KEY",               "value": "<your value>" },
        { "name": "LIVEKIT_URL",                  "value": "wss://test-n1vl6im4.livekit.cloud" },
        { "name": "LIVEKIT_API_KEY",              "value": "<your value>" },
        { "name": "LIVEKIT_API_SECRET",           "value": "<your value>" },
        { "name": "LIVEKIT_SIP_OUTBOUND_TRUNK_ID","value": "<your value>" },
        { "name": "LIVEKIT_SIP_INBOUND_HOST",     "value": "<your value>" },
        { "name": "JWT_SECRET",                   "value": "<your value>" },
        { "name": "ENCRYPTION_KEY",               "value": "<your value>" },
        { "name": "EMAIL_USER",                   "value": "no-reply@convis.ai" },
        { "name": "EMAIL_PASS",                   "value": "<your value>" }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/convis-livekit-agent",
          "awslogs-create-group": "true",
          "awslogs-region": "ap-south-1",
          "awslogs-stream-prefix": "agent"
        }
      }
    }
  ]
}
```

> No `secrets` section. Plain env vars. Fill in real values **locally only — do not commit**.

### 8c. Register the task def + create the service

```powershell
aws ecs register-task-definition --cli-input-json file://deployment-docs/aws/ecs-livekit-agent-task-ap-south-1.json --region ap-south-1

# Get default VPC subnet IDs
aws ec2 describe-subnets --region ap-south-1 --filters "Name=default-for-az,Values=true" --query 'Subnets[*].SubnetId' --output text

# Get default SG ID
aws ec2 describe-security-groups --region ap-south-1 --filters "Name=group-name,Values=default" --query 'SecurityGroups[0].GroupId' --output text
```

Then (substitute the subnet/SG values):
```powershell
aws ecs create-service `
  --cluster default `
  --service-name convis-livekit-agent `
  --task-definition convis-livekit-agent `
  --desired-count 1 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[subnet-aaa,subnet-bbb,subnet-ccc],securityGroups=[sg-xxx],assignPublicIp=ENABLED}" `
  --region ap-south-1
```

Watch logs:
```powershell
aws logs tail /ecs/convis-livekit-agent --region ap-south-1 --follow
```
Expect: `[AGENT] Worker registered with LiveKit Cloud`.

### 8d. Cut over from us-east-1 agent

LiveKit may load-balance jobs between any registered worker. To force calls onto the new agent:

```powershell
aws ecs update-service --cluster convis --service convis-livekit-agent --desired-count 0 --region us-east-1
```

Make a test call. Confirm it lands on the ap-south-1 agent (check `aws logs tail /ecs/convis-livekit-agent --region ap-south-1`).

If the test call works for 10+ minutes without issues, you're cut over.

---

## Part 9 — Cleanup us-east-1 (after Part 8 has been stable for at least a few hours)

```powershell
# Delete the us-east-1 agent service
aws ecs delete-service --cluster convis --service convis-livekit-agent --force --region us-east-1

# Delete the us-east-1 cluster
aws ecs delete-cluster --cluster convis --region us-east-1

# Delete us-east-1 ECR repos
aws ecr delete-repository --repository-name convis-api --force --region us-east-1
aws ecr delete-repository --repository-name convis-web --force --region us-east-1

# Delete the 16 us-east-1 secrets (replace with the actual list)
$secrets = @("mongodb_uri","jwt_secret","encryption_key","email_user","email_pass","openai_api_key","deepgram_api_key","elevenlabs_api_key","livekit_url","livekit_api_key","livekit_api_secret","livekit_sip_inbound_host","twilio_account_sid","twilio_auth_token","cartesia_api_key","vobiz_sip_inbound_pass")
foreach ($s in $secrets) {
  aws secretsmanager delete-secret --secret-id "convis/$s" --force-delete-without-recovery --region us-east-1
}
```

Optionally prune the ap-south-1 secrets too (you have 42 but you're using plain env vars now). Wait until everything is stable for a week before doing this.

---

## Part 10 — Custom domains (optional, do whenever)

Either path requires an ACM cert in ap-south-1:

```powershell
aws acm request-certificate --domain-name api.convis.ai --validation-method DNS --region ap-south-1
aws acm request-certificate --domain-name app.convis.ai --validation-method DNS --region ap-south-1
```

Add the DNS validation CNAMEs at your registrar. Once issued:

**Option A — Attach cert directly to the Express Mode ALB:**
1. Find the ALB in EC2 console → Load Balancers (ap-south-1).
2. Add HTTPS listener on 443 using the ACM cert.
3. Add forwarding rules: `api.convis.ai` → `convis-api` target group; `app.convis.ai` → `convis-web` target group.
4. CNAME `api.convis.ai` and `app.convis.ai` to the ALB DNS name at your registrar.

**Option B — CloudFront in front of convis-web (better for global dashboard latency):**
1. Create CloudFront distribution with origin = `convis-web-xxxxxxxxxxxx.ap-south-1.on.aws`.
2. Attach the ACM cert (must be in us-east-1 for CloudFront — request a separate cert there).
3. CNAME `app.convis.ai` to the CloudFront distribution domain.

After domains are live, update:
- `convis-api/.env` → `API_BASE_URL=https://api.convis.ai`, `BASE_URL=https://api.convis.ai`, plus the three TW_* URLs.
- `convis-web/.env.production` → `NEXT_PUBLIC_API_URL=https://api.convis.ai`, rebuild + redeploy convis-web image.
- Twilio Console → Phone Numbers → Voice URL → `https://api.convis.ai/api/inbound-calls/connect/<assistant_id>`.

---

## Cost estimate (steady state, ap-south-1)

| Item | Monthly |
|---|---|
| convis-api Fargate (1 vCPU/2GB) | ~$36 |
| convis-web Fargate (1 vCPU/2GB) | ~$36 |
| Shared ALB | ~$18 |
| convis-livekit-agent Fargate (1 vCPU/2GB) | ~$25 |
| ECR storage | ~$1 |
| **Total** | **~$116/mo** |

Plus per-call OpenAI / Sarvam / Twilio costs (variable).

---

## Troubleshooting

**Express Mode service stuck in PROVISIONING for >10 min**
Open the service → **Events** tab. The reason is usually:
- `unable to pull image` → image isn't in ap-south-1 ECR (run Part 1).
- `ResourceInitializationError: ... secret ...` → you put something in the Secrets section. Remove everything from Secrets and put it all in Environment.
- Capacity issue in the region → wait or pick a different subnet/AZ.

**curl returns connection refused or DNS resolution fails**
Service hasn't finished provisioning yet, or networking is misconfigured. Check the service is Active in console first.

**`{"configured": false}` on `/api/livekit/`**
`LIVEKIT_*` env vars didn't load. Check the values in the Environment section of the running task definition. Re-update if blank.

**Twilio signature 403**
The URL Twilio is hitting must match `API_BASE_URL` exactly. If you put CloudFront in front, the request reaches the api with a different Host header and signatures break. Either don't put CloudFront in front of the api, or configure Twilio with the CloudFront URL.

**Voice call latency feels high after agent migration**
Expected — the agent is now in Mumbai but OpenAI/LiveKit Cloud endpoints are US-based. Each ASR/LLM/TTS hit adds ~180-220ms RTT. Use Sarvam (Indian endpoints) where possible.

**`docker buildx --push` succeeds but image not in ECR**
Known silent-failure mode. Always run `aws ecr describe-images --image-ids imageTag=$TAG` to verify. Re-run the build if missing.

---

## What changed vs `AWS_APP_RUNNER.md`

- **Region:** us-east-1 → ap-south-1.
- **Platform for api/web:** App Runner → ECS Express Mode (App Runner deprecated April 30, 2026).
- **Cluster:** `convis` → `default` (Express Mode's default cluster).
- **Secrets:** Secrets Manager `valueFrom` ARNs → plain env vars. (Trade-off accepted by the team — secrets visible in task definitions but no IAM/cross-region debugging.)
- **Build args for convis-web:** Build-args were silently ignored (Dockerfile gap); fix is to either commit URLs to `.env.production` or add `ARG`/`ENV` lines to the Dockerfile.
- **Agent task definition:** Same shape, but converted from Secrets Manager refs to plain env vars and pointing at ap-south-1 ECR.

When in doubt, this doc supersedes `AWS_APP_RUNNER.md`.
