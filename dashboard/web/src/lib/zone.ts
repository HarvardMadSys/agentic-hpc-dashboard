/* The one time zone the page shows every time in: the service's.
 *
 * The service formats some times itself (in its local zone), and the page
 * formats the rest from epochs; drawn in the browser's zone, the two disagreed
 * on one screen as soon as reader and cluster were in different zones (a
 * session read 17:36 in a card and 02:59 in the portrait above it). So the page
 * reads every epoch in the service's zone, which /api/config names
 * (`_meta.tz`: an IANA name, else the current UTC offset), and says which zone
 * that is in its header. A service too old to say leaves the browser's zone,
 * and the header says that instead.
 *
 * Every date operation in src/ goes through here: `zdate` hands back a Date
 * whose UTC fields ARE the zone's wall clock (read it with getUTC*, step it with
 * setUTC*), and `zinstant` turns such a wall-clock Date back into an instant.
 */

export interface ZoneInfo {
  /** IANA name, e.g. America/New_York; null when the service could not tell */
  name: string | null;
  /** what the zone is called now, e.g. EST */
  abbrev: string | null;
  /** seconds east of UTC, now */
  utc_offset_s: number | null;
}

let zone: ZoneInfo | null = null;
let parts: Intl.DateTimeFormat | null = null;

/** Set the page's zone (App.tsx, from /api/config). An IANA name the browser
 *  does not know falls back to the fixed offset, then to the browser's zone. */
export function setZone(z: ZoneInfo | null): void {
  zone = z;
  parts = null;
  if (z?.name) {
    try {
      parts = new Intl.DateTimeFormat('en-US', {
        timeZone: z.name,
        hourCycle: 'h23',
        year: 'numeric',
        month: 'numeric',
        day: 'numeric',
        hour: 'numeric',
        minute: 'numeric',
        second: 'numeric',
      });
    } catch {
      parts = null;
    }
  }
}

/** The zone in force, as the page names it: the service's, or the browser's. */
export function zoneLabel(): { short: string; tip: string } {
  if (zone && (parts || zone.utc_offset_s != null)) {
    const off = offsetWords(offsetAt(Date.now()));
    const short = zone.abbrev || off;
    const name = zone.name ? `${zone.name} (${off})` : off;
    return { short, tip: `every time on this page is in the dashboard service's zone: ${name}` };
  }
  const off = offsetWords(-new Date().getTimezoneOffset() * 60);
  return {
    short: off,
    tip: `every time on this page is in your browser's zone (${off}): the service did not say its own`,
  };
}

function offsetWords(s: number): string {
  const sign = s < 0 ? '−' : '+';
  const a = Math.abs(s);
  const h = Math.floor(a / 3600);
  const m = Math.round((a % 3600) / 60);
  return `UTC${sign}${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/** Seconds east of UTC in the page's zone at instant `ms`. */
export function offsetAt(ms: number): number {
  if (parts) {
    const f: Record<string, number> = {};
    for (const p of parts.formatToParts(new Date(ms))) {
      if (p.type !== 'literal') f[p.type] = Number(p.value);
    }
    const wallAsUtc = Date.UTC(f.year, f.month - 1, f.day, f.hour === 24 ? 0 : f.hour, f.minute, f.second);
    // whole seconds: the instant's own milliseconds are not in the parts
    return Math.round((wallAsUtc - Math.floor(ms / 1000) * 1000) / 1000);
  }
  if (zone?.utc_offset_s != null) return zone.utc_offset_s;
  return -new Date(ms).getTimezoneOffset() * 60;
}

/** A Date whose UTC fields are the page zone's wall clock at instant `ms`. */
export function zdate(ms: number): Date {
  return new Date(ms + offsetAt(ms) * 1000);
}

/** The instant (ms) whose wall clock in the page's zone is the UTC fields of
 *  `w` (a Date from `zdate`, stepped with setUTC*). Two passes settle a step
 *  across a change of offset (daylight saving). */
export function zinstant(w: Date | number): number {
  const wall = typeof w === 'number' ? w : w.getTime();
  const guess = wall - offsetAt(wall) * 1000;
  return wall - offsetAt(guess) * 1000;
}

const p2 = (n: number) => String(n).padStart(2, '0');

/** `MM-DD` of an instant, in the page's zone. */
export function monthDay(ms: number): string {
  const d = zdate(ms);
  return `${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())}`;
}

/** `hh:mm:ss` of an instant, in the page's zone. */
export function hms(ms: number): string {
  const d = zdate(ms);
  return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`;
}

/** `YYYY-MM-DD` of an instant, in the page's zone. */
export function ymd(ms: number): string {
  const d = zdate(ms);
  return `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())}`;
}
