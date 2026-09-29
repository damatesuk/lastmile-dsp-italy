# Last Mile DSP — Italy 🇮🇹

Delivery Performance Dashboard for DSP Italy operations.

## Features

- **Scorecard** (landing tab) — goal vs actual radar, RCA donut per metric from Amazon's order-level attribution, WK-vs-monthly-goal tiles (FTDC, FTFDF, Lates +15, FTPDF, DNR) and secondary metrics (RFC, stops per route, orders, routes, late pickup). Monthly goals live in `SC_GOALS` in `src/Dashboard.jsx`.
- **Network Overview** — KPI trends across all depots (Late +15, FTFDF, PDNR, FDNR)
- **Depot Deep-Dive** — Individual station performance with week-over-week comparisons
- **Upstream vs Controllable** — Attribution analysis of late deliveries
- **NCC Drivers** — Non-compliance driver tracking with severity classification
- **Late Drivers** — Late delivery driver analysis across stations
- **Driver Scorecard** — Combined performance view per driver

## Stations

`LSRL` · `UIT4` · `UIT1` · `UBA1` · `UIL7`

## Data Range

W47/2025 → W40/2026, refreshed daily from Amazon Lighthouse (W9–W26 not loaded — Lighthouse keeps ~12 weeks; UIT7 closed after W5/2026)

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

The downloader (`lighthouse_downloader.py`) does *Reset to original* on the dashboard first, then sets every control explicitly (date range, Time Granularity = Weekly, Show By, Show Attribution, Defect = All), so a filter someone left on in QuickSight (e.g. `station_code = UIT4`) never leaks into the export. `--steps setup,network,orders` runs the export in phases (useful where a single run must finish in < 3 min); if it fails it writes `logs/debug_<step>.png/.txt` with a DOM inventory.

Manual re-merge / redeploy without downloading:
```
./daily_update.sh --no-download --force-deploy
```
