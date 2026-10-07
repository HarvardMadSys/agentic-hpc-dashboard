/* Sporadicity -- where a session was launched vs where it actually reached.
 *
 * The launch directory is the anchor; every directory a session's argv names
 * (or a cwd it was read in) is placed in a zone relative to it, and the blast
 * radius is how many levels up from the anchor the furthest one sits.
 *
 * Two rules the payload forces:
 *
 * - Argv evidence is a FLOOR. A file a program opens on its own never reaches
 *   argv, so nothing here says "this session touched only X". The coverage
 *   panel names what was dropped and what could not be resolved.
 * - The agent's own state (~/.claude, its /tmp/claude-* scratchpad) is drawn in
 *   the zone mix but is never reach: the plates, the radius and the rankings
 *   leave it out, or every Claude Code session would read as escaping.
 */
import { useState } from 'react';
import type { Cls, ReachClassBlock, ReachRank, ReachRow, Sporadicity } from '../api/types';
import { REACH_RANKS } from '../api/types';
import { GroupBars, StackRow } from '../charts/primitives';
import { Segmented } from '../components/ClassFilter';
import { Basis, Legend, Panel } from '../components/Panel';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, pc } from '../lib/format';
import { pick, shown, winLabel } from '../lib/panels';
import type { Panels } from '../lib/panels';

const ZONE_ORDER = [
  'cwd',
  'home',
  'home-dot',
  'tool-state',
  'tmp',
  'shared',
  'other-home',
  'system',
  'other',
];
const ZONE_LBL: Record<string, string> = {
  cwd: 'launch dir',
  home: 'own home',
  'home-dot': 'home dot-dirs',
  'tool-state': 'agent state',
  tmp: 'tmp',
  shared: 'shared storage',
  'other-home': "another user's home",
  system: 'system',
  other: 'elsewhere',
};
/** Two short lines per zone: nine groups share one axis, and a one-line label
 *  as long as "another user's home" runs into its neighbours. */
const ZONE_AXIS: Record<string, [string, string]> = {
  cwd: ['launch', 'dir'],
  home: ['own', 'home'],
  'home-dot': ['home', 'dot-dirs'],
  'tool-state': ['agent', 'state'],
  tmp: ['tmp', ''],
  shared: ['shared', 'storage'],
  'other-home': ['other', 'user home'],
  system: ['system', ''],
  other: ['else-', 'where'],
};
/** Crimson stays reserved for `agent`; another user's home takes the alarm red. */
const ZONE_COL: Record<string, string> = {
  cwd: 'var(--s5)',
  home: 'var(--s2)',
  'home-dot': 'var(--s8)',
  'tool-state': 'var(--edge)',
  tmp: 'var(--s6)',
  shared: 'var(--s4)',
  'other-home': 'var(--crit)',
  system: 'var(--s7)',
  other: 'var(--sg)',
};
const RADIUS_K2: Record<string, string> = {
  '0': 'stayed',
  '1': 'sibling',
  '2': 'cousin',
  '3+': 'further',
  root: 'left tree',
};
const RANK_LBL: Record<ReachRank, string> = {
  radius: 'radius',
  writes: 'writes outside',
  dirs: 'dirs outside',
  other_home: 'other homes',
};

const zl = (z: string) => ZONE_LBL[z] ?? z;
const zc = (z: string) => ZONE_COL[z] ?? 'var(--sg)';
const sum = (m: Partial<Record<string, number>> | undefined) =>
  Object.values(m ?? {}).reduce<number>((a, b) => a + (b ?? 0), 0);
/** A share of a class's anchored sessions, or null when it has none. */
const ofSessions = (b: ReachClassBlock | undefined, n: number | undefined) =>
  b && b.sessions ? (100 * (n ?? 0)) / b.sessions : null;
const pcOrDash = (n: number | null | undefined) => (n == null ? DASH : pc(n));

function ZoneChip({ z, n }: { z: string; n?: number }) {
  return (
    <span className="zone" data-tip={zl(z)}>
      <i className="dot" style={{ background: zc(z) }} />
      {zl(z)}
      {n != null && <b> {fint(n)}</b>}
    </span>
  );
}

function ClassMix({
  have,
  B,
  field,
  zones,
  unit,
}: {
  have: string[];
  B: Record<string, ReachClassBlock>;
  field: 'refs' | 'writes';
  zones: string[];
  unit: string;
}) {
  return (
    <>
      {have.map((c) => {
        const m = B[c]?.[field] ?? {};
        const tot = sum(m);
        return (
          <div key={c}>
            <div className="eyebrow" style={{ margin: '7px 0 4px' }}>
              {CLS_LBL[c] ?? c} {'·'} {fint(tot)} {unit}
            </div>
            {tot ? (
              <StackRow
                segs={zones.map((z) => ({ k: `${zl(z)} (${unit})`, v: m[z] ?? 0, c: zc(z) }))}
                W={520}
                H={20}
              />
            ) : (
              <span className="nm">none</span>
            )}
          </div>
        );
      })}
    </>
  );
}

function Where({ r }: { r: ReachRow }) {
  return (
    <div className="where">
      {r.top_outside.map((d) => (
        <div key={d.dir} data-tip={`${zl(d.zone)} · ${d.refs} mention(s), ${d.writes} write(s)${
          d.radius != null ? ` · ${d.radius} level(s) up` : ''}`}>
          <i className="dot" style={{ background: zc(d.zone) }} />
          {d.writes > 0 && <span className="sev HIGH">W</span>}
          {d.cd && <span className="sev low">cd</span>}
          <span className="mono">{d.dir}</span>
        </div>
      ))}
      {r.more_outside > 0 && <div className="nm">+{r.more_outside} more</div>}
    </div>
  );
}

export function SporadicityTab({
  P,
  cls,
  rank,
  setRank,
}: {
  P: Panels;
  cls: Cls[];
  rank: ReachRank;
  setRank: (r: ReachRank) => void;
}) {
  const sp = pick<Sporadicity>(P, 'sporadicity', 'ebpfm');
  const [writesOnly, setWritesOnly] = useState<'any' | 'writes'>('any');
  const B = sp.by_class ?? {};
  const have = shown(B, cls);
  const zones = sp.zones?.length ? sp.zones : ZONE_ORDER;
  const buckets = sp.radius_buckets?.length ? sp.radius_buckets : ['0', '1', '2', '3+', 'root'];
  const win = winLabel(sp);

  const rows = sp.rows ?? {};
  const listed: ReachRow[] = (sp.ranks?.[rank] ?? [])
    .map((sid) => rows[sid])
    .filter((r): r is ReachRow => !!r)
    .filter((r) => !r.actor3 || cls.includes(r.actor3 as Cls));
  const dom = sp.dominant_user_share_pct?.[rank] ?? null;
  const ranked = sp.ranks?.[rank]?.length ?? 0;
  const qualifying = sp.rank_totals?.[rank] ?? ranked;
  const dirs = (sp.dirs ?? []).filter((d) => cls.some((c) => (d.classes[c] ?? 0) > 0));

  const cov = sp.coverage;
  const covHave = shown(cov?.records, cls);
  const tok = (c: string, k: string) => cov?.tokens?.[c]?.[k] ?? 0;

  const series = have.map((c) => ({ k: c, lbl: CLS_LBL[c] ?? c, c: CLS_COL[c] ?? 'var(--sg)' }));
  const zoneField = writesOnly === 'writes' ? 'write_sessions_by_zone' : 'sessions_by_zone';

  return (
    <>
      <div className="plates">
        <Plate
          k={`% of sessions reaching outside ${win}`}
          total={null}
          stripe="var(--s4)"
          by={Object.fromEntries(have.map((c) => [c, ofSessions(B[c], B[c]?.sessions_outside)]))}
          fmtFn={pcOrDash}
          note="named a directory outside its launch dir — the agent's own state excluded"
          tip="share of sessions with a launch directory; trees launched at / are counted apart, in coverage"
        />
        <Plate
          k="wrote outside · % of sessions"
          total={null}
          stripe="var(--crit)"
          by={Object.fromEntries(
            have.map((c) => [c, ofSessions(B[c], B[c]?.sessions_write_outside)]),
          )}
          fmtFn={pcOrDash}
          note="handed a path outside the launch dir to rm, mv, cp, mkdir, >, sed -i …"
        />
        <Plate
          k="left the launch tree · % of sessions"
          total={null}
          stripe="var(--s6)"
          by={Object.fromEntries(have.map((c) => [c, ofSessions(B[c], B[c]?.radius?.root)]))}
          fmtFn={pcOrDash}
          note="its furthest reference shares only / with the launch dir"
        />
        <Plate
          k="another user's home · sessions"
          total={have.length ? have.reduce((a, c) => a + (B[c]?.sessions_other_home ?? 0), 0) : null}
          stripe="var(--crit)"
          by={Object.fromEntries(have.map((c) => [c, B[c]?.sessions_other_home ?? 0]))}
          note="named a path under someone else's home root"
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Where references land — by zone"
          p={sp}
          empty={!have.length}
          emptyWhat="no session with a launch directory for the selection"
          basis={
            <Basis
              items={[
                ...Object.entries(sp.zone_meaning ?? {}),
                ['tool-state', sp.notes?.['tool-state']],
                ['dedup', sp.notes?.dedup],
              ]}
            />
          }
        >
          <div className="eyebrow">every mention</div>
          <ClassMix have={have} B={B} field="refs" zones={zones} unit="mentions" />
          <div className="eyebrow" style={{ marginTop: 10 }}>
            writes only
          </div>
          <ClassMix have={have} B={B} field="writes" zones={zones} unit="writes" />
          <Legend items={zones.map((z) => ({ k: zl(z), c: zc(z) }))} />
        </Panel>

        <Panel
          title="Sessions touching each zone"
          p={sp}
          empty={!have.length}
          emptyWhat="no session with a launch directory for the selection"
          header={
            <Segmented
              value={writesOnly}
              options={[
                { k: 'any', lbl: 'any mention', tip: 'a session counts once per zone it named' },
                { k: 'writes', lbl: 'writes', tip: 'a session counts once per zone it wrote into' },
              ]}
              onChange={setWritesOnly}
            />
          }
        >
          <GroupBars
            groups={zones.map((z) => ({
              k: ZONE_AXIS[z]?.[0] ?? z,
              k2: ZONE_AXIS[z]?.[1] || undefined,
              v: Object.fromEntries(have.map((c) => [c, ofSessions(B[c], B[c]?.[zoneField]?.[z])])),
            }))}
            series={series}
            W={560}
            H={220}
            ylabel="% of the class's sessions"
            fmtVal={(v) => pc(v)}
            tickFmt={(v) => `${v}%`}
          />
          <Legend items={series.map((s) => ({ k: s.lbl, c: s.c }))} />
        </Panel>

        <Panel
          title="Blast radius — levels up from the launch dir"
          p={sp}
          span
          empty={!have.length}
          emptyWhat="no session with a launch directory for the selection"
          basis={<Basis items={[['radius', sp.notes?.radius], ['anchor', sp.notes?.anchor]]} />}
        >
          <div className="grid">
            <div>
              <div className="eyebrow">furthest reference</div>
              <GroupBars
                groups={buckets.map((b) => ({
                  k: b,
                  k2: RADIUS_K2[b],
                  v: Object.fromEntries(have.map((c) => [c, ofSessions(B[c], B[c]?.radius?.[b])])),
                }))}
                series={series}
                W={520}
                H={210}
                ylabel="% of the class's sessions"
                fmtVal={(v) => pc(v)}
                tickFmt={(v) => `${v}%`}
                labelTop
              />
            </div>
            <div>
              <div className="eyebrow">furthest write</div>
              <GroupBars
                groups={buckets.map((b) => ({
                  k: b,
                  k2: RADIUS_K2[b],
                  v: Object.fromEntries(
                    have.map((c) => [c, ofSessions(B[c], B[c]?.write_radius?.[b])]),
                  ),
                }))}
                series={series}
                W={520}
                H={210}
                ylabel="% of the class's sessions"
                fmtVal={(v) => pc(v)}
                tickFmt={(v) => `${v}%`}
                labelTop
              />
            </div>
          </div>
          <Legend items={series.map((s) => ({ k: s.lbl, c: s.c }))} />
        </Panel>

        <Panel
          title="Sessions ranked by reach"
          p={sp}
          span
          empty={!listed.length}
          emptyWhat={
            Object.keys(rows).length
              ? 'no session at this rank matches the class filter'
              : 'no session named a directory outside its launch dir'
          }
          header={
            <>
              {qualifying > ranked && (
                <span className="nm" data-tip="the backend lists the top of each ranking only">
                  top {fint(ranked)} of {fint(qualifying)}
                </span>
              )}
              {dom != null && (
                <span
                  className="sev"
                  style={dom >= 50 ? { borderColor: 'var(--crit)', color: 'var(--crit)' } : undefined}
                  data-tip="of this ranking's volume held by its single largest user"
                >
                  top user {pc(dom)}
                </span>
              )}
              <Segmented
                label="rank by"
                value={rank}
                options={REACH_RANKS.map((r) => ({
                  k: r,
                  lbl: RANK_LBL[r],
                  tip: sp.rank_meaning?.[r] ?? RANK_LBL[r],
                }))}
                onChange={setRank}
              />
            </>
          }
          basis={
            <Basis
              items={[
                ['rank_meaning', sp.rank_meaning],
                ['writes', sp.notes?.writes],
                ['sessions', sp.notes?.sessions],
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
                  <th>launch dir</th>
                  <th className="num">radius</th>
                  <th className="num">dirs out</th>
                  <th className="num">writes out</th>
                  <th className="num" data-tip="directories in another user's home">other homes</th>
                  <th>zones</th>
                  <th style={{ minWidth: 320 }}>where</th>
                  <th>sandbox</th>
                  <th>approval</th>
                </tr>
              </thead>
              <tbody>
                {listed.map((r) => (
                  <tr key={r.sid}>
                    <td
                      className="mono"
                      style={{ fontSize: '10.5px' }}
                      data-tip={`${r.sid}\n${fint(r.n_records)} records · wall ${dur(r.wall_s)}`}
                    >
                      {r.user ?? DASH}
                      <span style={{ color: 'var(--muted)' }}>@{(r.host || '').replace('login', '') || DASH}</span>
                      {r.agent_type && <div style={{ color: 'var(--muted)' }}>{r.agent_type}</div>}
                    </td>
                    <td>
                      <span className="dot" style={{ background: CLS_COL[r.actor3 ?? 'unlabeled'], marginRight: 5 }} />
                      {CLS_LBL[r.actor3 ?? 'unlabeled'] ?? r.actor3}
                    </td>
                    <td
                      className="mono"
                      style={{ fontSize: '10.5px', maxWidth: 220, wordBreak: 'break-all' }}
                      data-tip={
                        r.anchor_src === 'root'
                          ? "the agent root's own cwd"
                          : 'the most common cwd in the session (its root was not in the window)'
                      }
                    >
                      {r.anchor}
                    </td>
                    <td className="num" style={rank === 'radius' ? { fontWeight: 600 } : undefined}>
                      {r.root_escape ? 'root' : fint(r.radius)}
                    </td>
                    <td className="num" style={rank === 'dirs' ? { fontWeight: 600 } : undefined}>
                      {fint(r.outside_dirs)}
                    </td>
                    <td className="num" style={rank === 'writes' ? { fontWeight: 600 } : undefined}>
                      {r.outside_writes ? fint(r.outside_writes) : <span className="nm">0</span>}
                    </td>
                    <td className="num" style={rank === 'other_home' ? { fontWeight: 600 } : undefined}>
                      {r.other_home_dirs ? (
                        <span style={{ color: 'var(--crit)' }}>{fint(r.other_home_dirs)}</span>
                      ) : (
                        <span className="nm">0</span>
                      )}
                    </td>
                    <td>
                      {Object.entries(r.zones)
                        .sort((a, b) => ZONE_ORDER.indexOf(a[0]) - ZONE_ORDER.indexOf(b[0]))
                        .map(([z, v]) => (
                          <div key={z}>
                            <ZoneChip z={z} n={v.dirs} />
                          </div>
                        ))}
                    </td>
                    <td>
                      <Where r={r} />
                    </td>
                    <td>
                      <span className="sev low">{r.sandbox ?? 'unknown'}</span>
                    </td>
                    <td>
                      <span className={`sev ${r.approval === 'bypassed' ? 'HIGH' : 'low'}`}>
                        {r.approval ?? 'unknown'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Directories reached from outside their launch dir"
          p={sp}
          span
          empty={!dirs.length}
          emptyWhat="no directory was named from outside its session's launch dir"
          basis={
            <Basis
              items={[
                ['rollup', `paths cut to their first ${sp.rollup_depth ?? 4} components`],
                ['dirs_total', sp.dirs_total],
                ['meaning', 'a directory counts only for sessions launched elsewhere: the same path inside its own session’s launch dir is not reach'],
              ]}
            />
          }
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>directory</th>
                  <th>zone</th>
                  <th className="num">sessions</th>
                  <th className="num">users</th>
                  <th>by class</th>
                  <th className="num">mentions</th>
                  <th className="num">writes</th>
                  <th className="num">cd</th>
                </tr>
              </thead>
              <tbody>
                {dirs.map((d) => (
                  <tr key={d.dir}>
                    <td className="mono" style={{ maxWidth: 380 }}>
                      {d.dir}
                    </td>
                    <td>
                      <ZoneChip z={d.zone} />
                    </td>
                    <td className="num">{fint(d.sessions)}</td>
                    <td className="num">{fint(d.users)}</td>
                    <td className="mono" style={{ fontSize: '10.5px', whiteSpace: 'nowrap' }}>
                      {cls
                        .filter((c) => (d.classes[c] ?? 0) > 0)
                        .map((c) => (
                          <span key={c} style={{ marginRight: 8 }} data-tip={CLS_LBL[c]}>
                            <i className="dot" style={{ background: CLS_COL[c], marginRight: 3 }} />
                            {fint(d.classes[c])}
                          </span>
                        ))}
                    </td>
                    <td className="num">{fint(d.refs)}</td>
                    <td className="num">{d.writes ? fint(d.writes) : <span className="nm">0</span>}</td>
                    <td className="num">{d.cds ? fint(d.cds) : <span className="nm">0</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Coverage — what argv can and cannot show"
          p={sp}
          span
          empty={!covHave.length}
          emptyWhat="no exit records in the window"
          basis={
            <Basis
              items={[
                ['evidence', sp.notes?.evidence],
                ['cwd', sp.notes?.cwd],
                ['dropped', sp.notes?.dropped],
                ['buffer', sp.buffer],
              ]}
            />
          }
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>class</th>
                  <th className="num">records</th>
                  <th className="num" data-tip="records whose cwd was read from /proc, not inherited from the parent">
                    cwd read
                  </th>
                  <th className="num" data-tip="sessions with a launch directory">sessions</th>
                  <th className="num" data-tip="trees launched at / — no directory to measure reach from">
                    launched at /
                  </th>
                  <th className="num">mentions kept</th>
                  <th className="num" data-tip="the command word, and executables under the system bin dirs">
                    program
                  </th>
                  <th className="num" data-tip="/dev, /proc, /sys">pseudo</th>
                  <th className="num" data-tip="Claude Code's shell snapshot and -cwd file">harness</th>
                  <th className="num" data-tip="relative paths resolved against a known base">rel. resolved</th>
                  <th className="num" data-tip="relative paths whose base was an inherited, possibly stale cwd">
                    rel. unresolved
                  </th>
                  <th className="num" data-tip="argv the collector cut; its last word is dropped">argv cut</th>
                </tr>
              </thead>
              <tbody>
                {covHave.map((c) => {
                  const rec = cov?.records?.[c] ?? 0;
                  return (
                    <tr key={c}>
                      <td>
                        <span className="dot" style={{ background: CLS_COL[c], marginRight: 5 }} />
                        {CLS_LBL[c] ?? c}
                      </td>
                      <td className="num">{fint(rec)}</td>
                      <td className="num">{rec ? pc((100 * (cov?.cwd_read?.[c] ?? 0)) / rec) : DASH}</td>
                      <td className="num">{fint(B[c]?.sessions ?? null)}</td>
                      <td className="num">{fint(B[c]?.no_anchor ?? null)}</td>
                      <td className="num">{fint(tok(c, 'kept'))}</td>
                      <td className="num">{fint(tok(c, 'program'))}</td>
                      <td className="num">{fint(tok(c, 'pseudo'))}</td>
                      <td className="num">{fint(tok(c, 'harness'))}</td>
                      <td className="num">{fint(tok(c, 'relative_resolved'))}</td>
                      <td className="num">{fint(tok(c, 'relative_unresolved'))}</td>
                      <td className="num">{fint(cov?.args_truncated?.[c] ?? 0)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="nm" style={{ marginTop: 6 }}>
            {fint(cov?.dedup_dropped ?? null)} shell mentions dropped as duplicates of their own child's
          </div>
          {(sp.buffer?.untracked_records ?? 0) > 0 && (
            <div className="nm" style={{ marginTop: 4, color: 'var(--crimson-ink)' }}>
              {fint(sp.buffer.untracked_records)} records belonged to sessions never tracked — the
              session table was full (reducers.sporadicity.max_sessions); every figure above is
              short by them
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}
