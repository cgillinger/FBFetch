# fetch_facebook_reach.py
# Version 2.8 - Årsbaserad katalogstruktur
# 
# Detta skript sparar nu alla CSV-filer i årsspecifika kataloger (reachÅRTAL/)
# med statusrapporter i en "status"-undermapp.

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

# Skapa datumstämplad loggfil
def setup_logging():
    """Konfigurera loggning med datumstämplad loggfil"""
    now = datetime.now()
    log_dir = "logs"
    
    # Skapa loggdirektory om den inte finns
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Skapa datumstämplad loggfilnamn
    log_filename = os.path.join(log_dir, f"facebook_reach_{now.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    
    # Konfigurera loggning
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.FileHandler("facebook_reach.log"),
            logging.StreamHandler()
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
rate_limit_backoff = 1.0
consecutive_successes = 0

def get_year_directory(year):
    """Returnera katalognamn för ett givet år"""
    return f"reach{year}"

def get_status_directory(year):
    """Returnera sökväg till status-katalog för ett givet år"""
    return os.path.join(get_year_directory(year), "status")

def ensure_directory_exists(directory):
    """Skapa katalog om den inte finns"""
    if not os.path.exists(directory):
        os.makedirs(directory)
        logger.debug(f"Skapade katalog: {directory}")

def extract_year_from_filename(filename):
    """Extrahera år från filnamn (FB_YYYY_MM.csv eller FB_YYYY-MM-DD_to_YYYY-MM-DD.csv)"""
    try:
        # Ta bort katalogväg om den finns
        basename = os.path.basename(filename)
        # Ta bort .csv
        basename = basename.replace(".csv", "")
        # Dela upp på understreck
        parts = basename.split("_")
        
        if len(parts) >= 2 and parts[0] == "FB":
            # Kontrollera om det är årtal (4 siffror)
            year_candidate = parts[1]
            if year_candidate.isdigit() and len(year_candidate) == 4:
                return int(year_candidate)
            # Annars kanske det är ett datum (YYYY-MM-DD)
            elif "-" in year_candidate:
                return int(year_candidate.split("-")[0])
        
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
        
        logger.info(f"Token skapades för {days_since} dagar sedan ({days_left} dagar kvar till utgång).")
        
        if days_left <= 7:
            logger.warning(f"VARNING: Din token går ut inom {days_left} dagar! Skapa en ny token snart.")
        elif days_left <= 0:
            logger.error(f"KRITISKT: Din token har gått ut! Skapa en ny token omedelbart.")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Kunde inte tolka TOKEN_LAST_UPDATED: {e}")

def load_page_cache():
    """Ladda cache med sidnamn för att minska API-anrop"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                logger.debug(f"Laddar sid-cache från {CACHE_FILE}")
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"Kunde inte ladda cache-fil, skapar ny cache")
    return {}

def save_page_cache(cache):
    """Spara cache med sidnamn för framtida körningar"""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
            logger.debug(f"Sparade sid-cache till {CACHE_FILE}")
    except Exception as e:
        logger.error(f"Kunde inte spara cache: {e}")

def api_request(url, params, retries=MAX_RETRIES):
    """Gör API-förfrågan med dynamisk rate limit-hantering"""
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

def get_page_ids_with_access(token):
    """Hämta alla sidor som token har åtkomst till"""
    logger.info("Hämtar tillgängliga sidor...")
    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {"access_token": token, "limit": 100, "fields": "id,name"}
    
    pages = []
    next_url = url
    
    while next_url:
        data = api_request(url if next_url == url else next_url, {} if next_url != url else params)
        
        if not data or "data" not in data:
            break
            
        pages.extend(data["data"])
        logger.debug(f"Hittade {len(data['data'])} sidor i denna batch")
        
        next_url = data.get("paging", {}).get("next")
        if next_url and next_url != url:
            logger.debug(f"Hämtar nästa sida från: {next_url}")
        else:
            break
    
    if not pages:
        logger.warning("Inga sidor hittades. Token kanske saknar 'pages_show_list'-behörighet.")
    
    page_ids = [(page["id"], page["name"]) for page in pages]
    logger.info(f"Hittade {len(page_ids)} sidor att analysera")
    return page_ids

def filter_placeholder_pages(page_list):
    """Filtrera bort placeholder-sidor som SrholderX (där X är ett tal)"""
    filtered_pages = []
    filtered_out = []
    
    for page_id, page_name in page_list:
        if page_name and page_name.startswith('Srholder') and page_name[8:].isdigit():
            filtered_out.append((page_id, page_name))
            logger.debug(f"Filtrerar bort placeholder-sida: {page_name} (ID: {page_id})")
        else:
            filtered_pages.append((page_id, page_name))
    
    if filtered_out:
        placeholder_names = []
        for _, name in filtered_out:
            placeholder_names.append(name)
        logger.info(f"Filtrerade bort {len(filtered_out)} placeholder-sidor: {', '.join(placeholder_names)}")
    
    logger.info(f"{len(filtered_pages)} sidor kvar efter filtrering")
    return filtered_pages

def get_page_name(page_id, cache):
    """Hämta sidans namn från cache eller API"""
    if page_id in cache:
        return cache[page_id]
    
    logger.debug(f"Hämtar namn för sida {page_id}...")
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}"
    params = {"fields": "name", "access_token": ACCESS_TOKEN}
    
    data = api_request(url, params)
    
    if not data or "error" in data:
        error_msg = data.get("error", {}).get("message", "Okänt fel") if data else "Fel vid API-anrop"
        logger.warning(f"Kunde inte hämta namn för sida {page_id}: {error_msg}")
        return None
    
    name = data.get("name", f"Page {page_id}")
    cache[page_id] = name
    return name

def get_page_access_token(page_id, system_token):
    """Konvertera systemanvändartoken till en Page Access Token för en specifik sida"""
    logger.debug(f"Hämtar Page Access Token för sida {page_id}...")
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}"
    params = {
        "fields": "access_token",
        "access_token": system_token
    }
    
    data = api_request(url, params)
    
    if not data or "error" in data or "access_token" not in data:
        error_msg = data.get("error", {}).get("message", "Okänt fel") if data and "error" in data else "Kunde inte hämta token"
        logger.warning(f"Kunde inte hämta Page Access Token för sida {page_id}: {error_msg}")
        return None
    
    return data["access_token"]

# ═══════════════════════════════════════════════════════════════
# HÄR SLUTAR DEL ETT
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# HÄR BÖRJAR DEL TVÅ
# ═══════════════════════════════════════════════════════════════

def get_page_publications(page_id, page_token, since, until, page_name=None):
    """Hämta antal publiceringar för en sida under en specifik tidsperiod genom paginering"""
    display_name = page_name if page_name else page_id
    logger.debug(f"Hämtar antal publiceringar för sida {display_name} från {since} till {until}...")
    
    try:
        since_timestamp = int(datetime.strptime(since, "%Y-%m-%d").timestamp())
        until_timestamp = int(datetime.strptime(until, "%Y-%m-%d").timestamp()) + 86399
        
        post_count = 0
        url = f"https://graph.facebook.com/{API_VERSION}/{page_id}/published_posts"
        
        params = {
            "access_token": page_token,
            "since": since_timestamp,
            "until": until_timestamp,
            "limit": 100,
            "fields": "id"
        }
        
        next_page = True
        page_num = 0
        
        while next_page:
            page_num += 1
            logger.debug(f"Hämtar sida {page_num} av publiceringar för {display_name}...")
            
            data = api_request(url, params)
            
            if data and "data" in data:
                posts_in_page = len(data["data"])
                post_count += posts_in_page
                logger.debug(f"  Hittade {posts_in_page} posts på sida {page_num} (totalt: {post_count})")
                
                if "paging" in data and "next" in data["paging"] and posts_in_page > 0:
                    url = data["paging"]["next"]
                    params = {}
                else:
                    next_page = False
            elif data and "error" in data:
                error_msg = data["error"].get("message", "Okänt fel")
                error_code = data["error"].get("code", "N/A")
                logger.error(f"Error {error_code}: Kunde inte hämta publiceringar för sida '{display_name}': {error_msg}")
                break
            else:
                logger.warning(f"  Kunde inte hämta publiceringar för sida {display_name}: Inget data")
                break
            
            if page_num > 50:
                logger.warning(f"Avbryter efter {page_num} sidor för att undvika oändlig loop")
                break
        
        logger.info(f"  Publiceringar för {display_name}: {post_count} (från {since} till {until})")
        return post_count
            
    except Exception as e:
        logger.error(f"  Fel vid hämtning av publiceringar för sida {display_name}: {e}")
        return 0

def get_page_metrics(page_id, system_token, since, until, page_name=None):
    """Hämta räckvidd, interaktionsdata och antal publiceringar för en sida under en specifik tidsperiod"""
    display_name = page_name if page_name else page_id
    logger.debug(f"Hämtar metriker för sida {display_name} från {since} till {until}...")
    
    result = {
        "reach": 0,
        "engaged_users": 0,
        "engagements": 0,
        "reactions": 0,
        "publications": 0,
        "reactions_details": {},
        "status": "OK",
        "comment": ""
    }
    
    page_token = get_page_access_token(page_id, system_token)
    
    if not page_token:
        result["status"] = "NO_ACCESS"
        result["comment"] = "Kunde inte hämta Page Access Token"
        logger.warning(f"Kunde inte hämta Page Access Token för sida {display_name}")
        return result
    
    result["publications"] = get_page_publications(page_id, page_token, since, until, page_name)
    
    metrics_mapping = [
        {"api_name": "page_impressions_unique", "result_key": "reach", "display_name": "Räckvidd"},
        {"api_name": "page_post_engagements", "result_key": "engagements", "display_name": "Interaktioner"},
        {"api_name": "page_actions_post_reactions_total", "result_key": "reactions", "display_name": "Reaktioner"}
    ]
    
    api_errors = []
    
    for metric_info in metrics_mapping:
        try:
            url = f"https://graph.facebook.com/{API_VERSION}/{page_id}/insights"
            params = {
                "access_token": page_token,
                "since": since,
                "until": until,
                "period": "total_over_range",
                "metric": metric_info["api_name"]
            }
            
            data = api_request(url, params)
            
            if data and "data" in data and data["data"]:
                for metric in data["data"]:
                    if metric["values"] and len(metric["values"]) > 0:
                        value = metric["values"][0].get("value", 0)
                        
                        if metric_info["result_key"] == "reactions" and isinstance(value, dict):
                            result["reactions_details"] = value
                            total_reactions = sum(int(v) for k, v in value.items() 
                                              if isinstance(v, (int, float)) or 
                                              (isinstance(v, str) and v.isdigit()))
                            
                            logger.info(f"Reaktioner för {display_name}: {value}, totalt: {total_reactions}")
                            result[metric_info["result_key"]] = total_reactions
                        else:
                            result[metric_info["result_key"]] = value
                            
                        logger.debug(f"  {metric_info['display_name']} för {display_name}: {value}")
            elif data and "error" in data:
                error_msg = data["error"].get("message", "Okänt fel")
                error_code = data["error"].get("code", "N/A")
                api_errors.append(f"{metric_info['display_name']}: {error_msg} (kod {error_code})")
                logger.error(f"Error {error_code}: Saknas mätvärde '{metric_info['display_name']}' för sida '{display_name}': {error_msg}")
            else:
                logger.warning(f"  Kunde inte hämta {metric_info['display_name']} för sida {display_name}: Inget data")
                
        except Exception as e:
            api_errors.append(f"{metric_info['display_name']}: {str(e)}")
            logger.warning(f"  Fel vid hämtning av {metric_info['display_name']} för sida {display_name}: {e}")
            continue
    
    if api_errors:
        result["status"] = "API_ERROR"
        result["comment"] = "; ".join(api_errors[:3])
    elif all(result[key] == 0 for key in ["reach", "engaged_users", "engagements", "reactions", "publications"]):
        result["status"] = "NO_DATA"
        result["comment"] = "Alla värden är noll"
    
    return result

def read_existing_csv(filename):
    """Läs in befintlig CSV-fil och returnera en dict med Page ID -> data och info om saknade kolumner"""
    existing_data = {}
    missing_columns = set()
    
    if os.path.exists(filename):
        try:
            with open(filename, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                
                expected_columns = {
                    "Page", "Page ID", "Reach", "Engaged Users", "Engagements", 
                    "Reactions", "Publications", "Status", "Comment"
                }
                
                missing_columns = expected_columns - set(fieldnames)
                
                if missing_columns:
                    logger.info(f"Saknade kolumner i {filename}: {', '.join(missing_columns)}")
                
                for row in reader:
                    if "Page ID" in row:
                        page_data = {
                            "Page": row["Page"],
                            "Page ID": row["Page ID"],
                            "Reach": int(row.get("Reach", 0))
                        }
                        
                        if "Engaged Users" in row:
                            page_data["Engaged Users"] = int(row.get("Engaged Users", 0))
                        if "Engagements" in row:
                            page_data["Engagements"] = int(row.get("Engagements", 0))
                        if "Reactions" in row:
                            try:
                                page_data["Reactions"] = int(row.get("Reactions", 0))
                            except ValueError:
                                page_data["Reactions"] = 0
                        
                        if "Publications" in row:
                            page_data["Publications"] = int(row.get("Publications", 0))
                        
                        if "Status" in row:
                            page_data["Status"] = row["Status"]
                        if "Comment" in row:
                            page_data["Comment"] = row["Comment"]
                            
                        existing_data[row["Page ID"]] = page_data
            logger.info(f"Läste in {len(existing_data)} befintliga sidor från {filename}")
        except Exception as e:
            logger.error(f"Fel vid inläsning av befintlig CSV-fil {filename}: {e}")
    
    return existing_data, missing_columns

def get_missing_data_for_page(page_id, page_token, since, until, missing_columns, page_name=None):
    """Hämta endast saknade kolumner för en befintlig sida"""
    display_name = page_name if page_name else page_id
    logger.debug(f"Hämtar saknade data för sida {display_name}: {', '.join(missing_columns)}")
    
    result = {}
    
    if "Publications" in missing_columns:
        result["Publications"] = get_page_publications(page_id, page_token, since, until, page_name)
    
    return result

def update_existing_page_data(existing_data, page_id, missing_data):
    """Uppdatera befintlig siddata med saknade värden"""
    if page_id in existing_data:
        for key, value in missing_data.items():
            existing_data[page_id][key] = value
        
        existing_data[page_id]["Status"] = "UPDATED"
        existing_data[page_id]["Comment"] = "Saknade kolumner tillagda"

def process_in_batches(page_list, cache, start_date, end_date, existing_data=None, missing_columns=None, batch_size=BATCH_SIZE):
    """Bearbeta sidor i batches för att förbättra prestanda"""
    total_pages = len(page_list)
    results = []
    success = 0
    failed = 0
    skipped = 0
    updated = 0
    
    if existing_data:
        results = list(existing_data.values())
        
    existing_page_ids = set(existing_data.keys()) if existing_data else set()
    
    pages_needing_full_processing = []
    pages_needing_partial_update = []
    
    for page_id, page_name in page_list:
        if page_id in existing_page_ids:
            if missing_columns and missing_columns:
                pages_needing_partial_update.append((page_id, page_name))
        else:
            pages_needing_full_processing.append((page_id, page_name))
    
    logger.info(f"Bearbetningsplan:")
    logger.info(f"  - Nya sidor (full bearbetning): {len(pages_needing_full_processing)}")
    logger.info(f"  - Befintliga sidor (partiell uppdatering): {len(pages_needing_partial_update)}")
    logger.info(f"  - Hoppar över: {len(existing_page_ids) - len(pages_needing_partial_update)}")
    
    if pages_needing_full_processing:
        logger.info(f"Bearbetar {len(pages_needing_full_processing)} nya sidor...")
        for i in range(0, len(pages_needing_full_processing), batch_size):
            batch = pages_needing_full_processing[i:i+batch_size]
            logger.info(f"Bearbetar ny-sidor batch {i//batch_size + 1}/{(len(pages_needing_full_processing) + batch_size - 1)//batch_size} ({len(batch)} sidor)")
            
            for page_id, page_name in batch:
                try:
                    name = page_name or get_page_name(page_id, cache)
                    
                    if not name:
                        logger.warning(f"Kunde inte hitta namn för sida {page_id}, hoppar över")
                        failed += 1
                        continue
                    
                    logger.info(f"Hämtar FULL data för: {name} (ID: {page_id})")
                    metrics = get_page_metrics(page_id, ACCESS_TOKEN, start_date, end_date, page_name=name)
                    
                    if metrics is not None:
                        page_result = {
                            "Page": name,
                            "Page ID": page_id,
                            "Reach": metrics["reach"],
                            "Engaged Users": metrics["engaged_users"],
                            "Engagements": metrics["engagements"],
                            "Reactions": metrics["reactions"],
                            "Publications": metrics["publications"],
                            "Status": metrics["status"],
                            "Comment": metrics.get("comment", "")
                        }
                        
                        results.append(page_result)
                        success += 1
                    else:
                        logger.warning(f"Inga data för sida {page_id} ({name})")
                        results.append({
                            "Page": name,
                            "Page ID": page_id,
                            "Reach": 0,
                            "Engaged Users": 0,
                            "Engagements": 0,
                            "Reactions": 0,
                            "Publications": 0,
                            "Status": "UNKNOWN",
                            "Comment": "Oväntat fel vid hämtning av data"
                        })
                        failed += 1
                except Exception as e:
                    logger.error(f"Fel vid bearbetning av sida {page_id}: {e}")
                    failed += 1
    
    if pages_needing_partial_update:
        logger.info(f"Uppdaterar {len(pages_needing_partial_update)} befintliga sidor med saknade kolumner...")
        for i in range(0, len(pages_needing_partial_update), batch_size):
            batch = pages_needing_partial_update[i:i+batch_size]
            logger.info(f"Bearbetar uppdatering-batch {i//batch_size + 1}/{(len(pages_needing_partial_update) + batch_size - 1)//batch_size} ({len(batch)} sidor)")
            
            for page_id, page_name in batch:
                try:
                    name = page_name or get_page_name(page_id, cache)
                    
                    if not name:
                        logger.warning(f"Kunde inte hitta namn för sida {page_id}, hoppar över")
                        failed += 1
                        continue
                    
                    logger.info(f"Uppdaterar saknade data för: {name} (ID: {page_id})")
                    
                    page_token = get_page_access_token(page_id, ACCESS_TOKEN)
                    
                    if not page_token:
                        logger.warning(f"Kunde inte hämta Page Access Token för sida {name}")
                        failed += 1
                        continue
                    
                    missing_data = get_missing_data_for_page(page_id, page_token, start_date, end_date, missing_columns, name)
                    
                    update_existing_page_data(existing_data, page_id, missing_data)
                    updated += 1
                    
                except Exception as e:
                    logger.error(f"Fel vid uppdatering av sida {page_id}: {e}")
                    failed += 1
    
    skipped = len(existing_page_ids) - len(pages_needing_partial_update)
    
    save_page_cache(cache)
    
    return results, success, failed, skipped, updated

def safe_int_value(value, default=0):
    """Säkerställer att ett värde är ett heltal, och hanterar olika datatyper"""
    if isinstance(value, (int, float)):
        return int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        return int(value)
    elif isinstance(value, dict):
        try:
            total = sum(int(v) for k, v in value.items() if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()))
            logger.info(f"Summerar reaktioner från dictionary: {value} = {total}")
            return total
        except Exception as e:
            logger.warning(f"Kunde inte summera dictionary-värde: {value}, fel: {e}, använder 0")
            return default
    else:
        return default

def save_results(data, filename):
    """Spara resultaten till en CSV-fil i årsspecifik katalog"""
    try:
        # Extrahera år från filnamnet
        year = extract_year_from_filename(filename)
        
        if year:
            # Skapa årsspecifik katalog
            year_dir = get_year_directory(year)
            ensure_directory_exists(year_dir)
            
            # Bygg fullständig sökväg
            full_path = os.path.join(year_dir, os.path.basename(filename))
        else:
            # Fallback till nuvarande år om vi inte kan extrahera året
            current_year = datetime.now().year
            year_dir = get_year_directory(current_year)
            ensure_directory_exists(year_dir)
            full_path = os.path.join(year_dir, os.path.basename(filename))
            logger.warning(f"Kunde inte extrahera år från {filename}, använder {current_year}")
        
        sorted_data = sorted(data, key=lambda x: safe_int_value(x.get("Reach", 0)), reverse=True)
        
        fieldnames = ["Page", "Page ID", "Reach"]
        
        if sorted_data and len(sorted_data) > 0:
            if "Engaged Users" in sorted_data[0]:
                fieldnames.append("Engaged Users")
            if "Engagements" in sorted_data[0]:
                fieldnames.append("Engagements")
            if "Reactions" in sorted_data[0]:
                fieldnames.append("Reactions")
            if "Publications" in sorted_data[0]:
                fieldnames.append("Publications")
            if "Status" in sorted_data[0]:
                fieldnames.append("Status")
            if "Comment" in sorted_data[0]:
                fieldnames.append("Comment")
        
        with open(full_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(sorted_data)
            
        logger.info(f"Sparade data till {full_path}")
        return True
    except Exception as e:
        logger.error(f"Kunde inte spara data: {e}")
        return False

def get_existing_reports():
    """Scanna alla reachÅRTAL-kataloger efter befintliga Facebook-räckviddsrapporter"""
    existing_reports = set()
    
    # Hitta alla reachÅRTAL-kataloger
    for directory in glob.glob("reach*/"):
        try:
            # Extrahera år från katalognamn
            year_str = directory.replace("reach", "").replace("/", "").replace("\\", "")
            if year_str.isdigit() and len(year_str) == 4:
                # Sök efter CSV-filer i denna katalog
                for filename in glob.glob(os.path.join(directory, "FB_*.csv")):
                    try:
                        basename = os.path.basename(filename)
                        parts = basename.replace(".csv", "").split("_")
                        if len(parts) == 3 and parts[0] == "FB":
                            year = parts[1]
                            month = parts[2]
                            if year.isdigit() and month.isdigit() and len(year) == 4 and len(month) == 2:
                                existing_reports.add(f"{year}-{month}")
                                logger.debug(f"Hittade befintlig rapport för {year}-{month}: {filename}")
                    except Exception as e:
                        logger.warning(f"Kunde inte tolka filnamn {filename}: {e}")
        except Exception as e:
            logger.warning(f"Kunde inte bearbeta katalog {directory}: {e}")
    
    return existing_reports

def get_missing_months(existing_reports, start_year_month):
    """Bestäm vilka månader som behöver bearbetas"""
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

def analyze_page_presence(previous_month, current_month):
    """Jämför sidor mellan två månader och identifierar nya och bortfallna sidor"""
    logger.info(f"Analyserar sidnärvaro mellan {previous_month} och {current_month}")
    
    prev_df = pd.read_csv(previous_month) if isinstance(previous_month, str) else previous_month
    curr_df = pd.read_csv(current_month) if isinstance(current_month, str) else current_month
    
    if isinstance(current_month, str):
        parts = os.path.basename(current_month).replace(".csv", "").split("_")
        if len(parts) >= 3:
            month_str = f"{parts[1]}-{parts[2]}"
        else:
            month_str = "Okänd"
    else:
        month_str = "Aktuell"
    
    prev_page_ids = set(prev_df["Page ID"].astype(str))
    curr_page_ids = set(curr_df["Page ID"].astype(str))
    
    new_page_ids = curr_page_ids - prev_page_ids
    missing_page_ids = prev_page_ids - curr_page_ids
    
    results = []
    
    for page_id in new_page_ids:
        page_info = curr_df[curr_df["Page ID"].astype(str) == page_id].iloc[0]
        results.append({
            "Page ID": page_id,
            "Page": page_info["Page"],
            "Status": "NY",
            "Månad": month_str,
            "Kommentar": "Inte med i föregående månad"
        })
    
    for page_id in missing_page_ids:
        page_info = prev_df[prev_df["Page ID"].astype(str) == page_id].iloc[0]
        results.append({
            "Page ID": page_id,
            "Page": page_info["Page"],
            "Status": "BORTFALLEN",
            "Månad": month_str,
            "Kommentar": "Fanns i föregående månad"
        })
    
    for _, row in curr_df.iterrows():
        page_id = str(row["Page ID"])
        if "Status" in row and row["Status"] != "OK" and row["Status"] != "SKIPPED":
            results.append({
                "Page ID": page_id,
                "Page": row["Page"],
                "Status": row["Status"],
                "Månad": month_str,
                "Kommentar": row.get("Comment", "")
            })
    
    result_df = pd.DataFrame(results)
    
    logger.info(f"Analys klar: {len(new_page_ids)} nya sidor, {len(missing_page_ids)} bortfallna sidor")
    
    return result_df

def save_status_report(status_df, year, month):
    """Sparar en statusrapport för en specifik månad i status-undermapp"""
    filename = f"FB_STATUS_{year}_{month:02d}.csv"
    
    # Skapa status-katalog i årsspecifik katalog
    status_dir = get_status_directory(year)
    ensure_directory_exists(status_dir)
    
    # Fullständig sökväg
    full_path = os.path.join(status_dir, filename)
    
    try:
        status_df.to_csv(full_path, index=False, encoding="utf-8")
        logger.info(f"Sparade statusrapport till {full_path}")
        return True
    except Exception as e:
        logger.error(f"Kunde inte spara statusrapport: {e}")
        return False

def generate_custom_filename(start_date, end_date):
    """Generera filnamn för custom datumintervall"""
    start_obj = datetime.strptime(start_date, "%Y-%m-%d")
    end_obj = datetime.strptime(end_date, "%Y-%m-%d")
    
    if start_obj.month == end_obj.month and start_obj.year == end_obj.year:
        if start_obj.day == 1 and end_obj == datetime(end_obj.year, end_obj.month, monthrange(end_obj.year, end_obj.month)[1]):
            return f"FB_{start_obj.year}_{start_obj.month:02d}.csv"
        else:
            return f"FB_{start_obj.year}_{start_obj.month:02d}_{start_obj.day:02d}-{end_obj.day:02d}.csv"
    else:
        return f"FB_{start_obj.strftime('%Y-%m-%d')}_to_{end_obj.strftime('%Y-%m-%d')}.csv"

def parse_date_args(args):
    """Tolka kommandoradsargument för datumintervall och returnera (start_date, end_date)"""
    today = datetime.now().date()
    
    if args.from_date and args.to_date:
        try:
            start_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
            end_date = datetime.strptime(args.to_date, "%Y-%m-%d").date()
            return str(start_date), str(end_date)
        except ValueError:
            logger.error("Felaktigt datumformat. Använd YYYY-MM-DD")
            sys.exit(1)
    
    if args.current_month_so_far:
        start_date = today.replace(day=1)
        end_date = today
        return str(start_date), str(end_date)
    
    if args.last_n_days:
        try:
            days = int(args.last_n_days)
            start_date = today - timedelta(days=days-1)
            end_date = today
            return str(start_date), str(end_date)
        except ValueError:
            logger.error("--last-n-days måste vara ett heltal")
            sys.exit(1)
    
    if args.last_week:
        start_date = today - timedelta(days=6)
        end_date = today
        return str(start_date), str(end_date)
    
    if args.last_month:
        start_date = today - timedelta(days=29)
        end_date = today
        return str(start_date), str(end_date)
    
    return None, None

def process_custom_period(start_date, end_date, cache, page_list=None, update_all=False):
    """Bearbeta data för ett custom datumintervall"""
    logger.info(f"Bearbetar custom period: {start_date} till {end_date}")
    
    output_file = generate_custom_filename(start_date, end_date)
    
    if not page_list:
        page_list = get_page_ids_with_access(ACCESS_TOKEN)
    
    if not page_list:
        logger.error("Inga sidor hittades. Avbryter.")
        return False
    
    page_list = filter_placeholder_pages(page_list)
    
    if not page_list:
        logger.error("Inga sidor kvar efter filtrering. Avbryter.")
        return False
    
    # Extrahera år för att bygga korrekt sökväg
    year = extract_year_from_filename(output_file)
    if year:
        year_dir = get_year_directory(year)
        full_output_path = os.path.join(year_dir, output_file)
    else:
        current_year = datetime.now().year
        year_dir = get_year_directory(current_year)
        full_output_path = os.path.join(year_dir, output_file)
    
    existing_data = {}
    missing_columns = set()
    if os.path.exists(full_output_path) and not update_all:
        existing_data, missing_columns = read_existing_csv(full_output_path)
        logger.info(f"Hittade {len(existing_data)} befintliga sidor i fil {full_output_path}")
        if missing_columns:
            logger.info(f"Saknade kolumner kommer att läggas till: {', '.join(missing_columns)}")
    
    all_data, ok, fail, skipped, updated = process_in_batches(
        page_list, cache, start_date, end_date, 
        existing_data=None if update_all else existing_data,
        missing_columns=None if update_all else missing_columns
    )
    
    if all_data:
        save_results(all_data, output_file)
        
        try:
            total_reach = sum(safe_int_value(item.get("Reach", 0)) for item in all_data)
            
            has_engaged = any("Engaged Users" in item for item in all_data)
            has_engagements = any("Engagements" in item for item in all_data)
            has_reactions = any("Reactions" in item for item in all_data)
            has_publications = any("Publications" in item for item in all_data)
            
            if has_engaged:
                total_engaged = sum(safe_int_value(item.get("Engaged Users", 0)) for item in all_data)
            else:
                total_engaged = 0
                
            if has_engagements:
                total_engagements = sum(safe_int_value(item.get("Engagements", 0)) for item in all_data)
            else:
                total_engagements = 0
                
            if has_reactions:
                total_reactions = sum(safe_int_value(item.get("Reactions", 0)) for item in all_data)
            else:
                total_reactions = 0
                
            if has_publications:
                total_publications = sum(safe_int_value(item.get("Publications", 0)) for item in all_data)
            else:
                total_publications = 0
            
            logger.info(f"Summering för {start_date} till {end_date}:")
            logger.info(f"  - Total räckvidd: {total_reach:,}")
            
            if has_engaged:
                logger.info(f"  - Engagerade användare: {total_engaged:,}")
            if has_engagements:
                logger.info(f"  - Totala interaktioner: {total_engagements:,}")
            if has_reactions:
                logger.info(f"  - Reaktioner: {total_reactions:,}")
            if has_publications:
                logger.info(f"  - Publiceringar: {total_publications:,}")
            
            if skipped > 0:
                logger.info(f"{skipped} sidor hoppades över")
            if updated > 0:
                logger.info(f"{updated} sidor uppdaterades med saknade kolumner")
                
            status_counts = {}
            for item in all_data:
                if "Status" in item:
                    status = item["Status"]
                    status_counts[status] = status_counts.get(status, 0) + 1
            
            if status_counts:
                logger.info(f"Statusöversikt:")
                for status, count in status_counts.items():
                    logger.info(f"  - {status}: {count} sidor")
        
        except Exception as e:
            logger.error(f"Fel vid beräkning av summor: {e}")
        
        return True
    else:
        logger.warning(f"Inga data att spara för {start_date} till {end_date}")
        return False

def process_month(year, month, cache, page_list=None, update_all=False, generate_status=True):
    """Bearbeta data för en specifik månad"""
    start_date = f"{year}-{month:02d}-01"
    
    last_day = monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day}"
    
    output_file = f"FB_{year}_{month:02d}.csv"
    
    logger.info(f"Bearbetar månad: {year}-{month:02d} (från {start_date} till {end_date})")
    
    if not page_list:
        page_list = get_page_ids_with_access(ACCESS_TOKEN)
    
    if not page_list:
        logger.error("Inga sidor hittades. Avbryter.")
        return False
    
    page_list = filter_placeholder_pages(page_list)
    
    if not page_list:
        logger.error("Inga sidor kvar efter filtrering. Avbryter.")
        return False
    
    # Bygg sökväg med årsspecifik katalog
    year_dir = get_year_directory(year)
    full_output_path = os.path.join(year_dir, output_file)
    
    existing_data = {}
    missing_columns = set()
    if os.path.exists(full_output_path) and not update_all:
        existing_data, missing_columns = read_existing_csv(full_output_path)
        logger.info(f"Hittade {len(existing_data)} befintliga sidor i fil {full_output_path}")
        if missing_columns:
            logger.info(f"Saknade kolumner kommer att läggas till: {', '.join(missing_columns)}")
    
    all_data, ok, fail, skipped, updated = process_in_batches(
        page_list, cache, start_date, end_date, 
        existing_data=None if update_all else existing_data,
        missing_columns=None if update_all else missing_columns
    )
    
    if all_data:
        save_results(all_data, output_file)
        
        try:
            total_reach = sum(safe_int_value(item.get("Reach", 0)) for item in all_data)
            
            has_engaged = any("Engaged Users" in item for item in all_data)
            has_engagements = any("Engagements" in item for item in all_data)
            has_reactions = any("Reactions" in item for item in all_data)
            has_publications = any("Publications" in item for item in all_data)
            
            if has_engaged:
                total_engaged = sum(safe_int_value(item.get("Engaged Users", 0)) for item in all_data)
            else:
                total_engaged = 0
                
            if has_engagements:
                total_engagements = sum(safe_int_value(item.get("Engagements", 0)) for item in all_data)
            else:
                total_engagements = 0
                
            if has_reactions:
                total_reactions = sum(safe_int_value(item.get("Reactions", 0)) for item in all_data)
            else:
                total_reactions = 0
                
            if has_publications:
                total_publications = sum(safe_int_value(item.get("Publications", 0)) for item in all_data)
            else:
                total_publications = 0
            
            logger.info(f"Summering för {year}-{month:02d}:")
            logger.info(f"  - Total räckvidd: {total_reach:,}")
            
            if has_engaged:
                logger.info(f"  - Engagerade användare: {total_engaged:,}")
            if has_engagements:
                logger.info(f"  - Totala interaktioner: {total_engagements:,}")
            if has_reactions:
                logger.info(f"  - Reaktioner: {total_reactions:,}")
            if has_publications:
                logger.info(f"  - Publiceringar: {total_publications:,}")
            
            if skipped > 0:
                logger.info(f"{skipped} sidor hoppades över")
            if updated > 0:
                logger.info(f"{updated} sidor uppdaterades med saknade kolumner")
                
            status_counts = {}
            for item in all_data:
                if "Status" in item:
                    status = item["Status"]
                    status_counts[status] = status_counts.get(status, 0) + 1
            
            if status_counts:
                logger.info(f"Statusöversikt:")
                for status, count in status_counts.items():
                    logger.info(f"  - {status}: {count} sidor")
            
            if generate_status:
                previous_month = f"{year}-{month-1:02d}" if month > 1 else f"{year-1}-12"
                prev_year, prev_month_num = previous_month.split("-")
                prev_year_dir = get_year_directory(int(prev_year))
                previous_file = os.path.join(prev_year_dir, f"FB_{prev_year}_{prev_month_num}.csv")
                
                if os.path.exists(previous_file):
                    logger.info(f"Genererar statusrapport genom att jämföra med {previous_file}")
                    try:
                        status_df = analyze_page_presence(previous_file, full_output_path)
                        save_status_report(status_df, year, month)
                    except Exception as e:
                        logger.error(f"Kunde inte generera statusrapport: {e}")
        
        except Exception as e:
            logger.error(f"Fel vid beräkning av summor: {e}")
        
        return True
    else:
        logger.warning(f"Inga data att spara för {year}-{month:02d}")
        return False

def main():
    """Huvudfunktion för att köra hela processen"""
    parser = argparse.ArgumentParser(description="Generera Facebook-räckviddsrapport för alla sidor och månader")
    
    date_group = parser.add_argument_group("Datumargument för månader")
    date_group.add_argument("--start", help="Startår-månad (YYYY-MM)")
    date_group.add_argument("--month", help="Kör endast för angiven månad (YYYY-MM)")
    
    custom_group = parser.add_argument_group("Custom datumintervall")
    custom_group.add_argument("--from", dest="from_date", help="Custom startdatum (YYYY-MM-DD)")
    custom_group.add_argument("--to", dest="to_date", help="Custom slutdatum (YYYY-MM-DD)")
    custom_group.add_argument("--current-month-so-far", action="store_true", 
                            help="Hämta data från 1:a i månaden till idag")
    custom_group.add_argument("--last-n-days", type=int, metavar="N",
                            help="Hämta data för senaste N dagar (inklusive idag)")
    custom_group.add_argument("--last-week", action="store_true", 
                            help="Hämta data för senaste 7 dagar (inklusive idag)")
    custom_group.add_argument("--last-month", action="store_true", 
                            help="Hämta data för senaste 30 dagar (inklusive idag)")
    
    ops_group = parser.add_argument_group("Operationsmodifikatorer")
    ops_group.add_argument("--update-all", action="store_true", 
                          help="Uppdatera alla sidor även om de redan finns i CSV-filen")
    ops_group.add_argument("--check-new", action="store_true", 
                          help="Kontrollera efter nya sidor i alla befintliga månader")
    ops_group.add_argument("--status", 
                          help="Generera endast statusrapport för angiven månad (YYYY-MM)")
    ops_group.add_argument("--debug", action="store_true", 
                          help="Aktivera debug-loggning")
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug-läge aktiverat")
    
    date_args_count = sum([
        bool(args.start), bool(args.month), bool(args.from_date and args.to_date),
        args.current_month_so_far, bool(args.last_n_days), args.last_week, args.last_month,
        args.check_new, bool(args.status)
    ])
    
    if date_args_count > 1:
        logger.error("Endast ett datumargument kan användas åt gången")
        parser.print_help()
        sys.exit(1)
    
    start_year_month = args.start or INITIAL_START_YEAR_MONTH
    
    logger.info(f"Facebook Reach & Interactions Report Generator – v2.8")
    logger.info(f"Startdatum: {start_year_month}")
    logger.info("Dynamisk rate limit-hantering aktiverad")
    logger.info("Årsbaserad katalogstruktur (reachÅRTAL/)")
    logger.info("-------------------------------------------------------------------")
    
    check_token_expiry()
    
    if not validate_token(ACCESS_TOKEN):
        logger.error("Token kunde inte valideras. Avbryter.")
        return
    
    cache = load_page_cache()
    
    if args.status:
        try:
            year, month = map(int, args.status.split("-"))
            year_dir = get_year_directory(year)
            current_file = os.path.join(year_dir, f"FB_{year}_{month:02d}.csv")
            
            if not os.path.exists(current_file):
                logger.error(f"Fil {current_file} hittades inte. Kan inte generera statusrapport.")
                return
                
            if month > 1:
                prev_month = month - 1
                prev_year = year
            else:
                prev_month = 12
                prev_year = year - 1
                
            prev_year_dir = get_year_directory(prev_year)
            prev_file = os.path.join(prev_year_dir, f"FB_{prev_year}_{prev_month:02d}.csv")
            
            if not os.path.exists(prev_file):
                logger.error(f"Fil {prev_file} hittades inte. Kan inte jämföra med föregående månad.")
                return
                
            logger.info(f"Genererar statusrapport för {year}-{month:02d}")
            status_df = analyze_page_presence(prev_file, current_file)
            save_status_report(status_df, year, month)
            return
        except Exception as e:
            logger.error(f"Fel vid generering av statusrapport: {e}")
            return
    
    page_list = get_page_ids_with_access(ACCESS_TOKEN)
    
    if not page_list:
        logger.error("Inga sidor hittades. Avbryter.")
        return
    
    page_list = filter_placeholder_pages(page_list)
    
    if not page_list:
        logger.error("Inga sidor kvar efter filtrering. Avbryter.")
        return
    
    start_date, end_date = parse_date_args(args)
    if start_date and end_date:
        logger.info(f"Kör för custom datumintervall: {start_date} till {end_date}")
        process_custom_period(start_date, end_date, cache, page_list, update_all=args.update_all)
        save_page_cache(cache)
        return
    
    if args.check_new:
        logger.info("Kontrollerar efter nya sidor i alla befintliga månader...")
        existing_reports = get_existing_reports()
        
        for report in sorted(existing_reports):
            year, month = map(int, report.split("-"))
            logger.info(f"Kontrollerar {year}-{month:02d} efter nya sidor...")
            process_month(year, month, cache, page_list, update_all=args.update_all, generate_status=True)
            
        logger.info("Kontroll efter nya sidor slutförd")
        save_page_cache(cache)
        return
    
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
            logger.info(f"Kör endast för specifik månad: {year}-{month:02d}")
            process_month(year, month, cache, page_list, update_all=args.update_all, generate_status=True)
            save_page_cache(cache)
            return
        except ValueError:
            logger.error(f"Ogiltigt månadsformat: {args.month}. Använd YYYY-MM.")
            return
    
    existing_reports = get_existing_reports()
    logger.info(f"Hittade {len(existing_reports)} befintliga rapporter: {', '.join(sorted(existing_reports)) if existing_reports else 'Inga'}")
    
    missing_months = get_missing_months(existing_reports, start_year_month)
    
    if not missing_months:
        logger.info("Alla månader är redan bearbetade. Inget att göra.")
        logger.info("Om du vill kontrollera efter nya sidor i befintliga rapporter, använd --check-new")
        return
    
    logger.info(f"Behöver bearbeta {len(missing_months)} saknade månader: {', '.join([f'{y}-{m:02d}' for y, m in missing_months])}")
    
    for year, month in missing_months:
        logger.info(f"Bearbetar data för {year}-{month:02d}...")
        
        success = process_month(year, month, cache, page_list, update_all=args.update_all, generate_status=True)
        
        save_page_cache(cache)
        
        if not success:
            logger.warning(f"Kunde inte slutföra bearbetningen för {year}-{month:02d}")
        else:
            logger.info(f"Slutförde bearbetningen för {year}-{month:02d}")
        
        if missing_months.index((year, month)) < len(missing_months) - 1:
            if rate_limit_backoff > 1.5:
                pause_time = min(MONTH_PAUSE_SECONDS, 30)
                logger.info(f"Pausar i {pause_time} sekunder mellan månader (pga tidigare rate limits)...")
                time.sleep(pause_time)
            else:
                logger.info("Fortsätter direkt till nästa månad (inga rate limit-problem)...")
    
    elapsed_time = time.time() - start_time
    avg_rate = api_call_count / (elapsed_time / 3600) if elapsed_time > 0 else 0
    logger.info(f"Total körtid: {elapsed_time:.1f} sekunder")
    logger.info(f"API-anrop: {api_call_count} totalt")
    logger.info(f"Genomsnittlig hastighet: {avg_rate:.0f} anrop/timme")
    if rate_limit_backoff > 1.0:
        logger.info(f"Slutlig backoff: {rate_limit_backoff:.1f}x (träffade rate limits under körningen)")
    else:
        logger.info(f"Inga rate limits träffades - maximal hastighet använd!")
    logger.info(f"Klar! Bearbetade {len(missing_months)} månader")

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

# ═══════════════════════════════════════════════════════════════
# HÄR SLUTAR DEL TVÅ
# ═══════════════════════════════════════════════════════════════