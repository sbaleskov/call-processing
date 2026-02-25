#!/usr/bin/env bash
# Deploy transcription worker to a remote server.
#
# Usage:
#   ./remote/deploy.sh user@host          # full install
#   ./remote/deploy.sh user@host --update  # redeploy worker.py only
#
# What it does:
#   1. Installs system deps (python3, ffmpeg)
#   2. Creates venv with faster-whisper + diarization deps
#   3. Copies worker.py to the server
#   4. Runs a quick health check
#
# After deploy, set in your .env:
#   TRANSCRIBE_REMOTE=true
#   REMOTE_HOST=<your-server-ip>

set -euo pipefail

TARGET="${1:?Usage: ./remote/deploy.sh user@host [--update]}"
UPDATE_ONLY="${2:-}"
INSTALL_DIR=/opt/call-processing
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$UPDATE_ONLY" != "--update" ]; then
    echo ">>> Installing system dependencies on $TARGET..."
    ssh "$TARGET" "apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-dev ffmpeg"

    echo ">>> Creating Python environment..."
    ssh "$TARGET" "mkdir -p $INSTALL_DIR && python3 -m venv $INSTALL_DIR/venv"

    echo ">>> Installing Python packages..."
    ssh "$TARGET" "$INSTALL_DIR/venv/bin/pip install -q --upgrade pip && \
        $INSTALL_DIR/venv/bin/pip install -q faster-whisper numpy soundfile python-speech-features scikit-learn"
fi

echo ">>> Deploying worker.py..."
scp "$SCRIPT_DIR/worker.py" "$TARGET:$INSTALL_DIR/worker.py"

echo ">>> Verifying installation..."
ssh "$TARGET" "$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/worker.py --check"

echo ""
echo "=== Deployed to $TARGET:$INSTALL_DIR ==="
echo ""
echo "Add to your .env:"
echo "  TRANSCRIBE_REMOTE=true"
echo "  REMOTE_HOST=$(echo "$TARGET" | cut -d@ -f2)"
echo "  REMOTE_USER=$(echo "$TARGET" | cut -d@ -f1)"
