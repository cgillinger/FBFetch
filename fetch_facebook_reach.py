# fetch_facebook_reach.py

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
            logging.FileHandler(log_filename),  # Datumstämplad loggfil
            logging.FileHandler("facebook_reach.log"),  # Behåll den senaste loggfilen för enkelt åtkomst
            logging.StreamHandler()  # Terminal-utskrift
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Startar loggning till fil: {log_filename}")
    
    return logger

# Konfigurera loggning med datumstämplad fil
logger = setup_logging()

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
        
        if days_left <= 7:
            logger.warning(f"⚠️ VARNING: Din token går ut inom {days_left} dagar! Skapa en ny token snart.")
        elif days_left <= 0:
            logger.error(f"❌ KRITISKT: Din token har gått ut! Skapa en ny token omedelbart.")
            sys.exit(1)
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
            
            # För alla HTTP-svarkoder, försök tolka JSON-innehållet
            try:
                json_data = response.json()
                
                # Särskild hantering för 400-fel (Bad Request)
                if response.status_code == 400 and "error" in json_data:
                    error_code = json_data["error"].get("code")
                    error_msg = json_data["error"].get("message", "Okänt fel")
                    
                    # Hantera specifika felkoder
                    if error_code == 4:  # App-specifikt rate limit
                        wait_time = 60 * (attempt + 1)  # Vänta längre för varje försök
                        logger.warning(f"App rate limit: {error_msg}. Väntar {wait_time} sekunder...")
                        time.sleep(wait_time)
                        continue
                        
                    elif error_code == 190:  # Ogiltig token
                        logger.error(f"Access token ogiltig: {error_msg}")
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
            logger.debug(f"Hämtar nästa sida från: {next_url}")
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

def get_page_metrics(page_id, system_token, since, until, page_name=None):
    """Hämta räckvidd och interaktionsdata för en sida under en specifik tidsperiod"""
    display_name = page_name if page_name else page_id
    logger.debug(f"Hämtar metriker för sida {display_name} från {since} till {until}...")
    
    # Skapa resultatstruktur
    result = {
        "reach": 0,
        "engaged_users": 0,
        "engagements": 0,
        "reactions": 0,
        "reactions_details": {},  # Lagra detaljerade reaktionsdata
        "status": "OK",           # Defaultstatus
        "comment": ""             # Plats för ytterligare information om felet
    }
    
    # Först hämta en Page Access Token för denna specifika sida
    page_token = get_page_access_token(page_id, system_token)
    
    if not page_token:
        result["status"] = "NO_ACCESS"
        result["comment"] = "Kunde inte hämta Page Access Token"
        logger.warning(f"⚠️ Kunde inte hämta Page Access Token för sida {display_name}")
        return result
    
    # Definition av metriker och deras mappning
    metrics_mapping = [
        {"api_name": "page_impressions_unique", "result_key": "reach", "display_name": "Räckvidd"},
        {"api_name": "page_post_engagements", "result_key": "engagements", "display_name": "Interaktioner"},
        {"api_name": "page_actions_post_reactions_total", "result_key": "reactions", "display_name": "Reaktioner"}
    ]
    
    api_errors = []  # Samla fel från API-anrop
    
    # Hämta varje metrik separat för att isolera fel
    for metric_info in metrics_mapping:
        try:
            # Använd Page Access Token för att hämta insikter
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
                # Extrahera värden från svaret
                for metric in data["data"]:
                    if metric["values"] and len(metric["values"]) > 0:
                        value = metric["values"][0].get("value", 0)
                        
                        # Särskild hantering för reaktioner som kan vara dictionary
                        if metric_info["result_key"] == "reactions" and isinstance(value, dict):
                            # Spara detaljerade reaktionsdata
                            result["reactions_details"] = value
                            # Beräkna summan av alla reaktioner
                            total_reactions = sum(int(v) for k, v in value.items() 
                                              if isinstance(v, (int, float)) or 
                                              (isinstance(v, str) and v.isdigit()))
                            
                            logger.info(f"Reaktioner för {display_name}: {value}, totalt: {total_reactions}")
                            result[metric_info["result_key"]] = total_reactions
                        else:
                            result[metric_info["result_key"]] = value
                            
                        logger.debug(f"  ✓ {metric_info['display_name']} för {display_name}: {value}")
            elif data and "error" in data:
                # Här fångar vi upp och ger ett tydligt felmeddelande per metrik
                error_msg = data["error"].get("message", "Okänt fel")
                error_code = data["error"].get("code", "N/A")
                api_errors.append(f"{metric_info['display_name']}: {error_msg} (kod {error_code})")
                logger.error(f"Error {error_code}: Saknas mätvärde '{metric_info['display_name']}' för sida '{display_name}': {error_msg}")
            else:
                logger.warning(f"  ✗ Kunde inte hämta {metric_info['display_name']} för sida {display_name}: Inget data")
                
        except Exception as e:
            # Logga felet för denna specifika metrik
            api_errors.append(f"{metric_info['display_name']}: {str(e)}")
            logger.warning(f"  ✗ Fel vid hämtning av {metric_info['display_name']} för sida {display_name}: {e}")
            continue
    
    # Kontrollera och uppdatera status baserat på resultatet
    if api_errors:
        result["status"] = "API_ERROR"
        result["comment"] = "; ".join(api_errors[:3])  # Begränsa längden på kommentaren
    elif all(result[key] == 0 for key in ["reach", "engaged_users", "engagements", "reactions"]):
        result["status"] = "NO_DATA"
        result["comment"] = "Alla värden är noll"
    
    # Returnera resultatet oavsett status
    return result

def read_existing_csv(filename):
    """Läs in befintlig CSV-fil och returnera en dict med Page ID -> data"""
    existing_data = {}
    if os.path.exists(filename):
        try:
            with open(filename, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if "Page ID" in row:
                        # Konvertera numeriska värden till heltal
                        page_data = {
                            "Page": row["Page"],
                            "Page ID": row["Page ID"],
                            "Reach": int(row.get("Reach", 0))
                        }
                        
                        # Hantera nya interaktionsfält om de finns
                        if "Engaged Users" in row:
                            page_data["Engaged Users"] = int(row.get("Engaged Users", 0))
                        if "Engagements" in row:
                            page_data["Engagements"] = int(row.get("Engagements", 0))
                        if "Reactions" in row:
                            # Konvertera Reactions till heltal om möjligt
                            try:
                                page_data["Reactions"] = int(row.get("Reactions", 0))
                            except ValueError:
                                # Om det är ett dictionary eller annat format som inte kan konverteras
                                page_data["Reactions"] = 0
                        
                        # Hantera statusfält om det finns
                        if "Status" in row:
                            page_data["Status"] = row["Status"]
                        if "Comment" in row:
                            page_data["Comment"] = row["Comment"]
                            
                        existing_data[row["Page ID"]] = page_data
            logger.info(f"Läste in {len(existing_data)} befintliga sidor från {filename}")
        except Exception as e:
            logger.error(f"Fel vid inläsning av befintlig CSV-fil {filename}: {e}")
    return existing_data

def process_in_batches(page_list, cache, start_date, end_date, existing_data=None, batch_size=BATCH_SIZE):
    """Bearbeta sidor i batches för att förbättra prestanda"""
    total_pages = len(page_list)
    results = []
    success = 0
    failed = 0
    skipped = 0
    
    # Om vi har befintlig data, lägg till den i resultatlistan först
    if existing_data:
        # För varje existerande sida, sätt statusen till SKIPPED
        for page_id, data in existing_data.items():
            data["Status"] = "SKIPPED"
            data["Comment"] = "Redan i CSV-filen"
        results = list(existing_data.values())
        
    # Skapa en uppsättning av sidor som redan finns i befintlig data
    existing_page_ids = set(existing_data.keys()) if existing_data else set()
    
    for i in range(0, total_pages, batch_size):
        batch = page_list[i:i+batch_size]
        logger.info(f"Bearbetar batch {i//batch_size + 1}/{(total_pages + batch_size - 1)//batch_size} ({len(batch)} sidor)")
        
        for page_id, page_name in batch:
            # Hoppa över sidor som redan finns i den befintliga datan
            if page_id in existing_page_ids:
                logger.debug(f"Hoppar över sida {page_id} ({page_name}) eftersom den redan finns i CSV-filen")
                skipped += 1
                continue
                
            try:
                # Använd det redan kända namnet om det finns
                name = page_name or get_page_name(page_id, cache)
                
                if not name:
                    logger.warning(f"⚠️ Kunde inte hitta namn för sida {page_id}, hoppar över")
                    failed += 1
                    continue
                
                logger.info(f"📊 Hämtar data för: {name} (ID: {page_id}) [#{i + batch.index((page_id, page_name)) + 1}/{total_pages}]")
                metrics = get_page_metrics(page_id, ACCESS_TOKEN, start_date, end_date, page_name=name)
                
                if metrics is not None:
                    # Skapa basresultat med grundläggande metriker
                    page_result = {
                        "Page": name,
                        "Page ID": page_id,
                        "Reach": metrics["reach"],
                        "Engaged Users": metrics["engaged_users"],
                        "Engagements": metrics["engagements"],
                        "Reactions": metrics["reactions"],
                        "Status": metrics["status"],            # Lägg till status i resultatet
                        "Comment": metrics.get("comment", "")   # Lägg till eventuell kommentar
                    }
                    
                    # Lägg till detaljerade reaktioner om de finns
                    if "reactions_details" in metrics and metrics["reactions_details"]:
                        # Logga detaljerade reaktioner men inkludera dem inte i resultatet (kan göras om önskat)
                        reactions_details = metrics["reactions_details"]
                        logger.info(f"Detaljerade reaktioner för {name}: {reactions_details}")
                    
                    results.append(page_result)
                    success += 1
                else:
                    logger.warning(f"⚠️ Inga data för sida {page_id} ({name})")
                    results.append({
                        "Page": name,
                        "Page ID": page_id,
                        "Reach": 0,
                        "Engaged Users": 0,
                        "Engagements": 0,
                        "Reactions": 0,
                        "Status": "UNKNOWN",
                        "Comment": "Oväntat fel vid hämtning av data"
                    })
                    failed += 1
            except Exception as e:
                logger.error(f"Fel vid bearbetning av sida {page_id}: {e}")
                failed += 1
        
        # Visa framsteg
        total_processed = success + failed + skipped
        progress = total_processed / (total_pages + len(existing_page_ids)) * 100
        logger.info(f"Framsteg: {progress:.1f}% klar ({success} lyckade, {failed} misslyckade)")
        
        # Spara cache regelbundet för att inte förlora data vid fel
        if i % (batch_size * 5) == 0 and i > 0:
            save_page_cache(cache)
    
    return results, success, failed, skipped

def safe_int_value(value, default=0):
    """Säkerställer att ett värde är ett heltal, och hanterar olika datatyper"""
    if isinstance(value, (int, float)):
        return int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        return int(value)
    elif isinstance(value, dict):
        # Om det är ett dictionary med reaktioner, summera alla värden
        try:
            # Filtrera ut eventuella icke-numeriska värden
            total = sum(int(v) for k, v in value.items() if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()))
            logger.info(f"Summerar reaktioner från dictionary: {value} = {total}")
            return total
        except Exception as e:
            logger.warning(f"Kunde inte summera dictionary-värde: {value}, fel: {e}, använder 0")
            return default
    else:
        return default

def save_results(data, filename):
    """Spara resultaten till en CSV-fil"""
    try:
        # Sortera resultaten efter räckvidd (högst först)
        sorted_data = sorted(data, key=lambda x: safe_int_value(x.get("Reach", 0)), reverse=True)
        
        # Definiera fältnamn baserat på tillgängliga nycklar i första raden
        fieldnames = ["Page", "Page ID", "Reach"]
        
        # Lägg till interaktionsfält om de finns
        if sorted_data and len(sorted_data) > 0:
            if "Engaged Users" in sorted_data[0]:
                fieldnames.append("Engaged Users")
            if "Engagements" in sorted_data[0]:
                fieldnames.append("Engagements")
            if "Reactions" in sorted_data[0]:
                fieldnames.append("Reactions")
            # Lägg till Status och Comment om de finns
            if "Status" in sorted_data[0]:
                fieldnames.append("Status")
            if "Comment" in sorted_data[0]:
                fieldnames.append("Comment")
        
        with open(filename, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(sorted_data)
            
        logger.info(f"✅ Sparade data till {filename}")
        return True
    except Exception as e:
        logger.error(f"❌ Kunde inte spara data: {e}")
        return False

def get_existing_reports():
    """Scanna katalogen efter befintliga Facebook-räckviddsrapporter och returnera en uppsättning av bearbetade månader (YYYY-MM)"""
    existing_reports = set()
    for filename in glob.glob("FB_*.csv"):
        try:
            # Extrahera år och månad från filnamnet (FB_YYYY_MM.csv)
            parts = filename.replace(".csv", "").split("_")
            if len(parts) == 3 and parts[0] == "FB":
                year = parts[1]
                month = parts[2]
                if year.isdigit() and month.isdigit() and len(year) == 4 and len(month) == 2:
                    existing_reports.add(f"{year}-{month}")
                    logger.debug(f"Hittade befintlig rapport för {year}-{month}: {filename}")
        except Exception as e:
            logger.warning(f"Kunde inte tolka filnamn {filename}: {e}")
    return existing_reports

def get_missing_months(existing_reports, start_year_month):
    """Bestäm vilka månader som behöver bearbetas"""
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

def analyze_page_presence(previous_month, current_month):
    """
    Jämför sidor mellan två månader och identifierar nya och bortfallna sidor.
    
    Args:
        previous_month: Sökväg till CSV-fil för föregående månad eller pandas DataFrame
        current_month: Sökväg till CSV-fil för aktuell månad eller pandas DataFrame
        
    Returns:
        DataFrame med: Page ID, Page, Status (NY, BORTFALLEN, OFÖRÄNDRAD), Månad
    """
    logger.info(f"Analyserar sidnärvaro mellan {previous_month} och {current_month}")
    
    # Konvertera till DataFrame om strängar ges
    prev_df = pd.read_csv(previous_month) if isinstance(previous_month, str) else previous_month
    curr_df = pd.read_csv(current_month) if isinstance(current_month, str) else current_month
    
    # Extrahera år och månad från filnamn om det är en sträng
    if isinstance(current_month, str):
        parts = current_month.replace(".csv", "").split("_")
        if len(parts) >= 3:
            month_str = f"{parts[1]}-{parts[2]}"
        else:
            month_str = "Okänd"
    else:
        month_str = "Aktuell"
    
    # Hitta nya sidor (i current_month men inte i previous_month)
    prev_page_ids = set(prev_df["Page ID"].astype(str))
    curr_page_ids = set(curr_df["Page ID"].astype(str))
    
    new_page_ids = curr_page_ids - prev_page_ids
    missing_page_ids = prev_page_ids - curr_page_ids
    unchanged_page_ids = prev_page_ids.intersection(curr_page_ids)
    
    # Skapa en lista med alla sidor och deras status
    results = []
    
    # Lägg till nya sidor
    for page_id in new_page_ids:
        page_info = curr_df[curr_df["Page ID"].astype(str) == page_id].iloc[0]
        results.append({
            "Page ID": page_id,
            "Page": page_info["Page"],
            "Status": "NY",
            "Månad": month_str,
            "Kommentar": "Inte med i föregående månad"
        })
    
    # Lägg till bortfallna sidor
    for page_id in missing_page_ids:
        page_info = prev_df[prev_df["Page ID"].astype(str) == page_id].iloc[0]
        results.append({
            "Page ID": page_id,
            "Page": page_info["Page"],
            "Status": "BORTFALLEN",
            "Månad": month_str,
            "Kommentar": "Fanns i föregående månad"
        })
    
    # Lägg till oförändrade sidor (valfritt, kan bli många)
    # for page_id in unchanged_page_ids:
    #     page_info = curr_df[curr_df["Page ID"].astype(str) == page_id].iloc[0]
    #     results.append({
    #         "Page ID": page_id,
    #         "Page": page_info["Page"],
    #         "Status": "OFÖRÄNDRAD",
    #         "Månad": month_str,
    #         "Kommentar": ""
    #     })
    
    # Lägg till statusuppdateringar för nuvarande månad
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
    
    # Konvertera till DataFrame och returnera
    result_df = pd.DataFrame(results)
    
    logger.info(f"Analys klar: {len(new_page_ids)} nya sidor, {len(missing_page_ids)} bortfallna sidor")
    
    return result_df

def save_status_report(status_df, year, month):
    """Sparar en statusrapport för en specifik månad"""
    filename = f"FB_STATUS_{year}_{month:02d}.csv"
    
    try:
        status_df.to_csv(filename, index=False, encoding="utf-8")
        logger.info(f"✅ Sparade statusrapport till {filename}")
        return True
    except Exception as e:
        logger.error(f"❌ Kunde inte spara statusrapport: {e}")
        return False

def process_month(year, month, cache, page_list=None, update_all=False, generate_status=True):
    """Bearbeta data för en specifik månad"""
    # Sätt datumintervall för månaden
    start_date = f"{year}-{month:02d}-01"
    
    # Beräkna slutdatum (sista dagen i månaden)
    last_day = monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day}"
    
    # Sätt utdatafilnamn
    output_file = f"FB_{year}_{month:02d}.csv"
    
    logger.info(f"Bearbetar månad: {year}-{month:02d} (från {start_date} till {end_date})")
    
    # Hämta sidlista om den inte redan hämtats
    if not page_list:
        page_list = get_page_ids_with_access(ACCESS_TOKEN)
    
    if not page_list:
        logger.error("❌ Inga sidor hittades. Avbryter.")
        return False
    
    # Kontrollera om det finns befintlig data för denna månad
    existing_data = {}
    if os.path.exists(output_file) and not update_all:
        existing_data = read_existing_csv(output_file)
        logger.info(f"Hittade {len(existing_data)} befintliga sidor i fil {output_file}")
    
    # Bearbeta data för denna månad, hoppa över sidor som redan finns om inte update_all=True
    all_data, ok, fail, skipped = process_in_batches(page_list, cache, start_date, end_date, 
                                                  existing_data=None if update_all else existing_data)
    
    # Spara resultaten
    if all_data:
        save_results(all_data, output_file)
        
        # Visa total räckvidd och interaktioner för alla sidor med säker summering
        try:
            # Använd safe_int_value för att förhindra typfel vid summering
            total_reach = sum(safe_int_value(item.get("Reach", 0)) for item in all_data)
            
            # Beräkna totaler för interaktioner om tillgängligt
            has_engaged = any("Engaged Users" in item for item in all_data)
            has_engagements = any("Engagements" in item for item in all_data)
            has_reactions = any("Reactions" in item for item in all_data)
            
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
            
            logger.info(f"📈 Summering för {year}-{month:02d}:")
            logger.info(f"  - Total räckvidd: {total_reach:,}")
            
            if has_engaged:
                logger.info(f"  - Engagerade användare: {total_engaged:,}")
            if has_engagements:
                logger.info(f"  - Totala interaktioner: {total_engagements:,}")
            if has_reactions:
                logger.info(f"  - Reaktioner: {total_reactions:,}")
            
            if skipped > 0:
                logger.info(f"📈 {skipped} sidor fanns redan i CSV-filen och hoppades över")
                
            # Statusrapport om statuskolumn finns
            status_counts = {}
            for item in all_data:
                if "Status" in item:
                    status = item["Status"]
                    status_counts[status] = status_counts.get(status, 0) + 1
            
            if status_counts:
                logger.info(f"📋 Statusöversikt:")
                for status, count in status_counts.items():
                    logger.info(f"  - {status}: {count} sidor")
            
            # Generera statusrapport om föregående månad finns
            if generate_status:
                previous_month = f"{year}-{month-1:02d}" if month > 1 else f"{year-1}-12"
                previous_file = f"FB_{previous_month.split('-')[0]}_{previous_month.split('-')[1]}.csv"
                
                if os.path.exists(previous_file):
                    logger.info(f"Genererar statusrapport genom att jämföra med {previous_file}")
                    try:
                        status_df = analyze_page_presence(previous_file, output_file)
                        save_status_report(status_df, year, month)
                    except Exception as e:
                        logger.error(f"Kunde inte generera statusrapport: {e}")
        
        except Exception as e:
            logger.error(f"Fel vid beräkning av summor: {e}")
        
        return True
    else:
        logger.warning(f"⚠️ Inga data att spara för {year}-{month:02d}")
        return False

def main():
    """Huvudfunktion för att köra hela processen"""
    # Parsa kommandoradsargument
    parser = argparse.ArgumentParser(description="Generera Facebook-räckviddsrapport för alla sidor och månader")
    parser.add_argument("--start", help="Startår-månad (YYYY-MM)")
    parser.add_argument("--month", help="Kör endast för angiven månad (YYYY-MM)")
    parser.add_argument("--update-all", action="store_true", help="Uppdatera alla sidor även om de redan finns i CSV-filen")
    parser.add_argument("--check-new", action="store_true", help="Kontrollera efter nya sidor i alla befintliga månader")
    parser.add_argument("--status", help="Generera endast statusrapport för angiven månad (YYYY-MM)")
    parser.add_argument("--debug", action="store_true", help="Aktivera debug-loggning")
    args = parser.parse_args()
    
    # Sätt debug-läge om begärt
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug-läge aktiverat")
    
    # Använd argument om de finns
    start_year_month = args.start or INITIAL_START_YEAR_MONTH
    
    logger.info(f"📊 Facebook Reach & Interactions Report Generator – v2.0")
    logger.info(f"Startdatum: {start_year_month}")
    logger.info("-------------------------------------------------------------------")
    
    # Kontrollera token och varna om den snart går ut
    check_token_expiry()
    
    # Validera token
    if not validate_token(ACCESS_TOKEN):
        logger.error("❌ Token kunde inte valideras. Avbryter.")
        return
    
    # Ladda cache för sidnamn
    cache = load_page_cache()
    
    # Om --status används, generera endast statusrapport
    if args.status:
        try:
            year, month = map(int, args.status.split("-"))
            current_file = f"FB_{year}_{month:02d}.csv"
            
            if not os.path.exists(current_file):
                logger.error(f"❌ Fil {current_file} hittades inte. Kan inte generera statusrapport.")
                return
                
            # Bestäm föregående månad
            if month > 1:
                prev_month = month - 1
                prev_year = year
            else:
                prev_month = 12
                prev_year = year - 1
                
            prev_file = f"FB_{prev_year}_{prev_month:02d}.csv"
            
            if not os.path.exists(prev_file):
                logger.error(f"❌ Fil {prev_file} hittades inte. Kan inte jämföra med föregående månad.")
                return
                
            logger.info(f"Genererar statusrapport för {year}-{month:02d}")
            status_df = analyze_page_presence(prev_file, current_file)
            save_status_report(status_df, year, month)
            return
        except Exception as e:
            logger.error(f"❌ Fel vid generering av statusrapport: {e}")
            return
    
    # Hämta alla tillgängliga sidor (en gång för alla månader)
    page_list = get_page_ids_with_access(ACCESS_TOKEN)
    
    if not page_list:
        logger.error("❌ Inga sidor hittades. Avbryter.")
        return
    
    # Om check-new-argument, kontrollera alla befintliga månader efter nya sidor
    if args.check_new:
        logger.info("Kontrollerar efter nya sidor i alla befintliga månader...")
        existing_reports = get_existing_reports()
        
        for report in sorted(existing_reports):
            year, month = map(int, report.split("-"))
            logger.info(f"Kontrollerar {year}-{month:02d} efter nya sidor...")
            process_month(year, month, cache, page_list, update_all=args.update_all, generate_status=True)
            
        logger.info("✅ Kontroll efter nya sidor slutförd")
        save_page_cache(cache)
        return
    
    # Om specifik månad angivits, kör endast den
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
    
    # Hämta befintliga rapporter
    existing_reports = get_existing_reports()
    logger.info(f"Hittade {len(existing_reports)} befintliga rapporter: {', '.join(sorted(existing_reports)) if existing_reports else 'Inga'}")
    
    # Få saknade månader
    missing_months = get_missing_months(existing_reports, start_year_month)
    
    if not missing_months:
        logger.info("✅ Alla månader är redan bearbetade. Inget att göra.")
        logger.info("Om du vill kontrollera efter nya sidor i befintliga rapporter, använd --check-new")
        return
    
    logger.info(f"Behöver bearbeta {len(missing_months)} saknade månader: {', '.join([f'{y}-{m:02d}' for y, m in missing_months])}")
    
    # Bearbeta varje saknad månad
    for year, month in missing_months:
        logger.info(f"⏳ Bearbetar data för {year}-{month:02d}...")
        
        # Bearbeta denna månad
        success = process_month(year, month, cache, page_list, update_all=args.update_all, generate_status=True)
        
        # Spara cache efter varje månad
        save_page_cache(cache)
        
        if not success:
            logger.warning(f"⚠️ Kunde inte slutföra bearbetningen för {year}-{month:02d}")
        else:
            logger.info(f"✅ Slutförde bearbetningen för {year}-{month:02d}")
        
        # Pausa för att respektera API-begränsningar om det finns fler månader att bearbeta
        if missing_months.index((year, month)) < len(missing_months) - 1:
            logger.info(f"Pausar i {MONTH_PAUSE_SECONDS} sekunder för att respektera API-begränsningar...")
            time.sleep(MONTH_PAUSE_SECONDS)
    
    # Visa statistik om API-användning
    elapsed_time = time.time() - start_time
    logger.info(f"⏱️ Total körtid: {elapsed_time:.1f} sekunder")
    logger.info(f"🌐 API-anrop: {api_call_count} ({api_call_count/elapsed_time*3600:.1f}/timme)")
    logger.info(f"✅ Klar! Bearbetade {len(missing_months)} månader")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Avbruten av användare. Sparar mellanlagrat arbete...")
        # Här kunde vi implementera att spara framsteg
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Oväntat fel: {e}")
        import traceback
        logger.critical(traceback.format_exc())
        sys.exit(1)
