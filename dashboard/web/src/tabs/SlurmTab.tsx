/* Slurm: what each session asks of the scheduler, and how it asks.
 *
 * "An agent submitted job 51754668" says nothing about agent behaviour; how
 * agents drive the scheduler does. On boslogin06 they made ~17 queries a job
 * where people made 0.2, one Claude session polled squeue every 5 s for five
 * hours, another submitted 112 jobs in a minute, and four in five agent
 * submissions asked for `--parsable`: a program reading the job id. So the tab
 * is about sessions, cadence, and manner, from the window's `slurm` reducer
 * (rc_dashboard/reducers/slurm.py):
 *   - per agent session (folded, as Trajectories): jobs, refusals, the busiest
 *     minute, distinct command lines, the flags it submits with, its queries
 *     and its longest polling loop, cancels, GPU-hours asked;
 *   - per class, the same measures side by side;
 *   - the scheduler's refusals, by reason;
 *   - the latest submissions from the live tier, with what came back.
 * Requests are read off argv: a #SBATCH line in the script is not seen.
 * Its own tab because not every cluster runs Slurm: `slurm.enabled: false`
 * in the service's config hides it.
 */
import { GLOSS } from '../lib/glossary';
import { Fragment, useState } from 'react';
import type {
  Cls,
  LiveResponse,
  LiveSubmit,
  SlurmClass,
  SlurmPanel,
  SlurmSession,
  ToolMix,
} from '../api/types';
import { Lines } from '../charts/primitives';
import type { LineSeries } from '../charts/primitives';
import { clockLabel, fullClock, timeTicks } from '../charts/scale';
import { Segmented } from '../components/ClassFilter';
import { Basis, Legend, NoRows, Panel } from '../components/Panel';
import { CmdCell } from '../components/Cmd';
import { Plate } from '../components/Plate';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, pc } from '../lib/format';
import { pick, shown, winLabel } from '../lib/panels';
import type { Panels } from '../lib/panels';

type Flag = 'parsable' | 'dependency' | 'array' | 'wrap';
const FLAGS: [Flag, string, string][] = [
  ['parsable', '--parsable', 'prints only the job id: a program, not a person, is reading it'],
  ['dependency', '--dependency', 'starts after another job: a pipeline of chained jobs'],
  ['array', '--array', 'one submission, many tasks'],
  ['wrap', '--wrap', 'an inline command, no batch script'],
];

const who = (r: SlurmSession) =>
  r.agent_type ? AGENT_TYPE_LBL[r.agent_type] ?? r.agent_type : CLS_LBL[String(r.cls)] ?? String(r.cls);

/** "18:41 – 23:59", or with dates when the session crosses midnight. */
function span(a: number, b: number): string {
  const day = (e: number) => fullClock(e).slice(0, 5);
  const hm = (e: number) => fullClock(e).slice(6);
  return day(a) === day(b) ? `${hm(a)} – ${hm(b)}` : `${fullClock(a)} – ${fullClock(b)}`;
}

function sessionTip(r: SlurmSession): string {
  const lines = [
    r.session ? `session ${r.session}` : `everything ${r.user ?? 'this user'} ran outside an agent`,
    r.keys > 1 ? `${fint(r.keys)} collector keys folded into it${r.fold ? ` (${Object.entries(r.fold).map(([k, n]) => `${k} ${n}`).join(', ')})` : ''}` : '',
    r.first != null && r.last != null ? `${fullClock(r.first)} → ${fullClock(r.last)}` : '',
  ];
  return lines.filter(Boolean).join('\n');
}

function Polling({ r }: { r: SlurmSession }) {
  const l = r.loop;
  if (!l) return <span className="nm">{r.queries ? 'not polling' : DASH}</span>;
  const txt = `every ${dur(l.every_s)} for ${dur(l.span_s)}`;
  return (
    <span
      className={l.polling ? undefined : 'nm'}
      style={l.polling ? { color: 'var(--crimson-ink)' } : undefined}
      data-tip={`${fint(l.polls)} checks of the queue, ${fullClock(l.from)} → ${fullClock(l.to)}\n(calls within 2 s count as one check; checks within 3 min of each other are one stretch of polling)${l.polling ? '' : '\nunder 10 min: not counted as polling'}`}
    >
      {txt}
    </span>
  );
}

function FlagCell({ r }: { r: SlurmSession }) {
  const on = FLAGS.filter(([f]) => (r.flags[f] ?? 0) > 0);
  if (!on.length) return <span className="nm">{r.jobs + r.rejected ? 'no flags' : DASH}</span>;
  // one flag a line: side by side they made this the widest column
  return (
    <span className="mono">
      {on.map(([f, lbl, tip]) => (
        <div key={f} data-tip={tip} style={{ whiteSpace: 'nowrap' }}>
          {lbl} {fint(r.flags[f])}
        </div>
      ))}
    </span>
  );
}

function SessionDetail({ r }: { r: SlurmSession }) {
  const kv = (o: Record<string, number> | undefined | null) =>
    o && Object.keys(o).length ? Object.entries(o).map(([k, n]) => `${k} ${fint(n)}`).join(' · ') : DASH;
  const rows: [string, React.ReactNode][] = [
    ['what came back', kv(r.outcomes as Record<string, number>)],
    ['submitted with', kv(r.tools)],
    ['queue checks, by command', kv(r.queries_by)],
    ['checks (calls within 2 s count once)', r.polls ? `${fint(r.polls)} · typically ${dur(r.poll_gap_s)} apart` : DASH],
    ['partitions', kv(r.partitions)],
    ['cancel calls (scancel)', r.cancels ? fint(r.cancels) : DASH],
    ['srun / salloc runs', r.runs ? `${fint(r.runs)} (${fint(r.runs_failed)} exited non-zero)` : DASH],
    ['request on the command line', r.argv_req_pct == null ? DASH : pc(r.argv_req_pct)],
    ['GPUs requested', r.gpu_jobs ? `${fint(r.gpus)} over ${fint(r.gpu_jobs)} jobs` : DASH],
    ['GPU-hours requested', r.gpu_h != null ? `${fint(r.gpu_h)} (GPUs × time limit, ${fint(r.gpu_h_known)} of ${fint(r.gpu_jobs)} GPU jobs had both on the command line)` : DASH],
    ['refused because', r.fails.length ? r.fails.map((f) => `${f.why} (${fint(f.n)})`).join('\n') : DASH],
    ['latest command', r.args ? <span className="mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{r.args}</span> : DASH],
  ];
  return (
    <dl className="kv">
      {rows.map(([k, v]) => (
        <Fragment key={k}>
          <dt>{k}</dt>
          <dd style={{ whiteSpace: 'pre-wrap' }}>{v}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

type Sort = 'queries' | 'jobs' | 'polling' | 'refused';
const SORTS: Record<Sort, (r: SlurmSession) => number> = {
  queries: (r) => r.queries,
  jobs: (r) => r.jobs,
  polling: (r) => (r.loop?.polling ? r.loop.span_s : 0),
  refused: (r) => r.rejected,
};

function Sessions({ rows, total }: { rows: SlurmSession[]; total: number }) {
  const [sort, setSort] = useState<Sort>('queries');
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const sorted = [...rows].filter((r) => SORTS[sort](r) > 0 || sort === 'queries').sort((a, b) => SORTS[sort](b) - SORTS[sort](a));
  const keyOf = (r: SlurmSession, i: number) => r.session ?? `${r.cls}:${r.user ?? i}`;
  return (
    <>
      <Segmented<Sort>
        value={sort}
        onChange={setSort}
        label="rank by"
        options={[
          { k: 'queries', lbl: 'most queue checks', tip: 'squeue, sacct, scontrol, … calls' },
          { k: 'jobs', lbl: 'most jobs', tip: 'submissions that came back with a job id' },
          { k: 'polling', lbl: 'longest polling', tip: 'the longest stretch of checking the queue at least every 3 min, 10 min or more' },
          { k: 'refused', lbl: 'most refused', tip: 'submissions the scheduler turned down' },
        ]}
      />
      {sorted.length ? (
        <div className="scroller vscroll">
          <table className="roomy byuser-t wrap-head">
            <thead>
              <tr>
                <th data-tip="an agent session (its tool calls folded into it), or everything one person or IDE ran; first and last Slurm activity beneath">session</th>
                <th className="num" data-tip="submissions that came back with a job id">jobs submitted</th>
                <th className="num" data-tip="submissions the scheduler turned down: a QOS limit, a partition rule, a bad script">refused</th>
                <th className="num" data-tip="the most jobs it submitted within a single minute">most in a minute</th>
                <th className="num" data-tip="different submit commands once numbers are masked: one script resubmitted 100 times is 1; a newly written script each time counts each">distinct commands</th>
                <th data-tip="flags on its submit commands, and how many submissions used each">sbatch flags</th>
                <th className="num" data-tip="calls to squeue, sacct, scontrol, sinfo, … asking how jobs or the cluster are doing">queue checks</th>
                <th className="num" data-tip="queue checks for every job it submitted">checks per job</th>
                <th data-tip="its longest stretch of checking the queue at least every 3 min: how often, and for how long">polling the queue</th>
              </tr>
            </thead>
            {sorted.map((r, i) => {
              const k = keyOf(r, i);
              const o = !!open[k];
              return (
                <tbody key={k}>
                  <tr className={`byuser-row${o ? ' open' : ''}`} onClick={() => setOpen((m) => ({ ...m, [k]: !o }))}>
                    <td data-tip={sessionTip(r)}>
                      <span className="caret">{o ? '▾' : '▸'}</span> <b>{r.user ?? DASH}</b>{' '}
                      <i className="u-dot" style={{ background: CLS_COL[String(r.cls)] }} />
                      <span className="nm">{who(r)}</span>
                      {r.first != null && r.last != null && (
                        <div className="mono nm" style={{ fontSize: '10.5px', paddingLeft: 14 }}>
                          {span(r.first, r.last)}
                        </div>
                      )}
                    </td>
                    <td className="num">{fint(r.jobs)}</td>
                    <td className="num" style={r.rejected ? { color: 'var(--crimson-ink)' } : undefined}>
                      {r.rejected ? fint(r.rejected) : DASH}
                    </td>
                    <td className="num">{r.peak_per_min ? fint(r.peak_per_min) : DASH}</td>
                    <td className="num">{r.jobs + r.rejected ? `${fint(r.shapes)}${r.shapes_capped ? '+' : ''}` : DASH}</td>
                    <td><FlagCell r={r} /></td>
                    <td className="num">{fint(r.queries)}</td>
                    <td className="num">{r.queries_per_job == null ? DASH : fmt(r.queries_per_job)}</td>
                    <td><Polling r={r} /></td>
                  </tr>
                  {o && (
                    <tr className="byuser-sub">
                      <td colSpan={9}>
                        <SessionDetail r={r} />
                      </td>
                    </tr>
                  )}
                </tbody>
              );
            })}
          </table>
        </div>
      ) : (
        <NoRows what="no session matches this ranking" />
      )}
      <p className="paths">
        {`${fint(sorted.length)} session${sorted.length === 1 ? '' : 's'}`}
        {total > rows.length ? ` · the service sends the busiest ${fint(rows.length)} of ${fint(total)}` : ''}
      </p>
    </>
  );
}

/* One row per measure, one column per class: the classes differ in size by
 * orders of magnitude, so the rows that compare them are rates and shares. */
function ByClass({ by, have, tm }: { by: Record<string, SlurmClass>; have: string[]; tm: ToolMix }) {
  const kind = (c: string, b: string) => tm.by_class?.[c]?.rows?.find((r) => r.b === b);
  const schedPct = (c: string) => {
    const t = tm.by_class?.[c];
    if (!t?.calls) return null;
    return (100 * ((kind(c, 'slurm-submit')?.calls ?? 0) + (kind(c, 'slurm-query')?.calls ?? 0))) / t.calls;
  };
  const tl = (c: string, q: 'p50' | 'p90') => by[c]?.time_limit_s?.[q] ?? null;
  const rows: [string, string | undefined, (c: string) => React.ReactNode][] = [
    ['jobs submitted', 'submissions that came back with a job id', (c) => fint(by[c].jobs)],
    ['by sessions / users', undefined, (c) => `${fint(by[c].sessions)} / ${fint(by[c].users)}`],
    ['queue checks per job', 'squeue, sacct, scontrol, … calls for every job submitted', (c) => (by[c].queries_per_job == null ? DASH : fmt(by[c].queries_per_job))],
    ['queue checks', undefined, (c) => fint(by[c].queries)],
    ['sessions polling the queue', 'a poll at least every 3 min, for 10 min or more', (c) => fint(by[c].polling_sessions)],
    ['hours spent polling', 'the sum of those stretches of polling', (c) => (by[c].polling_h ? fmt(by[c].polling_h) : DASH)],
    ['share of its processes that are scheduler commands', 'from the tool mix: sbatch, srun, salloc, scancel, squeue, sacct, sinfo, scontrol, … over every process the class ran', (c) => pc(schedPct(c))],
    ...FLAGS.map(([f, lbl, tip]): [string, string, (c: string) => React.ReactNode] => [
      `submitted with ${lbl}`,
      `${tip}; share of the submissions that asked for a job`,
      (c) => pc(by[c].flags_pct?.[f]),
    ]),
    ['request on the command line', 'partition, time, memory, or GPUs in argv; the rest are in the script (#SBATCH), which is not read', (c) => pc(by[c].argv_req_pct)],
    ['dry runs (--test-only)', 'asks when a job would start, makes none', (c) => fint(by[c].dry_runs)],
    ['refused by the scheduler', undefined, (c) => (by[c].asked ? `${fint(by[c].rejected)} (${pc(by[c].rejected_pct)})` : DASH)],
    ['busiest minute (one session)', 'the most jobs one session submitted in a single minute', (c) => fint(by[c].peak_per_min)],
    ['srun / salloc from these hosts', 'interactive steps and allocations, and how many exited non-zero', (c) => (by[c].runs ? `${fint(by[c].runs)} (${fint(by[c].runs_failed)} failed)` : DASH)],
    ['cancels', undefined, (c) => fint(by[c].cancels)],
    ['GPU·h asked', 'GPUs × time limit, where both were on the command line', (c) => (by[c].gpu_h == null ? DASH : fint(by[c].gpu_h))],
    ['time limit asked, median / p90', 'where it was on the command line', (c) => (tl(c, 'p50') == null ? DASH : `${dur(tl(c, 'p50'))} / ${dur(tl(c, 'p90'))}`)],
  ];
  return (
    <div className="scroller">
      <table className="roomy">
        <thead>
          <tr>
            <th />
            {have.map((c) => (
              <th key={c} className="num">
                <i className="u-dot" style={{ background: CLS_COL[c] }} />
                {CLS_LBL[c] ?? c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([k, tip, cell]) => (
            <tr key={k}>
              <td data-tip={tip}>{k}</td>
              {have.map((c) => (
                <td key={c} className="num">{cell(c)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TimeChart({ s, have, field, ylabel }: {
  s: NonNullable<SlurmPanel['series']>;
  have: string[];
  field: 'jobs' | 'queries';
  ylabel: string;
}) {
  const x0 = s.t[0];
  const x1 = s.t[s.t.length - 1];
  const series: LineSeries[] = have
    .filter((c) => s.by_class[c])
    .map((c) => ({
      lbl: CLS_LBL[c] ?? c,
      c: CLS_COL[c],
      pts: s.t.map((x, i) => ({ x, y: s.by_class[c][field][i] ?? null })),
    }));
  if (!series.length || x0 == null) return <NoRows what="nothing in the window" />;
  return (
    <>
      <Lines
        series={series}
        W={520}
        H={200}
        ymin={0}
        xmin={x0}
        xmax={x1 > x0 ? x1 : x0 + s.bin_s}
        xticks={(a, b) => timeTicks(a, b)}
        xfmt={clockLabel}
        xtip={fullClock}
        ylabel={ylabel}
        xname="at"
        yname={ylabel}
        minSpan={3 * s.bin_s}
      />
      <Legend items={series.map((l) => ({ k: l.lbl, c: l.c }))} />
    </>
  );
}

function Latest({ rows }: { rows: LiveSubmit[] }) {
  const out = (r: LiveSubmit) => {
    const o = r.outcome as string | undefined;
    if (o === 'rejected') return <span style={{ color: 'var(--crimson-ink)' }} data-tip={String(r.why ?? '')}>refused</span>;
    if (o === 'dry_run') return <span className="nm" data-tip="--test-only: asks when it would start, makes no job">dry run</span>;
    if (o === 'step') return <span className="nm" data-tip="a step inside an allocation it already held">step</span>;
    return r.job_id != null ? <span className="mono">{String(r.job_id)}</span> : <span className="nm">{DASH}</span>;
  };
  const req = (r: LiveSubmit) => {
    const bits = [
      r.partition ? String(r.partition) : null,
      r.time_limit_s ? dur(Number(r.time_limit_s)) : null,
      r.gpus ? `${r.gpus} GPU` : null,
    ].filter(Boolean);
    return bits.length ? bits.join(' · ') : <span className="nm" data-tip="no request on the command line: it is in the script's #SBATCH lines, which are not read">in the script</span>;
  };
  return (
    <div className="scroller vscroll">
      <table className="roomy">
        <thead>
          <tr>
            <th>at</th>
            <th>user</th>
            <th>from</th>
            <th>job</th>
            <th data-tip="the partition, time limit, and GPUs the command line asked for; “in the script” when they were #SBATCH lines the collector cannot see">asked for</th>
            <th>command</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.job_id ?? ''}-${r.ts ?? ''}-${i}`}>
              <td className="mono nm">{r.ts ?? DASH}</td>
              <td>{r.user ?? DASH}</td>
              <td>
                {typeof r.actor3 === 'string' && <i className="u-dot" style={{ background: CLS_COL[r.actor3] }} />}
                {r.agent_type ? AGENT_TYPE_LBL[r.agent_type] ?? r.agent_type : CLS_LBL[String(r.actor3 ?? '')] ?? DASH}
              </td>
              <td>{out(r)}</td>
              <td className="mono">{req(r)}</td>
              <CmdCell text={r.args == null ? null : String(r.args)} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SlurmTab({ P, live, cls }: { P: Panels; live: LiveResponse | null; cls: Cls[] }) {
  const sp = pick<SlurmPanel>(P, 'slurm', 'ebpfm');
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const by = sp.by_class ?? {};
  const have = shown(by, cls);
  const a = by.agent;
  const win = winLabel(sp);
  const byOf = (f: (c: SlurmClass) => number | null) =>
    Object.fromEntries(have.map((c) => [c, f(by[c])]));
  const topWhy = (sp.rejections ?? []).find((r) => r.cls === 'agent');
  const sessions = (sp.sessions ?? []).filter((r) => cls.includes(r.cls as Cls));
  const submits = (live?.submits ?? []).filter(
    (r) => typeof r.actor3 !== 'string' || cls.includes(r.actor3 as Cls),
  );
  const rej = (sp.rejections ?? []).filter((r) => cls.includes(r.cls as Cls));
  const basis = <Basis items={Object.entries(sp.basis ?? {})} />;

  return (
    <>
      <div className="plates">
        <Plate
          k={`agent jobs submitted ${win}`}
          total={cls.includes('agent') && sp._present ? a?.jobs ?? 0 : null}
          stripe="var(--s1)"
          by={byOf((c) => c.jobs)}
          tip="submissions that came back with a job id; dry runs and steps into a held allocation are not jobs"
          note={a ? `${fint(a.sessions)} agent sessions · busiest ${fint(a.peak_per_min)} in one minute` : undefined}
        />
        <Plate
          k="queue checks per job"
          total={cls.includes('agent') ? a?.queries_per_job ?? null : null}
          fmtFn={fmt}
          stripe="var(--s5)"
          by={byOf((c) => c.queries_per_job)}
          tip="squeue, sacct, scontrol, sinfo, … calls for every job submitted: an agent waiting on a job polls; a person mostly does not"
          note={a ? `${fint(a.queries)} queue checks by agents` : undefined}
        />
        <Plate
          k="agent sessions polling the queue"
          total={cls.includes('agent') && sp._present ? a?.polling_sessions ?? 0 : null}
          stripe={(a?.polling_sessions ?? 0) > 0 ? 'var(--crit)' : 'var(--s2)'}
          by={byOf((c) => c.polling_sessions)}
          tip="a session that polled the scheduler at least every 3 minutes for 10 minutes or more"
          note={a ? `${fmt(a.polling_h)} h spent polling` : undefined}
        />
        <Plate
          k="submissions refused"
          total={cls.includes('agent') && sp._present ? a?.rejected ?? 0 : null}
          stripe={(a?.rejected ?? 0) > 0 ? 'var(--crit)' : 'var(--s2)'}
          by={byOf((c) => c.rejected)}
          tip="no job id, a non-zero exit, and the scheduler's own refusal"
          note={a?.rejected ? `${pc(a.rejected_pct)} of agent submissions${topWhy ? ` · most: ${topWhy.why}` : ''}` : undefined}
        />
      </div>

      <div className="grid">
        <Panel title="Queue checks (squeue, sacct, …)" p={sp} empty={!sp.series?.t.length} emptyWhat="no queue check in the window" basis={basis}>
          {sp.series && <TimeChart s={sp.series} have={have} field="queries" ylabel={`checks / ${dur(sp.series.bin_s)}`} />}
        </Panel>
        <Panel title="Jobs submitted" p={sp} empty={!sp.series?.t.length} emptyWhat="no submission in the window">
          {sp.series && <TimeChart s={sp.series} have={have} field="jobs" ylabel={`jobs / ${dur(sp.series.bin_s)}`} />}
        </Panel>
      </div>

      <div className="grid wide">
        <Panel
          title="Sessions at the scheduler"
          ask={[{ panel: 'slurm', keys: ['sessions', 'sessions_total', 'basis'] }]}
          p={sp}
          span
          empty={!sessions.length}
          emptyWhat="no session submitted or queried in the window"
          basis={basis}
        >
          <Sessions rows={sessions} total={sp.sessions_total ?? sessions.length} />
        </Panel>

        <Panel title="How each class uses the scheduler" p={sp} span empty={!have.length} emptyWhat="no class block for the selection"
          ask={[{ panel: 'slurm', keys: ['by_class', 'basis'] }]}>
          <ByClass by={by} have={have} tm={tm} />
        </Panel>

        <Panel title="Submissions the scheduler refused" p={sp} span empty={!rej.length} emptyWhat="the scheduler refused nothing in the window"
          ask={[{ panel: 'slurm', keys: ['rejections', 'by_class'] }]}>
          <div className="scroller">
            <table className="roomy">
              <thead>
                <tr>
                  <th>from</th>
                  <th>tool</th>
                  <th data-tip={GLOSS.refused}>why</th>
                  <th className="num">times</th>
                  <th>e.g.</th>
                </tr>
              </thead>
              <tbody>
                {rej.map((r, i) => (
                  <tr key={i}>
                    <td>
                      <i className="u-dot" style={{ background: CLS_COL[r.cls] }} />
                      {CLS_LBL[r.cls] ?? r.cls}
                    </td>
                    <td className="mono">{r.tool}</td>
                    <td className="mono" style={{ whiteSpace: 'normal', minWidth: 320 }}>{r.why}</td>
                    <td className="num">{fint(r.n)}</td>
                    <td className="nm" data-tip={r.eg?.args}>{r.eg ? `${r.eg.user ?? DASH} · ${r.eg.ts ?? ''}` : DASH}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Latest submissions" span live feed="live" empty={!submits.length} emptyWhat="no submission in the window"
          ask={[{ panel: 'live', keys: ['submits'] }]}>
          <Latest rows={submits} />
        </Panel>
      </div>
    </>
  );
}
