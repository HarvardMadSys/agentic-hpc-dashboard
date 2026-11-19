/* Each class's share of a quantity: out of every class the service counted.
 *
 * The class chips hide a class; they never change another's share. A share is
 * out of EVERY class the panel counted -- agent, human, IDE, and `unlabeled`,
 * which is no class of its own but is part of the whole -- or turning the IDE
 * off would hand its share to the other two, and with only agent on every row
 * would read 100%. Live's charts made the rule first (LiveCharts.tsx).
 *
 * A class the panel counted whose value is null (not measured, which is not
 * zero) leaves the whole unknown: a sum without it is not the whole, so there
 * is no share, a dash, and `unknown` says who is missing. A class the panel
 * did not count at all is not in `all`: nothing of it was seen, which is zero.
 */
import { CLS_LBL } from './classes';

export interface Whole {
  /** the sum over every class whose value is known; null when none is */
  tot: number | null;
  /** the classes counted whose value is not known (null): not measured */
  unknown: string[];
}

/** The whole a share is out of: `get` of every class in `all` -- the keys of
 *  the by-class map the quantity comes from, whatever the filter shows. */
export function wholeOf(all: Iterable<string>, get: (c: string) => number | null | undefined): Whole {
  let tot: number | null = null;
  const unknown: string[] = [];
  for (const c of all) {
    const v = get(c);
    if (v == null || !Number.isFinite(v)) unknown.push(c);
    else tot = (tot ?? 0) + v;
  }
  return { tot, unknown };
}

/** `v` as a percentage of `w`: null -- a dash -- when `v` is not known, when a
 *  class of the whole is not, or when the whole is nothing. */
export function shareOf(v: number | null | undefined, w: Whole): number | null {
  if (v == null || !Number.isFinite(v) || w.unknown.length || !w.tot) return null;
  return (100 * v) / w.tot;
}

/** Every class the by-class maps hold, in the order first seen: the classes a
 *  quantity drawn from more than one panel counted. */
export function countedIn(...maps: (Record<string, unknown> | null | undefined)[]): string[] {
  return [...new Set(maps.flatMap((m) => (m ? Object.keys(m) : [])))];
}

/** Why a share is a dash, in words: "no share: IDE not measured". Null when
 *  the whole is known. */
export function noShare(w: Whole): string | null {
  if (!w.unknown.length) return null;
  const names = w.unknown.map((c) => CLS_LBL[c] ?? c);
  const list = names.length < 3 ? names.join(' and ') : `${names.slice(0, -1).join(', ')}, and ${names[names.length - 1]}`;
  return `no share: ${list} not measured, and a whole without ${names.length === 1 ? 'it' : 'them'} is not the whole`;
}
