/* The page-wide user and agent-type filter: two dropdowns beside the class chips.
 *
 * Unlike the class chips this is not a mask over numbers already on screen. One
 * person's quantiles, deduplication and trajectory chains cannot be un-mixed
 * from everyone's, so the service builds a filtered view the way it builds a
 * window, and the panels show their usual "reducing" state while it does.
 *
 * The choices are the unfiltered view's own counts (`facets`), so offering them
 * costs no read of their own. Only the busiest users are listed; anyone else
 * can be typed in, which is what the search bar's user field used to be for.
 */
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { Facets } from '../api/types';
import { AGENT_TYPE_LBL, CLS, CLS_COL, CLS_LBL } from '../lib/classes';
import { UNTAGGED } from '../lib/filters';
import type { Who } from '../lib/filters';
import { fint } from '../lib/format';
import './WhoFilter.css';

/** An agent type in words; the filter's `none` is the label table's untagged. */
export const atypeLbl = (t: string): string => AGENT_TYPE_LBL[t === UNTAGGED ? '' : t] ?? t;

/** Offered before the first unfiltered build has counted anything. */
const KNOWN_TYPES = Object.keys(AGENT_TYPE_LBL).map((t) => (t === '' ? UNTAGGED : t));

const MIX = [...CLS, 'unlabeled'];

/** One username routinely shows up as agent, human and IDE at once; the bar
 *  says which classes picking this person will narrow. */
function Mix({ by }: { by: Partial<Record<string, number>> }) {
  const on = MIX.filter((c) => (by[c] ?? 0) > 0);
  const tot = on.reduce((a, c) => a + (by[c] ?? 0), 0);
  if (!tot) return <span />;
  return (
    <span className="who-mix" data-tip={on.map((c) => `${CLS_LBL[c]} ${fint(by[c])}`).join(', ')}>
      {on.map((c) => (
        <i key={c} style={{ width: `${(100 * (by[c] ?? 0)) / tot}%`, background: CLS_COL[c] }} />
      ))}
    </span>
  );
}

function Drop({ label, n, tip, children }: { label: string; n: number; tip: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', esc);
    };
  }, [open]);
  return (
    <div className="who-drop" ref={ref}>
      <button
        type="button"
        className={`who-btn${n ? ' on' : ''}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        data-tip={open ? undefined : tip}
      >
        {label}
        {n > 0 && <b>{n}</b>}
        <span className="who-caret">▾</span>
      </button>
      {open && <div className="who-pop">{children}</div>}
    </div>
  );
}

function Opt({
  on,
  toggle,
  name,
  mono,
  by,
  n,
}: {
  on: boolean;
  toggle: () => void;
  name: string;
  mono?: boolean;
  by?: Partial<Record<string, number>>;
  n?: number | null;
}) {
  return (
    <label className="who-opt">
      <input type="checkbox" checked={on} onChange={toggle} />
      <span className={`who-name${mono ? ' mono' : ''}`} title={name}>
        {name}
      </span>
      {by ? <Mix by={by} /> : <span />}
      <span className="who-n">{n != null ? fint(n) : ''}</span>
    </label>
  );
}

const flip = (xs: string[], x: string) => (xs.includes(x) ? xs.filter((y) => y !== x) : [...xs, x]);

export function WhoFilter({
  who,
  facets,
  onChange,
}: {
  who: Who;
  /** The unfiltered view's counts; null until a build has made them. */
  facets: Facets | null;
  onChange: (w: Who) => void;
}) {
  const [q, setQ] = useState('');
  const ql = q.trim().toLowerCase();
  const listed = facets?.users ?? [];
  // Picked users stay visible even when they are not among the busiest listed.
  const extra = who.users.filter((u) => !listed.some((r) => r.user === u));
  const rows = listed.filter((r) => !ql || r.user.toLowerCase().includes(ql));
  const typed = q.trim();
  const canAdd = typed !== '' && !listed.some((r) => r.user === typed) && !who.users.includes(typed);
  const more = facets ? facets.users_total - facets.users_shown : 0;

  const types = facets?.agent_types?.length
    ? facets.agent_types
    : KNOWN_TYPES.map((t) => ({ agent_type: t, events: null, by_class: undefined }));
  const typeExtra = who.atypes.filter((t) => !types.some((r) => r.agent_type === t));

  const setUsers = (users: string[]) => onChange({ ...who, users });
  const setTypes = (atypes: string[]) => onChange({ ...who, atypes });
  const active = who.users.length + who.atypes.length > 0;

  return (
    <>
      <Drop label="user" n={who.users.length} tip="show only these people's processes, on every tab">
        <input
          className="who-find"
          autoFocus
          placeholder="find or type a user"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || !typed) return;
            e.preventDefault();
            if (!who.users.includes(typed)) setUsers([...who.users, typed]);
            setQ('');
          }}
        />
        <div className="who-list" role="listbox" aria-multiselectable>
          {canAdd && (
            <Opt on={false} toggle={() => (setUsers([...who.users, typed]), setQ(''))} name={typed} mono />
          )}
          {extra.map((u) => (
            <Opt key={u} on toggle={() => setUsers(flip(who.users, u))} name={u} mono />
          ))}
          {rows.map((r) => (
            <Opt
              key={r.user}
              on={who.users.includes(r.user)}
              toggle={() => setUsers(flip(who.users, r.user))}
              name={r.user}
              mono
              by={r.by_class}
              n={r.events}
            />
          ))}
          {!facets && !extra.length && !canAdd && (
            <div className="who-more">counting users in this window…</div>
          )}
        </div>
        {more > 0 && !ql && <div className="who-more">{fint(more)} more · type a name</div>}
      </Drop>
      <Drop label="agent type" n={who.atypes.length} tip="show only processes of these agents or editors, on every tab">
        <div className="who-list" role="listbox" aria-multiselectable>
          {[...types, ...typeExtra.map((t) => ({ agent_type: t, events: null, by_class: undefined }))].map((r) => (
            <Opt
              key={r.agent_type}
              on={who.atypes.includes(r.agent_type)}
              toggle={() => setTypes(flip(who.atypes, r.agent_type))}
              name={atypeLbl(r.agent_type)}
              by={r.by_class}
              n={r.events}
            />
          ))}
        </div>
      </Drop>
      {active && (
        <span className="who-sum">
          {who.users.map((u) => (
            <button key={'u' + u} className="who-tag mono" onClick={() => setUsers(who.users.filter((x) => x !== u))} data-tip={`stop filtering to ${u}`}>
              {u}
              <span className="x">×</span>
            </button>
          ))}
          {who.atypes.map((t) => (
            <button
              key={'t' + t}
              className="who-tag"
              onClick={() => setTypes(who.atypes.filter((x) => x !== t))}
              data-tip={`stop filtering to ${atypeLbl(t)}`}
            >
              {atypeLbl(t)}
              <span className="x">×</span>
            </button>
          ))}
          <button className="btn" onClick={() => onChange({ users: [], atypes: [] })}>
            clear
          </button>
        </span>
      )}
    </>
  );
}
