/* A table header that sorts its table: an arrow shows the column in use. */
import type { ReactNode } from 'react';
import type { Sorter, SortDir } from '../lib/useSort';

export function SortTh<K extends string>({
  s,
  k,
  children,
  num,
  first = 'desc',
  tip,
  className,
  colSpan,
  rowSpan,
  style,
}: {
  s: Sorter<K>;
  k: K;
  children: ReactNode;
  /** a number column: right-aligned */
  num?: boolean;
  /** the direction of the first click: largest first for numbers, A to Z for text */
  first?: SortDir;
  tip?: string;
  className?: string;
  colSpan?: number;
  rowSpan?: number;
  style?: React.CSSProperties;
}) {
  const on = s.key === k;
  return (
    <th
      className={[num ? 'num' : '', 'sortable', on ? 'sorted' : '', className ?? ''].filter(Boolean).join(' ')}
      onClick={() => s.toggle(k, first)}
      aria-sort={on ? (s.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
      data-tip={tip}
      colSpan={colSpan}
      rowSpan={rowSpan}
      style={style}
    >
      {children}
      <span className="sort-arrow" aria-hidden="true">{on ? (s.dir === 'asc' ? '▲' : '▼') : '↕'}</span>
    </th>
  );
}
