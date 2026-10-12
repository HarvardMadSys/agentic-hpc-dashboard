/* A table of rows, grouped by user: one compact row per user, opened with a
 * click into that user's own rows.
 *
 * Tables that list processes, sessions, or submissions one per row run to
 * hundreds of rows, most of them one person's campaign; what a reader scans
 * for first is WHO, then what they ran. Users keep the order the rows came
 * in -- a ranked table stays ranked by each user's top row -- and the
 * subtable is the caller's own table without its user column. Every user is
 * listed, in a box that scrolls with its header held in view, rather than 15
 * at a time behind "show more".
 */
import { useState } from 'react';
import type { ReactNode } from 'react';
import { DASH, fint } from '../lib/format';

export interface GroupCol<T> {
  h: ReactNode;
  num?: boolean;
  tip?: string;
  cell: (rows: T[]) => ReactNode;
}

export function ByUser<T>({
  rows,
  user,
  cols,
  sub,
  noun,
  open: openFirst = false,
  scroll = true,
}: {
  rows: T[];
  user: (r: T) => string | null | undefined;
  /** summary columns for a user's rows, after the user and the count */
  cols: GroupCol<T>[];
  /** the caller's table, without its user column, for one user's rows */
  sub: (rows: T[]) => ReactNode;
  /** what one row is: "process", "session", "submission" */
  noun: string;
  /** open the first user's rows on arrival */
  open?: boolean;
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
  const isOpen = (u: string, i: number) => opened[u] ?? (openFirst && i === 0);
  if (!rows.length) return null;
  const nouns = noun.endsWith('s') ? `${noun}es` : `${noun}s`;
  const plural = (n: number) => `${fint(n)} ${n === 1 ? noun : nouns}`;
  return (
    <div className="byuser">
      <div className={scroll ? 'scroller vscroll' : 'scroller'}>
        <table className="roomy byuser-t">
          <thead>
            <tr>
              <th>user</th>
              <th className="num">{nouns}</th>
              {cols.map((c, i) => (
                <th key={i} className={c.num ? 'num' : undefined} data-tip={c.tip}>
                  {c.h}
                </th>
              ))}
            </tr>
          </thead>
          {groups.map(([u, rs], i) => {
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
