/* The server's present, for anything the page compares with a server time.
 *
 * Every window, age and bound the service reports is taken against ITS clock,
 * and that clock is not always the browser's: a service over a capture copied
 * from another cluster pins its clock to the capture's end (rc_dashboard/
 * clock.py), and a browser's own clock can simply be wrong. Taking "now" from
 * `Date.now()` then asked the events query for a day the capture does not hold
 * and called a minute-old record two days old. Each live frame carries the
 * service's `clock.now`; between frames the offset holds.
 */
let offsetMs = 0;

export function syncServerClock(nowS: number | null | undefined): void {
  if (nowS != null && Number.isFinite(nowS)) offsetMs = nowS * 1000 - Date.now();
}

/** Epoch milliseconds on the server's clock. */
export function serverNow(): number {
  return Date.now() + offsetMs;
}
