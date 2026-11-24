import { useMemo } from 'react';
import { setZone } from './zone';
import type { ZoneInfo } from './zone';

/** The service's zone (lib/zone.ts), set from /api/config's `_meta.tz` before
 *  anything below the caller draws. Returns a key that changes with it, so the
 *  caller can draw again what it drew in the browser's zone meanwhile. Every page
 *  that shows a time calls it: the dashboard (App.tsx), and the process tree popped
 *  out of it (TreePopout.tsx), a page of its own that App never wraps. */
export function useServiceZone(config: Record<string, unknown> | null | undefined): string {
  const meta = config?._meta as Record<string, { _value?: unknown }> | undefined;
  const tzInfo = (meta?.tz?._value ?? null) as ZoneInfo | null;
  const tzKey = tzInfo ? `${tzInfo.name ?? ''}|${tzInfo.utc_offset_s ?? ''}` : '';
  useMemo(() => setZone(tzInfo), [tzKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return tzKey;
}
