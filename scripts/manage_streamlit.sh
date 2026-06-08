#!/usr/bin/env bash
set -euo pipefail

# Manage Streamlit for this project:
# - kills any running streamlit serving `app/chat_ai.py` (or any process on port 8501)
# - restarts it (prefers project's .venv if present)
# - writes logs to `logs/streamlit.log`
# - opens the default browser to http://localhost:8501

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_PATH="$ROOT_DIR/app/chat_ai.py"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/streamlit.log"

echo "Managing Streamlit for app: $APP_PATH"

# Find processes that match the exact streamlit run command
PIDS="$(pgrep -f "streamlit run $APP_PATH" || true)"
if [ -n "$PIDS" ]; then
  echo "Found running Streamlit PIDs: $PIDS. Killing..."
  kill $PIDS || true
  sleep 1
fi

# Also check any process listening on port 8501
PORT_PIDS="$(lsof -ti:8501 2>/dev/null || true)"
if [ -n "$PORT_PIDS" ]; then
  echo "Killing processes on port 8501: $PORT_PIDS"
  kill $PORT_PIDS || true
  sleep 1
fi

# Activate a virtualenv if present
if [ -f "$ROOT_DIR/.venv/bin/activate" ]; then
  echo "Activating virtualenv at $ROOT_DIR/.venv"
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.venv/bin/activate"
elif [ -f "$ROOT_DIR/.venv_streamlit/bin/activate" ]; then
  echo "Activating virtualenv at $ROOT_DIR/.venv_streamlit"
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.venv_streamlit/bin/activate"
else
  echo "No project virtualenv found; using system Python/streamlit"
fi

echo "Starting Streamlit (logs -> $LOG_FILE)"
nohup streamlit run "$APP_PATH" --server.port 8501 > "$LOG_FILE" 2>&1 &
sleep 2
NEWPID=$!
echo "Streamlit started with PID $NEWPID"

URL="http://localhost:8501"
if command -v xdg-open >/dev/null; then
  xdg-open "$URL" || true
elif command -v python3 >/dev/null; then
  python3 -m webbrowser "$URL" || true
fi

echo "Streamlit manage script finished. Tail logs with: tail -f $LOG_FILE"
