#!/usr/bin/env bash
# bot.sh — control script for polymarket-crypto-agents
# Commands: start | stop | restart | status | logs
#
# The "start" command launches a supervisor process that keeps the bot
# running: if the python process exits for any reason, the supervisor
# relaunches it after a short backoff. Both the supervisor and the child
# bot PIDs are tracked via pidfiles so "stop" cleanly kills both.

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PY="$DIR/venv/bin/python"
LOG="$DIR/bot_output.log"
SUP_LOG="$DIR/supervisor.log"
SUP_PID="$DIR/.bot.supervisor.pid"
CHILD_PID="$DIR/.bot.child.pid"

# Mode can be overridden: BOT_MODE="--dry-run" ./bot.sh start
MODE="${BOT_MODE:---paper-trade}"
RESTART_DELAY_S="${RESTART_DELAY_S:-5}"

is_running() {
  local pidfile="$1"
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null
}

cmd_start() {
  if is_running "$SUP_PID"; then
    echo "Already running (supervisor PID $(cat "$SUP_PID"))"
    exit 0
  fi

  if [ ! -x "$PY" ]; then
    echo "ERROR: venv python not found at $PY" >&2
    exit 1
  fi

  echo "Starting supervisor (mode: $MODE)..."

  # Supervisor loop: re-launch bot whenever it exits.
  # Using setsid so the supervisor survives the parent shell.
  setsid bash -c '
    trap "kill $CHILD_PID_VAL 2>/dev/null; exit 0" TERM INT
    while true; do
      echo "[supervisor $(date -Iseconds)] launching bot" >> "'"$SUP_LOG"'"
      "'"$PY"'" main.py '"$MODE"' >> "'"$LOG"'" 2>&1 &
      CHILD_PID_VAL=$!
      echo $CHILD_PID_VAL > "'"$CHILD_PID"'"
      wait $CHILD_PID_VAL
      EXIT=$?
      echo "[supervisor $(date -Iseconds)] bot exited code=$EXIT; restarting in '"$RESTART_DELAY_S"'s" >> "'"$SUP_LOG"'"
      rm -f "'"$CHILD_PID"'"
      sleep '"$RESTART_DELAY_S"'
    done
  ' </dev/null >>"$SUP_LOG" 2>&1 &

  echo $! > "$SUP_PID"
  sleep 1
  echo "Supervisor PID: $(cat "$SUP_PID")"
  echo "Bot log:        $LOG"
  echo "Supervisor log: $SUP_LOG"
  cmd_status
}

cmd_stop() {
  local stopped=0
  if is_running "$SUP_PID"; then
    kill "$(cat "$SUP_PID")" 2>/dev/null || true
    stopped=1
  fi
  rm -f "$SUP_PID"

  if is_running "$CHILD_PID"; then
    kill "$(cat "$CHILD_PID")" 2>/dev/null || true
    stopped=1
  fi
  rm -f "$CHILD_PID"

  # Safety net: kill any stray main.py processes from this project dir
  pkill -f "$DIR/main.py" 2>/dev/null || true

  if [ "$stopped" -eq 1 ]; then
    echo "Stopped."
  else
    echo "Nothing was running."
  fi
}

cmd_status() {
  if is_running "$SUP_PID"; then
    echo "supervisor : RUNNING (PID $(cat "$SUP_PID"))"
  else
    echo "supervisor : STOPPED"
  fi
  if is_running "$CHILD_PID"; then
    echo "bot process: RUNNING (PID $(cat "$CHILD_PID"))"
  else
    echo "bot process: NOT RUNNING"
  fi
}

cmd_restart() {
  cmd_stop
  sleep 2
  cmd_start
}

cmd_logs() {
  tail -n 50 -f "$LOG"
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}"
    echo ""
    echo "Environment overrides:"
    echo "  BOT_MODE=\"--dry-run\"           # default: --paper-trade"
    echo "  RESTART_DELAY_S=10             # default: 5"
    exit 1
    ;;
esac
