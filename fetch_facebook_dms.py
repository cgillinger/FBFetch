# fetch_facebook_dms.py
# Version 1.0 - DM-räknare för Facebook-sidor
#
# Detta skript räknar direktmeddelanden (conversations) på Facebook-sidors inbox
# och genererar CSV-rapporter per månad.
#
# KRÄVER: Token med 'pages_messaging' behörighet

import csv
import json
import os
import time
import requests
import logging
import argparse
import sys
import urllib.parse
from datetime import datetime, timedelta
from calendar import monthrange
from config import (
    ACCESS_TOKEN, TOKEN_LAST_UPDATED, INITIAL_START_YEAR_MONTH,
    API_VERSION, CACHE_FILE,
    BATCH_SIZE, MAX_RETRIES, RETRY_DELAY,
    TOKEN_VALID_DAYS
)

# Konfigurera loggning
def setup_logging():
    """Konfigurera loggning med datumstämplad loggfil"""
    now = datetime.now()
    log_dir = "logs"
    
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    log_filename = os.path.join(log_dir, f"facebook_dms_{now.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.FileHandler("facebook_dms.log"),
            logging.StreamHandler()
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Startar loggning till fil: {log_filename}")
    
    return logger

logger = setup_logging()


def _mask_url(url):
    """Returnerar URL med access_token ersatt av [REDACTED] för säker loggning."""
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if "access_token" in params:
            params["access_token"] = ["[REDACTED]"]
        new_query = urllib.parse.urlencode(params, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        return "[URL ej visningsbar]"


def _unpack_next_url(next_url):
    """Extraherar access_token från en Facebook-pagineringslänk och returnerar
    (clean_url, params) där token ligger i params-dikt (ej i URL:en)."""
    parsed = urllib.parse.urlparse(next_url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    token_list = qs.pop("access_token", [])
    token = token_list[0] if token_list else None
    clean_query = urllib.parse.urlencode(qs, doseq=True)
    clean_url = urllib.parse.urlunparse(parsed._replace(query=clean_query))
    params = {"access_token": token} if token else {}
    return clean_url, params


# API-anropsräknare
api_call_count = 0
start_time = time.time()
rate_limit_backoff = 1.0
consecutive_successes = 0

# Hjälpfunktioner för katalogstruktur
def get_year_directory(year):
    """Returnera katalognamn för ett givet år"""
    return f"dms{year}"

def ensure_directory_exists(directory):
    """Skapa katalog om den inte finns"""
    if not os.path.exists(directory):
        os.makedirs(directory)
        logger.debug(f"Skapade katalog: {directory}")

def extract_year_from_filename(filename):
    """Extrahera år från filnamn (FB_DMs_YYYY_MM.csv)"""
    try:
        basename = os.path.basename(filename)
        parts = basename.replace(".csv", "").split("_")
        if len(parts) >= 3 and parts[0] == "FB" and parts[1] == "DMs":
            year_candidate = parts[2]
            if year_candidate.isdigit() and len(year_candidate) == 4:
                return int(year_candidate)
        return None
    except Exception as e:
        logger.warning(f"Kunde inte extrahera år från filnamn {filename}: {e}")
        return None

def check_token_expiry():
    """Kontrollera om token snart går ut och varna användaren"""
    try:
        last_updated = datetime.strptime(TOKEN_LAST_UPDATED, "%Y-%m-%d")
        days_since = (datetime.now() - last_updated).days
        days_left = TOKEN_VALID_DAYS - days_since
        
        logger.info(f"🔑 Token skapades för {days_since} dagar sedan ({days_left} dagar kvar till utgång).")
        
        if days_left <= 7:
            logger.warning(f"⚠️ VARNING: Din token går ut inom {days_left} dagar! Skapa en ny token snart.")
        elif days_left <= 0:
            logger.error(f"❌ KRITISKT: Din token har gått ut! Skapa en ny token omedelbart.")
            return False
        
        return True
    except Exception as e:
        logger.error(f"❌ Kunde inte validera token-utgångsdatum: {e}")
        return False

def api_request(url, params, retry_count=0):
    """Gör ett API-anrop med felhantering och rate limiting.

    access_token skickas som Authorization-header (Bearer) om det finns i params,
    så att token aldrig exponeras i URL:er eller loggmeddelanden.
    """
    global api_call_count, start_time, rate_limit_backoff, consecutive_successes

    api_call_count += 1

    # Dynamisk rate limiting
    if api_call_count % 50 == 0:
        elapsed = time.time() - start_time
        rate = api_call_count / elapsed * 3600
        logger.info(f"📊 API-hastighet: {rate:.0f} anrop/timme ({api_call_count} anrop på {elapsed/60:.1f} min)")

    # Flytta access_token från query-params till Authorization-header
    safe_params = dict(params)
    token = safe_params.pop("access_token", None)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        time.sleep(0.1 * rate_limit_backoff)
        response = requests.get(url, params=safe_params, headers=headers, timeout=30)

        if response.status_code == 200:
            consecutive_successes += 1
            if consecutive_successes > 10 and rate_limit_backoff > 1.0:
                rate_limit_backoff = max(1.0, rate_limit_backoff * 0.9)
            return response.json()

        elif response.status_code == 429 or response.status_code == 17:
            consecutive_successes = 0
            rate_limit_backoff = min(5.0, rate_limit_backoff * 1.5)
            logger.warning(f"⚠️ Rate limit träffad. Väntar {RETRY_DELAY * rate_limit_backoff:.1f}s...")
            time.sleep(RETRY_DELAY * rate_limit_backoff)

            if retry_count < MAX_RETRIES:
                return api_request(url, params, retry_count + 1)
            else:
                logger.error(f"❌ Max retry-försök nådda för {_mask_url(url)}")
                return None

        else:
            logger.error(f"❌ HTTP {response.status_code}: {response.text}")
            return None

    except requests.exceptions.Timeout:
        logger.warning(f"⚠️ Timeout för {_mask_url(url)}")
        if retry_count < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
            return api_request(url, params, retry_count + 1)
        return None

    except Exception as e:
        logger.error(f"❌ API-fel: {e}")
        return None

def load_cache():
    """Ladda sidnamn från cache"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ Kunde inte ladda cache: {e}")
    return {}

def save_cache(cache):
    """Spara sidnamn till cache"""
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ Kunde inte spara cache: {e}")

def get_all_pages():
    """Hämta alla Facebook-sidor som token har åtkomst till"""
    logger.info("📋 Hämtar lista över Facebook-sidor...")
    
    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {"access_token": ACCESS_TOKEN, "limit": 100}
    
    data = api_request(url, params)
    
    if not data or "data" not in data:
        logger.error("❌ Kunde inte hämta sidor. Kontrollera din token och behörigheter.")
        return []
    
    pages = data["data"]
    page_ids = [(page["id"], page["name"]) for page in pages]
    logger.info(f"✅ Hittade {len(page_ids)} sidor")
    
    return page_ids

def filter_placeholder_pages(page_list):
    """Filtrera bort placeholder-sidor (Srholder*)"""
    filtered_pages = []
    filtered_out = []
    
    for page_id, page_name in page_list:
        if page_name and page_name.startswith('Srholder') and page_name[8:].isdigit():
            filtered_out.append((page_id, page_name))
        else:
            filtered_pages.append((page_id, page_name))
    
    if filtered_out:
        placeholder_names = [name for _, name in filtered_out]
        logger.info(f"🚫 Filtrerade bort {len(filtered_out)} placeholder-sidor: {', '.join(placeholder_names)}")
    
    logger.info(f"✅ {len(filtered_pages)} sidor kvar efter filtrering")
    return filtered_pages

def get_page_access_token(page_id):
    """Konvertera systemanvändartoken till Page Access Token"""
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}"
    params = {
        "fields": "access_token",
        "access_token": ACCESS_TOKEN
    }
    
    data = api_request(url, params)
    
    if not data or "error" in data or "access_token" not in data:
        error_msg = data.get("error", {}).get("message", "Okänt fel") if data and "error" in data else "Kunde inte hämta token"
        logger.warning(f"⚠️ Kunde inte hämta Page Access Token för sida {page_id}: {error_msg}")
        return None
    
    return data["access_token"]

def count_conversations_for_month(page_id, page_token, year, month):
    """Räkna konversationer för en specifik månad
    
    VIKTIGA BEGRÄNSNINGAR:
    - API:et har max ~500 konversationer per anrop trots pagination
    - Kan MISSA konversationer om sidan har fler än detta
    - 'updated_time' är SENASTE aktiviteten, inte när konversationen startades
    - 'message_count' kan vara 0 eller saknas i vissa fall
    """
    days_in_month = monthrange(year, month)[1]
    since_date = datetime(year, month, 1)
    until_date = datetime(year, month, days_in_month, 23, 59, 59)
    
    since_timestamp = int(since_date.timestamp())
    until_timestamp = int(until_date.timestamp())
    
    logger.debug(f"  ⚠️ API-BEGRÄNSNING: Kan max hämta ~500 konversationer per sida")
    
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}/conversations"
    params = {
        "access_token": page_token,
        "fields": "id,updated_time,message_count",
        "limit": 100
    }
    
    total_conversations = 0
    total_messages = 0
    conversations_in_period = []
    max_iterations = 100  # Säkerhetsgräns för att undvika oändlig loop
    iteration = 0
    
    while True:
        iteration += 1
        if iteration > max_iterations:
            logger.warning(f"    ⚠️ BEGRÄNSNING TRÄFFAD: Stoppade efter {max_iterations} iterationer (~{total_conversations} konversationer)")
            logger.warning(f"    ⚠️ Sidan kan ha FLER konversationer som INTE räknades!")
            break
        
        data = api_request(url, params)
        
        if not data:
            break
        
        if "data" in data:
            conversations = data["data"]
            
            # Filtrera konversationer som uppdaterades under månaden
            for conv in conversations:
                updated_time_str = conv.get("updated_time")
                if not updated_time_str:
                    continue
                
                # Parse ISO 8601 timestamp
                try:
                    updated_time = datetime.fromisoformat(updated_time_str.replace('Z', '+00:00'))
                    updated_timestamp = int(updated_time.timestamp())
                    
                    # Kolla om konversationen uppdaterades under månaden
                    if since_timestamp <= updated_timestamp <= until_timestamp:
                        conversations_in_period.append(conv)
                        total_conversations += 1
                        
                        # Lägg till message_count om tillgängligt
                        msg_count = conv.get("message_count", 0)
                        total_messages += msg_count
                        
                except Exception as e:
                    logger.debug(f"Kunde inte parse timestamp: {updated_time_str} - {e}")
                    continue
        
        # Pagination
        if "paging" in data and "next" in data["paging"]:
            url, params = _unpack_next_url(data["paging"]["next"])
        else:
            break
    
    # Varna om vi nådde max konversationer
    if total_conversations >= 400:
        logger.warning(f"    ⚠️ VARNING: {total_conversations} konversationer hittades - närmar sig API-gränsen!")
        logger.warning(f"    ⚠️ Siffran kan vara OFULLSTÄNDIG om sidan har fler än ~500 DM:s totalt")
    
    return total_conversations, total_messages

def process_page_for_month(page_id, page_name, year, month):
    """Bearbeta en sida för en specifik månad och räkna DM:s"""
    logger.info(f"  📄 Bearbetar: {page_name}")
    
    # Hämta Page Access Token
    page_token = get_page_access_token(page_id)
    if not page_token:
        logger.warning(f"    ⚠️ Kunde inte hämta Page Access Token, hoppar över denna sida")
        return {
            "page_id": page_id,
            "page_name": page_name,
            "conversations": 0,
            "messages": 0
        }
    
    # Räkna konversationer och meddelanden
    conversations, messages = count_conversations_for_month(page_id, page_token, year, month)
    
    logger.info(f"    ✅ Konversationer: {conversations}, Meddelanden: {messages}")
    
    return {
        "page_id": page_id,
        "page_name": page_name,
        "conversations": conversations,
        "messages": messages
    }

def save_to_csv(data, year, month, page_name=None):
    """Spara data till CSV-fil i årsspecifik katalog"""
    # Skapa filnamn med sidnamn om det finns endast en sida
    if page_name and len(data) == 1:
        # Rensa sidnamn från specialtecken för filnamn
        safe_name = "".join(c for c in page_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_name = safe_name.replace(' ', '_')
        filename = f"FB_DMs_{year}_{month:02d}_{safe_name}.csv"
    else:
        filename = f"FB_DMs_{year}_{month:02d}.csv"
    
    # Skapa årsspecifik katalog
    year_dir = get_year_directory(year)
    ensure_directory_exists(year_dir)
    
    # Fullständig sökväg
    full_path = os.path.join(year_dir, filename)
    
    logger.info(f"💾 Sparar resultat till {full_path}...")
    
    try:
        with open(full_path, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['Page ID', 'Page Name', 'Conversations', 'Messages']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            writer.writeheader()
            for row in data:
                writer.writerow({
                    'Page ID': row['page_id'],
                    'Page Name': row['page_name'],
                    'Conversations': row['conversations'],
                    'Messages': row['messages']
                })
        
        logger.info(f"✅ Sparade {len(data)} sidor till {full_path}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Kunde inte spara CSV: {e}")
        return False

def get_months_to_process(start_year_month, specific_month=None):
    """Bestäm vilka månader som ska bearbetas"""
    if specific_month:
        # Bearbeta endast specifik månad
        try:
            year, month = map(int, specific_month.split('-'))
            return [(year, month)]
        except:
            logger.error(f"❌ Ogiltigt månadsformat: {specific_month}. Använd YYYY-MM")
            return []
    
    # Bearbeta från startdatum till föregående månad
    try:
        start_year, start_month = map(int, start_year_month.split('-'))
    except:
        logger.error(f"❌ Ogiltigt startdatum: {start_year_month}")
        return []
    
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    
    # Föregående månad
    if current_month == 1:
        end_year = current_year - 1
        end_month = 12
    else:
        end_year = current_year
        end_month = current_month - 1
    
    months = []
    year, month = start_year, start_month
    
    while (year < end_year) or (year == end_year and month <= end_month):
        # Kontrollera om filen redan finns i årsspecifik katalog
        year_dir = get_year_directory(year)
        filename = os.path.join(year_dir, f"FB_DMs_{year}_{month:02d}.csv")
        if not os.path.exists(filename):
            months.append((year, month))
        else:
            logger.info(f"⏭️ Hoppar över {year}-{month:02d} (filen finns redan)")
        
        # Nästa månad
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    
    return months

def main():
    """Huvudfunktion"""
    parser = argparse.ArgumentParser(description='Hämta DM-statistik från Facebook-sidor')
    parser.add_argument('--month', help='Specifik månad att bearbeta (YYYY-MM)')
    parser.add_argument('--start', help='Startmånad (överrider config.py)', default=INITIAL_START_YEAR_MONTH)
    parser.add_argument('--debug', action='store_true', help='Aktivera debug-loggning')
    parser.add_argument('--page-id', help='Specifikt Page ID att bearbeta (annars alla sidor)')
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    logger.info("=" * 80)
    logger.info("💬 FACEBOOK DM-RÄKNARE")
    logger.info("=" * 80)
    logger.warning("⚠️ KRÄVER: Token med 'pages_messaging' behörighet!")
    logger.info("=" * 80)
    logger.warning("")
    logger.warning("📋 VIKTIGA API-BEGRÄNSNINGAR FÖR DM-RÄKNING:")
    logger.warning("  1. Max ~500 konversationer kan hämtas per sida (trots pagination)")
    logger.warning("  2. Sidor med fler DM:s får OFULLSTÄNDIGA siffror")
    logger.warning("  3. 'updated_time' = senaste aktivitet (ej start-datum)")
    logger.warning("  4. 'message_count' kan vara 0 eller saknas")
    logger.warning("")
    logger.warning("💡 REKOMMENDATION:")
    logger.warning("  - För exakta DM-statistik: Använd Facebook Business Manager")
    logger.warning("  - Detta skript ger UPPSKATTNINGAR för mindre aktiva sidor")
    logger.warning("")
    logger.info("=" * 80)
    
    # Kontrollera token
    if not check_token_expiry():
        logger.error("❌ Token-problem. Avbryter.")
        return 1
    
    # Hämta sidor
    all_pages = get_all_pages()
    if not all_pages:
        logger.error("❌ Inga sidor hittades. Avbryter.")
        return 1
    
    # Filtrera placeholder-sidor
    pages = filter_placeholder_pages(all_pages)
    if not pages:
        logger.error("❌ Inga giltiga sidor efter filtrering. Avbryter.")
        return 1
    
    # Om inget --page-id argument gavs, fråga användaren
    if not args.page_id:
        logger.info("\n" + "=" * 80)
        logger.info("📋 TILLGÄNGLIGA SIDOR:")
        logger.info("=" * 80)
        for page_id, page_name in pages[:10]:
            logger.info(f"  {page_id} - {page_name}")
        if len(pages) > 10:
            logger.info(f"  ... och {len(pages) - 10} sidor till")
        logger.info("=" * 80)
        
        user_input = input("\n🔍 Vilka sidor vill du ha data från? (alla/PAGE_ID): ").strip()
        
        if user_input.lower() != "alla":
            selected_page = None
            for page_id, page_name in pages:
                if page_id == user_input:
                    selected_page = (page_id, page_name)
                    break
            
            if not selected_page:
                logger.error(f"❌ Page ID '{user_input}' hittades inte i listan. Avbryter.")
                return 1
            
            pages = [selected_page]
            logger.info(f"✅ Valde sida: {selected_page[1]} (ID: {selected_page[0]})")
        else:
            logger.info(f"✅ Bearbetar alla {len(pages)} sidor")
    else:
        selected_page = None
        for page_id, page_name in pages:
            if page_id == args.page_id:
                selected_page = (page_id, page_name)
                break
        
        if not selected_page:
            logger.error(f"❌ Page ID '{args.page_id}' hittades inte. Avbryter.")
            return 1
        
        pages = [selected_page]
        logger.info(f"✅ Bearbetar endast: {selected_page[1]} (ID: {selected_page[0]})")
    
    # Bestäm vilka månader som ska bearbetas
    months_to_process = get_months_to_process(args.start, args.month)
    
    if not months_to_process:
        logger.info("✅ Inga månader att bearbeta.")
        return 0
    
    logger.info(f"📅 Kommer att bearbeta {len(months_to_process)} månad(er)")
    
    # Bearbeta varje månad
    for year, month in months_to_process:
        logger.info(f"\n{'='*80}")
        logger.info(f"📆 Bearbetar månad: {year}-{month:02d}")
        logger.info(f"{'='*80}")
        
        month_data = []
        
        for page_id, page_name in pages:
            result = process_page_for_month(page_id, page_name, year, month)
            month_data.append(result)
        
        # Spara resultat
        save_to_csv(month_data, year, month, page_name=pages[0][1] if len(pages) == 1 else None)
        
        logger.info(f"\n✅ Månad {year}-{month:02d} slutförd!")
    
    logger.info(f"\n{'='*80}")
    logger.info("🎉 KLART! Alla månader bearbetade.")
    logger.info(f"{'='*80}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
