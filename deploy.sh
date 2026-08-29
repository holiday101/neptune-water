#!/bin/bash
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────
SSH_HOST="ubuntu@54.226.186.201"       # membergolfonline.com
SSH_KEY="$HOME/.ssh/my-ec2-key.pem"
REMOTE_DIR="/home/ubuntu/neptune-water"
SERVICE="neptune-water.service"
HEALTH_URL="https://membergolfonline.com/water/"

# ── Deploy ─────────────────────────────────────────────────────
echo "🚀 Deploying to $SSH_HOST..."

ssh -i "$SSH_KEY" "$SSH_HOST" bash -s << ENDSSH
set -euo pipefail
cd "$REMOTE_DIR"

echo "── Pulling latest code..."
git pull --ff-only origin main

echo "── Installing/updating dependencies..."
.venv/bin/pip install -q -r requirements.txt

echo "── Restarting service..."
sudo -n systemctl restart "$SERVICE"
sleep 2
sudo -n systemctl is-active "$SERVICE"

echo "✅ Deploy complete!"
ENDSSH

echo "── Health check..."
sleep 2
CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 "$HEALTH_URL")
if [ "$CODE" = "200" ]; then
  echo "✅ $HEALTH_URL responded HTTP $CODE"
else
  echo "⚠️  $HEALTH_URL responded HTTP $CODE — check server logs"
  exit 1
fi
