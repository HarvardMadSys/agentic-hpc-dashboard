/* The logo's glyph, without its crimson tile: the tile is `.bmark` (styles.css),
 * so the rail and the top bar keep their one crimson fill in chrome. The full
 * mark, tile included, is docs/logo/mark.svg and public/favicon.svg; keep the
 * three in step.
 *
 * A process tree seen from its root: one child is the agent (the spark), the
 * other branch is ordinary processes (the dimmed dots).
 */
export function LogoGlyph({ size = 30 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false">
      <path
        d="M9.5 10.5V21a2.5 2.5 0 0 0 2.5 2.5H14.5M9.5 15.5H13"
        fill="none"
        stroke="currentColor"
        strokeWidth={2.2}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="9.5" cy="8.5" r="2.7" />
      <path d="M19.6 9.5C20.32 14.78 20.32 14.78 25.6 15.5C20.32 16.22 20.32 16.22 19.6 21.5C18.88 16.22 18.88 16.22 13.6 15.5C18.88 14.78 18.88 14.78 19.6 9.5Z" />
      <circle cx="18" cy="23.5" r="1.9" fillOpacity={0.6} />
      <circle cx="23" cy="23.5" r="1.9" fillOpacity={0.6} />
    </svg>
  );
}
