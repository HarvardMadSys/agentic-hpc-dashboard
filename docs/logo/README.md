# Logo

An observatory: a dome on its base, its telescope trained on the agent's spark.
Crimson `#A51C30` is the page's accent and the `agent` series colour, so the
mark uses it and nothing else.

| file | use |
|---|---|
| `mark.svg` | the mark on its crimson tile; also `dashboard/web/public/favicon.svg` |
| `mark-512.png` | the mark as a raster, for places that won't take SVG |
| `lockup.svg` / `lockup-dark.svg` | mark + "Agent Observatory" over "Harvard MaDSys", for light / dark grounds |
| `dashboard/web/public/apple-touch-icon.png` | the mark, 180 px and full bleed, for a phone's home screen (`index.html`) |

The wordmark is IBM Plex Sans 600 and 500 (the dashboard's face),
outlined to paths so it renders the same without the fonts installed.

Harvard appears as words, not as the Veritas shield: Harvard's identity rules
keep the shield unaltered and uncombined, so it is shown only in the official
logos under `dashboard/web/public/logos/` (the rail's credits and the README).

In the app the glyph is `dashboard/web/src/components/Logo.tsx`, drawn in
`currentColor` on the `.bmark` tile. The sign-in page
(`dashboard/rc_dashboard/auth.py`) draws its own copy.
Keep the four in step: `Logo.tsx`, `auth.py`, `mark.svg` and `favicon.svg`, and export
`mark-512.png` and `apple-touch-icon.png` again from `mark.svg` when it changes.
