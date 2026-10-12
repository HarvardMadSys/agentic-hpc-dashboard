/* A command line in a table cell: one line, cut with an ellipsis where it runs
 * long, and the whole of it in the hover (a wide tooltip, `data-tip-wide`).
 * Command lines are already masked by the service (rc_dashboard/redact.py), so
 * the hover shows nothing the cell would not. */
import type { CSSProperties, ReactNode } from 'react';
import { DASH } from '../lib/format';

export function CmdCell({
  text,
  children,
  className = '',
  style,
}: {
  /** the whole command line: the hover, and the cell when there are no children */
  text: string | null | undefined;
  /** a styled rendering of the same text, e.g. the command name coloured */
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <td className={`cmd-cut mono ${className}`.trim()} style={style} data-tip={text || undefined} data-tip-wide>
      {children ?? (text || DASH)}
    </td>
  );
}
