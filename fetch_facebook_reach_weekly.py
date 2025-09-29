#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facebook Weekly Reach & Interactions Report Generator – v1.3

Syfte:
  - Hämta veckovisa räckviddssiffror (viktigast) och interaktioner på kontonivå (Facebook-sidor) för en given månad
  - Skriver en CSV per vecka + valfri sammanfogad månad-CSV

Viktigt:
  - Använder Page Insights med period=week (inte total_over_range)
  - Tar ENDAST metriker som fortfarande fungerar 2025-09: 
      * page_impressions_unique (räckvidd – viktigast)
      * page_post_engagements   (interaktioner som proxy)
  - Tidzon: Graph API använder UTC i end_time. Vi matchar end_date eller end_date+1 dag.

Inmatning:
  - ACCESS TOKEN via env META_ACCESS_TOKEN (page- eller user-token med läsrätt på sidor)

Körningsexempel:
  python3 fetch_facebook_reach_weekly.py --month 2025-05

Krav: requests
"""
from __future__ import annotations
import os
import sys
import csv
import time
import json
import math
import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

# ========================= Konfig =========================
# Läs API-version: config.API_VERSION > env FB_API_VERSION > v20.0
try:
    from config import API_VERSION as CONFIG_API_VERSION  # type: ignore
    _CFG_API_VER = CONFIG_API_VERSION or ""
except Exception:
    _CFG_API_VER = ""
API_VERSION = _CFG_API_VER or os.getenv("FB_API_VERSION", "v20.0")
GRAPH_BASE = f"https://graph.facebook.com/{API_VERSION}"
# Försök först läsa token från config.py, annars fall tillbaka till env
try:
    from config import ACCESS_TOKEN as CONFIG_ACCESS_TOKEN  # type: ignore
    _CFG_TOKEN = CONFIG_ACCESS_TOKEN or ""
except Exception:
    _CFG_TOKEN = ""

ACCESS_TOKEN = _CFG_TOKEN or os.getenv("META_ACCESS_TOKEN")
TOKEN_SOURCE = "config.py" if _CFG_TOKEN else ("env META_ACCESS_TOKEN" if os.getenv("META_ACCESS_TOKEN") else "MISSING")

# Kataloger
OUT_ROOT = os.getenv("OUT_DIR", "weekly_reports")
LOG_DIR = os.getenv("LOG_DIR", "logs")

# Metriker (bara fungerande per 2025-09)
PAGE_METRICS = [
    "page_impressions_unique",   # UNIK RÄCKVIDD – huvudmått
    "page_post_engagements",     # Interaktioner (proxy när andra är borttagna)
]

# ======================== Logging =========================
logger = logging.getLogger("fb_weekly")
logger.setLevel(logging.INFO)
logger.propagate = False

if not os.path.isdir(LOG_DIR):
    os.makedirs(LOG_DIR, exist_ok=True)

fh: Optional[logging.FileHandler] = None
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(ch)


def start_file_logging():
    global fh
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(LOG_DIR, f"facebook_reach_weekly_{ts}.log")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(fh)
    logger.info(f"Startar loggning till fil: {path}")


# =================== Hjälpdatatyper =======================
@dataclass
class Page:
    id: str
    name: str
    access_token: str = ""  # Page Access Token (krävs för /{page-id}/insights)


@dataclass
class MetricResult:
    reach: int = 0
    engagements: int = 0
    status: str = "OK"  # OK | NO_ACTIVITY | ERROR | NO_DATA
    comment: str = ""


# =================== Datum/veckohjälp =====================

def monday_on_or_after(d: date) -> date:
    # 0=måndag ... 6=söndag
    return d if d.weekday() == 0 else d + timedelta(days=(7 - d.weekday()))


def sunday_of_week(monday: date) -> date:
    return monday + timedelta(days=6)


def iter_weeks_in_month(year: int, month: int) -> List[Tuple[int, date, date]]:
    """Returnera (week_num, start=måndag, slut=söndag) för alla veckor som BÖRJAR i månaden."""
    first = date(year, month, 1)
    # första måndag i/efter månadens första dag
    start = monday_on_or_after(first)
    weeks: List[Tuple[int, date, date]] = []
    while start.month == month:
        end = sunday_of_week(start)
        week_num = start.isocalendar()[1]
        weeks.append((week_num, start, end))
        start = start + timedelta(days=7)
    return weeks


# =================== HTTP/Graph utils =====================
class ApiError(Exception):
    pass


def api_get(url: str, params: Dict[str, str], *, timeout: int = 30) -> Dict:
    # Token i params
    p = dict(params)
    # Enkel retry för nätfel och 5xx
    backoffs = [0.5, 1.0, 2.0, 4.0]
    last_exc: Optional[Exception] = None
    for attempt, bo in enumerate([0.0, *backoffs], start=1):
        try:
            if bo:
                time.sleep(bo)
            resp = requests.get(url, params=p, timeout=timeout)
            if resp.status_code == 429:
                # Rate limit – respektera Retry-After
                ra = resp.headers.get("Retry-After")
                wait_s = 0.0
                if ra:
                    try:
                        wait_s = min(float(ra), 120.0)
                    except Exception:
                        wait_s = 5.0
                else:
                    wait_s = 5.0
                logger.warning(f"429 Rate limited. Väntar {wait_s:.1f}s…")
                time.sleep(wait_s)
                continue
            if 500 <= resp.status_code < 600:
                logger.warning(f"Serverfel {resp.status_code}. Försök {attempt}…")
                continue
            if resp.status_code != 200:
                # 4xx – kasta direkt med text (inkl OAuthException)
                raise ApiError(f"HTTP {resp.status_code} - {resp.text}")
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            logger.warning(f"Nätverksfel: {e} (försök {attempt})")
            continue
    if last_exc:
        raise ApiError(str(last_exc))
    raise ApiError("Okänt fel i api_get")


# =================== Graph-funktioner =====================

def validate_token() -> Dict:
    url = f"{GRAPH_BASE}/me"
    params = {"access_token": ACCESS_TOKEN, "fields": "name,id"}
    return api_get(url, params)


def list_pages(limit: int = 500) -> List[Page]:
    pages: List[Page] = []
    url = f"{GRAPH_BASE}/me/accounts"
    # Begär endast id,name här (stabilare); hämta page tokens per sida separat
    params = {"access_token": ACCESS_TOKEN, "fields": "id,name", "limit": str(limit)}
    while True:
        data = api_get(url, params)
        for it in data.get("data", []):
            pages.append(Page(
                id=str(it.get("id")),
                name=it.get("name") or "",
                access_token=""  # tokens hämtas separat
            ))
        paging = data.get("paging", {})
        next_url = paging.get("next")
        if not next_url:
            break
        url = next_url
        params = {}
    return pages


def get_page_token(page_id: str) -> str:
    """Hämta Page Access Token för en enskild sida via /{page-id}?fields=access_token."""
    url = f"{GRAPH_BASE}/{page_id}"
    params = {"access_token": ACCESS_TOKEN, "fields": "access_token"}
    try:
        data = api_get(url, params)
        tok = data.get("access_token") or ""
        if not tok:
            logger.warning(f"Ingen page-token i svar för sida {page_id}")
        return tok
    except ApiError as e:
        logger.warning(f"Kunde inte hämta page-token för sida {page_id}: {e}")
        return ""


def pick_week_value(values: List[Dict], end_date: date) -> int:
    if not values:
        return 0
    end_date_str = end_date.strftime("%Y-%m-%d")
    day_after_str = (end_date + timedelta(days=1)).strftime("%Y-%m-%d")

    def _coerce(x):
        if isinstance(x, dict):
            return int(sum((int(v or 0) for v in x.values())))
        try:
            return int(x or 0)
        except Exception:
            return 0

    # primär match: exakt slutdag
    for v in values:
        et = (v.get("end_time") or "")[:10]
        if et == end_date_str:
            return _coerce(v.get("value"))
    # sekundär: dagen efter
    for v in values:
        et = (v.get("end_time") or "")[:10]
        if et == day_after_str:
            return _coerce(v.get("value"))
    # fallback: sista datapunkt
    last = values[-1]
    return _coerce(last.get("value"))


def get_page_metrics(page_id: str, start_date: date, end_date: date, token: str) -> MetricResult:
    url = f"{GRAPH_BASE}/{page_id}/insights"
    params = {
        "access_token": token,
        "metric": ",".join(PAGE_METRICS),
        "period": "week",
        "since": start_date.isoformat(),
        "until": end_date.isoformat(),
    }
    try:
        data = api_get(url, params)
    except ApiError as e:
        logger.error(f"HTTP-fel: {e}")
        return MetricResult(status="ERROR", comment=str(e))

    metrics_map = {m: 0 for m in PAGE_METRICS}
    arr = data.get("data", [])
    if not arr:
        return MetricResult(status="NO_DATA", comment="Inga data i svar")

    for m in arr:
        name = m.get("name")
        values = m.get("values", [])
        val = pick_week_value(values, end_date)
        if name in metrics_map:
            metrics_map[name] = val

    reach = int(metrics_map.get("page_impressions_unique", 0))
    engagements = int(metrics_map.get("page_post_engagements", 0))

    status = "OK"
    comment = ""
    if reach == 0 and engagements == 0:
        status = "NO_ACTIVITY"
        comment = "Inga aktiviteter under perioden"

    return MetricResult(reach=reach, engagements=engagements, status=status, comment=comment)


# =================== CSV-utskrift =========================

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_week_csv(out_dir: str, week_num: int, rows: List[Dict[str, str]]) -> str:
    ensure_dir(out_dir)
    fname = os.path.join(out_dir, f"week_{week_num}.csv")
    fieldnames = [
        "page_id", "page_name", "year", "week", "start_date", "end_date",
        "reach", "engagements", "status", "comment",
    ]
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return fname


def combine_month_csv(out_dir: str) -> Optional[str]:
    if not os.path.isdir(out_dir):
        return None
    files = [os.path.join(out_dir, fn) for fn in os.listdir(out_dir) if fn.startswith("week_") and fn.endswith(".csv")]
    if not files:
        return None
    files.sort()
    combined = os.path.join(out_dir, "combined.csv")

    header_written = False
    with open(combined, "w", newline="", encoding="utf-8") as out:
        writer = None
        for fp in files:
            with open(fp, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = list(reader)
                if not rows:
                    continue
                if not header_written:
                    writer = csv.writer(out)
                    writer.writerow(rows[0])
                    header_written = True
                for row in rows[1:]:
                    writer.writerow(row)
    return combined


# =================== Körlogik =============================

def process_week(year: int, week_num: int, start_date: date, end_date: date, pages: List[Page], out_dir: str) -> bool:
    logger.info(f"--- Vecka {week_num}: {start_date} till {end_date} ---")
    rows: List[Dict[str, str]] = []
    token_cache: Dict[str, str] = {}
    for p in pages:
        page_token = p.access_token
        if not page_token:
            page_token = token_cache.get(p.id) or get_page_token(p.id)
            token_cache[p.id] = page_token
        if not page_token:
            logger.error(f"Saknar Page Access Token för sida {p.id} ({p.name}) – hoppar över")
            mr = MetricResult(status="ERROR", comment="Missing page access token")
        else:
            mr = get_page_metrics(p.id, start_date, end_date, page_token)
        if mr.status == "ERROR":
            logger.warning(f"Ingen data för sida {p.id} ({p.name}) – {mr.comment}")
        elif mr.status in ("NO_DATA", "NO_ACTIVITY"):
            logger.warning(f"Ingen/Ingen aktivitet för sida {p.id} ({p.name})")
        rows.append({
            "page_id": p.id,
            "page_name": p.name,
            "year": str(year),
            "week": str(week_num),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "reach": str(mr.reach),
            "engagements": str(mr.engagements),
            "status": mr.status,
            "comment": mr.comment,
        })
    if not rows:
        return False
    out_file = write_week_csv(out_dir, week_num, rows)
    logger.info(f"Skrev {out_file}")
    return True


def process_month(year: int, month: int, pages: List[Page]) -> None:
    ym_dir = os.path.join(OUT_ROOT, f"{year:04d}_{month:02d}")
    weeks = iter_weeks_in_month(year, month)
    logger.info("======================================================================")
    logger.info(f"Bearbetar veckor för {year}-{month:02d}")
    logger.info("======================================================================")
    logger.info(f"Hittade {len(weeks)} veckor som börjar i {year}-{month:02d}")
    for (w, s, e) in weeks:
        process_week(year, w, s, e, pages, ym_dir)
    combined = combine_month_csv(ym_dir)
    if combined:
        logger.info(f"Skrev sammanfogad månad: {combined}")


# =================== CLI / main ===========================

def parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Facebook Weekly Reach & Interactions Report Generator – v1.3")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--month", help="Månad i format YYYY-MM (t.ex. 2025-05)")
    g.add_argument("--week", help="Vecka i ISO-format YYYY-Www (t.ex. 2025-W19)")
    ap.add_argument("--pages-json", help="Valfri JSON-fil med [{'id':..., 'name':...}, ...] för att begränsa sidor", default=None)
    ap.add_argument("--no-combine", help="Skippa sammanfogad combined.csv", action="store_true")
    return ap.parse_args(argv)


def main(argv: List[str]) -> None:
    if not ACCESS_TOKEN:
        print("Saknar ACCESS TOKEN. Sätt env META_ACCESS_TOKEN.", file=sys.stderr)
        sys.exit(2)

    start_file_logging()
    logger.info("Facebook Weekly Reach & Interactions Report Generator - v1.3")
    logger.info("Startdatum: 2025-01")
    logger.info("Veckorapporter sparas i: weekly_reports/YYYY_MM/")
    logger.info("-------------------------------------------------------------------")
    logger.info(f"Token-källa: {TOKEN_SOURCE}")

    # Token-info (ej exakt ålder, men vi testar att den är giltig)
    logger.info("Validerar access token...")
    me = validate_token()
    logger.info(f"[OK] Token validerad för användare: {me.get('name')}, ID: {me.get('id')}")

    args = parse_args(argv)

    # Hämta sidor
    if args.pages_json and os.path.isfile(args.pages_json):
        with open(args.pages_json, "r", encoding="utf-8") as f:
            arr = json.load(f)
        pages = [Page(id=str(x["id"]), name=x.get("name", "")) for x in arr]
    else:
        logger.info("Hämtar tillgängliga sidor...")
        pages = list_pages()
        logger.info(f"[OK] Hittade {len(pages)} sidor")

    # Körning
    if args.month:
        y, m = map(int, args.month.split("-"))
        logger.info(f"Kör endast för månad: {y}-{m:02d}")
        process_month(y, m, pages)
    else:
        # En enskild vecka
        # Format: YYYY-Www (ISO vecka)
        try:
            y, w = args.week.split("-W")
            y = int(y)
            w = int(w)
        except Exception:
            raise SystemExit("Ogiltigt --week. Använd t.ex. 2025-W19")
        # ISO: vecka w, måndag = isocalendar
        # hitta måndagen i veckan
        # metod: starta vid 4 jan (garanterat vecka 1) och gå till önskad vecka
        # enklare: ta första torsdag i året och bygg datum – men Python 3.8+ har fromisocalendar
        start = date.fromisocalendar(y, w, 1)
        end = start + timedelta(days=6)
        out_dir = os.path.join(OUT_ROOT, f"{start.year:04d}_{start.month:02d}")
        process_week(start.year, w, start, end, pages, out_dir)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        logger.warning("Avbruten av användaren (Ctrl+C)")
        sys.exit(130)
