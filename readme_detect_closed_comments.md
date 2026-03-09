# detect_closed_comments.py

Identifierar möjliga stängda kommentarsfält på Facebook-sidor via Meta Graph API.

Eftersom det inte finns någon officiell API-flagga för stängda kommentarer används en heuristik baserad på tre API-signaler. Resultatet är en **probabilistisk uppskattning**, inte definitiv sanning.

---

## Krav

- Python 3.8+
- Paketen `requests` (installeras med `pip install requests`)
- En `config.py` baserad på `config.py.example`
- En systemanvändartoken med behörigheterna:
  - `pages_read_engagement`
  - `pages_read_user_content`
  - `read_insights`
- Systemanvändaren behöver minst **ANALYZE-åtkomst** till de aktuella sidorna

---

## Användning

```bash
# Alla sidor, alla inlägg
python detect_closed_comments.py

# Specifik sida
python detect_closed_comments.py --page-id 123456789

# Begränsa antal inlägg per sida
python detect_closed_comments.py --post-limit 50

# Eget filnamn på CSV-utdata
python detect_closed_comments.py --output mitt_resultat.csv

# Kombinera flaggor
python detect_closed_comments.py --page-id 123456789 --post-limit 100 --output sida_x.csv

# Aktivera debug-loggning
python detect_closed_comments.py --debug
```

---

## Utdata

### CSV-fil

Sparas som `closed_comments_YYYY-MM-DD.csv` (om inget annat anges med `--output`).

| Kolumn | Beskrivning |
|---|---|
| `page_id` | Facebook-sidans ID |
| `page_name` | Sidans namn |
| `post_id` | Inläggets ID |
| `created_time` | Publiceringstidpunkt |
| `can_comment` | Om aktören kan kommentera (`True`/`False`/`None`) |
| `comment_count` | Antal kommentarer enligt API:et |
| `comments_edge_error` | Om `/comments`-kanten returnerade fel (`True`/`False`) |
| `classification` | Klassificering (se nedan) |
| `message_preview` | De första 60 tecknen i inläggets text |

### Terminalsammanfattning

Efter körning skrivs en sammanfattning med antal inlägg per klass samt en lista över inlägg klassificerade som `probably_closed`.

---

## Klassificering

| Klass | Betydelse |
|---|---|
| `open` | Kommentering sannolikt öppen – `can_comment` är `True` |
| `restricted` | Kommentering begränsad – `can_comment` är `False` men kommentarer finns |
| `probably_closed` | Kommentarsfältet är troligen stängt – `can_comment` är `False`, inga kommentarer, `/comments`-kanten ger fel |
| `uncertain` | Oklart läge – `can_comment` är `False`, inga kommentarer, men `/comments`-kanten fungerar |
| `unknown` | Kunde inte hämta tillräcklig data för klassificering |

### Beslutslogik

```
can_comment = True                          → open
can_comment = False, kommentarer finns      → restricted
can_comment = False, 0 kommentarer, fel     → probably_closed
can_comment = False, 0 kommentarer, ok      → uncertain
```

---

## Begränsningar

Felklassificering kan förekomma när:

- Tokenen saknar fulla rättigheter till sidan
- Kommentarer är modererade eller dolda
- Kommentering är begränsad till en viss målgrupp
- Sidinställningar påverkar vad API:et exponerar

Behandla alltid resultatet som en indikation, inte som ett definitivt svar.

---

## Loggfiler

Loggfiler sparas automatiskt i katalogen `logs/` med tidsstämplat filnamn:

```
logs/detect_closed_comments_YYYY-MM-DD_HH-MM-SS.log
```
