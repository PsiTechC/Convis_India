#!/usr/bin/env bash
# import-secrets.sh — push every secret into AWS Secrets Manager once.
#
# Usage:
#   AWS_REGION=us-east-1 AWS_PROFILE=default \
#     ./import-secrets.sh /path/to/convis-api/.env
#
# Reads KEY=VALUE lines from the .env file (skips comments + blanks) and
# upserts each into Secrets Manager under name "convis/<lowercase_key>".
# Idempotent — re-running updates existing secrets in place.
set -euo pipefail

ENV_FILE="${1:-./convis-api/.env}"
REGION="${AWS_REGION:?AWS_REGION must be set}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file not found: $ENV_FILE" >&2
  exit 1
fi

# Whitelist of vars to push as secrets — anything else is rejected to avoid
# accidentally publishing comments / placeholders.
SECRETS=(
  MONGODB_URI
  JWT_SECRET
  ENCRYPTION_KEY
  EMAIL_USER
  EMAIL_PASS
  OPENAI_API_KEY
  DEEPGRAM_API_KEY
  ELEVENLABS_API_KEY
  LIVEKIT_URL
  LIVEKIT_API_KEY
  LIVEKIT_API_SECRET
  LIVEKIT_SIP_INBOUND_HOST
  LIVEKIT_SIP_OUTBOUND_TRUNK_ID
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
)

put_secret() {
  local key="$1" value="$2"
  local name="convis/$(echo "$key" | tr '[:upper:]' '[:lower:]')"
  echo ">> Upserting $name (${#value} chars)"
  if aws secretsmanager describe-secret --region "$REGION" --secret-id "$name" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value \
      --region "$REGION" \
      --secret-id "$name" \
      --secret-string "$value" >/dev/null
  else
    aws secretsmanager create-secret \
      --region "$REGION" \
      --name "$name" \
      --secret-string "$value" >/dev/null
  fi
}

# Read the .env file and push each whitelisted key.
while IFS= read -r line || [[ -n "$line" ]]; do
  # skip blanks + comments
  [[ -z "$line" || "$line" == \#* ]] && continue
  # split on first =
  key="${line%%=*}"
  value="${line#*=}"
  # trim whitespace from key
  key="$(echo "$key" | tr -d ' \t')"
  # skip if not in whitelist
  found=0
  for k in "${SECRETS[@]}"; do
    [[ "$k" == "$key" ]] && found=1 && break
  done
  [[ $found -eq 0 ]] && continue
  # skip empty values
  [[ -z "$value" ]] && { echo "skip $key (empty)"; continue; }
  put_secret "$key" "$value"
done < "$ENV_FILE"

echo
echo "Done. Verify with: aws secretsmanager list-secrets --region $REGION --query 'SecretList[?starts_with(Name, \`convis/\`)].Name'"
