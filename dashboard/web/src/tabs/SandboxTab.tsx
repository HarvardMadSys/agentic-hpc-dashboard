/* Sandbox -- two INDEPENDENT axes, and the ancestry matrix.
 *
 * `sandbox` and `approval_mode` are different questions: an agent can be
 * confined and still be running with approvals bypassed. They are therefore
 * never crossed into a single "autonomy" score here; each gets its own chart.
 *
 * Four sandbox states, not two. `denied` is the collector failing to read the
 * field, NOT a process running unconfined -- folding it into `unsandboxed`
 * would deflate the sandbox rate for every user except the collector's owner.
 * It is drawn in a different hue and hatched.
 *
 * Both grains are shown: per event and per tree. One long-lived sandboxed root
 * emitting a million events is one tree, and the two answers differ.
 */
import type { Cls, Sandbox } from '../api/types';
import { StackRow } from '../charts/primitives';
import { Basis, Legend, Panel } from '../components/Panel';
import { Coverage } from '../components/Coverage';
import { Plate } from '../components/Plate';
import { CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import type { Panels } from '../lib/panels';

const SANDBOX_ORDER = ['sandboxed', 'unsandboxed', 'denied', 'unknown'];
const APPROVAL_ORDER = ['supervised', 'bypassed', 'unknown'];

/** `denied` gets its own hue AND a hatch; `unknown` is dotted. */
const SB_COL: Record<string, string> = {
  sandboxed: 'var(--s5)',
  unsandboxed: 'var(--s6)',
  denied: 'url(#hatchDenied)',
  unknown: 'url(#hatchUnknown)',
};
const SB_LEGEND_COL: Record<string, string> = {
  sandboxed: 'var(--s5)',
  unsandboxed: 'var(--s6)',
  denied: 'var(--sg)',
  unknown: 'var(--edge)',
};
const AP_COL: Record<string, string> = {
  supervised: 'var(--s2)',
  bypassed: 'var(--crit)',
  unknown: 'url(#hatchUnknown)',
};
const AP_LEGEND_COL: Record<string, string> = {
  supervised: 'var(--s2)',
  bypassed: 'var(--crit)',
  unknown: 'var(--edge)',
};

function axisBlock(
  by: Record<string, Record<string, number>> | undefined,
  have: string[],
  order: string[],
  col: Record<string, string>,
  grain: string,
) {
  if (!by) return null;
  return have.map((c) => {
    const m = by[c] ?? {};
    const tot = order.reduce((a, s) => a + (m[s] ?? 0), 0);
    if (!tot) return null;
    return (
      <div key={c}>
        <div className="eyebrow" style={{ margin: '7px 0 4px' }}>
          {CLS_LBL[c] ?? c} {'·'} {fint(tot)} {grain}
        </div>
        <StackRow
          segs={order.map((s) => ({ k: `${s} (${grain})`, v: m[s] ?? 0, c: col[s] }))}
          W={520}
          H={20}
        />
      </div>
    );
  });
}

export function SandboxTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const sb = pick<Sandbox>(P, 'sandbox', 'ebpfm');
  const sbStates = sb.states?.sandbox?.length ? sb.states.sandbox : SANDBOX_ORDER;
  const apStates = sb.states?.approval?.length ? sb.states.approval : APPROVAL_ORDER;
  const evHave = shown(sb.by_class_events, cls);
  const trHave = shown(sb.by_class_trees, cls);
  const anc = sb.ancestry_matrix ?? [];

  const pctOf = (m: Record<string, number> | undefined, s: string) => {
    if (!m) return null;
    const tot = Object.values(m).reduce((a, b) => a + b, 0);
    return tot ? (100 * (m[s] ?? 0)) / tot : null;
  };

  return (
    <>
      <div className="plates">
        <Plate
          k="sandboxed · % of events"
          total={null}
          stripe="var(--s5)"
          by={Object.fromEntries(evHave.map((c) => [c, pctOf(sb.by_class_events?.[c], 'sandboxed')]))}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          note="per class; `denied` is excluded from the numerator, not counted as unsandboxed"
        />
        <Plate
          k="sandboxed · % of trees"
          total={null}
          stripe="var(--s5)"
          by={Object.fromEntries(trHave.map((c) => [c, pctOf(sb.by_class_trees?.[c], 'sandboxed')]))}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          note="the tree grain de-weights one loud long-lived root"
        />
        <Plate
          k="denied · % of events"
          total={null}
          stripe="var(--sg)"
          by={Object.fromEntries(evHave.map((c) => [c, pctOf(sb.by_class_events?.[c], 'denied')]))}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          note="unreadable, not unconfined — this is instrument reach, not behaviour"
        />
        <Plate
          k="bypassed · % of trees"
          total={null}
          stripe="var(--crit)"
          by={Object.fromEntries(
            trHave.map((c) => [c, pctOf(sb.approval_by_class_trees?.[c], 'bypassed')]),
          )}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          note="approval is an axis of its own: a confined tree can still be bypassed"
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Sandbox state — four states, two grains"
          p={sb}
          empty={!evHave.length && !trHave.length}
          emptyWhat="no sandbox breakdown for the selection"
          basis={
            <Basis
              items={[
                ...Object.entries(sb.notes ?? {}),
                ['sandbox_src', sb.sandbox_src],
                ['states', sbStates.join(' · ')],
              ]}
            />
          }
        >
          <div className="eyebrow">per event</div>
          {axisBlock(sb.by_class_events, evHave, sbStates, SB_COL, 'events')}
          <div className="eyebrow" style={{ marginTop: 8 }}>
            per tree
          </div>
          {axisBlock(sb.by_class_trees, trHave, sbStates, SB_COL, 'trees')}
          <Legend items={sbStates.map((s) => ({ k: s, c: SB_LEGEND_COL[s] ?? 'var(--sg)' }))} />
        </Panel>

        <Panel
          title="Approval mode — an independent axis"
          p={sb}
          empty={
            !shown(sb.approval_by_class_events, cls).length &&
            !shown(sb.approval_by_class_trees, cls).length
          }
          emptyWhat="no approval breakdown for the selection"
        >
          <div className="eyebrow">per event</div>
          {axisBlock(
            sb.approval_by_class_events,
            shown(sb.approval_by_class_events, cls),
            apStates,
            AP_COL,
            'events',
          )}
          <div className="eyebrow" style={{ marginTop: 8 }}>
            per tree
          </div>
          {axisBlock(
            sb.approval_by_class_trees,
            shown(sb.approval_by_class_trees, cls),
            apStates,
            AP_COL,
            'trees',
          )}
          <Legend items={apStates.map((s) => ({ k: s, c: AP_LEGEND_COL[s] ?? 'var(--sg)' }))} />
        </Panel>

        <Panel
          title="Ancestry × sandbox"
          p={sb}
          span
          empty={!anc.length}
          emptyWhat="the feed carried no ancestry rows"
          basis={
            <Basis
              items={[
                ['reading', 'bwrap + unsandboxed = a capability probe; bwrap + sandboxed = real confinement'],
                ['sandbox_detail', sb.sandbox_detail],
                ['agent_only', sb.agent_only],
              ]}
            />
          }
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>ancestry</th>
                  <th>sandbox</th>
                  <th className="num">n</th>
                  <th className="num">D-state wait</th>
                  <th className="num">coverage</th>
                  <th>reading</th>
                </tr>
              </thead>
              <tbody>
                {anc.map((a, i) => (
                  <tr key={a.ancestry + a.sandbox + i}>
                    <td className="mono">{a.ancestry}</td>
                    <td>
                      <span
                        className="sev"
                        style={{
                          borderColor: SB_LEGEND_COL[a.sandbox] ?? 'var(--edge)',
                          color:
                            a.sandbox === 'unsandboxed'
                              ? '#8a6410'
                              : a.sandbox === 'sandboxed'
                                ? '#166b50'
                                : 'var(--muted)',
                        }}
                      >
                        {a.sandbox}
                      </span>
                    </td>
                    <td className="num">{fint(a.n)}</td>
                    <td className="num">
                      {a.dstate_wait_s == null ? (
                        <span className="nm">not measured</span>
                      ) : (
                        fmt(a.dstate_wait_s) + ' s'
                      )}
                    </td>
                    <td className="num">
                      <Coverage pct={a.dstate_coverage_pct} n={a.n} />
                    </td>
                    <td>{a.reading}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Where the sandbox reading came from"
          p={sb}
          empty={!Object.keys(sb.sandbox_src ?? {}).length}
          emptyWhat="no source breakdown"
        >
          <StackRow
            segs={Object.entries(sb.sandbox_src ?? {}).map(([k, v], i) => ({
              k,
              v,
              c: ['var(--s2)', 'var(--s5)', 'var(--s6)', 'var(--s4)', 'var(--sg)'][i % 5],
            }))}
            W={520}
            H={20}
          />
          <Legend
            items={Object.keys(sb.sandbox_src ?? {}).map((k, i) => ({
              k,
              c: ['var(--s2)', 'var(--s5)', 'var(--s6)', 'var(--s4)', 'var(--sg)'][i % 5],
            }))}
          />
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>source</th>
                  <th className="num">n</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(sb.sandbox_src ?? {}).map(([k, v]) => (
                  <tr key={k}>
                    <td className="mono">{k}</td>
                    <td className="num">{fint(v)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </>
  );
}
