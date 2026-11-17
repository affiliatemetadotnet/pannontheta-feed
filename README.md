# Pannon-Theta – Árukereső termékfeed

Naponta automatikusan frissülő Árukereső XML feed a pannontheta.hu webáruházból.

**Feed címe:** https://affiliatemetadotnet.github.io/pannontheta-feed/arukereso.xml
**Riport:** https://affiliatemetadotnet.github.io/pannontheta-feed/arukereso_riport.txt

## Működés

A `.github/workflows/arukereso-feed.yml` minden nap 13:37 UTC-kor (14:37 / nyáron 15:37 budapesti idő) lefut:

1. `fetch_api.py` – letölti az összes terméket és kategóriát a Shoprenter API-ból
2. `arukereso_feed.py` – elkészíti az XML-t, ellenőrzi a képeket, termékoldalakat és árakat
3. ha legalább 50 termék van benne, kiteszi a GitHub Pages-re (különben a tegnapi feed marad kint)

Kézi indítás: **Actions** fül → *Árukereső feed frissítése* → **Run workflow**.
Minden futás összefoglalójában ott a riport (mi került be, mi maradt ki és miért).

## Szabályok

- csak székek (irodai, tárgyaló, konferencia, gamer, 24 órás, térdeplő, munkaszék, labdaszék) –
  alkatrész, kiegészítő, asztal, csomag és OUTLET/bemutató darab nem
- **raktáron** (`stock1` vagy `stock2` > 0) → `1 munkanap`
- minden raktár 0 és a nincs-készleten állapot „Külső raktárról 3-5 munkanap” → `5 munkanap`
- webp és helyettesítő („nincs kép”) képek kiszűrve – az Árukereső csak jpg/png/gif/bmp-t fogad el
- terméknevek: `arukereso_nevek.json` (új termék automatikus nevet kap, a riport jelzi)

## Titkos adatok

A Shoprenter API-kulcs a repó **Settings → Secrets and variables → Actions** alatt van
(`SHOPRENTER_CLIENT_ID`, `SHOPRENTER_CLIENT_SECRET`), a kódban nem szerepel.
