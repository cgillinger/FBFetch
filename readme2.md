# Facebook Räckvidd & Interaktioner - Readme v2.0

## Innehållsförteckning
1. [Översikt](#översikt)
2. [Installation](#installation)
3. [Konfigurera skriptet](#konfigurera-skriptet)
4. [Skaffa Facebook Access Token](#skaffa-facebook-access-token)
5. [Köra skriptet](#köra-skriptet)
6. [Alla kommandoradsargument](#alla-kommandoradsargument)
7. [Interaktionsdata och nya mätvärden](#interaktionsdata-och-nya-mätvärden)
8. [Hantering av gamla CSV-filer](#hantering-av-gamla-csv-filer)
9. [Filnamnskonventioner](#filnamnskonventioner)
10. [Felsökning](#felsökning)
11. [Schemaläggning](#schemaläggning)

## Översikt

Detta skript automatiserar insamling av räckviddstatistik och interaktionsdata från dina Facebook-sidor. Det hämtar korrekt "total_over_range" räckvidd och interaktionsmått för både kompletta månader och anpassade tidsperioder.

### Nya funktioner v2.0:
- **🎯 Partiella månader**: Hämta data för delar av månader (t.ex. "maj hittills")
- **📅 Custom datumintervall**: Specificera exakta start- och slutdatum
- **⚡ Snabbkommandon**: Fördefinierade perioder som senaste veckan/månaden
- **🔧 Utökade kommandoradsargument**: Fler sätt att köra skriptet

Huvudfunktioner:
- Hämtar korrekt räckvidd som matchar Facebook Insights
- Samlar in engagemang, reaktioner och klick-data
- Automatiskt kompletterar nya sidor i befintliga rapporter
- Stödjer inkrementell uppdatering med minimal API-användning
- Hanterar smidigt övergången från tidigare versioner
- **Nytt**: Stöd för anpassade tidsperioder

## Installation

1. Installera Python 3.6 eller senare
2. Installera nödvändiga paket:
   ```
   pip install requests pandas
   ```
3. Ladda ner följande filer till samma mapp:
   - `fetch_facebook_reach.py` (huvudskriptet)
   - `config.py` (konfigurationsfil)
   - `check_token_permissions.py` (valfritt diagnostikverktyg)

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

```bash
python fetch_facebook_reach.py
```

Skriptet kommer automatiskt att:
1. Identifiera vilka månader som saknar rapporter
2. Hämta alla tillgängliga Facebook-sidor du har åtkomst till
3. Samla in räckvidd och interaktionsdata för varje sida
4. Skapa CSV-filer med namnet `FB_YYYY_MM.csv` för varje månad

### Partiella månader och anpassade perioder

#### Nuvarande månad hittills
```bash
# Hämtar data från 1:a till idag (t.ex. 1-19 maj om det är 19 maj)
python fetch_facebook_reach.py --current-month-so-far
```

#### Anpassat datumintervall
```bash
# Specificera exakta start- och slutdatum
python fetch_facebook_reach.py --from 2025-05-01 --to 2025-05-19
```

#### Senaste N dagar
```bash
# Senaste 14 dagar (inklusive idag)
python fetch_facebook_reach.py --last-n-days 14
```

#### Fördefinierade perioder
```bash
# Senaste veckan (7 dagar)
python fetch_facebook_reach.py --last-week

# Senaste månaden (30 dagar)
python fetch_facebook_reach.py --last-month
```

## Alla kommandoradsargument

### Datumargument för månader

| Argument | Beskrivning | Exempel |
|----------|-------------|---------|
| `--start YYYY-MM` | Ange ett eget startdatum (överrider INITIAL_START_YEAR_MONTH) | `--start 2024-01` |
| `--month YYYY-MM` | Kör endast för en specifik månad | `--month 2025-04` |

### Custom datumintervall

| Argument | Beskrivning | Exempel |
|----------|-------------|---------|
| `--from YYYY-MM-DD` | Custom startdatum (måste användas med --to) | `--from 2025-05-01` |
| `--to YYYY-MM-DD` | Custom slutdatum (måste användas med --from) | `--to 2025-05-19` |
| `--current-month-so-far` | Från 1:a i månaden till idag | |
| `--last-n-days N` | Senaste N dagar (inklusive idag) | `--last-n-days 14` |
| `--last-week` | Senaste 7 dagar (inklusive idag) | |
| `--last-month` | Senaste 30 dagar (inklusive idag) | |

### Operationsmodifikatorer

| Argument | Beskrivning | Exempel |
|----------|-------------|---------|
| `--update-all` | Uppdaterar alla sidor även om de redan finns i CSV-filen | |
| `--check-new` | Kontrollerar efter nya sidor i alla befintliga månader | |
| `--status YYYY-MM` | Generera endast statusrapport för angiven månad | `--status 2025-04` |
| `--debug` | Aktivera utförlig loggning för felsökning | |

### Exempel på kompletta kommandon

```bash
# Grundläggande körning
python fetch_facebook_reach.py

# Specifik månad
python fetch_facebook_reach.py --month 2025-04

# Nuvarande månad hittills
python fetch_facebook_reach.py --current-month-so-far

# Anpassat datumintervall
python fetch_facebook_reach.py --from 2025-05-01 --to 2025-05-19

# Senaste 2 veckor
python fetch_facebook_reach.py --last-n-days 14

# Uppdatera alla sidor för april
python fetch_facebook_reach.py --month 2025-04 --update-all

# Kontrollera nya sidor i alla befintliga månader
python fetch_facebook_reach.py --check-new

# Generera statusrapport för april
python fetch_facebook_reach.py --status 2025-04

# Debug-läge för felsökning
python fetch_facebook_reach.py --current-month-so-far --debug
```

**OBS!** Endast ett datumargument kan användas åt gången. Du kan inte kombinera t.ex. `--month` med `--last-week`.

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

```bash
python fetch_facebook_reach.py --update-all
```

### För att kontrollera nya sidor i alla befintliga rapporter

```bash
python fetch_facebook_reach.py --check-new
```

## Filnamnskonventioner

Skriptet använder olika filnamnskonventioner beroende på tidsperiod:

### Kompletta månader
- `FB_YYYY_MM.csv` (t.ex. `FB_2025_05.csv`)

### Partiella månader (inom samma månad)
- `FB_YYYY_MM_DD-DD.csv` (t.ex. `FB_2025_05_01-19.csv`)

### Custom perioder (över månader/år)
- `FB_YYYY-MM-DD_to_YYYY-MM-DD.csv` (t.ex. `FB_2025-04-25_to_2025-05-19.csv`)

### Statusrapporter
- `FB_STATUS_YYYY_MM.csv` (t.ex. `FB_STATUS_2025_05.csv`)

### Loggfiler
- `facebook_reach.log` (senaste loggen)
- `logs/facebook_reach_YYYY-MM-DD_HH-MM-SS.log` (arkiverade loggar)

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

### "Endast ett datumargument kan användas åt gången"
- **Problem**: Du försöker använda flera inkompatibla argument
- **Lösning**: Välj endast ett sätt att specificera datumintervall (t.ex. antingen `--month` eller `--last-week`, inte båda)

### Använd diagnostikverktyget
För att kontrollera dina token-behörigheter och tillgängliga sidor:

```bash
python check_token_permissions.py
```

Detta verktyg visar:
- Token-validitet och utgångsdatum
- Lista över alla tillgängliga sidor
- Behörigheter för varje sida
- Vilka sidor som har insights tillgängliga

## Schemaläggning

### Automatisk körning för kompletta månader

För att automatiskt köra skriptet varje månad:

#### Windows:
1. Öppna Task Scheduler (Aktivitetsschemaläggaren)
2. Skapa en ny uppgift
3. Ställ in att den ska köra `python` med sökvägen till skriptet
4. Schemalägg den att köra t.ex. den 1:a i varje månad

#### Mac/Linux:
1. Öppna Terminal
2. Kör `crontab -e`
3. Lägg till en rad som: `0 0 1 * * cd /sökväg/till/mappen && python fetch_facebook_reach.py`

### Automatisk körning för partiella månader

För att få daglig uppdatering av "månad hittills":

#### Windows Task Scheduler:
- Schemalägg att köra dagligen: `python fetch_facebook_reach.py --current-month-so-far --update-all`

#### Mac/Linux crontab:
```bash
# Kör varje dag kl 08:00
0 8 * * * cd /sökväg/till/mappen && python fetch_facebook_reach.py --current-month-so-far --update-all
```

### Veckorapporter

För att få veckorapporter varje måndag:

```bash
# Kör varje måndag kl 09:00
0 9 * * 1 cd /sökväg/till/mappen && python fetch_facebook_reach.py --last-week --update-all
```

**Tips**: Använd `--update-all` för schemalagda körningar för att säkerställa att alla värden uppdateras även om filen redan finns.

## Vanliga frågor (FAQ)

### Kan jag köra skriptet för samma period flera gånger?
Ja, skriptet hoppar automatiskt över sidor som redan finns i CSV-filen om du inte använder `--update-all`.

### Hur påverkar partiella månader mitt API-anrop limit?
Varje körning använder samma antal API-anrop som en vanlig månadskörning. Skriptet cachear sidnamn för att minimera anrop.

### Kan jag kombinera flera perioder i samma körning?
Nej, endast en tidsperiod per körning. Du kan däremot köra skriptet flera gånger med olika argument.

### Vad händer om jag kör `--current-month-so-far` flera gånger samma dag?
Med `--update-all` uppdateras alla värden. Utan `--update-all` hoppas befintliga sidor över för att spara API-anrop.

### Kan jag ändra filnamnsformatet?
Filnamnskonventionen är hårdkodad men du kan enkelt döpa om filerna efteråt. Funktionen `generate_custom_filename()` i skriptet hanterar namngivningen.

---

### Version History
- **v2.0**: Lagt till stöd för partiella månader och custom datumintervall
- **v1.x**: Grundfunktionalitet för kompletta månader och interaktionsdata