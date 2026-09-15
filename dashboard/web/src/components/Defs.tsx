/* One hidden SVG carrying document-level paint servers.
 *
 * `denied` is not a measured state -- it is the collector being unable to read
 * the field, and it must not look like `unsandboxed`. It therefore gets both a
 * different hue AND a hatch, so it is separable at a glance and under any
 * colour-vision deficiency.
 */
export function Defs() {
  return (
    <svg
      width="0"
      height="0"
      aria-hidden="true"
      style={{ position: 'absolute', width: 0, height: 0, overflow: 'hidden' }}
    >
      <defs>
        <pattern id="hatchDenied" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <rect width="6" height="6" fill="var(--sg)" />
          <line x1="0" y1="0" x2="0" y2="6" stroke="var(--surface)" strokeWidth="2.4" />
        </pattern>
        <pattern id="hatchUnknown" width="5" height="5" patternUnits="userSpaceOnUse">
          <rect width="5" height="5" fill="var(--edge)" />
          <circle cx="2.5" cy="2.5" r="0.9" fill="var(--surface)" />
        </pattern>
      </defs>
    </svg>
  );
}
