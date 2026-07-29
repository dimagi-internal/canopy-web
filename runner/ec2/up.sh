#!/usr/bin/env bash
# Spin UP the canopy cloud runner via CloudFormation. Idempotent (create or update).
# Secrets must already be in Secrets Manager — see ./secrets.sh.
#
#   aws sso login --profile labs
#   ./secrets.sh canopy <pat-file>     # once
#   ./secrets.sh claude <token-file>   # once (a VALID claude OAuth token)
#   ./up.sh                            # deploy the stack
#   ./down.sh                          # delete the stack
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="${AWS_PROFILE:-labs}"
REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-canopy-cloud-runner}"
AWS=(aws --profile "$PROFILE" --region "$REGION")

echo ">> account"; "${AWS[@]}" sts get-caller-identity --query Account --output text

# Secrets must exist first (the instance role reads them at boot).
for s in canopy/cloud-runner/canopy-pat canopy/cloud-runner/claude-oauth-token; do
  if ! "${AWS[@]}" secretsmanager describe-secret --secret-id "$s" >/dev/null 2>&1; then
    echo "!! missing secret '$s' — run ./secrets.sh first" >&2; exit 1
  fi
done

MYIP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
echo ">> SSH allowed from ${MYIP}/32"

# Publish cloud_runner.py to Secrets Manager as a single-line gzip+base64 blob.
# It deliberately does NOT go in UserData: EC2 caps UserData at a hard 16384 bytes
# and the gz+b64 runner alone is ~12 KB, so splicing it in blew the cap and the
# stack CREATE_FAILED with "User data is limited to 16384 bytes". The instance role
# already grants GetSecretValue on canopy/cloud-runner/*, so this needs no new IAM,
# and canopy-fetch-env re-fetches it on every service start (see runner.cfn.yaml).
CODE_SECRET="${CODE_SECRET:-canopy/cloud-runner/runner-code}"
echo ">> publishing cloud_runner.py -> $CODE_SECRET"
B64=$(python3 -c "import base64,gzip;print(base64.b64encode(gzip.compress(open('cloud_runner.py','rb').read())).decode())")
if "${AWS[@]}" secretsmanager describe-secret --secret-id "$CODE_SECRET" >/dev/null 2>&1; then
  "${AWS[@]}" secretsmanager put-secret-value --secret-id "$CODE_SECRET" \
    --secret-string "$B64" >/dev/null
else
  "${AWS[@]}" secretsmanager create-secret --name "$CODE_SECRET" \
    --description 'cloud_runner.py (gzip+base64) — published by up.sh' \
    --secret-string "$B64" >/dev/null
fi
echo "   ${#B64} bytes published"

echo ">> validating template"
"${AWS[@]}" cloudformation validate-template --template-body "file://runner.cfn.yaml" >/dev/null

echo ">> deploying stack $STACK"
"${AWS[@]}" cloudformation deploy \
  --stack-name "$STACK" --template-file runner.cfn.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "SshCidr=${MYIP}/32" \
  ${EXTRA_PARAMS:-}

echo ">> outputs"
OUT=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output json)
IP=$(echo "$OUT" | python3 -c "import sys,json;print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='PublicIp'))")
KID=$(echo "$OUT" | python3 -c "import sys,json;print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='KeyPairId'))")

# Pull the CFN-managed private key out of SSM for SSH access.
KEYFILE="./${STACK}-key.pem"
"${AWS[@]}" ssm get-parameter --name "/ec2/keypair/${KID}" --with-decryption \
  --query 'Parameter.Value' --output text > "$KEYFILE"
chmod 600 "$KEYFILE"

echo ""
echo "==> UP. ip=$IP"
echo "    ssh:  ssh -i $KEYFILE ubuntu@$IP"
echo "    logs: ssh -i $KEYFILE ubuntu@$IP 'journalctl -u canopy-runner -f'"
echo "    cloud-init boots the runner automatically (give it ~3 min for node+claude)."
