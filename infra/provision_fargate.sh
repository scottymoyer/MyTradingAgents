#!/usr/bin/env bash
# Provision the serverless-batch COMPUTE layer for TradingAgents:
#   - KMS customer-managed key (CMK)   — encrypts the runtime secret
#   - Secrets Manager secret            — the run's API keys + DB URL (CMK-encrypted)
#   - Fargate security group            — + ingress on the RDS SG so the task reaches Postgres
#   - CloudWatch Logs group             — task logs
#   - IAM roles                         — task execution / task / scheduler
#   - ECS Fargate cluster + task def    — the run_ddog batch chain (fargate_entry.sh)
#
# One-time and re-runnable (tolerates already-exists). The MWF EventBridge schedule
# is created separately (and DISABLED) by:  ./infra/provision_fargate.sh schedule
# so a manual run can be verified before the schedule is allowed to fire.
#
# Secrets are read from the box's existing env files into a 0600 scratch JSON and
# handed to AWS via --secret-string file://… ; the scratch file is shredded and NO
# secret value is ever printed/echoed/logged (only key NAMES are).
#
# Why a CMK: the earlier Aurora/RDS failures were the *default AWS-managed* KMS keys
# being inaccessible on this Free-Plan account. A customer-managed key is our own and
# works; if create-key unexpectedly fails here, fall back to SSM Parameter Store String.
#
# Requires the tradingagents-eks-builder instance role. It lacks ECS/KMS/Secrets/
# Scheduler/Logs perms, so STEP 0 self-attaches an inline policy granting them
# (the role already has IAMFullAccess).
set -uo pipefail
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-2}"
REGION="$AWS_DEFAULT_REGION"

# ---- coordinates (verified) ------------------------------------------------
ACCT=963910217112
VPC=vpc-0929617e55f0cced4
# All three subnets are public (MapPublicIpOnLaunch=true) + IGW attached, so a
# task with assignPublicIp=ENABLED egresses with no NAT gateway.
SUBNETS=(subnet-022dbb3c8796f2572 subnet-0dc4031fe78770b45 subnet-015d9d736b31aa1db)
RDS_SG=sg-0556581aca12ee614
BUCKET="tradingagents-${ACCT}-results"
IMAGE_TAG="${IMAGE_TAG:-latest}"          # pin to a :<gitsha> for the real schedule
IMAGE="${ACCT}.dkr.ecr.${REGION}.amazonaws.com/tradingagents-ddog:${IMAGE_TAG}"

INSTANCE_ROLE=tradingagents-eks-builder
KMS_ALIAS=alias/tradingagents
SECRET_NAME=tradingagents/runtime
CLUSTER=tradingagents
LOG_GROUP=/ecs/tradingagents
TASK_FAMILY=tradingagents-run
EXEC_ROLE=tradingagents-ecs-exec
TASK_ROLE=tradingagents-task
SCHED_ROLE=tradingagents-scheduler
FARGATE_SG_NAME=tradingagents-fargate-sg
SCHEDULE_NAME=tradingagents-mwf

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKDEF_TMPL="$REPO_DIR/infra/ecs-taskdef.json"
SCRATCH_DIR="$(mktemp -d)"
trap 'rm -rf "$SCRATCH_DIR"' EXIT
chmod 700 "$SCRATCH_DIR"

exec_role_arn="arn:aws:iam::${ACCT}:role/${EXEC_ROLE}"
task_role_arn="arn:aws:iam::${ACCT}:role/${TASK_ROLE}"
sched_role_arn="arn:aws:iam::${ACCT}:role/${SCHED_ROLE}"

# ===========================================================================
create_schedule() {
  # Look up the Fargate SG (must already exist from the main run).
  local sg
  sg=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$FARGATE_SG_NAME" "Name=vpc-id,Values=$VPC" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
  local subnets_csv; subnets_csv=$(IFS=,; echo "${SUBNETS[*]}")
  local taskdef_arn
  taskdef_arn=$(aws ecs describe-task-definition --task-definition "$TASK_FAMILY" \
    --query 'taskDefinition.taskDefinitionArn' --output text)
  cat > "$SCRATCH_DIR/target.json" <<JSON
{
  "Arn": "arn:aws:ecs:${REGION}:${ACCT}:cluster/${CLUSTER}",
  "RoleArn": "${sched_role_arn}",
  "EcsParameters": {
    "TaskDefinitionArn": "${taskdef_arn}",
    "LaunchType": "FARGATE",
    "NetworkConfiguration": {
      "awsvpcConfiguration": {
        "Subnets": ["${SUBNETS[0]}","${SUBNETS[1]}","${SUBNETS[2]}"],
        "SecurityGroups": ["${sg}"],
        "AssignPublicIp": "ENABLED"
      }
    }
  }
}
JSON
  echo ">> EventBridge schedule $SCHEDULE_NAME (cron 0 8 MON,WED,FRI UTC), state DISABLED"
  aws scheduler create-schedule --name "$SCHEDULE_NAME" \
    --schedule-expression "cron(0 8 ? * MON,WED,FRI *)" \
    --schedule-expression-timezone "UTC" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --state DISABLED \
    --target "file://$SCRATCH_DIR/target.json" 2>&1 | grep -iE "ScheduleArn|already exists" \
  || aws scheduler update-schedule --name "$SCHEDULE_NAME" \
    --schedule-expression "cron(0 8 ? * MON,WED,FRI *)" \
    --schedule-expression-timezone "UTC" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --state DISABLED \
    --target "file://$SCRATCH_DIR/target.json" 2>&1 | grep -iE "ScheduleArn|error" || true
  echo ">> schedule created DISABLED. Enable after a green manual run with:"
  echo "   aws scheduler update-schedule --name $SCHEDULE_NAME --state ENABLED \\"
  echo "     --schedule-expression 'cron(0 8 ? * MON,WED,FRI *)' --schedule-expression-timezone UTC \\"
  echo "     --flexible-time-window '{\"Mode\":\"OFF\"}' --target file://<regenerated target.json>"
}

if [ "${1:-}" = "schedule" ]; then
  create_schedule
  exit 0
fi

# ===========================================================================
# STEP 0 — self-grant the provisioning perms the instance role lacks.
# ===========================================================================
echo ">> [0] grant $INSTANCE_ROLE the ecs/kms/secrets/scheduler/logs provisioning perms"
cat > "$SCRATCH_DIR/prov-policy.json" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "EcsProvision", "Effect": "Allow",
      "Action": ["ecs:*","logs:*"], "Resource": "*" },
    { "Sid": "SecretsProvision", "Effect": "Allow",
      "Action": ["secretsmanager:*"], "Resource": "*" },
    { "Sid": "KmsProvision", "Effect": "Allow",
      "Action": ["kms:*"], "Resource": "*" },
    { "Sid": "SchedulerProvision", "Effect": "Allow",
      "Action": ["scheduler:*"], "Resource": "*" },
    { "Sid": "PassTaskRoles", "Effect": "Allow",
      "Action": ["iam:PassRole"], "Resource": "*" }
  ]
}
JSON
aws iam put-role-policy --role-name "$INSTANCE_ROLE" \
  --policy-name fargate-provision \
  --policy-document "file://$SCRATCH_DIR/prov-policy.json" \
  && echo "   attached fargate-provision inline policy"
echo "   waiting 15s for IAM propagation..."; sleep 15

# ===========================================================================
# STEP 1 — KMS customer-managed key + alias.
# ===========================================================================
echo ">> [1] KMS CMK $KMS_ALIAS"
KEY_ARN=$(aws kms describe-key --key-id "$KMS_ALIAS" \
  --query 'KeyMetadata.Arn' --output text 2>/dev/null)
if [ -z "$KEY_ARN" ] || [ "$KEY_ARN" = "None" ]; then
  KEY_ARN=$(aws kms create-key \
    --description "tradingagents runtime secret encryption" \
    --tags TagKey=app,TagValue=tradingagents \
    --query 'KeyMetadata.Arn' --output text 2>&1)
  if [[ "$KEY_ARN" != arn:aws:kms:* ]]; then
    echo "!! KMS create-key FAILED: $KEY_ARN"
    echo "!! Fall back to SSM Parameter Store String (see plan). Aborting."
    exit 1
  fi
  KEY_ID="${KEY_ARN##*/}"
  aws kms create-alias --alias-name "$KMS_ALIAS" --target-key-id "$KEY_ID" 2>&1 | tail -1 || true
fi
echo "   KEY_ARN=$KEY_ARN"

# ===========================================================================
# STEP 2 — Secrets Manager secret (built from the box's env files; CMK-encrypted).
# ===========================================================================
echo ">> [2] build runtime secret JSON from env files (values never printed)"
python3 - "$SCRATCH_DIR/secret.json" <<'PY'
import json, sys
SOURCES = [
    "/home/ubuntu/TradingAgents/.env",
    "/home/ubuntu/.tradingagents.env",
    "/home/ubuntu/.datadog-mcp.env",
]
REQUIRED = ["OPENROUTER_API_KEY", "TWITTERAPI_IO_KEY", "TRADINGAGENTS_DATABASE_URL", "DD_API_KEY"]
OPTIONAL = ["FRED_API_KEY", "ALPHA_VANTAGE_API_KEY"]
want = REQUIRED + OPTIONAL
vals = {}
for path in SOURCES:
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in want and v:
                    vals[k] = v
    except FileNotFoundError:
        pass
missing = [k for k in REQUIRED if k not in vals]
if missing:
    sys.stderr.write("!! MISSING required secret keys: %s\n" % missing)
    sys.exit(1)
# Optional keys referenced by the task def must exist too (task fails otherwise);
# insert empty string if the source didn't have them.
for k in OPTIONAL:
    vals.setdefault(k, "")
with open(sys.argv[1], "w") as out:
    json.dump(vals, out)
print("   secret JSON built with keys:", sorted(vals))  # NAMES only
PY
if [ $? -ne 0 ]; then echo "!! secret build failed"; exit 1; fi
chmod 600 "$SCRATCH_DIR/secret.json"

SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" \
  --query 'ARN' --output text 2>/dev/null)
if [ -z "$SECRET_ARN" ] || [ "$SECRET_ARN" = "None" ]; then
  SECRET_ARN=$(aws secretsmanager create-secret --name "$SECRET_NAME" \
    --description "tradingagents Fargate runtime secrets" \
    --kms-key-id "$KEY_ARN" \
    --secret-string "file://$SCRATCH_DIR/secret.json" \
    --query 'ARN' --output text)
else
  aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
    --secret-string "file://$SCRATCH_DIR/secret.json" --query 'ARN' --output text >/dev/null
  # ensure it is CMK-encrypted (in case it pre-existed with default key)
  aws secretsmanager update-secret --secret-id "$SECRET_NAME" --kms-key-id "$KEY_ARN" \
    --query 'ARN' --output text >/dev/null 2>&1 || true
fi
shred -u "$SCRATCH_DIR/secret.json" 2>/dev/null || rm -f "$SCRATCH_DIR/secret.json"
echo "   SECRET_ARN=$SECRET_ARN"

# ===========================================================================
# STEP 3 — IAM roles (execution / task / scheduler).
# ===========================================================================
echo ">> [3] IAM roles"
ecs_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
sched_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

# 3a. execution role
aws iam create-role --role-name "$EXEC_ROLE" --assume-role-policy-document "$ecs_trust" \
  2>&1 | grep -iE "Arn|already exists" || true
aws iam attach-role-policy --role-name "$EXEC_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy 2>&1 | tail -1 || true
cat > "$SCRATCH_DIR/exec-inline.json" <<JSON
{ "Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":"${SECRET_ARN}"},
  {"Effect":"Allow","Action":["kms:Decrypt"],"Resource":"${KEY_ARN}"}
]}
JSON
aws iam put-role-policy --role-name "$EXEC_ROLE" --policy-name secrets-decrypt \
  --policy-document "file://$SCRATCH_DIR/exec-inline.json" && echo "   exec role policy set"

# 3b. task role (app runtime: S3 read/write for reports + watchlist)
aws iam create-role --role-name "$TASK_ROLE" --assume-role-policy-document "$ecs_trust" \
  2>&1 | grep -iE "Arn|already exists" || true
cat > "$SCRATCH_DIR/task-inline.json" <<JSON
{ "Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],"Resource":"arn:aws:s3:::${BUCKET}/*"},
  {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::${BUCKET}"}
]}
JSON
aws iam put-role-policy --role-name "$TASK_ROLE" --policy-name s3-reports \
  --policy-document "file://$SCRATCH_DIR/task-inline.json" && echo "   task role policy set"

# 3c. scheduler role (EventBridge Scheduler -> ecs:RunTask + PassRole)
aws iam create-role --role-name "$SCHED_ROLE" --assume-role-policy-document "$sched_trust" \
  2>&1 | grep -iE "Arn|already exists" || true
cat > "$SCRATCH_DIR/sched-inline.json" <<JSON
{ "Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["ecs:RunTask"],"Resource":"arn:aws:ecs:${REGION}:${ACCT}:task-definition/${TASK_FAMILY}:*"},
  {"Effect":"Allow","Action":["iam:PassRole"],"Resource":["${exec_role_arn}","${task_role_arn}"],
   "Condition":{"StringLike":{"iam:PassedToService":"ecs-tasks.amazonaws.com"}}}
]}
JSON
aws iam put-role-policy --role-name "$SCHED_ROLE" --policy-name run-task \
  --policy-document "file://$SCRATCH_DIR/sched-inline.json" && echo "   scheduler role policy set"

# ===========================================================================
# STEP 4 — Fargate security group + RDS ingress.
# ===========================================================================
echo ">> [4] Fargate SG $FARGATE_SG_NAME + RDS 5432 ingress"
FARGATE_SG=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=$FARGATE_SG_NAME" "Name=vpc-id,Values=$VPC" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ "$FARGATE_SG" = "None" ] || [ -z "$FARGATE_SG" ]; then
  FARGATE_SG=$(aws ec2 create-security-group --group-name "$FARGATE_SG_NAME" \
    --description "tradingagents fargate task egress" --vpc-id "$VPC" \
    --query 'GroupId' --output text)
fi
echo "   FARGATE_SG=$FARGATE_SG"
aws ec2 authorize-security-group-ingress --group-id "$RDS_SG" --protocol tcp --port 5432 \
  --source-group "$FARGATE_SG" 2>&1 | grep -iE "Return|Duplicate|already|InvalidPermission" || true

# ===========================================================================
# STEP 5 — CloudWatch Logs group.
# ===========================================================================
echo ">> [5] log group $LOG_GROUP"
aws logs create-log-group --log-group-name "$LOG_GROUP" 2>&1 | tail -1 || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30 2>&1 | tail -1 || true

# ===========================================================================
# STEP 6 — ECS cluster.
# ===========================================================================
echo ">> [6] ECS cluster $CLUSTER"
aws ecs create-cluster --cluster-name "$CLUSTER" \
  --query 'cluster.clusterArn' --output text 2>&1 | tail -1 || true

# ===========================================================================
# STEP 7 — register the task definition.
# ===========================================================================
echo ">> [7] register task definition $TASK_FAMILY (image $IMAGE)"
sed -e "s|__EXEC_ROLE_ARN__|${exec_role_arn}|g" \
    -e "s|__TASK_ROLE_ARN__|${task_role_arn}|g" \
    -e "s|__IMAGE__|${IMAGE}|g" \
    -e "s|__BUCKET__|${BUCKET}|g" \
    -e "s|__REGION__|${REGION}|g" \
    -e "s|__LOG_GROUP__|${LOG_GROUP}|g" \
    -e "s|__SECRET_ARN__|${SECRET_ARN}|g" \
    "$TASKDEF_TMPL" > "$SCRATCH_DIR/taskdef.json"
TASKDEF_ARN=$(aws ecs register-task-definition \
  --cli-input-json "file://$SCRATCH_DIR/taskdef.json" \
  --query 'taskDefinition.taskDefinitionArn' --output text)
echo "   TASKDEF_ARN=$TASKDEF_ARN"

echo ""
echo ">> DONE. Coordinates for a manual verification run:"
echo "   CLUSTER=$CLUSTER"
echo "   TASKDEF=$TASKDEF_ARN"
echo "   SUBNET=${SUBNETS[0]}  FARGATE_SG=$FARGATE_SG  (assignPublicIp=ENABLED)"
echo ""
echo ">> Manual run (PAID LLM calls):"
echo "   aws ecs run-task --cluster $CLUSTER --launch-type FARGATE \\"
echo "     --task-definition $TASK_FAMILY \\"
echo "     --network-configuration 'awsvpcConfiguration={subnets=[${SUBNETS[0]}],securityGroups=[$FARGATE_SG],assignPublicIp=ENABLED}'"
echo ""
echo ">> Then create the (disabled) schedule:  ./infra/provision_fargate.sh schedule"
