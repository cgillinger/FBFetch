# Städanalys — FBFetch

*Framtagen 2026-07-06 på branch `claude/repot-cleanup-review-3xzctz`. Avsedd att följas upp i terminalsession med åtkomst till server2.*

## Sammanfattning

Repot innehåller 16 Python-skript, tre README-filer och en 1,3 MB PDF — allt i roten. Ungefär hälften av filerna är historiskt bagage: gamla dokument, deprecerade skript, versionsdubbletter och tre överlappande behörighetsverktyg. `README.md` är aktuell och bra; problemet är allt runt omkring.

Målbild efter städning: ca 8 skript i roten, inga versionsnummer i filnamn, en README, `requirements.txt` + `LICENSE` på plats.

---

## Fynd

### 1. Tre README-filer — skiftlägeskollision på Windows

- `readme.md` och `readme2.md` dokumenterar gamla `fetch_facebook_reach.py` v1/v2, som numera är `DEPRECATED_` och inte fungerar.
- `README.md` vs `readme.md` skiljer sig bara i skiftläge → på Windows (case-insensitivt filsystem) kan de inte samexistera; `git checkout` ger oförutsägbart resultat om vilken som vinner.

**Åtgärd:** Ta bort `readme.md` och `readme2.md`. Innehållet finns i git-historiken.

### 2. Fyra DEPRECATED_-skript (~165 KB död kod)

`DEPRECATED_fetch_facebook_reach.py`, `DEPRECATED_fetch_facebook_reach_no_click.py`, `DEPRECATED_fetch_facebook_reach_weekly.py`, `DEPRECATED_fetch_instagram_reach.py`.

Metas gamla räckviddsmått dog 2026-06-15; skripten behålls enligt README "endast som referens" — men git-historiken är den referensen.

**Åtgärd:** Sätt en tagg (t.ex. `pre-viewers`) på sista committen där de finns, ta sedan bort alla fyra. README-tabellen "Deprekerade skript" behålls men får peka på taggen. Mjukare alternativ: flytta till `deprecated/`-mapp.

### 3. `fetch_instagram_posts_ver4_2.py` — död dubblett

README dokumenterar endast `ver4_6`; 4.2 refereras ingenstans.

**Åtgärd:** Ta bort. Döp samtidigt om `fetch_instagram_posts_ver4_6.py` → `fetch_instagram_posts.py`.
⚠️ **Kräver server2-synk:** om skriptet körs schemalagt bryter namnbytet jobbet — uppdatera Task Scheduler/cron samtidigt.

### 4. `fetch_facebook_comments.py` vs `_ver2.py` — olika verktyg, förvirrande namn

Räknare respektive fullständig exportör; båda dokumenterade och ska behållas. Namnen antyder dock gammal/ny version.

**Åtgärd:** Döp om till `fetch_facebook_comments_count.py` och `fetch_facebook_comments_export.py`.
⚠️ **Kräver server2-synk:** README noterar att ver2 "speglar versionen som körs på server2" — kontrollera vilket jobb som pekar på filen innan namnbyte.

### 5. Tre överlappande behörighetsverktyg

- `check_token_permissions.py` — äldre FB-check
- `permissions_check.py` — kallar sig själv "Förbättrad version" av samma sak
- `instagram-permission-checker.py` — vars egen filhuvudkommentar säger `check_instagram_permissions.py`

**Åtgärd:** Konsolidera till ett verktyg, förslagsvis `permissions_check.py` med `--instagram`-flagga; ta bort de två andra. Snabbvariant: ta bara bort `check_token_permissions.py`.

### 6. `Page_insights - Graph API.pdf` (1,3 MB)

Metas egen dokumentation, som dessutom beskriver de mått som deprecerades i juni.

**Åtgärd:** Ta bort; ersätt med länk till Metas docs i README.

### 7. Inaktuella exempel i README.md

Rad 213–225 samt 337 och 344 kör exempel mot `fetch_facebook_reach_weekly.py` utan `DEPRECATED_`-prefixet — kommandona fungerar inte som de står.

**Åtgärd:** Stryk avsnittet om deprecated-skripten tas bort (punkt 2); annars rätta filnamnen.

### 8. Saknade filer

- **`requirements.txt`** — skripten importerar `requests`, `pandas`, `openpyxl`, `python-dotenv`, men README listar bara `requests` under Krav.
- **`LICENSE`** — README har MIT-badge men LICENSE-fil saknas.

**Åtgärd:** Lägg till båda; uppdatera Krav-avsnittet i README.

---

## Åtgärdsplan

### Steg A — riskfritt, ingen funktion påverkas

1. Tagga nuvarande läge: `git tag pre-viewers-cleanup`
2. Ta bort: `readme.md`, `readme2.md`, `fetch_instagram_posts_ver4_2.py`, `Page_insights - Graph API.pdf`, de fyra `DEPRECATED_*.py`
3. Rätta README-exemplen (punkt 7), lägg till PDF-ersättande länk
4. Lägg till `requirements.txt` och `LICENSE`

### Steg B — kräver koll mot server2 / schemalagda jobb

5. Inventera server2: vilka filnamn refereras i cron/Task Scheduler? (`crontab -l`, schtasks, ev. wrapperscript)
6. Döp om `fetch_instagram_posts_ver4_6.py` → `fetch_instagram_posts.py`
7. Döp om `fetch_facebook_comments.py` → `fetch_facebook_comments_count.py` och `fetch_facebook_comments_ver2.py` → `fetch_facebook_comments_export.py`
8. Uppdatera jobben på server2 till de nya namnen, verifiera en körning
9. Konsolidera behörighetsverktygen (punkt 5), uppdatera README-tabellen

### Checklista server2

- [ ] `crontab -l` / schemalagda jobb inventerade
- [ ] Jobb som pekar på `fetch_facebook_comments_ver2.py` identifierat
- [ ] Jobb som pekar på `fetch_instagram_posts_ver4_6.py` identifierat (om det finns)
- [ ] Nya filnamn utrullade + jobb uppdaterade
- [ ] En schemalagd körning verifierad efter bytet
