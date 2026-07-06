#!/usr/bin/env python3
# permissions_check.py
# Verifierar att token har alla nödvändiga behörigheter för projektets skript.
#
# Standardkörning kontrollerar Facebook-behörigheter och sidåtkomst.
# Med --instagram kontrolleras även länkade Instagram-konton och insights-
# åtkomst (ersätter tidigare instagram-permission-checker.py).

import os
import csv
import datetime
import argparse
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
    "read_insights",             # Läsa insights (fetch_viewers.py)
    "pages_read_user_content",   # Läsa kommentarer (fetch_facebook_comments.py) — annars falsk trygghet
}

# Krävs därtill vid --instagram
INSTAGRAM_SCOPES = {
    "instagram_basic",           # Instagram-kontodata
    "instagram_manage_insights", # Instagram insights
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

def debug_token(token, extra_required=frozenset()):
    """Verifiera token och lista behörigheter"""
    print("\n" + "="*80)
    print("TOKEN-VERIFIERING")
    print("="*80)

    required_scopes = REQUIRED_SCOPES | set(extra_required)

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
    missing_required = required_scopes - scopes
    optional_scopes = {k: v for k, v in OPTIONAL_SCOPES.items() if k not in required_scopes}
    present_optional = {k: v for k, v in optional_scopes.items() if k in scopes}
    missing_optional = {k: v for k, v in optional_scopes.items() if k not in scopes}

    print("\nNÖDVÄNDIGA behörigheter:")
    for scope in sorted(required_scopes):
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
            print("  - fetch_viewers.py (viewers-data)")
        if "pages_read_user_content" in missing_required:
            print("  - fetch_facebook_comments.py (kommentarer)")
        if missing_required & INSTAGRAM_SCOPES:
            print("  - fetch_viewers.py --instagram, fetch_instagram_posts.py")
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

def get_instagram_accounts(token):
    """Hämta Facebook-sidor med länkade Instagram-konton"""
    print("\n" + "="*80)
    print("HÄMTAR INSTAGRAM-KONTON")
    print("="*80)

    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {
        "access_token": token,
        "fields": "id,name,instagram_business_account{id,name,username}",
        "limit": 100
    }

    accounts = []
    next_url = url
    page_count = 0

    while next_url:
        data = api_request(next_url if next_url != url else url, {} if next_url != url else params)
        batch = data.get("data", [])

        for page in batch:
            page_count += 1
            ig = page.get("instagram_business_account")
            if ig:
                accounts.append({
                    "page_id": page.get("id"),
                    "page_name": page.get("name"),
                    "instagram_id": ig.get("id"),
                    "instagram_username": ig.get("username", ""),
                    "instagram_name": ig.get("name", ""),
                    "insights_ok": None,
                    "insights_msg": ""
                })

        next_url = data.get("paging", {}).get("next")

        if next_url:
            print(f"Gått igenom {page_count} sidor så här långt...")

    print(f"\nHittade {len(accounts)} länkade Instagram-konton på {page_count} sidor")

    if not accounts:
        print("VARNING: Inga länkade Instagram-konton hittades!")
        print("Token kanske saknar 'instagram_basic', eller så är inga konton länkade.")

    return accounts

def test_instagram_insights(accounts, token):
    """Testa insights-åtkomst för varje Instagram-konto"""
    if not accounts:
        return

    print("\n" + "="*80)
    print("TESTAR INSTAGRAM INSIGHTS-ÅTKOMST")
    print("="*80)

    sorted_accounts = sorted(accounts, key=lambda a: a.get("instagram_username", "").lower())

    for i, account in enumerate(sorted_accounts, 1):
        url = f"https://graph.facebook.com/{API_VERSION}/{account['instagram_id']}/insights"
        params = {
            "access_token": token,
            "metric": "reach",
            "period": "day",
            "limit": 1
        }

        data = api_request(url, params)

        if data.get("data"):
            account["insights_ok"] = True
            account["insights_msg"] = "OK"
            symbol, status = "✓", "OK"
        else:
            error = data.get("error", {})
            account["insights_ok"] = False
            account["insights_msg"] = error.get("message", "Inga data")
            symbol, status = "✗", account["insights_msg"]

        username = f"@{account['instagram_username']}" if account["instagram_username"] else account["instagram_id"]
        print(f"  {symbol} {username[:40]:40} - {status}")

    ok_count = sum(1 for a in accounts if a["insights_ok"])
    print(f"\nResultat: {ok_count}/{len(accounts)} konton med tillgängliga insights")

    if ok_count < len(accounts):
        print("\nVARNING: Inte alla Instagram-konton har insights tillgängliga!")
        print("Detta kan bero på att kontot inte är ett affärskonto eller saknar aktivitet.")

def save_full_report(scopes, pages, placeholder_pages, instagram_accounts=None):
    """Spara fullständig rapport med scopes, sidor och ev. Instagram-konton"""
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
            writer.writerow([])

        # Sektion 4: Instagram-konton
        if instagram_accounts is not None:
            writer.writerow(["INSTAGRAM-KONTON"])
            writer.writerow(["Instagram ID", "Användarnamn", "Facebook-sida", "Insights OK"])
            for account in instagram_accounts:
                writer.writerow([
                    account["instagram_id"],
                    account["instagram_username"],
                    account["page_name"],
                    "Ja" if account["insights_ok"] else f"Nej ({account['insights_msg']})"
                ])

    print(f"\nFullständig rapport sparad till: {filepath}")

def print_summary(pages, real_pages, placeholder_pages, instagram_accounts=None):
    """Skriv ut sammanfattning"""
    print("\n" + "="*80)
    print("SAMMANFATTNING")
    print("="*80)
    print(f"\nTotalt antal sidor: {len(pages)}")
    print(f"  - Riktiga sidor: {len(real_pages)}")
    print(f"  - Placeholder-sidor (Srholder*): {len(placeholder_pages)}")
    print(f"\nSkripten kommer bearbeta {len(real_pages)} riktiga sidor.")

    if instagram_accounts is not None:
        insights_ok = sum(1 for a in instagram_accounts if a["insights_ok"])
        print(f"\nInstagram-konton: {len(instagram_accounts)}")
        print(f"  - Med tillgängliga insights: {insights_ok}")
        print(f"  - Utan tillgängliga insights: {len(instagram_accounts) - insights_ok}")

def main():
    parser = argparse.ArgumentParser(
        description="Verifierar att token har alla nödvändiga behörigheter för projektets skript."
    )
    parser.add_argument(
        "--instagram",
        action="store_true",
        help="Kontrollera även länkade Instagram-konton och insights-åtkomst"
    )
    args = parser.parse_args()

    print("="*80)
    title = "FACEBOOK & INSTAGRAM" if args.instagram else "FACEBOOK"
    print(f"{title} TOKEN & BEHÖRIGHETS-KONTROLL")
    print("="*80)
    print(f"\nLäser token från config.py...")
    print(f"API-version: {API_VERSION}")

    # Steg 1: Verifiera token
    extra_required = INSTAGRAM_SCOPES if args.instagram else frozenset()
    token_info, scopes = debug_token(ACCESS_TOKEN, extra_required)
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

    # Steg 5 (valfritt): Instagram-konton och insights
    instagram_accounts = None
    if args.instagram:
        instagram_accounts = get_instagram_accounts(ACCESS_TOKEN)
        test_instagram_insights(instagram_accounts, ACCESS_TOKEN)

    # Steg 6: Spara rapport
    save_full_report(scopes, real_pages, placeholder_pages, instagram_accounts)

    # Steg 7: Sammanfattning
    print_summary(pages, real_pages, placeholder_pages, instagram_accounts)

    print("\n" + "="*80)
    print("KLART!")
    print("="*80)
    print("\nDu kan nu köra:")
    print("  - python3 fetch_viewers.py --facebook --instagram --month")
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
