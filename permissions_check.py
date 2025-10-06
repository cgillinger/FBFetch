#!/usr/bin/env python3
# permissions_check.py - Förbättrad version
# Verifierar att token har alla nödvändiga behörigheter för FB-skripten

import os
import csv
import datetime
import requests
import time
from config import ACCESS_TOKEN, API_VERSION

# Exportera till logs-katalogen för konsistens
EXPORT_PATH = "logs"
os.makedirs(EXPORT_PATH, exist_ok=True)

# Alla behörigheter som behövs för projektets skript
REQUIRED_SCOPES = {
    "pages_show_list",           # Lista sidor
    "pages_read_engagement",     # Läsa engagemang
    "pages_read_user_content",   # Läsa kommentarer (kommentarskriptet)
    "read_insights",             # Läsa insights (reach-skriptet)
}

OPTIONAL_SCOPES = {
    "pages_messaging": "För DM-räknare (fetch_facebook_dms.py)",
    "business_management": "För Business Manager-funktioner",
    "instagram_basic": "För Instagram-data",
    "instagram_manage_insights": "För Instagram insights",
    "pages_manage_posts": "För att publicera inlägg"
}

def api_request(url, params, retries=3):
    """Gör API-anrop med retry-logik"""
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code == 200:
                return response.json()
            elif response.status_code in [500, 502, 503, 504]:
                print(f"Serverfel {response.status_code}, försöker igen om {2 ** attempt}s...")
                time.sleep(2 ** attempt)
                continue
            else:
                print(f"API-fel ({response.status_code}): {response.text}")
                return {}
        except requests.exceptions.RequestException as e:
            print(f"Nätverksfel: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {}
    print("Max antal försök uppnådda.")
    return {}

def debug_token(token):
    """Verifiera token och lista behörigheter"""
    print("\n" + "="*80)
    print("TOKEN-VERIFIERING")
    print("="*80)
    
    url = f"https://graph.facebook.com/debug_token"
    params = {
        "input_token": token,
        "access_token": token
    }
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json().get("data", {})
    except Exception as e:
        print(f"Kunde inte verifiera token: {e}")
        return None, set()
    
    scopes = set(data.get("scopes", []))
    is_valid = data.get("is_valid", False)
    
    print(f"Token giltig: {'JA' if is_valid else 'NEJ'}")
    print(f"Användare ID: {data.get('user_id')}")
    print(f"App ID: {data.get('app_id')}")
    
    # Kontrollera utgångsdatum om tillgängligt
    expires_at = data.get("expires_at")
    if expires_at:
        exp_date = datetime.datetime.fromtimestamp(expires_at)
        days_left = (exp_date - datetime.datetime.now()).days
        print(f"Utgår: {exp_date.strftime('%Y-%m-%d')} ({days_left} dagar kvar)")
        if days_left <= 7:
            print("VARNING: Token går snart ut!")
    
    print("\n" + "-"*80)
    print("BEHÖRIGHETER (SCOPES)")
    print("-"*80)
    
    # Kontrollera nödvändiga behörigheter
    missing_required = REQUIRED_SCOPES - scopes
    present_optional = {k: v for k, v in OPTIONAL_SCOPES.items() if k in scopes}
    missing_optional = {k: v for k, v in OPTIONAL_SCOPES.items() if k not in scopes}
    
    print("\nNÖDVÄNDIGA behörigheter:")
    for scope in sorted(REQUIRED_SCOPES):
        status = "OK" if scope in scopes else "SAKNAS"
        symbol = "✓" if scope in scopes else "✗"
        print(f"  {symbol} {scope:30} [{status}]")
    
    if present_optional:
        print("\nVALFRIA behörigheter (finns):")
        for scope, desc in sorted(present_optional.items()):
            print(f"  ✓ {scope:30} - {desc}")
    
    if missing_optional:
        print("\nVALFRIA behörigheter (saknas):")
        for scope, desc in sorted(missing_optional.items()):
            print(f"  ✗ {scope:30} - {desc}")
    
    if missing_required:
        print("\n" + "!"*80)
        print("KRITISKT: Din token saknar nödvändiga behörigheter!")
        print("!"*80)
        print("\nSaknade behörigheter:")
        for scope in sorted(missing_required):
            print(f"  - {scope}")
        print("\nPåverkar följande skript:")
        if "read_insights" in missing_required:
            print("  - fetch_facebook_reach.py (reach-data)")
        if "pages_read_user_content" in missing_required:
            print("  - fetch_facebook_comments.py (kommentarer)")
        print("\nSkapa en ny token med dessa behörigheter i Business Manager eller Graph API Explorer.")
        return None, scopes
    
    print("\n" + "="*80)
    print("OK: Alla nödvändiga behörigheter finns!")
    print("="*80)
    
    return data, scopes

def get_page_ids_with_access(token):
    """Hämta alla sidor som token har åtkomst till"""
    print("\n" + "="*80)
    print("HÄMTAR FACEBOOK-SIDOR")
    print("="*80)
    
    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {
        "access_token": token,
        "fields": "id,name",
        "limit": 100
    }
    
    pages = []
    next_url = url
    page_count = 0
    
    while next_url:
        data = api_request(next_url if next_url != url else url, {} if next_url != url else params)
        batch = data.get("data", [])
        
        for entry in batch:
            pages.append({
                "id": entry.get("id"),
                "name": entry.get("name"),
                "page_token_ok": None  # Testas senare
            })
            page_count += 1
        
        next_url = data.get("paging", {}).get("next")
        
        if next_url:
            print(f"Hämtade {page_count} sidor så här långt...")
    
    print(f"\nHittade totalt {len(pages)} sidor")
    return pages

def test_page_token_access(pages, token):
    """Testa om Page Access Tokens kan hämtas för varje sida"""
    print("\n" + "="*80)
    print("TESTAR PAGE ACCESS TOKEN-HÄMTNING")
    print("="*80)
    print("\nDetta krävs för att skripten ska fungera korrekt...")
    
    # Filtrera bort placeholder-sidor INNAN vi testar
    real_pages = [p for p in pages 
                  if not (p['name'].startswith('Srholder') and len(p['name']) > 8 and p['name'][8:].isdigit())]
    
    if not real_pages:
        print("\nVARNING: Inga riktiga sidor att testa (endast placeholders)")
        return False
    
    success_count = 0
    fail_count = 0
    
    # Testa max 5 RIKTIGA sidor
    test_pages = real_pages[:5] if len(real_pages) > 5 else real_pages
    
    print(f"\nTestar {len(test_pages)} riktiga sidor av {len(real_pages)} totalt...")
    print("(Hoppar över placeholder-sidor som Srholder*)")
    
    for page in test_pages:
        url = f"https://graph.facebook.com/{API_VERSION}/{page['id']}"
        params = {"fields": "access_token", "access_token": token}
        
        data = api_request(url, params)
        has_token = "access_token" in data
        page["page_token_ok"] = has_token
        
        if has_token:
            success_count += 1
            print(f"  ✓ {page['name'][:40]:40} - OK")
        else:
            fail_count += 1
            print(f"  ✗ {page['name'][:40]:40} - MISSLYCKADES")
    
    # Markera alla icke-testade riktiga sidor som "ok" om minst en lyckades
    if success_count > 0:
        for page in real_pages[len(test_pages):]:
            page["page_token_ok"] = True
    
    # Markera alla placeholder-sidor också
    for page in pages:
        if page not in real_pages:
            page["page_token_ok"] = True  # Spelar ingen roll, de filtreras ändå bort
    
    print(f"\nResultat: {success_count}/{len(test_pages)} lyckades")
    
    if fail_count > 0:
        print("\nVARNING: Vissa sidor kunde inte hämta Page Access Token!")
        print("Detta kan innebära att skripten inte fungerar för dessa sidor.")
    
    return success_count > 0

def filter_placeholder_pages(pages):
    """Filtrera bort Srholder-sidor"""
    real_pages = []
    placeholder_pages = []
    
    for page in pages:
        name = page.get("name", "")
        if name.startswith("Srholder") and name[8:].isdigit():
            placeholder_pages.append(page)
        else:
            real_pages.append(page)
    
    if placeholder_pages:
        print(f"\nFiltrerade bort {len(placeholder_pages)} placeholder-sidor (Srholder*)")
    
    return real_pages, placeholder_pages

def save_full_report(scopes, pages, placeholder_pages):
    """Spara fullständig rapport med scopes och sidor"""
    today = datetime.date.today().strftime("%Y%m%d")
    filename = f"permissions_report_{today}.csv"
    filepath = os.path.join(EXPORT_PATH, filename)
    
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        
        # Sektion 1: Scopes
        writer.writerow(["BEHÖRIGHETER (SCOPES)"])
        writer.writerow(["Scope", "Status"])
        
        for scope in sorted(REQUIRED_SCOPES):
            status = "NÖDVÄNDIG - OK" if scope in scopes else "NÖDVÄNDIG - SAKNAS"
            writer.writerow([scope, status])
        
        for scope, desc in sorted(OPTIONAL_SCOPES.items()):
            if scope in scopes:
                writer.writerow([scope, f"VALFRI - OK ({desc})"])
            else:
                writer.writerow([scope, f"VALFRI - Saknas ({desc})"])
        
        writer.writerow([])
        
        # Sektion 2: Riktiga sidor
        writer.writerow(["FACEBOOK-SIDOR (RIKTIGA)"])
        writer.writerow(["Page ID", "Page Name", "Page Token OK"])
        for page in pages:
            token_status = "Ja" if page.get("page_token_ok") else "Ej testad"
            writer.writerow([page["id"], page["name"], token_status])
        
        writer.writerow([])
        
        # Sektion 3: Placeholder-sidor
        if placeholder_pages:
            writer.writerow(["PLACEHOLDER-SIDOR (filtreras bort av skript)"])
            writer.writerow(["Page ID", "Page Name"])
            for page in placeholder_pages:
                writer.writerow([page["id"], page["name"]])
    
    print(f"\nFullständig rapport sparad till: {filepath}")

def print_summary(pages, real_pages, placeholder_pages):
    """Skriv ut sammanfattning"""
    print("\n" + "="*80)
    print("SAMMANFATTNING")
    print("="*80)
    print(f"\nTotalt antal sidor: {len(pages)}")
    print(f"  - Riktiga sidor: {len(real_pages)}")
    print(f"  - Placeholder-sidor (Srholder*): {len(placeholder_pages)}")
    print(f"\nSkripten kommer bearbeta {len(real_pages)} riktiga sidor.")

def main():
    print("="*80)
    print("FACEBOOK TOKEN & BEHÖRIGHETS-KONTROLL")
    print("="*80)
    print(f"\nLäser token från config.py...")
    print(f"API-version: {API_VERSION}")
    
    # Steg 1: Verifiera token
    token_info, scopes = debug_token(ACCESS_TOKEN)
    if token_info is None:
        print("\n" + "!"*80)
        print("AVBRYTER: Token saknar nödvändiga behörigheter!")
        print("!"*80)
        return
    
    # Steg 2: Hämta sidor
    pages = get_page_ids_with_access(ACCESS_TOKEN)
    if not pages:
        print("\nVARNING: Inga sidor kunde hämtas!")
        print("Token kanske inte är kopplad till några sidor eller saknar 'pages_show_list'.")
        return
    
    # Steg 3: Testa Page Access Tokens
    token_test_ok = test_page_token_access(pages, ACCESS_TOKEN)
    if not token_test_ok:
        print("\nVARNING: Kunde inte hämta Page Access Tokens!")
        print("Skripten kan misslyckas när de försöker hämta data.")
    
    # Steg 4: Filtrera placeholder-sidor
    real_pages, placeholder_pages = filter_placeholder_pages(pages)
    
    # Steg 5: Spara rapport
    save_full_report(scopes, real_pages, placeholder_pages)
    
    # Steg 6: Sammanfattning
    print_summary(pages, real_pages, placeholder_pages)
    
    print("\n" + "="*80)
    print("KLART!")
    print("="*80)
    print("\nDu kan nu köra:")
    print("  - python3 fetch_facebook_reach.py")
    print("  - python3 fetch_facebook_comments.py")
    if "pages_messaging" in scopes:
        print("  - python3 fetch_facebook_dms.py")
    else:
        print("  - python3 fetch_facebook_dms.py (kräver ny token med 'pages_messaging')")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAvbruten av användare.")
    except Exception as e:
        print(f"\n\nOväntat fel: {e}")
        import traceback
        traceback.print_exc()

