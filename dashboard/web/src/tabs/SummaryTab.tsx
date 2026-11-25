/* Summary -- the page's first screen: the four questions a reader opens the
 * dashboard to ask, each answered in at most two numbers, each number in plain
 * words, each card linking to where it is broken down.
 *
 *   Is collection healthy?          /api/status: each host's collector verdict
 *                                    (health.py: the verdicts the Feeds tab's
 *                                    Collectors table draws) and the level they
 *                                    roll up to (status.py)
 *   Who runs agents, and how much?  /api/trajectories' `summary`: the users with
 *                                    an agent session in the window, and those
 *                                    sessions (Live's sessions plate); every
 *                                    class's sessions side by side, by word
 *   Is anything risky?              `panels.risk`: the agents' commands this
 *                                    site's rules say should not run here, and
 *                                    credentials on their command lines (Live's
 *                                    risky-actions plate, Risk › Site rules)
 *   Is a login node struggling?     `panels.node`: the busiest node's CPU, and
 *                                    what the service's own lines flag there
 *                                    (Login node health, Needs attention)
 *
 * Nothing here is reduced. Each number is one the service already sends, read
 * as the panel it comes from reads it, so this screen and that panel cannot
 * disagree; the one request of its own is /api/status. Eight numbers at most,
 * and a number nobody measured reads "not measured", never 0, with why beside
 * it. A count is the agent class's, never the agent and IDE classes folded
 * together; the classes' sessions are drawn side by side, each with its word.
 */
import { memo } from 'react';
import type { MouseEvent, ReactNode } from 'react';
import type { Cls, CollectorVerdict, NodePanel, RiskPanel, StatusDoc, StatusLevel, Trajectories } from '../api/types';
import { useStatus } from '../api/hooks';
import { Legend, Panel, slug } from '../components/Panel';
import { SplitBar } from '../components/SplitBar';
import type { SplitPart } from '../components/SplitBar';
import { CLS, CLS_COL, CLS_LBL } from '../lib/classes';
import { fint, pc } from '../lib/format';
import { useNodeWords } from '../lib/nodes';
import type { NodeWords } from '../lib/nodes';
import { pick, winLabel } from '../lib/panels';
import type { Panels } from '../lib/panels';

/** A number on the screen, or null: nobody measured it -- or, `shown: false`,
 *  it was measured and the class filter hides it. */
export interface Num {
  v: number | null;
  /** how the page writes it; `fint` of it by default */
  text?: string;
  shown?: false;
}
/** One figure: its number, a second one it is out of, and its words. */
export interface Fig {
  n: Num;
  of?: Num;
  what: string;
  /** how it is counted: its words' hover, which "how to read" gathers */
  tip: string;
}
/** The card's verdict in a word, beside a colour: `ok`, `stale` (something to
 *  look at), `missing` (data not arriving) or `empty` (not checked). */
export interface Verdict {
  cls: 'ok' | 'stale' | 'missing' | 'empty';
  text: string;
  tip?: string;
}
/** Where a card is broken down: a tab, and the part of it to land on. */
export interface Link {
  tab: string;
  sec?: string;
  where: string;
}
export interface Answer {
  verdict: Verdict | null;
  figs: Fig[];
  /** why a number is not measured, or what stands behind one, in words */
  note: string | null;
  links: Link[];
  /** each class's sessions, side by side (the agents' card) */
  split?: SplitPart[];
}

const nm = (): Num => ({ v: null });
const hidden = (): Num => ({ v: null, shown: false });
const num = (v: number, text?: string): Num => ({ v, text });
const plural = (n: number, one: string, many: string) => (n === 1 ? one : many);

/** A few names, then "and more": a count of the rest would be one more number. */
function names(list: string[], most = 4): string {
  return list.length > most ? `${list.slice(0, most).join(', ')} and more` : list.join(', ');
}

/* ------------------------------------------------- is collection healthy */

const LEVEL: Record<StatusLevel, Verdict> = {
  ok: { cls: 'ok', text: 'working' },
  warn: { cls: 'stale', text: 'something to look at' },
  crit: { cls: 'missing', text: 'data not arriving' },
};
/** A collector's verdict as the Feeds tab words it (FeedsTab.tsx `VERDICT`). */
const VERDICT_WORDS: Record<CollectorVerdict, string> = {
  ok: 'OK',
  degraded: 'degraded',
  stopped: 'stopped',
  silent: 'silent',
  unknown: 'no heartbeat',
};

export function collection(status: StatusDoc | null, error: string | null, nodes: NodeWords): Answer {
  const links: Link[] = [{ tab: 'feeds', sec: 'sec-collectors', where: 'System › Feeds' }];
  const tip =
    'a collector is OK while it writes its heartbeat, a record a minute, within feeds.ebpf.max_age_s; degraded: ' +
    'running but losing data it could have; stopped: a stop record after its last heartbeat; silent: neither. ' +
    'The verdicts System › Feeds draws, from /api/status';
  const what = `${nodes.many}’ collectors OK`;
  if (!status) {
    return {
      verdict: { cls: 'empty', text: 'not checked', tip: error ?? undefined },
      figs: [{ n: nm(), what, tip }],
      note: error ? '/api/status did not answer' : 'waiting for /api/status',
      links,
    };
  }
  const hosts = status.hosts ?? [];
  const verdict = { ...(LEVEL[status.level] ?? LEVEL.warn), tip: status.why?.length ? status.why.join('\n') : undefined };
  if (!hosts.length) {
    return { verdict, figs: [{ n: nm(), what, tip }], note: `no ${nodes.one}’s collector has written a record here`, links };
  }
  const ok = hosts.filter((h) => h.verdict === 'ok').length;
  const wrong = [
    ...hosts.filter((h) => h.verdict !== 'ok').map((h) => `${h.host} (${VERDICT_WORDS[h.verdict] ?? h.verdict})`),
    // a collector writing away while the copy that brings its files here fails
    ...hosts.filter((h) => h.verdict === 'ok' && h.transport && !h.transport.ok).map((h) => `${h.host} (copy failed)`),
  ];
  return {
    verdict,
    figs: [{ n: num(ok), of: num(hosts.length), what, tip }],
    note: wrong.length
      ? `not OK: ${names(wrong)}`
      : status.level === 'ok'
        ? null
        : 'what is wrong is listed on System › Feeds',
    links,
  };
}

/* ----------------------------------------- who runs agents, and how much */

export function agents(traj: Trajectories | null, error: string | null, cls: Cls[]): Answer {
  const links: Link[] = [
    { tab: 'users', where: 'Users' },
    { tab: 'sessions', sec: 'sec-ranked-sessions', where: 'Agents › Sessions' },
  ];
  const userTip = 'accounts with at least one agent session in the window: the Users tab lists each, with a portrait';
  const sessTip =
    'an agent and everything it ran; a one-off probe, such as a script checking `codex --version`, is not one. ' +
    'Live’s sessions plate counts the same';
  const absent = (note: string, n = nm): Answer => ({
    verdict: null,
    figs: [{ n: n(), what: 'users ran an agent', tip: userTip }, { n: n(), what: 'agent sessions', tip: sessTip }],
    note,
    links,
  });
  if (!cls.includes('agent')) return absent('the agent class is filtered out', hidden);
  if (!traj) return absent(error ? '/api/trajectories did not answer' : 'waiting for /api/trajectories');
  if (traj._present === false) return absent('not measured: no session records to read yet');
  const sum = traj.summary;
  if (!sum) return absent('not measured: the service sent no count of sessions');
  // the summary holds every class with a session in the window: none is 0
  const a = sum.agent;
  const users = a?.users ?? 0;
  const sessions = a?.sessions ?? 0;
  const split: SplitPart[] = CLS.map((c) => ({
    k: c,
    lbl: CLS_LBL[c],
    v: sum[c]?.sessions ?? 0,
    amt: `${fint(sum[c]?.sessions ?? 0)} sessions`,
    c: CLS_COL[c],
    muted: !cls.includes(c),
    note: cls.includes(c) ? undefined : 'hidden by the class filter: still part of the whole',
  }));
  return {
    verdict: null,
    figs: [
      { n: num(users), what: plural(users, 'user ran an agent', 'users ran an agent'), tip: userTip },
      { n: num(sessions), what: plural(sessions, 'agent session', 'agent sessions'), tip: sessTip },
    ],
    note: `${winLabel(traj) === 'in window' ? 'in the window' : winLabel(traj)}; each class’s sessions:`,
    links,
    split,
  };
}

/* --------------------------------------------------- is anything risky */

/** A section's rows summed for one class, as Live's plate sums them. */
const classSum = (rows: { classes?: Partial<Record<string, number>> }[] | undefined, c: string) =>
  (rows ?? []).reduce((s, r) => s + (r.classes?.[c] ?? 0), 0);

export function risky(P: Panels, cls: Cls[]): Answer {
  const risk = pick<RiskPanel>(P, 'risk', 'ebpfm');
  const links: Link[] = [{ tab: 'risk', where: 'Risk › Site rules' }];
  const dgTip =
    'commands the agent class ran that this site’s rules say should not run on a login node (risk.profile): the ' +
    'rows of Risk › Site rules’ command sections, summed, as Live’s risky-actions plate sums them';
  const secTip = 'credentials typed into an agent’s command line, by the same rules: counted, never shown';
  const unmeasured = (note: string, verdict: Verdict | null = { cls: 'empty', text: 'not checked' }, n = nm): Answer => ({
    verdict,
    figs: [{ n: n(), what: 'agent commands that should not run here', tip: dgTip }],
    note,
    links,
  });
  // nothing is counted while the panel is not present, though its envelope may
  // still carry the empty kinds (Live's plate reads it the same way)
  if (risk._present === false) {
    const why = risk._waiting
      ? 'waiting for the service'
      : risk._unsent
        ? 'no risk panel was sent'
        : risk._error
          ? 'its rules could not be applied'
          : 'no records to check yet';
    return unmeasured(`not checked: ${why}`);
  }
  if (!cls.includes('agent')) return unmeasured('the agent class is filtered out', null, hidden);
  const hasDg = risk.dangerous != null;
  const hasSec = risk.secrets != null;
  if (!hasDg && !hasSec) return unmeasured('this cluster’s risk profile counts no command or credential section');
  const dg = classSum(risk.dangerous?.rows, 'agent');
  const sec = classSum(risk.secrets?.rows, 'agent');
  const figs: Fig[] = [];
  if (hasDg) figs.push({ n: num(dg), what: plural(dg, 'agent command that should not run here', 'agent commands that should not run here'), tip: dgTip });
  if (hasSec) figs.push({ n: num(sec), what: plural(sec, 'credential on an agent’s command line', 'credentials on agents’ command lines'), tip: secTip });
  // the first section with an agent's hit, as Needs attention links it
  const hit = (risk.sections ?? []).find((sc) => {
    if (sc.kind === 'login_compute') return false;
    const want = new Set(sc.rules);
    const rows = sc.kind === 'credentials' ? risk.secrets?.rows : risk.dangerous?.rows;
    return (rows ?? []).some((r) => want.has(r.id) && (r.classes?.agent ?? 0) > 0);
  });
  if (hit) links[0] = { ...links[0], sec: slug(hit.title) };
  return {
    verdict: dg + sec > 0 ? { cls: 'stale', text: 'flagged' } : { cls: 'ok', text: 'nothing flagged' },
    figs,
    note: `${winLabel(risk) === 'in window' ? 'in the window' : winLabel(risk)}; the human and IDE classes’ are on Risk › Site rules`,
    links,
  };
}

/* ------------------------------------------ is a login node struggling */

export function struggling(P: Panels, nodes: NodeWords): Answer {
  const node = pick<NodePanel>(P, 'node', 'ebpf_node');
  const hosts = node.hosts ?? [];
  const links: Link[] = [{ tab: 'resources', sec: 'sec-node-health', where: `Usage › ${nodes.Many} › ${nodes.One} health` }];
  const busyTip =
    `the share of all its cores’ time spent working, at its newest snapshot: a ${nodes.one} near 100% has no CPU ` +
    'left for the next person to log in. Login node health draws it as CPU busy';
  const th = node.thresholds;
  const flagTip =
    'what the service’s own lines flag, as Needs attention flags it: a local filesystem at or past ' +
    `${th ? `${th.disk_full_pct}% of its space or ${th.inode_full_pct}% of its file slots` : 'the full line'} ` +
    '(node.disk_full_pct, node.inode_full_pct), an account the kernel held back at its CPU limit, an NFS mount retransmitting';
  if (node._present === false || !hosts.length) {
    return {
      verdict: { cls: 'empty', text: 'not checked' },
      figs: [{ n: nm(), what: 'CPU busy on the busiest', tip: busyTip }, { n: nm(), what: 'problems flagged', tip: flagTip }],
      note: node._waiting
        ? 'waiting for the service'
        : node._error
          ? 'not checked: the node snapshot could not be read'
          : `not checked: no ${nodes.one} has written a snapshot yet`,
      links,
    };
  }
  let busiest: { host: string; busy: number } | null = null;
  for (const h of hosts) {
    if (h.cpu_idle_pct == null) continue;
    const busy = 100 - h.cpu_idle_pct;
    if (!busiest || busy > busiest.busy) busiest = { host: h.host, busy };
  }
  const full = (node.disk ?? []).filter((d) => d.full).length;
  const held = hosts.reduce((s, h) => s + (h.per_user ?? []).filter((u) => (u.throttled_delta_s ?? 0) > 0).length, 0);
  const retrying = (node.nfs ?? []).filter((m) => (m.rd_retrans ?? 0) + (m.wr_retrans ?? 0) > 0).length;
  const flags = full + held + retrying;
  const kinds = [
    full ? plural(full, 'a local filesystem full', 'local filesystems full') : null,
    held ? plural(held, 'an account held back at its CPU limit', 'accounts held back at their CPU limit') : null,
    retrying ? 'NFS retransmissions' : null,
  ].filter((k): k is string => k != null);
  return {
    verdict: flags ? { cls: 'stale', text: 'flagged' } : { cls: 'ok', text: 'nothing flagged' },
    figs: [
      busiest
        ? { n: num(busiest.busy, pc(busiest.busy)), what: `CPU busy on ${busiest.host}, the busiest ${nodes.one}`, tip: busyTip }
        : { n: nm(), what: `CPU busy: no ${nodes.one} reported its CPU`, tip: busyTip },
      { n: num(flags), what: plural(flags, 'problem flagged', 'problems flagged'), tip: flagTip },
    ],
    note: kinds.length ? kinds.join(' · ') : `each ${nodes.one}’s newest snapshot: now, not over the window`,
    links,
  };
}

/* ------------------------------------------------------------------ view */

/** A number in the plates' type; one nobody measured says so, never 0. */
function NumEl({ n, small = false }: { n: Num; small?: boolean }) {
  if (n.v == null) return <span className="sum-num nm">{n.shown === false ? 'not shown' : 'not measured'}</span>;
  return <b className={small ? 'sum-num sm' : 'sum-num'}>{n.text ?? fint(n.v)}</b>;
}

/** A plain click is the page's own navigation; one with a modifier opens the
 *  link where the reader asked, a new tab or window. */
function follow(e: MouseEvent, go: () => void) {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  go();
}

/** A card's verdict: a word in a pill, the colour beside it. */
function VerdictPill({ v }: { v: Verdict | null }) {
  if (!v) return null;
  return (
    <span className={`pill ${v.cls}`} data-tip={v.tip}>
      {v.text}
    </span>
  );
}

interface BodyProps {
  /** what the links are a breakdown of, for a screen reader */
  what: string;
  a: Answer;
  go: (tab: string, sec?: string) => void;
  href: (tab: string) => string;
  children?: ReactNode;
}

/** A card's figures, the words behind them, and where each is broken down. */
function Body({ what, a, go, href, children }: BodyProps) {
  return (
    <>
      <div className="sum-figs">
        {a.figs.map((f) => (
          <div className="sum-fig" key={f.what}>
            <NumEl n={f.n} />
            {f.of && f.n.v != null && (
              <span className="sum-of">
                of <NumEl n={f.of} small />
              </span>
            )}
            <span className="sum-what" data-tip={f.tip}>
              {f.what}
            </span>
          </div>
        ))}
      </div>
      {a.note && <p className="sum-note">{a.note}</p>}
      {children}
      <nav className="sum-links" aria-label={`where ${what} is broken down`}>
        {a.links.map((l) => (
          <a key={l.tab} className="sum-go" href={href(l.tab)} onClick={(e) => follow(e, () => go(l.tab, l.sec))}>
            {l.where} {'→'}
          </a>
        ))}
      </nav>
    </>
  );
}

export interface SummaryProps {
  P: Panels;
  traj: Trajectories | null;
  trajError?: string | null;
  cls: Cls[];
  /** open a tab, and land on a part of it */
  go: (tab: string, sec?: string) => void;
  /** a tab's link, over the page's window and filters, for a new tab */
  href: (tab: string) => string;
}

/** The tab: asks /api/status itself, while it is open, and draws the four answers. */
export function SummaryTab(props: SummaryProps) {
  const st = useStatus();
  return <Summary {...props} status={st.data} statusError={st.error} />;
}

export const Summary = memo(function Summary({
  P,
  traj,
  trajError = null,
  cls,
  go,
  href,
  status,
  statusError,
}: SummaryProps & { status: StatusDoc | null; statusError: string | null }) {
  const nodes = useNodeWords();
  const col = collection(status, statusError, nodes);
  const ag = agents(traj, trajError, cls);
  const rk = risky(P, cls);
  const nd = struggling(P, nodes);
  return (
    <>
      <div className="grid sum">
        <Panel title="Is collection healthy?" id="sec-summary-collection" header={<VerdictPill v={col.verdict} />}>
          <Body what="collection" a={col} go={go} href={href} />
        </Panel>
        <Panel title="Who runs agents, and how much?" id="sec-summary-agents" header={<VerdictPill v={ag.verdict} />}>
          <Body what="who runs agents" a={ag} go={go} href={href}>
            {ag.split && ag.split.some((p) => p.v > 0) && (
              <div className="sum-split">
                <SplitBar parts={ag.split} what="sessions in the window" />
                <Legend items={ag.split.map((p) => ({ k: p.lbl, c: p.c }))} />
              </div>
            )}
          </Body>
        </Panel>
        <Panel title="Is anything risky?" id="sec-summary-risk" header={<VerdictPill v={rk.verdict} />}>
          <Body what="what was risky" a={rk} go={go} href={href} />
        </Panel>
        <Panel title={`Is a ${nodes.one} struggling?`} id="sec-summary-nodes" header={<VerdictPill v={nd.verdict} />}>
          <Body what={`how each ${nodes.one} is doing`} a={nd} go={go} href={href} />
        </Panel>
      </div>
      <p className="sum-more">
        What runs now, and who runs it:{' '}
        <a className="sum-go" href={href('live')} onClick={(e) => follow(e, () => go('live'))}>
          Live {'→'}
        </a>
      </p>
    </>
  );
});
