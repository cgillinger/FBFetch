# Flyttkandidater → cgillinger/fetch_comments

*Noterat 2026-07-06 i samband med städanalysen. Beslut: avvaktar — genomförs inte i städomgång 1.*

Målbild vid flytt: **FBFetch = insights/statistik** (viewers, demographics, Instagram-poster, DM-statistik, page status), **fetch_comments = allt kommentarsrelaterat**.

## 1. `detect_closed_comments (1).py` — självklar kandidat

- Identifierar stängda kommentarsfält på sidors inlägg (heuristik på `can_comment`, `comments.summary`, `/comments`-edge). Renodlad kommentarsdomän.
- Otrackad i FBFetch idag (webbläsarnedladdningsnamn med " (1)") — har aldrig fått ett riktigt hem.
- Flytten är alltså en *placering*, inte en flytt.

## 2. `fetch_facebook_comments.py` (räknaren) — bra kandidat

- Räknar kommentarer + replies per månad → CSV. Samma domän som exportören som redan bor i fetch_comments.
- Om den flyttas försvinner behovet av omdöpning till `_count` — namnkrocken med `ver2` finns inte kvar när `ver2` tagits bort ur FBFetch.

## Teknisk hake vid flytt: konfigurationsstil

- `fetch_comments.py` (i fetch_comments-repot) är självförsörjande: läser `FB_ACCESS_TOKEN` från env/`.env`, allt annat via argparse-flaggor.
- Båda kandidatskripten importerar istället `ACCESS_TOKEN`, `API_VERSION`, `CACHE_FILE` m.fl. från FBFetch:s `config.py`.
- **Flytten ska inkludera anpassning till env-stilen** (`from config import …` → `os.getenv`/flaggor), annars dras ett `config.py`-beroende in i ett repo som medvetet saknar sådant.

## Ej kandidater

- `fetch_facebook_dms.py` — meddelanden, inte kommentarer.
- Övriga skript — insights-domän, stannar i FBFetch.

## Kom-ihåg vid genomförande

- [ ] Flytta + anpassa `detect_closed_comments.py` (döp om utan " (1)")
- [ ] Flytta + anpassa `fetch_facebook_comments.py`
- [ ] Uppdatera README i båda repon (FBFetch: ta bort ur skripttabellen; fetch_comments: dokumentera nya verktyg)
- [ ] Ta bort filerna ur FBFetch efter att flytten är mergad
- [ ] Ingen server2-påverkan: inget av skripten körs schemalagt (verifierat 2026-07-06 — cron.d, /etc/crontab, chris + root crontab)
