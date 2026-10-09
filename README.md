# eiriksbok-lyd

Fortellerstemmen til artikkelfilmene i [Eiriksbok](https://bok.haaland.de/).

- **Stemme:** Google Chirp 3 HD, `nb-NO-Chirp3-HD-Algieba`, tempo 1,1.
- **Hvor:** publisert til Cloudflare Pages, `https://eiriksbok-lyd.pages.dev`. Filmspilleren
  (`src/features/film/useFilmNarrator.ts` i Eiriksbok) henter `film/<fag>/<emne>/<id>.json` og
  spiller mp3-en den peker på. Finnes ikke lyden, eller er manuset endret siden lyden ble laget,
  bruker filmen nettleserens stemme.
- **Hvordan:** `.github/workflows/lag-lyd.yml` kjører hver time. Den henter filmmanusene fra
  Eiriksbok, lager lyd til filmer som er nye eller endret (`lag_lyd.py`), sjekker hver replikk
  med Nasjonalbibliotekets Whisper, committer og publiserer.

## Filer

| Fil | Hva |
|---|---|
| `site/film/<fag>/<emne>/<id>-<avtrykk>.mp3` | Hele filmens fortellerstemme, med pausene bakt inn |
| `site/film/<fag>/<emne>/<id>.json` | Start og slutt for hver replikk, fingeravtrykk og eventuelle avvik |
| `site/filmer.json` | Oversikt over alle filmer med lyd |
| `kvote.json` | Tegn sendt til Google per måned. Jobben stopper før 800 000 (gratiskvoten er 1 million) |
| `.sist-publisert` | Hvilken versjon av `site/` som sist ble publisert |

## Hemmeligheter (Settings > Secrets and variables > Actions)

| Navn | Hva |
|---|---|
| `GOOGLE_TTS_KEY` | Google Cloud API-nøkkel begrenset til Cloud Text-to-Speech API |
| `CLOUDFLARE_API_TOKEN` | Cloudflare API-token med «Cloudflare Pages: Edit» |
| `CLOUDFLARE_ACCOUNT_ID` | Konto-ID-en fra Cloudflare-dashbordet |

Mangler en hemmelighet, hopper jobben over det steget og skriver en advarsel.

## For hånd

- **Lag én film på nytt:** Actions > Lag filmlyd > Run workflow, fyll inn filmen og huk av «tving».
- **Lokalt:** `pip install -r requirements.txt`, sett `GOOGLE_TTS_KEY`, og kjør
  `python lag_lyd.py --manus ../Eiriksbok/src/features/film/manus`.
- **Avvik:** når Whisper hører noe annet enn manuset to ganger, lages et issue her. Lytt på
  replikken. Er uttalen feil, skriv `uttale` på replikken i manuset, så lages lyden på nytt.

Bytte stemme eller tempo: endre `STEMME`/`TEMPO` øverst i `lag_lyd.py`. Alle filmer får nytt
fingeravtrykk og lages på nytt ved neste kjøring (ca. 6000 tegn per film).
