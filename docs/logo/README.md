# Logo

A process tree seen from its root: one child is the agent (the spark), the other
branch is ordinary processes (the dimmed dots). Crimson `#A51C30` is the page's
accent and the `agent` series colour, so the mark uses it and nothing else.

| file | use |
|---|---|
| `mark.svg` | the mark on its crimson tile; also `dashboard/web/public/favicon.svg` |
| `mark-512.png` | the mark as a raster, for places that won't take SVG |
| `lockup.svg` / `lockup-dark.svg` | mark + wordmark under a HARVARD · MADSYS line, for light / dark grounds |

The wordmark is IBM Plex Sans 600 and IBM Plex Mono 500 (the dashboard's faces),
outlined to paths so it renders the same without the fonts installed.

Harvard appears as words, not as the Veritas shield: Harvard's identity rules
keep the shield unaltered and uncombined, so it is shown only in the official
logos under `dashboard/web/public/logos/` (the rail's credits and the README).

In the app the glyph is `dashboard/web/src/components/Logo.tsx`, drawn in
`currentColor` on the `.bmark` tile. Keep it, `mark.svg` and `favicon.svg` in step.
