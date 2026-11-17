/* The logo's glyph, without its crimson tile: the tile is `.bmark` (styles.css),
 * so the rail and the top bar keep their one crimson fill in chrome. The full
 * mark, tile included, is docs/logo/mark.svg and public/favicon.svg, and the
 * sign-in page (rc_dashboard/auth.py) draws its own copy; keep the four in step.
 *
 * An observatory: a dome on its base, its telescope trained on the agent's spark.
 */
export function LogoGlyph({ size = 30 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M5.7 24.1a8 8 0 0 1 16 0z" />
      <path d="M14.4 17.9L18.4 13.9" stroke="currentColor" strokeWidth={3.2} strokeLinecap="round" />
      <rect x="4.7" y="23.9" width="18" height="2.6" rx="1.3" />
      <path d="M23.4 4.6C23.96 8.34 23.96 8.34 27.7 8.9C23.96 9.46 23.96 9.46 23.4 13.2C22.84 9.46 22.84 9.46 19.1 8.9C22.84 8.34 22.84 8.34 23.4 4.6Z" />
    </svg>
  );
}
