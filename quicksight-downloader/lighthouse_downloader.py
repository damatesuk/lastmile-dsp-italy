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
from urllib.parse import urlparse

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
    u = urlparse(page.url); path = u.path.lower()
    return ("quicksight.aws.amazon.com" in u.netloc and ("/dashboards/" in path or "/start" in path)
            and "/auth/signin" not in path and "midway" not in u.netloc)


def _login_step(page, timeout_ms=30000, after_choose=False):
    """Espera al siguiente estado del login: 'in' (ya dentro), 'choose' (selector de cuenta), 'pwd' (pide contraseña) o 'user' (pide usuario/cuenta).
    after_choose=True: acabamos de pulsar la cuenta recordada; el selector sigue visible unos segundos mientras redirige → no contarlo."""
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        if is_logged_in(page): return "in"
        if page.locator("input[type=password]:visible").count(): return "pwd"
        chooser = page.get_by_text(re.compile(r"Choose your account", re.I)).count()
        if chooser and not after_choose: return "choose"
        if not chooser and page.locator("input[type=text]:visible, input[type=email]:visible").count(): return "user"
        page.wait_for_timeout(500)
    return "?"


def login(page, user, pwd):
    """Login por usuario/contraseña de Quick (enable-sso=0 evita la redirección a Midway)."""
    log(f"Login QuickSight (cuenta {ACCOUNT}) …")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    st = _login_step(page, 20000)
    # Página «Choose your account» (cuenta recordada por el navegador): pulsar la cuenta
    if st == "choose":
        acct = page.get_by_role("button").filter(has_text=re.compile(re.escape(ACCOUNT)))
        if not acct.count(): acct = page.get_by_text(ACCOUNT, exact=False)
        acct.first.click(); log("Cuenta recordada seleccionada")
        st = _login_step(page, 60000, after_choose=True)
        log(f"Tras el selector de cuenta: estado «{st}» en {page.url[:80]}")
    if st == "user":
        # Paso 0: nombre de cuenta (sólo si lo pide)
        if ACCOUNT not in page.content():
            page.locator("input[type=text]:visible").first.fill(ACCOUNT)
            page.locator("button[type=submit], button:has-text('Next')").first.click()
            st = _login_step(page, 30000)
        # Paso 1: usuario
        if st == "user":
            page.locator("input[type=text]:visible, input[type=email]:visible").first.fill(user)
            page.locator("button[type=submit]").first.click()
            st = _login_step(page, 45000)
    if st == "pwd":
        # Paso 2: contraseña (página us-east-1.signin.aws)
        page.locator("input[type=password]:visible").first.fill(pwd)
        page.locator("button[type=submit]").first.click()
        st = None
    if st != "in":
        # Esperar a que termine la cadena de redirecciones signin.aws → QuickSight (puede tardar >10 s)
        try:
            page.wait_for_url(re.compile(r"quicksight\.aws\.amazon\.com/sn/.*(start|dashboards)"), timeout=90000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
    if "midway" in page.url.lower():
        raise RuntimeError("Redirigido a Midway: la cuenta exige SSO. Revisa ACCOUNT / usuario.")
    if not is_logged_in(page):
        try:
            page.screenshot(path=str(HERE / "login_error.png"), timeout=10000)
            (HERE / "logs").mkdir(exist_ok=True); (HERE / "logs" / "login_error.html").write_text(page.content(), encoding="utf-8")
        except Exception as e:
            log(f"Sin captura del fallo de login: {e}", "WARN")
        raise RuntimeError(f"Login fallido (estado «{st}»), URL: {page.url} (ver login_error.png / logs/login_error.html)")
    log("Login OK")


# ─── Helpers de UI ────────────────────────────────────────────────────
# Selectores estables de QuickSight (data-automation-id), verificados el 29/09/2026:
#   panel de controles  [data-automation-id="sheet-control-panel-toggle-expand"] (aria-expanded true/false)
#   valor de un control [data-automation-id="sheet_control_value"][data-automation-context="<nombre>"] (role=combobox)
#   fechas              input[data-automation-id="date_picker_0"] (Start Date) / "date_picker_1" (End Date) + botón "Calendar" → "Apply"
#   menú de un visual   button "Menu options, <título>, Pivot table|Table" — sólo existe mientras el ratón está sobre el visual
CTRL_VALUE = '[data-automation-id="sheet_control_value"]'


def open_sheet(page, name):
    tab = page.get_by_role("tab", name=re.compile(re.escape(name)))
    tab.first.click()
    page.wait_for_timeout(6000)
    log(f"Hoja «{name}» abierta")


def dump_debug(page, tag):
    """Captura + inventario de elementos para diagnosticar selectores (logs/debug_<tag>.*)."""
    try:
        logs = HERE / "logs"; logs.mkdir(exist_ok=True)
        page.screenshot(path=str(logs / f"debug_{tag}.png"), full_page=True)
        els = page.evaluate("""() => [...document.querySelectorAll('button,[role=button],[role=combobox],[role=option],[role=heading],[role=tab],h1,h2,h3,h5,input,[role=textbox],[data-automation-id]:not([data-automation-id^=sn-table-cell])')]
            .slice(0, 800).map(e => { const a = {}; for (const at of e.attributes) if (/^(aria-|data-automation|role|title|placeholder|type|value)/.test(at.name)) a[at.name] = at.value.slice(0, 80);
              return [e.tagName, JSON.stringify(a), (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,80)].join(' | '); })""")
        (logs / f"debug_{tag}.txt").write_text("\n".join(els), encoding="utf-8")
        log(f"Diagnóstico guardado en logs/debug_{tag}.png/.txt", "WARN")
    except Exception as e:
        log(f"No se pudo guardar el diagnóstico: {e}", "WARN")


def reset_to_original(page):
    """«Reset to original»: quita filtros persistidos del usuario (p. ej. station_code = UIT4) antes de exportar."""
    btn = page.locator('[data-automation-id="analysis_navbar_reset"]')
    if not btn.count():
        log("No encuentro «Reset to original»; sigo con el estado guardado", "WARN"); return
    btn.first.click(); page.wait_for_timeout(1500)
    confirm = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"reset|yes|ok|confirm", re.I))
    if confirm.count():
        confirm.first.click()
    page.wait_for_timeout(6000)
    log("Dashboard restablecido a «Original»")


def expand_controls(page):
    """Despliega el panel «Controls» de la hoja si está plegado."""
    tog = page.locator('[data-automation-id="sheet-control-panel-toggle-expand"]')
    if not tog.count():
        log("La hoja no tiene panel de controles", "WARN"); return
    if tog.first.get_attribute("aria-expanded") != "true":
        tog.first.click(); page.wait_for_timeout(2000)
    if tog.first.get_attribute("aria-expanded") != "true":
        dump_debug(page, "controls"); raise RuntimeError("No consigo desplegar el panel de controles")


def control_values(page):
    """{nombre de control: valor visible} de la hoja actual."""
    vals = {}
    loc = page.locator(CTRL_VALUE)
    for i in range(loc.count()):
        c = loc.nth(i)
        vals[c.get_attribute("data-automation-context") or f"#{i}"] = (c.inner_text() or "").strip().replace("\n", " ")
    return vals


def log_controls(page, expect_all=()):
    vals = control_values(page)
    log("Controles: " + "; ".join(f"{k} = {v}" for k, v in vals.items()))
    bad = [k for k in expect_all if k in vals and vals[k] != "All"]
    if bad:
        log(f"Filtros que NO están en «All»: {bad} — la exportación saldrá filtrada", "WARN")
    return vals


def select_combo(page, control_name, option_text):
    """Elige option_text en el control desplegable data-automation-context=control_name."""
    combo = page.locator(f'{CTRL_VALUE}[data-automation-context="{control_name}"]')
    if not combo.count():
        dump_debug(page, "combo_" + re.sub(r"\W+", "_", control_name))
        raise RuntimeError(f"No encuentro el control «{control_name}»")
    cur = (combo.first.inner_text() or "").strip()
    if cur == option_text:
        return
    dismiss_popovers(page)
    combo.first.scroll_into_view_if_needed(); combo.first.click(); page.wait_for_timeout(800)
    opt = page.get_by_role("option", name=re.compile(r"^\s*" + re.escape(option_text) + r"\s*$"))
    if not opt.count():
        opt = page.get_by_text(option_text, exact=True)
    if not opt.count():
        dump_debug(page, "opt_" + re.sub(r"\W+", "_", option_text)); page.keyboard.press("Escape")
        raise RuntimeError(f"No encuentro la opción «{option_text}» en «{control_name}»")
    opt.last.click()
    page.wait_for_timeout(5000)
    log(f"{control_name} → {option_text}")


def dismiss_popovers(page):
    """Cierra popovers MUI abiertos (calendario, listas) que interceptan los clics."""
    for _ in range(3):
        if not page.locator('.MuiPopover-root:visible, [role="presentation"] [role="dialog"]:visible').count():
            return
        page.keyboard.press("Escape"); page.wait_for_timeout(600)
    if page.locator('.MuiPopover-root:visible').count():
        tab = page.get_by_role("tab", selected=True)
        if tab.count(): tab.first.click(); page.wait_for_timeout(600)


def set_radio(page, value):
    """Marca el radio con value=<value> (p. ej. Show Attribution = True)."""
    r = page.locator(f'input[type=radio][value="{value}"]')
    if not r.count():
        log(f"No encuentro el radio «{value}»", "WARN"); return
    if not r.first.is_checked():
        r.first.check(force=True); page.wait_for_timeout(4000); log(f"Radio → {value}")


def set_date_range(page, start: dt.date, end: dt.date):
    """Control «delivery_date between» del panel desplegado: Start Date / End Date + Calendar → Apply."""
    expand_controls(page)
    s = page.locator('input[data-automation-id="date_picker_0"]'); e = page.locator('input[data-automation-id="date_picker_1"]')
    if not s.count():
        # Control de fecha relativa (hoja Order Level View): caja «YYYY/MM/DD … to …» → popover Start date / End date
        box = page.locator('input[data-automation-id="sheet-control-relative-date"]')
        if box.count():
            return set_relative_date_range(page, box.first, start, end)
        s, e = page.get_by_label("Start Date"), page.get_by_label("End Date")
    if not s.count():
        dump_debug(page, "dates"); raise RuntimeError("No encuentro los campos Start Date / End Date")
    want_s, want_e = f"{start:%Y/%m/%d} 00:00:00", f"{end:%Y/%m/%d} 00:00:00"
    if s.first.input_value().strip() == want_s and e.first.input_value().strip() == want_e:
        log(f"Rango ya en {want_s} → {want_e}"); return
    s.first.scroll_into_view_if_needed()
    s.first.fill(want_s); s.first.press("Tab"); page.wait_for_timeout(500)
    e.first.fill(want_e); e.first.press("Tab"); page.wait_for_timeout(800)
    # Escribir no aplica el filtro: hay que abrir el calendario y pulsar «Apply» (el popover queda abierto → Escape)
    cal = page.get_by_role("button", name="Calendar")
    (cal.first if cal.count() else s.first).click(); page.wait_for_timeout(1200)
    apply = page.get_by_role("button", name=re.compile(r"^Apply$"))
    if apply.count():
        apply.first.click(); page.wait_for_timeout(1000)
    else:
        log("No aparece el botón Apply del calendario; el rango puede no haberse aplicado", "WARN")
    dismiss_popovers(page)
    page.wait_for_timeout(7000)
    got_s, got_e = s.first.input_value().strip(), e.first.input_value().strip()
    log(f"Rango aplicado: {got_s} → {got_e}")
    if (got_s, got_e) != (want_s, want_e):
        dump_debug(page, "dates_apply"); log(f"El rango no coincide con el pedido ({want_s} → {want_e})", "WARN")


def set_relative_date_range(page, box, start: dt.date, end: dt.date):
    """Control de fecha relativa: popover «Date type» → Relative date → Last number of days = N."""
    days = (end - start).days + 1
    cur = box.input_value().strip()
    if cur == f"Last {days} days":
        log(f"Rango pedidos ya en «{cur}»"); return
    box.scroll_into_view_if_needed(); box.click(); page.wait_for_timeout(1500)
    dlg = page.get_by_role("dialog", name=re.compile(r"Date type"))
    if not dlg.count():
        dump_debug(page, "orders_dates"); dismiss_popovers(page)
        log(f"No se abre el popover de fechas; se exporta con el rango actual ({cur})", "WARN"); return
    for val in ("relative", "Last number of days"):
        r = dlg.locator(f'input[type=radio][value="{val}"]')
        if r.count() and not r.first.is_checked():
            r.first.check(force=True); page.wait_for_timeout(500)
    n = dlg.locator('input[aria-label="Last number of days"]')
    n.first.fill(str(days)); n.first.press("Tab"); page.wait_for_timeout(800)
    apply = page.get_by_role("button", name=re.compile(r"^Apply$"))
    if apply.count():
        apply.first.click(); page.wait_for_timeout(1000)
    dismiss_popovers(page)
    page.wait_for_timeout(6000)
    got = box.input_value().strip()
    log(f"Rango pedidos: «{got}» ({days} días)")
    if got != f"Last {days} days":
        dump_debug(page, "orders_dates_apply"); log("El rango de pedidos no se ha aplicado como se pedía", "WARN")


def select_all_multi(page, control_name):
    """Control multi-selección (p. ej. «Defect»): marca «Select all» para exportar sin filtro."""
    combo = page.locator(f'{CTRL_VALUE}[data-automation-context="{control_name}"]')
    if not combo.count():
        log(f"No encuentro el control «{control_name}»", "WARN"); return
    if (combo.first.inner_text() or "").strip() == "All":
        return
    dismiss_popovers(page)
    combo.first.scroll_into_view_if_needed(); combo.first.click(); page.wait_for_timeout(1500)
    sel = page.locator('[data-automation-id="dropdown_select_all_search_result_entry"]')
    if not sel.count():
        dump_debug(page, "multi_" + re.sub(r"\W+", "_", control_name)); dismiss_popovers(page)
        log(f"No encuentro «Select all» en «{control_name}»", "WARN"); return
    if sel.first.get_attribute("aria-selected") != "true":
        sel.first.click(); page.wait_for_timeout(800)
    close = page.get_by_role("button", name=re.compile(r"^Close$"))
    if close.count(): close.first.click()
    else: page.keyboard.press("Escape")
    page.wait_for_timeout(5000)
    log(f"{control_name} → {(combo.first.inner_text() or '').strip()}")


def export_visual(page, title_pattern, anchor_text, out_path: Path):
    """Pasa el ratón por el visual (su ⋮ sólo existe en hover) → «Menu options, <título>» → Export to CSV."""
    dismiss_popovers(page)
    menu = page.get_by_role("button", name=re.compile(r"^Menu options, " + title_pattern))
    candidates = (page.locator('[data-automation-id="analysis_visual"][data-automation-context*="' + anchor_text.replace('"', '') + '"]'),
                  page.locator('[data-automation-id="grid-block"]').filter(has_text=re.compile(title_pattern)),
                  page.get_by_text(anchor_text, exact=True),
                  page.get_by_text(re.compile(title_pattern)))
    for attempt in range(8):  # el visual puede tardar en cargar (banner «New data present»); reintentamos ~1 min
        for anchor in candidates:
            if menu.count():
                break
            for i in range(min(anchor.count(), 3)):
                a = anchor.nth(anchor.count() - 1 - i)  # de atrás hacia delante (el visual suele ir después del texto de cabecera)
                try:
                    a.scroll_into_view_if_needed(timeout=5000); a.hover(timeout=5000); page.wait_for_timeout(1200)
                except PWTimeout:
                    continue
                if menu.count():
                    break
        if menu.count():
            break
        page.mouse.move(5, 5); page.wait_for_timeout(5000)
    if not menu.count():
        dump_debug(page, "export_" + re.sub(r"\W+", "_", title_pattern)[:30])
        raise RuntimeError(f"No encuentro el menú del visual «{title_pattern}» (ancla «{anchor_text}»)")
    menu.first.hover(); menu.first.click(); page.wait_for_timeout(800)
    with page.expect_download(timeout=180000) as dl:
        page.get_by_role("menuitem", name=re.compile(r"Export to CSV")).first.click()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dl.value.save_as(str(out_path))
    page.keyboard.press("Escape"); page.wait_for_timeout(500)
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
    ap.add_argument("--steps", default="setup,network,orders",
                    help="fases a ejecutar (setup = métricas por estación, network = métricas red, orders = pedidos); por defecto todas")
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

        steps = [x.strip() for x in a.steps.split(",") if x.strip()]
        try:
          if "setup" in steps:
            reset_to_original(page)
            # ── Hoja 1: métricas semanales por estación ──
            open_sheet(page, "DSP Lighthouse metrics")
            set_date_range(page, start, end)
            log_controls(page, expect_all=("country equals", "station_code equals", "metric_name equals", "provider_company_name equals", "Category equals", "Sub Category equals"))
            select_combo(page, "Time Granularity", "2. Weekly")
            select_combo(page, "Show By", "4. By Station Code")
            select_combo(page, "Split By", "5. No Split")
            set_radio(page, "True")  # Show Attribution → sub-métricas «~ %»
            export_visual(page, r"Business Metrics", "Routes (% Change PoP)", out_dir / f"business_metrics_by_station_{rng}.csv")
            export_visual(page, r"Metric by Show By", "Sub Category", out_dir / f"quality_metrics_by_station_{rng}.csv")
          if "network" in steps:
            # ── Hoja 1: misma pivot a nivel de red (Show By = DSP Company) ──
            open_sheet(page, "DSP Lighthouse metrics")
            expand_controls(page)
            log_controls(page)
            select_combo(page, "Show By", "5. By DSP Company")
            export_visual(page, r"Metric by Show By", "Sub Category", out_dir / f"quality_metrics_network_{rng}.csv")
            select_combo(page, "Show By", "4. By Station Code")  # dejamos la vista como estaba
          if "orders" in steps:
            # ── Hoja 2: defectos a nivel de pedido ──
            open_sheet(page, "Order Level View")
            set_date_range(page, start, end)
            select_all_multi(page, "Defect")
            log_controls(page, expect_all=("region_id equals", "country equals", "program equals", "delivery_station_code equals", "provider_company_name equals", "Defect", "Rootcause"))
            export_visual(page, r"Order Level Data", "Order Level Data", out_dir / f"order_level_defects_{rng}.csv")

        except Exception:
          dump_debug(page, "fail"); raise
        ctx.storage_state(path=str(STATE_FILE))
        browser.close()

    if "orders" in steps: (out_dir / "manifest.json").write_text(json.dumps({
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
