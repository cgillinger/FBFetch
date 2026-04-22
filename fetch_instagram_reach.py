# fetch_instagram_reach.py
# Hämtar månatlig unik reach för alla Instagram Business-konton via Meta Graph API.

import csv
import os
import sys
import time
import logging
import argparse
import requests
import urllib.parse
from datetime import datetime, timezone
from calendar import monthrange
from dataclasses import dataclass

from config import (
    ACCESS_TOKEN, TOKEN_LAST_UPDATED, INITIAL_START_YEAR_MONTH,
    API_VERSION, MAX_RETRIES, RETRY_DELAY, TOKEN_VALID_DAYS,
)

REQUIRED_SCOPES = {
    "instagram_basic",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
}

# ─── Loggning ─────────────────────────────────────────────────────────────────

def setup_logging(debug: bool = False) -> logging.Logger:
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"instagram_reach_{ts}.log")
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    lg = logging.getLogger(__name__)
    lg.info(f"Startar loggning till: {log_file}")
    return lg

logger = logging.getLogger(__name__)

# ─── Rate limit-tillstånd ─────────────────────────────────────────────────────

_api_call_count = 0
_last_rate_limit_time = None
_rate_limit_backoff = 1.0
_consecutive_successes = 0

# ─── API-hjälpfunktioner ──────────────────────────────────────────────────────

def _unpack_next_url(next_url):
    """Extrahera access_token från pagineringslänk och returnera (clean_url, params)."""
    parsed = urllib.parse.urlparse(next_url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    token_list = qs.pop("access_token", [])
    token = token_list[0] if token_list else None
    clean_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(qs, doseq=True)))
    return clean_url, ({"access_token": token} if token else {})


def api_get(url, params, retries=MAX_RETRIES):
    """GET med Bearer-autentisering, retry och dynamisk rate limit-backoff."""
    global _api_call_count, _last_rate_limit_time, _rate_limit_backoff, _consecutive_successes

    if _last_rate_limit_time:
        elapsed = time.time() - _last_rate_limit_time
        wait = 60 * _rate_limit_backoff - elapsed
        if wait > 0:
            logger.info(f"Väntar {wait:.1f}s efter rate limit (backoff {_rate_limit_backoff:.1f}x)")
            time.sleep(wait)

    safe = dict(params)
    token = safe.pop("access_token", None)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    for attempt in range(retries):
        try:
            _api_call_count += 1
            r = requests.get(url, params=safe, headers=headers, timeout=30)
        except requests.RequestException as e:
            logger.error(f"Nätverksfel: {e}")
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
            continue

        if r.status_code == 429:
            _last_rate_limit_time = time.time()
            _rate_limit_backoff = min(_rate_limit_backoff * 1.5, 10.0)
            wait = int(r.headers.get("Retry-After", 60 * _rate_limit_backoff))
            logger.warning(f"Rate limit! Väntar {wait}s")
            time.sleep(wait)
            continue

        if r.status_code >= 500:
            wait = min(RETRY_DELAY * (2 ** attempt), 30)
            logger.warning(f"Serverfel {r.status_code}, väntar {wait}s")
            time.sleep(wait)
            continue

        try:
            data = r.json()
        except Exception:
            logger.error(f"JSON-parsningsfel: {r.text[:100]}")
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
            continue

        if r.status_code == 400 and "error" in data:
            code = data["error"].get("code")
            msg = data["error"].get("message", "")
            if code == 4:
                _last_rate_limit_time = time.time()
                _rate_limit_backoff = min(_rate_limit_backoff * 1.5, 10.0)
                wait = min(60 * _rate_limit_backoff, 300)
                logger.warning(f"App rate limit: {msg}. Väntar {wait}s")
                time.sleep(wait)
                continue
            if code == 190:
                logger.error(f"Token ogiltig: {msg}")
                return None

        _consecutive_successes += 1
        if _consecutive_successes >= 50 and _rate_limit_backoff > 1.0:
            _rate_limit_backoff = max(_rate_limit_backoff * 0.8, 1.0)
            _consecutive_successes = 0

        return data

    return None

# ─── Token-validering ─────────────────────────────────────────────────────────

def check_token_expiry():
    try:
        last = datetime.strptime(TOKEN_LAST_UPDATED, "%Y-%m-%d")
        days_since = (datetime.now() - last).days
        days_left = TOKEN_VALID_DAYS - days_since
        logger.info(f"Token skapades för {days_since} dagar sedan ({days_left} dagar kvar).")
        if days_left <= 0:
            logger.error("KRITISKT: Token har gått ut! Skapa ny token omedelbart.")
            sys.exit(1)
        if days_left <= 7:
            logger.warning(f"VARNING: Token går ut inom {days_left} dagar!")
    except Exception as e:
        logger.error(f"Kunde inte tolka TOKEN_LAST_UPDATED: {e}")


def validate_token_scopes(token):
    """Validera token och kontrollera att nödvändiga scopes finns."""
    logger.info("Validerar token och scopes...")
    url = f"https://graph.facebook.com/{API_VERSION}/debug_token"
    data = api_get(url, {"input_token": token, "access_token": token})

    if not data or "data" not in data:
        logger.error("Kunde inte validera token.")
        sys.exit(1)

    info = data["data"]
    if not info.get("is_valid"):
        msg = info.get("error", {}).get("message", "Okänd anledning")
        logger.error(f"Token ogiltig: {msg}")
        sys.exit(1)

    granted = set(info.get("scopes", []))
    missing = REQUIRED_SCOPES - granted
    if missing:
        print(f"❌ Token saknar följande behörigheter: {', '.join(sorted(missing))}")
        print("   Generera ny token i Business Manager med dessa scopes tillagda.")
        sys.exit(1)

    logger.info(f"Token validerad. App ID: {info.get('app_id')}, scopes OK.")

# ─── Instagram-konton ─────────────────────────────────────────────────────────

@dataclass
class IGAccount:
    ig_id: str
    ig_username: str
    ig_name: str
    fb_page_name: str


def get_instagram_accounts(token):
    """Hämta IG Business-konton via me/accounts."""
    logger.info("Hämtar Instagram-konton via me/accounts...")
    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {"access_token": token, "limit": 100, "fields": "id,name,instagram_business_account"}

    accounts = []

    while True:
        data = api_get(url, params)
        if not data or "data" not in data:
            break

        for page in data["data"]:
            page_name = page.get("name", "")
            # Srholder-filter borttaget: dessa sidor är bryggan till riktiga IG-konton
            # och ska inte filtreras här (filtret finns kvar i FB-skriptet där det hör hemma).

            ig_data = page.get("instagram_business_account")
            if not ig_data:
                continue

            ig_id = ig_data["id"]
            ig_info = api_get(
                f"https://graph.facebook.com/{API_VERSION}/{ig_id}",
                {"fields": "username,name", "access_token": token},
            )
            if not ig_info or "error" in ig_info:
                continue

            accounts.append(IGAccount(
                ig_id=ig_id,
                ig_username=ig_info.get("username", f"ig_{ig_id}"),
                ig_name=ig_info.get("name", ""),
                fb_page_name=page_name,
            ))

        next_url = data.get("paging", {}).get("next")
        if next_url:
            url, params = _unpack_next_url(next_url)
        else:
            break

    logger.info(f"Hittade {len(accounts)} Instagram-konton.")
    return accounts

# ═══════════════════════════════════════════════════════════════
# HÄR SLUTAR DEL ETT
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# HÄR BÖRJAR DEL TVÅ
# ═══════════════════════════════════════════════════════════════

# ─── Metrik-hämtning ──────────────────────────────────────────────────────────

def get_month_boundaries(year, month):
    """Returnera (since_ts, until_ts, period_start_iso, period_end_iso).

    Använder alltid exakt 30 dagar (2 592 000 s) för att undvika
    Instagram API:s gräns på max 30 dagar mellan since och until.
    Period_end är sista dagen med data (dagen före until).
    """
    first_day = datetime(year, month, 1, tzinfo=timezone.utc)
    since_ts = int(first_day.timestamp())
    until_ts = since_ts + (30 * 86400)
    period_start = first_day.strftime("%Y-%m-%d")
    period_end = datetime.fromtimestamp(until_ts - 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    return since_ts, until_ts, period_start, period_end


def fetch_insight(ig_id, metric, since, until, token, account_name=None):
    """Hämta ett aggregerat insights-värde via metric_type=total_value."""
    label = f"@{account_name}" if account_name else ig_id
    url = f"https://graph.facebook.com/{API_VERSION}/{ig_id}/insights"
    params = {
        "metric": metric,
        "period": "day",
        "metric_type": "total_value",
        "since": since,
        "until": until,
        "access_token": token,
    }
    data = api_get(url, params)
    if data and "error" in data:
        msg = data["error"].get("message", "Okänt fel")
        logger.warning(f"⚠️  {metric.capitalize()}-fel för {label}: {msg}")
        return 0
    if not data or "data" not in data or not data["data"]:
        return 0
    try:
        return int(data["data"][0]["total_value"]["value"])
    except (KeyError, TypeError, ValueError):
        return 0


def fetch_followers(ig_id, token):
    data = api_get(
        f"https://graph.facebook.com/{API_VERSION}/{ig_id}",
        {"fields": "followers_count", "access_token": token},
    )
    return int(data.get("followers_count", 0)) if data else 0


def fetch_publications(ig_id, token, year, month):
    """Räkna poster vars timestamp faller inom månaden, paginerar tills poster är äldre."""
    month_start = datetime(year, month, 1, tzinfo=timezone.utc)
    month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12 \
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)

    url = f"https://graph.facebook.com/{API_VERSION}/{ig_id}/media"
    params = {"fields": "id,timestamp", "limit": 100, "access_token": token}
    count = 0
    page_num = 0

    while url and page_num < 100:
        page_num += 1
        data = api_get(url, params)
        if not data or "data" not in data:
            break

        oldest_in_page = None
        for post in data["data"]:
            ts_str = post.get("timestamp", "")
            if not ts_str:
                continue
            try:
                post_dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
            except Exception:
                continue
            if oldest_in_page is None or post_dt < oldest_in_page:
                oldest_in_page = post_dt
            if month_start <= post_dt < month_end:
                count += 1

        if oldest_in_page and oldest_in_page < month_start:
            break

        next_url = data.get("paging", {}).get("next")
        if next_url:
            url, params = _unpack_next_url(next_url)
        else:
            break

    return count


def fetch_account_metrics(account, token, year, month):
    since, until, period_start, period_end = get_month_boundaries(year, month)
    result = {
        "ig_username": account.ig_username,
        "ig_name": account.ig_name,
        "fb_page_name": account.fb_page_name,
        "Reach": 0, "Views": 0, "Followers": 0, "Publications": 0,
        "Period_start": period_start, "Period_end": period_end,
        "Status": "OK", "Comment": "",
    }
    errors = []

    for metric_key, fetch_fn in [
        ("Reach", lambda: fetch_insight(account.ig_id, "reach", since, until, token, account.ig_username)),
        ("Views", lambda: fetch_insight(account.ig_id, "views", since, until, token, account.ig_username)),
        ("Followers", lambda: fetch_followers(account.ig_id, token)),
        ("Publications", lambda: fetch_publications(account.ig_id, token, year, month)),
    ]:
        try:
            result[metric_key] = fetch_fn()
        except Exception as e:
            errors.append(f"{metric_key.lower()}: {e}")

    if errors:
        result["Status"] = "API_ERROR"
        result["Comment"] = "; ".join(errors[:3])

    return result

# ─── CSV och kataloger ────────────────────────────────────────────────────────

FIELDNAMES = ["ig_username", "ig_name", "fb_page_name", "Reach", "Views",
              "Followers", "Publications", "Period_start", "Period_end", "Status", "Comment"]


def output_path(year, month):
    dir_name = f"IGReach{year}"
    os.makedirs(dir_name, exist_ok=True)
    return os.path.join(dir_name, f"IG_{year}_{month:02d}.csv")


def save_csv(rows, path):
    sorted_rows = sorted(rows, key=lambda r: r.get("Reach", 0), reverse=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(sorted_rows)
    os.replace(tmp, path)
    logger.info(f"Sparade {len(sorted_rows)} konton → {path}")

# ─── Månadsbearbetning ────────────────────────────────────────────────────────

def process_month(accounts, token, year, month, force):
    path = output_path(year, month)
    if os.path.exists(path) and not force:
        logger.info(f"Hoppar över {year}-{month:02d} (fil finns, använd --force för att skriva över)")
        return True

    logger.info(f"Bearbetar {year}-{month:02d} ({len(accounts)} konton)...")
    rows = []
    for i, account in enumerate(accounts, 1):
        logger.info(f"  [{i}/{len(accounts)}] @{account.ig_username}")
        rows.append(fetch_account_metrics(account, token, year, month))

    if rows:
        save_csv(rows, path)
        total_reach = sum(r["Reach"] for r in rows)
        total_views = sum(r["Views"] for r in rows)
        logger.info(f"Summering {year}-{month:02d}: reach={total_reach:,}, views={total_views:,}")

    return bool(rows)


def months_to_process(start_ym):
    start_year, start_month = map(int, start_ym.split("-"))
    now = datetime.now()
    months = []
    y, m = start_year, start_month
    while (y, m) < (now.year, now.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hämtar månatlig Instagram reach för alla IG Business-konton"
    )
    parser.add_argument("--month", help="Kör för angiven månad (YYYY-MM)")
    parser.add_argument("--start", help="Överrid startdatum (YYYY-MM)")
    parser.add_argument("--force", action="store_true", help="Skriv över befintliga filer")
    parser.add_argument("--debug", action="store_true", help="Debug-loggning")
    args = parser.parse_args()

    global logger
    logger = setup_logging(debug=args.debug)

    logger.info("Instagram Reach Report – startar")
    check_token_expiry()
    validate_token_scopes(ACCESS_TOKEN)

    accounts = get_instagram_accounts(ACCESS_TOKEN)
    if not accounts:
        logger.error("Inga Instagram-konton hittades. Avbryter.")
        sys.exit(1)

    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            logger.error(f"Ogiltigt månadsformat: {args.month}. Använd YYYY-MM.")
            sys.exit(1)
        process_month(accounts, ACCESS_TOKEN, year, month, args.force)
    else:
        start_ym = args.start or INITIAL_START_YEAR_MONTH
        target_months = months_to_process(start_ym)
        if not target_months:
            logger.info("Inga månader att bearbeta.")
            return
        logger.info(f"Bearbetar {len(target_months)} månader "
                    f"({target_months[0][0]}-{target_months[0][1]:02d} → "
                    f"{target_months[-1][0]}-{target_months[-1][1]:02d})")
        for i, (year, month) in enumerate(target_months):
            process_month(accounts, ACCESS_TOKEN, year, month, args.force)
            if i < len(target_months) - 1:
                time.sleep(2)

    logger.info("OBS: IG reach = enbart organisk (FB reach inkluderar även betald).")
    logger.info("Klar!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Avbruten av användare.")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Oväntat fel: {e}")
        import traceback
        logger.critical(traceback.format_exc())
        sys.exit(1)
