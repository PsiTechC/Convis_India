# Registers the convis-livekit-agent task definition in ap-south-1 with
# all env vars from convis-api/.env inlined as plain env (no Secrets Manager).
#
# Usage:
#   pwsh deployment-docs/aws/register-agent-task-ap-south-1.ps1
#   # or from Windows PowerShell 5.1:
#   powershell -ExecutionPolicy Bypass -File deployment-docs/aws/register-agent-task-ap-south-1.ps1
#
# Re-running is safe — every call creates a new task definition revision.
# The .env file is read fresh each time, so update .env then re-run to roll new env vars.

$ErrorActionPreference = 'Stop'

# Config (override via $env: if you ever need to)
$Region        = if ($env:AWS_REGION) { $env:AWS_REGION } else { 'ap-south-1' }
$AccountId     = '942617679452'
$Family        = 'convis-livekit-agent'
$ImageUri      = "$AccountId.dkr.ecr.$Region.amazonaws.com/convis-api:latest"
$ExecRoleArn   = "arn:aws:iam::$AccountId`:role/ecsTaskExecutionRole"
$TaskRoleArn   = "arn:aws:iam::$AccountId`:role/convis-agent-task-role"
$LogGroup      = '/ecs/convis-livekit-agent'

# Locate .env relative to the script (script lives in deployment-docs/aws/)
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$EnvFile  = Join-Path $RepoRoot 'convis-api\.env'
if (-not (Test-Path $EnvFile)) {
    Write-Error "Env file not found: $EnvFile"
    exit 1
}
Write-Host "Reading env from: $EnvFile" -ForegroundColor Cyan

# Parse .env: KEY=VALUE per line, # comments, blank lines ignored.
# Trim trailing/leading whitespace from both key and value to handle the
# 'LIVEKIT_SIP_INBOUND_HOST ' (trailing space) bug in the source .env.
$envEntries = [System.Collections.ArrayList]::new()
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#')) { return }
    if ($line -match '^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
        $name  = $matches[1].Trim()
        $value = $matches[2].Trim()
        # Strip surrounding quotes if present
        if ($value -match '^"(.*)"$') { $value = $matches[1] }
        if ($value -match "^'(.*)'$") { $value = $matches[1] }
        # Skip empty values — passing empty env vars to AWS is fine but noisy
        if ($value) {
            [void]$envEntries.Add([ordered]@{ name = $name; value = $value })
        }
    }
}

# Production overrides: .env has ENVIRONMENT=development; the deployed agent
# must run as 'production' regardless. LIVEKIT_AGENT_NAME pinned to convis-agent.
$forced = @{
    'ENVIRONMENT'        = 'production'
    'LIVEKIT_AGENT_NAME' = 'convis-agent'
}
foreach ($k in $forced.Keys) {
    $existing = $envEntries | Where-Object { $_.name -eq $k }
    if ($existing) { $existing.value = $forced[$k] }
    else { [void]$envEntries.Add([ordered]@{ name = $k; value = $forced[$k] }) }
}

Write-Host ("Loaded {0} env vars from .env (plus overrides)" -f $envEntries.Count) -ForegroundColor Cyan

# Build the task definition object
$taskDef = [ordered]@{
    family                  = $Family
    networkMode             = 'awsvpc'
    requiresCompatibilities = @('FARGATE')
    cpu                     = '1024'
    memory                  = '2048'
    executionRoleArn        = $ExecRoleArn
    taskRoleArn             = $TaskRoleArn
    containerDefinitions    = @(
        [ordered]@{
            name        = 'livekit-agent'
            image       = $ImageUri
            command     = @('python', '-m', 'app.services.livekit.agent_worker', 'start')
            essential   = $true
            environment = @($envEntries)
            logConfiguration = [ordered]@{
                logDriver = 'awslogs'
                options   = [ordered]@{
                    'awslogs-group'         = $LogGroup
                    'awslogs-create-group'  = 'true'
                    'awslogs-region'        = $Region
                    'awslogs-stream-prefix' = 'agent'
                }
            }
        }
    )
}

# Serialize to JSON and write to a temp file (no BOM — IAM/ECS reject BOM)
$json = $taskDef | ConvertTo-Json -Depth 12
$tempFile = Join-Path $env:TEMP "convis-agent-task-def.json"
[System.IO.File]::WriteAllText($tempFile, $json)
Write-Host "Task def written to: $tempFile" -ForegroundColor Cyan

# Register the task definition with ECS
Write-Host "Registering task definition with ECS in $Region ..." -ForegroundColor Cyan
$registerOutput = aws ecs register-task-definition `
    --cli-input-json "file://$tempFile" `
    --region $Region `
    --query 'taskDefinition.{family:family,revision:revision,status:status,taskDefinitionArn:taskDefinitionArn}' `
    --output table

if ($LASTEXITCODE -ne 0) {
    Write-Error "register-task-definition failed. Temp file kept at $tempFile for inspection."
    exit 1
}

Write-Output $registerOutput

# Cleanup
Remove-Item $tempFile -Force -ErrorAction SilentlyContinue
Write-Host "Done. Next step: create the ECS service (see deployment-docs/aws/AWS_ECS_EXPRESS_MODE.md Part 8c)." -ForegroundColor Green
