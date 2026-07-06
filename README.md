# FBFetch

> Automatiserad datainsamling från Facebook och Instagram via Meta Graph API — viewers, interaktioner, kommentarer, DM:s och sidstatus exporteras till CSV.

![Python](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)
![Meta Graph API](https://img.shields.io/badge/Meta%20Graph%20API-v25.0-1877F2?logo=facebook&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

---

> ⛔ **Viktigt (2026-06-15):** Metas gamla räckviddsmått (`page_impressions_unique` m.fl.) är **deprekerade i alla Graph API-versioner** — se [Metas Page Insights-dokumentation](https://developers.facebook.com/docs/graph-api/reference/page/insights/). De gamla reach-skripten är borttagna ur repot och finns i git-historiken under taggen [`pre-viewers-cleanup`](../../tree/pre-viewers-cleanup). Använd **`fetch_viewers.py`** (se [Användning](#användning)), som hämtar Metas nya Viewers/Media-Views-familj. Kräver Graph API **v25.0+**.

---

## Innehåll

- [Översikt](#översikt)
- [Skript](#skript)
- [Krav](#krav)
- [Installation](#installation)
- [Konfiguration](#konfiguration)
- [Access Token](#access-token)
- [Användning](#användning)
- [Utdataformat](#utdataformat)
- [Felsökning](#felsökning)
- [Schemaläggning](#schemaläggning)

---

## Översikt

FBFetch är en samling Python-skript för att hämta statistik från Facebook-sidor och Instagram-konton via Meta Graph API. All data sparas i CSV-format, redo att importeras i Excel, Google Sheets eller BI-verktyg.

**Fungerar med:**
- Flera Facebook-sidor i ett och samma konto
- Systemanvändartoken från Meta Business Manager (rekommenderas)
- Inkrementell körning — hoppar automatiskt över redan hämtad data

---

## Skript

| Skript | Beskrivning |
|--------|-------------|
| `fetch_viewers.py` | **⭐ Rekommenderas.** Konsoliderad Viewers/Media-Views för Facebook **+** Instagram, månad **och** vecka. Ersätter de deprekerade reach-skripten. |
| `fetch_facebook_comments.py` | Kommentarer på Facebook-inlägg — **räknare** (aggregerade antal per sida/månad), med `--filter`-grupper (p4lokalt/riks/…) |
| `fetch_facebook_dms.py` | Direktmeddelanden (DM) från Facebook-sidor |
| `fetch_instagram_posts.py` | Instagram-inlägg, räckvidd och engagemang |
| `fetch_page_status.py` | Integritetsstatus per sida via Page Integrity API — ögonblicksbild till CSV (ingen historik, stämplas med `run_date`) |
| `demographics.py` | Demografidata för sidor |
| `diagnostics.py` | Diagnostik och felsökningsverktyg |
| `permissions_check.py` | Verifierar token-behörigheter och sidåtkomst; `--instagram` testar även länkade Instagram-konton och insights-åtkomst |

> **Kommentarsexportören** (fullständig export av varje kommentar + svar) bor i ett eget repo: [cgillinger/fetch_comments](https://github.com/cgillinger/fetch_comments). Det är den versionen som körs schemalagt på servern — uppdateringar görs där, inte här.

### ⛔ Borttagna reach-skript (döda mått sedan 2026-06-15)

De gamla skripten finns i git-historiken under taggen [`pre-viewers-cleanup`](../../tree/pre-viewers-cleanup):

| Skript (borttaget) | Ersätts av |
|--------------------|-----------|
| `DEPRECATED_fetch_facebook_reach_weekly.py` | `fetch_viewers.py --facebook --week` |
| `DEPRECATED_fetch_facebook_reach.py` | `fetch_viewers.py --facebook --month` |
| `DEPRECATED_fetch_facebook_reach_no_click.py` | `fetch_viewers.py --facebook` |
| `DEPRECATED_fetch_instagram_reach.py` | `fetch_viewers.py --instagram --month` |

---

## Krav

- Python 3.8+
- `requests` (alla skript)
- `pandas` + `openpyxl` (endast `fetch_instagram_posts.py` och `demographics.py`)

```bash
pip install -r requirements.txt
```

---

## Installation

```bash
git clone https://github.com/cgillinger/FBFetch.git
cd FBFetch
pip install -r requirements.txt

# Skapa din konfigurationsfil
cp config.py.example config.py
```

> **OBS:** Lägg aldrig upp `config.py` i git — den innehåller din access token. Filen är redan undantagen via `.gitignore`.

---

## Konfiguration

Öppna `config.py` och fyll i dina värden:

```python
# Startdatum för datainsamling (YYYY-MM)
INITIAL_START_YEAR_MONTH = "2025-01"

# Din Meta access token
ACCESS_TOKEN = "EAAiY..."

# Datum då token skapades — används för att varna om utgång
TOKEN_LAST_UPDATED = "2025-05-12"
TOKEN_VALID_DAYS   = 60

# API-version — v25.0+ krävs för Viewers/Media-Views-måtten (fetch_viewers.py)
API_VERSION = "v25.0"
```

Token kan också anges via miljövariabel om du föredrar det:

```bash
export META_ACCESS_TOKEN="EAAiY..."
```

---

## Access Token

### Alternativ 1 — Graph API Explorer (snabbtest)

1. Gå till [Graph API Explorer](https://developers.facebook.com/tools/explorer/)
2. Välj din app och klicka **Generate Access Token**
3. Markera behörigheterna nedan och generera token

### Alternativ 2 — Systemanvändare i Business Manager (rekommenderas för produktion)

1. Gå till [Meta Business Manager](https://business.facebook.com/settings/) → **Systemanvändare**
2. Skapa eller välj en systemanvändare
3. Tilldela åtkomst till alla relevanta Facebook-sidor (behörighet: *Innehållshanteraren* eller högre)
4. Generera en token med nedanstående behörigheter och 60 dagars livslängd

**Nödvändiga behörigheter:**

```
pages_read_engagement
pages_show_list
read_insights
pages_read_user_content      # kommentarer (fetch_facebook_comments.py)
instagram_basic              # Instagram-skripten
instagram_manage_insights    # Instagram-skripten
```

> Tokens är giltiga i 60 dagar. Skriptet varnar automatiskt när utgångsdatum närmar sig.

---

## Användning

### Viewers — `fetch_viewers.py` (rekommenderas)

Konsoliderat skript för både Facebook och Instagram, månad och vecka. Hämtar Metas nya Viewers/Media-Views-mått till separata mappar per plattform och granularitet. Kräver Graph API **v25.0+** (sätt `API_VERSION` i `config.py` eller använd `--api-version`).

**Mått & perioder (faktiska, verifierade 2026-07):**

| Plattform | Granularitet | Mått | Period / metod |
|-----------|--------------|------|----------------|
| Facebook | månad | `page_total_media_view_unique` | `total_over_range` över kalendermånaden (unikt, dedupat) |
| Facebook | vecka | `page_total_media_view_unique` | `total_over_range` över mån–sön |
| Instagram | månad | `reach` (+ `views`) | `metric_type=total_value`, hårt 30-dagarsfönster |
| Instagram | vecka | `reach` (+ `views`) | `metric_type=total_value`, 7-dagarsfönster |

> ⚠️ **FB vecka använder `total_over_range`, INTE `period=week`.** Metas `period=week` returnerar ett *rullande 7-dagarsvärde per dag*; summering av datapunkterna blåser upp veckotalet ~6–9× (verifierat). `total_over_range` över mån–sön ger korrekt unikt veckotal. Invarianterna håller: **vecka ≤ månad ≤ summa av veckor**.

```bash
# Fas 0 — sondera vad som går att hämta (skriver bara till probe_results/)
python fetch_viewers.py --probe --facebook --instagram --sample 3

# Produktion — senast avslutade period
python fetch_viewers.py --facebook --month
python fetch_viewers.py --instagram --week
python fetch_viewers.py --facebook --instagram --month --week

# Specifik period
python fetch_viewers.py --facebook --month --year-month 2026-05
python fetch_viewers.py --instagram --week --iso-week 2026-W23
```

**Kommandoradsargument:**

| Argument | Beskrivning |
|----------|-------------|
| `--facebook` / `--instagram` | Plattform(ar) att hämta (minst en krävs) |
| `--month` / `--week` | Granularitet (minst en krävs; ej med `--probe`) |
| `--probe` | Fas 0-sondering; skriver endast `probe_results/`, kör aldrig produktion |
| `--sample N` | Antal sidor/konton i probe (default 3) |
| `--year-month YYYY-MM` | Målmånad (annars senast avslutade) |
| `--iso-week YYYY-Www` | Målvecka (annars senast avslutade) |
| `--api-version vXX.0` | Override av Graph API-version (default = `config.py`) |

**Utdata** hamnar i `Facebook/` respektive `Instagram/`:

```
Facebook/
├── month/2026/FB_2026_06.csv
└── week/2026_06/week_26.csv
Instagram/
├── month/2026/IG_2026_06.csv
└── week/2026_06/week_26.csv
```

CSV:erna innehåller `Period_start`/`Period_end` samt en `Views_Source`-kolumn (`mått@API-version`) — eftersom det nya viewers-måttet inte är identiskt med gammal reach markeras definitionsbytet där. Varje sidas rad skrivs och flushas direkt (krasch-säkert), aldrig batch-sparning i slutet.

> **Instagram:** hårt 30-dagarsfönster per månad (Metas gräns). IG-reach är enbart organisk.

---

## Utdataformat

Utdatastrukturen för `fetch_viewers.py` visas under [Användning](#användning). Varje CSV innehåller `Period_start`/`Period_end` samt `Views_Source` — läs alltid periodkolumnerna, aldrig filnamnsetiketten.

### Loggfiler

```
logs/
└── fetch_viewers_2026-07-06_08-30-00.log
```

---

## Felsökning

### Token ogiltig eller utgången
Skaffa en ny token enligt [instruktionerna ovan](#access-token) och uppdatera `config.py`.

### Inga sidor hittades
Kontrollera att token har behörigheterna `pages_show_list` och `pages_read_engagement`, och att systemanvändaren har tilldelats sidorna.

```bash
python permissions_check.py              # Facebook
python permissions_check.py --instagram  # + Instagram-konton och insights
```

### Rate limit
Skriptet hanterar `429`-svar automatiskt med exponentiell backoff och respekterar `Retry-After`-headern. Ingen manuell åtgärd krävs.

### Sidor visas trots att de borde filtreras
Placeholder-sidor med namnmönstret `Srholder*` (t.ex. `Srholder9a`, `SRholder8g`) filtreras automatiskt bort. Kontrollera att sidnamnet matchar mönstret `^[Ss][Rr]holder\w*$`.

### Diskrepans mot Facebook Insights
`fetch_viewers.py` hämtar veckovärden med `total_over_range` (unikt, dedupat över mån–sön) — **inte** `period=week`, som ger rullande 7-dagarsvärden och blåser upp summerade veckotal ~6–9×. Små avvikelser mot Metas UI (±1–2%) kan förekomma beroende på tidzonhantering.

---

## Schemaläggning

### Linux/macOS — cron

```bash
crontab -e
```

```cron
# Kör varje måndag kl 06:00 — hämtar föregående vecka automatiskt (FB + IG)
0 6 * * 1 cd /opt/fbfetch && python fetch_viewers.py --facebook --instagram --week >> logs/cron.log 2>&1

# Månadsvis den 2:a kl 07:00 — föregående månad
0 7 2 * * cd /opt/fbfetch && python fetch_viewers.py --facebook --instagram --month >> logs/cron.log 2>&1
```

### Docker

```bash
docker run --rm \
  -v "$(pwd):/app" -w /app \
  python:3.12-slim \
  sh -c "pip install requests && python fetch_viewers.py --facebook --instagram --week"
```

### Windows — Task Scheduler

1. Öppna **Aktivitetsschemaläggaren** → Skapa grundläggande uppgift
2. Utlösare: **Veckovis**, måndag
3. Åtgärd: `python C:\fbfetch\fetch_viewers.py --facebook --instagram --week`

---

## Bidra

Pull requests välkomnas. Öppna gärna ett issue först för större ändringar.

---

## Om projektet

> Det här är ett personligt hobbyprojekt som jag byggt för eget bruk och lagt upp ifall det är till nytta för någon annan. Jag jobbar på det på fritiden, så issues och PR:ar är välkomna men svar kan dröja. Använd på egen risk.

---

*Byggt för intern statistikinsamling. Kräver ett giltigt Meta-konto med administratörsbehörighet till de sidor du vill hämta data för.*
