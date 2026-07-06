# fetch_page_status.py
# Månatlig integritetsstatus för Facebook-sidor via Page Integrity API.
#
# Hämtar GET /{PAGE_ID}/page_status för alla sidor token har åtkomst till och
# skriver en månatlig statusöversikt till CSV (en rad per sida).
#
# OBS: Detta är INTE ett insights-/metrik-skript. API:t ger en ögonblicksbild av
# aktuell status vid anropet — det finns ingen rapportperiod och ingen historik.
# Varje rad stämplas därför med run_date (inte Period_start/Period_end).

import csv
import os
import sys
import time
import argparse
import logging
import json
from datetime import datetime

import requests

from config import (
    ACCESS_TOKEN, TOKEN_LAST_UPDATED, API_VERSION,
    MAX_RETRIES, RETRY_DELAY, TOKEN_VALID_DAYS, MAX_REQUESTS_PER_HOUR,
)

# Konfigurera loggning
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("facebook_page_status.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Behörighet som Page Integrity API kräver utöver vanlig sid-läsning
REQUIRED_SCOPE = "pages_manage_metadata"

# Page Integrity API kräver en relativt ny Graph-version. Om den konfigurerade
# versionen är för gammal faller vi tillbaka till denna.
FALLBACK_API_VERSION = "v24.0"

# CSV-kolumner (översiktsformat, en rad per sida)
FIELDNAMES = [
    "run_date",
    "page_id",
    "page_name",
    "status",
    "num_violations",
    "violation_types",
    "num_restrictions",
    "restricted_features",
    "earliest_expiration",
    "is_srholder",
    "error_message",
]

# Räknare för rate limit-hantering
api_call_count = 0
start_time = time.time()

# Den API-version som faktiskt används (kan ändras till fallback under körning)
effective_api_version = API_VERSION


def check_token_expiry():
    """Kontrollera om token snart går ut och varna användaren."""
    try:
        last_updated = datetime.strptime(TOKEN_LAST_UPDATED, "%Y-%m-%d")
        days_since = (datetime.now() - last_updated).days
        days_left = TOKEN_VALID_DAYS - days_since
        logger.info(f"🔑 Token skapades för {days_since} dagar sedan ({days_left} dagar kvar till utgång).")
        if days_left <= 0:
            logger.error("❌ KRITISKT: Din token har gått ut! Skapa en ny token omedelbart.")
            sys.exit(1)
        elif days_left <= 7:
            logger.warning(f"⚠️ VARNING: Din token går ut inom {days_left} dagar! Skapa en ny token snart.")
    except Exception as e:
        logger.error(f"⚠️ Kunde inte tolka TOKEN_LAST_UPDATED: {e}")


def api_request(url, params, retries=MAX_RETRIES):
    """Gör API-förfrågan med återförsök och rate limit-hantering.

    Återanvänt mönster från diagnostics.py. Returnerar tolkad JSON (kan innehålla
    'error') eller None vid hårda fel.
    """
    global api_call_count

    current_time = time.time()
    elapsed_hours = (current_time - start_time) / 3600
    rate = api_call_count / elapsed_hours if elapsed_hours > 0 else 0

    if rate > MAX_REQUESTS_PER_HOUR * 0.9:
        wait_time = 3600 / MAX_REQUESTS_PER_HOUR
        logger.warning(f"Närmar oss rate limit ({int(rate)}/h). Väntar {wait_time:.1f} sekunder...")
        time.sleep(wait_time)

    for attempt in range(retries):
        try:
            api_call_count += 1
            response = requests.get(url, params=params, timeout=30)

            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', RETRY_DELAY))
                logger.warning(f"Rate limit nått! Väntar {retry_after} sekunder... (försök {attempt+1}/{retries})")
                time.sleep(retry_after)
                continue

            elif response.status_code >= 500:
                wait_time = RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Serverfel: {response.status_code}. Väntar {wait_time} sekunder... (försök {attempt+1}/{retries})")
                time.sleep(wait_time)
                continue

            elif response.status_code == 400:
                # Returnera felkroppen så anroparen kan inspektera felmeddelandet
                # (t.ex. "Unknown path components" för fel API-version).
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    data = None
                if data and "error" in data:
                    error_code = data["error"].get("code")
                    error_msg = data["error"].get("message", "Okänt fel")
                    if error_code == 4:  # App-specifikt rate limit
                        wait_time = 60 * (attempt + 1)
                        logger.warning(f"App rate limit: {error_msg}. Väntar {wait_time} sekunder...")
                        time.sleep(wait_time)
                        continue
                    elif error_code == 190:  # Ogiltig token
                        logger.error(f"Access token ogiltig: {error_msg}")
                        return data
                    return data
                logger.error(f"HTTP-fel 400: {response.text[:200]}")
                return None

            if response.status_code != 200:
                logger.error(f"HTTP-fel {response.status_code}: {response.text[:200]}")
                if attempt < retries - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.info(f"Väntar {wait_time} sekunder innan nytt försök... (försök {attempt+1}/{retries})")
                    time.sleep(wait_time)
                    continue
                return None

            try:
                return response.json()
            except json.JSONDecodeError:
                logger.error(f"Kunde inte tolka JSON-svar: {response.text[:100]}")
                return None

        except requests.RequestException as e:
            logger.error(f"Nätverksfel: {e}")
            if attempt < retries - 1:
                wait_time = RETRY_DELAY * (2 ** attempt)
                logger.info(f"Väntar {wait_time} sekunder innan nytt försök... (försök {attempt+1}/{retries})")
                time.sleep(wait_time)
            else:
                return None

    return None


def validate_token(token):
    """Validera att token är giltig."""
    logger.info("Validerar token...")
    url = f"https://graph.facebook.com/{effective_api_version}/debug_token"
    params = {"input_token": token, "access_token": token}
    data = api_request(url, params)

    if not data or "data" not in data:
        logger.error("❌ Kunde inte validera token")
        return False
    if not data["data"].get("is_valid"):
        logger.error(f"❌ Token är ogiltig: {data['data'].get('error', {}).get('message', 'Okänd anledning')}")
        return False

    logger.info(f"✅ Token validerad. App ID: {data['data'].get('app_id')}")
    return True


def check_required_scope(token):
    """Mjuk kontroll: varna (men avbryt inte) om pages_manage_metadata saknas."""
    url = f"https://graph.facebook.com/{effective_api_version}/debug_token"
    params = {"input_token": token, "access_token": token}
    data = api_request(url, params)

    scopes = set()
    if data and "data" in data:
        scopes = set(data["data"].get("scopes", []))

    if REQUIRED_SCOPE not in scopes:
        logger.warning(f"VARNING: token saknar {REQUIRED_SCOPE} — page_status kan returnera fel")
    else:
        logger.info(f"✅ Token har {REQUIRED_SCOPE}.")


def get_page_ids_with_access(token):
    """Hämta alla sidor som token har åtkomst till (Srholder INKLUDERAS)."""
    logger.info("Hämtar tillgängliga sidor...")
    url = f"https://graph.facebook.com/{effective_api_version}/me/accounts"
    params = {"access_token": token, "limit": 100, "fields": "id,name"}

    pages = []
    next_url = url
    while next_url:
        data = api_request(url if next_url == url else next_url, {} if next_url != url else params)
        if not data or "data" not in data:
            break
        pages.extend(data["data"])
        next_url = data.get("paging", {}).get("next")
        if not next_url or next_url == url:
            break

    if not pages:
        logger.warning("Inga sidor hittades. Token kanske saknar 'pages_show_list'-behörighet.")

    page_list = [(page["id"], page.get("name", f"Page {page['id']}")) for page in pages]
    logger.info(f"✅ Hittade {len(page_list)} sidor (Srholder inkluderade).")
    return page_list


def get_page_access_token(page_id, system_token):
    """Konvertera systemtoken till en Page Access Token för en specifik sida."""
    logger.debug(f"Hämtar Page Access Token för sida {page_id}...")
    url = f"https://graph.facebook.com/{effective_api_version}/{page_id}"
    params = {"fields": "access_token", "access_token": system_token}
    data = api_request(url, params)

    if not data or "error" in data or "access_token" not in data:
        error_msg = data.get("error", {}).get("message", "Okänt fel") if data and "error" in data else "Kunde inte hämta token"
        logger.warning(f"⚠️ Kunde inte hämta Page Access Token för sida {page_id}: {error_msg}")
        return None

    return data["access_token"]


def is_version_error(error_msg):
    """Avgör om felet beror på för gammal API-version (felaktig endpoint-väg)."""
    if not error_msg:
        return False
    msg = error_msg.lower()
    return (
        "unknown path components" in msg
        or "nonexisting field" in msg
        or "does not exist on" in msg
    )


def fetch_page_status(page_id, page_token):
    """Anropa page_status för en sida. Returnerar (data, error_message).

    Faller vid behov tillbaka till FALLBACK_API_VERSION om versionen är för gammal.
    """
    global effective_api_version

    def _call(version):
        url = f"https://graph.facebook.com/{version}/{page_id}/page_status"
        return api_request(url, {"access_token": page_token})

    data = _call(effective_api_version)

    if data and "error" in data:
        error_msg = data["error"].get("message", "Okänt fel")
        # Versionsfallback: om endpointen inte känns igen, prova en nyare version.
        if is_version_error(error_msg) and effective_api_version != FALLBACK_API_VERSION:
            logger.warning(
                f"page_status stöds inte i {effective_api_version} "
                f"({error_msg}) — byter till {FALLBACK_API_VERSION} för resten av körningen."
            )
            effective_api_version = FALLBACK_API_VERSION
            data = _call(effective_api_version)
            if data and "error" in data:
                return None, data["error"].get("message", "Okänt fel")
        else:
            return None, error_msg

    if not data:
        return None, "inget svar från API:t"

    return data, ""


def is_srholder(page_name):
    """Informativ flagga: matchar Srholder-mönstret (namn börjar Srholder + siffra).

    Brett mönster: "Srholder" följt av minst en siffra, ev. med bokstavssuffix
    (t.ex. Srholder7, Srholder9a, Srholder8g) flaggas alla som Srholder-sidor.
    """
    if not page_name:
        return False
    return page_name.startswith("Srholder") and len(page_name) > 8 and page_name[8].isdigit()


def epoch_to_iso(ts):
    """Konvertera Unix-epoch till läsbar ISO-tid (YYYY-MM-DD HH:MM). Tom vid fel/saknad."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return ""


def build_row(run_date, page_id, page_name, data):
    """Bygg en CSV-rad utifrån ett lyckat page_status-svar."""
    violations = data.get("violations") or []
    restrictions = data.get("restrictions") or []

    violation_types = [v.get("type", "") for v in violations if v.get("type")]

    # Endast restriktioner med status == RESTRICTED räknas som aktiva
    active_restrictions = [r for r in restrictions if (r.get("status") or "").upper() == "RESTRICTED"]
    restricted_features = [r.get("feature", "") for r in active_restrictions if r.get("feature")]

    # Tidigaste utgång bland aktiva restriktioner (saknad = permanent, ignoreras)
    expirations = [r.get("expiration_time") for r in active_restrictions if r.get("expiration_time")]
    earliest = min(expirations) if expirations else None

    return {
        "run_date": run_date,
        "page_id": page_id,
        "page_name": page_name,
        "status": data.get("status", "ok"),
        "num_violations": len(violations),
        "violation_types": ";".join(violation_types),
        "num_restrictions": len(active_restrictions),
        "restricted_features": ";".join(restricted_features),
        "earliest_expiration": epoch_to_iso(earliest),
        "is_srholder": "Ja" if is_srholder(page_name) else "Nej",
        "error_message": "",
    }


def build_error_row(run_date, page_id, page_name, error_message):
    """Bygg en CSV-rad för en sida där anropet misslyckades."""
    return {
        "run_date": run_date,
        "page_id": page_id,
        "page_name": page_name,
        "status": "error",
        "num_violations": 0,
        "violation_types": "",
        "num_restrictions": 0,
        "restricted_features": "",
        "earliest_expiration": "",
        "is_srholder": "Ja" if is_srholder(page_name) else "Nej",
        "error_message": error_message,
    }


def append_row(output_file, row):
    """Skriv en rad till CSV i append-läge. Skriv header endast om filen är ny/tom."""
    write_header = not os.path.exists(output_file) or os.path.getsize(output_file) == 0
    with open(output_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def load_pages_json(path):
    """Ladda en valfri JSON-fil [{"id":..., "name":...}, ...] för att begränsa sidor."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pages = []
    for entry in data:
        pid = entry.get("id")
        if not pid:
            continue
        pages.append((str(pid), entry.get("name", f"Page {pid}")))
    return pages


def main():
    global effective_api_version

    parser = argparse.ArgumentParser(
        description="Hämtar månatlig integritetsstatus (page_status) för Facebook-sidor."
    )
    parser.add_argument("--pages-json", help="Valfri JSON [{id, name}, ...] för att begränsa till vissa sidor")
    parser.add_argument("--api-version", help="Överrida API_VERSION från config.py")
    parser.add_argument("--output-dir", help="Överrida default output-katalog")
    args = parser.parse_args()

    if args.api_version:
        effective_api_version = args.api_version

    logger.info("📋 Facebook Page Status (integritetsstatus)")
    logger.info("-------------------------------------------------------------------")
    logger.info(f"Använder API-version: {effective_api_version}")

    check_token_expiry()

    if not validate_token(ACCESS_TOKEN):
        logger.error("❌ Token kunde inte valideras. Avbryter.")
        sys.exit(1)

    # Mjuk scope-kontroll: varna men avbryt inte
    check_required_scope(ACCESS_TOKEN)

    # Hämta sidor (Srholder inkluderade)
    if args.pages_json:
        logger.info(f"Läser sidlista från {args.pages_json}...")
        try:
            page_list = load_pages_json(args.pages_json)
            logger.info(f"✅ {len(page_list)} sidor från JSON-fil.")
        except Exception as e:
            logger.error(f"❌ Kunde inte läsa {args.pages_json}: {e}")
            sys.exit(1)
    else:
        page_list = get_page_ids_with_access(ACCESS_TOKEN)

    if not page_list:
        logger.error("❌ Inga sidor att bearbeta. Avbryter.")
        sys.exit(1)

    # Bestäm output-katalog och filnamn (års-subkatalog)
    now = datetime.now()
    run_date = now.strftime("%Y-%m-%d")
    output_dir = args.output_dir if args.output_dir else f"status{now.strftime('%Y')}"
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"pagestatus_{now.strftime('%Y%m%d')}.csv")
    logger.info(f"Skriver till: {output_file}")

    total = len(page_list)
    counts = {"ok": 0, "warning": 0, "restricted": 0, "suspended": 0, "error": 0}

    for i, (page_id, page_name) in enumerate(page_list, start=1):
        try:
            page_token = get_page_access_token(page_id, ACCESS_TOKEN)
            if not page_token:
                row = build_error_row(run_date, page_id, page_name, "kunde inte hämta page access token")
                append_row(output_file, row)
                counts["error"] += 1
                logger.info(f"[{i}/{total}] {page_name}: error")
                continue

            data, error_message = fetch_page_status(page_id, page_token)
            if data is None:
                row = build_error_row(run_date, page_id, page_name, error_message)
                append_row(output_file, row)
                counts["error"] += 1
                logger.info(f"[{i}/{total}] {page_name}: error ({error_message})")
                continue

            row = build_row(run_date, page_id, page_name, data)
            append_row(output_file, row)
            status = row["status"]
            counts[status] = counts.get(status, 0) + 1
            logger.info(f"[{i}/{total}] {page_name}: {status}")

        except Exception as e:
            logger.error(f"Fel vid bearbetning av sida {page_id}: {e}")
            row = build_error_row(run_date, page_id, page_name, f"oväntat fel: {e}")
            append_row(output_file, row)
            counts["error"] += 1
            logger.info(f"[{i}/{total}] {page_name}: error")

    logger.info(
        f"Sammanfattning: {total} sidor körda — "
        f"ok: {counts.get('ok', 0)}, warning: {counts.get('warning', 0)}, "
        f"restricted: {counts.get('restricted', 0)}, suspended: {counts.get('suspended', 0)}, "
        f"error: {counts.get('error', 0)}"
    )
    logger.info(f"Sparad till: {output_file}")


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
