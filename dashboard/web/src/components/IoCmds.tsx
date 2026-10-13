/* Every command behind an I/O number: a drawer at the right of the page.
 *
 * Any I/O figure on the page is an `IoNum`; clicking one opens this with the
 * number's filters (lib/iocmds.ts). The service reads the window's records
 * again (rc_dashboard/iocmds.py), which takes seconds on a live node and up to
 * a minute for a day of a big capture, so the drawer says how far it has read.
 * Then it lists every command line that ran, each once, with what all of its
 * processes moved -- sorted by the quantity that was clicked, a page at a time
 * so a day's hundreds of thousands of command lines never arrive at once. */
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { getIoCmds, startIoCmds } from '../api/client';
import type { Cls, IoCmdJob, IoCmdRow, IoSort } from '../api/types';
import { closeIoCmds, openIoCmds, useIoCmds } from '../lib/iocmds';
import type { IoQuery } from '../lib/iocmds';
import type { Who } from '../lib/filters';
import { CLS, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, mb } from '../lib/format';
import type { Sorter, SortDir } from '../lib/useSort';
import { CmdCell } from './Cmd';
import { SortTh } from './SortTh';
import './IoCmds.css';

const PAGE = 200;
const POLL_MS = 600;
/** "show all" is offered up to this many more rows; past it, a page of 1000 at a time */
const ALL_MAX = 20000;

/** An I/O figure that opens the commands behind it. */
export function IoNum({ q, children, className }: { q: IoQuery; children: ReactNode; className?: string }) {
  return (
    <button
      type="button"
      className={`ionum ${className ?? ''}`.trim()}
      onClick={(e) => {
        e.stopPropagation();
        openIoCmds(q);
      }}
      data-tip="click: every command behind this number"
    >
      {children}
    </button>
  );
}

/** The lookup's query string: the number's filters, then the page's where the
 *  number names none of its own. */
function params(q: IoQuery, win: number | null, who: Who, cls: Cls[]): URLSearchParams {
  const p = new URLSearchParams();
  const many = (k: string, v: (string | number)[] | undefined) => v?.forEach((x) => p.append(k, String(x)));
  if (win != null) p.set('window_min', String(win));
  many('class', q.class ?? (q.unfiltered || cls.length >= CLS.length ? undefined : cls));
  many('user', q.user ?? (q.unfiltered ? undefined : who.users));
  many('agent_type', q.agent_type ?? (q.unfiltered ? undefined : who.atypes));
  many('tool', q.tool);
  many('bucket', q.bucket);
  if (q.host) p.set('host', q.host);
  if (q.apid?.length) p.set('apid', q.apid.join(','));
  if (q.cwd) p.set('cwd', q.cwd);
  if (q.from != null) p.set('from', String(q.from));
  if (q.to != null) p.set('to', String(q.to));
  return p;
}

/** What the lookup was narrowed to, in words, for the drawer's head. */
function narrowed(q: IoQuery, who: Who, cls: Cls[]): string[] {
  const out: string[] = [];
  const c = q.class ?? (q.unfiltered || cls.length >= CLS.length ? undefined : cls);
  if (c?.length) out.push(c.map((x) => CLS_LBL[x as Cls] ?? x).join(', '));
  const u = q.user ?? (q.unfiltered ? undefined : who.users);
  if (u?.length) out.push(u.join(', '));
  const a = q.agent_type ?? (q.unfiltered ? undefined : who.atypes);
  if (a?.length) out.push(a.join(', '));
  if (q.tool?.length) out.push(`tool ${q.tool.join(', ')}`);
  if (q.bucket?.length) out.push(`kind ${q.bucket.join(', ')}`);
  if (q.apid?.length) out.push(`agent pid ${q.apid.length > 3 ? `${q.apid[0]} and ${q.apid.length - 1} subagents` : q.apid.join(', ')}${q.host ? ` on ${q.host}` : ''}`);
  if (q.cwd) out.push(`in ${q.cwd}`);
  return out;
}

/** Is column `c` what the list is sorted by? Disk and network sums light both of theirs. */
const lit = (c: IoSort, by: IoSort) =>
  c === by || (by === 'disk_mb' && (c === 'rd_mb' || c === 'wr_mb')) || (by === 'net_mb' && (c === 'tx_mb' || c === 'rx_mb'));

const COLS: { k: IoSort; lbl: string; tip: string }[] = [
  { k: 'rd_mb', lbl: 'disk read', tip: 'bytes the processes read from disk (read_bytes)' },
  { k: 'wr_mb', lbl: 'disk written', tip: 'bytes the processes wrote to disk (write_bytes)' },
  { k: 'rchar_mb', lbl: 'syscall read', tip: 'bytes read through system calls, pipes and terminals included (rchar)' },
  { k: 'wchar_mb', lbl: 'syscall written', tip: 'bytes written through system calls, pipes and terminals included (wchar)' },
  { k: 'tx_mb', lbl: 'sent', tip: 'network bytes sent' },
  { k: 'rx_mb', lbl: 'received', tip: 'network bytes received' },
];

export function IoCmdsDrawer({ win, who, cls }: { win: number | null; who: Who; cls: Cls[] }) {
  const q = useIoCmds();
  const [job, setJob] = useState<IoCmdJob | null>(null);
  const [rows, setRows] = useState<IoCmdRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: IoSort; dir: SortDir }>({ key: 'rd_mb', dir: 'desc' });
  const [more, setMore] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  const sortRef = useRef(sort);
  sortRef.current = sort;
  const qs = q ? params(q, win, who, cls).toString() : '';

  // a new number: its own sort, and a fresh lookup
  useEffect(() => {
    if (q) setSort({ key: q.sort, dir: 'desc' });
  }, [q]);

  useEffect(() => {
    if (!q) return;
    const ac = new AbortController();
    setJob(null);
    setRows([]);
    setErr(null);
    (async () => {
      try {
        let j = await startIoCmds(new URLSearchParams(qs), ac.signal);
        setJob(j);
        while (j.state !== 'done' && j.state !== 'error') {
          await new Promise((r) => setTimeout(r, POLL_MS));
          if (ac.signal.aborted) return;
          j = await getIoCmds(j.id, { offset: 0, limit: PAGE, sort: sort.key, dir: sort.dir }, ac.signal);
          setJob(j);
        }
        if (j.state === 'done') {
          // the first page, in the order the reader has by now
          const first = await getIoCmds(j.id, { offset: 0, limit: PAGE, sort: sortRef.current.key, dir: sortRef.current.dir }, ac.signal);
          setJob(first);
          setRows(first.rows ?? []);
        }
      } catch (e) {
        if (!ac.signal.aborted) setErr(String(e));
      }
    })();
    return () => ac.abort();
    // `sort` is read through sortRef once the lookup is done
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, qs]);

  // a header click re-sorts on the service: the whole list, not the page held
  const resort = async (key: IoSort, dir: SortDir) => {
    setSort({ key, dir });
    if (job?.state !== 'done') return;
    try {
      const j = await getIoCmds(job.id, { offset: 0, limit: PAGE, sort: key, dir });
      setJob(j);
      setRows(j.rows ?? []);
      panel.current?.querySelector('.iocmd-scroll')?.scrollTo({ top: 0 });
    } catch (e) {
      setErr(String(e));
    }
  };
  const loadMore = async (all: boolean) => {
    if (job?.state !== 'done') return;
    setMore(true);
    try {
      const total = job.commands ?? 0;
      let have = rows.slice();
      do {
        const j = await getIoCmds(job.id, { offset: have.length, limit: 1000, sort: sort.key, dir: sort.dir });
        have = have.concat(j.rows ?? []);
        if (!(j.rows ?? []).length) break;
        if (!all) break;
      } while (have.length < total);
      setRows(have);
    } catch (e) {
      setErr(String(e));
    } finally {
      setMore(false);
    }
  };

  useEffect(() => {
    if (!q) return;
    const k = (e: KeyboardEvent) => e.key === 'Escape' && closeIoCmds();
    window.addEventListener('keydown', k);
    panel.current?.focus();
    return () => window.removeEventListener('keydown', k);
  }, [q]);

  if (!q) return null;

  // the table's headers sort on the service; the third click is the clicked quantity again
  const s: Sorter<IoSort> = {
    key: sort.key,
    dir: sort.dir,
    toggle: (k, first = 'desc') => {
      if (k !== sort.key) resort(k, first);
      else if (sort.dir === first) resort(k, first === 'desc' ? 'asc' : 'desc');
      else resort(q.sort, 'desc');
    },
    sort: (r) => r,
  };
  const t = job?.totals;
  const reading = job && (job.state === 'queued' || job.state === 'reading');
  const pct = job && job.total_bytes ? Math.min(100, (100 * job.bytes) / job.total_bytes) : 0;
  const nar = narrowed(q, who, cls);
  const total = job?.commands ?? 0;

  return (
    <div className="iocmd-wrap" onClick={() => closeIoCmds()}>
      <div
        className="iocmd"
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-label={`every command behind ${q.title}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="iocmd-head">
          <div className="iocmd-title">
            <h3>Every command behind {q.title}</h3>
            <button className="linkbtn" onClick={() => closeIoCmds()} data-tip="or press Esc">close</button>
          </div>
          <div className="iocmd-sub nm">
            {nar.length ? nar.join(' · ') : 'every process'}
            {job?.from ? ` · ${job.from.slice(5, 16)} to ${job.to?.slice(5, 16) ?? 'now'}` : ''}
          </div>
          {reading && (
            <div className="iocmd-prog">
              <div className="iocmd-bar"><i style={{ width: `${pct}%` }} /></div>
              <span className="nm">
                {job.state === 'queued' ? 'waiting for another lookup to finish' : 'reading the window again'}
                {' · '}
                {mb(job.bytes / 1e6)} of {mb(job.total_bytes / 1e6)} · {fint(job.matched)} processes so far · {dur(job.elapsed_s)}
              </span>
            </div>
          )}
          {!job && !err && <div className="nm iocmd-prog">starting…</div>}
          {(err || job?.state === 'error') && (
            <div className="iocmd-err">could not list them: {err ?? job?.error}</div>
          )}
          {job?.state === 'done' && t && (
            <div className="iocmd-tot">
              <span><b>{fint(total)}</b> command line{total === 1 ? '' : 's'}</span>
              <span><b>{fint(t.n)}</b> processes</span>
              {COLS.map((c) => (
                <span key={c.k} className={lit(c.k, q.sort) ? 'on' : undefined}>
                  {c.lbl} <b>{mb(t[c.k as keyof typeof t] as number)}</b>
                </span>
              ))}
              <span className="nm" data-tip="a process's bytes are on its exit record; one with none was not measured, not idle">
                {t.io_n < t.n ? `${fint(t.n - t.io_n)} processes carried no I/O record` : null}
                {` · read in ${dur(job.elapsed_s)}`}
              </span>
              {job.commands_over ? (
                <span className="nm">{fint(job.commands_over)} processes past the service's hold of command lines: counted above, not listed</span>
              ) : null}
            </div>
          )}
        </div>

        {job?.state === 'done' && (
          <div className="iocmd-scroll">
            {rows.length ? (
              <table className="iocmd-t">
                <thead>
                  <tr>
                    <SortTh s={s} k="cmd" first="asc">command</SortTh>
                    <SortTh s={s} k="n" num tip="processes that ran this command line">processes</SortTh>
                    {COLS.map((c) => (
                      <SortTh key={c.k} s={s} k={c.k} num tip={c.tip}>{c.lbl}</SortTh>
                    ))}
                    <SortTh s={s} k="last" tip="when the last of them exited">last</SortTh>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={i}>
                      <CmdCell text={r.cmd + (r.users.length ? `\n\nran by ${r.users.join(', ')}` : '') + (r.cut ? '\n(cut: the command line is longer)' : '')}>
                        {r.cmd}
                      </CmdCell>
                      <td className="num">{fint(r.n)}</td>
                      {COLS.map((c) => {
                        const v = r[c.k as keyof IoCmdRow] as number;
                        return (
                          <td key={c.k} className={`num${lit(c.k, sort.key) ? ' on' : ''}${v ? '' : ' zero'}`}>
                            {v ? mb(v) : DASH}
                          </td>
                        );
                      })}
                      <td className="mono nm iocmd-when">{r.last ? r.last.slice(5, 16) : DASH}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="nm iocmd-none">no process in this window matches: nothing ran here, or nothing it ran has exited yet</div>
            )}
            {rows.length < total && (
              <div className="iocmd-more">
                <span className="nm">{fint(rows.length)} of {fint(total)} shown</span>
                <button className="ws-more" disabled={more} onClick={() => loadMore(false)}>show {fint(Math.min(1000, total - rows.length))} more</button>
                {/* past this many rows a page stops answering: the rest are a sort away */}
                {total - rows.length <= ALL_MAX && (
                  <button className="linkbtn" disabled={more} onClick={() => loadMore(true)}>show all {fint(total)}</button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
