/* A tab whose code is a chunk of its own (React.lazy), so the page's first
 * paint loads, parses and runs the shell and the Live tab only.
 *
 * Every such tab's code is fetched once the page is idle after it first paints
 * (`preloadTabs`, App.tsx), so a tab opened after that is drawn at once, from
 * the module, with no fallback: what is on screen is what it was before the
 * split. A tab opened sooner -- a link straight to it -- waits for its code in
 * the shell's <Suspense>. A tab that began by waiting keeps its lazy wrapper
 * for as long as it is mounted: swapping it for the module's own component
 * would mount the tab afresh.
 *
 * Its stylesheet is not split off with it: App imports each tab's CSS where it
 * imported the tab, so the page's one stylesheet holds the same rules in the
 * same order, and no rule can win or lose by arriving later.
 */
import { Component, createElement, lazy, memo, useState } from 'react';
import type { ComponentType, ReactNode } from 'react';

const loads: (() => Promise<unknown>)[] = [];

export function lazyTab<P extends object>(load: () => Promise<ComponentType<P>>): ComponentType<P> {
  let got: ComponentType<P> | null = null;
  let once: Promise<ComponentType<P>> | null = null;
  const fetchOnce = () => (once ??= load().then((c) => (got = c)));
  loads.push(fetchOnce);
  const Lazy = lazy(() => fetchOnce().then((c) => ({ default: c })));
  function Tab(props: P) {
    // the module's own component, when it had come as this tab mounted
    const [C] = useState(() => got);
    return createElement(C ?? Lazy, props);
  }
  return memo(Tab) as unknown as ComponentType<P>;
}

/** Fetch every lazy tab's code ahead of its first use. One that fails stays failed (as React.lazy and
 *  the browser's module map keep it), and its tab is left blank by `TabBoundary`. */
export function preloadTabs(): void {
  for (const f of loads) f().catch(() => {});
}

/** A chunk that would not load: the module script's fetch failed (each engine words it its own way). */
const chunkFailed = (e: unknown): boolean =>
  e instanceof Error && /dynamically imported module|module script failed|error loading dynamically|Loading chunk/i.test(e.message);

/** A tab whose code could not be fetched (a deploy replaced it under a page
 *  opened just before) is left blank, rather than taking the whole page down:
 *  the page itself never fetched code after it loaded until the tabs were
 *  split. Any other error goes on up, as it always did. */
export class TabBoundary extends Component<{ children: ReactNode }, { error: unknown }> {
  state: { error: unknown } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (error == null) return this.props.children;
    if (!chunkFailed(error)) throw error;
    return null;
  }
}
