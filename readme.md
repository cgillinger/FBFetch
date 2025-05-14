# Facebook Räckvidd & Interaktioner - Readme

## Innehållsförteckning
1. [Översikt](#översikt)
2. [Installation](#installation)
3. [Konfigurera skriptet](#konfigurera-skriptet)
4. [Skaffa Facebook Access Token](#skaffa-facebook-access-token)
5. [Köra skriptet](#köra-skriptet)
6. [Interaktionsdata och nya mätvärden](#interaktionsdata-och-nya-mätvärden)
7. [Hantering av gamla CSV-filer](#hantering-av-gamla-csv-filer)
8. [Kommandoradsargument](#kommandoradsargument)
9. [Felsökning](#felsökning)
10. [Schemaläggning](#schemaläggning)

## Översikt

Detta skript automatiserar insamling av räckviddstatistik och interaktionsdata från dina Facebook-sidor. Det hämtar korrekt "total_over_range" räckvidd och interaktionsmått för varje månad och sparar datan i CSV-filer.

Huvudfunktioner:
- Hämtar korrekt räckvidd som matchar Facebook Insights
- Samlar in engagemang, reaktioner och klick-data
- Automatiskt kompletterar nya sidor i befintliga rapporter
- Stödjer inkrementell uppdatering med minimal API-användning
- Hanterar smidigt övergången från tidigare versioner

## Installation

1. Installera Python 3.6 eller senare
2. Installera nödvändiga paket:
   ```
   pip install requests
   ```
3. Ladda ner följande filer till samma mapp:
   - `fetch_facebook_reach.py` (huvudskriptet)
   - `config.py` (konfigurationsfil)

## Konfigurera skriptet

Öppna `config.py` och uppdatera följande inställningar:

```python
# 1. TIDSPERIOD - Startdatum för datainsamling
INITIAL_START_YEAR_MONTH = "2025-01"  # Ändra detta till önskat startdatum (YYYY-MM)

# 2. TOKEN-INFORMATION - Uppdatera varje gång du skapar ny token
ACCESS_TOKEN = "EAAiY..."  # Din Facebook access token
TOKEN_LAST_UPDATED = "2025-05-12"  # Dagens datum (YYYY-MM-DD) när du förnyar token
TOKEN_VALID_DAYS = 60  # Facebooks tokens är vanligtvis giltiga i 60 dagar

# 3. API OCH PRESTANDA-INSTÄLLNINGAR - Ändra bara vid behov
API_VERSION = "v19.0"  # Facebook Graph API-version
CACHE_FILE = "page_names.json"  # Cache för sidnamn
BATCH_SIZE = 10  # Antal sidor att bearbeta samtidigt
MAX_RETRIES = 3  # Antal försök vid API-fel
RETRY_DELAY = 5  # Sekunder att vänta mellan försök
MAX_REQUESTS_PER_HOUR = 200  # Ungefärlig gräns från Facebook
MONTH_PAUSE_SECONDS = 60  # Sekunder att vänta mellan månader
```

## Skaffa Facebook Access Token

### Metod 1: via Graph API Explorer (för utvecklare)

1. Gå till [Facebook Developers Graph API Explorer](https://developers.facebook.com/tools/explorer/)
2. Välj din app i rullgardinsmenyn (eller skapa en ny)
3. Klicka på "Generate Access Token"
4. Se till att markera följande behörigheter:
   - `pages_read_engagement`
   - `pages_show_list`
   - `read_insights`
5. Klicka på "Generate Token" och godkänn behörigheter
6. Kopiera den genererade token till `ACCESS_TOKEN` i config.py
7. Uppdatera `TOKEN_LAST_UPDATED` med dagens datum

### Metod 2: via Systemanvändare i Business Manager (rekommenderas)

För att använda en systemanvändartoken från Facebook Business Manager:

1. Gå till [Meta Business Manager](https://business.facebook.com/settings/)
2. Klicka på "Användare" i vänstermenyn och välj "Systemanvändare"
3. Använd en befintlig systemanvändare eller skapa en ny
4. Tilldela användaren behörighet till dina Facebook-sidor:
   - Klicka på systemanvändaren
   - Välj "Tilldela resurser"
   - Välj "Sidor" och markera alla sidor du vill hämta data för
   - Ge minst "Innehållshanteraren"-behörighet
5. Generera en access token:
   - Gå till systemanvändarens information
   - Klicka på "Generera ny token"
   - Välj den integrerade applikationen (t.ex. Business Manager)
   - Markera följande behörigheter:
     - `pages_read_engagement`
     - `pages_show_list`
     - `read_insights`
   - Ange en passande token-livslängd (60 dagar rekommenderas)
   - Klicka på "Generera token"
6. Kopiera den genererade token till `ACCESS_TOKEN` i config.py
7. Uppdatera `TOKEN_LAST_UPDATED` med dagens datum (YYYY-MM-DD)

**OBS!** Tokens är vanligtvis giltiga i 60 dagar. Skriptet varnar när din token närmar sig utgångsdatum.

## Köra skriptet

### Grundläggande användning

För att köra skriptet och bearbeta alla månader från konfigurerat startdatum:

```
python fetch_facebook_reach.py
```

Skriptet kommer automatiskt att:
1. Identifiera vilka månader som saknar rapporter
2. Hämta alla tillgängliga Facebook-sidor du har åtkomst till
3. Samla in räckvidd och interaktionsdata för varje sida
4. Skapa CSV-filer med namnet `FB_YYYY_MM.csv` för varje månad

### För att köra en specifik månad

```
python fetch_facebook_reach.py --month 2025-04
```

### För att visa utförlig loggning

```
python fetch_facebook_reach.py --debug
```

## Interaktionsdata och nya mätvärden

Skriptet samlar nu in följande mätvärden för varje sida:

1. **Reach** - Antal unika användare som sett innehåll från sidan (page_impressions_unique)
2. **Engaged Users** - Antal unika användare som interagerat med sidan (page_engaged_users)
3. **Engagements** - Totalt antal interaktioner med sidans inlägg (page_post_engagements)
4. **Reactions** - Antal reaktioner på inlägg (page_actions_post_reactions_total)
5. **Clicks** - Antal klick på innehåll (page_consumptions)

Dessa mätvärden sparas som extra kolumner i CSV-filerna och ger en mer komplett bild av sidans prestanda.

## Hantering av gamla CSV-filer

Om du har CSV-filer från tidigare version av skriptet (utan interaktionsdata) kommer det nya skriptet att:

1. **Identifiera befintliga sidor** - Skriptet läser in och respekterar befintliga räckviddsvärden
2. **Endast komplettera med nya sidor** - Befintliga sidor i CSV-filen hoppas över för att spara API-anrop
3. **Lägga till nya kolumner** - Nya interaktionskolumner läggs till i CSV-filen
4. **Behålla bakåtkompatibilitet** - Äldre sidor kommer ha nollvärden för interaktionskolumner

### För att uppdatera alla sidor inklusive befintliga

```
python fetch_facebook_reach.py --update-all
```

### För att kontrollera nya sidor i alla befintliga rapporter

```
python fetch_facebook_reach.py --check-new
```

## Kommandoradsargument

Skriptet stödjer följande kommandoradsargument:

- `--start YYYY-MM` - Ange ett eget startdatum (överrider INITIAL_START_YEAR_MONTH)
- `--month YYYY-MM` - Kör endast för en specifik månad
- `--update-all` - Uppdaterar alla sidor även om de redan finns i CSV-filer
- `--check-new` - Kontrollerar efter nya sidor i alla befintliga månadsrapporter
- `--debug` - Aktiverar utförlig loggning för felsökning

## Felsökning

### "Token kunde inte valideras"
- **Problem**: Din Facebook-token är ogiltig eller har gått ut
- **Lösning**: Följ instruktionerna för att [skaffa en ny token](#skaffa-facebook-access-token) via antingen Graph API Explorer eller Business Manager

### "Inga sidor hittades"
- **Problem**: Din token har inte rätt behörigheter eller saknar åtkomst till sidorna
- **Lösning**: 
  - För Graph API tokens: Kontrollera att du valt rätt behörigheter och är admin på minst en Facebook-sida
  - För systemanvändare: Kontrollera att användaren har tilldelats alla Facebook-sidor med behörighet "Innehållshanteraren" eller högre

### "Rate limit nått"
- **Problem**: Du har gjort för många API-anrop på kort tid
- **Lösning**: Skriptet kommer automatiskt vänta och försöka igen. Du kan öka RETRY_DELAY i config.py

### Diskrepans mellan API-värden och Facebook Insights
- **Problem**: Värdena från API:et matchar inte exakt det du ser i gränssnittet
- **Lösning**: Skriptet använder nu `total_over_range` vilket ger mycket bättre överensstämmelse. Små skillnader kan fortfarande förekomma på grund av Facebooks olika beräkningsmetoder.

## Schemaläggning

För att automatiskt köra skriptet varje månad:

### Windows:
1. Öppna Task Scheduler (Aktivitetsschemaläggaren)
2. Skapa en ny uppgift
3. Ställ in att den ska köra `python` med sökvägen till skriptet
4. Schemalägg den att köra t.ex. den 1:a i varje månad

### Mac/Linux:
1. Öppna Terminal
2. Kör `crontab -e`
3. Lägg till en rad som: `0 0 1 * * cd /sökväg/till/mappen && python fetch_facebook_reach.py`