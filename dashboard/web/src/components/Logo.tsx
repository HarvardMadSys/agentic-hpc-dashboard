/* The logo's glyph, without its crimson tile: the tile is `.bmark` (styles.css),
 * so the rail and the top bar keep their one crimson fill in chrome. The full
 * mark, tile included, is docs/logo/mark.svg and public/favicon.svg; keep the
 * three in step.
 *
 * pstree's └─ from a root process, ending in the agent's spark.
 */
export function LogoGlyph({ size = 30 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false">
      <circle cx="9" cy="8.5" r="3" />
      <path
        d="M9 12.1V18.3a1.2 1.2 0 0 0 1.2 1.2H13.1"
        fill="none"
        stroke="currentColor"
        strokeWidth={2.6}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M19.5 12.5C20.41 18.59 20.41 18.59 26.5 19.5C20.41 20.41 20.41 20.41 19.5 26.5C18.59 20.41 18.59 20.41 12.5 19.5C18.59 18.59 18.59 18.59 19.5 12.5Z" />
    </svg>
  );
}
