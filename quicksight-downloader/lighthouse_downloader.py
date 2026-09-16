#!/usr/bin/env python3
"""
lighthouse_downloader.py
========================
Descarga diaria (Playwright) del dashboard QuickSight "AGL DSP Lighthouse" y,
opcionalmente, fusiona los datos en src/Dashboard.jsx.

Exporta 4 CSV en  downloads/lighthouse/<YYYY-MM-DD>/ :
  quality_metrics_by_station_<rango>.csv   pivot de métricas de calidad, Show By = 4. By Station Code (semanal)
  quality_metrics_network_<rango>.csv      misma pivot con Show By = 5. By DSP Company (red LSRL)
  business_metrics_by_station_<rango>.csv  pivot Business Metrics (Orders / Routes) por estación
  order_level_defects_<rango>.csv          tabla "Order Level Data" (una fila por defecto de pedido)

Uso:
  python3 lighthouse_downloader.py                 # últimas 12 semanas, con navegador visible
  python3 lighthouse_downloader.py --headless      # sin UI (cron / launchd)
  python3 lighthouse_downloader.py --weeks 6 --merge --commit
  python3 lighthouse_downloader.py --from 2026-06-28 --to 2026-09-13

Credenciales: variables de entorno QS_USERNAME / QS_PASSWORD (o fichero .env cargado por run.sh).
La sesión del navegador se guarda en .qs_state.json para no volver a hacer login cada día.

Requisitos: pip install playwright && playwright install chromium
"""
import argparse, datetime as dt, json, os, re, subprocess, sys, time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright no instalado:  pip install playwright && playwright install chromium")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent if (HERE.parent / "src" / "Dashboard.jsx").exists() else HERE
ACCOUNT = os.environ.get("QS_ACCOUNT", "agl-iris-qs-ext")
BASE = "https://us-east-1.quicksight.aws.amazon.com"
DASHBOARD_ID = os.environ.get("QS_LIGHTHOUSE_ID", "8274a68c-035d-4b8c-99b9-cc2438a5c9cd")
DASH_URL = f"{BASE}/sn/account/{ACCOUNT}/dashboards/{DASHBOARD_ID}"
LOGIN_URL = (f"{BASE}/sn/auth/signin?enable-sso=0&redirect_uri="
             f"https%3A%2F%2Fus-east-1.quicksight.aws.amazon.com%2Fsn%2Fstart%3Fdirectory-alias%3D{ACCOUNT}"
             f"%26enable-sso%3D0%26state%3DhashArgs%2523%26isauthcode%3Dtrue")
STATE_FILE = HERE / ".qs_state.json"
W1_2026 = dt.date(2025, 12, 28)  # semana Amazon: domingo → sábado


def log(msg, lvl="INFO"):
    print(f"[{dt.datetime.now():%H:%M:%S}] [{lvl}] {msg}", flush=True)


def amazon_week(d: dt.date):
    n = (d - W1_2026).days // 7 + 1
    return f"W{n}"


# ─── Login ────────────────────────────────────────────────────────────
def is_logged_in(page):
    url = page.url.lower()
    return ("/dashboards/" in url or "/start" in url) and "signin" not in url and "midway" not in url


def login(page, user, pwd):
    """Login por usuario/contraseña de Quick (enable-sso=0 evita la redirección a Midway)."""
    log(f"Login QuickSight (cuenta {ACCOUNT}) …")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    # Página «Choose your account» (cuenta recordada por el navegador): pulsar la cuenta
    if page.get_by_text(re.compile(r"Choose your account", re.I)).count():
        acct = page.get_by_role("button").filter(has_text=re.compile(re.escape(ACCOUNT)))
        if not acct.count(): acct = page.get_by_text(ACCOUNT, exact=False)
        acct.first.click(); log("Cuenta recordada seleccionada")
        page.wait_for_timeout(3000)
    if not page.locator("input[type=password]").count():
        # Paso 0: nombre de cuenta (sólo si lo pide)
        if page.locator("input[type=text]").count() and ACCOUNT not in page.content():
            page.locator("input[type=text]").first.fill(ACCOUNT)
            page.locator("button[type=submit], button:has-text('Next')").first.click()
            page.wait_for_timeout(3000)
        # Paso 1: usuario
        if not page.locator("input[type=password]").count():
            page.locator("input[type=text]").first.wait_for(timeout=20000)
            page.locator("input[type=text]").first.fill(user)
            page.locator("button[type=submit]").first.click()
    # Paso 2: contraseña (página us-east-1.signin.aws)
    page.locator("input[type=password]").wait_for(timeout=45000)
    page.locator("input[type=password]").fill(pwd)
    page.locator("button[type=submit]").first.click()
    # Esperar a que termine la cadena de redirecciones signin.aws → QuickSight (puede tardar >10 s)
    try:
        page.wait_for_url(re.compile(r"quicksight\.aws\.amazon\.com/sn/.*(start|dashboards)"), timeout=90000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3000)
    if "midway" in page.url.lower():
        raise RuntimeError("Redirigido a Midway: la cuenta exige SSO. Revisa ACCOUNT / usuario.")
    if not is_logged_in(page):
        page.screenshot(path=str(HERE / "login_error.png"))
        raise RuntimeError(f"Login fallido, URL: {page.url} (ver login_error.png)")
    log("Login OK")


# ─── Helpers de UI ────────────────────────────────────────────────────
def open_sheet(page, name):
    tab = page.get_by_role("tab", name=re.compile(re.escape(name)))
    tab.first.click()
    page.wait_for_timeout(6000)
    log(f"Hoja «{name}» abierta")


def expand_controls(page):
    """El panel de controles suele ir plegado en un navegador nuevo; lo desplegamos si no vemos los combos."""
    def visible():
        return page.get_by_label("Start Date").count() or page.locator('[role="combobox"]').count() > 2
    if visible():
        return
    for loc in (page.get_by_role("button", name=re.compile(r"controls", re.I)),
                page.locator('[aria-label*="ontrols"]'),
                page.get_by_text(re.compile(r"^\s*Controls\s*$"))):
        if loc.count():
            loc.first.click(); page.wait_for_timeout(2500)
            log("Panel de controles desplegado")
            if visible():
                return
    names = [b.strip().replace("\n", " ")[:30] for b in page.get_by_role("button").all_inner_texts() if b.strip()][:40]
    log(f"No veo los controles de la hoja; botones visibles: {names}", "WARN")


def dump_debug(page, tag):
    """Captura + inventario de elementos para diagnosticar selectores (logs/debug_<tag>.*)."""
    try:
        logs = HERE / "logs"; logs.mkdir(exist_ok=True)
        page.screenshot(path=str(logs / f"debug_{tag}.png"), full_page=True)
        els = page.evaluate("""() => [...document.querySelectorAll('button,[role=combobox],[role=option],[role=heading],[role=tab],h1,h2,h3,input,[role=textbox]')]
            .slice(0, 600).map(e => [e.tagName, e.getAttribute('role')||'', e.getAttribute('aria-label')||'', (e.innerText||e.value||'').trim().replace(/\\s+/g,' ').slice(0,80)].join(' | '))""")
        (logs / f"debug_{tag}.txt").write_text("\n".join(els), encoding="utf-8")
        log(f"Diagnóstico guardado en logs/debug_{tag}.png/.txt", "WARN")
    except Exception as e:
        log(f"No se pudo guardar el diagnóstico: {e}", "WARN")


def combo_text(c):
    """Texto visible de un combobox (QuickSight usa a veces <input role=combobox> con el valor en value)."""
    t = ""
    for getter in (lambda: c.inner_text(), lambda: c.input_value(),
                   lambda: c.get_attribute("aria-label") or "", lambda: c.get_attribute("title") or ""):
        try:
            t = (getter() or "").strip().replace("\n", " ")
        except Exception:
            t = ""
        if t:
            break
    return t


def select_combo(page, current_pattern, option_text):
    """Abre el combobox cuyo texto cumple current_pattern y elige option_text."""
    combos = page.locator('[role="combobox"], select')
    items = [(combos.nth(i), combo_text(combos.nth(i))) for i in range(combos.count())]
    target = next((c for c, t in items if re.search(current_pattern, t)), None)
    if target is None:
        if any(t == option_text for _, t in items):
            log(f"Control ya en «{option_text}»"); return
        log(f"Combobox {current_pattern!r} no encontrado; comboboxes visibles: {[t[:40] for _, t in items]}", "WARN"); return
    if combo_text(target) == option_text:
        return
    target.scroll_into_view_if_needed(); target.click(); page.wait_for_timeout(800)
    opt = page.get_by_role("option", name=re.compile(re.escape(option_text)))
    if not opt.count():
        opt = page.get_by_text(option_text, exact=True)
    if not opt.count():
        dump_debug(page, "combo_" + re.sub(r"\W+", "_", option_text)); page.keyboard.press("Escape")
        raise RuntimeError(f"No encuentro la opción «{option_text}» en el desplegable")
    opt.last.click()
    page.wait_for_timeout(5000)
    log(f"Control → {option_text}")


def set_metrics_date_range(page, start: dt.date, end: dt.date):
    """Hoja 'DSP Lighthouse metrics': inputs Start Date / End Date + calendario + Apply."""
    s, e = page.get_by_label("Start Date"), page.get_by_label("End Date")
    if not s.count():
        dump_debug(page, "metrics_dates"); raise RuntimeError("No encuentro el control de fechas (Start Date) de la hoja de métricas")
    s.first.scroll_into_view_if_needed()
    s.first.fill(f"{start:%Y/%m/%d} 00:00:00"); s.first.press("Enter")
    e.first.fill(f"{end:%Y/%m/%d} 00:00:00"); e.first.press("Enter")
    page.wait_for_timeout(800)
    # El picker exige «Apply»: abrimos el calendario (botón "Calendar") y pulsamos Apply
    cal = page.get_by_role("button", name="Calendar")
    (cal.first if cal.count() else s.first).click(); page.wait_for_timeout(1000)
    apply = page.get_by_role("button", name="Apply")
    if apply.count():
        apply.first.click()
    else:
        log("No aparece el botón Apply del calendario; el rango puede no haberse aplicado", "WARN")
        page.keyboard.press("Escape")
    page.wait_for_timeout(8000)
    log(f"Rango métricas aplicado: {s.first.input_value()} → {e.first.input_value()}")


def set_orders_date_range(page, start: dt.date, end: dt.date):
    """Hoja 'Order Level View': control único 'YYYY/MM/DD - YYYY/MM/DD' con popover Start date / End date."""
    box = page.get_by_role("textbox", name=re.compile(r"^\d{4}/\d{2}/\d{2} .* - \d{4}/\d{2}/\d{2}"))
    if not box.count():
        box = page.locator('input[value*=" - "]')
    if not box.count():
        dump_debug(page, "orders_dates"); raise RuntimeError("No encuentro el control de fechas de 'Order Level View'")
    box.first.scroll_into_view_if_needed(); box.first.click(); page.wait_for_timeout(1000)
    page.get_by_label("Start date").fill(f"{start:%Y/%m/%d} 00:00:00"); page.get_by_label("Start date").press("Enter")
    page.wait_for_timeout(500)
    page.get_by_label("End date").fill(f"{end:%Y/%m/%d} 00:00:00"); page.get_by_label("End date").press("Enter")
    page.wait_for_timeout(1000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(8000)
    log(f"Rango pedidos: {box.first.input_value()}")


def export_visual(page, title_pattern, out_path: Path):
    """Pasa el ratón por el visual (su ⋮ sólo aparece al hacer hover) → Menu options → Export to CSV."""
    menu = page.get_by_role("button", name=re.compile(r"^Menu options, " + title_pattern))
    if not menu.count():
        head = page.get_by_role("heading", name=re.compile(title_pattern))
        if not head.count(): head = page.get_by_text(re.compile(title_pattern))
        if head.count():
            h = head.last; h.scroll_into_view_if_needed(); h.hover(); page.wait_for_timeout(800)
            if not menu.count():
                bb = h.bounding_box()
                if bb: page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] + 120); page.wait_for_timeout(800)
    if not menu.count():
        dump_debug(page, "export_" + re.sub(r"\W+", "_", title_pattern)[:30])
        raise RuntimeError(f"No encuentro el menú del visual «{title_pattern}»")
    menu.first.scroll_into_view_if_needed(); menu.first.hover(); menu.first.click()
    page.wait_for_timeout(700)
    with page.expect_download(timeout=120000) as dl:
        page.get_by_role("menuitem", name="Export to CSV").click()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dl.value.save_as(str(out_path))
    page.keyboard.press("Escape")
    size = out_path.stat().st_size
    log(f"Guardado {out_path.name} ({size:,} bytes)")
    if size < 200:
        log(f"{out_path.name} parece vacío", "WARN")


# ─── Main ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weeks", type=int, default=12, help="semanas hacia atrás (por defecto 12)")
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD (domingo de inicio); anula --weeks")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD (por defecto hoy)")
    ap.add_argument("--out", default=str(REPO / "data" / "lighthouse"), help="carpeta de salida")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--merge", action="store_true", help="ejecuta scripts/lighthouse_to_dashboard.py al terminar")
    ap.add_argument("--commit", action="store_true", help="git add + commit (sin push) tras el merge")
    ap.add_argument("--push", action="store_true", help="git push tras el commit (dispara el deploy de Netlify)")
    a = ap.parse_args()

    user = os.environ.get("QS_USERNAME"); pwd = os.environ.get("QS_PASSWORD")
    if not (user and pwd):
        sys.exit("Faltan QS_USERNAME / QS_PASSWORD en el entorno (usa run.sh o exporta las variables).")

    today = dt.date.today()
    end = dt.date.fromisoformat(a.date_to) if a.date_to else today
    if a.date_from:
        start = dt.date.fromisoformat(a.date_from)
    else:
        last_sunday = end - dt.timedelta(days=(end.weekday() + 1) % 7)
        start = last_sunday - dt.timedelta(weeks=a.weeks - 1)
    rng = f"{amazon_week(start)}-{amazon_week(end)}"
    out_dir = Path(a.out) / f"{today:%Y-%m-%d}"
    log(f"Rango {start} → {end} ({rng}); salida {out_dir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=a.headless)
        ctx_kwargs = {"accept_downloads": True, "viewport": {"width": 1500, "height": 1700}}
        if STATE_FILE.exists():
            ctx_kwargs["storage_state"] = str(STATE_FILE)
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        page.set_default_timeout(30000)

        page.goto(DASH_URL, wait_until="domcontentloaded"); page.wait_for_timeout(6000)
        if not is_logged_in(page):
            login(page, user, pwd)
            page.goto(DASH_URL, wait_until="domcontentloaded"); page.wait_for_timeout(8000)
        ctx.storage_state(path=str(STATE_FILE))

        try:
          # ── Hoja 1: métricas semanales ──
          open_sheet(page, "DSP Lighthouse metrics")
          expand_controls(page)
          select_combo(page, r"\d\. (Daily|Weekly|Monthly|Quarterly)", "2. Weekly")
          select_combo(page, r"\d\. By ", "4. By Station Code")
          set_metrics_date_range(page, start, end)
          export_visual(page, r"Business Metrics", out_dir / f"business_metrics_by_station_{rng}.csv")
          export_visual(page, r"Metric by Show By", out_dir / f"quality_metrics_by_station_{rng}.csv")
          select_combo(page, r"\d\. By ", "5. By DSP Company")
          export_visual(page, r"Metric by Show By", out_dir / f"quality_metrics_network_{rng}.csv")
          select_combo(page, r"\d\. By ", "4. By Station Code")  # dejamos la vista como estaba

          # ── Hoja 2: defectos a nivel de pedido ──
          open_sheet(page, "Order Level View")
          expand_controls(page)
          set_orders_date_range(page, start, end)
          export_visual(page, r"Order Level Data", out_dir / f"order_level_defects_{rng}.csv")

        except Exception:
          dump_debug(page, "fail"); raise
        ctx.storage_state(path=str(STATE_FILE))
        browser.close()

    (out_dir / "manifest.json").write_text(json.dumps({
        "downloaded_at": dt.datetime.now().isoformat(timespec="seconds"),
        "range": {"from": start.isoformat(), "to": end.isoformat(), "weeks": rng},
        "dashboard": DASH_URL}, indent=2))
    log("Descarga completa")

    if a.merge:
        script = REPO / "scripts" / "lighthouse_to_dashboard.py"
        log(f"Fusionando en Dashboard.jsx …")
        subprocess.run([sys.executable, str(script), str(out_dir), "--jsx", str(REPO / "src" / "Dashboard.jsx")], check=True)
        if a.commit:
            subprocess.run(["git", "add", "src/Dashboard.jsx", str(out_dir.relative_to(REPO))], cwd=REPO, check=True)
            r = subprocess.run(["git", "commit", "-m", f"data: Lighthouse {rng} ({today})"], cwd=REPO)
            if r.returncode == 0 and a.push:
                subprocess.run(["git", "push"], cwd=REPO, check=True)
                log("Push hecho → Netlify desplegará la nueva versión")


if __name__ == "__main__":
    try:
        main()
    except PWTimeout as e:
        log(f"Timeout de Playwright: {e}", "ERROR"); sys.exit(2)
