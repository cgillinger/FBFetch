# FBFetch

> Automatiserad datainsamling från Facebook och Instagram via Meta Graph API — räckvidd, interaktioner, kommentarer och DM:s exporteras till CSV.

![Python](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)
![Meta Graph API](https://img.shields.io/badge/Meta%20Graph%20API-v20.0-1877F2?logo=facebook&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

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
| `fetch_facebook_reach_weekly.py` | **Veckovis** räckvidd och interaktioner per Facebook-sida |
| `fetch_facebook_reach.py` | **Månadsvis** räckvidd och interaktioner per Facebook-sida |
| `fetch_facebook_reach_no_click.py` | Räckvidd utan klick-mätvärden |
| `fetch_facebook_comments.py` | Kommentarer på Facebook-inlägg |
| `fetch_facebook_dms.py` | Direktmeddelanden (DM) från Facebook-sidor |
| `fetch_instagram_posts_ver4_6.py` | Instagram-inlägg, räckvidd och engagemang |
| `demographics.py` | Demografidata för sidor |
| `diagnostics.py` | Diagnostik och felsökningsverktyg |
| `check_token_permissions.py` | Kontrollera token-behörigheter och sidåtkomst |
| `permissions_check.py` | Verifierar API-behörigheter |
| `instagram-permission-checker.py` | Verifierar Instagram-behörigheter |

---

## Krav

- Python 3.8+
- `requests`

```bash
pip install requests
```

---

## Installation

```bash
git clone https://github.com/cgillinger/FBFetch.git
cd FBFetch
pip install requests

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

# API-version (uppdatera vid behov)
API_VERSION = "v20.0"
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
```

> Tokens är giltiga i 60 dagar. Skriptet varnar automatiskt när utgångsdatum närmar sig.

---

## Användning

### Veckovis räckvidd (`fetch_facebook_reach_weekly.py`)

Huvudskriptet för veckorapporter. Hämtar `page_impressions_unique` (räckvidd) och `page_post_engagements` (interaktioner) per Facebook-sida.

```bash
# Alla veckor från startdatum i config.py (hoppar över redan hämtade)
python fetch_facebook_reach_weekly.py

# Specifik månad
python fetch_facebook_reach_weekly.py --month 2025-05

# Specifik vecka (ISO-format)
python fetch_facebook_reach_weekly.py --week 2025-W19

# Anpassa startdatum utan att ändra config.py
python fetch_facebook_reach_weekly.py --start 2025-03

# Begränsa till ett urval sidor via JSON-fil
python fetch_facebook_reach_weekly.py --pages-json mina_sidor.json
```

**Kommandoradsargument:**

| Argument | Beskrivning |
|----------|-------------|
| `--month YYYY-MM` | Kör endast för angiven månad |
| `--week YYYY-Www` | Kör endast för angiven ISO-vecka |
| `--start YYYY-MM` | Overrida startdatum från config.py |
| `--pages-json FIL` | JSON-fil med sidurval `[{"id": "...", "name": "..."}]` |
| `--no-combine` | Hoppa över skapandet av `combined.csv` |

---

### Månadsvis räckvidd (`fetch_facebook_reach.py`)

```bash
# Alla månader från startdatum
python fetch_facebook_reach.py

# Specifik månad
python fetch_facebook_reach.py --month 2025-04

# Uppdatera alla sidor i befintliga rapporter
python fetch_facebook_reach.py --update-all

# Kontrollera och komplettera nya sidor i alla befintliga månader
python fetch_facebook_reach.py --check-new
```

---

## Utdataformat

### Veckorapporter

Sparas under `weekly_reports/YYYY_MM/`:

```
weekly_reports/
└── 2025_05/
    ├── week_19.csv
    ├── week_20.csv
    ├── week_21.csv
    └── combined.csv       ← Alla veckor i månaden sammanfogade
```

**Kolumner i vecko-CSV:**

| Kolumn | Beskrivning |
|--------|-------------|
| `page_id` | Facebook-sidans ID |
| `page_name` | Sidans namn |
| `year` | År |
| `week` | ISO-veckonummer |
| `start_date` | Veckans startdatum (måndag) |
| `end_date` | Veckans slutdatum (söndag) |
| `reach` | Unik räckvidd (`page_impressions_unique`) |
| `engagements` | Interaktioner (`page_post_engagements`) |
| `status` | `OK` / `NO_ACTIVITY` / `NO_DATA` / `ERROR` |
| `comment` | Eventuell felkommentar |

### Loggfiler

```
logs/
└── facebook_reach_weekly_2025-05-19_08-30-00.log
```

---

## Felsökning

### Token ogiltig eller utgången
Skaffa en ny token enligt [instruktionerna ovan](#access-token) och uppdatera `config.py`.

### Inga sidor hittades
Kontrollera att token har behörigheterna `pages_show_list` och `pages_read_engagement`, och att systemanvändaren har tilldelats sidorna.

```bash
python check_token_permissions.py
```

### Rate limit
Skriptet hanterar `429`-svar automatiskt med exponentiell backoff och respekterar `Retry-After`-headern. Ingen manuell åtgärd krävs.

### Sidor visas trots att de borde filtreras
Placeholder-sidor med namnmönstret `Srholder*` (t.ex. `Srholder9a`, `SRholder8g`) filtreras automatiskt bort. Kontrollera att sidnamnet matchar mönstret `^[Ss][Rr]holder\w*$`.

### Diskrepans mot Facebook Insights
Skriptet använder `period=week` med `page_impressions_unique`, samma metrik som Facebook Insights visar för veckovärden. Små avvikelser (±1–2%) kan förekomma beroende på tidzonhantering.

---

## Schemaläggning

### Linux/macOS — cron

```bash
crontab -e
```

```cron
# Kör varje måndag kl 06:00 — hämtar föregående vecka automatiskt
0 6 * * 1 cd /opt/fbfetch && python fetch_facebook_reach_weekly.py >> logs/cron.log 2>&1
```

### Docker

```bash
docker run --rm \
  -e META_ACCESS_TOKEN="EAAiY..." \
  -v "$(pwd)/weekly_reports:/app/weekly_reports" \
  -v "$(pwd)/logs:/app/logs" \
  python:3.11-slim \
  python /app/fetch_facebook_reach_weekly.py
```

### Windows — Task Scheduler

1. Öppna **Aktivitetsschemaläggaren** → Skapa grundläggande uppgift
2. Utlösare: **Veckovis**, måndag
3. Åtgärd: `python C:\fbfetch\fetch_facebook_reach_weekly.py`

---

## Bidra

Pull requests välkomnas. Öppna gärna ett issue först för större ändringar.

---

*Byggt för intern statistikinsamling. Kräver ett giltigt Meta-konto med administratörsbehörighet till de sidor du vill hämta data för.*
