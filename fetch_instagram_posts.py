# fetch_instagram_posts.py
# Version 1.0 - Instagram Post-nivå Analytics
# 
# Detta skript hämtar detaljerad statistik för alla Instagram-inlägg under en vald tidsperiod.
# Fokuserar på post-nivå metriker som är tillförlitliga från Instagram Graph API.
#
# VIKTIGA NOTERINGAR:
# - Kräver Instagram Business eller Creator-konto
# - Endast organiska data (inga annonsdata inkluderas)
# - Post-nivå data är betydligt mer tillförlitlig än konto-aggregat
# - API begränsar till ~25 media per request (kräver paginering)
#
# METRIKER SOM HÄMTAS PER POST:
# - reach: Antal unika konton som sett inlägget (TILLFÖRLITLIG på postnivå)
# - comments: Antal kommentarer på inlägget
# - likes: Antal gilla-markeringar (hjärtan)
# - shares: Antal delningar (främst för Reels, ofta 0 för vanliga posts)
# - saved: Antal gånger inlägget sparats av användare
# - views: Antal visningar för video/Reels (ersätter impressions för nya posts)
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

# Skapa datumstämplad loggfil
def setup_logging():
    """Konfigurera loggning med datumstämplad loggfil"""
    now = datetime.now()
    log_dir = "logs"
    
    # Skapa loggdirektory om den inte finns
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Skapa datumstämplad loggfilnamn
    log_filename = os.path.join(log_dir, f"instagram_posts_{now.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    
    # Konfigurera loggning
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),  # Datumstämplad loggfil
            logging.FileHandler("instagram_posts.log"),  # Behåll den senaste loggfilen för enkelt åtkomst
            logging.StreamHandler()  # Terminal-utskrift
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Startar loggning till fil: {log_filename}")
    
    return logger

# Konfigurera loggning med datumstämplad fil
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
                    
                    # Hantera specifika felkoder
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
                
                # Visa progress var 100:e anrop
                if api_call_count % 100 == 0:
                    elapsed = time.time() - start_time
                    current_rate = api_call_count / (elapsed / 3600) if elapsed > 0 else 0
                    logger.info(f"Progress: {api_call_count} API-anrop, {current_rate:.0f}/h")
                
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
# HÄR BÖRJAR DEL 2 - Post-hämtning och Insights-integrering
# ===================================================================================

def get_instagram_posts_for_period(instagram_id, since_date, until_date, account_name=None):
    """
    Hämta alla Instagram-posts för en specifik tidsperiod.
    
    VIKTIGT: Denna funktion hämtar bara post-metadata, inte insights.
    Insights hämtas separat för att optimera API-anrop.
    
    FILTRERINGSKRITERIER:
    - Datum: Endast posts inom angiven period
    - Typ: Endast FEED och REELS (hoppar över STORIES)
    - Status: Endast publika posts som användaren äger
    
    Args:
        instagram_id: Instagram Business Account ID
        since_date: Startdatum (YYYY-MM-DD)
        until_date: Slutdatum (YYYY-MM-DD) 
        account_name: Kontonamn för loggning
    
    Returns:
        list: Lista med post-objekt innehållande metadata
    """
    display_name = account_name if account_name else instagram_id
    logger.debug(f"Hämtar posts för {display_name} från {since_date} till {until_date}...")
    
    try:
        posts = []
        url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}/media"
        
        # Hämta media med relevanta fält för filtrering och visning
        # FÄLTBESKRIVNINGAR:
        # - id: Unikt post-ID för insights-hämtning
        # - timestamp: Publiceringsdatum/tid (ISO format)
        # - media_type: IMAGE, VIDEO, CAROUSEL_ALBUM
        # - media_product_type: FEED, REELS, STORY (vi filtrerar bort STORY)
        # - caption: Posttext (används för preview)
        # - permalink: Länk till posten (för referens)
        params = {
            "access_token": ACCESS_TOKEN,
            "limit": 25,  # Instagram API-begränsning per sida
            "fields": "id,timestamp,media_type,media_product_type,caption,permalink"
        }
        
        page_num = 0
        should_continue = True
        
        while url and should_continue:
            page_num += 1
            logger.debug(f"Hämtar sida {page_num} av posts för {display_name}...")
            
            data = api_request(url, params)
            
            if data and "data" in data:
                media_in_page = data["data"]
                logger.debug(f"  Hittade {len(media_in_page)} posts på sida {page_num}")
                
                # Om inga media på denna sida, avbryt
                if len(media_in_page) == 0:
                    should_continue = False
                    break
                
                # Filtrera posts baserat på datum och typ
                found_any_in_period = False
                for post in media_in_page:
                    post_timestamp = post.get("timestamp", "")
                    if post_timestamp:
                        # Timestamp format: "2025-01-15T10:30:00+0000"
                        post_date = post_timestamp[:10]  # Extrahera YYYY-MM-DD
                        
                        # Om detta post är äldre än since_date, stoppa paginering
                        # OPTIMERING: Instagram API returnerar posts i kronologisk ordning (nyast först)
                        if post_date < since_date:
                            logger.debug(f"    Hittade post från {post_date} som är äldre än {since_date}, stoppar paginering")
                            should_continue = False
                            break
                        
                        # Om post är inom perioden, lägg till den
                        if since_date <= post_date <= until_date:
                            # Filtrera bort Stories (vi fokuserar på Feed och Reels)
                            # MEDIA_PRODUCT_TYPE förklaring:
                            # - FEED: Vanliga Instagram-posts i flödet
                            # - REELS: Kortformat video-innehåll
                            # - STORY: Tillfälligt innehåll (24h) - hoppas över
                            media_product_type = post.get("media_product_type", "FEED")
                            if media_product_type in ["FEED", "REELS"]:
                                post["post_date"] = post_date
                                post["account_name"] = display_name
                                posts.append(post)
                                found_any_in_period = True
                                
                                media_type = post.get("media_type", "UNKNOWN")
                                logger.debug(f"    Post från {post_date}: {media_type} ({media_product_type})")
                            else:
                                logger.debug(f"    Hoppar över Story från {post_date}")
                
                # Om vi inte hittade några posts i perioden på denna sida och 
                # alla är från före perioden, avbryt
                if not found_any_in_period and should_continue:
                    # Kontrollera om det första mediet i sidan är äldre än vår period
                    if media_in_page and media_in_page[0].get("timestamp", "")[:10] < since_date:
                        logger.debug(f"    Alla posts på sida {page_num} är äldre än {since_date}, stoppar")
                        should_continue = False
                        break
                
                # Kontrollera om det finns fler sidor (bara om vi ska fortsätta)
                if should_continue:
                    paging = data.get("paging", {})
                    if "next" in paging:
                        url = paging["next"]
                        params = {}  # Töm params eftersom allt finns i URL:en
                    else:
                        url = None  # Inga fler sidor
                        
            elif data and "error" in data:
                error_msg = data["error"].get("message", "Okänt fel")
                error_code = data["error"].get("code", "N/A")
                logger.error(f"Error {error_code}: Kunde inte hämta posts för {display_name}: {error_msg}")
                break
            else:
                logger.warning(f"  Kunde inte hämta posts för {display_name}: Inget data")
                break
            
            # Säkerhetsbroms för att undvika oändliga loopar
            if page_num > 200:
                logger.warning(f"Avbryter efter {page_num} sidor för att undvika oändlig loop")
                break
        
        logger.info(f"  Posts för {display_name}: {len(posts)} (från {since_date} till {until_date}) - {page_num} sidor paginerade")
        return posts
            
    except Exception as e:
        logger.error(f"  Fel vid hämtning av posts för {display_name}: {e}")
        return []

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

def process_posts_with_insights(posts, account_name=None):
    """
    Bearbeta posts och hämta insights för varje post.
    
    VIKTIGT: Denna funktion gör många API-anrop (ett per post för insights).
    Rate limiting och error handling är kritiskt här.
    
    OPTIMERINGAR:
    - 0.5 sekunder paus mellan posts för att vara snäll mot API
    - Robust error handling per post (en misslyckad post stoppar inte resten)
    - Progress-loggning för långa körningar
    
    Args:
        posts: Lista med post-objekt från get_instagram_posts_for_period()
        account_name: Kontonamn för loggning
        instagram_id: Instagram Business Account ID (numeriskt)
    
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
            
            # Förkorta caption för visning (max 50 tecken + "...")
            # CAPTION-HANTERING: Instagram-captions kan vara mycket långa.
            # Vi sparar bara en preview för CSV-läsbarhet.
            caption = post.get("caption", "")
            caption_preview = (caption[:47] + "...") if len(caption) > 50 else caption
            
            # Skapa komplett post-record
            # KOLUMNORDNING: Följer användarens specificerade ordning för CSV
            complete_post = {
                "Account": display_name,
                "Instagram_ID": instagram_id or "",  # NYTT: Numeriskt Instagram Business Account ID
                "Post_ID": post_id,
                "Post_Date": post_date,
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
# HÄR SLUTAR DEL 2 - Post-hämtning och Insights-integrering
# ===================================================================================
# ===================================================================================
# HÄR BÖRJAR DEL 2 - Post-hämtning och Insights-integrering
# ===================================================================================

def get_instagram_posts_for_period(instagram_id, since_date, until_date, account_name=None):
    """
    Hämta alla Instagram-posts för en specifik tidsperiod.
    
    VIKTIGT: Denna funktion hämtar bara post-metadata, inte insights.
    Insights hämtas separat för att optimera API-anrop.
    
    FILTRERINGSKRITERIER:
    - Datum: Endast posts inom angiven period
    - Typ: Endast FEED och REELS (hoppar över STORIES)
    - Status: Endast publika posts som användaren äger
    
    Args:
        instagram_id: Instagram Business Account ID
        since_date: Startdatum (YYYY-MM-DD)
        until_date: Slutdatum (YYYY-MM-DD) 
        account_name: Kontonamn för loggning
    
    Returns:
        list: Lista med post-objekt innehållande metadata
    """
    display_name = account_name if account_name else instagram_id
    logger.debug(f"Hämtar posts för {display_name} från {since_date} till {until_date}...")
    
    try:
        posts = []
        url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}/media"
        
        # Hämta media med relevanta fält för filtrering och visning
        # FÄLTBESKRIVNINGAR:
        # - id: Unikt post-ID för insights-hämtning
        # - timestamp: Publiceringsdatum/tid (ISO format)
        # - media_type: IMAGE, VIDEO, CAROUSEL_ALBUM
        # - media_product_type: FEED, REELS, STORY (vi filtrerar bort STORY)
        # - caption: Posttext (används för preview)
        # - permalink: Länk till posten (för referens)
        params = {
            "access_token": ACCESS_TOKEN,
            "limit": 25,  # Instagram API-begränsning per sida
            "fields": "id,timestamp,media_type,media_product_type,caption,permalink"
        }
        
        page_num = 0
        should_continue = True
        
        while url and should_continue:
            page_num += 1
            logger.debug(f"Hämtar sida {page_num} av posts för {display_name}...")
            
            data = api_request(url, params)
            
            if data and "data" in data:
                media_in_page = data["data"]
                logger.debug(f"  Hittade {len(media_in_page)} posts på sida {page_num}")
                
                # Om inga media på denna sida, avbryt
                if len(media_in_page) == 0:
                    should_continue = False
                    break
                
                # Filtrera posts baserat på datum och typ
                found_any_in_period = False
                for post in media_in_page:
                    post_timestamp = post.get("timestamp", "")
                    if post_timestamp:
                        # Timestamp format: "2025-01-15T10:30:00+0000"
                        post_date = post_timestamp[:10]  # Extrahera YYYY-MM-DD
                        
                        # Om detta post är äldre än since_date, stoppa paginering
                        # OPTIMERING: Instagram API returnerar posts i kronologisk ordning (nyast först)
                        if post_date < since_date:
                            logger.debug(f"    Hittade post från {post_date} som är äldre än {since_date}, stoppar paginering")
                            should_continue = False
                            break
                        
                        # Om post är inom perioden, lägg till den
                        if since_date <= post_date <= until_date:
                            # Filtrera bort Stories (vi fokuserar på Feed och Reels)
                            # MEDIA_PRODUCT_TYPE förklaring:
                            # - FEED: Vanliga Instagram-posts i flödet
                            # - REELS: Kortformat video-innehåll
                            # - STORY: Tillfälligt innehåll (24h) - hoppas över
                            media_product_type = post.get("media_product_type", "FEED")
                            if media_product_type in ["FEED", "REELS"]:
                                post["post_date"] = post_date
                                post["account_name"] = display_name
                                posts.append(post)
                                found_any_in_period = True
                                
                                media_type = post.get("media_type", "UNKNOWN")
                                logger.debug(f"    Post från {post_date}: {media_type} ({media_product_type})")
                            else:
                                logger.debug(f"    Hoppar över Story från {post_date}")
                
                # Om vi inte hittade några posts i perioden på denna sida och 
                # alla är från före perioden, avbryt
                if not found_any_in_period and should_continue:
                    # Kontrollera om det första mediet i sidan är äldre än vår period
                    if media_in_page and media_in_page[0].get("timestamp", "")[:10] < since_date:
                        logger.debug(f"    Alla posts på sida {page_num} är äldre än {since_date}, stoppar")
                        should_continue = False
                        break
                
                # Kontrollera om det finns fler sidor (bara om vi ska fortsätta)
                if should_continue:
                    paging = data.get("paging", {})
                    if "next" in paging:
                        url = paging["next"]
                        params = {}  # Töm params eftersom allt finns i URL:en
                    else:
                        url = None  # Inga fler sidor
                        
            elif data and "error" in data:
                error_msg = data["error"].get("message", "Okänt fel")
                error_code = data["error"].get("code", "N/A")
                logger.error(f"Error {error_code}: Kunde inte hämta posts för {display_name}: {error_msg}")
                break
            else:
                logger.warning(f"  Kunde inte hämta posts för {display_name}: Inget data")
                break
            
            # Säkerhetsbroms för att undvika oändliga loopar
            if page_num > 200:
                logger.warning(f"Avbryter efter {page_num} sidor för att undvika oändlig loop")
                break
        
        logger.info(f"  Posts för {display_name}: {len(posts)} (från {since_date} till {until_date}) - {page_num} sidor paginerade")
        return posts
            
    except Exception as e:
        logger.error(f"  Fel vid hämtning av posts för {display_name}: {e}")
        return []

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
    
    VIKTIGT: Denna funktion gör många API-anrop (ett per post för insights).
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
            
            # Förkorta caption för visning (max 50 tecken + "...")
            # CAPTION-HANTERING: Instagram-captions kan vara mycket långa.
            # Vi sparar bara en preview för CSV-läsbarhet.
            caption = post.get("caption", "")
            caption_preview = (caption[:47] + "...") if len(caption) > 50 else caption
            
            # Skapa komplett post-record
            # KOLUMNORDNING: Följer användarens specificerade ordning för CSV
            complete_post = {
                "Account": display_name,
                "Instagram_ID": instagram_account_id if instagram_account_id else "",  # FIXAD: Använd rätt variabelnamn
                "Post_ID": post_id,
                "Post_Date": post_date,
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
# HÄR SLUTAR DEL 2 - Post-hämtning och Insights-integrering
# ===================================================================================
# ===================================================================================
# HÄR BÖRJAR DEL 3 - CSV-hantering, huvudkörning och kommandoradsargument
# ===================================================================================

def process_account_posts_for_month(instagram_id, account_name, year, month, update_existing=False):
    """
    Bearbeta alla posts för ett specifikt Instagram-konto under en månad.
    
    FILNAMNSKONVENTION: IG_Posts_YYYY_MM.csv (samma som tidigare skript)
    Denna konvention gör det enkelt för visualiseringsappar att tolka period från filnamn.
    
    Args:
        instagram_id: Instagram Business Account ID
        account_name: Kontonamn (för loggning och CSV)
        year: År (YYYY)
        month: Månad (MM)
        update_existing: Om befintliga CSV-filer ska uppdateras
    
    Returns:
        tuple: (success_count, error_count, posts_processed)
    """
    # Sätt datumintervall för månaden
    start_date = f"{year}-{month:02d}-01"
    
    # Beräkna slutdatum (sista dagen i månaden)
    last_day = monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day}"
    
    # Sätt utdatafilnamn enligt konvention
    output_file = f"IG_Posts_{year}_{month:02d}.csv"
    
    logger.info(f"Bearbetar posts för @{account_name}: {year}-{month:02d} (från {start_date} till {end_date})")
    
    try:
        # Hämta alla posts för perioden
        posts = get_instagram_posts_for_period(instagram_id, start_date, end_date, account_name)
        
        if not posts:
            logger.info(f"  Inga posts hittades för @{account_name} under {year}-{month:02d}")
            return 0, 0, 0
        
        # Bearbeta posts med insights
        complete_posts = process_posts_with_insights(posts, account_name)
        
        if complete_posts:
            # Spara eller uppdatera CSV-fil
            success = save_posts_to_csv(complete_posts, output_file, account_name, update_existing)
            
            if success:
                success_count = len([p for p in complete_posts if p.get("Status") == "OK"])
                error_count = len(complete_posts) - success_count
                
                # Visa summering
                show_posts_summary(complete_posts, account_name, year, month)
                
                return success_count, error_count, len(complete_posts)
            else:
                return 0, len(complete_posts), len(complete_posts)
        else:
            logger.warning(f"  Inga posts kunde bearbetas för @{account_name}")
            return 0, 0, 0
            
    except Exception as e:
        logger.error(f"Fel vid bearbetning av posts för @{account_name}: {e}")
        return 0, 1, 0

def save_posts_to_csv(posts_data, filename, account_name=None, update_existing=False):
    """
    Spara post-data till CSV-fil.
    
    CSV-STRUKTUR (i användarens specificerade ordning):
    Account, Post_ID, Post_Date, Media_Type, Media_Product_Type, Caption_Preview,
    Reach, Comments, Likes, Shares, Saved, Views, Status, Error_Message
    
    SORTERING: Posts grupperas per konto (A-Ö), sedan kronologiskt inom varje konto (äldst först).
    Detta gör CSV-filen mycket mer läsbar för analys av varje kontos utveckling över tid.
    
    Args:
        posts_data: Lista med post-dictionaries
        filename: Utdatafilnamn (IG_Posts_YYYY_MM.csv)
        account_name: Kontonamn för loggning
        update_existing: Om befintlig fil ska uppdateras (för flera konton i samma fil)
    
    Returns:
        bool: True om lyckad, False om fel
    """
    try:
        display_name = account_name if account_name else "Unknown"
        
        # Läs in befintlig data om filen finns och vi ska uppdatera
        existing_posts = []
        if update_existing and os.path.exists(filename):
            existing_posts = read_existing_posts_csv(filename)
            logger.info(f"Läste in {len(existing_posts)} befintliga posts från {filename}")
        
        # Kombinera befintlig data med ny data
        all_posts = existing_posts + posts_data
        
        # Ta bort dubbletter baserat på Post_ID
        seen_post_ids = set()
        unique_posts = []
        for post in all_posts:
            post_id = post.get("Post_ID", "")
            if post_id and post_id not in seen_post_ids:
                seen_post_ids.add(post_id)
                unique_posts.append(post)
        
        # Sortera posts: Gruppera per konto (A-Ö), sedan kronologiskt inom varje konto (äldst först)
        sorted_posts = sorted(unique_posts, 
                             key=lambda x: (x.get("Account", ""), x.get("Post_Date", "")))
        
        # Definiera CSV-kolumner i användarens specificerade ordning (UPPDATERAD med Instagram_ID)
        # KOLUMNBESKRIVNINGAR:
        # - Account: Instagram-kontonamn (@username)
        # - Instagram_ID: Numeriskt Instagram Business Account ID (för referens)
        # - Post_ID: Unikt Instagram media ID
        # - Post_Date: Publiceringsdatum (YYYY-MM-DD)
        # - Media_Type: IMAGE, VIDEO, CAROUSEL_ALBUM
        # - Media_Product_Type: FEED eller REELS  
        # - Caption_Preview: Första 50 tecken av posttext
        # - Reach: Unika konton som sett posten
        # - Comments: Antal kommentarer
        # - Likes: Antal hjärtan/gilla-markeringar
        # - Shares: Antal delningar (främst Reels)
        # - Saved: Antal sparningar/bokmärken
        # - Views: Antal videovisningar (för VIDEO/REELS)
        # - Status: OK, API_ERROR, NO_DATA, EXCEPTION
        # - Error_Message: Felmeddelande om Status != OK
        fieldnames = [
            "Account", "Instagram_ID", "Post_ID", "Post_Date", "Media_Type", "Media_Product_Type", "Caption_Preview",
            "Reach", "Comments", "Likes", "Shares", "Saved", "Views", "Status", "Error_Message"
        ]
        
        with open(filename, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(sorted_posts)
            
        logger.info(f"Sparade {len(sorted_posts)} posts till {filename} (varav {len(posts_data)} nya)")
        return True
        
    except Exception as e:
        logger.error(f"Kunde inte spara posts till CSV: {e}")
        return False

def read_existing_posts_csv(filename):
    """
    Läs in befintlig posts CSV-fil.
    
    Används när flera konton ska kombineras i samma månadsfil
    eller när vi uppdaterar befintliga filer.
    
    Returns:
        list: Lista med befintliga post-dictionaries
    """
    posts = []
    
    if not os.path.exists(filename):
        return posts
        
    try:
        with open(filename, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Konvertera numeriska fält tillbaka till integers
                numeric_fields = ["Reach", "Comments", "Likes", "Shares", "Saved", "Views"]
                for field in numeric_fields:
                    if field in row:
                        row[field] = safe_int_value(row[field], 0)
                posts.append(row)
                
    except Exception as e:
        logger.error(f"Fel vid läsning av befintlig CSV {filename}: {e}")
        
    return posts

def show_posts_summary(posts_data, account_name, year, month):
    """
    Visa summering av post-data för loggning.
    
    SUMMERING INKLUDERAR:
    - Totaler per metrik
    - Fördelning per post-typ
    - Genomsnitt per post
    - Status-översikt
    """
    try:
        if not posts_data:
            return
            
        display_name = account_name if account_name else "Unknown"
        
        # Beräkna totaler (endast för posts med status OK)
        ok_posts = [p for p in posts_data if p.get("Status") == "OK"]
        
        if ok_posts:
            # KORRIGERAD SUMMERING: Endast metriker som KAN summeras
            # INTE reach och views (unika per post, meningslöst att summera)
            total_comments = sum(safe_int_value(p.get("Comments", 0)) for p in ok_posts)
            total_likes = sum(safe_int_value(p.get("Likes", 0)) for p in ok_posts)
            total_shares = sum(safe_int_value(p.get("Shares", 0)) for p in ok_posts)
            total_saved = sum(safe_int_value(p.get("Saved", 0)) for p in ok_posts)
            
            # Genomsnitt för metriker som INTE ska summeras
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
        
        # Post-typ fördelning
        type_counts = {}
        for post in posts_data:
            post_type = f"{post.get('Media_Type', 'UNKNOWN')}/{post.get('Media_Product_Type', 'FEED')}"
            type_counts[post_type] = type_counts.get(post_type, 0) + 1
        
        if type_counts:
            logger.info(f"  - Post-typer:")
            for post_type, count in sorted(type_counts.items()):
                logger.info(f"    • {post_type}: {count} posts")
        
        # Status-översikt
        status_counts = {}
        for post in posts_data:
            status = post.get("Status", "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1
        
        if len(status_counts) > 1:  # Bara visa om det finns fel
            logger.info(f"  - Status-översikt:")
            for status, count in status_counts.items():
                logger.info(f"    • {status}: {count} posts")
        
    except Exception as e:
        logger.error(f"Fel vid summering av posts: {e}")

def get_existing_post_reports():
    """
    Scanna katalogen efter befintliga Instagram post-rapporter.
    
    FILNAMNSFORMAT: IG_Posts_YYYY_MM.csv
    
    Returns:
        set: Uppsättning av bearbetade månader (YYYY-MM format)
    """
    existing_reports = set()
    
    for filename in glob.glob("IG_Posts_*.csv"):
        try:
            # Extrahera år och månad från filnamnet (IG_Posts_YYYY_MM.csv)
            parts = filename.replace(".csv", "").split("_")
            if len(parts) == 3 and parts[0] == "IG" and parts[1] == "Posts":
                year = parts[2]
                month = parts[3] if len(parts) > 3 else None
                
                # Hantera olika format
                if month and year.isdigit() and month.isdigit():
                    if len(year) == 4 and len(month) == 2:
                        existing_reports.add(f"{year}-{month}")
                        logger.debug(f"Hittade befintlig post-rapport för {year}-{month}: {filename}")
                        
        except Exception as e:
            logger.warning(f"Kunde inte tolka post-filnamn {filename}: {e}")
            
    return existing_reports

def get_missing_months_for_posts(existing_reports, start_year_month):
    """
    Bestäm vilka månader som behöver bearbetas för posts.
    
    LOGIK: Samma som för konto-reach, men för post-rapporter.
    Arbetar bakåt från aktuell månad till start-datum.
    """
    missing_months = []
    
    # Tolka startår och månad
    start_year, start_month = map(int, start_year_month.split("-"))
    
    # Hämta aktuellt år och månad
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    
    # Generera alla månader från startdatum till sista avslutade månad
    year = start_year
    month = start_month
    
    while (year < current_year) or (year == current_year and month < current_month):
        month_str = f"{year}-{month:02d}"
        if month_str not in existing_reports:
            missing_months.append((year, month))
        
        # Gå till nästa månad
        month += 1
        if month > 12:
            month = 1
            year += 1
    
    return missing_months

def process_all_accounts_for_month(account_list, year, month, update_existing=False):
    """
    Bearbeta alla Instagram-konton för en specifik månad.
    
    KOMBINATION: Alla konton kombineras i samma CSV-fil per månad.
    Detta gör det enkelt att jämföra prestanda mellan konton.
    
    Args:
        account_list: Lista med (instagram_id, account_name, facebook_page) tuples
        year: År att bearbeta
        month: Månad att bearbeta
        update_existing: Om befintliga filer ska uppdateras
        
    Returns:
        tuple: (total_success, total_errors, total_posts)
    """
    logger.info(f"Bearbetar alla konton för {year}-{month:02d}...")
    
    total_success = 0
    total_errors = 0
    total_posts = 0
    
    for i, (instagram_id, account_name, facebook_page) in enumerate(account_list):
        logger.info(f"Konto {i+1}/{len(account_list)}: @{account_name}")
        
        try:
            success, errors, posts = process_account_posts_for_month(
                instagram_id, account_name, year, month, 
                update_existing=(update_existing or i > 0)  # Uppdatera från andra kontot
            )
            
            total_success += success
            total_errors += errors
            total_posts += posts
            
            # Kort paus mellan konton
            if i < len(account_list) - 1:
                logger.info(f"Pausar kort innan nästa konto...")
                time.sleep(2)
                
        except Exception as e:
            logger.error(f"Fel vid bearbetning av konto @{account_name}: {e}")
            total_errors += 1
            continue
    
    logger.info(f"Månadsresultat {year}-{month:02d}: {total_success} lyckade posts, {total_errors} fel, {total_posts} totalt")
    return total_success, total_errors, total_posts

def main():
    """
    Huvudfunktion för att köra hela post-analytics processen.
    
    KOMMANDORADSARGUMENT:
    --month YYYY-MM: Kör endast för specifik månad
    --start YYYY-MM: Sätt startdatum för batch-körning
    --update-all: Uppdatera befintliga filer
    --debug: Aktivera debug-loggning
    """
    # Parsa kommandoradsargument
    parser = argparse.ArgumentParser(
        description="Generera Instagram post-nivå analytics för alla konton och månader",
        epilog="Exempel: python fetch_instagram_posts.py --month 2025-08"
    )
    
    # Datum-grupp för månader
    date_group = parser.add_argument_group("Datumargument för månader")
    date_group.add_argument("--start", help="Startår-månad (YYYY-MM)")
    date_group.add_argument("--month", help="Kör endast för angiven månad (YYYY-MM)")
    
    # Operationsmodifikatorer
    ops_group = parser.add_argument_group("Operationsmodifikatorer")
    ops_group.add_argument("--update-all", action="store_true", 
                          help="Uppdatera alla posts även om de redan finns i CSV-filen")
    ops_group.add_argument("--debug", action="store_true", 
                          help="Aktivera debug-loggning")
    
    args = parser.parse_args()
    
    # Sätt debug-läge om begärt
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug-läge aktiverat")
    
    # Kontrollera för inkompatibla argumentkombinationer
    if args.start and args.month:
        logger.error("Använd antingen --start eller --month, inte båda")
        parser.print_help()
        sys.exit(1)
    
    # Använd argument om de finns
    start_year_month = args.start or INITIAL_START_YEAR_MONTH
    
    logger.info("Instagram Post Analytics Generator – v1.0")
    logger.info(f"Startdatum: {start_year_month}")
    logger.info("Dynamisk rate limit-hantering aktiverad")
    logger.info("-------------------------------------------------------------------")
    
    # Kontrollera token och varna om den snart går ut
    check_token_expiry()
    
    # Validera token
    if not validate_token(ACCESS_TOKEN):
        logger.error("Token kunde inte valideras. Avbryter.")
        return
    
    # Ladda cache för kontonamn
    cache = load_account_cache()
    
    # Hämta alla tillgängliga Instagram-konton
    account_list = get_instagram_accounts_with_access(ACCESS_TOKEN)
    
    if not account_list:
        logger.error("Inga Instagram-konton hittades. Avbryter.")
        return
    
    # Om specifik månad angivits, kör endast den
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
            logger.info(f"Kör endast för specifik månad: {year}-{month:02d}")
            
            success, errors, posts = process_all_accounts_for_month(
                account_list, year, month, args.update_all
            )
            
            save_account_cache(cache)
            logger.info(f"Klart för {year}-{month:02d}: {success} lyckade posts, {errors} fel")
            return
            
        except ValueError:
            logger.error(f"Ogiltigt månadsformat: {args.month}. Använd YYYY-MM.")
            return
    
    # Hämta befintliga post-rapporter
    existing_reports = get_existing_post_reports()
    logger.info(f"Hittade {len(existing_reports)} befintliga post-rapporter: {', '.join(sorted(existing_reports)) if existing_reports else 'Inga'}")
    
    # Få saknade månader
    missing_months = get_missing_months_for_posts(existing_reports, start_year_month)
    
    if not missing_months:
        logger.info("Alla månader är redan bearbetade för posts. Inget att göra.")
        logger.info("Använd --month YYYY-MM för att köra specifik månad eller --update-all för att uppdatera.")
        return
    
    logger.info(f"Behöver bearbeta {len(missing_months)} saknade månader: {', '.join([f'{y}-{m:02d}' for y, m in missing_months])}")
    
    # Bearbeta varje saknad månad
    total_success_all = 0
    total_errors_all = 0
    total_posts_all = 0
    
    for i, (year, month) in enumerate(missing_months):
        logger.info(f"Bearbetar månad {i+1}/{len(missing_months)}: {year}-{month:02d}")
        
        try:
            success, errors, posts = process_all_accounts_for_month(
                account_list, year, month, args.update_all
            )
            
            total_success_all += success
            total_errors_all += errors  
            total_posts_all += posts
            
            # Spara cache efter varje månad
            save_account_cache(cache)
            
            # Pausa mellan månader om vi har haft rate limit-problem
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
    
    # Visa slutstatistik
    elapsed_time = time.time() - start_time
    avg_rate = api_call_count / (elapsed_time / 3600) if elapsed_time > 0 else 0
    
    logger.info("-------------------------------------------------------------------")
    logger.info("SLUTRESULTAT:")
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
        
    logger.info("Klar med Instagram post-analytics!")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Avbruten av användare. Sparar mellanlagrat arbete...")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Oväntat fel: {e}")
        import traceback
        logger.critical(traceback.format_exc())
        sys.exit(1)

# ===================================================================================
# HÄR SLUTAR DEL 3 - CSV-hantering, huvudkörning och kommandoradsargument
# ===================================================================================
# 
# SLUTFÖRT INSTAGRAM POSTS ANALYTICS SCRIPT v1.0
# 
# Användning:
# python fetch_instagram_posts.py                     # Kör alla saknade månader
# python fetch_instagram_posts.py --month 2025-08     # Kör endast augusti 2025
# python fetch_instagram_posts.py --update-all        # Uppdatera befintliga rapporter
# python fetch_instagram_posts.py --debug             # Aktivera debug-loggning
# 
# Utdata: IG_Posts_YYYY_MM.csv filer med post-nivå analytics
# 
# CSV-KOLUMNER (i specificerad ordning):
# Account, Post_ID, Post_Date, Media_Type, Media_Product_Type, Caption_Preview,
# Reach, Comments, Likes, Shares, Saved, Views, Status, Error_Message
# 
# CSV-SORTERING (uppdaterad):
# - Grupperat per konto (A-Ö alfabetisk ordning)
# - Inom varje konto: kronologiskt (äldst först)
# - Perfekt för att följa varje kontos utveckling över tid
# 
# METRIKER PER POST-TYP:
# - IMAGE/FEED: reach, comments, likes, saved (shares oftast 0)
# - VIDEO/FEED: reach, comments, likes, saved, views  
# - REELS: reach, comments, likes, shares, saved, views
# - CAROUSEL: reach, comments, likes, saved (views för videor i carousel)
# 
# ===================================================================================