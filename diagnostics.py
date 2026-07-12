# diagnostics.py
# Komplett diagnostikskript för Facebook räckviddsmätningar

import csv
import json
import os
import time
import requests
import logging
import argparse
import sys
import glob
from datetime import datetime, timedelta
from calendar import monthrange
from config import (
    ACCESS_TOKEN, TOKEN_LAST_UPDATED, INITIAL_START_YEAR_MONTH,
    API_VERSION, CACHE_FILE, 
    BATCH_SIZE, MAX_RETRIES, RETRY_DELAY, 
    TOKEN_VALID_DAYS, MAX_REQUESTS_PER_HOUR,
    MONTH_PAUSE_SECONDS
)

# Konfigurera loggning
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("facebook_reach_diagnostic.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Räknare för API-anrop
api_call_count = 0
start_time = time.time()

def check_token_expiry():
    """Kontrollera om token snart går ut och varna användaren"""
    try:
        last_updated = datetime.strptime(TOKEN_LAST_UPDATED, "%Y-%m-%d")
        days_since = (datetime.now() - last_updated).days
        days_left = TOKEN_VALID_DAYS - days_since
        
        logger.info(f"🔑 Token skapades för {days_since} dagar sedan ({days_left} dagar kvar till utgång).")
        
        if days_left <= 0:
            logger.error(f"❌ KRITISKT: Din token har gått ut! Skapa en ny token omedelbart.")
            sys.exit(1)
        elif days_left <= 7:
            logger.warning(f"⚠️ VARNING: Din token går ut inom {days_left} dagar! Skapa en ny token snart.")
    except Exception as e:
        logger.error(f"⚠️ Kunde inte tolka TOKEN_LAST_UPDATED: {e}")

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
    """Gör API-förfrågan med återförsök och rate limit-hantering"""
    global api_call_count
    
    # Kontrollera om vi närmar oss rate limit
    current_time = time.time()
    elapsed_hours = (current_time - start_time) / 3600
    rate = api_call_count / elapsed_hours if elapsed_hours > 0 else 0
    
    if rate > MAX_REQUESTS_PER_HOUR * 0.9:  # Om vi använt 90% av rate limit
        wait_time = 3600 / MAX_REQUESTS_PER_HOUR  # Vänta tillräckligt för att hålla oss under gränsen
        logger.warning(f"Närmar oss rate limit ({int(rate)}/h). Väntar {wait_time:.1f} sekunder...")
        time.sleep(wait_time)
    
    for attempt in range(retries):
        try:
            api_call_count += 1
            response = requests.get(url, params=params, timeout=30)
            
            # Hantera vanliga HTTP-fel
            if response.status_code == 429:  # Too Many Requests
                retry_after = int(response.headers.get('Retry-After', RETRY_DELAY))
                logger.warning(f"Rate limit nått! Väntar {retry_after} sekunder... (försök {attempt+1}/{retries})")
                time.sleep(retry_after)
                continue
                
            elif response.status_code >= 500:  # Server error
                wait_time = RETRY_DELAY * (2 ** attempt)  # Exponentiell backoff
                logger.warning(f"Serverfel: {response.status_code}. Väntar {wait_time} sekunder... (försök {attempt+1}/{retries})")
                time.sleep(wait_time)
                continue
                
            elif response.status_code == 400:  # Bad Request
                data = response.json()
                if "error" in data:
                    error_code = data["error"].get("code")
                    error_msg = data["error"].get("message", "Okänt fel")
                    
                    # Hantera specifika felkoder
                    if error_code == 4:  # App-specifikt rate limit
                        wait_time = 60 * (attempt + 1)  # Vänta längre för varje försök
                        logger.warning(f"App rate limit: {error_msg}. Väntar {wait_time} sekunder...")
                        time.sleep(wait_time)
                        continue
                        
                    elif error_code == 190:  # Ogiltig token
                        logger.error(f"Access token ogiltig: {error_msg}")
                        return None
                        
            # Om allt ovan misslyckas och responskoden fortfarande är en felsignal
            if response.status_code != 200:
                logger.error(f"HTTP-fel {response.status_code}: {response.text}")
                if attempt < retries - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.info(f"Väntar {wait_time} sekunder innan nytt försök... (försök {attempt+1}/{retries})")
                    time.sleep(wait_time)
                    continue
                return None
                
            # Analysera JSON-svaret
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
    """Validera att token är giltig och hämta användarbehörigheter"""
    logger.info("Validerar token...")
    url = f"https://graph.facebook.com/{API_VERSION}/debug_token"
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
        
        # Hantera paginering
        next_url = data.get("paging", {}).get("next")
        if next_url and next_url != url:
            # Logga inte själva URL:en — pagineringslänken innehåller access_token
            logger.debug(f"Hämtar nästa sida med paginering ({len(pages)} sidor hittills)...")
        else:
            break
    
    if not pages:
        logger.warning("Inga sidor hittades. Token kanske saknar 'pages_show_list'-behörighet.")
    
    page_ids = [(page["id"], page["name"]) for page in pages]
    logger.info(f"✅ Hittade {len(page_ids)} sidor att analysera")
    return page_ids

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
        logger.warning(f"⚠️ Kunde inte hämta namn för sida {page_id}: {error_msg}")
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
        logger.warning(f"⚠️ Kunde inte hämta Page Access Token för sida {page_id}: {error_msg}")
        return None
    
    return data["access_token"]

def get_single_metric(page_id, page_token, since, until, metric_name, period="total_over_range"):
    """Hämta ett enskilt mätvärde från Facebook API"""
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}/insights"
    params = {
        "access_token": page_token,
        "since": since,
        "until": until,
        "period": period,
        "metric": metric_name
    }
    
    data = api_request(url, params)
    
    if not data or "error" in data:
        if data and "error" in data:
            error_msg = data["error"].get("message", "Okänt fel")
            if "must be a valid insights metric" in error_msg:
                logger.debug(f"Metriken '{metric_name}' finns inte för sida {page_id} (vanligt för mindre sidor)")
            else:
                logger.debug(f"Kunde inte hämta {metric_name} för sida {page_id}: {error_msg}")
        return 0
    
    if "data" not in data or not data["data"]:
        return 0
    
    # Extrahera värdet från svaret
    for item in data["data"]:
        if item.get("name") == metric_name and item.get("values") and len(item["values"]) > 0:
            return item["values"][0].get("value", 0)
    
    return 0

def process_month_diagnostic(year, month, test_metrics):
    """Kör diagnostik för en månad med olika mätvärden"""
    # Sätt datumintervall för månaden
    start_date = f"{year}-{month:02d}-01"
    last_day = monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day}"
    
    logger.info(f"Diagnostisk körning för {year}-{month:02d} från {start_date} till {end_date}")
    
    # Hämta sidlista
    page_list = get_page_ids_with_access(ACCESS_TOKEN)
    if not page_list:
        logger.error("❌ Inga sidor hittades. Avbryter.")
        return False
    
    # Skapa en cache för sidnamn
    cache = load_page_cache()
    
    # Förbered resultatlista för varje mätvärde
    results_by_metric = {metric: [] for metric in test_metrics.keys()}
    
    # Bearbeta alla sidor
    total_pages = len(page_list)
    success = 0
    failed = 0
    
    for i, (page_id, page_name) in enumerate(page_list):
        try:
            name = page_name or get_page_name(page_id, cache)
            if not name:
                logger.warning(f"⚠️ Kunde inte hitta namn för sida {page_id}, hoppar över")
                failed += 1
                continue
            
            logger.info(f"📊 Hämtar diagnostikdata för: {name} (ID: {page_id}) [{i+1}/{total_pages}]")
            
            # Hämta page token
            page_token = get_page_access_token(page_id, ACCESS_TOKEN)
            if not page_token:
                logger.warning(f"⚠️ Kunde inte hämta token för sida {page_id}, hoppar över")
                failed += 1
                continue
            
            # Testa varje mätvärde separat
            page_results = {"Page": name, "Page ID": page_id}
            
            for metric_key, metric_details in test_metrics.items():
                metric_name = metric_details["api_name"]
                try:
                    value = get_single_metric(page_id, page_token, start_date, end_date, metric_name)
                    page_results[metric_key] = value
                    logger.debug(f"  - {metric_key}: {value}")
                except Exception as e:
                    logger.warning(f"Kunde inte hämta {metric_key} för {name}: {e}")
                    page_results[metric_key] = 0
            
            # Lägg till resultaten i respektive lista
            for metric_key in test_metrics.keys():
                results_by_metric[metric_key].append({
                    "Page": name,
                    "Page ID": page_id,
                    "Reach": page_results[metric_key]  # Använd detta mätvärde som "Reach"
                })
            
            success += 1
            
            # Visa framsteg
            progress = (i + 1) / total_pages * 100
            logger.info(f"Framsteg: {progress:.1f}% klar ({success} lyckade, {failed} misslyckade)")
            
        except Exception as e:
            logger.error(f"Fel vid bearbetning av sida {page_id}: {e}")
            failed += 1
    
    # Spara resultat för varje mätvärde till separata filer
    for metric_key, results in results_by_metric.items():
        if results:
            output_file = f"FB_{year}_{month:02d}_{metric_key}.csv"
            try:
                # Sortera resultaten efter räckvidd (högst först)
                sorted_data = sorted(results, key=lambda x: x.get("Reach", 0), reverse=True)
                
                with open(output_file, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=["Page", "Page ID", "Reach"])
                    writer.writeheader()
                    writer.writerows(sorted_data)
                    
                total_reach = sum(item.get("Reach", 0) for item in results)
                logger.info(f"✅ Sparade data för {metric_key} till {output_file} (Total: {total_reach:,})")
            except Exception as e:
                logger.error(f"❌ Kunde inte spara data för {metric_key}: {e}")
    
    # Spara cache
    save_page_cache(cache)
    
    # Skapa en jämförelsefil
    create_comparison_report(year, month, test_metrics, results_by_metric)
    
    return True

def create_comparison_report(year, month, test_metrics, results_by_metric):
    """Skapa en sammanfattande jämförelserapport"""
    output_file = f"FB_{year}_{month:02d}_comparison.csv"
    
    try:
        # Förbereda jämförelsedata
        comparison_data = []
        
        # Samla alla page_ids från alla resultat
        all_page_ids = set()
        for results in results_by_metric.values():
            all_page_ids.update(item["Page ID"] for item in results)
        
        # Skapa en mapp från page_id till sidnamn
        name_map = {}
        for results in results_by_metric.values():
            for item in results:
                name_map[item["Page ID"]] = item["Page"]
        
        # Skapa jämförelsedata för varje sida
        for page_id in all_page_ids:
            page_data = {"Page": name_map.get(page_id, f"Page {page_id}"), "Page ID": page_id}
            
            for metric_key in test_metrics.keys():
                # Hitta räckvidd för denna sida i detta mätvärdes resultat
                metric_results = results_by_metric[metric_key]
                matching_results = [item for item in metric_results if item["Page ID"] == page_id]
                
                if matching_results:
                    page_data[metric_key] = matching_results[0]["Reach"]
                else:
                    page_data[metric_key] = 0
            
            comparison_data.append(page_data)
        
        # Sortera efter det första mätvärdet (högst först)
        first_metric = next(iter(test_metrics.keys()))
        sorted_data = sorted(comparison_data, key=lambda x: x.get(first_metric, 0), reverse=True)
        
        # Spara jämförelsefil
        fieldnames = ["Page", "Page ID"] + list(test_metrics.keys())
        with open(output_file, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(sorted_data)
        
        logger.info(f"✅ Sparade jämförelserapport till {output_file}")
        
        # Beräkna totaler för varje mätvärde
        totals = {}
        for metric_key in test_metrics.keys():
            totals[metric_key] = sum(item.get(metric_key, 0) for item in comparison_data)
        
        logger.info("Jämförelse av totala räckvidder:")
        for metric_key, total in totals.items():
            description = test_metrics[metric_key]["description"]
            logger.info(f"  - {description}: {total:,}")
        
    except Exception as e:
        logger.error(f"❌ Kunde inte skapa jämförelserapport: {e}")

def main():
    """Huvudfunktion för att köra diagnostik"""
    # Parsa kommandoradsargument
    parser = argparse.ArgumentParser(description="Diagnostisk körning av Facebook-räckviddsmått")
    parser.add_argument("--start", help="Startår-månad (YYYY-MM)")
    parser.add_argument("--month", help="Specifik månad att testa (YYYY-MM)")
    parser.add_argument("--debug", action="store_true", help="Aktivera debug-loggning")
    args = parser.parse_args()
    
    # Sätt debug-läge om begärt
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug-läge aktiverat")
    
    logger.info(f"📊 Facebook Reach Diagnostic Tool")
    logger.info("-------------------------------------------------------------------")
    
    # Kontrollera token och varna om den snart går ut
    check_token_expiry()
    
    # Validera token
    if not validate_token(ACCESS_TOKEN):
        logger.error("❌ Token kunde inte valideras. Avbryter.")
        return
    
    # Definiera mätvärden att testa
    test_metrics = {
        "total_unique": {
            "api_name": "page_impressions_unique",
            "description": "Unika visningar (total räckvidd)"
        },
        "organic_unique": {
            "api_name": "page_impressions_unique_organic",
            "description": "Organisk räckvidd (unika användare)"
        },
        "total_impressions": {
            "api_name": "page_impressions",
            "description": "Totala visningar (inklusive upprepade)"
        },
        "page_engaged_users": {
            "api_name": "page_engaged_users",
            "description": "Engagerade användare"
        }
    }
    
    # Bestäm vilken månad att diagnostisera
    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
            process_month_diagnostic(year, month, test_metrics)
        except ValueError:
            logger.error(f"❌ Ogiltigt månadsformat: {args.month}. Använd YYYY-MM (t.ex. 2025-01)")
    else:
        # Använd senaste avslutade månaden
        now = datetime.now()
        if now.month == 1:
            year, month = now.year - 1, 12
        else:
            year, month = now.year, now.month - 1
        
        logger.info(f"Ingen månad specificerad, använder senaste avslutade månaden: {year}-{month:02d}")
        process_month_diagnostic(year, month, test_metrics)
    
    logger.info("✅ Diagnostik slutförd!")

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
