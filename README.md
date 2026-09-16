# Last Mile DSP — Italy 🇮🇹

Delivery Performance Dashboard for DSP Italy operations.

## Features

- **Network Overview** — KPI trends across all depots (Late +15, FTFDF, PDNR, FDNR)
- **Depot Deep-Dive** — Individual station performance with week-over-week comparisons
- **Upstream vs Controllable** — Attribution analysis of late deliveries
- **NCC Drivers** — Non-compliance driver tracking with severity classification
- **Late Drivers** — Late delivery driver analysis across stations
- **Driver Scorecard** — Combined performance view per driver

## Stations

`LSRL` · `UIT4` · `UIT1` · `UBA1` · `UIL7`

## Data Range

W47/2025 → W37/2026 (W9–W26 not loaded; UIT7 closed after W5/2026)

## Tech

Single-page React app with Recharts, loaded via CDN. No build step required.

## Multi-Language

🇬🇧 English · 🇮🇹 Italiano · 🇪🇸 Español

## Daily update (automated)

`quicksight-downloader/daily_update.sh` runs the whole cycle: QuickSight export (Playwright) → `scripts/lighthouse_to_dashboard.py` merge into `src/Dashboard.jsx` → git commit + push → `npm run build` → `netlify deploy --prod`. The two most recent weeks are recalculated on every run (the current week arrives partial).

```
cd quicksight-downloader
./daily_update.sh --headed          # first run: watch the login/export in a visible browser
./schedule_daily.sh                 # install the launchd job (07:45 daily)
./schedule_daily.sh --status        # check it / see the last log
```
Credentials in `quicksight-downloader/.env` (`QS_USERNAME`, `QS_PASSWORD`, optional `NETLIFY_AUTH_TOKEN`). Logs in `quicksight-downloader/logs/`.
