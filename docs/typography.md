# Typography

NoNonCents uses two font families, both self-hosted:

- Plus Jakarta Sans for interface, body, forms, navigation, tables, alerts, labels,
  supporting text, and chart legends.
- Space Grotesk for the wordmark, headings, balances, totals, and major financial
  summaries.

Both are variable WOFF2 assets stored under `budget/static/fonts/` and loaded with
`font-display: swap`. The interface and display fallback stacks remain fully usable if
a font is delayed or unavailable. Semantic family, scale, and financial-numeral tokens
are centralized in `budget/static/budget/typography.css`.

## Sources and licensing

- Plus Jakarta Sans variable WOFF2, version 2.4653, from the official
  `tokotype/PlusJakartaSans` repository.
- Space Grotesk variable WOFF2, version 2.0, from the official
  `floriankarsten/space-grotesk` repository.

Each font is distributed under the SIL Open Font License 1.1. Its unmodified `OFL.txt`
is included beside the corresponding WOFF2 asset. No external font provider is used.

