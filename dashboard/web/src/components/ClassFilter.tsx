/* The class filter, shared across every tab.
 *
 * Three classes, always offered as three. At least one is always selected --
 * an empty selection would render an all-zero dashboard that reads as "no
 * activity on the cluster", so the last selected chip cannot be turned off.
 */
import { CLS, CLS_COL, CLS_LBL } from '../lib/classes';
import type { Cls } from '../api/types';

export function ClassFilter({
  value,
  onChange,
  extra,
}: {
  value: Cls[];
  onChange: (v: Cls[]) => void;
  extra?: React.ReactNode;
}) {
  const on = new Set(value);
  const toggle = (c: Cls) => {
    if (on.has(c)) {
      if (on.size > 1) onChange(value.filter((x) => x !== c));
      return;
    }
    onChange(CLS.filter((x) => on.has(x) || x === c));
  };
  return (
    <div className="filters">
      <span className="eyebrow">class</span>
      {CLS.map((c) => {
        const sel = on.has(c);
        return (
          <button
            key={c}
            className="chip"
            aria-pressed={sel}
            disabled={sel && on.size === 1}
            onClick={() => toggle(c)}
            data-tip={
              sel && on.size === 1
                ? 'at least one class must stay selected'
                : `${sel ? 'hide' : 'show'} ${CLS_LBL[c]}`
            }
          >
            <span className="sw" style={{ background: CLS_COL[c] }} />
            {CLS_LBL[c]}
          </button>
        );
      })}
      {extra}
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { k: T; lbl: string; tip?: string }[];
  onChange: (v: T) => void;
  label?: string;
}) {
  return (
    <div className="wrapctl" style={{ gap: 7 }}>
      {label && <span className="eyebrow">{label}</span>}
      <div className="seg">
        {options.map((o) => (
          <button
            key={o.k}
            aria-pressed={o.k === value}
            onClick={() => onChange(o.k)}
            data-tip={o.tip}
          >
            {o.lbl}
          </button>
        ))}
      </div>
    </div>
  );
}
