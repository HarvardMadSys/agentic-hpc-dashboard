/* Trajectories.
 *
 * The rank switcher only REORDERS: the events column and the cpu column are
 * both always present, because ranking by one and showing only that one is how
 * a poll loop gets mistaken for work (and vice versa).
 *
 * `dominant_user_share_pct` is shown for the active rank, unmissably, because
 * the top list is frequently one user's campaign rather than a cluster pattern.
 */
import type { Async } from '../api/hooks';
import type { Cls, TrajRank, TrajRow, Trajectories } from '../api/types';
import { TRAJ_RANKS } from '../api/types';
import { Ribbon, StackRow } from '../charts/primitives';
import { Basis, Legend, Panel } from '../components/Panel';
import { Segmented } from '../components/ClassFilter';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, mb, pc } from '../lib/format';
import { pcol, plbl, purposeLegend } from '../lib/purposes';
import { absent } from '../lib/panels';

const RANK_LBL: Record<TrajRank, string> = {
  events: 'events',
  cpu_s: 'cpu_s',
  distinct_tools: 'distinct tools',
  chain_runs: 'chain runs',
};

/** `pgrep x120 > git > rg x3 ...` -- the tool tokens as text, bounded. */
function ToolTokens({ rle, truncated }: { rle: [string, number][]; truncated: boolean }) {
  const MAX = 14;
  const head = rle.slice(0, MAX);
  return (
    <span className="rle">
      {head.map(([t, n], i) => (
        <span key={i}>
          {i > 0 && <span className="sep">{' › '}</span>}
          <b>{t}</b>
          {n > 1 && <span> {'×'}{n}</span>}
        </span>
      ))}
      {(rle.length > MAX || truncated) && (
        <span className="sep" data-tip={truncated ? 'the backend truncated this chain' : `${rle.length - MAX} more runs`}>
          {' …'}
        </span>
      )}
    </span>
  );
}

export function TrajectoriesTab({
  traj,
  rank,
  setRank,
  cls,
}: {
  traj: Async<Trajectories>;
  rank: TrajRank;
  setRank: (r: TrajRank) => void;
  cls: Cls[];
}) {
  const t = traj.data;
  const env = t ?? (absent('ebpfm', traj.error ?? 'no trajectory payload yet') as unknown as Trajectories);
  const rows = t?.rows ?? {};
  const order = t?.ranks?.[rank] ?? [];
  // The share is per-ranking: ranking by events while showing the cpu_s share
  // would understate exactly the concentration this plate exists to flag.
  const domShare = t?.dominant_user_share_pct?.[rank] ?? null;
  const listed: TrajRow[] = order
    .map((sid) => rows[sid])
    .filter((r): r is TrajRow => !!r)
    .filter((r) => !r.actor3 || cls.includes(r.actor3 as Cls));

  const counts = t?.by_class_counts ?? {};
  const keyTypes = t?.by_key_type ?? {};

  return (
    <>
      <div className="plates">
        <Plate
          k="sessions in buffer"
          total={Object.keys(rows).length || null}
          stripe="var(--s1)"
          by={Object.fromEntries(Object.keys(counts).map((c) => [c, counts[c] ?? null]))}
          note={`${fint(listed.length)} shown at rank ${RANK_LBL[rank]}`}
        />
        <Plate
          k="events in listed sessions"
          total={listed.length ? listed.reduce((a, r) => a + (r.n_events || 0), 0) : null}
          stripe="var(--s4)"
          by={Object.fromEntries(
            cls.map((c) => [
              c,
              listed.filter((r) => r.actor3 === c).reduce((a, r) => a + (r.n_events || 0), 0) || null,
            ]),
          )}
        />
        <Plate
          k="cpu seconds in listed sessions"
          total={listed.length ? listed.reduce((a, r) => a + (r.cpu_s || 0), 0) : null}
          stripe="var(--s6)"
          by={Object.fromEntries(
            cls.map((c) => [
              c,
              listed.filter((r) => r.actor3 === c).reduce((a, r) => a + (r.cpu_s || 0), 0) || null,
            ]),
          )}
          fmtFn={(n) => fmt(n)}
          unit="s"
        />
        {/* The single most important caveat on this tab, given plate weight. */}
        <Plate
          k={`dominant user share · rank ${RANK_LBL[rank]}`}
          total={domShare}
          stripe={(domShare ?? 0) >= 50 ? 'var(--crit)' : 'var(--s2)'}
          by={{}}
          classes={[]}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          note="of this ranking's volume held by its single largest user"
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Ranked sessions"
          p={env}
          span
          empty={!listed.length}
          emptyWhat={
            Object.keys(rows).length
              ? 'no session at this rank matches the class filter'
              : 'the trajectory buffer is empty'
          }
          header={
            <Segmented
              label="rank by"
              value={rank}
              options={TRAJ_RANKS.map((r) => ({
                k: r,
                lbl: RANK_LBL[r],
                tip: `reorder by ${RANK_LBL[r]} — every column stays visible`,
              }))}
              onChange={setRank}
            />
          }
          basis={
            <Basis
              items={[
                ['rank_meaning', t?.rank_meaning],
                ['basis', t?.basis],
                ['window', t?.window],
                ['buffer', t?.buffer],
                ['ribbon', 'segment width is the run length; colour is the purpose run'],
              ]}
            />
          }
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>session</th>
                  <th>class</th>
                  <th>user</th>
                  <th>host</th>
                  {/* both always visible: the switcher reorders, it never hides */}
                  <th className="num">events</th>
                  <th className="num">cpu s</th>
                  <th className="num">tools</th>
                  <th className="num">chains</th>
                  <th className="num">wall</th>
                  <th className="num">peak RSS</th>
                  <th className="num">rd / wr</th>
                  <th>sandbox</th>
                  <th>approval</th>
                </tr>
              </thead>
              <tbody>
                {listed.map((r) => (
                  <tr key={r.sid}>
                    <td
                      className="mono"
                      style={{ fontSize: '10.5px', maxWidth: 190, wordBreak: 'break-all' }}
                      data-tip={`${r.sid}\nkey_type ${r.key_type}${
                        r.agent_type ? '\nagent_type ' + r.agent_type : ''
                      }`}
                    >
                      {r.sid}
                    </td>
                    <td>
                      <span
                        className="dot"
                        style={{
                          background: CLS_COL[r.actor3 ?? 'unlabeled'],
                          marginRight: 5,
                        }}
                      />
                      {CLS_LBL[r.actor3 ?? 'unlabeled'] ?? r.actor3}
                    </td>
                    <td className="mono" data-tip={(r.users ?? []).length > 1 ? `${r.n_users} users: ${(r.users ?? []).join(", ")}` : (r.pids ?? []).length ? `pids ${(r.pids ?? []).slice(0, 4).join(", ")}` : undefined}>{r.user ?? (r.users ?? [])[0] ?? DASH}{(r.users ?? []).length > 1 && (<span style={{ color: "var(--muted)" }}>{` +${(r.n_users ?? 1) - 1}`}</span>)}</td>
                    <td className="mono" style={{ color: 'var(--muted)' }}>
                      {(r.host || '').replace('login', '') || DASH}
                    </td>
                    <td
                      className="num"
                      style={rank === 'events' ? { fontWeight: 600 } : undefined}
                    >
                      {fint(r.n_events)}
                    </td>
                    <td className="num" style={rank === 'cpu_s' ? { fontWeight: 600 } : undefined}>
                      {fmt(r.cpu_s)}
                    </td>
                    <td
                      className="num"
                      style={rank === 'distinct_tools' ? { fontWeight: 600 } : undefined}
                    >
                      {fint(r.distinct_tools)}
                    </td>
                    <td
                      className="num"
                      style={rank === 'chain_runs' ? { fontWeight: 600 } : undefined}
                    >
                      {fint(r.chain_runs)}
                    </td>
                    <td className="num">{dur(r.wall_s)}</td>
                    <td className="num">{mb(r.peak_rss_mb)}</td>
                    <td className="num">
                      {mb(r.io_rd_mb)} / {mb(r.io_wr_mb)}
                    </td>
                    <td>
                      <span className="sev low">{r.sandbox ?? 'unknown'}</span>
                    </td>
                    <td>
                      {r.approval === 'bypassed' || r.autonomous ? (
                        <span className="sev HIGH">{r.approval ?? 'bypass'}</span>
                      ) : (
                        <span className="sev low">{r.approval ?? 'unknown'}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Tool sequence — ribbon and tokens"
          p={env}
          span
          empty={!listed.length}
          emptyWhat="no session to draw"
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>session</th>
                  <th className="num">events</th>
                  <th className="num">cpu s</th>
                  <th style={{ minWidth: 300 }}>purpose over the sequence</th>
                  <th style={{ minWidth: 300 }}>tool runs</th>
                </tr>
              </thead>
              <tbody>
                {listed.slice(0, 20).map((r) => (
                  <tr key={r.sid}>
                    <td className="mono" style={{ fontSize: '10px', maxWidth: 150, wordBreak: 'break-all' }}>
                      {r.sid}
                    </td>
                    <td className="num">{fint(r.n_events)}</td>
                    <td className="num">{fmt(r.cpu_s)}</td>
                    <td>
                      {r.purpose_rle?.length ? (
                        <Ribbon
                          rle={r.purpose_rle}
                          colour={pcol}
                          label={(k, n) => `${plbl(k)} ×${n}`}
                          W={320}
                          H={13}
                        />
                      ) : (
                        <span className="nm">no purpose run-lengths</span>
                      )}
                    </td>
                    <td>
                      {r.tool_rle?.length ? (
                        <ToolTokens rle={r.tool_rle} truncated={r.chain_truncated} />
                      ) : (
                        <span className="nm">no tool run-lengths</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Legend items={purposeLegend()} />
        </Panel>

        <Panel
          title="Buffer composition"
          p={env}
          empty={!Object.keys(counts).length && !Object.keys(keyTypes).length}
          emptyWhat="no composition counts"
        >
          <div className="eyebrow">by class</div>
          <StackRow
            segs={Object.keys(counts).map((c) => ({
              k: CLS_LBL[c] ?? c,
              v: counts[c] ?? 0,
              c: CLS_COL[c] ?? 'var(--sg)',
            }))}
            W={520}
            H={20}
          />
          <Legend
            items={Object.keys(counts).map((c) => ({
              k: CLS_LBL[c] ?? c,
              c: CLS_COL[c] ?? 'var(--sg)',
            }))}
          />
          <div className="eyebrow" style={{ marginTop: 8 }}>
            by session key type
          </div>
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>key_type</th>
                  <th className="num">sessions</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(keyTypes).map(([k, v]) => (
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
