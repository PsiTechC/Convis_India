# Convis-India — AWS Deploy via GitHub Actions

End-to-end deploy guide. Push to a branch → GitHub Actions builds Docker images → pushes to ECR → rolls App Runner (convis-api, convis-web) and ECS Fargate (convis-livekit-agent). Auth via GitHub OIDC — no long-lived AWS keys in GitHub.

Estimated time for first deploy: ~2 hours. Subsequent deploys: 5 minutes (just `git push`).

---

## Architecture

```
GitHub push → GitHub Actions
  ├── Build convis-api image → ECR
  ├── Build convis-web image → ECR
  └── Roll three services:
        ├── App Runner: convis-api          (HTTP, FastAPI)
        ├── App Runner: convis-web          (HTTP, Next.js)
        └── ECS Fargate: convis-livekit-agent (long-running, no HTTP)

External services:
  - MongoDB Atlas (or self-hosted) — connects via MONGODB_URI
  - LiveKit Cloud — voice media plane
  - Vobiz — PSTN carrier
  - Sarvam — ASR + LLM + TTS APIs
```

The agent worker uses the **same image** as convis-api — the ECS task definition overrides the container command to start the agent process instead of FastAPI.

---

## Prerequisites

- AWS account with an IAM user that has admin (or at least IAM + ECR + App Runner + ECS + Secrets Manager full access)
- AWS CLI v2 installed locally and configured: `aws configure`
- GitHub repository: `PsiTechC/Convis_India`
- LiveKit Cloud project + SIP trunk configured (see [AWS_APP_RUNNER.md](AWS_APP_RUNNER.md) Section "LiveKit setup" if not)
- Vobiz account with at least one DID + outbound SIP trunk created, IP ACL whitelisting LiveKit egress IPs
- MongoDB Atlas (or self-hosted) cluster reachable from AWS

---

## Phase 1 — One-time AWS setup

These resources exist forever after creating them once. GitHub Actions only **updates** them on every deploy.

### 1.1 Set shell variables (PowerShell)

```powershell
$env:ACCT = "<your 12-digit AWS account id>"
$env:REGION = "ap-south-1"    # Mumbai recommended for India / Sarvam latency
$env:AWS_REGION = $env:REGION
```

Verify CLI works:
```powershell
aws sts get-caller-identity
```

### 1.2 Create ECR repositories

```powershell
aws ecr create-repository --repository-name convis-api --region $env:REGION
aws ecr create-repository --repository-name convis-web --region $env:REGION
```

### 1.3 Push secrets to AWS Secrets Manager

Your `.env` file must contain the production values for these. Required keys:

- `MONGODB_URI` — connection string for prod Mongo cluster
- `JWT_SECRET` — JWT signing key (must be different from dev)
- `ENCRYPTION_KEY` — Fernet key for encrypting sensitive DB fields
- `EMAIL_USER`, `EMAIL_PASS` — SMTP credentials
- `SARVAM_API_KEY` — Sarvam API key (all three services: ASR, LLM, TTS)
- `OPENAI_API_KEY` — still used by RAG embeddings + post-call summaries
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` — LiveKit Cloud creds
- `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` — `ST_xxxxxxxx` from LiveKit Cloud
- `LIVEKIT_SIP_INBOUND_HOST` — `<project>-sip.livekit.cloud`

Push them:

```powershell
cd c:\Users\Tejas-Psitech\Desktop\Psitech\Convis_India
bash ./deployment-docs/aws/import-secrets.sh ./convis-api/.env
```

This walks every line in `.env` and creates / updates a secret at `convis/<lowercase_name>`. Idempotent — re-run anytime to sync changes.

Verify:
```powershell
aws secretsmanager list-secrets --region $env:REGION `
  --query "SecretList[?starts_with(Name, 'convis/')].Name" --output table
```

You should see at least: `convis/mongodb_uri`, `convis/sarvam_api_key`, `convis/livekit_url`, etc.

### 1.4 Create IAM roles for App Runner and ECS

#### a. App Runner ECR access role (lets App Runner pull from ECR)

```powershell
aws iam create-role --role-name AppRunnerECRAccessRole `
  --assume-role-policy-document '{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"build.apprunner.amazonaws.com\"},
      \"Action\":\"sts:AssumeRole\"
    }]}'

aws iam attach-role-policy --role-name AppRunnerECRAccessRole `
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
```

#### b. App Runner instance role (reads secrets at runtime)

```powershell
aws iam create-role --role-name convis-api-instance-role `
  --assume-role-policy-document '{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"tasks.apprunner.amazonaws.com\"},
      \"Action\":\"sts:AssumeRole\"
    }]}'

aws iam put-role-policy --role-name convis-api-instance-role `
  --policy-name SecretsRead `
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Action\":[\"secretsmanager:GetSecretValue\"],
      \"Resource\":\"arn:aws:secretsmanager:$($env:REGION):$($env:ACCT):secret:convis/*\"
    }]}"
```

#### c. ECS task execution + task roles (for the agent worker)

```powershell
# Execution role — pulls image, fetches secrets
aws iam create-role --role-name ecsTaskExecutionRole `
  --assume-role-policy-document '{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"},
      \"Action\":\"sts:AssumeRole\"
    }]}' 2>$null

aws iam attach-role-policy --role-name ecsTaskExecutionRole `
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

aws iam put-role-policy --role-name ecsTaskExecutionRole `
  --policy-name SecretsRead `
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Action\":[\"secretsmanager:GetSecretValue\"],
      \"Resource\":\"arn:aws:secretsmanager:$($env:REGION):$($env:ACCT):secret:convis/*\"
    }]}"

# Task role — what the running container can do (mostly nothing, just outbound HTTPS)
aws iam create-role --role-name convis-agent-task-role `
  --assume-role-policy-document '{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"},
      \"Action\":\"sts:AssumeRole\"
    }]}'
# No policy needed — agent only makes outbound HTTPS
```

### 1.5 GitHub OIDC provider in AWS (one-time per account)

```powershell
aws iam create-open-id-connect-provider `
  --url https://token.actions.githubusercontent.com `
  --client-id-list sts.amazonaws.com `
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

If you see `EntityAlreadyExistsException`, that's fine — it's a per-account resource, skip.

### 1.6 IAM role that GitHub Actions assumes

Save this as `trust-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCT_PLACEHOLDER:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:PsiTechC/Convis_India:*"
        }
      }
    }
  ]
}
```

Replace `ACCT_PLACEHOLDER` with your account ID. The `StringLike` condition pins this role to your specific GitHub repo — no other repo can assume it.

Create the role:

```powershell
aws iam create-role `
  --role-name GitHubActionsConvisDeployRole `
  --assume-role-policy-document file://trust-policy.json
```

Attach the permissions it needs:

```powershell
aws iam put-role-policy --role-name GitHubActionsConvisDeployRole `
  --policy-name DeployPermissions `
  --policy-document @"
{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {
      \"Effect\": \"Allow\",
      \"Action\": [
        \"ecr:GetAuthorizationToken\",
        \"ecr:BatchCheckLayerAvailability\",
        \"ecr:GetDownloadUrlForLayer\",
        \"ecr:BatchGetImage\",
        \"ecr:InitiateLayerUpload\",
        \"ecr:UploadLayerPart\",
        \"ecr:CompleteLayerUpload\",
        \"ecr:PutImage\",
        \"ecr:DescribeRepositories\",
        \"ecr:DescribeImages\"
      ],
      \"Resource\": \"*\"
    },
    {
      \"Effect\": \"Allow\",
      \"Action\": [
        \"apprunner:StartDeployment\",
        \"apprunner:DescribeService\",
        \"apprunner:ListServices\"
      ],
      \"Resource\": \"*\"
    },
    {
      \"Effect\": \"Allow\",
      \"Action\": [
        \"ecs:UpdateService\",
        \"ecs:DescribeServices\",
        \"ecs:DescribeTaskDefinition\",
        \"ecs:RegisterTaskDefinition\"
      ],
      \"Resource\": \"*\"
    },
    {
      \"Effect\": \"Allow\",
      \"Action\": [\"iam:PassRole\"],
      \"Resource\": [
        \"arn:aws:iam::$($env:ACCT):role/ecsTaskExecutionRole\",
        \"arn:aws:iam::$($env:ACCT):role/convis-agent-task-role\"
      ]
    }
  ]
}
"@
```

Note the role ARN — you'll paste it implicitly via GitHub repo variables:
`arn:aws:iam::<ACCT>:role/GitHubActionsConvisDeployRole`

---

## Phase 2 — GitHub configuration

### 2.1 Repository variables

Go to GitHub → repo → **Settings → Secrets and variables → Actions → Variables tab → New repository variable**:

| Variable | Value |
|---|---|
| `AWS_REGION` | `ap-south-1` (or whichever you chose) |
| `AWS_ACCOUNT_ID` | your 12-digit account number |
| `NEXT_PUBLIC_API_URL` | `https://api.convis.ai` (or whatever your prod API domain will be) |

No GitHub **Secrets** are needed — OIDC handles AWS auth, app secrets are in AWS Secrets Manager.

### 2.2 Confirm the workflows are present

These files should already be in the repo (committed earlier):
- `.github/workflows/deploy.yml` — main build + push + roll
- `.github/workflows/rollback.yml` — manual rollback to a previous SHA

If not, copy them from a working branch.

---

## Phase 3 — First deploy bootstrap

The workflow assumes the App Runner services and ECS service already exist. On the very first deploy, they don't. You'll seed ECR via GHA, then create the services pointing at the images.

### 3.1 Push code → GHA fires → produces the first images

```powershell
cd c:\Users\Tejas-Psitech\Desktop\Psitech\Convis_India
git push origin Tejas-feature-branch
```

In the GitHub **Actions** tab, watch the run:
- `build-api` job ✓ should succeed → pushes `convis-api:latest` + `convis-api:<sha>` to ECR
- `build-web` job ✓ should succeed → same for convis-web
- `deploy-api`, `deploy-web`, `deploy-agent` jobs ✗ will FAIL because the target services don't exist yet — that's expected on the first run

After the run, verify the images are in ECR:

```powershell
aws ecr describe-images --repository-name convis-api --region $env:REGION --query 'imageDetails[*].imageTags'
aws ecr describe-images --repository-name convis-web --region $env:REGION --query 'imageDetails[*].imageTags'
```

You should see `latest` and a sha-like tag in each.

### 3.2 Create the App Runner service for convis-api

Easiest via AWS Console:

1. AWS Console → **App Runner** → **Create service**
2. **Source and deployment**:
   - Repository type: **Container registry** → **Amazon ECR**
   - Container image URI: `<ACCT>.dkr.ecr.<REGION>.amazonaws.com/convis-api:latest`
   - ECR access role: **Use existing service role** → `AppRunnerECRAccessRole`
   - Deployment trigger: **Automatic** (App Runner re-pulls when ECR `:latest` is updated)
3. **Service settings**:
   - Service name: `convis-api`
   - Virtual CPU: 1 vCPU
   - Memory: 2 GB
   - Port: `8000`
4. **Environment variables (non-secret)** — paste these:
   ```
   ENVIRONMENT       = production
   DATABASE_NAME     = convis_india
   LIVEKIT_AGENT_NAME = convis-agent
   API_BASE_URL      = https://api.convis.ai
   FRONTEND_URL      = https://app.convis.ai
   CORS_ORIGINS      = https://app.convis.ai
   ```
5. **Environment secrets** — for each entry below, add it pointing at the Secrets Manager ARN. The ARN format is `arn:aws:secretsmanager:<REGION>:<ACCT>:secret:convis/<key>`:
   ```
   MONGODB_URI
   JWT_SECRET
   ENCRYPTION_KEY
   EMAIL_USER
   EMAIL_PASS
   SARVAM_API_KEY
   OPENAI_API_KEY
   LIVEKIT_URL
   LIVEKIT_API_KEY
   LIVEKIT_API_SECRET
   LIVEKIT_SIP_OUTBOUND_TRUNK_ID
   LIVEKIT_SIP_INBOUND_HOST
   ```
6. **Security**:
   - Instance role: `convis-api-instance-role`
7. **Health check**:
   - Protocol: HTTP
   - Path: `/health`
   - Interval: 20s, Timeout: 5s
8. **Networking** → leave default (public)
9. Click **Create & deploy**

App Runner returns an `https://<random>.<region>.awsapprunner.com` URL. The first deployment takes ~5 minutes.

Verify:
```powershell
curl https://<your-app-runner-url>/health     # → {"status":"healthy"}
```

### 3.3 Create the App Runner service for convis-web

Same flow, image `convis-web:latest`, port `3000`. No secrets needed (the Next.js bundle has `NEXT_PUBLIC_API_URL` baked in at build time). No instance role needed.

### 3.4 Create the ECS cluster + agent worker service

```powershell
# Substitute placeholders into the task def
$taskDef = Get-Content "./deployment-docs/aws/ecs-livekit-agent-task.json" -Raw
$taskDef = $taskDef.Replace('${REGION}', $env:REGION).Replace('${ACCT}', $env:ACCT)
$taskDef | Out-File -Encoding utf8 "$env:TEMP\agent-task.json"

# Register task definition
aws ecs register-task-definition --cli-input-json "file://$env:TEMP/agent-task.json" --region $env:REGION

# Create cluster
aws ecs create-cluster --cluster-name convis --region $env:REGION

# Find your default VPC subnets and security group
aws ec2 describe-vpcs --filters "Name=is-default,Values=true" --region $env:REGION --query 'Vpcs[0].VpcId' --output text
# Use the VpcId above to find subnets:
aws ec2 describe-subnets --filters "Name=vpc-id,Values=<VPC_ID>" --region $env:REGION --query 'Subnets[*].SubnetId'
aws ec2 describe-security-groups --filters "Name=vpc-id,Values=<VPC_ID>" "Name=group-name,Values=default" --region $env:REGION --query 'SecurityGroups[0].GroupId' --output text

# Then create the service (replace SUBNETS and SG values)
$SUBNETS = "subnet-xxxxxxxx,subnet-yyyyyyyy"
$SG = "sg-zzzzzzzz"

aws ecs create-service `
  --cluster convis `
  --service-name convis-livekit-agent `
  --task-definition convis-livekit-agent `
  --desired-count 1 `
  --launch-type FARGATE `
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" `
  --region $env:REGION
```

Tail logs to confirm the agent registered with LiveKit:

```powershell
aws logs tail /ecs/convis-livekit-agent --follow --region $env:REGION
```

Expected output (first ~30 seconds of cold start):
```
[AGENT] prewarm: Silero VAD loaded
[AGENT] prewarm: MongoDB connection established
[AGENT] prewarm: TLS+DNS warmed for ['api.sarvam.ai']
INFO livekit.agents - starting worker convis-agent
INFO livekit.agents - registered worker
```

### 3.5 Re-run the GitHub Actions workflow

Now that the services exist, GHA can roll them. Trigger the workflow:
- Push a small commit to the branch, OR
- GitHub → **Actions tab** → click `Deploy to AWS` → **Run workflow** → pick your branch → Run

All five jobs should pass this time. The deploy is live.

---

## Phase 4 — Custom domain + TLS (optional but recommended)

```powershell
$apiArn = aws apprunner list-services --region $env:REGION `
  --query "ServiceSummaryList[?ServiceName=='convis-api'].ServiceArn|[0]" --output text
aws apprunner associate-custom-domain `
  --service-arn $apiArn `
  --domain-name api.convis.ai

$webArn = aws apprunner list-services --region $env:REGION `
  --query "ServiceSummaryList[?ServiceName=='convis-web'].ServiceArn|[0]" --output text
aws apprunner associate-custom-domain `
  --service-arn $webArn `
  --domain-name app.convis.ai
```

App Runner returns CNAME records — add them at your DNS provider. ACM cert is auto-provisioned, takes ~5-15 min to validate.

After DNS propagates, change `API_BASE_URL` / `FRONTEND_URL` / `CORS_ORIGINS` in the App Runner config to the real domains, and update the `NEXT_PUBLIC_API_URL` GitHub variable. Trigger a redeploy.

---

## Phase 5 — Day-2 operations

### Subsequent deploys

Just push to your branch:
```powershell
git commit -am "fix XYZ"
git push
```

GHA fires. Watch the Actions tab. Live in ~5 min.

### Rollback to a previous SHA

When something breaks in production:

1. Go to GitHub → **Actions tab** → **Rollback to a previous SHA** workflow
2. Click **Run workflow**
3. Inputs:
   - **sha**: a previous git SHA (find it in the deploy workflow's run history)
   - **service**: pick `all`, `convis-api`, `convis-web`, or `agent-only`
   - **reason**: a short note for the team log
4. Click Run

The workflow re-tags the old SHA's image as `:latest` in ECR and triggers redeploys. ~2-5 minutes to fully cut over.

### Verify the live deploy

```powershell
# API health
curl https://api.convis.ai/health

# Web is up
curl -I https://app.convis.ai

# Agent worker logs
aws logs tail /ecs/convis-livekit-agent --follow --region ap-south-1

# Look at what image App Runner is currently serving
aws apprunner describe-service `
  --service-arn $(aws apprunner list-services --region $env:REGION --query "ServiceSummaryList[?ServiceName=='convis-api'].ServiceArn|[0]" --output text) `
  --region $env:REGION `
  --query 'Service.SourceConfiguration.ImageRepository.ImageIdentifier'
```

### Update a secret

Edit your local `.env`, then re-run the import script:
```powershell
bash ./deployment-docs/aws/import-secrets.sh ./convis-api/.env
```

App Runner pulls secrets at container start, not on update. To pick up a changed secret, **force a redeploy**:
```powershell
$apiArn = aws apprunner list-services --region $env:REGION `
  --query "ServiceSummaryList[?ServiceName=='convis-api'].ServiceArn|[0]" --output text
aws apprunner start-deployment --service-arn $apiArn --region $env:REGION
```
Or just push a no-op commit.

### Scale up

```powershell
# App Runner: change vCPU/RAM (requires service update via console or aws apprunner update-service)

# ECS agent: increase desired count (e.g. 2 workers for higher concurrency)
aws ecs update-service `
  --cluster convis `
  --service convis-livekit-agent `
  --desired-count 2 `
  --region $env:REGION
```

---

## Verification checklist after deploy

| Check | How |
|---|---|
| API healthy | `curl https://api.convis.ai/health` → `{"status":"healthy"}` |
| Web loads | Open `https://app.convis.ai` in browser, login screen renders |
| Agent registered with LiveKit | `aws logs tail /ecs/convis-livekit-agent` → `registered worker` |
| Browser test call works | Login → AI Agent → click Browser call → hear Sarvam greeting |
| Outbound PSTN works | `/phone-numbers` → click Phone → enter your mobile → it rings |
| Vobiz wallet positive | check Vobiz dashboard (negative balance = `USER_REJECTED` on outbound) |
| Sarvam credits not exhausted | check Sarvam dashboard |
| MongoDB writes go to `convis_india` | API logs show `Successfully connected to MongoDB`, dashboard creates assistants without errors |

---

## Common gotchas

**1. `Not authorized to perform sts:AssumeRoleWithWebIdentity` in GHA logs**
The OIDC trust policy `StringLike` condition doesn't match the actual `sub` claim. Confirm `repo:PsiTechC/Convis_India:*` exactly matches your GitHub owner/repo.

**2. App Runner service "create" step succeeds but app crashes immediately**
Usually a missing secret. Check the App Runner service logs in CloudWatch — look for `KeyError` or `Settings validation failed: missing X`. Add the secret to Secrets Manager + add to service config + force-redeploy.

**3. `aws ecr put-image: ImageAlreadyExistsException` during rollback**
The `latest` tag is already pointing at the desired image. Means the rollback is already in the state you want. Nothing to do.

**4. `not authorized on convis_india` from MongoDB**
The MongoDB user grants are scoped to `convis_python`. Grant `readWrite` on `convis_india` to the same user. Atlas: Database Access → Edit User → Database Permissions → add `convis_india`.

**5. ECS agent task starts then dies in <30 seconds**
Usually `LIVEKIT_URL` not set, or wrong secret ARN. `aws logs tail /ecs/convis-livekit-agent` → look for `ws_url is required` or `Failed to retrieve secret`.

**6. App Runner says "deployment succeeded" but old code still serving**
Browser cache, OR App Runner served a cached image. Hard-refresh; check the image SHA via `aws apprunner describe-service` query (see Day-2 ops).

**7. `NEXT_PUBLIC_API_URL` not actually applied in the browser**
Next.js `NEXT_PUBLIC_*` is baked in at `docker build` time, not runtime. To change it: update the `NEXT_PUBLIC_API_URL` GitHub variable, then trigger a new build (push a commit or rerun the workflow). Runtime App Runner env vars do NOT affect the browser bundle.

**8. Vobiz outbound calls fail with `USER_REJECTED`**
Almost always one of: negative Vobiz wallet, DLT registration missing, or LiveKit egress IP not in Vobiz IP ACL. Check the Vobiz call log on Vobiz side for the SIP response code.

**9. GHA build takes 10+ minutes consistently**
Layer cache not working. Ensure the workflow has `cache-from: type=gha` and `cache-to: type=gha,mode=max` on the buildx step. First build is always slow; subsequent should be 2-3 min.

**10. ECR storage filling up**
ECR keeps every image you push. After 6 months you'll have hundreds of `<sha>` tags. Set a lifecycle policy:
```powershell
aws ecr put-lifecycle-policy --repository-name convis-api --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"Keep last 20 sha tags + always-latest","selection":{"tagStatus":"tagged","tagPatternList":["*"],"countType":"imageCountMoreThan","countNumber":20},"action":{"type":"expire"}}]}'
```

---

## Manual recovery / disaster procedures

### Roll back via console (when GHA is unavailable)

1. AWS Console → App Runner → service → Configuration → Image URI
2. Change tag from `:latest` to `:<previous-sha>` (find SHAs in ECR console)
3. Save → service redeploys

### Pause auto-deploy temporarily

Turn off automatic deployment in the App Runner Console → Configuration. Manual rolls only after that until you re-enable.

### Stop the agent worker

```powershell
aws ecs update-service --cluster convis --service convis-livekit-agent --desired-count 0 --region $env:REGION
```

Inbound calls during the outage will time out at LiveKit (no agent picks up). Restart with `--desired-count 1`.

---

## Cost expectations

Approximate monthly cost for a low-traffic Indian-only deployment (1-5 concurrent calls average):

| Service | Cost (USD/month) | Notes |
|---|---|---|
| App Runner (convis-api) | $30-50 | 1 vCPU / 2 GB, autoscales |
| App Runner (convis-web) | $20-40 | Same config |
| ECS Fargate (agent) | $30 | Always-on, 1 vCPU / 2 GB |
| ECR storage | $1-5 | Depends on image retention |
| Secrets Manager | $4 | $0.40/secret/month × ~10 secrets |
| CloudWatch Logs | $5-15 | Depends on log volume |
| Data transfer out | $10-30 | LiveKit ↔ ECS audio bandwidth |
| **AWS subtotal** | **~$100-150** | |
| LiveKit Cloud | $20-100 | Beyond free tier; ~$0.01/min talking |
| Sarvam API | varies | Per-token pricing; ~$0.30-3 per voice agent call |
| Vobiz | varies | ~₹0.50-1/min outbound mobile |

GitHub Actions for a private repo on free tier: **$0** if you stay under 2000 build minutes/month.

---

## Reference

- Main workflow: [.github/workflows/deploy.yml](../../.github/workflows/deploy.yml)
- Rollback workflow: [.github/workflows/rollback.yml](../../.github/workflows/rollback.yml)
- Agent ECS task definition: [ecs-livekit-agent-task.json](ecs-livekit-agent-task.json)
- App Runner config (informational): [convis-api/apprunner.yaml](../../convis-api/apprunner.yaml)
- Original AWS deploy doc (manual flow): [AWS_APP_RUNNER.md](AWS_APP_RUNNER.md)
- Secrets import script: [import-secrets.sh](import-secrets.sh)

---

## Quick reference — first-deploy checklist

Copy-paste this and tick each step:

```
[ ] 1.1  Set shell variables ACCT, REGION
[ ] 1.2  Create ECR repos: convis-api, convis-web
[ ] 1.3  Push secrets to Secrets Manager (import-secrets.sh)
[ ] 1.4a AppRunnerECRAccessRole created
[ ] 1.4b convis-api-instance-role created + SecretsRead policy
[ ] 1.4c ecsTaskExecutionRole + convis-agent-task-role created
[ ] 1.5  GitHub OIDC provider added to AWS
[ ] 1.6  GitHubActionsConvisDeployRole created + DeployPermissions policy
[ ] 2.1  GitHub repo variables set: AWS_REGION, AWS_ACCOUNT_ID, NEXT_PUBLIC_API_URL
[ ] 2.2  .github/workflows/deploy.yml + rollback.yml committed
[ ] 3.1  git push → GHA runs → builds images, deploy jobs fail (expected first time)
[ ] 3.2  App Runner convis-api service created (Console)
[ ] 3.3  App Runner convis-web service created (Console)
[ ] 3.4  ECS cluster + convis-livekit-agent service created
[ ] 3.5  Re-run GHA → all jobs pass
[ ] 4    Custom domain + TLS attached (optional)
[ ] V1   curl /health → 200
[ ] V2   browser test call works end-to-end
[ ] V3   outbound PSTN call rings, agent answers
```
