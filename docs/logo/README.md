# Logo

The logo is an observatory: a dome on its base, with its telescope pointed at the
agent's spark. Crimson `#A51C30` is the page's accent colour and the `agent`
series colour, so the mark uses that colour and no other.

| file | use |
|---|---|
| `mark.svg` | the mark on its crimson tile; also `dashboard/web/public/favicon.svg` |
| `mark-512.png` | the mark as a raster, for places that won't take SVG |
| `lockup.svg` / `lockup-dark.svg` | mark + "Agent Observatory" over "Harvard MaDSys", for light / dark grounds |
| `dashboard/web/public/apple-touch-icon.png` | the mark, 180 px and full bleed, for a phone's home screen (`index.html`) |

The wordmark is set in IBM Plex Sans 600 and 500 (the dashboard's typeface) and
converted to paths, so it renders the same on machines without the fonts.

Harvard appears as text, not as the Veritas shield: Harvard's identity rules forbid
altering the shield or combining it with other marks, so it appears only in the official
logos under `dashboard/web/public/logos/` (the rail's credits and the root README).

The glyph has four copies to keep in step: `mark.svg`, `favicon.svg`,
`dashboard/web/src/components/Logo.tsx` (drawn in `currentColor` on the `.bmark` tile),
and the sign-in page's own, in `dashboard/rc_dashboard/auth.py`. When `mark.svg` changes,
export `mark-512.png` and `apple-touch-icon.png` from it again.
