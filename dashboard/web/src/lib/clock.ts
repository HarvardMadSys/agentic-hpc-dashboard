/* The server's present, for anything the page compares with a server time.
 *
 * Every window, age and bound the service reports is taken against ITS clock,
 * and that clock is not always the browser's: a service over a capture copied
 * from another cluster pins its clock to the capture's end (rc_dashboard/
 * clock.py), and a browser's own clock can simply be wrong. Taking "now" from
 * `Date.now()` then asked the events query for a day the capture does not hold
 * and called a minute-old record two days old. Each live frame carries the
 * service's `clock.now`; between frames the offset holds.
 *
 * A pinned clock is also FROZEN: the capture it stands over will not grow. A
 * reader asks `clockPinned()` where "not yet" would promise what cannot come,
 * as for a live edge the census cannot say.
 */
let offsetMs = 0;
let pinned = false;

export function syncServerClock(nowS: number | null | undefined): void {
  if (nowS != null && Number.isFinite(nowS)) offsetMs = nowS * 1000 - Date.now();
}

/** Epoch milliseconds on the server's clock. */
export function serverNow(): number {
  return Date.now() + offsetMs;
}

/** Record whether the service's clock is pinned to a capture's end (`clock.pinned` of a live frame). */
export function setClockPinned(b: boolean): void {
  pinned = b;
}

/** True when the service's clock is pinned, and so frozen: a frame from the wall clock says false. */
export function clockPinned(): boolean {
  return pinned;
}
