# fetch_facebook_comments.py
# Version 1.0 - Kommentarräknare för Facebook-sidor
#
# Detta skript räknar kommentarer och replies på Facebook-sidors inlägg
# och genererar CSV-rapporter per månad.

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
    
    log_filename = os.path.join(log_dir, f"facebook_comments_{now.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.FileHandler("facebook_comments.log"),
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

def get_posts_for_month(page_id, page_token, year, month):
    """Hämta alla posts för en sida under en specifik månad"""
    days_in_month = monthrange(year, month)[1]
    since_date = datetime(year, month, 1)
    until_date = datetime(year, month, days_in_month, 23, 59, 59)
    
    since_timestamp = int(since_date.timestamp())
    until_timestamp = int(until_date.timestamp())
    
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}/posts"
    params = {
        "access_token": page_token,
        "since": since_timestamp,
        "until": until_timestamp,
        "limit": 100,
        "fields": "id,created_time"
    }
    
    all_posts = []
    
    while True:
        data = api_request(url, params)
        
        if not data or "data" not in data:
            break
        
        posts = data["data"]
        all_posts.extend(posts)
        
        # Kolla pagination
        if "paging" in data and "next" in data["paging"]:
            url, params = _unpack_next_url(data["paging"]["next"])
        else:
            break
    
    return all_posts

def count_comments_on_post(post_id, page_token):
    """Räkna kommentarer och replies på ett specifikt inlägg"""
    url = f"https://graph.facebook.com/{API_VERSION}/{post_id}/comments"
    params = {
        "access_token": page_token,
        "summary": "true",
        "filter": "stream",
        "limit": 100
    }
    
    total_comments = 0
    total_replies = 0
    
    while True:
        data = api_request(url, params)
        
        if not data:
            break
        
        if "data" in data:
            comments = data["data"]
            total_comments += len(comments)
            
            # Räkna replies på varje kommentar
            for comment in comments:
                comment_id = comment.get("id")
                if comment_id:
                    reply_count = count_replies_on_comment(comment_id, page_token)
                    total_replies += reply_count
        
        # Summary ger total count om tillgängligt
        if "summary" in data and "total_count" in data["summary"]:
            # Använd summary för snabbare räkning
            total_comments = data["summary"]["total_count"]
            break
        
        # Pagination
        if "paging" in data and "next" in data["paging"]:
            url, params = _unpack_next_url(data["paging"]["next"])
        else:
            break
    
    return total_comments, total_replies

def count_replies_on_comment(comment_id, page_token):
    """Räkna replies (svar) på en specifik kommentar"""
    url = f"https://graph.facebook.com/{API_VERSION}/{comment_id}/comments"
    params = {
        "access_token": page_token,
        "summary": "true",
        "limit": 100
    }
    
    data = api_request(url, params)
    
    if not data:
        return 0
    
    # Använd summary om tillgängligt
    if "summary" in data and "total_count" in data["summary"]:
        return data["summary"]["total_count"]
    
    # Annars räkna manuellt
    if "data" in data:
        return len(data["data"])
    
    return 0

def process_page_for_month(page_id, page_name, year, month):
    """Bearbeta en sida för en specifik månad och räkna kommentarer"""
    logger.info(f"  📄 Bearbetar: {page_name}")
    
    # Hämta Page Access Token
    page_token = get_page_access_token(page_id)
    if not page_token:
        logger.warning(f"    ⚠️ Kunde inte hämta Page Access Token, hoppar över denna sida")
        return {
            "page_id": page_id,
            "page_name": page_name,
            "comments": 0,
            "replies": 0,
            "total": 0
        }
    
    # Hämta alla posts för månaden
    posts = get_posts_for_month(page_id, page_token, year, month)
    
    if not posts:
        logger.info(f"    ℹ️ Inga inlägg hittades för {year}-{month:02d}")
        return {
            "page_id": page_id,
            "page_name": page_name,
            "comments": 0,
            "replies": 0,
            "total": 0
        }
    
    logger.info(f"    📊 Hittade {len(posts)} inlägg, räknar kommentarer...")
    
    total_comments = 0
    total_replies = 0
    
    # Räkna kommentarer och replies för varje post
    for i, post in enumerate(posts, 1):
        post_id = post["id"]
        comments, replies = count_comments_on_post(post_id, page_token)
        total_comments += comments
        total_replies += replies
        
        if i % 10 == 0:
            logger.info(f"    ⏳ Bearbetat {i}/{len(posts)} inlägg...")
    
    total = total_comments + total_replies
    
    logger.info(f"    ✅ Kommentarer: {total_comments}, Replies: {total_replies}, Total: {total}")
    
    return {
        "page_id": page_id,
        "page_name": page_name,
        "comments": total_comments,
        "replies": total_replies,
        "total": total
    }

def save_to_csv(data, year, month):
    """Spara data till CSV-fil"""
    filename = f"FB_Comments_{year}_{month:02d}.csv"
    
    logger.info(f"💾 Sparar resultat till {filename}...")
    
    try:
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['Page ID', 'Page Name', 'Comments', 'Replies', 'Total']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            writer.writeheader()
            for row in data:
                writer.writerow({
                    'Page ID': row['page_id'],
                    'Page Name': row['page_name'],
                    'Comments': row['comments'],
                    'Replies': row['replies'],
                    'Total': row['total']
                })
        
        logger.info(f"✅ Sparade {len(data)} sidor till {filename}")
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
        # Kontrollera om filen redan finns
        filename = f"FB_Comments_{year}_{month:02d}.csv"
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
    parser = argparse.ArgumentParser(description='Hämta kommentarstatistik från Facebook-sidor')
    parser.add_argument('--month', help='Specifik månad att bearbeta (YYYY-MM)')
    parser.add_argument('--start', help='Startmånad (överrider config.py)', default=INITIAL_START_YEAR_MONTH)
    parser.add_argument('--debug', action='store_true', help='Aktivera debug-loggning')
    parser.add_argument('--page-id', help='Specifikt Page ID att bearbeta (annars alla sidor)')
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    logger.info("=" * 80)
    logger.info("🚀 FACEBOOK KOMMENTARRÄKNARE")
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
        for page_id, page_name in pages[:10]:  # Visa första 10
            logger.info(f"  {page_id} - {page_name}")
        if len(pages) > 10:
            logger.info(f"  ... och {len(pages) - 10} sidor till")
        logger.info("=" * 80)
        
        user_input = input("\n🔍 Vilka sidor vill du ha data från? (alla/PAGE_ID): ").strip()
        
        if user_input.lower() != "alla":
            # Användaren valde specifikt Page ID
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
        # --page-id argument användes
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
        save_to_csv(month_data, year, month)
        
        logger.info(f"\n✅ Månad {year}-{month:02d} slutförd!")
    
    logger.info(f"\n{'='*80}")
    logger.info("🎉 KLART! Alla månader bearbetade.")
    logger.info(f"{'='*80}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
