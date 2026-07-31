#!/usr/bin/env bash
# Spin DOWN: delete the CloudFormation stack (instance, SG, role, key pair). Secrets
# in Secrets Manager are left in place for reuse; pass --purge-secrets to delete them.
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="${AWS_PROFILE:-labs}"
REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-canopy-cloud-runner}"
AWS=(aws --profile "$PROFILE" --region "$REGION")

echo ">> deleting stack $STACK"
"${AWS[@]}" cloudformation delete-stack --stack-name "$STACK"
"${AWS[@]}" cloudformation wait stack-delete-complete --stack-name "$STACK"
rm -f "./${STACK}-key.pem"
echo "==> stack deleted."

if [[ "${1:-}" == "--purge-secrets" ]]; then
  for s in canopy/cloud-runner/canopy-pat canopy/cloud-runner/claude-oauth-token canopy/cloud-runner/op-service-account-token canopy/cloud-runner/runner-code canopy/cloud-runner/runner-code-sha; do
    echo ">> deleting secret $s"
    "${AWS[@]}" secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery || true
  done
fi
