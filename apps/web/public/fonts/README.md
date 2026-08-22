# Vendored typefaces

Aleph's three faces, committed rather than fetched from a CDN.

## Why they are here

`apps/web/index.html` used to carry three `<link>` tags pointing at
`fonts.googleapis.com`. Aleph ships as a docker compose stack into networks
that may have no outbound route, and a CDN font does not fail loudly — it falls
back. On an air-gapped install every one of the three faces silently became a
system font, the interface stopped looking like itself, and nothing reported
it. `docs/plan.md` WS-E3.

The `@font-face` rules live in `apps/web/src/styles/fonts.css`. The families
they declare are the ones `apps/web/src/styles/tokens.css` names in
`--font-ui`, `--font-mono` and `--font-prose`; each of those keeps a real
fallback stack after the vendored name, so a missing file degrades to a system
face rather than to nothing.

## Provenance

| | |
|---|---|
| Source | `https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@100..800&family=Newsreader:opsz,wght@6..72,200..800&family=Public+Sans:wght@100..900&display=swap` |
| Fetched | 2026-08-22, with a Chrome user agent so the CDN serves `woff2` |
| Files | byte-identical to what `fonts.gstatic.com` served; not re-encoded, not re-subset |
| Licence | SIL Open Font License 1.1 — `OFL-jetbrains-mono.txt`, `OFL-newsreader.txt`, `OFL-public-sans.txt` |
| Versions | JetBrains Mono v24, Newsreader v26, Public Sans v21 (the `/s/<family>/vNN/` path segment upstream) |

One **variable** file per family per subset, not one per weight. Each
`@font-face` declares the font's real weight axis (`100 900` for Public Sans,
`100 800` for JetBrains Mono, `200 800` for Newsreader), so every weight the UI
asks for comes out of the same file and adding one costs no bytes.

## Byte cost

The per-subset `unicode-range` split is Google's and is kept verbatim, so a
browser downloads only the subsets the text on screen actually needs.

| file | bytes |
|---|---|
| `jetbrains-mono-cyrillic-ext.woff2` | 2,020 |
| `jetbrains-mono-cyrillic.woff2` | 12,064 |
| `jetbrains-mono-greek.woff2` | 9,084 |
| `jetbrains-mono-latin-ext.woff2` | 15,204 |
| `jetbrains-mono-latin.woff2` | 40,480 |
| `jetbrains-mono-vietnamese.woff2` | 7,468 |
| `newsreader-latin-ext.woff2` | 86,628 |
| `newsreader-latin.woff2` | 131,848 |
| `newsreader-vietnamese.woff2` | 27,204 |
| `public-sans-latin-ext.woff2` | 18,372 |
| `public-sans-latin.woff2` | 26,636 |
| `public-sans-vietnamese.woff2` | 7,576 |
| **all 12** | **384,584** |

A page of English text loads the three `-latin` files only: **198,964 bytes**
(194 KiB). The remaining 185,620 bytes are fetched only
when Latin-Extended, Vietnamese, Greek or Cyrillic text is rendered — which is
the point of keeping the split rather than merging each family into one file.

No face is preloaded. Three `<link rel=preload>` tags would pull every subset
up front, including the ones the page never renders, which is the cost the
split exists to avoid.

## Re-vendoring

Re-take the stylesheet and the files together. The `unicode-range` values in
`fonts.css` were copied from that stylesheet; a range left behind after a
re-subset does not error, it just stops a glyph resolving.
