#!/usr/bin/env bash
# Provision the durable data layer for TradingAgents:
#   - RDS PostgreSQL (db.t4g.micro) — the decisions ledger
#   - private S3 bucket            — report markdown
# One-time and re-runnable (tolerates already-exists). Requires the
# tradingagents-eks-builder instance role with RDS/S3 permissions.
#
# Notes on this account (AWS Free Plan):
#   - Aurora Serverless v2 is gated (FreeTierRestrictionError) — hence standard RDS.
#   - The default KMS keys are not accessible, so we skip --storage-encrypted and use
#     a MANUAL master password (not --manage-master-user-password, which needs KMS).
#     Hardening follow-ups: enable storage encryption + move the password to Secrets
#     Manager (or IAM DB auth) once KMS access is sorted / the plan is upgraded.
#
# The master password is read from $PGPW_FILE (default below) so it is never echoed;
# generate one first, e.g.:  openssl rand -hex 20 > "$PGPW_FILE"; chmod 600 "$PGPW_FILE"
set -uo pipefail
export AWS_DEFAULT_REGION=us-east-2

ACCT=963910217112
VPC=vpc-0929617e55f0cced4
EC2_SG=sg-02c2ef42a8b6b3707
SUBNETS="subnet-022dbb3c8796f2572 subnet-0dc4031fe78770b45"   # us-east-2c + us-east-2b
BUCKET="tradingagents-${ACCT}-results"
INSTANCE=tradingagents-pg
SUBNET_GROUP=tradingagents-db-subnets
DB_SG_NAME=tradingagents-db-sg
DB_NAME=tradingagents
MASTER_USER=tauser
ENGINE_VERSION=16.9
PGPW_FILE="${PGPW_FILE:-$HOME/.tradingagents/.pgpw}"

echo ">> S3 bucket: $BUCKET"
aws s3api create-bucket --bucket "$BUCKET" \
  --create-bucket-configuration LocationConstraint="$AWS_DEFAULT_REGION" 2>&1 | tail -1 || true
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true 2>&1 | tail -1 || true

echo ">> DB subnet group: $SUBNET_GROUP"
aws rds create-db-subnet-group --db-subnet-group-name "$SUBNET_GROUP" \
  --db-subnet-group-description "tradingagents postgres" --subnet-ids $SUBNETS \
  2>&1 | grep -iE "DBSubnetGroupArn|already exists" || true

echo ">> DB security group + ingress 5432 from the EC2 SG"
DB_SG=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=$DB_SG_NAME" "Name=vpc-id,Values=$VPC" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ "$DB_SG" = "None" ] || [ -z "$DB_SG" ]; then
  DB_SG=$(aws ec2 create-security-group --group-name "$DB_SG_NAME" \
    --description "tradingagents postgres 5432 from ec2" --vpc-id "$VPC" --query 'GroupId' --output text)
fi
echo "   DB_SG=$DB_SG"
aws ec2 authorize-security-group-ingress --group-id "$DB_SG" --protocol tcp --port 5432 \
  --source-group "$EC2_SG" 2>&1 | grep -iE "Return|Duplicate|already" || true

if [ ! -f "$PGPW_FILE" ]; then
  echo ">> generating master password -> $PGPW_FILE (not printed)"
  openssl rand -hex 20 > "$PGPW_FILE"; chmod 600 "$PGPW_FILE"
fi

echo ">> RDS PostgreSQL instance: $INSTANCE (private, manual password, unencrypted)"
aws rds create-db-instance \
  --db-instance-identifier "$INSTANCE" \
  --engine postgres --engine-version "$ENGINE_VERSION" \
  --db-instance-class db.t4g.micro \
  --master-username "$MASTER_USER" --master-user-password "$(cat "$PGPW_FILE")" \
  --allocated-storage 20 --storage-type gp3 \
  --db-name "$DB_NAME" \
  --db-subnet-group-name "$SUBNET_GROUP" --vpc-security-group-ids "$DB_SG" \
  --no-publicly-accessible --backup-retention-period 1 --no-multi-az \
  2>&1 | grep -iE "DBInstanceArn|already exists" || true

echo ">> waiting for the instance to become available (~5-10 min)..."
aws rds wait db-instance-available --db-instance-identifier "$INSTANCE"

ENDPOINT=$(aws rds describe-db-instances --db-instance-identifier "$INSTANCE" \
  --query 'DBInstances[0].Endpoint.Address' --output text)
echo ">> DONE"
echo ">> ENDPOINT=$ENDPOINT  DB_NAME=$DB_NAME  MASTER_USER=$MASTER_USER  BUCKET=$BUCKET"
echo ">> Build: TRADINGAGENTS_DATABASE_URL=postgresql://$MASTER_USER:<pw-from-$PGPW_FILE>@$ENDPOINT:5432/$DB_NAME"
