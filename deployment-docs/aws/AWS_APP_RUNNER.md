# AWS deployment — App Runner + ECS Fargate

Convis runs on AWS as **two services**:

| Service | Where | Why |
|---|---|---|
| `convis-api` (FastAPI) | App Runner | HTTP, autoscales by request count |
| `convis-web` (Next.js) | App Runner | HTTP |
| `convis-livekit-agent` | ECS Fargate (always-on) | Long-running, no HTTP server, connects out to LiveKit Cloud |

App Runner is HTTP-only and would scale the agent worker to zero (no requests = no instance) — calls would silently fail.

## Prereqs

- AWS account ID and region picked. Throughout this doc: `${ACCT}` = your 12-digit account, `${REGION}` = e.g. `us-east-1`.
- AWS CLI v2 + Docker Desktop / Buildx installed locally.
- LiveKit Inbound Trunk + SIP host already configured (see `LIVEKIT_SIP_SETUP.md`).
- Twilio number's Voice URL pointed at `https://api.<your-domain>/api/inbound-calls/connect/<assistant_id>` (update after step 6).

## 1 — Push secrets to Secrets Manager (one-time)

```bash
cd /path/to/Convis-main
AWS_REGION=${REGION} ./deployment-docs/aws/import-secrets.sh ./convis-api/.env
```

This whitelist-pushes every secret in `.env` into Secrets Manager under `convis/<lowercase_name>`. Idempotent — safe to re-run.

Verify:
```bash
aws secretsmanager list-secrets --region ${REGION} \
  --query 'SecretList[?starts_with(Name, `convis/`)].Name'
```

## 2 — Build + push images to ECR

```bash
# Create repos (one-time)
aws ecr create-repository --repository-name convis-api --region ${REGION}
aws ecr create-repository --repository-name convis-web --region ${REGION}

# Login
aws ecr get-login-password --region ${REGION} | \
  docker login --username AWS --password-stdin ${ACCT}.dkr.ecr.${REGION}.amazonaws.com

# Build for amd64 (App Runner runs x86_64) + push
TAG=$(git rev-parse --short HEAD)

docker buildx build --platform linux/amd64 \
  -t ${ACCT}.dkr.ecr.${REGION}.amazonaws.com/convis-api:${TAG} \
  -t ${ACCT}.dkr.ecr.${REGION}.amazonaws.com/convis-api:latest \
  --push ./convis-api

docker buildx build --platform linux/amd64 \
  -t ${ACCT}.dkr.ecr.${REGION}.amazonaws.com/convis-web:${TAG} \
  -t ${ACCT}.dkr.ecr.${REGION}.amazonaws.com/convis-web:latest \
  --push ./convis-web
```

## 3 — IAM roles (one-time)

Three roles needed:

### a. App Runner ECR access role
Lets App Runner pull from ECR.

```bash
aws iam create-role --role-name AppRunnerECRAccessRole \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"build.apprunner.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]}'

aws iam attach-role-policy --role-name AppRunnerECRAccessRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
```

### b. App Runner instance role (reads secrets at runtime)

```bash
aws iam create-role --role-name convis-api-instance-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"tasks.apprunner.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]}'

aws iam put-role-policy --role-name convis-api-instance-role \
  --policy-name SecretsRead \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Action\":[\"secretsmanager:GetSecretValue\"],
      \"Resource\":\"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/*\"
    }]}"
```

### c. ECS task + execution roles (for the agent worker)

```bash
# Execution role — pulls image, fetches secrets for the container
aws iam create-role --role-name ecsTaskExecutionRole \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"ecs-tasks.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]}' 2>/dev/null || true   # role already exists is fine

aws iam attach-role-policy --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

aws iam put-role-policy --role-name ecsTaskExecutionRole \
  --policy-name SecretsRead \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Action\":[\"secretsmanager:GetSecretValue\"],
      \"Resource\":\"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/*\"
    }]}"

# Task role — what the running container can do (talk to LiveKit Cloud, etc.)
aws iam create-role --role-name convis-agent-task-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"ecs-tasks.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]}'
# No policies needed — agent only makes outbound HTTPS to LiveKit/Deepgram/etc.
```

## 4 — Deploy convis-api on App Runner

```bash
aws apprunner create-service \
  --service-name convis-api \
  --source-configuration "{
    \"ImageRepository\": {
      \"ImageIdentifier\": \"${ACCT}.dkr.ecr.${REGION}.amazonaws.com/convis-api:latest\",
      \"ImageRepositoryType\": \"ECR\",
      \"ImageConfiguration\": {
        \"Port\": \"8000\",
        \"RuntimeEnvironmentVariables\": {
          \"ENVIRONMENT\": \"production\",
          \"DATABASE_NAME\": \"convis_python\",
          \"LIVEKIT_AGENT_NAME\": \"convis-agent\",
          \"API_BASE_URL\": \"https://api.convis.ai\",
          \"FRONTEND_URL\": \"https://app.convis.ai\",
          \"CORS_ORIGINS\": \"https://app.convis.ai\"
        },
        \"RuntimeEnvironmentSecrets\": {
          \"MONGODB_URI\":               \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/mongodb_uri\",
          \"JWT_SECRET\":                \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/jwt_secret\",
          \"ENCRYPTION_KEY\":            \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/encryption_key\",
          \"EMAIL_USER\":                \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/email_user\",
          \"EMAIL_PASS\":                \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/email_pass\",
          \"OPENAI_API_KEY\":            \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/openai_api_key\",
          \"DEEPGRAM_API_KEY\":          \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/deepgram_api_key\",
          \"ELEVENLABS_API_KEY\":        \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/elevenlabs_api_key\",
          \"LIVEKIT_URL\":               \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/livekit_url\",
          \"LIVEKIT_API_KEY\":           \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/livekit_api_key\",
          \"LIVEKIT_API_SECRET\":        \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/livekit_api_secret\",
          \"LIVEKIT_SIP_INBOUND_HOST\":  \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/livekit_sip_inbound_host\",
          \"TWILIO_ACCOUNT_SID\":        \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/twilio_account_sid\",
          \"TWILIO_AUTH_TOKEN\":         \"arn:aws:secretsmanager:${REGION}:${ACCT}:secret:convis/twilio_auth_token\"
        }
      }
    },
    \"AutoDeploymentsEnabled\": true,
    \"AuthenticationConfiguration\": {
      \"AccessRoleArn\": \"arn:aws:iam::${ACCT}:role/AppRunnerECRAccessRole\"
    }
  }" \
  --instance-configuration "{
    \"Cpu\": \"1 vCPU\",
    \"Memory\": \"2 GB\",
    \"InstanceRoleArn\": \"arn:aws:iam::${ACCT}:role/convis-api-instance-role\"
  }" \
  --health-check-configuration '{
    "Protocol": "HTTP",
    "Path": "/health",
    "Interval": 20,
    "Timeout": 5,
    "HealthyThreshold": 1,
    "UnhealthyThreshold": 5
  }' \
  --region ${REGION}
```

App Runner returns an `https://<random>.<region>.awsapprunner.com` URL — note it for step 6.

## 5 — Deploy convis-web on App Runner

```bash
aws apprunner create-service \
  --service-name convis-web \
  --source-configuration "{
    \"ImageRepository\": {
      \"ImageIdentifier\": \"${ACCT}.dkr.ecr.${REGION}.amazonaws.com/convis-web:latest\",
      \"ImageRepositoryType\": \"ECR\",
      \"ImageConfiguration\": {
        \"Port\": \"3000\",
        \"RuntimeEnvironmentVariables\": {
          \"NODE_ENV\": \"production\",
          \"NEXT_PUBLIC_API_URL\": \"https://api.convis.ai\",
          \"NEXT_PUBLIC_API_BASE_URL\": \"https://api.convis.ai\"
        }
      }
    },
    \"AutoDeploymentsEnabled\": true,
    \"AuthenticationConfiguration\": {
      \"AccessRoleArn\": \"arn:aws:iam::${ACCT}:role/AppRunnerECRAccessRole\"
    }
  }" \
  --instance-configuration '{
    "Cpu": "1 vCPU",
    "Memory": "2 GB"
  }' \
  --health-check-configuration '{
    "Protocol": "HTTP",
    "Path": "/",
    "Interval": 20,
    "Timeout": 5
  }' \
  --region ${REGION}
```

> ⚠️ `NEXT_PUBLIC_*` is baked into the **client bundle at build time**. Setting them as runtime env vars on App Runner does NOT update what the browser sees. To change them, rebuild + repush the image. (Alternative: use `next.config.js` `serverRuntimeConfig` — but that's a refactor.)

## 6 — Deploy the LiveKit agent worker on ECS Fargate

```bash
# Substitute placeholders
sed "s|\${REGION}|${REGION}|g; s|\${ACCT}|${ACCT}|g" \
  ./deployment-docs/aws/ecs-livekit-agent-task.json > /tmp/agent-task.json

# Register task definition
aws ecs register-task-definition --cli-input-json file:///tmp/agent-task.json --region ${REGION}

# Create cluster (one-time)
aws ecs create-cluster --cluster-name convis --region ${REGION}

# Create service. Pick a public subnet — agent makes outbound HTTPS only.
# Replace subnet/sg IDs with your VPC's defaults (or create dedicated ones).
SUBNETS="subnet-xxxxxxxx,subnet-yyyyyyyy"   # any subnets with NAT or public IP
SG="sg-zzzzzzzz"                             # default SG with outbound 443 OK

aws ecs create-service \
  --cluster convis \
  --service-name convis-livekit-agent \
  --task-definition convis-livekit-agent \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SG}],assignPublicIp=ENABLED}" \
  --region ${REGION}
```

The agent doesn't need a load balancer — it connects OUT to LiveKit Cloud and never receives HTTP.

Watch logs:
```bash
aws logs tail /ecs/convis-livekit-agent --follow --region ${REGION}
```
Expect: `[AGENT] Worker registered with LiveKit Cloud`

## 7 — Custom domain + TLS

```bash
# Get the App Runner default URLs
API_URL=$(aws apprunner list-services --region ${REGION} \
  --query "ServiceSummaryList[?ServiceName=='convis-api'].ServiceUrl|[0]" --output text)
WEB_URL=$(aws apprunner list-services --region ${REGION} \
  --query "ServiceSummaryList[?ServiceName=='convis-web'].ServiceUrl|[0]" --output text)

# Attach custom domains (DNS records returned to add at your registrar)
aws apprunner associate-custom-domain \
  --service-arn $(aws apprunner list-services --region ${REGION} --query "ServiceSummaryList[?ServiceName=='convis-api'].ServiceArn|[0]" --output text) \
  --domain-name api.convis.ai

aws apprunner associate-custom-domain \
  --service-arn $(aws apprunner list-services --region ${REGION} --query "ServiceSummaryList[?ServiceName=='convis-web'].ServiceArn|[0]" --output text) \
  --domain-name app.convis.ai
```

Add the returned CNAME records at your DNS provider. App Runner auto-provisions ACM certs.

## 8 — Update Twilio webhook URLs

In Twilio Console → Phone Numbers → Active Numbers → click each number you want live → Voice Configuration → A call comes in → URL:
```
https://api.convis.ai/api/inbound-calls/connect/<assistant_id>
```

## 9 — Smoke test

```bash
curl https://api.convis.ai/health                    # {"status":"healthy"}
curl https://api.convis.ai/api/livekit/              # {"configured": true}
curl https://api.convis.ai/api/outbound-calls/       # {"transport": "twilio-twiml"}
```

Browser:
1. Open `https://app.convis.ai`
2. Log in
3. Open an assistant → click the call button → speak → confirm reply
4. Check `aws logs tail /ecs/convis-livekit-agent --follow` to see the agent receive the job

PSTN inbound: dial `+14472473607` from any phone, expect the agent to answer.

## Re-deploys

After code changes:

```bash
TAG=$(git rev-parse --short HEAD)
docker buildx build --platform linux/amd64 \
  -t ${ACCT}.dkr.ecr.${REGION}.amazonaws.com/convis-api:${TAG} \
  -t ${ACCT}.dkr.ecr.${REGION}.amazonaws.com/convis-api:latest \
  --push ./convis-api

# Tell App Runner to roll
aws apprunner start-deployment --service-arn <api-arn> --region ${REGION}

# Roll the agent (forces a new task with the new image)
aws ecs update-service --cluster convis --service convis-livekit-agent \
  --force-new-deployment --region ${REGION}
```

## Rough cost (US East, low traffic)

| | Monthly |
|---|---|
| App Runner api (1 vCPU, 2GB, 1 min instance) | ~$30 |
| App Runner web (1 vCPU, 2GB, 1 min instance) | ~$30 |
| ECS Fargate agent (1 vCPU, 2GB, always on) | ~$25 |
| Secrets Manager (15 secrets) | ~$6 |
| ECR storage + data transfer | ~$5 |
| **Total** | **~$95–110** before per-call provider costs |

## Known production gates not yet satisfied

These don't block deployment but should be tracked:
- Per-endpoint IDOR sweep on phone_numbers/* (router-level auth done; cross-user access via `{user_id}` path param still possible)
- `pip-audit` / `npm audit` not run
- 8 stale provider files (frontend dropdowns still show Cartesia/Sarvam — runtime-broken but UI shipping)
- Live e2e calls never tested before

## Troubleshooting

**App Runner stuck "Operation in progress" >10 min** → first build is slow due to heavy ML imports. Wait. If it fails, check `aws apprunner describe-service`'s `StatusReason`.

**`{"configured": false}` on `/api/livekit/`** → `LIVEKIT_*` secrets didn't load. Check the App Runner instance role has `secretsmanager:GetSecretValue` and that secrets exist with exact names `convis/livekit_url` etc.

**Agent worker not picking up jobs** → `aws logs tail /ecs/convis-livekit-agent`. If you see auth errors, secrets aren't reaching the task — verify `executionRoleArn` policy.

**Twilio signature 403** → `API_BASE_URL` in App Runner env vars must match the URL Twilio actually hits. If you fronted App Runner with CloudFront, signature won't match. Either drop CloudFront or point Twilio webhooks directly at the App Runner default URL (not the custom domain).
