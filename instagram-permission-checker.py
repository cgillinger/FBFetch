# check_instagram_permissions.py
#
# Ett diagnostikverktyg för att se vilka Instagram-konton och rättigheter din token ger åtkomst till.
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
        
        # Kontrollera om nödvändiga behörigheter för Instagram finns
        required_perms = {"instagram_basic", "instagram_manage_insights", "pages_show_list"}
        missing = required_perms - set(scopes)
        if missing:
            print_error(f"Saknade behörigheter för Instagram: {', '.join(missing)}")
            print("     Detta kan hindra hämtning av Instagram-data!")
        else:
            print("\n✅ Alla nödvändiga behörigheter för Instagram finns!")
    
    return is_valid

def get_facebook_pages_with_instagram():
    """Hämta Facebook-sidor med länkade Instagram-konton"""
    print_header("FACEBOOK-SIDOR MED INSTAGRAM-KONTON")
    
    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {
        "access_token": ACCESS_TOKEN, 
        "fields": "id,name,instagram_business_account{id,name,username,profile_picture_url}"
    }
    
    all_pages = []
    instagram_accounts = []
    next_url = url
    page_count = 0
    
    while next_url:
        data = api_request(url if next_url == url else next_url, {} if next_url != url else params)
        
        if not data or "data" not in data:
            break
            
        pages_batch = data["data"]
        
        for page in pages_batch:
            all_pages.append(page)
            if "instagram_business_account" in page:
                instagram_accounts.append({
                    "page_id": page["id"],
                    "page_name": page["name"],
                    "instagram_id": page["instagram_business_account"]["id"],
                    "instagram_username": page["instagram_business_account"].get("username", ""),
                    "instagram_name": page["instagram_business_account"].get("name", "")
                })
        
        page_count += len(pages_batch)
        
        # Visa löpande antal
        print(f"Hittade {page_count} Facebook-sidor...", end="\r")
        
        # Hantera paginering
        next_url = data.get("paging", {}).get("next")
    
    print(f"Hittade totalt {page_count} Facebook-sidor och {len(instagram_accounts)} länkade Instagram-konton." + " "*20)
    
    if not all_pages:
        print_error("Inga Facebook-sidor hittades! Token kanske saknar behörighet 'pages_show_list'.")
        return []
        
    if not instagram_accounts:
        print_error("Inga länkade Instagram-konton hittades! Token kanske saknar 'instagram_basic'-behörighet.")
    
    return instagram_accounts

def check_instagram_insights(instagram_id):
    """Kontrollera om vi kan komma åt insights för ett Instagram-konto"""
    url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}/insights"
    params = {
        "access_token": ACCESS_TOKEN,
        "metric": "reach",
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

def display_instagram_accounts(accounts):
    """Visa detaljerad information om alla Instagram-konton"""
    if not accounts:
        return
    
    # Sortera konton efter användarnamn
    sorted_accounts = sorted(accounts, key=lambda x: x.get("instagram_username", "").lower())
    
    print_subheader(f"DETALJERAD INSTAGRAM-INFORMATION ({len(accounts)} konton)")
    
    for i, account in enumerate(sorted_accounts, 1):
        instagram_username = account.get("instagram_username", "Okänt användarnamn")
        instagram_id = account.get("instagram_id", "Okänt ID")
        page_name = account.get("page_name", "Okänd Facebook-sida")
        
        print(f"\n{i}. @{instagram_username} (ID: {instagram_id})")
        print(f"   Länkad till Facebook-sida: {page_name}")
        
        # Kontrollera om vi kan hämta insikter
        can_get_insights, insights_msg = check_instagram_insights(instagram_id)
        if can_get_insights:
            print(f"   Insights: ✅ Tillgängligt")
        else:
            print(f"   Insights: ❌ Inte tillgängligt ({insights_msg})")
        
        # Försök hämta några specifika metriker för att testa
        if can_get_insights:
            try:
                # Testa hämta räckvidd för att se om det fungerar
                url = f"https://graph.facebook.com/{API_VERSION}/{instagram_id}/insights"
                params = {
                    "access_token": ACCESS_TOKEN,
                    "metric": "reach,profile_views,profile_activity",
                    "period": "day",
                    "limit": 1
                }
                
                data = api_request(url, params)
                if data and "data" in data:
                    print("   Tillgängliga metriker:")
                    for metric in data["data"]:
                        print(f"      - {metric.get('name')}: ✅")
            except Exception as e:
                print(f"   Fel vid test av metriker: {e}")

def summarize_instagram_accounts(accounts):
    """Visa sammanfattning av Instagram-konton och behörigheter"""
    if not accounts:
        return
    
    print_header("SAMMANFATTNING")
    
    # Räkna antal konton som har insights tillgängliga
    insights_available = 0
    insights_unavailable = 0
    
    for account in accounts:
        instagram_id = account.get("instagram_id")
        can_get_insights, _ = check_instagram_insights(instagram_id)
        if can_get_insights:
            insights_available += 1
        else:
            insights_unavailable += 1
    
    # Visa sammanfattning
    print_info("Totalt antal Instagram-konton", len(accounts))
    print_info("Konton med tillgängliga insights", f"{insights_available} ({insights_available/len(accounts)*100:.1f}%)")
    print_info("Konton utan tillgängliga insights", f"{insights_unavailable} ({insights_unavailable/len(accounts)*100:.1f}%)")
    
    # Visa varning om inte alla konton har insights
    if insights_unavailable > 0:
        print("\n⚠️ VARNING: Inte alla Instagram-konton har insights tillgängliga!")
        print("   Detta kan bero på att vissa konton inte har tillräckligt med aktivitet,")
        print("   inte är affärskonton, eller att de saknar särskilda behörigheter.")
    else:
        print("\n✅ Alla Instagram-konton har insights tillgängliga!")

def main():
    print_header("INSTAGRAM TOKEN & KONTOANALYS")
    print("Detta verktyg visar vilka Instagram-konton och rättigheter din token ger åtkomst till.")
    
    # Kontrollera token
    if not check_token():
        sys.exit(1)
    
    # Hämta Instagram-konton via Facebook-sidor
    instagram_accounts = get_facebook_pages_with_instagram()
    
    # Visa detaljerad information om Instagram-konton
    display_instagram_accounts(instagram_accounts)
    
    # Visa sammanfattning
    summarize_instagram_accounts(instagram_accounts)
    
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
