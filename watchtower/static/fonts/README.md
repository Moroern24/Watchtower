# Self-hosted fonts

The dashboard never loads fonts from a CDN (no visitor IP goes to Google — GDPR). If this folder
contains `fonts.css`, `GET /` links it; otherwise the page uses system fonts.

Expected files (SIL Open Font License 1.1, from the `@fontsource` packages):

| File | Font |
|---|---|
| `inter-latin-300-normal.woff2`, `-400-`, `-500-`, `-600-` | Inter (interface) |
| `inter-latin-ext-300-normal.woff2`, `-400-`, `-500-`, `-600-` | Inter, Central/Eastern European letters |
| `eb-garamond-latin-500-normal.woff2` | EB Garamond (wordmark) |

`fonts.css` declares them with `@font-face` (`font-display: swap`, `unicode-range` so latin-ext
loads only when needed) and URLs `/static/fonts/<file>`. Source: `@fontsource/inter@5.1.0` and
`@fontsource/eb-garamond@5.1.0`; license texts are in `LICENSE-*-OFL.txt` (keep them with the fonts).
