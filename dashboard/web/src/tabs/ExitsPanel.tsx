/* Non-zero exits -- how the tool mix's processes ended (`tool_mix.exits`).
 *
 * The same processes the Tool mix counts, after its shell dedup, read for the
 * one thing it leaves out: whether each one worked. A failure here is a
 * non-zero exit or a stop by a signal, with two things kept out because they
 * are not failures: an exit 1 from a program whose 1 is an answer (grep found
 * no line, diff found a difference), and SIGPIPE, which `cmd | head` sends
 * `cmd` once head has its lines. Each count opens the command lines behind it.
 *
 * A rate is over the processes with a verdict: a record without one (no wait
 * status) is neither a success nor a failure, and is counted apart.
 */
import { Fragment, useState } from 'react';
import type { Cls, ExitBlock, ExitTally, ToolMix } from '../api/types';
import { Segmented } from '../components/ClassFilter';
import { IoNum } from '../components/IoCmds';
import { NoRows, Panel } from '../components/Panel';
import { ShareCell } from '../components/ShareCell';
import { SortTh } from '../components/SortTh';
import { Term } from '../components/Term';
import { AGENT_TYPE_LBL, CLS_COL, CLS_INK, CLS_LBL } from '../lib/classes';
import { DASH, fint } from '../lib/format';
import { coveredBy } from '../lib/iocmds';
import type { IoQuery } from '../lib/iocmds';
import { pick, shown } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { useSort } from '../lib/useSort';

/** What an exit code usually means, where it has a convention. */
const CODE_WORDS: Record<number, string> = {
  1: 'a general failure',
  2: 'misused, or a usage error',
  124: 'timed out (timeout)',
  125: 'timeout or a container runtime itself failed',
  126: 'found, but could not be run',
  127: 'command not found',
};
/** Signals by number, as Linux numbers them. */
const SIG_NAME: Record<number, string> = {
  1: 'SIGHUP', 2: 'SIGINT', 3: 'SIGQUIT', 4: 'SIGILL', 6: 'SIGABRT', 7: 'SIGBUS', 8: 'SIGFPE',
  9: 'SIGKILL', 11: 'SIGSEGV', 13: 'SIGPIPE', 14: 'SIGALRM', 15: 'SIGTERM', 24: 'SIGXCPU', 25: 'SIGXFSZ',
};
const SIG_WORDS: Record<number, string> = {
  1: 'its terminal went away',
  2: 'interrupted (Ctrl-C)',
  6: 'aborted itself',
  9: 'killed outright: the out-of-memory killer, a timeout, scancel, or kill -9',
  11: 'crashed: a bad memory access',
  15: 'asked to stop',
  24: 'ran past its CPU-time limit',
};
const codeTip = (c: number) =>
  CODE_WORDS[c] ?? (c > 128 && c < 160 ? `a shell's exit after its command got signal ${c - 128} (${SIG_NAME[c - 128] ?? '?'})` : undefined);

/** failed: a non-zero exit (past the answers) or a stop by a signal (past SIGPIPE) */
const failed = (t: ExitTally) => t.nonzero + t.signal;
/** the processes a rate is over: those whose record says how they ended */
const rated = (t: ExitTally) => t.calls - t.none;
const rate = (t: ExitTally) => (rated(t) > 0 ? (100 * failed(t)) / rated(t) : null);

const WHAT_COUNTS =
  'failed: exited non-zero or stopped by a signal, except an exit 1 from grep, rg, ag, diff or cmp ' +
  '(their answer) and SIGPIPE; a share of the processes whose ending is known';

type By = 'tool' | 'kind';

export function ExitsPanel({ P, cls }: { P: Panels; cls: Cls[] }) {
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const ex = tm.exits;
  const by = ex?.by_class ?? {};
  const have = shown(by, cls);
  const [view, setView] = useState<By>('tool');
  // rows that name things open A to Z, the home sort; ranking is the headers'
  const s = useSort<string>('k', 'asc');
  const types = Object.entries(ex?.by_agent_type ?? {})
    .filter(([, b]) => b.calls > 0)
    .sort((a, b) => b[1].calls - a[1].calls);

  // the rows: every tool (or kind) listed for any shown class, each with every class's counts
  const rowsOf = (b: ExitBlock | undefined) => (view === 'tool' ? b?.tools ?? [] : b?.buckets ?? []);
  const keyOf = (r: { tool?: string; b?: string }) => (view === 'tool' ? r.tool! : r.b!);
  const cell = new Map<string, Map<string, ExitTally>>();
  for (const c of have) {
    for (const r of rowsOf(by[c])) {
      const k = keyOf(r as { tool?: string; b?: string });
      if (!cell.has(k)) cell.set(k, new Map());
      cell.get(k)!.set(c, r);
    }
  }
  // a kind of tool every class ran without a failure says nothing here
  const keys = Array.from(cell.keys()).filter((k) => have.some((c) => failed(cell.get(k)!.get(c) ?? ZERO) > 0));
  const ordered = s.sort(keys, (k, col) => {
    if (col === 'k') return k;
    const t = cell.get(k)!.get(col.slice(2));
    return t ? rate(t) : null;
  });

  const drill = (c: string, k: string): IoQuery => ({
    title: `failures · ${k} · ${CLS_LBL[c] ?? c}`,
    sort: 'n',
    class: [c],
    outcome: ['nonzero', 'signal'],
    ...(view === 'tool' ? { tool: [k] } : { bucket: [k] }),
    ...coveredBy(tm),
  });

  return (
    <Panel
      title="Commands that failed"
      id="sec-commands-that-failed"
      ask={[{ panel: 'tool_mix', keys: ['exits'] }]}
      p={tm}
      span
      empty={!ex || !have.length}
      emptyWhat={ex ? 'no process of the selected classes in the window' : 'this window was reduced before events were counted: it is counted at its next build'}
      header={
        <span className="wrapctl" style={{ gap: 6 }}>
          <Term tip={WHAT_COUNTS}><span className="nm">what counts</span></Term>
          <Segmented value={view} onChange={setView} options={[
            { k: 'tool', lbl: 'by tool', tip: 'the tools with the most failures' },
            { k: 'kind', lbl: 'by kind of tool', tip: 'every kind of tool with a failure' },
          ]} />
        </span>
      }
    >
      <div className="scroller">
        <table className="roomy exits-sum">
          <thead>
            <tr>
              <th>who</th>
              <th className="num">processes</th>
              <th data-tip="the share of processes with a known ending that failed">failed</th>
              <th className="num" data-tip="exited non-zero, past the answers">non-zero</th>
              <th className="num" data-tip="stopped by a signal other than SIGPIPE">signal</th>
              <th data-tip="the commonest exit codes, then signals; hover one for what it means">most often</th>
            </tr>
          </thead>
          <tbody>
            {have.map((c) => (
              <Fragment key={c}>
                <SumRow lbl={CLS_LBL[c] ?? c} col={CLS_COL[c]} b={by[c]}
                  q={{ title: `failures · ${CLS_LBL[c] ?? c}`, sort: 'n', class: [c],
                    outcome: ['nonzero', 'signal'], ...coveredBy(tm) }} />
                {c === 'agent' && types.length > 1 && types.map(([t, b]) => (
                  <SumRow key={t} sub lbl={AGENT_TYPE_LBL[t] ?? t} col={CLS_COL.agent} b={b}
                    q={{ title: `failures · ${AGENT_TYPE_LBL[t] ?? t}`, sort: 'n', class: ['agent'],
                      agent_type: [t], outcome: ['nonzero', 'signal'], ...coveredBy(tm) }} />
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {ordered.length ? (
        <div className="scroller" style={{ marginTop: 12 }}>
          <table className="roomy shares">
            <thead>
              <tr>
                <SortTh s={s} k="k" first="asc">{view === 'tool' ? 'tool' : 'kind of tool'}</SortTh>
                {have.map((c) => (
                  <SortTh key={c} s={s} k={`r:${c}`} className="cls" style={{ color: CLS_INK[c] }}
                    tip={`the share of ${CLS_LBL[c] ?? c}'s runs that failed, and how many; sorts by the share`}>
                    {CLS_LBL[c] ?? c}
                  </SortTh>
                ))}
              </tr>
            </thead>
            <tbody>
              {ordered.map((k) => (
                <tr key={k}>
                  <td className="mono">{k}</td>
                  {have.map((c) => {
                    const t = cell.get(k)!.get(c);
                    if (!t || !t.calls) return <td key={c}><span className="nm">{DASH}</span></td>;
                    const f = failed(t);
                    const tip = [
                      `${fint(t.calls)} processes`,
                      `${fint(t.nonzero)} exited non-zero`,
                      t.signal ? `${fint(t.signal)} stopped by a signal` : null,
                      t.answer ? `${fint(t.answer)} exit 1 that is an answer (not a failure)` : null,
                      t.pipe ? `${fint(t.pipe)} SIGPIPE (not a failure)` : null,
                      t.none ? `${fint(t.none)} with no ending recorded` : null,
                    ].filter(Boolean).join('\n');
                    const share = <ShareCell pct={rate(t)} sub={`${fint(f)} of ${fint(rated(t))}`} c={CLS_COL[c]} />;
                    return (
                      <td key={c} data-tip={tip}>
                        {f > 0 ? <IoNum q={drill(c, k)}>{share}</IoNum> : share}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <NoRows what={view === 'tool' ? 'no listed tool failed in the window' : 'no kind of tool failed in the window'} />
      )}
    </Panel>
  );
}

const ZERO: ExitTally = { calls: 0, nonzero: 0, answer: 0, signal: 0, pipe: 0, none: 0 };

function SumRow({ lbl, col, b, sub, q }: {
  lbl: string;
  col: string;
  b: ExitBlock | undefined;
  sub?: boolean;
  q: IoQuery;
}) {
  if (!b) return null;
  const f = failed(b);
  return (
    <tr className={sub ? 'sub' : undefined}>
      <td style={sub ? { paddingLeft: 22 } : undefined}>
        <i className="u-dot" style={{ background: col }} />
        {lbl}
      </td>
      <td className="num">{fint(b.calls)}</td>
      <td>
        {f > 0 ? (
          <IoNum q={q}><ShareCell pct={rate(b)} sub={fint(f)} c={col} /></IoNum>
        ) : <ShareCell pct={rate(b)} sub={fint(f)} c={col} />}
      </td>
      <td className="num">{fint(b.nonzero)}</td>
      <td className="num">{fint(b.signal)}</td>
      {/* one cell that wraps, so the table keeps to its panel's width */}
      <td className="mono nm wrap">
        {b.codes.length || b.signals.length ? [
          ...b.codes.slice(0, 3).map(([code, n]) => ({ k: `c${code}`, tip: codeTip(code), text: `exit ${code} ×${fint(n)}` })),
          ...b.signals.slice(0, 2).map(([g, n]) => ({ k: `s${g}`, tip: SIG_WORDS[g], text: `${SIG_NAME[g] ?? `signal ${g}`} ×${fint(n)}` })),
        ].map((x, i) => (
          // the separator is plain text, so the line can break there and only there
          <Fragment key={x.k}>{i ? ' · ' : null}<span className="exit-item" data-tip={x.tip}>{x.text}</span></Fragment>
        )) : DASH}
      </td>
    </tr>
  );
}

