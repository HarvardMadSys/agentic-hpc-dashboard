/* A table of rows, grouped by user: one compact row per user, opened with a
 * click into that user's own rows -- or, switched off, every row on its own
 * with its user in a column.
 *
 * Tables that list processes, sessions, or submissions one per row run to
 * hundreds of rows, most of them one person's campaign; what a reader scans
 * for first is WHO, then what they ran. Users open A to Z, whatever order
 * the rows came in, and the subtable is the caller's own table without its
 * user column. Every user is
 * listed, in a box that scrolls with its header held in view, rather than 15
 * at a time behind "show more".
 */
import { useState } from 'react';
import type { ReactNode } from 'react';
import { DASH, fint } from '../lib/format';
import { Segmented } from './ClassFilter';
import { SortTh } from './SortTh';
import { useSort } from '../lib/useSort';
import type { SortVal } from '../lib/useSort';

export interface GroupCol<T> {
  h: ReactNode;
  num?: boolean;
  tip?: string;
  cell: (rows: T[]) => ReactNode;
  /** what the column sorts by; a column without it does not sort */
  sort?: (rows: T[]) => SortVal;
}

export function ByUser<T>({
  rows,
  user,
  cols,
  sub,
  noun,
  open: openFirst = false,
  openUser = null,
  scroll = true,
}: {
  rows: T[];
  user: (r: T) => string | null | undefined;
  /** summary columns for a user's rows, after the user and the count */
  cols: GroupCol<T>[];
  /** the caller's table for one user's rows, without its user column; with
   *  it (`withUser`) for every row at once, when the grouping is off */
  sub: (rows: T[], withUser?: boolean) => ReactNode;
  /** what one row is: "process", "session", "submission" */
  noun: string;
  /** open the first user's rows on arrival */
  open?: boolean;
  /** open this user's rows, first in the list (a link from Active users) */
  openUser?: string | null;
  /** scroll in a box of its own; false when the caller's container scrolls */
  scroll?: boolean;
}) {
  const groups: [string, T[]][] = [];
  const at = new Map<string, number>();
  for (const r of rows) {
    const u = user(r) || DASH;
    let i = at.get(u);
    if (i == null) {
      i = groups.length;
      at.set(u, i);
      groups.push([u, []]);
    }
    groups[i][1].push(r);
  }
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  // each table its own switch: one flattened leaves the others grouped
  const [grouped, setGrouped] = useState(true);
  // the users sort by name, by how many rows they have, or by a column
  const s = useSort<string>('user', 'asc');
  const shown = s.sort(groups, ([u, rs], k) =>
    k === 'user' ? u : k === 'n' ? rs.length : cols[+k.slice(1)]?.sort?.(rs));
  // a user opened by a link leads the table's own order
  if (openUser && s.key === 'user' && s.dir === 'asc') {
    const i = shown.findIndex(([u]) => u === openUser);
    if (i > 0) shown.unshift(...shown.splice(i, 1));
  }
  const isOpen = (u: string, i: number) =>
    opened[u] ?? (openUser ? u === openUser : openFirst && i === 0);
  if (!rows.length) return null;
  const nouns = noun.endsWith('s') ? `${noun}es` : `${noun}s`;
  const plural = (n: number) => `${fint(n)} ${n === 1 ? noun : nouns}`;
  return (
    <div className="byuser">
      <div className="u-ctl">
        <Segmented<'user' | 'flat'>
          label="rows"
          value={grouped ? 'user' : 'flat'}
          onChange={(v) => setGrouped(v === 'user')}
          options={[
            { k: 'user', lbl: 'by user', tip: `one row per user, opened into their ${nouns}` },
            { k: 'flat', lbl: `every ${noun}`, tip: `every ${noun} on a row of its own, with its user` },
          ]}
        />
      </div>
      {grouped ? (
        <div className={scroll ? 'scroller vscroll' : 'scroller'}>
          <table className="roomy byuser-t">
            <thead>
              <tr>
                <SortTh s={s} k="user" first="asc">user</SortTh>
                <SortTh s={s} k="n" num>{nouns}</SortTh>
                {cols.map((c, i) =>
                  c.sort ? (
                    <SortTh key={i} s={s} k={`c${i}`} num={c.num} tip={c.tip}
                      first={c.num ? 'desc' : 'asc'}>
                      {c.h}
                    </SortTh>
                  ) : (
                    <th key={i} className={c.num ? 'num' : undefined} data-tip={c.tip}>
                      {c.h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            {shown.map(([u, rs], i) => {
              const o = isOpen(u, i);
              return (
                <tbody key={u}>
                  <tr className={`byuser-row${o ? ' open' : ''}`} onClick={() => setOpened((m) => ({ ...m, [u]: !o }))}>
                    <td>
                      <span className="u-caret">{o ? '▾' : '▸'}</span>
                      <span className="mono u-name">{u}</span>
                    </td>
                    <td className="num">{fint(rs.length)}</td>
                    {cols.map((c, j) => (
                      <td key={j} className={c.num ? 'num' : undefined}>
                        {c.cell(rs)}
                      </td>
                    ))}
                  </tr>
                  {o && (
                    <tr className="byuser-sub">
                      <td colSpan={cols.length + 2}>{sub(rs)}</td>
                    </tr>
                  )}
                </tbody>
              );
            })}
          </table>
        </div>
      ) : (
        <div className={scroll ? 'scroller vscroll' : 'scroller'}>{sub(rows, true)}</div>
      )}
      {groups.length > 1 && (
        <div className="u-foot">
          <span className="nm">{`${fint(groups.length)} users · ${plural(rows.length)}`}</span>
        </div>
      )}
    </div>
  );
}

/** "bash, curl +2": the distinct values of a field across a user's rows. */
export function distinct<T>(rows: T[], get: (r: T) => string | null | undefined, max = 3): string {
  const seen: string[] = [];
  for (const r of rows) {
    const v = get(r);
    if (v && !seen.includes(v)) seen.push(v);
  }
  if (!seen.length) return DASH;
  return seen.slice(0, max).join(', ') + (seen.length > max ? ` +${seen.length - max}` : '');
}
