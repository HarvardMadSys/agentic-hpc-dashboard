/* URL-backed view state.
 *
 * Every filter, the active tab and the trajectory rank live in the query string,
 * so a reload -- or a pasted link -- restores exactly the view that was on
 * screen, and the Download button can be built from the same string the table
 * was fetched with.
 */
import { useCallback, useEffect, useState } from 'react';
import { parseUrl, urlQuery } from './filters';
import type { UrlState } from './filters';

export function useUrlState(): [UrlState, (patch: Partial<UrlState>, push?: boolean) => void] {
  const [state, setState] = useState<UrlState>(() => parseUrl(location.search));

  useEffect(() => {
    const onPop = () => setState(parseUrl(location.search));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const update = useCallback((patch: Partial<UrlState>, push = false) => {
    setState((prev) => {
      const next: UrlState = { ...prev, ...patch };
      const q = urlQuery(next);
      const url = location.pathname + (q ? '?' + q : '') + location.hash;
      // A tab change is navigation (back should undo it); typing in a filter
      // field is not, or the history stack fills with keystrokes.
      if (push) history.pushState(null, '', url);
      else history.replaceState(null, '', url);
      return next;
    });
  }, []);

  return [state, update];
}
