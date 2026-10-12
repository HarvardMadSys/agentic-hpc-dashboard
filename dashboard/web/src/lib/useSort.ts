/* Click a column header to sort a table by it, again to flip the order (note 27).
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

export function useSort<K extends string>(initial: K | null = null, initialDir: SortDir = 'desc'): Sorter<K> {
  const [key, setKey] = useState<K | null>(initial);
  const [dir, setDir] = useState<SortDir>(initialDir);
  const toggle = (k: K, firstDir: SortDir = 'desc') => {
    if (k === key) setDir(dir === 'desc' ? 'asc' : 'desc');
    else {
      setKey(k);
      setDir(firstDir);
    }
  };
  const sort = <T,>(rows: T[], get: (row: T, k: K) => SortVal): T[] => {
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
  };
  return { key, dir, toggle, sort };
}
