#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# daily_update.sh — actualización diaria del dashboard LSRL Italy
# ═══════════════════════════════════════════════════════════════
#   1. descarga los 4 CSV de QuickSight (AGL DSP Lighthouse)   → lighthouse_downloader.py
#   2. fusiona los datos en src/Dashboard.jsx                   → scripts/lighthouse_to_dashboard.py
#   3. git commit + push (sólo si hay cambios)
#   4. npm run build + netlify deploy --prod (sólo si hay cambios, o --force-deploy)
#   5. notificación macOS con el resultado; log en logs/daily_<fecha>.log
#
# Uso:
#   ./daily_update.sh                      # ciclo completo (headless)
#   ./daily_update.sh --no-download        # sólo merge + build + deploy con la última carpeta data/lighthouse/*
#   ./daily_update.sh --no-deploy          # sin build ni deploy
#   ./daily_update.sh --no-push            # sin git push
#   ./daily_update.sh --force-deploy       # despliega aunque no haya cambios
#   ./daily_update.sh --weeks 6            # semanas hacia atrás a descargar (por defecto 12)
#   ./daily_update.sh --headed             # navegador visible (para depurar el login)
#
# Credenciales en .env (misma carpeta): QS_USERNAME, QS_PASSWORD y, opcionalmente,
# NETLIFY_AUTH_TOKEN (si no, se usa el login de `netlify` guardado en ~/.netlify).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily_$(date +%Y-%m-%d).log"
exec > >(tee -a "$LOG") 2>&1
echo "══════ $(date '+%d/%m/%Y %H:%M:%S') — inicio ══════"

# PATH completo (launchd no carga el perfil de la shell)
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.npm-global/bin:$HOME/.nvm/versions/node/current/bin:$PATH"
if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1 || true; fi

# candado para no solaparse
LOCK="$SCRIPT_DIR/.daily.lock"
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then echo "Ya hay una ejecución en curso (pid $(cat "$LOCK")); salgo."; exit 0; fi
echo $$ > "$LOCK"; trap 'rm -f "$LOCK"' EXIT

notify() { osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true; }
fail()   { echo "[ERROR] $1"; notify "Lighthouse daily update — FALLÓ" "$1 (ver $LOG)"; exit 1; }

# credenciales
set -a; [ -f "$SCRIPT_DIR/.env" ] && . "$SCRIPT_DIR/.env"; set +a
SITE_ID="${NETLIFY_SITE_ID:-0b31a9cd-e402-4d08-b951-000c108237ad}"

WEEKS=12; DOWNLOAD=1; DEPLOY=1; PUSH=1; FORCE=0; HEADLESS="--headless"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-download) DOWNLOAD=0 ;;
    --no-deploy)   DEPLOY=0 ;;
    --no-push)     PUSH=0 ;;
    --force-deploy) FORCE=1 ;;
    --headed)      HEADLESS="" ;;
    --weeks)       WEEKS="$2"; shift ;;
    *) echo "Opción desconocida: $1"; exit 2 ;;
  esac; shift
done

cd "$REPO" || fail "no encuentro el repo en $REPO"
command -v git >/dev/null || fail "git no está en el PATH"
command -v npm >/dev/null || fail "npm no está en el PATH (instala Node o ajusta PATH en daily_update.sh)"

# ── 1. descarga ────────────────────────────────────────────────
if [ "$DOWNLOAD" = 1 ]; then
  [ -n "${QS_USERNAME:-}" ] && [ -n "${QS_PASSWORD:-}" ] || fail "faltan QS_USERNAME / QS_PASSWORD en $SCRIPT_DIR/.env"
  PY="$SCRIPT_DIR/venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
  echo "── Descarga (últimas $WEEKS semanas) con $PY"
  "$PY" "$SCRIPT_DIR/lighthouse_downloader.py" $HEADLESS --weeks "$WEEKS" --out "$REPO/data/lighthouse" || fail "descarga de QuickSight"
fi
LATEST="$(ls -d "$REPO"/data/lighthouse/*/ 2>/dev/null | sort | tail -1)"
[ -n "$LATEST" ] || fail "no hay carpetas en data/lighthouse"
ls "$LATEST"/order_level_defects_*.csv >/dev/null 2>&1 || fail "la carpeta $LATEST no tiene order_level_defects_*.csv"
echo "── Datos: $LATEST"

# ── 2. fusión en Dashboard.jsx ─────────────────────────────────
python3 scripts/lighthouse_to_dashboard.py "$LATEST" --jsx src/Dashboard.jsx || fail "fusión en Dashboard.jsx"

# ── 3. git ─────────────────────────────────────────────────────
rm -f .git/HEAD.lock .git/index.lock .git/objects/maintenance.lock 2>/dev/null; find .git/objects -name 'tmp_obj_*' -delete 2>/dev/null
git add src/Dashboard.jsx >/dev/null 2>&1
CHANGED=0
if ! git diff --cached --quiet; then
  GIT_ID=(); [ -n "$(git config user.email)" ] || GIT_ID=(-c user.name="Lighthouse daily update" -c user.email="ercilio@deliverymates.com")
  git "${GIT_ID[@]}" commit -qm "data: Lighthouse daily update $(date +%d/%m/%Y) ($(basename "$LATEST"))" && CHANGED=1 && echo "── Commit hecho"
else
  echo "── Sin cambios en Dashboard.jsx"
fi
if [ "$PUSH" = 1 ] && [ "$CHANGED" = 1 ]; then
  git push -q origin main && echo "── Push OK" || echo "[WARN] git push falló (¿credenciales?) — el deploy sigue"
fi

# ── 4. build + deploy ──────────────────────────────────────────
if [ "$DEPLOY" = 1 ] && { [ "$CHANGED" = 1 ] || [ "$FORCE" = 1 ]; }; then
  [ -d node_modules ] || { echo "── npm install"; npm install --no-audit --no-fund --silent || fail "npm install"; }
  echo "── Build"; npm run build --silent || fail "vite build"
  echo "── Deploy a Netlify ($SITE_ID)"
  npx -y netlify-cli deploy --prod --dir dist --site "$SITE_ID" --message "daily update $(date +%d/%m/%Y)" ${NETLIFY_AUTH_TOKEN:+--auth "$NETLIFY_AUTH_TOKEN"} || fail "netlify deploy (¿has hecho 'npx netlify-cli login'?)"
  echo "── Deploy OK"
fi

# ── 5. limpieza: carpetas de datos de más de 30 días ───────────
find "$REPO/data/lighthouse" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>/dev/null || true
find "$LOG_DIR" -name 'daily_*.log' -mtime +60 -delete 2>/dev/null || true

WEEKS_IN="$(grep -o '"2026-W[0-9]*"' "$REPO/src/Dashboard.jsx" | sort -t W -k2 -n | tail -1 | tr -d '"')"
MSG="Datos hasta $WEEKS_IN · $( [ "$CHANGED" = 1 ] && echo 'cambios desplegados' || echo 'sin cambios' )"
echo "══════ $(date '+%H:%M:%S') — fin: $MSG ══════"
notify "Lighthouse daily update — OK" "$MSG"
