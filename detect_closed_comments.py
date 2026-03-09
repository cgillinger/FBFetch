# detect_closed_comments.py
# Version 1.0 - Identifierar möjliga stängda kommentarsfält på Facebook-sidor
#
# Använder heuristik baserad på tre API-signaler:
#   1. can_comment - om aktören kan kommentera inlägget
#   2. comments.summary - antal kommentarer
#   3. /comments edge - om kanten returnerar data eller fel
#
# Klassificering:
#   open            - can_comment=true
#   restricted      - can_comment=false, kommentarer finns
#   probably_closed - can_comment=false, 0 kommentarer, /comments ger fel
#   uncertain       - can_comment=false, 0 kommentarer, /comments fungerar
#
# OBS: Resultatet är en probabilistisk uppskattning, inte definitiv sanning.

import csv
import json
import os
import time
import requests
import logging
import argparse
import sys
import urllib.parse
from datetime import datetime
from config import (
    ACCESS_TOKEN, TOKEN_LAST_UPDATED, API_VERSION,
    CACHE_FILE, MAX_RETRIES, RETRY_DELAY, TOKEN_VALID_DAYS
)

# ---------------------------------------------------------------------------
# Loggning
# ---------------------------------------------------------------------------

def setup_logging():
    now = datetime.now()
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_filename = os.path.join(
        log_dir,
        f"detect_closed_comments_{now.strftime('%Y-%m-%d_%H-%M-%S')}.log"
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler()
        ]
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Loggning till: {log_filename}")
    return logger


logger = setup_logging()

# ---------------------------------------------------------------------------
# Hjälpfunktion: maskera token i URL för säker loggning
# ---------------------------------------------------------------------------

def _mask_url(url):
    """Returnerar URL med access_token ersatt av [REDACTED] för säker loggning."""
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if "access_token" in params:
            params["access_token"] = ["[REDACTED]"]
        new_query = urllib.parse.urlencode(params, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        return "[URL ej visningsbar]"


def _unpack_next_url(next_url):
    """Extraherar access_token från en Facebook-pagineringslänk och returnerar
    (clean_url, params) där token ligger i params-dikt (ej i URL:en)."""
    parsed = urllib.parse.urlparse(next_url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    token_list = qs.pop("access_token", [])
    token = token_list[0] if token_list else None
    clean_query = urllib.parse.urlencode(qs, doseq=True)
    clean_url = urllib.parse.urlunparse(parsed._replace(query=clean_query))
    params = {"access_token": token} if token else {}
    return clean_url, params


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

api_call_count = 0
start_time = time.time()
rate_limit_backoff = 1.0
consecutive_successes = 0


def check_token_expiry():
    try:
        last_updated = datetime.strptime(TOKEN_LAST_UPDATED, "%Y-%m-%d")
        days_since = (datetime.now() - last_updated).days
        days_left = TOKEN_VALID_DAYS - days_since
        logger.info(f"Token skapades för {days_since} dagar sedan ({days_left} dagar kvar).")
        if days_left <= 0:
            logger.error("Token har gått ut. Skapa en ny token omedelbart.")
            return False
        if days_left <= 7:
            logger.warning(f"VARNING: Token går ut inom {days_left} dagar!")
        return True
    except Exception as e:
        logger.error(f"Kunde inte validera token-utgångsdatum: {e}")
        return False


def api_request(url, params, retry_count=0):
    """GET-anrop med felhantering och rate limiting. Returnerar (data, had_error).

    access_token skickas som Authorization-header (Bearer) om det finns i params,
    så att token aldrig exponeras i URL:er eller loggmeddelanden.
    """
    global api_call_count, start_time, rate_limit_backoff, consecutive_successes

    api_call_count += 1

    if api_call_count % 50 == 0:
        elapsed = time.time() - start_time
        rate = api_call_count / elapsed * 3600
        logger.info(f"API-hastighet: {rate:.0f} anrop/timme ({api_call_count} anrop)")

    # Flytta access_token från query-params till Authorization-header
    safe_params = dict(params)
    token = safe_params.pop("access_token", None)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        time.sleep(0.1 * rate_limit_backoff)
        response = requests.get(url, params=safe_params, headers=headers, timeout=30)

        if response.status_code == 200:
            data = response.json()
            consecutive_successes += 1
            if consecutive_successes > 10 and rate_limit_backoff > 1.0:
                rate_limit_backoff = max(1.0, rate_limit_backoff * 0.9)
            # Räkna Graph-felnoder som fel
            if "error" in data:
                return data, True
            return data, False

        elif response.status_code in (429, 17):
            consecutive_successes = 0
            rate_limit_backoff = min(5.0, rate_limit_backoff * 1.5)
            wait = RETRY_DELAY * rate_limit_backoff
            logger.warning(f"Rate limit. Väntar {wait:.1f}s...")
            time.sleep(wait)
            if retry_count < MAX_RETRIES:
                return api_request(url, params, retry_count + 1)
            logger.error(f"Max retry nått för {_mask_url(url)}")
            return None, True

        else:
            logger.debug(f"HTTP {response.status_code}: {response.text[:200]}")
            return None, True

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout för {_mask_url(url)}")
        if retry_count < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
            return api_request(url, params, retry_count + 1)
        return None, True

    except Exception as e:
        logger.error(f"API-fel: {e}")
        return None, True


# ---------------------------------------------------------------------------
# Sidhämtning
# ---------------------------------------------------------------------------

def get_all_pages():
    """Hämta alla sidor som token har åtkomst till."""
    logger.info("Hämtar lista över Facebook-sidor...")
    url = f"https://graph.facebook.com/{API_VERSION}/me/accounts"
    params = {"access_token": ACCESS_TOKEN, "limit": 100}

    pages = []
    while True:
        data, had_error = api_request(url, params)
        if had_error or not data or "data" not in data:
            logger.error("Kunde inte hämta sidor. Kontrollera token och behörigheter.")
            break

        pages.extend(data["data"])

        next_url = data.get("paging", {}).get("next")
        if next_url:
            url, params = _unpack_next_url(next_url)
        else:
            break

    page_list = [(p["id"], p["name"]) for p in pages]
    logger.info(f"Hittade {len(page_list)} sidor.")
    return page_list


def filter_placeholder_pages(page_list):
    """Filtrera bort placeholder-sidor (Srholder*)."""
    filtered, removed = [], []
    for page_id, page_name in page_list:
        if page_name and page_name.startswith("Srholder") and page_name[8:].isdigit():
            removed.append(page_name)
        else:
            filtered.append((page_id, page_name))
    if removed:
        logger.info(f"Filtrerade bort {len(removed)} placeholder-sidor: {', '.join(removed)}")
    logger.info(f"{len(filtered)} sidor kvar efter filtrering.")
    return filtered


def get_page_access_token(page_id):
    """Hämta Page Access Token för sidan."""
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}"
    params = {"fields": "access_token", "access_token": ACCESS_TOKEN}
    data, had_error = api_request(url, params)
    if had_error or not data or "access_token" not in data:
        msg = data.get("error", {}).get("message", "okänt fel") if data else "inget svar"
        logger.warning(f"Kunde inte hämta Page Access Token för {page_id}: {msg}")
        return None
    return data["access_token"]


def get_posts_for_page(page_id, page_token, limit=None):
    """Hämta inlägg från en sida. limit=None hämtar alla (paginerat)."""
    url = f"https://graph.facebook.com/{API_VERSION}/{page_id}/posts"
    params = {
        "access_token": page_token,
        "limit": 100,
        "fields": "id,created_time,message"
    }

    all_posts = []
    while True:
        data, had_error = api_request(url, params)
        if had_error or not data or "data" not in data:
            break

        all_posts.extend(data["data"])

        if limit and len(all_posts) >= limit:
            all_posts = all_posts[:limit]
            break

        next_url = data.get("paging", {}).get("next")
        if next_url:
            url, params = _unpack_next_url(next_url)
        else:
            break

    return all_posts


# ---------------------------------------------------------------------------
# Klassificering
# ---------------------------------------------------------------------------

def classify_post(post_id, page_token):
    """
    Kör heuristik för ett inlägg och returnerar en dict med signaler + klassificering.

    Beslutstabell:
      can_comment=True                           -> open
      can_comment=False, count>0                 -> restricted
      can_comment=False, count=0, edge-error     -> probably_closed
      can_comment=False, count=0, edge ok        -> uncertain
    """
    result = {
        "post_id": post_id,
        "can_comment": None,
        "comment_count": None,
        "comments_edge_error": None,
        "classification": "unknown"
    }

    # --- Signal 1 & 2: can_comment + comments.summary ---
    url1 = f"https://graph.facebook.com/{API_VERSION}/{post_id}"
    params1 = {
        "access_token": page_token,
        "fields": "id,can_comment,comments.summary(true){id}"
    }
    data1, err1 = api_request(url1, params1)

    if err1 or not data1:
        logger.debug(f"  Post {post_id}: Kunde inte hämta fält 1 (can_comment/summary)")
        result["classification"] = "unknown"
        return result

    result["can_comment"] = data1.get("can_comment")

    summary = data1.get("comments", {}).get("summary", {})
    result["comment_count"] = summary.get("total_count")

    # --- Signal 3: /comments edge ---
    url2 = f"https://graph.facebook.com/{API_VERSION}/{post_id}/comments"
    params2 = {"access_token": page_token, "limit": 1}
    data2, err2 = api_request(url2, params2)

    result["comments_edge_error"] = err2 or (data2 is None)

    # --- Klassificering ---
    can_comment = result["can_comment"]
    count = result["comment_count"]
    edge_error = result["comments_edge_error"]

    if can_comment is True:
        result["classification"] = "open"
    elif can_comment is False:
        if count is not None and count > 0:
            result["classification"] = "restricted"
        elif edge_error:
            result["classification"] = "probably_closed"
        else:
            result["classification"] = "uncertain"
    else:
        # can_comment saknas i svaret
        if edge_error:
            result["classification"] = "probably_closed"
        else:
            result["classification"] = "uncertain"

    return result


# ---------------------------------------------------------------------------
# Bearbetning per sida
# ---------------------------------------------------------------------------

def process_page(page_id, page_name, post_limit=None):
    """Bearbeta en sida och returnera lista med klassificerade inlägg."""
    logger.info(f"  Bearbetar sida: {page_name} ({page_id})")

    page_token = get_page_access_token(page_id)
    if not page_token:
        logger.warning(f"    Hoppar över sidan – ingen Page Access Token.")
        return []

    posts = get_posts_for_page(page_id, page_token, limit=post_limit)
    if not posts:
        logger.info(f"    Inga inlägg hittades.")
        return []

    logger.info(f"    {len(posts)} inlägg att klassificera...")

    rows = []
    for i, post in enumerate(posts, 1):
        post_id = post["id"]
        created = post.get("created_time", "")
        message_preview = (post.get("message") or "")[:60]

        r = classify_post(post_id, page_token)
        r["page_id"] = page_id
        r["page_name"] = page_name
        r["created_time"] = created
        r["message_preview"] = message_preview
        rows.append(r)

        logger.debug(
            f"    [{i}/{len(posts)}] {post_id} -> {r['classification']} "
            f"(can_comment={r['can_comment']}, count={r['comment_count']}, "
            f"edge_error={r['comments_edge_error']})"
        )

        if i % 20 == 0:
            logger.info(f"    Bearbetat {i}/{len(posts)} inlägg...")

    # Summering per sida
    summary = {}
    for r in rows:
        c = r["classification"]
        summary[c] = summary.get(c, 0) + 1
    logger.info(f"    Klart: {summary}")

    return rows


# ---------------------------------------------------------------------------
# CSV-utdata
# ---------------------------------------------------------------------------

def save_to_csv(rows, filename):
    if not rows:
        logger.warning("Inga rader att spara.")
        return

    fieldnames = [
        "page_id", "page_name", "post_id", "created_time",
        "can_comment", "comment_count", "comments_edge_error",
        "classification", "message_preview"
    ]

    try:
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        logger.info(f"Sparade {len(rows)} rader till {filename}")
    except Exception as e:
        logger.error(f"Kunde inte spara CSV: {e}")


def print_summary(rows):
    """Skriv ut en textsammanfattning till stdout."""
    if not rows:
        return

    totals = {}
    for r in rows:
        c = r["classification"]
        totals[c] = totals.get(c, 0) + 1

    print("\n" + "=" * 60)
    print("SAMMANFATTNING")
    print("=" * 60)
    print(f"Totalt analyserade inlägg: {len(rows)}")
    for cls in ["open", "restricted", "probably_closed", "uncertain", "unknown"]:
        n = totals.get(cls, 0)
        if n:
            print(f"  {cls:<20} {n:>5}")

    # Visa inlägg som troligen har stängda kommentarer
    closed = [r for r in rows if r["classification"] == "probably_closed"]
    if closed:
        print(f"\nInlägg klassificerade som 'probably_closed' ({len(closed)} st):")
        for r in closed[:20]:
            print(
                f"  Sida: {r['page_name']:<30} "
                f"Post: {r['post_id']}  "
                f"({r.get('created_time', '')[:10]})"
            )
        if len(closed) > 20:
            print(f"  ... och {len(closed) - 20} till (se CSV).")
    print("=" * 60)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Identifiera möjliga stängda kommentarsfält på Facebook-sidor"
    )
    parser.add_argument(
        "--page-id",
        help="Bearbeta endast denna specifika sida (Page ID)",
    )
    parser.add_argument(
        "--post-limit",
        type=int,
        default=None,
        help="Max antal inlägg per sida (standard: alla)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Filnamn för CSV-utdata (standard: closed_comments_YYYY-MM-DD.csv)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Aktivera debug-loggning",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    output_file = args.output or f"closed_comments_{datetime.now().strftime('%Y-%m-%d')}.csv"

    logger.info("=" * 60)
    logger.info("DETECT CLOSED COMMENTS - Facebook Graph API")
    logger.info("=" * 60)

    if not check_token_expiry():
        logger.error("Token-problem. Avbryter.")
        return 1

    # Hämta sidor
    all_pages = get_all_pages()
    if not all_pages:
        logger.error("Inga sidor hittades. Avbryter.")
        return 1

    pages = filter_placeholder_pages(all_pages)
    if not pages:
        logger.error("Inga giltiga sidor efter filtrering. Avbryter.")
        return 1

    # Begränsa till specifik sida om --page-id angivits
    if args.page_id:
        match = [(pid, pname) for pid, pname in pages if pid == args.page_id]
        if not match:
            logger.error(f"Page ID '{args.page_id}' hittades inte. Avbryter.")
            return 1
        pages = match
        logger.info(f"Bearbetar: {pages[0][1]} ({pages[0][0]})")
    else:
        logger.info(f"Bearbetar {len(pages)} sida(or).")

    all_rows = []
    for page_id, page_name in pages:
        rows = process_page(page_id, page_name, post_limit=args.post_limit)
        all_rows.extend(rows)

    save_to_csv(all_rows, output_file)
    print_summary(all_rows)

    logger.info("Klart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
