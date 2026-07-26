#!/usr/bin/env bash
#
# Apply the canopy-web CloudFormation stack SAFELY.
#
# Why this exists
# ---------------
# Two things write to this stack's ECS resources: CloudFormation (which declares
# TaskDefinition + Service) and the deploy workflow (which registers task-def
# revisions and calls update-service directly, without telling CloudFormation).
# The stack's `ImageTag` parameter therefore freezes at whatever it was on the
# last apply, while the live service moves on with every deploy.
#
# On 2026-07-26 that gap was TEN DAYS and 118 task-def revisions. A plain
# `aws cloudformation deploy` would have re-registered the task definition from
# the stale parameter and pointed the service at it — a silent rollback to
# ten-day-old code, reported as a successful stack update.
#
# So the rule was: "before touching the stack, pin ImageTag to whatever is
# actually running." This script IS that rule, so nobody has to hold it in their
# head. It reads the running image off the live service, pins it, shows you the
# resource diff, and makes you confirm before anything changes.
#
# This is a safety net, not the destination. The real fix is to stop
# CloudFormation declaring TaskDefinition + Service at all, so releases and
# infrastructure have one writer each. Once that lands, delete this.
#
# Usage:  deploy/aws/apply-stack.sh [--yes] [--no-wait]
#         --yes       skip the confirmation prompt (CI / you already read the diff)
#         --no-wait   execute and return immediately instead of blocking on the
#                     rollout. A stack update takes minutes; an agent calling this
#                     will hit its command timeout and be left unsure whether the
#                     apply fired. It did — but "unsure" is the worst state to be
#                     in against prod, so offer a mode that never gets there.
set -euo pipefail

PROFILE="${AWS_PROFILE:-labs}"
STACK="${STACK_NAME:-canopy-web}"
CLUSTER="${CLUSTER_NAME:-labs-jj-cluster}"
SERVICE="${SERVICE_NAME:-labs-jj-canopy-web}"
TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/canopy-web.cfn.yaml"
CHANGE_SET="apply-$(date +%Y%m%d-%H%M%S)"
ASSUME_YES=""
NO_WAIT=""
for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=1 ;;
    --no-wait) NO_WAIT=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "Reading the image the service is ACTUALLY running…"
LIVE_TASKDEF=$(aws ecs describe-services --profile "$PROFILE" \
  --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].taskDefinition' --output text)
LIVE_IMAGE=$(aws ecs describe-task-definition --profile "$PROFILE" \
  --task-definition "$LIVE_TASKDEF" \
  --query 'taskDefinition.containerDefinitions[0].image' --output text)
LIVE_TAG="${LIVE_IMAGE##*:}"

STACK_TAG=$(aws cloudformation describe-stacks --profile "$PROFILE" \
  --stack-name "$STACK" \
  --query "Stacks[0].Parameters[?ParameterKey=='ImageTag'].ParameterValue | [0]" \
  --output text)

echo "  running now:      $LIVE_TAG"
echo "  stack believes:   $STACK_TAG"
if [ "$LIVE_TAG" != "$STACK_TAG" ]; then
  echo "  -> drifted; pinning ImageTag to the running image so this apply cannot roll back"
fi

# Every parameter carried forward EXCEPT ImageTag, which is pinned to reality.
# UsePreviousValue matters: `aws cloudformation deploy` would silently substitute
# template DEFAULTS for anything not overridden — and DesiredCount defaults to 0,
# which would scale the service to zero and take the site down.
say "Creating change set '$CHANGE_SET'…"
aws cloudformation create-change-set --profile "$PROFILE" \
  --stack-name "$STACK" \
  --change-set-name "$CHANGE_SET" \
  --template-body "file://$TEMPLATE" \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=VpcId,UsePreviousValue=true \
    ParameterKey=Subnets,UsePreviousValue=true \
    ParameterKey=SecurityGroup,UsePreviousValue=true \
    ParameterKey=ClusterName,UsePreviousValue=true \
    ParameterKey=HttpsListenerArn,UsePreviousValue=true \
    ParameterKey=ExecutionRoleArn,UsePreviousValue=true \
    ParameterKey=TaskRoleArn,UsePreviousValue=true \
    ParameterKey=ContainerPort,UsePreviousValue=true \
    ParameterKey=DesiredCount,UsePreviousValue=true \
    ParameterKey=ImageTag,ParameterValue="$LIVE_TAG" \
  --output text --query 'Id' >/dev/null

# FAILED here is usually "no changes" — a success for our purposes, not an error.
if ! aws cloudformation wait change-set-create-complete --profile "$PROFILE" \
      --stack-name "$STACK" --change-set-name "$CHANGE_SET" 2>/dev/null; then
  REASON=$(aws cloudformation describe-change-set --profile "$PROFILE" \
    --stack-name "$STACK" --change-set-name "$CHANGE_SET" \
    --query 'StatusReason' --output text 2>/dev/null || echo "unknown")
  aws cloudformation delete-change-set --profile "$PROFILE" \
    --stack-name "$STACK" --change-set-name "$CHANGE_SET" >/dev/null 2>&1 || true
  case "$REASON" in
    *"didn't contain changes"*|*"No updates"*)
      say "Nothing to do — the stack already matches the template."; exit 0 ;;
    *)
      echo "change set failed: $REASON" >&2; exit 1 ;;
  esac
fi

say "This apply will change:"
aws cloudformation describe-change-set --profile "$PROFILE" \
  --stack-name "$STACK" --change-set-name "$CHANGE_SET" \
  --query 'Changes[].ResourceChange.{Action:Action,Resource:LogicalResourceId,Type:ResourceType,Replacement:Replacement}' \
  --output table

cat <<'NOTE'
Read that list before continuing. Adding/modifying durable resources (buckets,
secrets, log groups) is routine. TaskDefinition + Service showing up is EXPECTED
whenever container config changes — it means a rolling restart on the SAME image
pinned above. A `Remove` on Service is never routine: check it says Retain first.
NOTE

if [ -z "$ASSUME_YES" ]; then
  read -r -p $'\nExecute this change set? [y/N] ' reply
  if [ "$reply" != "y" ] && [ "$reply" != "Y" ]; then
    aws cloudformation delete-change-set --profile "$PROFILE" \
      --stack-name "$STACK" --change-set-name "$CHANGE_SET" >/dev/null
    say "Aborted; change set deleted. Nothing was applied."
    exit 0
  fi
fi

say "Executing…"
aws cloudformation execute-change-set --profile "$PROFILE" \
  --stack-name "$STACK" --change-set-name "$CHANGE_SET"

if [ -n "$NO_WAIT" ]; then
  cat <<NOTE

Executing in the background. The apply HAS fired. Watch it with:

  aws cloudformation describe-stacks --profile $PROFILE --stack-name $STACK \
    --query 'Stacks[0].StackStatus' --output text
NOTE
  exit 0
fi

aws cloudformation wait stack-update-complete --profile "$PROFILE" --stack-name "$STACK"

say "Done. Stack status:"
aws cloudformation describe-stacks --profile "$PROFILE" --stack-name "$STACK" \
  --query 'Stacks[0].StackStatus' --output text
