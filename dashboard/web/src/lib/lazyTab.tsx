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
import { createElement, lazy, memo, useState } from 'react';
import type { ComponentType, ReactNode } from 'react';
import { Boundary } from '../components/Boundary';

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
 *  the browser's module map keep it), and its tab says so (`TabBoundary`). */
export function preloadTabs(): void {
  for (const f of loads) f().catch(() => {});
}

/** A tab that throws while it is drawn says so in its place -- "this tab could not be drawn: <why>
 *  · reload" -- and the shell around it stays. Its code that would not load (a deploy replaced it
 *  under a page opened just before; reload fetches the new) is one such error; a value it could not
 *  draw is another, and once took the whole page down, as nothing above App caught it. */
export function TabBoundary({ children }: { children: ReactNode }) {
  return <Boundary what="this tab">{children}</Boundary>;
}
