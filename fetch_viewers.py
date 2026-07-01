#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_viewers.py — konsoliderad Viewers/Media-Views-insamling för Facebook + Instagram.

Bakgrund
--------
Metas gamla räckviddsmått (page_impressions_unique m.fl.) är deprekerade i Graph API
för ALLA versioner sedan 2026-06-15. Ersättningen är en enhetlig Viewers/Media-Views-
familj (unika viewers ≈ gammal reach, men UTAN paid/organic-uppdelning).

Detta skript ersätter INTE de gamla skripten — de behålls orörda som referens/fallback:
  - fetch_facebook_reach.py         (månad)
  - fetch_facebook_reach_weekly.py  (vecka)
  - fetch_instagram_reach.py        (månad; ingen IG-vecka fanns tidigare)

Två faser:
  Fas 0  --probe   Kartlägger vilka mått/perioder/historik som faktiskt går att få.
                   Skriver ENDAST till probe_results/. Går ALDRIG vidare till produktion.
  Fas 1  produktion  --facebook/--instagram × --month/--week → separata mappar.

Konventioner speglade från de gamla skripten:
  - Bearer-auth (access_token skickas som Authorization-header, ej query).
  - Reaktiv rate-limit-backoff (429 / error code 4 / 5xx).
  - Sidlistning via me/accounts; Srholder-placeholders filtreras bort på FB-sidan.
  - IG: konton via instagram_business_account; hårt 30-dagarsfönster (until = since + 30*86400).
  - config.py återanvänds (ACCESS_TOKEN, API_VERSION, INITIAL_START_YEAR_MONTH, ...).

VIKTIG SKILLNAD mot gamla skripten (avsiktlig, enligt uppdrag):
  Append-per-sida — varje sidas rad skrivs och flushas direkt efter hämtning, aldrig
  batch-sparning i slutet. (En 7h-körning har tidigare gått förlorad p.g.a. slut-sparning.)
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
import traceback
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config (återanvänds från befintliga skript)
# ---------------------------------------------------------------------------
try:
    import config
except ImportError:
    sys.stderr.write("FEL: config.py saknas. Kopiera config.py.example → config.py.\n")
    sys.exit(1)

ACCESS_TOKEN = getattr(config, "ACCESS_TOKEN", None)
CONFIG_API_VERSION = getattr(config, "API_VERSION", "v19.0")
INITIAL_START_YEAR_MONTH = getattr(config, "INITIAL_START_YEAR_MONTH", "2026-01")
MAX_RETRIES = getattr(config, "MAX_RETRIES", 3)
RETRY_DELAY = getattr(config, "RETRY_DELAY", 5)
TOKEN_LAST_UPDATED = getattr(config, "TOKEN_LAST_UPDATED", None)
TOKEN_VALID_DAYS = getattr(config, "TOKEN_VALID_DAYS", 60)
# OUTPUT_ROOT finns inte i nuvarande config → default = aktuell katalog (som gamla skripten,
# vilka skriver reach{YYYY}/, IGReach{YYYY}/, weekly_reports/ relativt CWD).
OUTPUT_ROOT = getattr(config, "OUTPUT_ROOT", ".")

# ---------------------------------------------------------------------------
# FAS 0-RESULTAT LÅSES HÄR. Konstanterna nedan är BÄSTA GISSNING utifrån Metas
# dokumentation/tredjepartskällor per 2026-07 och MÅSTE bekräftas med --probe
# innan produktionsinsamling litas på. Uppdatera efter probe-rapporten.
# ---------------------------------------------------------------------------
# Facebook — unika viewers (närmaste motsvarighet till gammal reach).
FB_VIEWERS_METRIC = "page_total_media_view_unique"   # alt: page_views_total (ej unikt)
FB_MONTH_PERIOD = "total_over_range"                  # spegla gamla månadsskriptet; alt: "day"+summering
FB_WEEK_PERIOD = "week"                               # spegla gamla veckoskriptet
# Instagram — reach (unika konton) fanns kvar i gamla skriptet; media-views som komplement.
IG_VIEWERS_METRIC = "reach"                           # alt: "views" (media views, ej unikt)
IG_SECONDARY_METRIC = "views"
IG_PERIOD = "day"                                     # spegla gamla IG-skriptet
IG_METRIC_TYPE = "total_value"

# Probe-kandidater (Fas 0). Testas skarpt; status registreras per mått.
FB_PAGE_METRIC_CANDIDATES = [
    "page_total_media_view_unique",   # primär viewers-ersättare
    "page_views_total",               # ej unikt, kontroll
    "page_impressions_unique",        # gammal reach — bekräfta att den är DÖD
]
FB_POST_METRIC_CANDIDATES = [
    "post_total_media_view_unique",
    "post_impressions_unique",        # gammal — bekräfta död
]
IG_METRIC_CANDIDATES = [
    "reach",                          # unika konton
    "views",                          # media views (ny enhetlig riktning)
]
PROBE_PERIODS = ["day", "week", "days_28", "month", "total_over_range"]
# Historik-sondering: stega bakåt (månader) och registrera var datan tar slut.
PROBE_BACKSTEPS_MONTHS = [0, 1, 3, 6, 12, 18, 24]

# Srholder-placeholder-filter (spegla veckoskriptets bredare, skiftlägesokänsliga regex).
_PLACEHOLDER_RE = re.compile(r"^[Ss][Rr]holder\w*$")

GRAPH_BASE_TMPL = "https://graph.facebook.com/{ver}"

# ---------------------------------------------------------------------------
# Logging (svenska, spegla gamla skriptens stil)
# ---------------------------------------------------------------------------
logger = logging.getLogger("fetch_viewers")


def setup_logging(debug=False):
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    os.makedirs("logs", exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    fh = logging.FileHandler(os.path.join("logs", f"viewers_{stamp}.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(fh)
    logger.addHandler(sh)


# ---------------------------------------------------------------------------
# HTTP-lager med Bearer-auth + reaktiv backoff (spegla gamla api_get/api_request)
# ---------------------------------------------------------------------------
_rate_limit_backoff = 1.0
_last_rate_limit_time = None
_consecutive_successes = 0


def _unpack_next_url(next_url):
    """Bryt ut params ur en paginerings-URL och lägg tillbaka access_token."""
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(next_url)
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return base, qs


class ApiError(Exception):
    def __init__(self, message, code=None, subcode=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode


def api_get(url, params, token=None):
    """
    GET mot Graph API med Bearer-header och reaktiv backoff.
    Returnerar JSON-dict. Kastar ApiError vid Graph-fel (så anropare kan logga per sida
    och gå vidare). token default = ACCESS_TOKEN.
    """
    global _rate_limit_backoff, _last_rate_limit_time, _consecutive_successes
    token = token or ACCESS_TOKEN
    safe = dict(params)
    safe.pop("access_token", None)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    last_err = None
    for attempt in range(MAX_RETRIES):
        # Proaktiv väntan om vi nyligen rate-limitades.
        if _last_rate_limit_time is not None:
            elapsed = time.time() - _last_rate_limit_time
            wait = 60 * _rate_limit_backoff - elapsed
            if wait > 0:
                logger.debug(f"Väntar {wait:.1f}s (backoff {_rate_limit_backoff:.1f}x) före anrop")
                time.sleep(wait)
        try:
            resp = requests.get(url, params=safe, headers=headers, timeout=30)
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(RETRY_DELAY * (2 ** attempt))
            continue

        if resp.status_code == 200:
            _consecutive_successes += 1
            if _consecutive_successes >= 50:
                _rate_limit_backoff = max(_rate_limit_backoff * 0.8, 1.0)
                _consecutive_successes = 0
            return resp.json()

        if resp.status_code == 429:
            _last_rate_limit_time = time.time()
            _rate_limit_backoff = min(_rate_limit_backoff * 1.5, 10.0)
            _consecutive_successes = 0
            ra = resp.headers.get("Retry-After")
            wait_s = min(float(ra), 120.0) if ra else 60 * _rate_limit_backoff
            logger.warning(f"Rate limit (429)! Väntar {wait_s:.0f}s")
            time.sleep(wait_s)
            continue

        if 500 <= resp.status_code < 600:
            time.sleep(min(RETRY_DELAY * (2 ** attempt), 30))
            continue

        # 4xx med Graph-fel
        try:
            err = resp.json().get("error", {})
        except Exception:
            err = {}
        code = err.get("code")
        subcode = err.get("error_subcode")
        msg = err.get("message", resp.text[:200])
        if code == 4:  # app-level rate limit
            _last_rate_limit_time = time.time()
            _rate_limit_backoff = min(_rate_limit_backoff * 1.5, 10.0)
            wait_s = min(60 * _rate_limit_backoff, 300)
            logger.warning(f"App rate limit (code 4)! Väntar {wait_s:.0f}s")
            time.sleep(wait_s)
            continue
        if code == 190:  # token invalid — meningslöst att försöka igen
            raise ApiError(f"Token ogiltig: {msg}", code, subcode)
        raise ApiError(f"HTTP {resp.status_code} kod {code}/{subcode}: {msg}", code, subcode)

    raise ApiError(f"Gav upp efter {MAX_RETRIES} försök: {last_err}")


# ---------------------------------------------------------------------------
# Token-validering (spegla gamla skriptens expiry-koll)
# ---------------------------------------------------------------------------
def check_token_expiry():
    if not TOKEN_LAST_UPDATED:
        return
    try:
        created = datetime.strptime(TOKEN_LAST_UPDATED, "%Y-%m-%d")
    except ValueError:
        return
    days_used = (datetime.now() - created).days
    days_left = TOKEN_VALID_DAYS - days_used
    if days_left <= 0:
        logger.error(f"Token har gått ut ({days_used} dagar sedan {TOKEN_LAST_UPDATED}).")
        sys.exit(1)
    if days_left <= 7:
        logger.warning(f"Token går ut om {days_left} dagar — förnya snart.")


# ---------------------------------------------------------------------------
# Sid- och kontolistning
# ---------------------------------------------------------------------------
@dataclass
class FbPage:
    page_id: str
    name: str
    token: str


@dataclass
class IgAccount:
    ig_id: str
    ig_username: str
    ig_name: str
    fb_page_name: str


def _api_version(override):
    return override or CONFIG_API_VERSION


def list_fb_pages(api_version, exclude_srholder=True):
    """Lista FB-sidor via me/accounts (paginerat). Filtrera bort Srholder-placeholders."""
    base = GRAPH_BASE_TMPL.format(ver=api_version)
    url = f"{base}/me/accounts"
    params = {"limit": 100, "fields": "id,name,access_token", "access_token": ACCESS_TOKEN}
    pages, filtered = [], 0
    while True:
        data = api_get(url, params)
        for p in data.get("data", []):
            name = p.get("name", "") or ""
            if exclude_srholder and _PLACEHOLDER_RE.match(name):
                filtered += 1
                continue
            pages.append(FbPage(p["id"], name, p.get("access_token", ACCESS_TOKEN)))
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            break
        url, params = _unpack_next_url(nxt)
    logger.info(f"Filtrerade bort {filtered} Srholder-placeholder-sidor; {len(pages)} sidor kvar.")
    return pages


def list_ig_accounts(api_version):
    """
    Lista IG-konton via me/accounts → instagram_business_account.
    Srholder-sidor behålls som brygga (spegla gamla IG-skriptet: inget Srholder-filter här).
    """
    base = GRAPH_BASE_TMPL.format(ver=api_version)
    url = f"{base}/me/accounts"
    params = {"limit": 100, "fields": "id,name,instagram_business_account", "access_token": ACCESS_TOKEN}
    accounts = []
    while True:
        data = api_get(url, params)
        for p in data.get("data", []):
            ig = p.get("instagram_business_account")
            if not ig:
                continue
            ig_id = ig["id"]
            username, ig_name = "", ""
            try:
                info = api_get(f"{base}/{ig_id}", {"fields": "username,name", "access_token": ACCESS_TOKEN})
                username = info.get("username", "")
                ig_name = info.get("name", "")
            except ApiError as e:
                logger.warning(f"Kunde inte hämta IG-info för {ig_id}: {e}")
            accounts.append(IgAccount(ig_id, username, ig_name, p.get("name", "")))
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            break
        url, params = _unpack_next_url(nxt)
    logger.info(f"Hittade {len(accounts)} Instagram-konton.")
    return accounts


# ---------------------------------------------------------------------------
# Datum-logik
# ---------------------------------------------------------------------------
def month_bounds_calendar(year, month):
    """Riktiga kalendermånadsgränser (FB). Returnerar (since_str, until_str, p_start, p_end)."""
    last = monthrange(year, month)[1]
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{last:02d}"
    return start, end, start, end


def month_bounds_ig_30day(year, month):
    """
    IG: hårt 30-dagarsfönster (until = since + 30*86400) för att hålla sig under IG-gränsen.
    31-dagarsmånader fångar bara 30 dagar; feb når in i mars. Spegla gamla IG-skriptet.
    Returnerar (since_ts, until_ts, p_start, p_end).
    """
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    since_ts = int(first.timestamp())
    until_ts = since_ts + (30 * 86400)
    p_start = first.strftime("%Y-%m-%d")
    p_end = datetime.fromtimestamp(until_ts - 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    return since_ts, until_ts, p_start, p_end


def iso_week_bounds(iso_year, iso_week):
    """ISO-vecka mån–sön. Returnerar (monday_date, sunday_date)."""
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def last_complete_month():
    today = date.today()
    y, m = today.year, today.month
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    return y, m


def last_complete_iso_week():
    """Senast avslutade mån–sön-vecka (spegla veckoskriptets get_last_complete_week)."""
    today = date.today()
    days_since_sunday = (today.weekday() + 1) % 7
    if days_since_sunday == 0:
        last_sunday = today - timedelta(days=7)
    else:
        last_sunday = today - timedelta(days=days_since_sunday)
    last_monday = last_sunday - timedelta(days=6)
    iso = last_monday.isocalendar()
    return iso[0], iso[1], last_monday, last_sunday


def months_from_start(start_ym):
    sy, sm = map(int, start_ym.split("-"))
    now = date.today()
    out = []
    y, m = sy, sm
    while (y, m) < (now.year, now.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


# ---------------------------------------------------------------------------
# CSV — append-per-sida (skriv+flush varje rad direkt)
# ---------------------------------------------------------------------------
class AppendCsv:
    """Öppnar en CSV, skriver header direkt, och flushar efter varje rad. Krasch-säkert."""

    def __init__(self, path, fieldnames):
        self.path = path
        self.fieldnames = fieldnames
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._f = open(path, "w", newline="", encoding="utf-8-sig")
        self._w = csv.DictWriter(self._f, fieldnames=fieldnames, extrasaction="ignore")
        self._w.writeheader()
        self._f.flush()
        self.count = 0

    def write(self, row):
        self._w.writerow(row)
        self._f.flush()
        os.fsync(self._f.fileno())
        self.count += 1

    def close(self):
        self._f.close()


def viewers_source_tag(metric, api_version):
    """Diagnostikvärde: vilket mått + API-version som gav värdet (viewers ≠ gammal reach)."""
    return f"{metric}@{api_version}"


# ---------------------------------------------------------------------------
# Fetch-primitiver
# ---------------------------------------------------------------------------
def fetch_fb_page_metric(api_version, page, metric, period, since, until, token=None):
    """Ett insights-anrop för en FB-sida. Returnerar (värde|None, felmeddelande|None)."""
    base = GRAPH_BASE_TMPL.format(ver=api_version)
    url = f"{base}/{page.page_id}/insights"
    params = {
        "metric": metric,
        "period": period,
        "since": since,
        "until": until,
        "access_token": token or page.token,
    }
    try:
        data = api_get(url, params, token=token or page.token)
    except ApiError as e:
        return None, str(e)
    entries = data.get("data", [])
    if not entries:
        return None, None  # tomt (giltigt anrop, ingen data)
    total = 0
    got = False
    for values in entries[0].get("values", []):
        v = values.get("value")
        if isinstance(v, (int, float)):
            total += int(v)
            got = True
    return (total if got else None), None


def fetch_ig_metric(api_version, ig_id, metric, since_ts, until_ts):
    """Ett IG insights-anrop (metric_type=total_value). Returnerar (värde|None, fel|None)."""
    base = GRAPH_BASE_TMPL.format(ver=api_version)
    url = f"{base}/{ig_id}/insights"
    params = {
        "metric": metric,
        "period": IG_PERIOD,
        "metric_type": IG_METRIC_TYPE,
        "since": since_ts,
        "until": until_ts,
        "access_token": ACCESS_TOKEN,
    }
    try:
        data = api_get(url, params)
    except ApiError as e:
        return None, str(e)
    entries = data.get("data", [])
    if not entries:
        return None, None
    try:
        return int(entries[0]["total_value"]["value"]), None
    except (KeyError, TypeError, ValueError):
        return None, None


def fetch_ig_followers(api_version, ig_id):
    base = GRAPH_BASE_TMPL.format(ver=api_version)
    try:
        data = api_get(f"{base}/{ig_id}", {"fields": "followers_count", "access_token": ACCESS_TOKEN})
        return data.get("followers_count", "")
    except ApiError:
        return ""


# ---------------------------------------------------------------------------
# FAS 0 — PROBE
# ---------------------------------------------------------------------------
def probe(api_version, do_fb, do_ig, sample):
    os.makedirs("probe_results", exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    rows = []  # platform, level, metric, period, status, earliest_data_month, note

    logger.info(f"=== FAS 0 PROBE === API_VERSION={api_version} sample={sample}")

    y0, m0 = last_complete_month()
    since_m, until_m, _, _ = month_bounds_calendar(y0, m0)

    if do_fb:
        pages = list_fb_pages(api_version)[:sample]
        logger.info(f"[FB] Sonderar mot {len(pages)} sidor.")
        # A+B: metric- och periodtillgänglighet (sidnivå)
        for metric in FB_PAGE_METRIC_CANDIDATES:
            for period in PROBE_PERIODS:
                status, note = _probe_fb_period(api_version, pages, metric, period, since_m, until_m)
                rows.append(("facebook", "page", metric, period, status, "", note))
        # C: historik-bakåtsondering för det primära måttet (period FB_MONTH_PERIOD)
        earliest = _probe_fb_backwards(api_version, pages, FB_VIEWERS_METRIC, FB_MONTH_PERIOD)
        rows.append(("facebook", "page", FB_VIEWERS_METRIC, FB_MONTH_PERIOD,
                     "BACKSTEP", earliest, "första månad med data (bakåt)"))
        # Postnivå: bara metric-existens på ett urval poster
        for metric in FB_POST_METRIC_CANDIDATES:
            status, note = _probe_fb_post(api_version, pages, metric, since_m, until_m)
            rows.append(("facebook", "post", metric, "total_over_range", status, "", note))

    if do_ig:
        accounts = list_ig_accounts(api_version)[:sample]
        logger.info(f"[IG] Sonderar mot {len(accounts)} konton.")
        since_ts, until_ts, _, _ = month_bounds_ig_30day(y0, m0)
        for metric in IG_METRIC_CANDIDATES:
            status, note = _probe_ig(api_version, accounts, metric, since_ts, until_ts)
            rows.append(("instagram", "account", metric, IG_PERIOD, status, "", note))
        # D: 30-dagarsgräns — bekräfta att >30 dagar fel-ar
        note30 = _probe_ig_30day(api_version, accounts, IG_VIEWERS_METRIC, y0, m0)
        rows.append(("instagram", "account", IG_VIEWERS_METRIC, "31d-test", "INFO", "", note30))
        # C: historik bakåt för IG
        earliest_ig = _probe_ig_backwards(api_version, accounts, IG_VIEWERS_METRIC)
        rows.append(("instagram", "account", IG_VIEWERS_METRIC, IG_PERIOD,
                     "BACKSTEP", earliest_ig, "första månad med data (bakåt)"))

    _write_probe_report(stamp, api_version, rows, do_fb, do_ig)
    return rows


def _probe_fb_period(api_version, pages, metric, period, since, until):
    got_data = got_empty = 0
    err_msg = None
    for page in pages:
        val, err = fetch_fb_page_metric(api_version, page, metric, period, since, until)
        if err:
            err_msg = err
        elif val is not None:
            got_data += 1
        else:
            got_empty += 1
    if got_data:
        return "OK (data)", f"{got_data}/{len(pages)} sidor gav data"
    if err_msg:
        return "ERROR", err_msg[:160]
    return "OK (tomt)", f"{got_empty}/{len(pages)} tomma"


def _probe_fb_post(api_version, pages, metric, since, until):
    base = GRAPH_BASE_TMPL.format(ver=api_version)
    for page in pages:
        try:
            posts = api_get(f"{base}/{page.page_id}/published_posts",
                            {"fields": "id", "limit": 3, "access_token": page.token}, token=page.token)
        except ApiError:
            continue
        for p in posts.get("data", [])[:1]:
            url = f"{base}/{p['id']}/insights"
            try:
                data = api_get(url, {"metric": metric, "access_token": page.token}, token=page.token)
                if data.get("data"):
                    return "OK (data)", f"post {p['id']} gav data"
                return "OK (tomt)", "anrop OK men tom"
            except ApiError as e:
                return "ERROR", str(e)[:160]
    return "OK (tomt)", "inga poster att testa"


def _probe_fb_backwards(api_version, pages, metric, period):
    earliest = ""
    for back in sorted(PROBE_BACKSTEPS_MONTHS):
        y, m = _shift_month(*last_complete_month(), -back)
        since, until, _, _ = month_bounds_calendar(y, m)
        for page in pages:
            val, err = fetch_fb_page_metric(api_version, page, metric, period, since, until)
            if val is not None:
                earliest = f"{y}-{m:02d}"
                break
    return earliest


def _probe_ig(api_version, accounts, metric, since_ts, until_ts):
    got_data = 0
    err_msg = None
    for acc in accounts:
        val, err = fetch_ig_metric(api_version, acc.ig_id, metric, since_ts, until_ts)
        if err:
            err_msg = err
        elif val is not None:
            got_data += 1
    if got_data:
        return "OK (data)", f"{got_data}/{len(accounts)} konton gav data"
    if err_msg:
        return "ERROR", err_msg[:160]
    return "OK (tomt)", "anrop OK men tomt"


def _probe_ig_30day(api_version, accounts, metric, year, month):
    """Testa 31 dagars fönster — förväntas fel-a om IG-gränsen fortfarande gäller."""
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    since_ts = int(first.timestamp())
    until_ts = since_ts + (31 * 86400)
    for acc in accounts[:1]:
        _, err = fetch_ig_metric(api_version, acc.ig_id, metric, since_ts, until_ts)
        if err:
            return f"31d fel-ar (gräns kvar): {err[:120]}"
        return "31d gav svar (gräns ev. borttagen)"
    return "inga konton att testa"


def _probe_ig_backwards(api_version, accounts, metric):
    earliest = ""
    for back in sorted(PROBE_BACKSTEPS_MONTHS):
        y, m = _shift_month(*last_complete_month(), -back)
        since_ts, until_ts, _, _ = month_bounds_ig_30day(y, m)
        for acc in accounts:
            val, err = fetch_ig_metric(api_version, acc.ig_id, metric, since_ts, until_ts)
            if val is not None:
                earliest = f"{y}-{m:02d}"
                break
    return earliest


def _shift_month(year, month, delta):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _write_probe_report(stamp, api_version, rows, do_fb, do_ig):
    cols = ["platform", "level", "metric", "period", "status", "earliest_data_month", "note"]
    csv_path = os.path.join("probe_results", f"viewers_probe_{stamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)

    md_path = os.path.join("probe_results", f"viewers_probe_{stamp}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Viewers-probe {stamp}\n\n")
        f.write(f"- API_VERSION: `{api_version}`\n")
        f.write(f"- config.py API_VERSION: `{CONFIG_API_VERSION}`\n\n")
        f.write("| platform | level | metric | period | status | earliest | note |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write("| " + " | ".join(str(x) for x in r) + " |\n")
        f.write("\n## Slutrekommendation\n\n")
        f.write(_probe_recommendation(rows, do_fb, do_ig))
    logger.info(f"Probe-rapport skriven: {csv_path} + {md_path}")


def _probe_recommendation(rows, do_fb, do_ig):
    lines = []
    if do_fb:
        fb_ok = [r for r in rows if r[0] == "facebook" and r[1] == "page" and r[4] == "OK (data)"]
        if fb_ok:
            best = fb_ok[0]
            lines.append(f"- **Facebook (sida):** använd `{best[2]}` med `period={best[3]}`.")
        else:
            lines.append("- **Facebook:** INGET kandidatmått gav data — höj API_VERSION (v25.0+) och kör om.")
    if do_ig:
        ig_ok = [r for r in rows if r[0] == "instagram" and r[4] == "OK (data)"]
        if ig_ok:
            best = ig_ok[0]
            lines.append(f"- **Instagram:** använd `{best[2]}` med `period={best[3]}`, 30-dagarsfönster.")
        else:
            lines.append("- **Instagram:** INGET kandidatmått gav data — höj API_VERSION och verifiera scopes.")
    lines.append("\n> Uppdatera konstanterna högst upp i fetch_viewers.py enligt ovan innan produktionskörning.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FAS 1 — PRODUKTION
# ---------------------------------------------------------------------------
FB_MONTH_FIELDS = ["Page", "Page ID", "Reach", "Period_start", "Period_end",
                   "Views_Source", "Status", "Comment"]
FB_WEEK_FIELDS = ["page_id", "page_name", "year", "week", "start_date", "end_date",
                  "Period_start", "Period_end", "reach", "Views_Source", "status", "comment"]
IG_MONTH_FIELDS = ["ig_username", "ig_name", "fb_page_name", "Reach", "Views", "Followers",
                   "Period_start", "Period_end", "Views_Source", "Status", "Comment"]
IG_WEEK_FIELDS = ["ig_username", "ig_name", "fb_page_name", "year", "week", "Reach", "Views",
                  "Period_start", "Period_end", "Views_Source", "Status", "Comment"]


def out_dir(platform, granularity, year, month=None):
    """
    <OUTPUT_ROOT>/Facebook|Instagram/month/<YYYY>  eller  /week/<YYYY_MM>
    """
    plat = "Facebook" if platform == "facebook" else "Instagram"
    if granularity == "month":
        return os.path.join(OUTPUT_ROOT, plat, "month", f"{year}")
    return os.path.join(OUTPUT_ROOT, plat, "week", f"{year}_{month:02d}")


def run_fb_month(api_version, year, month):
    pages = list_fb_pages(api_version)
    since, until, p_start, p_end = month_bounds_calendar(year, month)
    src = viewers_source_tag(FB_VIEWERS_METRIC, api_version)
    path = os.path.join(out_dir("facebook", "month", year), f"FB_{year}_{month:02d}.csv")
    writer = AppendCsv(path, FB_MONTH_FIELDS)
    total = 0
    logger.info(f"[FB månad] {year}-{month:02d}: {len(pages)} sidor → {path}")
    for i, page in enumerate(pages, 1):
        val, err = fetch_fb_page_metric(api_version, page, FB_VIEWERS_METRIC, FB_MONTH_PERIOD, since, until)
        status = "OK" if err is None else "API_ERROR"
        if val is not None:
            total += val
        writer.write({
            "Page": page.name, "Page ID": page.page_id,
            "Reach": val if val is not None else "",
            "Period_start": p_start, "Period_end": p_end,
            "Views_Source": src, "Status": status, "Comment": err or "",
        })
        logger.info(f"  [{i}/{len(pages)}] {page.name}: {val if val is not None else '—'}")
    writer.close()
    logger.info(f"[FB månad] Sparad till {path}. Total viewers: {total:,}")


def run_fb_week(api_version, iso_year, iso_week):
    pages = list_fb_pages(api_version)
    monday, sunday = iso_week_bounds(iso_year, iso_week)
    src = viewers_source_tag(FB_VIEWERS_METRIC, api_version)
    path = os.path.join(out_dir("facebook", "week", monday.year, monday.month),
                        f"week_{iso_week:02d}.csv")
    writer = AppendCsv(path, FB_WEEK_FIELDS)
    logger.info(f"[FB vecka] {iso_year}-W{iso_week:02d} ({monday}–{sunday}): {len(pages)} sidor → {path}")
    for i, page in enumerate(pages, 1):
        val, err = fetch_fb_page_metric(api_version, page, FB_VIEWERS_METRIC, FB_WEEK_PERIOD,
                                        monday.isoformat(), sunday.isoformat())
        status = "OK" if err is None else "ERROR"
        writer.write({
            "page_id": page.page_id, "page_name": page.name,
            "year": iso_year, "week": iso_week,
            "start_date": monday.isoformat(), "end_date": sunday.isoformat(),
            "Period_start": monday.isoformat(), "Period_end": sunday.isoformat(),
            "reach": val if val is not None else "",
            "Views_Source": src, "status": status, "comment": err or "",
        })
        logger.info(f"  [{i}/{len(pages)}] {page.name}: {val if val is not None else '—'}")
    writer.close()
    logger.info(f"[FB vecka] Sparad till {path}.")


def run_ig_month(api_version, year, month):
    accounts = list_ig_accounts(api_version)
    since_ts, until_ts, p_start, p_end = month_bounds_ig_30day(year, month)
    src = viewers_source_tag(IG_VIEWERS_METRIC, api_version)
    path = os.path.join(out_dir("instagram", "month", year), f"IG_{year}_{month:02d}.csv")
    writer = AppendCsv(path, IG_MONTH_FIELDS)
    total_r = total_v = 0
    logger.info(f"[IG månad] {year}-{month:02d}: {len(accounts)} konton → {path}")
    for i, acc in enumerate(accounts, 1):
        reach, err_r = fetch_ig_metric(api_version, acc.ig_id, IG_VIEWERS_METRIC, since_ts, until_ts)
        views, err_v = fetch_ig_metric(api_version, acc.ig_id, IG_SECONDARY_METRIC, since_ts, until_ts)
        followers = fetch_ig_followers(api_version, acc.ig_id)
        errs = [e for e in (err_r, err_v) if e]
        status = "OK" if not errs else "API_ERROR"
        if reach:
            total_r += reach
        if views:
            total_v += views
        writer.write({
            "ig_username": acc.ig_username, "ig_name": acc.ig_name, "fb_page_name": acc.fb_page_name,
            "Reach": reach if reach is not None else "", "Views": views if views is not None else "",
            "Followers": followers, "Period_start": p_start, "Period_end": p_end,
            "Views_Source": src, "Status": status, "Comment": "; ".join(errs[:3]),
        })
        logger.info(f"  [{i}/{len(accounts)}] @{acc.ig_username}: reach={reach}, views={views}")
    writer.close()
    logger.info(f"[IG månad] Sparad till {path}. reach={total_r:,}, views={total_v:,}")
    logger.info("OBS: IG reach = enbart organisk; viewers ≠ gammal FB-reach (definitionsbrott).")


def run_ig_week(api_version, iso_year, iso_week):
    """Net-new: ingen IG-vecka fanns tidigare. 30-dagarsgränsen gäller inte veckofönster."""
    accounts = list_ig_accounts(api_version)
    monday, sunday = iso_week_bounds(iso_year, iso_week)
    since_ts = int(datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc).timestamp())
    until_ts = since_ts + (7 * 86400)
    src = viewers_source_tag(IG_VIEWERS_METRIC, api_version)
    path = os.path.join(out_dir("instagram", "week", monday.year, monday.month),
                        f"week_{iso_week:02d}.csv")
    writer = AppendCsv(path, IG_WEEK_FIELDS)
    logger.info(f"[IG vecka] {iso_year}-W{iso_week:02d} ({monday}–{sunday}): {len(accounts)} konton → {path}")
    for i, acc in enumerate(accounts, 1):
        reach, err_r = fetch_ig_metric(api_version, acc.ig_id, IG_VIEWERS_METRIC, since_ts, until_ts)
        views, err_v = fetch_ig_metric(api_version, acc.ig_id, IG_SECONDARY_METRIC, since_ts, until_ts)
        errs = [e for e in (err_r, err_v) if e]
        writer.write({
            "ig_username": acc.ig_username, "ig_name": acc.ig_name, "fb_page_name": acc.fb_page_name,
            "year": iso_year, "week": iso_week,
            "Reach": reach if reach is not None else "", "Views": views if views is not None else "",
            "Period_start": monday.isoformat(), "Period_end": sunday.isoformat(),
            "Views_Source": src, "Status": "OK" if not errs else "API_ERROR",
            "Comment": "; ".join(errs[:3]),
        })
        logger.info(f"  [{i}/{len(accounts)}] @{acc.ig_username}: reach={reach}, views={views}")
    writer.close()
    logger.info(f"[IG vecka] Sparad till {path}.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="Konsoliderad Viewers/Media-Views-insamling för Facebook + Instagram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exempel:\n"
            "  python fetch_viewers.py --probe --facebook --instagram --sample 3\n"
            "  python fetch_viewers.py --facebook --month\n"
            "  python fetch_viewers.py --instagram --week\n"
            "  python fetch_viewers.py --facebook --month --year-month 2026-05\n"
            "  python fetch_viewers.py --instagram --week --iso-week 2026-W23\n"
        ),
    )
    p.add_argument("--facebook", action="store_true", help="Inkludera Facebook.")
    p.add_argument("--instagram", action="store_true", help="Inkludera Instagram.")
    p.add_argument("--month", action="store_true", help="Månadsgranularitet.")
    p.add_argument("--week", action="store_true", help="Veckogranularitet.")
    p.add_argument("--probe", action="store_true", help="Fas 0: sondera (skriver bara probe_results/).")
    p.add_argument("--sample", type=int, default=3, help="Antal sidor/konton i probe (default 3).")
    p.add_argument("--year-month", dest="year_month", help="Målmånad YYYY-MM (annars senast avslutade).")
    p.add_argument("--iso-week", dest="iso_week", help="Målvecka YYYY-Www (annars senast avslutade).")
    p.add_argument("--api-version", dest="api_version",
                   help="Override av Graph API-version (default = config.py). Använd för att probe:a v25.0+.")
    p.add_argument("--debug", action="store_true", help="Debug-loggning.")
    return p


def main():
    args = build_parser().parse_args()
    setup_logging(args.debug)

    if not ACCESS_TOKEN:
        logger.error("ACCESS_TOKEN saknas i config.py.")
        sys.exit(1)

    api_version = _api_version(args.api_version)
    if api_version != CONFIG_API_VERSION:
        logger.info(f"Använder API-version {api_version} (override; config.py={CONFIG_API_VERSION}).")

    if not (args.facebook or args.instagram):
        logger.error("Ange minst en plattform: --facebook och/eller --instagram.")
        build_parser().print_help()
        sys.exit(2)

    check_token_expiry()

    # --- FAS 0 ---
    if args.probe:
        probe(api_version, args.facebook, args.instagram, args.sample)
        logger.info("Probe klar. Granska probe_results/ och uppdatera metric-/period-konstanterna. "
                    "Ingen produktionsinsamling har körts.")
        return

    # --- FAS 1 ---
    if not (args.month or args.week):
        logger.error("Ange minst en granularitet: --month och/eller --week.")
        build_parser().print_help()
        sys.exit(2)

    # Målperiod
    ym = None
    if args.year_month:
        y, m = map(int, args.year_month.split("-"))
        ym = (y, m)
    iso = None
    if args.iso_week:
        iy, iw = args.iso_week.split("-W")
        iso = (int(iy), int(iw))

    if args.month:
        y, m = ym if ym else last_complete_month()
        if args.facebook:
            run_fb_month(api_version, y, m)
        if args.instagram:
            run_ig_month(api_version, y, m)

    if args.week:
        if iso:
            iy, iw = iso
        else:
            iy, iw, _, _ = last_complete_iso_week()
        if args.facebook:
            run_fb_week(api_version, iy, iw)
        if args.instagram:
            run_ig_week(api_version, iy, iw)

    logger.info("Klar.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("Avbruten av användare.")
        sys.exit(130)
    except Exception:
        logger.error("Oväntat fel:\n" + traceback.format_exc())
        sys.exit(1)
