# check_token_permissions.py
#
# Ett diagnostikverktyg för att se vilka sidor och rättigheter din token ger åtkomst till.
# Kör detta innan du använder huvudskriptet för att verifiera att allt är korrekt inställt.

import requests
import json
import sys
from datetime import datetime
from config import ACCESS_TOKEN, TOKEN_LAST_UPDATED, TOKEN_VALID_DAYS, API_VERSION

# Konfigurera utskriftsformat för tydlighet
def print_header(text):
    print("\n" + "=" * 80)
    print(f" {text}")
    print("=" * 80)

def print_subheader(text):
    print("\n" + "-" * 60)
    print(f" {text}")
    print("-" * 60)

def print_info(label, value):
    print(f"{label:30}: {value}")

def print_error(text):
    print(f"\n❌ ERROR: {text}")

def api_request(url, params):
    """Gör ett API-anrop och returnerar JSON-svar"""
    try:
        response = requests.get(url, params=params, timeout=30)
        if response.status_code != 200:
            print_error(f"HTTP Error {response.status_code}: {response.text}")
            return None
        return response.json()
    except Exception as e:
        print_error(f"API-anrop misslyckades: {e}")
        return None

def check_token():
    """Kontrollera token-information"""
    print_header("TOKEN-INFORMATION")
    
    # Verifiera token med debug_token endpoint
    url = f"https://graph.facebook.com/{API_VERSION}/debug_token"
    params = {"input_token": ACCESS_TOKEN, "access_token": ACCESS_TOKEN}
    data = api_request(url, params)
    
    if not data or "data" not in data:
        print_error("Kunde inte hämta token-information.")
        return False
    
    token_data = data["data"]
    is_valid = token_data.get("is_valid", False)
    
    # Skriv ut grundläggande token-information
    print_info("Token giltig", "✅ JA" if is_valid else "❌ NEJ")
    
    if not is_valid:
        error_msg = token_data.get("error", {}).get("message", "Okänd anledning")
        print_error(f"Token är ogiltig: {error_msg}")
        return False
    
    # Beräkna utgångsdatum
    expires_at = token_data.get("expires_at")
    if expires_at:
        expiry_date = datetime.fromtimestamp(expires_at)
        print_info("Utgår", expiry_date.strftime("%Y-%m-%d %H:%M:%S"))
        
        days_left = (expiry_date - datetime.now()).days
        if days_left <= 0:
            print_error("Token har redan gått ut!")
        elif days_left <= 7:
            print_info("Dagar kvar", f"⚠️ {days_left} (skapa ny token snart!)")
        else:
            print_info("Dagar kvar", f"{days_left}")
    else:
        # Fallback om expires_at inte finns
        try:
            last_updated = datetime.strptime(TOKEN_LAST_UPDATED, "%Y-%m-%d")
            days_since = (datetime.now() - last_updated).days
            days_left = TOKEN_VALID_DAYS - days_since
            
            if days_left <= 0:
                print_info("Uppskattad giltighetstid", "❌ UTGÅNGEN")
            else:
                print_info("Uppskattad giltighetstid", f"ca {days_left} dagar kvar")
        except:
            print_info("Utgångsdatum", "Okänt")
    
    # Skriv ut ytterligare token-information
    print_info("App ID", token_data.get("app_id", "Okänd"))
    print_info("Användar-ID", token_data.get("user_id", "Okänt"))
    print_info("Token-typ", token_data.get("type", "Okänd"))
    
    # Skriv ut behörigheter för tokenen
    scopes = token_data.get("scopes", [])
    if scopes:
        print_subheader("TOKEN-BEHÖRIGHETER")
        for i, scope in enumerate(scopes, 1):
            print(f" {i:2}. {scope}")
        
        # Kontrollera om nödvändiga behörigheter finns
        required_perms = {"pages_show_list", "pages_read_engagement", "read_insights"}
        missing = required_perms - set(scopes)
        if missing:
            print_error(f"Saknade behörigheter: {', '.join(missing)}")
            print("     Detta kan hindra hämtning av räckviddsdata!")
        else:
            print("\n✅ Alla nödvändiga behörigheter finns!")
    
    return is_valid

def get_pages():
    """Hämta alla sidor som token har åtkomst till"""
    print_header("TILLGÄNGLIGA SIDOR")
    
    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {"access_token": ACCESS_TOKEN, "fields": "id,name,access_token,category,permissions"}
    
    all_pages = []
    next_url = url
    page_count = 0
    
    while next_url:
        data = api_request(url if next_url == url else next_url, {} if next_url != url else params)
        
        if not data or "data" not in data:
            break
            
        pages_batch = data["data"]
        all_pages.extend(pages_batch)
        page_count += len(pages_batch)
        
        # Visa löpande antal
        print(f"Hittade {page_count} sidor...", end="\r")
        
        # Hantera paginering
        next_url = data.get("paging", {}).get("next")
    
    print(f"Hittade totalt {page_count} sidor." + " "*20)
    
    if not all_pages:
        print_error("Inga sidor hittades! Token kanske saknar behörighet 'pages_show_list'.")
        return []
    
    return all_pages

def check_page_insights(page):
    """Kontrollera om vi kan komma åt insights för en sida"""
    page_id = page["id"]
    page_token = page.get("access_token")
    
    if not page_token:
        return False, "Ingen page access token tillgänglig"
    
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}/insights/page_impressions_unique"
    params = {
        "access_token": page_token,
        "period": "day",
        "limit": 1
    }
    
    data = api_request(url, params)
    
    if not data:
        return False, "API-anrop misslyckades"
        
    if "error" in data:
        error_code = data["error"].get("code")
        error_msg = data["error"].get("message", "Okänt fel")
        return False, f"Error {error_code}: {error_msg}"
    
    if "data" in data and data["data"]:
        return True, "OK"
    
    return False, "Inga data hittades"

def display_pages(pages):
    """Visa detaljerad information om alla sidor"""
    if not pages:
        return
    
    # Sortera sidor efter namn
    sorted_pages = sorted(pages, key=lambda x: x.get("name", "").lower())
    
    print_subheader(f"DETALJERAD SIDINFORMATION ({len(pages)} sidor)")
    
    for i, page in enumerate(sorted_pages, 1):
        name = page.get("name", "Okänt namn")
        page_id = page.get("id", "Okänt ID")
        category = page.get("category", "Okänd kategori")
        
        print(f"\n{i}. {name} (ID: {page_id})")
        print(f"   Kategori: {category}")
        
        # Kontrollera sidans behörigheter
        permissions = page.get("permissions", [])
        if permissions:
            print("   Behörigheter:")
            for perm in permissions:
                print(f"      - {perm}")
        else:
            print("   Behörigheter: Ingen information tillgänglig")
        
        # Kontrollera om vi kan hämta insikter
        can_get_insights, insights_msg = check_page_insights(page)
        if can_get_insights:
            print(f"   Insights: ✅ Tillgängligt")
        else:
            print(f"   Insights: ❌ Inte tillgängligt ({insights_msg})")

def summarize_pages(pages):
    """Visa sammanfattning av sidor och behörigheter"""
    if not pages:
        return
    
    print_header("SAMMANFATTNING")
    
    # Räkna antal sidor som har insights tillgängliga
    insights_available = 0
    insights_unavailable = 0
    
    for page in pages:
        can_get_insights, _ = check_page_insights(page)
        if can_get_insights:
            insights_available += 1
        else:
            insights_unavailable += 1
    
    # Visa sammanfattning
    print_info("Totalt antal sidor", len(pages))
    print_info("Sidor med tillgängliga insights", f"{insights_available} ({insights_available/len(pages)*100:.1f}%)")
    print_info("Sidor utan tillgängliga insights", f"{insights_unavailable} ({insights_unavailable/len(pages)*100:.1f}%)")
    
    # Visa varning om inte alla sidor har insights
    if insights_unavailable > 0:
        print("\n⚠️ VARNING: Inte alla sidor har insights tillgängliga!")
        print("   Detta kan bero på att vissa sidor inte har tillräckligt med aktivitet")
        print("   eller att de saknar särskilda behörigheter.")
    else:
        print("\n✅ Alla sidor har insights tillgängliga!")

def main():
    print_header("FACEBOOK TOKEN & SIDANALYS")
    print("Detta verktyg visar vilka sidor och rättigheter din token ger åtkomst till.")
    
    # Kontrollera token
    if not check_token():
        sys.exit(1)
    
    # Hämta sidor
    pages = get_pages()
    
    # Visa detaljerad information om sidor
    display_pages(pages)
    
    # Visa sammanfattning
    summarize_pages(pages)
    
    print("\nAnalys slutförd!")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAvbruten av användaren.")
        sys.exit(1)
    except Exception as e:
        print_error(f"Ett oväntat fel inträffade: {e}")
        sys.exit(1)
