/* The narrow layout: at or below this width the rail is a strip of tabs across
 * the top and every panel grid is one column (styles.css, its `max-width:
 * 900px` block). What is drawn differently there that CSS alone cannot say --
 * a chart drawn at the width it is shown at (charts/primitives.tsx `useFit`) --
 * reads it from here, so the two never disagree. */
import { useSyncExternalStore } from 'react';

export const NARROW_QUERY = '(max-width: 900px)';

function subscribe(cb: () => void) {
  const mq = window.matchMedia(NARROW_QUERY);
  mq.addEventListener('change', cb);
  return () => mq.removeEventListener('change', cb);
}

/** Whether the page is laid out narrow: a phone, a tablet held upright, a narrow window. */
export function useNarrow(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(NARROW_QUERY).matches,
    () => false,
  );
}
