# fetch_instagram_posts.py
# Version 4.2 - Instagram Post-nivå Analytics med Post-URL:er
# 
# Detta skript hämtar detaljerad statistik för alla Instagram-inlägg under en vald tidsperiod.
# Fokuserar på post-nivå metriker som är tillförlitliga från Instagram Graph API.
#
# VERSION 4.2 UPPDATERING:
# - Lägger till Post-URL (permalink) som ny kolumn i CSV-utmatning
# - Använder standardbibliotekets zoneinfo (Python ≥3.9)
# - UTF-8 loggning för Windows-kompatibilitet
# - Halvöppet intervall [start, end) med epoch-sekunder för entydighet
# - Robust server-side filtrering med smart fallback
#
# VIKTIGA NOTERINGAR:
# - Kräver Instagram Business eller Creator-konto
# - Endast organiska data (inga annonsdata inkluderas)
# - Post-nivå data är betydligt mer tillförlitlig än konto-aggregat
# - Stöder datumfiltrering direkt via API (v11.0+)
# - Kräver Python ≥3.9 för zoneinfo-stöd
#
# METRIKER SOM HÄMTAS PER POST:
# - reach: Antal unika konton som sett inlägget (TILLFÖRLITLIG på postnivå)
# - comments: Antal kommentarer på inlägget
# - likes: Antal gilla-markeringar (hjärtan)
# - shares: Antal delningar (främst för Reels, ofta 0 för vanliga posts)
# - saved: Antal gånger inlägget sparats av användare
# - views: Antal visningar för video/Reels (ersätter impressions för nya posts)
# - permalink: URL till inlägget på Instagram
#
# POST-TYPER SOM HANTERAS:
# - IMAGE: Vanliga fotoinlägg
# - VIDEO: Videoinlägg i feed
# - CAROUSEL_ALBUM: Flera bilder/videos i samma post
# - REELS: Instagram Reels (kortformat video)

import csv
import json
import os
import time
import requests
import logging
import argparse
import sys
import glob
import pandas as pd
from datetime import datetime, timedelta
from calendar import monthrange

# KRITISK FIX: Python version check och zoneinfo
if sys.version_info < (3, 9):
    print("KRITISKT FEL: Detta skript kräver Python 3.9 eller senare för zoneinfo-stöd.")
    print("Aktuell version:", sys.version)
    print("Uppgradera Python eller installera pytz-fallback.")
    sys.exit(1)

try:
    from zoneinfo import ZoneInfo
    print("Använder standardbibliotekets zoneinfo för tidszonhantering")
except ImportError:
    print("KRITISKT FEL: zoneinfo inte tillgängligt. Kräver Python ≥3.9.")
    print("Alternativ: Installera pytz och modifiera skriptet.")
    sys.exit(1)

from config import (
    ACCESS_TOKEN, TOKEN_LAST_UPDATED, INITIAL_START_YEAR_MONTH,
    API_VERSION, CACHE_FILE, 
    BATCH_SIZE, MAX_RETRIES, RETRY_DELAY, 
    TOKEN_VALID_DAYS, MAX_REQUESTS_PER_HOUR,
    MONTH_PAUSE_SECONDS
)

# ===================================================================================
# HÄR BÖRJAR DEL 1 - Grundläggande funktioner och API-hantering
# ===================================================================================

def setup_logging():
    """
    Konfigurera loggning med datumstämplad loggfil och UTF-8 encoding.
    
    UTF-8 encoding för att undvika UnicodeEncodeError på Windows.
    """
    now = datetime.now()
    log_dir = "logs"
    
    # Skapa loggdirektory om den inte finns
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Skapa datumstämplad loggfilnamn
    log_filename = os.path.join(log_dir, f"instagram_posts_{now.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    
    # UTF-8 encoding för alla loggfiler
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding="utf-8"),  # UTF-8 för Windows-kompatibilitet
            logging.FileHandler("instagram_posts.log", encoding="utf-8"),  # UTF-8 för senaste logg
            logging.StreamHandler(sys.stdout)  # Explicit stdout för konsistens
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Startar loggning v4.2 till fil: {log_filename}")
    
    return logger

# Konfigurera loggning med UTF-8 stöd
logger = setup_logging()

# Räknare för API-anrop och rate limit-hantering
api_call_count = 0
start_time = time.time()
last_rate_limit_time = None
rate_limit_backoff = 1.0  # Dynamisk backoff-multiplikator
consecutive_successes = 0  # Räkna lyckade anrop för att minska backoff

def check_token_expiry():
    """Kontrollera om token snart går ut och varna användaren"""
    try:
        last_updated = datetime.strptime(TOKEN_LAST_UPDATED, "%Y-%m-%d")
        days_since = (datetime.now() - last_updated).days
        days_left = TOKEN_VALID_DAYS - days_since
        
        logger.info(f"Token skapades för {days_since} dagar sedan ({days_left} dagar kvar till utgång).")
        
        if days_left <= 7:
            logger.warning(f"VARNING: Din token går ut inom {days_left} dagar! Skapa en ny token snart.")
        elif days_left <= 0:
            logger.error(f"KRITISKT: Din token har gått ut! Skapa en ny token omedelbart.")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Kunde inte tolka TOKEN_LAST_UPDATED: {e}")

def load_account_cache():
    """Ladda cache med Instagram-kontonamn för att minska API-anrop"""
    cache_file = "instagram_accounts.json"
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                logger.debug(f"Laddar Instagram-konto-cache från {cache_file}")
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"Kunde inte ladda cache-fil, skapar ny cache")
    return {}

def save_account_cache(cache):
    """Spara cache med Instagram-kontonamn för framtida körningar"""
    cache_file = "instagram_accounts.json"
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
            logger.debug(f"Sparade Instagram-konto-cache till {cache_file}")
    except Exception as e:
        logger.error(f"Kunde inte spara cache: {e}")

def api_request(url, params, retries=MAX_RETRIES):
    """
    Gör API-förfrågan med dynamisk rate limit-hantering för Instagram API.
    
    Förbättringar:
    - Förbättrad rate limit-rapportering med kvot-headers
    - Bättre diagnostik för server-side filter-stöd
    - Mer detaljerad felhantering för Instagram-specifika felkoder
    
    VIKTIGT: Instagram API har striktare rate limits än Facebook API.
    Denna funktion hanterar:
    - HTTP 429 (Too Many Requests)
    - Exponential backoff med dynamisk justering
    - Server errors (5xx)
    - Instagram-specifika felkoder
    """
    global api_call_count, last_rate_limit_time, rate_limit_backoff, consecutive_successes
    
    # Om vi nyligen träffade rate limit, vänta lite baserat på backoff
    if last_rate_limit_time:
        time_since_limit = time.time() - last_rate_limit_time
        if time_since_limit < (60 * rate_limit_backoff):  # Dynamisk väntetid
            wait_time = (60 * rate_limit_backoff) - time_since_limit
            logger.info(f"Väntar {wait_time:.1f}s efter tidigare rate limit (backoff: {rate_limit_backoff:.1f}x)")
            time.sleep(wait_time)
    
    for attempt in range(retries):
        try:
            api_call_count += 1
            response = requests.get(url, params=params, timeout=30)
            
            # Logga rate limit-headers om tillgängliga
            if 'X-App-Usage' in response.headers:
                usage = response.headers['X-App-Usage']
                logger.debug(f"API-användning: {usage}")
            if 'X-Page-Usage' in response.headers:
                page_usage = response.headers['X-Page-Usage']
                logger.debug(f"Sid-användning: {page_usage}")
            
            # Hantera vanliga HTTP-fel
            if response.status_code == 429:  # Too Many Requests
                last_rate_limit_time = time.time()
                rate_limit_backoff = min(rate_limit_backoff * 1.5, 10.0)  # Öka backoff, max 10x
                consecutive_successes = 0  # Återställ räknaren
                
                retry_after = int(response.headers.get('Retry-After', 60 * rate_limit_backoff))
                logger.warning(f"Rate limit nått! Väntar {retry_after}s (backoff: {rate_limit_backoff:.1f}x)")
                time.sleep(retry_after)
                continue
                
            elif response.status_code >= 500:  # Server error
                wait_time = min(RETRY_DELAY * (2 ** attempt), 30)  # Max 30 sekunder
                logger.warning(f"Serverfel: {response.status_code}. Väntar {wait_time}s... (försök {attempt+1}/{retries})")
                time.sleep(wait_time)
                continue
            
            # För alla HTTP-svarkoder, försök tolka JSON-innehållet
            try:
                json_data = response.json()
                
                # Särskild hantering för 400-fel (Bad Request)
                if response.status_code == 400 and "error" in json_data:
                    error_code = json_data["error"].get("code")
                    error_msg = json_data["error"].get("message", "Okänt fel")
                    
                    # Bättre hantering av specifika felkoder
                    if error_code == 4:  # App-specifikt rate limit
                        last_rate_limit_time = time.time()
                        rate_limit_backoff = min(rate_limit_backoff * 1.5, 10.0)
                        wait_time = min(60 * rate_limit_backoff, 300)  # Max 5 minuter
                        logger.warning(f"App rate limit: {error_msg}. Väntar {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                        
                    elif error_code == 190:  # Ogiltig token
                        logger.error(f"Access token ogiltig: {error_msg}")
                        return None
                    
                    elif error_code == 100:  # Instagram-specifikt fel
                        logger.error(f"Instagram API fel: {error_msg}")
                        # Kontrollera om det är server-side filter-problem
                        if any(term in error_msg.lower() for term in ["since", "until", "parameter", "unsupported"]):
                            logger.warning(f"Server-side datumfiltrering stöds inte: {error_msg}")
                        return None
                
                # Om vi kommer hit och har en icke-200 status, logga felet men returnera ändå JSON-data
                # så att anropande funktion kan hantera felet mer detaljerat
                if response.status_code != 200:
                    logger.error(f"HTTP-fel {response.status_code}: {response.text}")
                    
                    if attempt < retries - 1:
                        wait_time = RETRY_DELAY * (2 ** attempt)
                        logger.info(f"Väntar {wait_time} sekunder innan nytt försök... (försök {attempt+1}/{retries})")
                        time.sleep(wait_time)
                        continue
                    
                    # Returnera ändå JSON-data så att anropande funktion kan hantera felet
                    return json_data
                
                # Allt gick bra, returnera data
                consecutive_successes += 1
                
                # Minska backoff gradvis efter många lyckade anrop
                if consecutive_successes >= 50 and rate_limit_backoff > 1.0:
                    rate_limit_backoff = max(rate_limit_backoff * 0.8, 1.0)
                    logger.debug(f"50 lyckade anrop, minskar backoff till {rate_limit_backoff:.1f}x")
                    consecutive_successes = 0
                
                # Mer detaljerad progress-rapportering
                if api_call_count % 100 == 0:
                    elapsed = time.time() - start_time
                    current_rate = api_call_count / (elapsed / 3600) if elapsed > 0 else 0
                    logger.info(f"Progress: {api_call_count} API-anrop, {current_rate:.0f}/h, backoff: {rate_limit_backoff:.1f}x")
                
                return json_data
                
            except json.JSONDecodeError:
                logger.error(f"Kunde inte tolka JSON-svar: {response.text[:100]}")
                if attempt < retries - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.info(f"Väntar {wait_time} sekunder innan nytt försök... (försök {attempt+1}/{retries})")
                    time.sleep(wait_time)
                    continue
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
    """Validera att token är giltig och hämta användarbehörigheter"""
    logger.info("Validerar token...")
    url = f"https://graph.facebook.com/{API_VERSION}/debug_token"
    params = {"input_token": token, "access_token": token}
    
    data = api_request(url, params)
    
    if not data or "data" not in data:
        logger.error("Kunde inte validera token")
        return False
        
    if not data["data"].get("is_valid"):
        logger.error(f"Token är ogiltig: {data['data'].get('error', {}).get('message', 'Okänd anledning')}")
        return False
        
    logger.info(f"Token validerad. App ID: {data['data'].get('app_id')}")
    return True

def get_instagram_accounts_with_access(token):
    """
    Hämta alla Instagram Business/Creator-konton som token har åtkomst till.
    Detta görs via Facebook-sidor som är kopplade till Instagram-konton.
    
    VIKTIGT: Endast Business och Creator-konton kan användas med Graph API.
    Personliga Instagram-konton fungerar INTE.
    """
    logger.info("Hämtar tillgängliga Instagram-konton via Facebook-sidor...")
    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {
        "access_token": token, 
        "limit": 100, 
        "fields": "id,name,instagram_business_account"
    }
    
    instagram_accounts = []
    next_url = url
    
    while next_url:
        data = api_request(url if next_url == url else next_url, {} if next_url != url else params)
        
        if not data or "data" not in data:
            break
            
        pages = data["data"]
        logger.debug(f"Hittade {len(pages)} Facebook-sidor i denna batch")
        
        # Extrahera Instagram-konton från sidor
        for page in pages:
            instagram_data = page.get("instagram_business_account")
            if instagram_data:
                instagram_id = instagram_data.get("id")
                if instagram_id:
                    # Hämta Instagram-kontonamn
                    ig_name = get_instagram_account_name(instagram_id, token)
                    if ig_name:
                        instagram_accounts.append((instagram_id, ig_name, page.get("name", "Okänd Facebook-sida")))
                        logger.debug(f"Hittade Instagram-konto: {ig_name} (ID: {instagram_id})")
        
        # Hantera paginering
        next_url = data.get("paging", {}).get("next")
        if next_url and next_url != url:
            logger.debug(f"Hämtar nästa sida av Facebook-sidor...")
        else:
            break
    
    if not instagram_accounts:
        logger.warning("Inga Instagram Business/Creator-konton hittades kopplat till dina Facebook-sidor.")
        logger.info("Kontrollera att:")
        logger.info("1. Dina Instagram-konton är Business eller Creator-konton")
        logger.info("2. De är kopplade till Facebook-sidor du har åtkomst till")
        logger.info("3. Token har rätt behörigheter (instagram_basic, instagram_manage_insights)")
    
    logger.info(f"Hittade {len(instagram_accounts)} Instagram-konton att analysera")
    return instagram_accounts

def get_instagram_account_name(instagram_id, token):
    """
    Hämta Instagram-kontonamn från ID.
    
    Försöker hämta både username (@-namnet) och display name.
    Prioriterar username eftersom det är mer unikt.
    """
    url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}"
    params = {"fields": "name,username", "access_token": token}
    
    data = api_request(url, params)
    
    if not data or "error" in data:
        error_msg = data.get("error", {}).get("message", "Okänt fel") if data else "Fel vid API-anrop"
        logger.warning(f"Kunde inte hämta namn för Instagram-konto {instagram_id}: {error_msg}")
        return None
    
    # Använd username eller name (prioritera username)
    name = data.get("username") or data.get("name", f"IG_{instagram_id}")
    return name

# ===================================================================================
# HÄR SLUTAR DEL 1 - Grundläggande funktioner och API-hantering
# ===================================================================================
# ===================================================================================
# HÄR BÖRJAR DEL 2 - Post-hämtning och Insights-integrering (ROBUST v4.2)
# ===================================================================================

def get_instagram_posts_for_period(instagram_id, since_date, until_date, account_name=None):
    """
    Hämta alla Instagram-posts för en specifik tidsperiod med robust datumfiltrering.
    
    KRITISKA FÖRBÄTTRINGAR enligt teamfeedback:
    - Halvöppet intervall [start, end) i svensk tid för korrekt gränshantering
    - Epoch-sekunder till API för entydiga tidsformat
    - Robust server-side filtering med funktionell fallback
    - Förbättrad diagnostik: antal sidor, min/max datum, fallback-användning
    - Standardbibliotek zoneinfo istället för pytz
    
    Args:
        instagram_id: Instagram Business Account ID
        since_date: Startdatum (YYYY-MM-DD) 
        until_date: Slutdatum (YYYY-MM-DD)
        account_name: Kontonamn för loggning
    
    Returns:
        list: Lista med post-objekt innehållande metadata
    """
    display_name = account_name if account_name else instagram_id
    logger.info(f"Hämtar posts för {display_name} från {since_date} till {until_date} (robust v4.2)")
    
    try:
        # Halvöppet intervall [start, end) med zoneinfo
        sweden_tz = ZoneInfo("Europe/Stockholm")
        
        # Startdatum: 00:00 svensk tid första dagen
        start_sweden = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=sweden_tz)
        
        # Slutdatum: HALVÖPPET INTERVALL - 00:00 första dagen NÄSTA månad
        end_date_obj = datetime.strptime(until_date, "%Y-%m-%d")
        next_day_sweden = (end_date_obj + timedelta(days=1)).replace(tzinfo=sweden_tz)
        
        # Konvertera till UTC för API-anrop
        start_utc = start_sweden.astimezone(ZoneInfo("UTC"))
        end_utc = next_day_sweden.astimezone(ZoneInfo("UTC"))
        
        # Epoch-sekunder för entydiga API-anrop
        since_epoch = int(start_utc.timestamp())
        until_epoch = int(end_utc.timestamp())
        
        logger.debug(f"  v4.2 Tidszonkonvertering (halvöppet intervall):")
        logger.debug(f"    Sverige: {since_date} 00:00 → {until_date} 24:00 (halvöppet)")  
        logger.debug(f"    UTC epoch: {since_epoch} → {until_epoch}")
        
        # PRIMÄRT: Försök server-side filtrering först
        posts = attempt_server_side_filtering(instagram_id, since_epoch, until_epoch, display_name, start_sweden, next_day_sweden)
        
        # FALLBACK: Client-side filtrering om server-side misslyckas
        if posts is None:
            logger.warning(f"  Server-side filtrering misslyckades, använder client-side fallback")
            posts = fetch_with_client_filter(instagram_id, start_sweden, next_day_sweden, display_name)
        
        # Utökad kvalitetskontroll och diagnostik
        if posts:
            post_dates = [p.get("post_date") for p in posts if p.get("post_date")]
            if post_dates:
                min_date = min(post_dates)
                max_date = max(post_dates)
                logger.info(f"  ✓ Kvalitetskontroll: Posts spänner {min_date} → {max_date}")
                
                # Varning om oväntat få posts för aktiv månad
                if len(posts) < 3:
                    logger.warning(f"  ⚠ Endast {len(posts)} posts - oväntat få för aktiv månad?")
                    logger.info(f"    Överväg buffertkörning: ±1 dag extra för verifiering")
        
        return posts
        
    except Exception as e:
        logger.error(f"  Fel vid robust post-hämtning för {display_name}: {e}")
        return []

def attempt_server_side_filtering(instagram_id, since_epoch, until_epoch, display_name, start_sweden, next_day_sweden):
    """
    Försök server-side filtrering med epoch-tidsstämplar och bättre diagnostik
    """
    
    url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}/media"
    params = {
        "access_token": ACCESS_TOKEN,
        "since": since_epoch,              # Epoch-sekunder för entydighet
        "until": until_epoch,              # Epoch-sekunder för entydighet  
        "limit": 100,                      # Maximal effektivitet
        "fields": "id,timestamp,media_type,media_product_type,caption,permalink"
    }
    
    try:
        posts = []
        page_num = 0
        total_posts_found = 0
        posts_in_period = 0
        posts_outside_period = 0
        server_side_worked = True
        
        logger.debug(f"  Försöker server-side filtrering med epoch-tidsstämplar...")
        
        while url and page_num < 100:  # Säkerhetsbroms
            page_num += 1
            logger.debug(f"    Sida {page_num} för {display_name}...")
            
            data = api_request(url, params)
            
            if data and "data" in data:
                media_in_page = data["data"]
                total_posts_found += len(media_in_page)
                
                if len(media_in_page) == 0:
                    break
                
                # Bearbeta posts med halvöppet intervall
                for post in media_in_page:
                    processed_post = process_post_with_timezone(post, display_name, start_sweden, next_day_sweden)
                    if processed_post:
                        posts.append(processed_post)
                        posts_in_period += 1
                    else:
                        posts_outside_period += 1
                
                # Fortsätt paginering
                paging = data.get("paging", {})
                url = paging.get("next")
                params = {} if url else params
                        
            elif data and "error" in data:
                error_msg = data["error"].get("message", "")
                
                # Kontrollera om server-side filtrering inte stöds
                if any(term in error_msg.lower() for term in ["since", "until", "parameter", "unsupported"]):
                    logger.warning(f"    Server-side filtrering stöds ej: {error_msg}")
                    return None  # Signalera fallback behövs
                else:
                    logger.error(f"    API-fel: {error_msg}")
                    return None
            else:
                logger.warning(f"    Inget data returnerat")
                break
        
        # Kontrollera om server-side filtrering verkligen fungerade
        if posts_outside_period > posts_in_period * 2 and total_posts_found > 50:
            logger.warning(f"    ⚠ Server-side filtrering verkade inte fungera korrekt")
            logger.warning(f"      {posts_outside_period} utanför vs {posts_in_period} inom period")
            server_side_worked = False
            return None  # Fallback till client-side
        
        # Utökad diagnostik för server-side filtrering
        logger.info(f"  ✓ Server-side resultat för {display_name}:")
        logger.info(f"    • {total_posts_found} posts returnerade av server")
        logger.info(f"    • {posts_in_period} posts inom period (INKLUDERADE)")
        logger.info(f"    • {posts_outside_period} posts utanför period")
        logger.info(f"    • {page_num} sidor paginerade")
        logger.info(f"    • Server-side filter: {'fungerade' if server_side_worked else 'misslyckades'}")
        
        return posts
        
    except Exception as e:
        logger.warning(f"    Server-side filtrering misslyckades: {e}")
        return None

def fetch_with_client_filter(instagram_id, start_sweden, next_day_sweden, display_name):
    """
    Fallback med client-side filtrering och smart stopp-logik
    """
    
    logger.info(f"  Använder client-side filtrering för {display_name}")
    
    url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}/media"
    params = {
        "access_token": ACCESS_TOKEN,
        "limit": 100,
        "fields": "id,timestamp,media_type,media_product_type,caption,permalink"
    }
    
    posts = []
    page_num = 0
    consecutive_pages_without_hits = 0  # Smart stopp-logik
    total_pages_processed = 0
    
    while url and page_num < 200:  # Högre gräns för client-side
        page_num += 1
        total_pages_processed += 1
        page_hits = 0
        
        data = api_request(url, params)
        
        if data and "data" in data:
            media_in_page = data["data"]
            
            if len(media_in_page) == 0:
                consecutive_pages_without_hits += 1
                if consecutive_pages_without_hits >= 2:
                    logger.debug(f"    Stoppar efter {consecutive_pages_without_hits} tomma sidor")
                    break
                continue
            
            # Kontrollera om vi passerat perioden
            oldest_post_before_period = False
            
            for post in media_in_page:
                post_timestamp = post.get("timestamp", "")
                if post_timestamp:
                    try:
                        # Parserera UTC timestamp
                        post_utc = datetime.strptime(post_timestamp, "%Y-%m-%dT%H:%M:%S%z")
                        if post_utc.tzinfo is None:
                            post_utc = post_utc.replace(tzinfo=ZoneInfo("UTC"))
                        
                        # Konvertera till svensk tid
                        post_sweden = post_utc.astimezone(start_sweden.tzinfo)
                        
                        # Halvöppet intervall [start_sweden, next_day_sweden)
                        if start_sweden <= post_sweden < next_day_sweden:
                            processed_post = process_post_with_timezone(post, display_name, start_sweden, next_day_sweden)
                            if processed_post:
                                posts.append(processed_post)
                                page_hits += 1
                                consecutive_pages_without_hits = 0  # Återställ räknare
                        elif post_sweden < start_sweden:
                            oldest_post_before_period = True
                            
                    except Exception as e:
                        logger.debug(f"      Fel vid tidskonvertering: {e}")
                        continue
            
            # Smart stopp-logik - två sidor i rad utan träffar + passerat period
            if page_hits == 0:
                consecutive_pages_without_hits += 1
                if consecutive_pages_without_hits >= 2 and oldest_post_before_period:
                    logger.debug(f"    Smart stopp efter sida {page_num}: 2 sidor utan träffar + passerat period")
                    break
            
            # Fortsätt paginering
            paging = data.get("paging", {})
            url = paging.get("next")
            params = {} if url else params
            
        else:
            break
    
    # Logga antal sidor som lästes i fallback
    logger.info(f"  ✓ Client-side fallback för {display_name}:")
    logger.info(f"    • {len(posts)} posts hittade inom period")
    logger.info(f"    • {total_pages_processed} sidor paginerade totalt")
    logger.info(f"    • Smart stopp efter {consecutive_pages_without_hits} sidor utan träffar")
    
    return posts

def process_post_with_timezone(post, display_name, start_sweden=None, next_day_sweden=None):
    """
    Bearbeta en post med korrekt tidszonhantering och halvöppet intervall
    """
    
    try:
        post_timestamp = post.get("timestamp", "")
        if not post_timestamp:
            return None
        
        # Parserera UTC timestamp
        post_utc = datetime.strptime(post_timestamp, "%Y-%m-%dT%H:%M:%S%z")
        if post_utc.tzinfo is None:
            post_utc = post_utc.replace(tzinfo=ZoneInfo("UTC"))
        
        # Konvertera till svensk tid
        sweden_tz = ZoneInfo("Europe/Stockholm")
        post_sweden = post_utc.astimezone(sweden_tz)
        post_date = post_sweden.strftime("%Y-%m-%d")
        
        # Kontrollera halvöppet intervall om parametrar givna
        if start_sweden and next_day_sweden:
            if not (start_sweden <= post_sweden < next_day_sweden):
                logger.debug(f"      [-] Utanför halvöppet intervall: {post_date}")
                return None
        
        # Filtrera post-typer med explicit default
        media_product_type = post.get("media_product_type") or "FEED"
        
        if media_product_type in ["FEED", "REELS"]:
            post["post_date"] = post_date
            post["account_name"] = display_name
            
            media_type = post.get("media_type", "UNKNOWN")
            logger.debug(f"      [+] {post_date} {media_type}/{media_product_type}")
            return post
        else:
            # Logga när poster filtreras bort p.g.a. typ
            logger.debug(f"      [-] Filtrerad post-typ: {media_product_type} (Stories etc)")
            return None
            
    except Exception as e:
        logger.debug(f"      Fel vid post-bearbetning: {e}")
        return None

def get_post_insights(post_id, media_type, media_product_type, account_name=None):
    """
    Hämta insights för en specifik post.
    
    METRIKER SOM HÄMTAS (i prioriterad ordning):
    - reach: Unika konton som sett posten (TILLFÖRLITLIG på postnivå)
    - comments: Antal kommentarer på posten
    - likes: Antal gilla-markeringar (hjärtan/hearts)
    - shares: Antal delningar (främst för Reels, ofta 0 för Feed-posts)
    - saved: Antal gånger posten sparats av användare ("bokmärken")
    - views: Visningar för video/Reels (ersätter impressions för nya posts)
    
    VIKTIGT OM METRIKER PER POST-TYP:
    - IMAGE + FEED: reach, likes, comments, saved (shares oftast 0)
    - VIDEO + FEED: reach, likes, comments, saved, views
    - CAROUSEL_ALBUM: reach, likes, comments, saved (views för videor i carousel)
    - REELS: reach, likes, comments, saved, shares, views
    
    Args:
        post_id: Instagram media ID
        media_type: IMAGE, VIDEO, eller CAROUSEL_ALBUM
        media_product_type: FEED eller REELS
        account_name: Kontonamn för loggning
    
    Returns:
        dict: Insights-data för posten
    """
    display_name = account_name if account_name else "Unknown"
    logger.debug(f"Hämtar insights för post {post_id} ({media_type}/{media_product_type}) - {display_name}")
    
    # Grundläggande metriker som finns för de flesta post-typer
    # METRIK-DEFINITIONER:
    # - reach: Unika användare som sett posten (deduplicated per post)
    # - likes: Antal hjärtan/gilla-markeringar (Instagram's primära reaktion)
    # - comments: Antal kommentarer (inkluderar svar på kommentarer)
    # - saved: Antal "spara"-interaktioner (användare bokmärker posten)
    base_metrics = ["reach", "likes", "comments", "saved"]
    
    # Lägg till shares för Reels
    # SHARES förklaring: Antal gånger användare delat Reels till sina Stories
    # eller skickat som DM. Vanligen 0 för vanliga FEED-posts.
    metrics = base_metrics.copy()
    if media_product_type == "REELS":
        metrics.append("shares")
    
    # Lägg till views för video-innehåll
    # VIEWS förklaring: Antal gånger video/Reel spelats (minst 3 sekunder
    # eller nästan hela längden om kortare än 3 sek)
    # VIKTIGT: views ersätter impressions för posts skapade efter juli 2024
    if media_type == "VIDEO" or media_product_type == "REELS":
        metrics.append("views")
    
    # Skapa resultatstruktur med standardvärden
    result = {
        "reach": 0,          # Unika användare som sett posten
        "comments": 0,       # Antal kommentarer
        "likes": 0,          # Antal gilla-markeringar (hjärtan)
        "shares": 0,         # Antal delningar (främst Reels)
        "saved": 0,          # Antal sparningar/bokmärken
        "views": 0,          # Videovisningar (för VIDEO/REELS)
        "status": "OK",
        "error_message": ""
    }
    
    try:
        url = f"https://graph.facebook.com/{API_VERSION}/{post_id}/insights"
        params = {
            "access_token": ACCESS_TOKEN,
            "metric": ",".join(metrics)
        }
        
        data = api_request(url, params)
        
        if data and "data" in data:
            # Parserera insights-data
            # API-svar format: {"data": [{"name": "reach", "values": [{"value": 1234}]}, ...]}
            for metric_data in data["data"]:
                metric_name = metric_data.get("name", "")
                values = metric_data.get("values", [])
                
                if values and len(values) > 0:
                    # För post-insights är värdet vanligen i values[0].value
                    # (till skillnad från dagliga insights som har arrays)
                    metric_value = values[0].get("value", 0)
                    if metric_name in result:
                        result[metric_name] = metric_value
                        if metric_value > 0:  # Bara logga icke-noll värden för att minska spam
                            logger.debug(f"    {metric_name}: {metric_value}")
            
            # Logga framgångsrik hämtning
            logger.debug(f"    Insights hämtade för {post_id}: "
                        f"reach={result['reach']}, likes={result['likes']}, comments={result['comments']}")
                        
        elif data and "error" in data:
            error_msg = data["error"].get("message", "Okänt fel")
            error_code = data["error"].get("code", "N/A")
            result["status"] = "API_ERROR"
            result["error_message"] = f"Error {error_code}: {error_msg}"
            logger.warning(f"    Kunde inte hämta insights för {post_id}: {error_msg}")
        else:
            result["status"] = "NO_DATA"
            result["error_message"] = "Inget insights-data returnerat"
            logger.debug(f"    Inget insights-data för {post_id}")
            
    except Exception as e:
        result["status"] = "EXCEPTION"
        result["error_message"] = str(e)
        logger.warning(f"    Fel vid hämtning av insights för {post_id}: {e}")
    
    return result

def process_posts_with_insights(posts, account_name=None, instagram_account_id=None):
    """
    Bearbeta posts och hämta insights för varje post.
    
    Med modern datumfiltrering och robust fallback bearbetar 
    denna funktion betydligt färre posts per körning, vilket resulterar i 
    dramatiskt snabbare exekvering.
    
    VIKTIGT: Denna funktion gör fortfarande många API-anrop (ett per post för insights).
    Rate limiting och error handling är kritiskt här.
    
    OPTIMERINGAR:
    - 0.5 sekunder paus mellan posts för att vara snäll mot API
    - Robust error handling per post (en misslyckad post stoppar inte resten)
    - Progress-loggning för långa körningar
    
    Args:
        posts: Lista med post-objekt från get_instagram_posts_for_period()
        account_name: Kontonamn för loggning
        instagram_account_id: Instagram Business Account ID (numeriskt)
    
    Returns:
        list: Lista med komplett post-data inklusive insights
    """
    display_name = account_name if account_name else "Unknown"
    logger.info(f"Bearbetar {len(posts)} posts med insights för {display_name}")
    
    complete_posts = []
    success_count = 0
    error_count = 0
    
    for i, post in enumerate(posts):
        try:
            post_id = post.get("id", "")
            media_type = post.get("media_type", "UNKNOWN")
            media_product_type = post.get("media_product_type", "FEED")
            post_date = post.get("post_date", "")
            
            # Progress-loggning var 10:e post för långa körningar
            if (i + 1) % 10 == 0 or i == 0:
                logger.info(f"  Bearbetar post {i+1}/{len(posts)}: {post_id} ({post_date})")
            else:
                logger.debug(f"  Bearbetar post {i+1}/{len(posts)}: {post_id} ({post_date})")
            
            # Hämta insights för denna post
            insights = get_post_insights(post_id, media_type, media_product_type, display_name)
            
            # Förkorta caption för visning (max 200 tecken + "...")
            # CAPTION-HANTERING: Instagram-captions kan vara mycket långa.
            # Vi sparar bara en preview för CSV-läsbarhet.
            caption = post.get("caption", "")
            caption_preview = (caption[:197] + "...") if len(caption) > 200 else caption
            
            # Skapa komplett post-record
            # KOLUMNORDNING: Följer användarens specificerade ordning för CSV
            complete_post = {
                "Account": display_name,
                "Instagram_ID": instagram_account_id if instagram_account_id else "",
                "Post_ID": post_id,
                "Post_Date": post_date,
                "Post_URL": post.get("permalink", ""),  # NY i v4.2: Lägg till post-URL
                "Media_Type": media_type,           # IMAGE, VIDEO, CAROUSEL_ALBUM
                "Media_Product_Type": media_product_type,  # FEED eller REELS
                "Caption_Preview": caption_preview,
                # Metriker i den ordning användaren specificerat: reach, comments, likes, shares, saved, views
                "Reach": insights["reach"],
                "Comments": insights["comments"], 
                "Likes": insights["likes"],
                "Shares": insights["shares"],
                "Saved": insights["saved"],
                "Views": insights["views"],
                "Status": insights["status"],
                "Error_Message": insights.get("error_message", "")
            }
            
            complete_posts.append(complete_post)
            
            if insights["status"] == "OK":
                success_count += 1
            else:
                error_count += 1
                
            # Kort paus mellan posts för att vara snäll mot API:et
            # RATE LIMITING: Instagram API är striktare än Facebook API
            if i < len(posts) - 1:  # Inte för sista posten
                time.sleep(0.5)  # 0.5 sekunder mellan posts
                
        except Exception as e:
            logger.error(f"  Fel vid bearbetning av post {i+1}: {e}")
            error_count += 1
            continue
    
    logger.info(f"Slutresultat för {display_name}: {success_count} lyckade, {error_count} fel")
    return complete_posts

def safe_int_value(value, default=0):
    """
    Säkerställer att ett värde är ett heltal, och hanterar olika datatyper.
    
    Används för att hantera CSV-data som kan vara strängar, None, etc.
    """
    if isinstance(value, (int, float)):
        return int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        return int(value)
    else:
        return default

# ===================================================================================
# HÄR SLUTAR DEL 2 - Post-hämtning och Insights-integrering (ROBUST v4.2)
# ===================================================================================
# ===================================================================================
# HÄR BÖRJAR DEL 3 - CSV-hantering, huvudkörning och kommandoradsargument
# ===================================================================================

def ensure_csv_with_headers(filename):
    if not os.path.exists(filename):
        fieldnames = [
            "Account", "Instagram_ID", "Post_ID", "Post_Date", "Post_URL", "Media_Type", "Media_Product_Type", "Caption_Preview",
            "Reach", "Comments", "Likes", "Shares", "Saved", "Views", "Status", "Error_Message"
        ]
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
        logger.debug(f"Skapade CSV med headers: {filename}")

def append_posts_to_csv(filename, posts_data):
    if not posts_data:
        return 0
    
    try:
        ensure_csv_with_headers(filename)
        
        sorted_posts = sorted(posts_data, 
                             key=lambda x: (x.get("Account", ""), x.get("Post_Date", "")))
        
        fieldnames = [
            "Account", "Instagram_ID", "Post_ID", "Post_Date", "Post_URL", "Media_Type", "Media_Product_Type", "Caption_Preview",
            "Reach", "Comments", "Likes", "Shares", "Saved", "Views", "Status", "Error_Message"
        ]
        
        with open(filename, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writerows(sorted_posts)
        
        logger.info(f"Append: +{len(sorted_posts)} posts → {filename}")
        return len(sorted_posts)
        
    except Exception as e:
        logger.error(f"Append-fel {filename}: {e}")
        return 0

def process_account_posts_for_month(instagram_id, account_name, year, month):
    start_date = f"{year}-{month:02d}-01"
    last_day = monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day}"
    output_file = f"IG_Posts_{year}_{month:02d}.csv"
    
    logger.info(f"Bearbetar posts för @{account_name}: {year}-{month:02d}")
    
    try:
        posts = get_instagram_posts_for_period(instagram_id, start_date, end_date, account_name)
        
        if not posts:
            logger.info(f"  Inga posts för @{account_name}")
            return 0, 0, 0
        
        complete_posts = process_posts_with_insights(posts, account_name, instagram_id)
        
        if not complete_posts:
            logger.warning(f"  Inga bearbetade posts för @{account_name}")
            return 0, 0, 0
        
        written = append_posts_to_csv(output_file, complete_posts)
        show_posts_summary(complete_posts, account_name, year, month)
        
        success_count = len([p for p in complete_posts if p.get("Status") == "OK"])
        error_count = len(complete_posts) - success_count
        
        return success_count, error_count, written
            
    except Exception as e:
        logger.error(f"Fel vid bearbetning av @{account_name}: {e}")
        return 0, 1, 0

def show_posts_summary(posts_data, account_name, year, month):
    try:
        if not posts_data:
            return
            
        display_name = account_name if account_name else "Unknown"
        ok_posts = [p for p in posts_data if p.get("Status") == "OK"]
        
        if ok_posts:
            total_comments = sum(safe_int_value(p.get("Comments", 0)) for p in ok_posts)
            total_likes = sum(safe_int_value(p.get("Likes", 0)) for p in ok_posts)
            total_shares = sum(safe_int_value(p.get("Shares", 0)) for p in ok_posts)
            total_saved = sum(safe_int_value(p.get("Saved", 0)) for p in ok_posts)
            
            avg_reach = sum(safe_int_value(p.get("Reach", 0)) for p in ok_posts) / len(ok_posts)
            avg_views = sum(safe_int_value(p.get("Views", 0)) for p in ok_posts) / len(ok_posts)
            
            logger.info(f"Summering för @{display_name} - {year}-{month:02d}:")
            logger.info(f"  - Totaler över {len(ok_posts)} posts (summerbara metriker):")
            logger.info(f"    • Comments: {total_comments:,}")
            logger.info(f"    • Likes: {total_likes:,}")
            logger.info(f"    • Shares: {total_shares:,}")
            logger.info(f"    • Saved: {total_saved:,}")
            logger.info(f"  - Genomsnitt per post (unika metriker):")
            logger.info(f"    • Reach: {avg_reach:.0f}")
            logger.info(f"    • Views: {avg_views:.0f}")
            logger.info(f"    • Likes per post: {total_likes / len(ok_posts):.0f}")
        
        type_counts = {}
        for post in posts_data:
            post_type = f"{post.get('Media_Type', 'UNKNOWN')}/{post.get('Media_Product_Type', 'FEED')}"
            type_counts[post_type] = type_counts.get(post_type, 0) + 1
        
        if type_counts:
            logger.info(f"  - Post-typer:")
            for post_type, count in sorted(type_counts.items()):
                percentage = (count / len(posts_data)) * 100
                logger.info(f"    • {post_type}: {count} posts ({percentage:.1f}%)")
        
        status_counts = {}
        error_details = {}
        
        for post in posts_data:
            status = post.get("Status", "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1
            
            if status != "OK":
                error_msg = post.get("Error_Message", "Okänt fel")
                if error_msg not in error_details:
                    error_details[error_msg] = 0
                error_details[error_msg] += 1
        
        if len(status_counts) > 1 or "OK" not in status_counts:
            logger.info(f"  - Status-översikt:")
            for status, count in status_counts.items():
                logger.info(f"    • {status}: {count} posts")
                
            if error_details:
                logger.info(f"  - Fel-detaljer:")
                for error_msg, count in sorted(error_details.items()):
                    logger.info(f"    • {error_msg}: {count} poster")
        
    except Exception as e:
        logger.error(f"Fel vid summering av posts: {e}")

def get_existing_post_reports():
    existing_reports = set()
    
    for filename in glob.glob("IG_Posts_*.csv"):
        try:
            parts = filename.replace(".csv", "").split("_")
            if len(parts) == 4 and parts[0] == "IG" and parts[1] == "Posts":
                year = parts[2]
                month = parts[3]
                
                if year.isdigit() and month.isdigit():
                    if len(year) == 4 and len(month) == 2:
                        existing_reports.add(f"{year}-{month}")
                        logger.debug(f"Hittade befintlig post-rapport för {year}-{month}: {filename}")
                        
        except Exception as e:
            logger.warning(f"Kunde inte tolka post-filnamn {filename}: {e}")
            
    return existing_reports

def get_missing_months_for_posts(existing_reports, start_year_month):
    missing_months = []
    
    start_year, start_month = map(int, start_year_month.split("-"))
    
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    
    year = start_year
    month = start_month
    
    while (year < current_year) or (year == current_year and month < current_month):
        month_str = f"{year}-{month:02d}"
        if month_str not in existing_reports:
            missing_months.append((year, month))
        
        month += 1
        if month > 12:
            month = 1
            year += 1
    
    return missing_months

def process_all_accounts_for_month(account_list, year, month, update_existing=False):
    logger.info(f"Bearbetar alla konton för {year}-{month:02d}...")
    
    output_file = f"IG_Posts_{year}_{month:02d}.csv"
    total_success = 0
    total_errors = 0
    total_posts = 0
    
    if not update_existing and os.path.exists(output_file):
        os.remove(output_file)
        logger.info(f"Tog bort befintlig {output_file} för fresh start")
    
    for i, (instagram_id, account_name, facebook_page) in enumerate(account_list):
        logger.info(f"Konto {i+1}/{len(account_list)}: @{account_name}")
        
        try:
            success, errors, written = process_account_posts_for_month(instagram_id, account_name, year, month)
            
            total_success += success
            total_errors += errors
            total_posts += written
            
            if i < len(account_list) - 1:
                time.sleep(2)
                
        except Exception as e:
            logger.error(f"Fel vid bearbetning av @{account_name}: {e}")
            total_errors += 1
    
    logger.info(f"Månadsresultat {year}-{month:02d}: {total_success} lyckade, {total_errors} fel, {total_posts} totalt")
    
    if total_success == 0 and total_posts == 0:
        logger.info(f"Inga poster för {year}-{month:02d} – skapar tom CSV")
        ensure_csv_with_headers(output_file)
    
    return total_success, total_errors, total_posts

def main():
    parser = argparse.ArgumentParser(
        description="Instagram post-nivå analytics v4.2 (med Post-URL:er)",
        epilog="Exempel: python fetch_instagram_posts.py --month 2025-08"
    )
    
    date_group = parser.add_argument_group("Datumargument för månader")
    date_group.add_argument("--start", help="Startår-månad (YYYY-MM)")
    date_group.add_argument("--month", help="Kör endast för angiven månad (YYYY-MM)")
    
    ops_group = parser.add_argument_group("Operationsmodifikatorer")
    ops_group.add_argument("--update-all", action="store_true", 
                          help="Uppdatera alla posts även om de redan finns i CSV-filen")
    ops_group.add_argument("--debug", action="store_true", 
                          help="Aktivera debug-loggning")
    ops_group.add_argument("--media-types", choices=["feed", "reels", "all"], default="all",
                          help="Vilka mediatyper som ska inkluderas (feed/reels/all)")
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug-läge aktiverat")
    
    if args.start and args.month:
        logger.error("Använd antingen --start eller --month, inte båda")
        parser.print_help()
        sys.exit(1)
    
    start_year_month = args.start or INITIAL_START_YEAR_MONTH
    
    logger.info("Instagram Post Analytics v4.2 - Med Post-URL:er i CSV")
    logger.info(f"Startdatum: {start_year_month}")
    logger.info(f"Python-version: {sys.version}")
    logger.info(f"Mediatyp-filter: {args.media_types}")
    
    check_token_expiry()
    
    if not validate_token(ACCESS_TOKEN):
        logger.error("Token kunde inte valideras. Avbryter.")
        return
    
    cache = load_account_cache()
    account_list = get_instagram_accounts_with_access(ACCESS_TOKEN)
    
    if not account_list:
        logger.error("Inga Instagram-konton hittades. Avbryter.")
        return
    
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
            logger.info(f"Kör endast för specifik månad: {year}-{month:02d}")
            
            start_time_month = time.time()
            success, errors, posts = process_all_accounts_for_month(
                account_list, year, month, args.update_all
            )
            elapsed_time_month = time.time() - start_time_month
            
            save_account_cache(cache)
            logger.info(f"Klart för {year}-{month:02d}: {success} lyckade posts, {errors} fel i {elapsed_time_month:.1f} sekunder")
            return
            
        except ValueError:
            logger.error(f"Ogiltigt månadsformat: {args.month}. Använd YYYY-MM.")
            return
    
    existing_reports = get_existing_post_reports()
    logger.info(f"Hittade {len(existing_reports)} befintliga post-rapporter: {', '.join(sorted(existing_reports)) if existing_reports else 'Inga'}")
    
    missing_months = get_missing_months_for_posts(existing_reports, start_year_month)
    
    if not missing_months:
        logger.info("Alla månader är redan bearbetade för posts. Inget att göra.")
        logger.info("Använd --month YYYY-MM för att köra specifik månad eller --update-all för att uppdatera.")
        return
    
    logger.info(f"Behöver bearbeta {len(missing_months)} saknade månader: {', '.join([f'{y}-{m:02d}' for y, m in missing_months])}")
    
    total_success_all = 0
    total_errors_all = 0
    total_posts_all = 0
    
    for i, (year, month) in enumerate(missing_months):
        logger.info(f"Bearbetar månad {i+1}/{len(missing_months)}: {year}-{month:02d}")
        
        try:
            month_start_time = time.time()
            success, errors, posts = process_all_accounts_for_month(
                account_list, year, month, args.update_all
            )
            month_elapsed = time.time() - month_start_time
            
            total_success_all += success
            total_errors_all += errors  
            total_posts_all += posts
            
            logger.info(f"  Månad {year}-{month:02d} slutförd på {month_elapsed:.1f} sekunder")
            
            save_account_cache(cache)
            
            if i < len(missing_months) - 1:
                if rate_limit_backoff > 1.5:
                    pause_time = min(MONTH_PAUSE_SECONDS, 60)
                    logger.info(f"Pausar i {pause_time} sekunder mellan månader (pga rate limits)...")
                    time.sleep(pause_time)
                else:
                    logger.info("Fortsätter direkt till nästa månad...")
                    
        except Exception as e:
            logger.error(f"Fel vid bearbetning av månad {year}-{month:02d}: {e}")
            total_errors_all += 1
            continue
    
    elapsed_time = time.time() - start_time
    avg_rate = api_call_count / (elapsed_time / 3600) if elapsed_time > 0 else 0
    
    logger.info("-------------------------------------------------------------------")
    logger.info("SLUTRESULTAT v4.2:")
    logger.info(f"  - Månader bearbetade: {len(missing_months)}")
    logger.info(f"  - Posts framgångsrikt bearbetade: {total_success_all}")
    logger.info(f"  - Posts med fel: {total_errors_all}")
    logger.info(f"  - Totalt posts: {total_posts_all}")
    logger.info(f"  - Total körtid: {elapsed_time:.1f} sekunder")
    logger.info(f"  - API-anrop: {api_call_count} totalt")
    logger.info(f"  - Genomsnittlig hastighet: {avg_rate:.0f} anrop/timme")
    
    if rate_limit_backoff > 1.0:
        logger.info(f"  - Slutlig backoff: {rate_limit_backoff:.1f}x (träffade rate limits)")
    else:
        logger.info("  - Inga rate limits träffades")
        
    logger.info("Klar med Instagram post-analytics v4.2!")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Avbruten av användare. CSV-data fram till senaste konto är säkrad.")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Oväntat fel: {e}")
        import traceback
        logger.critical(traceback.format_exc())
        sys.exit(1)

# ===================================================================================
# HÄR SLUTAR DEL 3 - CSV-hantering, huvudkörning och kommandoradsargument
# ===================================================================================