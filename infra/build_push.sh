#!/usr/bin/env bash
# Build the Datadog-instrumented TradingAgents image and push it to ECR.
#
# Prereqs: docker, aws CLI, and AWS credentials (the tradingagents-eks-builder
# instance role covers this on the EC2 box). Run from the repo root on the `eks`
# branch.
#
#   ./infra/build_push.sh              # build + push :<gitsha> and :latest
#   AWS_REGION=us-east-2 ./infra/build_push.sh
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
REPO="${REGISTRY}/tradingagents-ddog"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

SHA="$(git rev-parse --short HEAD)"
GIT_URL="$(git config --get remote.origin.url || true)"

echo ">> building tradingagents-ddog (git ${SHA}) for ${REPO}"
# ddtrace runs under sudo because the box's docker needs sudo until the docker
# group membership takes effect on next login.
sudo docker build -f Dockerfile.ddog \
  --build-arg DD_GIT_COMMIT_SHA="$(git rev-parse HEAD)" \
  --build-arg DD_GIT_REPOSITORY_URL="${GIT_URL}" \
  -t "tradingagents-ddog:local" .

echo ">> logging in to ECR ${REGISTRY}"
aws ecr get-login-password --region "$AWS_REGION" \
  | sudo docker login --username AWS --password-stdin "$REGISTRY"

echo ">> tagging + pushing :${SHA} and :latest"
sudo docker tag tradingagents-ddog:local "${REPO}:${SHA}"
sudo docker tag tradingagents-ddog:local "${REPO}:latest"
sudo docker push "${REPO}:${SHA}"
sudo docker push "${REPO}:latest"

echo ">> done. pushed:"
echo "   ${REPO}:${SHA}"
echo "   ${REPO}:latest"
