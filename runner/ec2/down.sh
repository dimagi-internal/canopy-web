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
  # Every secret this stack owns. The four a human stages via secrets.sh, plus the
  # two up.sh publishes as the first-boot seed. gog-keyring-password was missing
  # here until 2026-09-05: a "full" purge left it behind, so a rebuild could pass
  # on inherited state — the exact false green a purge-then-rebuild exists to rule
  # out. Kept in step with secrets.sh/up.sh/wire.sh/runner.cfn.yaml by
  # tests/test_purge_secrets_is_complete.py, which derives the expected set from
  # those files rather than restating it.
  for s in canopy/cloud-runner/canopy-pat \
           canopy/cloud-runner/claude-oauth-token \
           canopy/cloud-runner/op-service-account-token \
           canopy/cloud-runner/gog-keyring-password \
           canopy/cloud-runner/runner-code \
           canopy/cloud-runner/runner-code-sha; do
    echo ">> deleting secret $s"
    "${AWS[@]}" secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery || true
  done
fi
