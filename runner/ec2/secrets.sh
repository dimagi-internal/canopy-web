#!/usr/bin/env bash
# Put/update the runner's secrets in Secrets Manager. Values are read from a FILE
# (or stdin) so they never land in shell history.
#
#   ./secrets.sh canopy path/to/pat.txt          # canopy-web PAT
#   ./secrets.sh claude path/to/claude-token.txt # claude OAuth token
#   ./secrets.sh op path/to/sa-token.txt         # 1Password service-account token
#                                                 # (wire.sh stages it into the runner's
#                                                 # credential bundle for bootstrap_agents.sh)
#   ./secrets.sh gog path/to/keyring-pw.txt      # password for gog's `file` keyring
#                                                 # backend; without it every
#                                                 # `gog auth tokens import` fails on a
#                                                 # headless box (no TTY for the prompt)
#   echo -n "<token>" | ./secrets.sh claude -     # or via stdin
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="${AWS_PROFILE:-labs}"
REGION="${AWS_REGION:-us-east-1}"
AWS=(aws --profile "$PROFILE" --region "$REGION")

kind="${1:-}"; src="${2:-}"
case "$kind" in
  canopy) SECRET=canopy/cloud-runner/canopy-pat ;;
  claude) SECRET=canopy/cloud-runner/claude-oauth-token ;;
  op)     SECRET=canopy/cloud-runner/op-service-account-token ;;
  gog)    SECRET=canopy/cloud-runner/gog-keyring-password ;;
  *) echo "usage: ./secrets.sh {canopy|claude|op|gog} <file|->" >&2; exit 1 ;;
esac
[[ -n "$src" ]] || { echo "give a file path or - for stdin" >&2; exit 1; }

if [[ "$src" == "-" ]]; then VALUE=$(cat); else VALUE=$(cat "$src"); fi
VALUE="${VALUE%$'\n'}"  # strip a single trailing newline
[[ -n "$VALUE" ]] || { echo "empty value" >&2; exit 1; }

if "${AWS[@]}" secretsmanager describe-secret --secret-id "$SECRET" >/dev/null 2>&1; then
  "${AWS[@]}" secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "$VALUE" >/dev/null
  echo "updated $SECRET (${#VALUE} chars)"
else
  "${AWS[@]}" secretsmanager create-secret --name "$SECRET" \
    --description "canopy cloud runner ($kind)" --secret-string "$VALUE" >/dev/null
  echo "created $SECRET (${#VALUE} chars)"
fi
