/* Click a column header to sort a table by it, again to flip the order, and a
 * third time to put the table back in its own order (note 27).
 *
 * Numbers sort largest first on the first click, text A to Z; a missing value
 * always sorts last in either direction, because "not measured" is not the
 * smallest measurement. A table with no sort chosen keeps the service's order.
 */
import { useState } from 'react';

export type SortDir = 'asc' | 'desc';
export type SortVal = number | string | null | undefined;

export interface Sorter<K extends string> {
  key: K | null;
  dir: SortDir;
  toggle: (k: K, firstDir?: SortDir) => void;
  sort: <T>(rows: T[], get: (row: T, k: K) => SortVal) => T[];
}

/** `rows` in the order `key` and `dir` give; the order they came in for no key. */
function sortRows<T, K extends string>(rows: T[], get: (row: T, k: K) => SortVal, key: K | null, dir: SortDir): T[] {
  if (key == null) return rows;
  const sign = dir === 'asc' ? 1 : -1;
  return rows
    .map((r, i) => ({ r, i, v: get(r, key) }))
    .sort((a, b) => {
      const am = a.v == null || a.v === '' || (typeof a.v === 'number' && Number.isNaN(a.v));
      const bm = b.v == null || b.v === '' || (typeof b.v === 'number' && Number.isNaN(b.v));
      if (am || bm) return am && bm ? a.i - b.i : am ? 1 : -1;
      const c = typeof a.v === 'number' && typeof b.v === 'number'
        ? a.v - b.v
        : String(a.v).localeCompare(String(b.v), undefined, { numeric: true });
      return c ? sign * c : a.i - b.i;
    })
    .map((x) => x.r);
}

export function useSort<K extends string>(initial: K | null = null, initialDir: SortDir = 'desc'): Sorter<K> {
  const [key, setKey] = useState<K | null>(initial);
  const [dir, setDir] = useState<SortDir>(initialDir);
  const toggle = (k: K, firstDir: SortDir = 'desc') => {
    if (k !== key) {
      setKey(k);
      setDir(firstDir);
    } else if (dir === firstDir) {
      setDir(firstDir === 'desc' ? 'asc' : 'desc');
    } else {
      // the third click: the table's own order again
      setKey(initial);
      setDir(initialDir);
    }
  };
  const sort = <T,>(rows: T[], get: (row: T, k: K) => SortVal): T[] => sortRows(rows, get, key, dir);
  return { key, dir, toggle, sort };
}

/** One sort per table, by an id, from a single hook: for a component that
 * draws its tables in a loop or a branch, where a hook per table cannot go. */
export function useSortMap<K extends string = string>(): (id: string) => Sorter<K> {
  const [st, setSt] = useState<Record<string, { key: K | null; dir: SortDir }>>({});
  return (id) => {
    const cur = st[id] ?? { key: null, dir: 'desc' as SortDir };
    return {
      key: cur.key,
      dir: cur.dir,
      toggle: (k: K, firstDir: SortDir = 'desc') =>
        setSt((m) => {
          const c = m[id] ?? { key: null, dir: 'desc' as SortDir };
          if (k !== c.key) return { ...m, [id]: { key: k, dir: firstDir } };
          if (c.dir === firstDir) return { ...m, [id]: { key: k, dir: firstDir === 'desc' ? 'asc' : 'desc' } };
          // the third click: the table's own order again
          const rest = { ...m };
          delete rest[id];
          return rest;
        }),
      sort: <T,>(rows: T[], get: (row: T, k: K) => SortVal): T[] => sortRows(rows, get, cur.key, cur.dir),
    };
  };
}
