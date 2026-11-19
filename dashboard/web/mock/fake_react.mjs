/* React's hooks, enough to run one hook outside a renderer: for check_live.mjs.
 *
 * Not a check itself, and nothing under `src/` imports it. check_live.mjs has node load
 * `src/api/useLive.ts` with `react` resolved here, so the hook's own code runs as written --
 * its state, refs, memos and effects -- while the test drives its socket and its clock.
 * `mount(hook, ...args)` renders it once and runs its effects; a state change marks it dirty,
 * and `flush()` renders it again, as often as it takes, running the effects whose deps moved.
 */
let cur = null;

function slot(make) {
  const i = cur.i++;
  if (!(i in cur.slots)) cur.slots[i] = make();
  return cur.slots[i];
}
const same = (a, b) => a !== undefined && b !== undefined && a.length === b.length && a.every((x, i) => Object.is(x, b[i]));

export function useState(init) {
  const h = cur;
  const s = slot(() => ({ v: typeof init === 'function' ? init() : init }));
  s.set ??= (v) => {
    const nv = typeof v === 'function' ? v(s.v) : v;
    if (!Object.is(nv, s.v)) {
      s.v = nv;
      h.dirty = true;
    }
  };
  return [s.v, s.set];
}
export const useRef = (init) => slot(() => ({ current: init }));
export function useMemo(f, deps) {
  const s = slot(() => ({ deps: undefined, v: undefined }));
  if (!same(s.deps, deps)) {
    s.v = f();
    s.deps = deps;
  }
  return s.v;
}
export const useCallback = (f, deps) => useMemo(() => f, deps);
export function useEffect(f, deps) {
  const s = slot(() => ({ deps: undefined, cleanup: null }));
  if (deps === undefined || !same(s.deps, deps)) {
    s.deps = deps;
    cur.pending.push([s, f]);
  }
}

/** The hook `fn` mounted with `args`: `value` is what it returned last. */
export function mount(fn, ...args) {
  const h = { slots: [], i: 0, pending: [], dirty: false, value: undefined };
  const render = () => {
    h.i = 0;
    h.pending = [];
    h.dirty = false;
    cur = h;
    try {
      h.value = fn(...args);
    } finally {
      cur = null;
    }
    for (const [s, f] of h.pending) {
      if (typeof s.cleanup === 'function') s.cleanup();
      s.cleanup = f() ?? null;
    }
  };
  render();
  return {
    get value() {
      return h.value;
    },
    flush() {
      for (let n = 0; h.dirty; n++) {
        if (n > 100) throw new Error('the hook renders without end');
        render();
      }
    },
    unmount() {
      for (const s of h.slots) if (s && typeof s.cleanup === 'function') s.cleanup();
    },
  };
}

export default { useState, useRef, useMemo, useCallback, useEffect };
