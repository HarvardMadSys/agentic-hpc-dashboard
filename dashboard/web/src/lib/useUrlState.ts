/* URL-backed view state.
 *
 * Every filter, the active tab and the trajectory rank live in the query string,
 * so a reload -- or a pasted link -- restores exactly the view that was on
 * screen, and the Download button can be built from the same string the table
 * was fetched with.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { parseUrl, patched, urlQuery } from './filters';
import type { UrlState } from './filters';

export function useUrlState(): [UrlState, (patch: Partial<UrlState>, push?: boolean) => void] {
  const [state, setState] = useState<UrlState>(() => parseUrl(location.search));
  // The state as of the last write, so a write composes with one made just
  // before it in the same tick. The history is written here, once per write,
  // not inside a setState updater: React may call an updater twice (StrictMode
  // does, in development), and each tab change then pushed two history
  // entries, so Back seemed to do nothing.
  const cur = useRef(state);

  useEffect(() => {
    const onPop = () => {
      cur.current = parseUrl(location.search);
      setState(cur.current);
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const update = useCallback((patch: Partial<UrlState>, push = false) => {
    // another tab closes the session open on this one (`patched`)
    const next: UrlState = patched(cur.current, patch);
    cur.current = next;
    const q = urlQuery(next);
    const url = location.pathname + (q ? '?' + q : '') + location.hash;
    // A tab change is navigation (back should undo it); typing in a filter
    // field is not, or the history stack fills with keystrokes.
    if (push) history.pushState(null, '', url);
    else history.replaceState(null, '', url);
    setState(next);
  }, []);

  return [state, update];
}
