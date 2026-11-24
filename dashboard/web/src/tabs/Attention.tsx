/* Needs attention -- the top of Live: what the service has already flagged,
 * on one list, each line linking to the panel that shows it.
 *
 * It holds no threshold of its own. Each line is a rule the service or another
 * panel already applies -- a feed past its own max age, a risk rule's hits, a
 * session that broke one of the scheduler's abnormal-activity rules, a
 * filesystem past the service's full line, an account the kernel held back at
 * its CPU limit, an NFS retransmission -- so this list and those panels cannot
 * disagree. The
 * agent's count leads, as on the panels' plates, with the other classes beside
 * it for scale.
 *
 * Three kinds of nothing stay three: a source with nothing to flag is
 * "checked"; a source that is absent is "not checked", named, never folded
 * into an all-clear; and with the agent class filtered out, the agent rules
 * say they were not applied.
 */
import type { ReactNode } from 'react';
import type { Cls, DangerousRow, FeedsResponse, NodePanel, RiskPanel, RiskSection, SecretRow, SlurmPanel } from '../api/types';
import { Panel, slug } from '../components/Panel';
import { go } from '../lib/cites';
import type { Arrival } from '../lib/cites';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { fint, fmt, lagStr, pc } from '../lib/format';
import { useNodeWords } from '../lib/nodes';
import { pick } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { RULE_LBL } from '../lib/slurmRules';

interface Flag {
  key: string;
  /** data: the numbers below are not current; the rest: something to act on */
  kind: 'data' | 'risk' | 'slurm' | 'node';
  what: ReactNode;
  note?: string;
  /** the per-class split, agent first */
  by?: Partial<Record<string, number>>;
  to: Arrival;
  where: string;
}

const at = (tab: string, anchor?: string): Arrival => ({ patch: { tab }, anchor, find: [] });
const plural = (n: number, one: string, many = one + 's') => `${fint(n)} ${n === 1 ? one : many}`;

export function Attention({ P, cls, feeds }: { P: Panels; cls: Cls[]; feeds: FeedsResponse | null }) {
  const nodes = useNodeWords();
  const flags: Flag[] = [];
  const checked: string[] = [];
  const unchecked: string[] = [];
  const agent = cls.includes('agent');
  const absent = (p: { _present?: boolean; _waiting?: boolean } | undefined) => !p || p._present === false;

  // ---- the data itself: a stale or missing feed qualifies everything below
  if (feeds) {
    checked.push('the feeds');
    for (const f of Object.values(feeds)) {
      if (f.status === 'missing' && f.required) {
        flags.push({ key: `feed:${f.name}`, kind: 'data', what: `${f.label} not found`,
          note: `no files where it is expected${f.configured?.length ? `: ${f.configured.join(', ')}` : ''}`,
          to: at('feeds'), where: 'System › Feeds' });
      } else if (f.status === 'empty' && f.required) {
        // found, but nothing in it: every panel below is empty for that reason,
        // which an all-clear above them would hide
        flags.push({ key: `feed:${f.name}`, kind: 'data', what: `${f.label} has no records`,
          note: `its folder is there and holds nothing yet${f.resolved?.length ? `: ${f.resolved.join(', ')}` : ''}`,
          to: at('feeds'), where: 'System › Feeds' });
      } else if (f.lag_s != null && f.max_age_s != null && f.lag_s > f.max_age_s) {
        flags.push({ key: `feed:${f.name}`, kind: 'data', what: `${f.label} is ${lagStr(f.lag_s)} behind`,
          note: `past its own limit of ${lagStr(f.max_age_s)}: what it feeds is not current`,
          to: at('feeds'), where: 'System › Feeds' });
      }
    }
  } else {
    unchecked.push('the feeds (no report yet)');
  }

  // ---- the risk rules: the agent's hits, the other classes beside them
  const risk = pick<RiskPanel>(P, 'risk', 'ebpfm');
  if (absent(risk)) {
    unchecked.push('the risk rules (no risk panel)');
  } else if (!agent) {
    unchecked.push('the risk rules (the agent class is filtered out)');
  } else {
    checked.push('the risk rules');
    const sections: RiskSection[] = risk.sections ?? [];
    const sum = (rows: { classes?: Partial<Record<string, number>> }[]) => {
      const by: Record<string, number> = {};
      for (const r of rows) for (const [c, n] of Object.entries(r.classes ?? {})) by[c] = (by[c] ?? 0) + (n ?? 0);
      return by;
    };
    for (const sc of sections) {
      if (sc.kind === 'login_compute') continue;      // no rule flags it: it is a share, not a hit
      const want = new Set(sc.rules);
      const rows: (DangerousRow | SecretRow)[] = sc.kind === 'credentials'
        ? (risk.secrets?.rows ?? []).filter((r) => want.has(r.id))
        : (risk.dangerous?.rows ?? []).filter((r) => want.has(r.id));
      const by = sum(rows);
      if (!(by.agent > 0)) continue;
      const top = rows
        .filter((r) => (r.classes?.agent ?? 0) > 0)
        .sort((a, b) => (b.classes?.agent ?? 0) - (a.classes?.agent ?? 0))
        .slice(0, 3)
        .map((r) => `${'command' in r ? r.command : r.pattern} ×${fint(r.classes?.agent ?? 0)}`);
      flags.push({
        key: `risk:${sc.id}`, kind: 'risk',
        what: sc.kind === 'credentials'
          ? `${sc.title}: ${plural(by.agent, 'match', 'matches')} on agent command lines`
          : `${sc.title}: agents ran ${plural(by.agent, 'such command')}`,
        note: sc.kind === 'credentials' ? `${top.join(' · ')} · the values are never shown` : top.join(' · '),
        by, to: at('risk', slug(sc.title)), where: `Risk › Site rules › ${sc.title}`,
      });
    }
  }

  // ---- the scheduler: the agent's sessions that broke one of its rules. One
  // judgment, the service's (reducers/slurm.py), here as on HPC's own table:
  // polling the queue is how an agent waits for its job, and one refusal is
  // not a pattern, so both are counted on HPC and flagged only by a rule.
  // Flagging every poll here once read "5 sessions polled" above an HPC table
  // that said no session broke a rule.
  const sp = pick<SlurmPanel>(P, 'slurm', 'ebpfm');
  if (absent(sp)) {
    unchecked.push('the scheduler (no Slurm panel)');
  } else if (!agent) {
    unchecked.push('the scheduler (the agent class is filtered out)');
  } else if (!sp.abnormal) {
    unchecked.push('the scheduler (the service judges no rule)');
  } else {
    checked.push('the scheduler');
    const by = sp.by_class ?? {};
    const a = by.agent;
    const broke = a?.abnormal_sessions ?? 0;
    if (a && broke > 0) {
      const rules = Object.entries(a.abnormal_by ?? {})
        .filter(([, n]) => (n ?? 0) > 0)
        .map(([id, n]) => `${RULE_LBL[id] ?? id} ×${fint(n)}`);
      flags.push({
        key: 'slurm:abnormal', kind: 'slurm',
        what: `${plural(broke, 'agent session')} broke a rule at the scheduler`,
        note: rules.join(' · '),
        by: Object.fromEntries(Object.entries(by).map(([c, b]) => [c, b?.abnormal_sessions ?? 0])),
        to: at('slurm', 'sec-abnormal-slurm-activity'), where: 'Usage › HPC › Abnormal Slurm activity',
      });
    }
  }

  // ---- the nodes themselves: no class here, they are the node's
  const node = pick<NodePanel>(P, 'node', 'ebpf_node');
  const health = at('resources', 'sec-node-health');
  const healthWhere = `Usage › ${nodes.Many} › ${nodes.One} health`;
  if (absent(node) || !(node.hosts?.length)) {
    unchecked.push(`${nodes.one} health (no node-tier snapshot)`);
  } else {
    checked.push(`${nodes.one} health`);
    const full = (node.disk ?? []).filter((d) => d.full);
    if (full.length) {
      flags.push({
        key: 'node:full', kind: 'node', what: `${plural(full.length, 'local filesystem')} full`,
        note: full.slice(0, 3).map((d) => `${d.mountpoint} on ${d.host}: ${pc(d.used_pct)} space, ${pc(d.inode_used_pct)} file slots`).join(' · '),
        to: health, where: healthWhere,
      });
    }
    const held = (node.hosts ?? []).flatMap((h) => (h.per_user ?? [])
      .filter((u) => (u.throttled_delta_s ?? 0) > 0).map((u) => ({ ...u, host: h.host })))
      .sort((a, b) => (b.throttled_delta_s ?? 0) - (a.throttled_delta_s ?? 0));
    if (held.length) {
      flags.push({
        key: 'node:held', kind: 'node', what: `${plural(held.length, 'account')} held back at a CPU limit`,
        note: held.slice(0, 3).map((u) => `${u.user} on ${u.host}: ${fmt(u.throttled_delta_s)} s`).join(' · '),
        to: health, where: healthWhere,
      });
    }
    const retr = (node.nfs ?? []).filter((m) => (m.rd_retrans ?? 0) + (m.wr_retrans ?? 0) > 0);
    if (retr.length) {
      flags.push({
        key: 'node:nfs', kind: 'node', what: `NFS retransmissions on ${plural(retr.length, 'mount')}`,
        note: retr.slice(0, 3).map((m) => `${m.mount} on ${m.host}: ${fint((m.rd_retrans ?? 0) + (m.wr_retrans ?? 0))}`).join(' · '),
        to: health, where: healthWhere,
      });
    }
  }

  const shownCls: string[] = cls;
  return (
    <Panel
      title="Needs attention"
      id="sec-needs-attention"
      span
      header={
        // green only for a full check that found nothing: with a source not
        // checked, "nothing flagged" is about part of the page, and says so
        flags.length ? (
          <span className="pill stale">{`${fint(flags.length)} flagged`}</span>
        ) : unchecked.length ? (
          <span className="pill" data-tip={`not checked: ${unchecked.join(', ')}`}>
            nothing flagged in what was checked
          </span>
        ) : (
          <span className="pill ok">nothing flagged</span>
        )
      }
    >
      {flags.length > 0 && (
        <ul className="attn">
          {flags.map((f) => (
            <li key={f.key}>
              <i className={`attn-dot ${f.kind}`} aria-hidden="true" />
              <span className="attn-what">{f.what}</span>
              <button className="linkbtn attn-go" onClick={() => go(f.to, f.where)} data-tip={`open ${f.where}`}>
                see {'→'}
              </button>
              {(f.note || f.by) && (
                <span className="attn-note">
                  {f.by && (
                    <span className="u-chips" style={{ marginRight: 8 }}>
                      {shownCls.filter((c) => f.by![c] != null).map((c) => (
                        <span key={c} className="u-chip">
                          <i className="u-dot" style={{ background: CLS_COL[c] }} />
                          {CLS_LBL[c] ?? c} <b>{fint(f.by![c])}</b>
                        </span>
                      ))}
                    </span>
                  )}
                  {f.note}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="attn-foot">
        {checked.length > 0 && <>checked: {checked.join(', ')}</>}
        {checked.length > 0 && unchecked.length > 0 && ' · '}
        {unchecked.length > 0 && <>not checked: {unchecked.join(', ')}</>}
      </div>
    </Panel>
  );
}
