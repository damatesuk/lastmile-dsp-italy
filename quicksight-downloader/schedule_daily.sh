#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Lighthouse daily update — programación en macOS (launchd)
# ═══════════════════════════════════════════════════════════════
# Instala un LaunchAgent que ejecuta daily_update.sh todos los días a las
# 07:45 (Amazon refresca Lighthouse a las 07:00, datos T-2D). Si el Mac está
# dormido a esa hora, launchd lanza el job en cuanto se despierta.
#
#   ./schedule_daily.sh            # instala / actualiza el job
#   ./schedule_daily.sh --run-now  # lo lanza inmediatamente (prueba)
#   ./schedule_daily.sh --status   # ¿está cargado? última salida
#   ./schedule_daily.sh --remove   # lo desinstala
#
# Variables opcionales: HOUR=7 MINUTE=45 ARGS="--weeks 12" ./schedule_daily.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.otl.lighthouse-daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$SCRIPT_DIR/logs"; mkdir -p "$LOG_DIR"
HOUR=${HOUR:-7}; MINUTE=${MINUTE:-45}
ARGS=${ARGS:-}

case "${1:-}" in
  --remove)
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"; echo "Job $LABEL eliminado"; exit 0 ;;
  --run-now)
    launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "Lanzado. Log: $LOG_DIR/daily_$(date +%Y-%m-%d).log"; exit 0 ;;
  --status)
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|last exit|program" || echo "No instalado"
    ls -t "$LOG_DIR"/daily_*.log 2>/dev/null | head -1 | xargs -I{} sh -c 'echo "--- {}"; tail -5 "{}"'; exit 0 ;;
esac

[ -x "$SCRIPT_DIR/daily_update.sh" ] || chmod +x "$SCRIPT_DIR/daily_update.sh"
[ -x "$SCRIPT_DIR/venv/bin/python" ] || echo "AVISO: no hay venv en $SCRIPT_DIR/venv — daily_update.sh usará python3 del sistema (necesita playwright)."

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>$SCRIPT_DIR/daily_update.sh</string>$(for a in $ARGS; do printf '\n    <string>%s</string>' "$a"; done)
  </array>
  <key>WorkingDirectory</key><string>$SCRIPT_DIR</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$LOG_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchd.err.log</string>
  <key>RunAtLoad</key><false/>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Instalado $LABEL → todos los días a las $(printf '%02d:%02d' "$HOUR" "$MINUTE")"
echo "Script:  $SCRIPT_DIR/daily_update.sh $ARGS"
echo "Logs:    $LOG_DIR/daily_<fecha>.log"
echo "Prueba:  ./schedule_daily.sh --run-now"
