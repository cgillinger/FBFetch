# fetch_instagram_posts_v4_6.py
# Version 4.6 - Kritisk Views-fix för Reels och Feed
# 
# Detta skript hämtar detaljerad statistik för alla Instagram-inlägg under en vald tidsperiod.
# Fokuserar på post-nivå metriker som är tillförlitliga från Instagram Graph API.
#
# VERSION 4.6 KRITISKA FÖRBÄTTRINGAR:
# - API-version uppgraderad till v22.0 för konsekvent Views-data från Reels
# - Separerad metrik-strategi per mediatyp (REELS vs FEED)
# - Stegvis fallback-system för att maximera Views-utvinning
# - Ny CSV-kolumn "Views_Source" för diagnostik
# - Förbättrad felhantering och diagnostik
#
# VIKTIGA NOTERINGAR:
# - Kräver Instagram Business eller Creator-konto
# - Kräver API-version v22.0+ för optimal Views-täckning
# - Post-nivå data är betydligt mer tillförlitlig än konto-aggregat
# - Kräver Python ≥3.9 för zoneinfo-stöd

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

# FEATURE TOGGLES - v4.6
ENABLE_MEDIA_FOLLOWS = True  # Sätt till False om Meta helt avvecklar 'follows' metriken

# ===================================================================================
# HÄR BÖRJAR DEL 1 - Grundläggande funktioner och API-hantering (v4.6)
# ===================================================================================

def setup_logging():
    """
    Konfigurera loggning med datumstämplad loggfil och UTF-8 encoding.
    """
    now = datetime.now()
    log_dir = "logs"
    
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    log_filename = os.path.join(log_dir, f"instagram_posts_{now.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding="utf-8"),
            logging.FileHandler("instagram_posts.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Startar Instagram Post Analytics v4.6 - loggning till: {log_filename}")
    
    return logger

# Konfigurera loggning
logger = setup_logging()

# Räknare för API-anrop och rate limit-hantering
api_call_count = 0
start_time = time.time()
last_rate_limit_time = None
rate_limit_backoff = 1.0
consecutive_successes = 0

# Statistik för 'follows' metrik
follows_success_count = 0
follows_fallback_count = 0

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
    Gör API-förfrågan med dynamisk rate limit-hantering för Instagram API v22+.
    
    v4.6: Optimerad för nya API-versioner med förbättrad felhantering
    """
    global api_call_count, last_rate_limit_time, rate_limit_backoff, consecutive_successes
    
    if last_rate_limit_time:
        time_since_limit = time.time() - last_rate_limit_time
        if time_since_limit < (60 * rate_limit_backoff):
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
            
            if response.status_code == 429:
                last_rate_limit_time = time.time()
                rate_limit_backoff = min(rate_limit_backoff * 1.5, 10.0)
                consecutive_successes = 0
                
                retry_after = int(response.headers.get('Retry-After', 60 * rate_limit_backoff))
                logger.warning(f"Rate limit nått! Väntar {retry_after}s (backoff: {rate_limit_backoff:.1f}x)")
                time.sleep(retry_after)
                continue
                
            elif response.status_code >= 500:
                wait_time = min(RETRY_DELAY * (2 ** attempt), 30)
                logger.warning(f"Serverfel: {response.status_code}. Väntar {wait_time}s... (försök {attempt+1}/{retries})")
                time.sleep(wait_time)
                continue
            
            try:
                json_data = response.json()
                
                if response.status_code == 400 and "error" in json_data:
                    error_code = json_data["error"].get("code")
                    error_msg = json_data["error"].get("message", "Okänt fel")
                    
                    if error_code == 4:
                        last_rate_limit_time = time.time()
                        rate_limit_backoff = min(rate_limit_backoff * 1.5, 10.0)
                        wait_time = min(60 * rate_limit_backoff, 300)
                        logger.warning(f"App rate limit: {error_msg}. Väntar {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                        
                    elif error_code == 190:
                        logger.error(f"Access token ogiltig: {error_msg}")
                        return None
                    
                    elif error_code == 100:
                        logger.error(f"Instagram API fel: {error_msg}")
                        return None
                
                if response.status_code != 200:
                    logger.error(f"HTTP-fel {response.status_code}: {response.text}")
                    
                    if attempt < retries - 1:
                        wait_time = RETRY_DELAY * (2 ** attempt)
                        logger.info(f"Väntar {wait_time} sekunder innan nytt försök... (försök {attempt+1}/{retries})")
                        time.sleep(wait_time)
                        continue
                    
                    return json_data
                
                consecutive_successes += 1
                
                if consecutive_successes >= 50 and rate_limit_backoff > 1.0:
                    rate_limit_backoff = max(rate_limit_backoff * 0.8, 1.0)
                    logger.debug(f"50 lyckade anrop, minskar backoff till {rate_limit_backoff:.1f}x")
                    consecutive_successes = 0
                
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
        
        for page in pages:
            instagram_data = page.get("instagram_business_account")
            if instagram_data:
                instagram_id = instagram_data.get("id")
                if instagram_id:
                    ig_name = get_instagram_account_name(instagram_id, token)
                    if ig_name:
                        instagram_accounts.append((instagram_id, ig_name, page.get("name", "Okänd Facebook-sida")))
                        logger.debug(f"Hittade Instagram-konto: {ig_name} (ID: {instagram_id})")
        
        next_url = data.get("paging", {}).get("next")
        if next_url and next_url != url:
            logger.debug(f"Hämtar nästa sida av Facebook-sidor...")
        else:
            break
    
    if not instagram_accounts:
        logger.warning("Inga Instagram Business/Creator-konton hittades.")
        logger.info("Kontrollera att:")
        logger.info("1. Dina Instagram-konton är Business eller Creator-konton")
        logger.info("2. De är kopplade till Facebook-sidor du har åtkomst till")
        logger.info("3. Token har rätt behörigheter (instagram_basic, instagram_manage_insights)")
    
    logger.info(f"Hittade {len(instagram_accounts)} Instagram-konton att analysera")
    return instagram_accounts

def get_instagram_account_name(instagram_id, token):
    """
    Hämta Instagram-kontonamn från ID.
    """
    url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}"
    params = {"fields": "name,username", "access_token": token}
    
    data = api_request(url, params)
    
    if not data or "error" in data:
        error_msg = data.get("error", {}).get("message", "Okänt fel") if data else "Fel vid API-anrop"
        logger.warning(f"Kunde inte hämta namn för Instagram-konto {instagram_id}: {error_msg}")
        return None
    
    name = data.get("username") or data.get("name", f"IG_{instagram_id}")
    return name

# ===================================================================================
# HÄR SLUTAR DEL 1 - Grundläggande funktioner och API-hantering (v4.6)
# ===================================================================================
# ===================================================================================
# HÄR BÖRJAR DEL 2 - Post-hämtning och Insights-integrering (v4.6 KRITISK VIEWS-FIX)
# ===================================================================================

# =========================
# VIEWS HANDLING v4.6 - SEPARERAD STRATEGI PER MEDIATYP
# =========================
VIEWS_FAMILY = ["views", "video_views", "plays"]  # prioritetsordning
BASE_METRICS = ["reach", "comments", "likes", "shares", "saved"]

def get_optimal_metrics_for_media(media_product_type: str, media_type: str) -> list:
    """
    Optimal metriklista per mediatyp för första försöket.
    Separerad strategi för att undvika metrik-konflikter.
    """
    pt = (media_product_type or "FEED").upper()
    mt = (media_type or "IMAGE").upper()
    
    if pt == "REELS":
        # ENDAST views för Reels - undvik konflikter med plays/video_views
        return BASE_METRICS + ["views"]
    
    if pt == "FEED":
        if mt == "VIDEO":
            # Feed-video: views + video_views som fallback
            return BASE_METRICS + ["views", "video_views"]
        else:
            # Feed-bild/karusell: endast views
            return BASE_METRICS + ["views"]
    
    return BASE_METRICS.copy()

def get_fallback_metrics_for_media(media_product_type: str, media_type: str) -> list:
    """
    Fallback-metriker vid 400/#100 fel - endast basmetriker + views
    """
    pt = (media_product_type or "FEED").upper()
    
    if pt == "REELS":
        return BASE_METRICS + ["views"]
    elif pt == "FEED":
        return BASE_METRICS + ["views"]
    
    return BASE_METRICS.copy()

def get_minimal_metrics() -> list:
    """
    Minimal metriklista vid upprepade fel - endast basmetriker
    """
    return BASE_METRICS.copy()

def safe_media_insights_v46(media_id: str, media_product_type: str, media_type: str, access_token: str, api_version: str):
    """
    v4.6 KRITISK FIX: Stegvis fallback-strategi för att maximera Views-data från Reels.
    
    Strategi:
    1) Försök optimal metriklista per mediatyp
    2) Vid 400/#100: försök fallback med endast views + basmetriker  
    3) Vid fortsatt fel: minimal lista utan views
    """
    import requests
    
    def _call_api(metric_list: list):
        url = f"https://graph.facebook.com/{api_version}/{media_id}/insights"
        params = {"metric": ",".join(metric_list), "access_token": access_token}
        r = requests.get(url, params=params, timeout=60)
        try:
            data = r.json()
        except Exception:
            data = {"error": {"message": f"Non-JSON response (status={r.status_code})"}}
        return r.status_code, data

    # STEG 1: Optimal metriklista
    optimal_metrics = get_optimal_metrics_for_media(media_product_type, media_type)
    status, data = _call_api(optimal_metrics)
    
    if status == 200 and isinstance(data, dict) and "data" in data:
        logger.debug(f"    ✓ Optimal metriklista lyckades: {', '.join(optimal_metrics)}")
        return data

    # STEG 2: Fallback vid 400/#100
    if status == 400 and isinstance(data, dict):
        err = data.get("error", {})
        if err.get("code") == 100:
            logger.debug(f"    ⚠ #100 med optimal lista, försöker fallback...")
            
            fallback_metrics = get_fallback_metrics_for_media(media_product_type, media_type)
            status2, data2 = _call_api(fallback_metrics)
            
            if status2 == 200 and isinstance(data2, dict) and "data" in data2:
                logger.debug(f"    ✓ Fallback lyckades: {', '.join(fallback_metrics)}")
                return data2
            
            # STEG 3: Minimal lista utan views
            logger.debug(f"    ⚠ Fallback misslyckades, försöker minimal lista...")
            minimal_metrics = get_minimal_metrics()
            status3, data3 = _call_api(minimal_metrics)
            
            if status3 == 200 and isinstance(data3, dict) and "data" in data3:
                logger.debug(f"    ✓ Minimal lista lyckades: {', '.join(minimal_metrics)}")
                return data3

    # Returnera ursprungligt fel för loggning
    logger.warning(f"    ✗ Alla fallback-strategier misslyckades för {media_id}")
    return data

def extract_views_from_insights_v46(insights_json: dict) -> tuple[int, str]:
    """
    v4.6: Extrahera Views med prioritet och källa-spårning
    Returnerar (value, source_metric)
    """
    if not isinstance(insights_json, dict):
        return 0, ""
    
    values = {}
    for m in insights_json.get("data", []):
        name = (m.get("name") or "").lower()
        vals = m.get("values", [])
        if vals:
            try:
                values[name] = int(vals[0].get("value", 0) or 0)
            except (TypeError, ValueError):
                values[name] = 0
    
    # Prioritetsordning: views > video_views > plays
    for key in ["views", "video_views", "plays"]:
        v = values.get(key, 0)
        if isinstance(v, int) and v > 0:
            return v, key
    
    return 0, ""

def get_instagram_posts_for_period(instagram_id, since_date, until_date, account_name=None):
    """
    Hämta alla Instagram-posts för en specifik tidsperiod med robust datumfiltrering.
    
    v4.6: Förbättrad diagnostik för Views-problemanalys
    """
    display_name = account_name if account_name else instagram_id
    logger.info(f"Hämtar posts för {display_name} från {since_date} till {until_date} (v4.6)")
    
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
        
        logger.debug(f"  Tidszonkonvertering (halvöppet intervall):")
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
                
                # Analys per mediatyp för diagnostik
                type_counts = {}
                for p in posts:
                    media_type = p.get("media_type", "UNKNOWN")
                    product_type = p.get("media_product_type", "FEED")
                    key = f"{product_type}/{media_type}"
                    type_counts[key] = type_counts.get(key, 0) + 1
                
                logger.info(f"  Post-fördelning: {dict(type_counts)}")
        
        return posts
        
    except Exception as e:
        logger.error(f"  Fel vid post-hämtning för {display_name}: {e}")
        return []

def attempt_server_side_filtering(instagram_id, since_epoch, until_epoch, display_name, start_sweden, next_day_sweden):
    """
    Försök server-side filtrering med epoch-tidsstämplar
    """
    
    url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}/media"
    params = {
        "access_token": ACCESS_TOKEN,
        "since": since_epoch,
        "until": until_epoch,
        "limit": 100,
        "fields": "id,timestamp,media_type,media_product_type,caption,permalink"
    }
    
    try:
        posts = []
        page_num = 0
        total_posts_found = 0
        posts_in_period = 0
        posts_outside_period = 0
        
        logger.debug(f"  Försöker server-side filtrering...")
        
        while url and page_num < 100:
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
                
                if any(term in error_msg.lower() for term in ["since", "until", "parameter", "unsupported"]):
                    logger.warning(f"    Server-side filtrering stöds ej: {error_msg}")
                    return None
                else:
                    logger.error(f"    API-fel: {error_msg}")
                    return None
            else:
                logger.warning(f"    Inget data returnerat")
                break
        
        logger.info(f"  ✓ Server-side resultat för {display_name}:")
        logger.info(f"    • {posts_in_period} posts inom period")
        logger.info(f"    • {page_num} sidor paginerade")
        
        return posts
        
    except Exception as e:
        logger.warning(f"    Server-side filtrering misslyckades: {e}")
        return None

def fetch_with_client_filter(instagram_id, start_sweden, next_day_sweden, display_name):
    """
    Fallback med client-side filtrering
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
    consecutive_pages_without_hits = 0
    
    while url and page_num < 200:
        page_num += 1
        page_hits = 0
        
        data = api_request(url, params)
        
        if data and "data" in data:
            media_in_page = data["data"]
            
            if len(media_in_page) == 0:
                consecutive_pages_without_hits += 1
                if consecutive_pages_without_hits >= 2:
                    break
                continue
            
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
                                consecutive_pages_without_hits = 0
                        elif post_sweden < start_sweden:
                            oldest_post_before_period = True
                            
                    except Exception as e:
                        logger.debug(f"      Fel vid tidskonvertering: {e}")
                        continue
            
            if page_hits == 0:
                consecutive_pages_without_hits += 1
                if consecutive_pages_without_hits >= 2 and oldest_post_before_period:
                    break
            
            # Fortsätt paginering
            paging = data.get("paging", {})
            url = paging.get("next")
            params = {} if url else params
            
        else:
            break
    
    logger.info(f"  ✓ Client-side resultat: {len(posts)} posts, {page_num} sidor")
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
            logger.debug(f"      [-] Filtrerad post-typ: {media_product_type}")
            return None
            
    except Exception as e:
        logger.debug(f"      Fel vid post-bearbetning: {e}")
        return None

def get_post_insights(post_id, media_type, media_product_type, account_name=None):
    """
    v4.6 KRITISK FIX: Hämta insights med separerad strategi per mediatyp.
    
    Stora förbättringar:
    - Separerad metrik-strategi för REELS vs FEED
    - Stegvis fallback för att maximera Views-data
    - Views_Source spårning för diagnostik
    """
    global follows_success_count, follows_fallback_count
    
    display_name = account_name if account_name else "Unknown"
    logger.debug(f"Hämtar insights för post {post_id} ({media_type}/{media_product_type}) - {display_name}")
    
    # Skapa resultatstruktur med standardvärden
    result = {
        "reach": 0,
        "comments": 0,
        "likes": 0,
        "follows": 0,
        "shares": 0,
        "saved": 0,
        "views": 0,
        "views_source": "",
        "status": "OK",
        "error_message": ""
    }
    
    try:
        # v4.6: Använd nya stegvisa fallback-strategin
        data = safe_media_insights_v46(post_id, media_product_type, media_type, ACCESS_TOKEN, API_VERSION)
        
        if data and "data" in data:
            # Parserera insights-data
            for metric_data in data["data"]:
                metric_name = metric_data.get("name", "")
                values = metric_data.get("values", [])
                
                if values and len(values) > 0:
                    metric_value = values[0].get("value", 0)
                    if metric_name in result:
                        result[metric_name] = metric_value
                        if metric_value > 0:
                            logger.debug(f"    {metric_name}: {metric_value}")
            
            # v4.6: Extrahera Views med källa-spårning
            views_value, views_source = extract_views_from_insights_v46(data)
            result["views"] = views_value
            result["views_source"] = views_source
            
            # Logga Views-källa för diagnostik
            if views_value > 0:
                logger.debug(f"    Views: {views_value} (från '{views_source}')")
            elif media_product_type == "REELS":
                logger.warning(f"    REELS utan Views-data: {post_id} - kontrollera API-version")
            
            # Hantera follows för FEED (uteslut för REELS)
            include_follows = ENABLE_MEDIA_FOLLOWS and (media_product_type or "").upper() == "FEED"
            if include_follows and result.get("follows", 0) >= 0:
                follows_success_count += 1
            
            logger.debug(f"    Slutresultat för {post_id}: reach={result['reach']}, likes={result['likes']}, views={result['views']}")
                        
        elif data and "error" in data:
            error_msg = data["error"].get("message", "Okänt fel")
            error_code = data["error"].get("code", "N/A")
            
            result["status"] = "API_ERROR"
            result["error_message"] = f"Error {error_code}: {error_msg}"
            logger.warning(f"    Insights-fel för {post_id}: {error_msg}")
        else:
            result["status"] = "NO_DATA"
            result["error_message"] = "Inget insights-data returnerat"
            logger.debug(f"    Inget insights-data för {post_id}")
            
    except Exception as e:
        result["status"] = "EXCEPTION"
        result["error_message"] = str(e)
        logger.warning(f"    Undantag vid insights för {post_id}: {e}")
    
    return result

def process_posts_with_insights(posts, account_name=None, instagram_account_id=None):
    """
    v4.6: Bearbeta posts med förbättrad Views-diagnostik
    """
    display_name = account_name if account_name else "Unknown"
    logger.info(f"Bearbetar {len(posts)} posts med insights för {display_name} (v4.6)")
    
    complete_posts = []
    success_count = 0
    error_count = 0
    
    # v4.6: Utökad Views-statistik
    views_stats = {"views": 0, "video_views": 0, "plays": 0, "none": 0}
    reels_with_views = 0
    feed_with_views = 0
    
    for i, post in enumerate(posts):
        try:
            post_id = post.get("id", "")
            media_type = post.get("media_type", "UNKNOWN")
            media_product_type = post.get("media_product_type", "FEED")
            post_date = post.get("post_date", "")
            
            # Progress-loggning var 10:e post
            if (i + 1) % 10 == 0 or i == 0:
                logger.info(f"  Bearbetar post {i+1}/{len(posts)}: {post_id} ({post_date})")
            else:
                logger.debug(f"  Bearbetar post {i+1}/{len(posts)}: {post_id} ({post_date})")
            
            # Hämta insights med v4.6 förbättrad strategi
            insights = get_post_insights(post_id, media_type, media_product_type, display_name)
            
            # Förkorta caption för visning
            caption = post.get("caption", "")
            caption_preview = (caption[:197] + "...") if len(caption) > 200 else caption
            
            # Skapa komplett post-record med ny Views_Source kolumn
            complete_post = {
                "Account": display_name,
                "Instagram_ID": instagram_account_id if instagram_account_id else "",
                "Post_ID": post_id,
                "Post_Date": post_date,
                "Post_URL": post.get("permalink", ""),
                "Media_Type": media_type,
                "Media_Product_Type": media_product_type,
                "Caption_Preview": caption_preview,
                "Reach": insights["reach"],
                "Comments": insights["comments"], 
                "Likes": insights["likes"],
                "Follows": insights["follows"],
                "Shares": insights["shares"],
                "Saved": insights["saved"],
                "Views": insights["views"],
                "Views_Source": insights["views_source"],
                "Status": insights["status"],
                "Error_Message": insights.get("error_message", "")
            }
            
            complete_posts.append(complete_post)
            
            # Samla Views-statistik för diagnostik
            views_source = insights.get("views_source", "")
            if views_source:
                views_stats[views_source] = views_stats.get(views_source, 0) + 1
                
                if media_product_type == "REELS":
                    reels_with_views += 1
                elif media_product_type == "FEED":
                    feed_with_views += 1
            else:
                views_stats["none"] += 1
            
            if insights["status"] == "OK":
                success_count += 1
            else:
                error_count += 1
                
            # Paus mellan posts
            if i < len(posts) - 1:
                time.sleep(0.5)
                
        except Exception as e:
            logger.error(f"  Fel vid bearbetning av post {i+1}: {e}")
            error_count += 1
            continue
    
    # v4.6: KRITISK DIAGNOSTIK för Views-fix
    logger.info(f"=== v4.6 VIEWS-DIAGNOSTIK för {display_name} ===")
    logger.info(f"Slutresultat: {success_count} lyckade, {error_count} fel")
    logger.info(f"Views-källor: {dict(views_stats)}")
    logger.info(f"REELS med Views: {reels_with_views}/{sum(1 for p in posts if p.get('media_product_type') == 'REELS')}")
    logger.info(f"FEED med Views: {feed_with_views}/{sum(1 for p in posts if p.get('media_product_type') == 'FEED')}")
    
    if reels_with_views == 0 and any(p.get('media_product_type') == 'REELS' for p in posts):
        logger.error("⚠ KRITISKT: Inga REELS fick Views-data - kontrollera API-version och token-behörigheter!")
    
    return complete_posts

def safe_int_value(value, default=0):
    """
    Säkerställer att ett värde är ett heltal
    """
    if isinstance(value, (int, float)):
        return int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        return int(value)
    else:
        return default

# ===================================================================================
# HÄR SLUTAR DEL 2 - Post-hämtning och Insights-integrering (v4.6 KRITISK VIEWS-FIX)
# ===================================================================================
# ===================================================================================
# HÄR BÖRJAR DEL 3 - CSV-hantering, huvudkörning och kommandoradsargument (v4.6)
# ===================================================================================

def ensure_csv_with_headers(filename):
    """Skapa CSV med headers inklusive ny Views_Source kolumn för v4.6"""
    if not os.path.exists(filename):
        fieldnames = [
            "Account", "Instagram_ID", "Post_ID", "Post_Date", "Post_URL", 
            "Media_Type", "Media_Product_Type", "Caption_Preview",
            "Reach", "Comments", "Likes", "Follows", "Shares", "Saved", 
            "Views", "Views_Source", "Status", "Error_Message"
        ]
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
        logger.debug(f"Skapade CSV med v4.6 headers: {filename}")

def append_posts_to_csv(filename, posts_data):
    """Spara posts till CSV med v4.6 förbättringar"""
    if not posts_data:
        return 0
    
    try:
        ensure_csv_with_headers(filename)
        
        sorted_posts = sorted(posts_data, 
                             key=lambda x: (x.get("Account", ""), x.get("Post_Date", "")))
        
        fieldnames = [
            "Account", "Instagram_ID", "Post_ID", "Post_Date", "Post_URL", 
            "Media_Type", "Media_Product_Type", "Caption_Preview",
            "Reach", "Comments", "Likes", "Follows", "Shares", "Saved", 
            "Views", "Views_Source", "Status", "Error_Message"
        ]
        
        with open(filename, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writerows(sorted_posts)
        
        logger.info(f"Sparade {len(sorted_posts)} posts → {filename}")
        return len(sorted_posts)
        
    except Exception as e:
        logger.error(f"CSV-fel {filename}: {e}")
        return 0

def process_account_posts_for_month(instagram_id, account_name, year, month):
    """Bearbeta posts för ett konto och en månad"""
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
    """Visa summering av posts med v4.6 Views-diagnostik"""
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
            total_follows = sum(safe_int_value(p.get("Follows", 0)) for p in ok_posts)
            total_views = sum(safe_int_value(p.get("Views", 0)) for p in ok_posts)
            
            avg_reach = sum(safe_int_value(p.get("Reach", 0)) for p in ok_posts) / len(ok_posts)
            
            logger.info(f"Summering för @{display_name} - {year}-{month:02d}:")
            logger.info(f"  - Totaler över {len(ok_posts)} posts:")
            logger.info(f"    • Comments: {total_comments:,}")
            logger.info(f"    • Likes: {total_likes:,}")
            logger.info(f"    • Views: {total_views:,}")
            logger.info(f"    • Shares: {total_shares:,}")
            logger.info(f"    • Saved: {total_saved:,}")
            logger.info(f"    • Follows: {total_follows:,}")
            logger.info(f"  - Genomsnitt per post:")
            logger.info(f"    • Reach: {avg_reach:.0f}")
            logger.info(f"    • Views per post: {total_views / len(ok_posts):.0f}")
        
        # v4.6: Views-källor analys
        views_sources = {}
        for post in ok_posts:
            source = post.get("Views_Source", "none")
            if source:
                views_sources[source] = views_sources.get(source, 0) + 1
        
        if views_sources:
            logger.info(f"  - Views-källor (v4.6):")
            for source, count in sorted(views_sources.items()):
                percentage = (count / len(ok_posts)) * 100
                logger.info(f"    • {source}: {count} posts ({percentage:.1f}%)")
        
        # Post-typ analys
        type_counts = {}
        reels_count = 0
        feed_count = 0
        
        for post in posts_data:
            post_type = f"{post.get('Media_Type', 'UNKNOWN')}/{post.get('Media_Product_Type', 'FEED')}"
            type_counts[post_type] = type_counts.get(post_type, 0) + 1
            
            if post.get('Media_Product_Type') == 'REELS':
                reels_count += 1
            elif post.get('Media_Product_Type') == 'FEED':
                feed_count += 1
        
        if type_counts:
            logger.info(f"  - Post-typer:")
            for post_type, count in sorted(type_counts.items()):
                percentage = (count / len(posts_data)) * 100
                logger.info(f"    • {post_type}: {count} posts ({percentage:.1f}%)")
        
        # Status-översikt
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
    """Hitta befintliga post-rapporter"""
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
                        logger.debug(f"Hittade befintlig rapport för {year}-{month}: {filename}")
                        
        except Exception as e:
            logger.warning(f"Kunde inte tolka filnamn {filename}: {e}")
            
    return existing_reports

def get_missing_months_for_posts(existing_reports, start_year_month):
    """Hitta månader som saknar rapporter"""
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
    """Bearbeta alla konton för en månad"""
    logger.info(f"Bearbetar alla konton för {year}-{month:02d} (v4.6)...")
    
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

def show_follows_summary():
    """Visa summering av 'follows' metrik-användning"""
    global follows_success_count, follows_fallback_count
    
    if follows_success_count > 0 or follows_fallback_count > 0:
        logger.info("-------------------------------------------------------------------")
        logger.info("FOLLOWS METRIK SUMMERING:")
        logger.info(f"  - Lyckade 'follows' hämtningar: {follows_success_count}")
        logger.info(f"  - Fallback utan 'follows': {follows_fallback_count}")
        logger.info(f"  - Total posts som begärde 'follows': {follows_success_count + follows_fallback_count}")
        
        if follows_fallback_count > 0:
            fallback_rate = (follows_fallback_count / (follows_success_count + follows_fallback_count)) * 100
            logger.info(f"  - Fallback-rate: {fallback_rate:.1f}%")
        
        if not ENABLE_MEDIA_FOLLOWS:
            logger.info("  - 'follows' metrik är DISABLED via ENABLE_MEDIA_FOLLOWS")

def main():
    """Huvudfunktion för Instagram Post Analytics v4.6"""
    parser = argparse.ArgumentParser(
        description="Instagram Post Analytics v4.6 - Kritisk Views-fix för Reels och Feed",
        epilog="Exempel: python fetch_instagram_posts_v46.py --month 2025-08"
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
        logger.debug("Debug-läge aktiverat för v4.6")
    
    if args.start and args.month:
        logger.error("Använd antingen --start eller --month, inte båda")
        parser.print_help()
        sys.exit(1)
    
    start_year_month = args.start or INITIAL_START_YEAR_MONTH
    
    logger.info("=== Instagram Post Analytics v4.6 - KRITISK VIEWS-FIX ===")
    logger.info(f"API-version: {API_VERSION}")
    logger.info(f"Startdatum: {start_year_month}")
    logger.info(f"Python-version: {sys.version}")
    logger.info(f"Mediatyp-filter: {args.media_types}")
    logger.info(f"ENABLE_MEDIA_FOLLOWS: {ENABLE_MEDIA_FOLLOWS}")
    
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
            show_follows_summary()
            logger.info(f"Klart för {year}-{month:02d}: {success} lyckade posts, {errors} fel i {elapsed_time_month:.1f} sekunder")
            return
            
        except ValueError:
            logger.error(f"Ogiltigt månadsformat: {args.month}. Använd YYYY-MM.")
            return
    
    existing_reports = get_existing_post_reports()
    logger.info(f"Hittade {len(existing_reports)} befintliga rapporter: {', '.join(sorted(existing_reports)) if existing_reports else 'Inga'}")
    
    missing_months = get_missing_months_for_posts(existing_reports, start_year_month)
    
    if not missing_months:
        logger.info("Alla månader är redan bearbetade. Inget att göra.")
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
    
    logger.info("===================================================================")
    logger.info("SLUTRESULTAT v4.6 - KRITISK VIEWS-FIX:")
    logger.info(f"  - Månader bearbetade: {len(missing_months)}")
    logger.info(f"  - Posts framgångsrikt bearbetade: {total_success_all}")
    logger.info(f"  - Posts med fel: {total_errors_all}")
    logger.info(f"  - Totalt posts: {total_posts_all}")
    logger.info(f"  - Total körtid: {elapsed_time:.1f} sekunder")
    logger.info(f"  - API-anrop: {api_call_count} totalt")
    logger.info(f"  - Genomsnittlig hastighet: {avg_rate:.0f} anrop/timme")
    logger.info(f"  - API-version använd: {API_VERSION}")
    
    if rate_limit_backoff > 1.0:
        logger.info(f"  - Slutlig backoff: {rate_limit_backoff:.1f}x (träffade rate limits)")
    else:
        logger.info("  - Inga rate limits träffades")
    
    show_follows_summary()
    logger.info("Klar med Instagram Post Analytics v4.6 - KRITISK VIEWS-FIX!")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Avbruten av användare. CSV-data fram till senaste konto är säkrad.")
        show_follows_summary()
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Oväntat fel: {e}")
        import traceback
        logger.critical(traceback.format_exc())
        show_follows_summary()
        sys.exit(1)

# ===================================================================================
# HÄR SLUTAR DEL 3 - CSV-hantering, huvudkörning och kommandoradsargument (v4.6)
# ===================================================================================