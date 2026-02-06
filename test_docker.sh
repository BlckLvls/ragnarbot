#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="ragnarbot-test"

echo "=== Building Docker image ==="
docker build -t "$IMAGE_NAME" .

echo ""
echo "=== Running 'ragnarbot onboard' ==="
docker run --name ragnarbot-test-run "$IMAGE_NAME" onboard

echo ""
echo "=== Running 'ragnarbot status' ==="
STATUS_OUTPUT=$(docker commit ragnarbot-test-run ragnarbot-test-onboarded > /dev/null && \
    docker run --rm ragnarbot-test-onboarded status 2>&1) || true

echo "$STATUS_OUTPUT"

echo ""
echo "=== Validating output ==="
PASS=true

check() {
    if echo "$STATUS_OUTPUT" | grep -q "$1"; then
        echo "  PASS: found '$1'"
    else
        echo "  FAIL: missing '$1'"
        PASS=false
    fi
}

check "ragnarbot Status"
check "Config:"
check "Workspace:"
check "Model:"
check "OpenRouter API:"
check "Anthropic API:"
check "OpenAI API:"

echo ""
if $PASS; then
    echo "=== All checks passed ==="
else
    echo "=== Some checks FAILED ==="
    exit 1
fi

# Cleanup
echo ""
echo "=== Cleanup ==="
docker rm -f ragnarbot-test-run 2>/dev/null || true
docker rmi -f ragnarbot-test-onboarded 2>/dev/null || true
docker rmi -f "$IMAGE_NAME" 2>/dev/null || true
echo "Done."
