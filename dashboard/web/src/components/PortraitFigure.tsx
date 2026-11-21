/* A portrait figure: one user, drawn from how they use coding agents.
 *
 * It is not a likeness. The service holds no picture of anyone and the names it
 * shows are aliases, so the person at the desk is drawn the same for every user,
 * in the grey this page gives what it does not know; only their posture is
 * measured. What changes is around them, and each part of that is one number on
 * the user's row or one of the service's rules (`rc_dashboard/portraits.py`),
 * said on hover and in the key:
 *
 *   the robots      the most sessions open at once (six drawn at most, the rest
 *                   a +N), coloured by agent and shared out by the window's
 *                   sessions per agent; a lit antenna is a session running now
 *   the laptop lid  where most sessions are driven from (`leverage.mode`)
 *   leaning back    most sessions are driven from a script: no one at a terminal
 *   a padlock       most launches bypassed approval; a shield: auto mode
 *   capsules        the sandboxed trait, round every robot
 *   the shelf       what the agents' actions are for: the top three uses, each
 *                   icon's area its share
 *   the clock       active minutes by hour of day, the hand at the busiest hour
 *   the window      the moon: the overnight trait; the sun: when they work is
 *                   known and the rule does not hold
 *   papers          text the agents wrote: commit messages, PR titles, log
 *                   entries, a sheet each
 *   a mug           the long trait; a stopwatch: the short trait
 *   an hourglass    the poller trait
 *   a server        Slurm jobs submitted, a light each, refused ones in red;
 *                   past ten the lights share them out
 *
 * The service holds every threshold; the only numbers here are drawing caps
 * (six robots, five sheets, ten lights, the smallest icon that reads), and each
 * part that hits one says so on hover. A part whose number is unknown is left out, or drawn as unread where
 * leaving it out would read as none (the calls of an agent the service does not
 * read): never guessed. Every position is a fixed slot, so a user's figure is
 * the same each time it is drawn, and two figures side by side differ only
 * where their numbers do. The colours are the page's tokens: an agent's own
 * colour (lib/classes.ts), a use's phase colour (lib/phases.ts), and a trait's
 * tone (crit, warn, ok).
 */
import { useId } from 'react';
import type { Leverage, Surface, TraitRule, UseKind, UserRow } from '../api/types';
import { AGENT_TYPE_COL } from '../lib/classes';
import { fint } from '../lib/format';
import { USE_VERB, agentLbl, apportion, busiestHour, catCol, hourSpan, leverageHeadline, pctWords, plural, ranked, ruleOf } from '../lib/portraits';
import { SURFACE_LBL } from '../lib/surfaces';
import { table } from '../lib/lookup';

export const FIG_W = 300;
export const FIG_H = 146;
/** the desk top's y: everything on the desk stands on it */
const DESK = 126;
const CX = FIG_W / 2;
/** The person and their laptop are drawn in a frame of their own, centred on x 130
 *  with the desk at y 128, and moved into place together. */
const P_CX = 130;
const P_DESK = 128;

const typeCol = (t: string | null | undefined): string => (t && AGENT_TYPE_COL[t]) || 'var(--s1)';

/** Each use's icon colour: the phase of the same name, as the call bars colour it. */
const USE_COL: Record<UseKind, string> = table({
  explore: catCol('explore'),
  develop: catCol('edit'),
  build: catCol('build'),
  run: catCol('run'),
  hpc: catCol('slurm'),
  git: catCol('git'),
});

/** Where the robots float, the nearest the person first: three a side, rising away
 *  from them, clear of the window, the clock and what stands on the desk. */
const SLOTS: [number, number][] = [
  [97, 97],
  [203, 97],
  [66, 82],
  [234, 82],
  [34, 72],
  [266, 72],
];
/** sheets in the stack of papers, and lights on the server, at most */
const SHEETS = 5;
const LIGHTS = 10;
/** a use icon's side at 100% of the actions, and the smallest that still reads */
const USE_MAX = 34;
const USE_MIN = 12;
/** the clock's centre, and a point on it: `h` hours round from the top, `r` out */
const CLOCK: [number, number] = [268, 30];
const onClock = (h: number, r: number): [number, number] => {
  const t = (h / 24) * 2 * Math.PI - Math.PI / 2;
  return [CLOCK[0] + r * Math.cos(t), CLOCK[1] + r * Math.sin(t)];
};
/** arms behind the head: the person leaning back, in their own frame */
const ARMS_UP = 'M108 104 L95 85 L118 71 M152 104 L165 85 L142 71';

/** Where most sessions are driven from, as the laptop's tooltip says it. */
const MODE_TIP: Record<'terminal' | 'script' | 'extension' | 'app', string> = {
  terminal: 'a terminal: the agent’s own interactive screen',
  script: 'a script: claude -p, codex exec, an SDK or an MCP server, with no one at a terminal',
  extension: 'an IDE extension',
  app: 'the desktop app or the web',
};

/* ------------------------------------------------------------------ parts */

/** One robot, its head's centre at the origin: an agent session. */
function Robot({ x, y, col, live, pod }: { x: number; y: number; col: string; live: boolean; pod: boolean }) {
  return (
    <g className="urf-robot" transform={`translate(${x} ${y})`}>
      {pod && (
        <rect
          className="urf-pod"
          x={-14}
          y={-19}
          width={28}
          height={45}
          rx={14}
          fill="var(--good)"
          fillOpacity={0.07}
          stroke="var(--good)"
          strokeWidth={1.4}
          strokeDasharray="3.5 2.5"
        />
      )}
      <line x1={0} y1={-7} x2={0} y2={-12} stroke="var(--ink-2)" strokeWidth={1.3} />
      <circle className={live ? 'urf-live' : undefined} cx={0} cy={-13.8} r={2.4} fill={live ? 'var(--live)' : 'var(--surface)'} stroke="var(--ink-2)" strokeWidth={1} />
      <rect x={-10.6} y={-2.6} width={2} height={5.2} rx={0.8} fill="var(--ink-2)" />
      <rect x={8.6} y={-2.6} width={2} height={5.2} rx={0.8} fill="var(--ink-2)" />
      <rect x={-9} y={-7} width={18} height={14} rx={4.5} fill={col} stroke="var(--ink-2)" strokeWidth={1} />
      <rect x={-6} y={-4.4} width={12} height={8} rx={3} fill="var(--surface)" />
      <circle cx={-2.6} cy={-0.4} r={1.5} fill="var(--ink)" />
      <circle cx={2.6} cy={-0.4} r={1.5} fill="var(--ink)" />
      <rect x={-6.5} y={8.6} width={13} height={10} rx={3} fill={col} stroke="var(--ink-2)" strokeWidth={1} />
      <line x1={-6.5} y1={11} x2={-9.5} y2={15} stroke="var(--ink-2)" strokeWidth={1.4} strokeLinecap="round" />
      <line x1={6.5} y1={11} x2={9.5} y2={15} stroke="var(--ink-2)" strokeWidth={1.4} strokeLinecap="round" />
      <circle cx={0} cy={13.6} r={1.6} fill="var(--surface)" />
    </g>
  );
}

/** A use's icon in a 24-unit box centred on the origin, in its colour. */
function UseIcon({ id }: { id: UseKind }) {
  const c = USE_COL[id];
  // strokes scale with the icon, so a small one keeps its shape
  switch (id) {
    case 'explore':
      return (
        <>
          <path d="M2.6 5.4 L5.4 2.6 L11.2 8.4 L8.4 11.2 Z" fill={c} />
          <circle cx={-2.4} cy={-2.4} r={7.4} fill="var(--surface)" stroke={c} strokeWidth={3.2} />
        </>
      );
    case 'develop':
      return (
        <>
          <path d="M-10 10 L-8.8 4.4 L5 -9.4 L9.4 -5 L-4.4 8.8 Z" fill={c} />
          <path d="M5 -9.4 L9.4 -5 L11 -6.6 Q11.6 -7.6 10.8 -8.6 L8.6 -10.8 Q7.6 -11.6 6.6 -11 Z" fill="var(--ink-2)" />
          <path d="M-10 10 L-8.8 4.4 L-4.4 8.8 Z" fill="var(--surface)" stroke={c} strokeWidth={1.4} />
        </>
      );
    case 'build':
      return (
        <g transform="rotate(-38)">
          <rect x={-1.9} y={-5} width={3.8} height={17} rx={1.4} fill="var(--ink-2)" />
          <path d="M-4 -11 L4.5 -11 L4.5 -4 L-4 -4 Z" fill={c} />
          <path d="M4.5 -10 L10 -9.2 L10 -5.8 L4.5 -5 Z" fill={c} />
          <path d="M-4 -10.6 Q-8.6 -10.4 -10.6 -6.4 Q-8 -7.8 -4 -7.4 Z" fill={c} />
        </g>
      );
    case 'run':
      return (
        <>
          <circle cx={0} cy={0} r={11} fill={c} />
          <path d="M-3.6 -6 L6.8 0 L-3.6 6 Z" fill="var(--surface)" />
        </>
      );
    case 'hpc':
      return (
        <>
          {[-10.5, -3.5, 3.5].map((y) => (
            <g key={y}>
              <rect x={-11} y={y} width={22} height={6} rx={1.6} fill={c} />
              <circle cx={-7.4} cy={y + 3} r={1.3} fill="var(--surface)" />
              <rect x={-3.6} y={y + 2.3} width={11} height={1.4} rx={0.7} fill="var(--surface)" opacity={0.7} />
            </g>
          ))}
        </>
      );
    case 'git':
      return (
        <>
          <path d="M-5 -7 L-5 7 M6 -3 Q6 3.6 -5 4.6" fill="none" stroke={c} strokeWidth={3} />
          <circle cx={-5} cy={-8.4} r={3.6} fill={c} />
          <circle cx={-5} cy={8.4} r={3.6} fill={c} />
          <circle cx={6} cy={-5} r={3.6} fill={c} />
        </>
      );
  }
}

/** The emblem on the laptop's lid: where most sessions are driven from. */
function Emblem({ l, surfaces }: { l: Leverage; surfaces: UserRow['surfaces'] }) {
  const ex = P_CX;
  const ey = P_DESK - 12.5;
  const mono = { fontFamily: "'IBM Plex Mono', monospace", fontWeight: 600, fill: 'var(--surface)', textAnchor: 'middle' as const };
  switch (l.mode.id) {
    case 'terminal':
      return <text x={ex} y={ey + 4} fontSize={11.5} {...mono}>&gt;_</text>;
    case 'extension':
      return <text x={ex} y={ey + 4} fontSize={11.5} {...mono}>{'{ }'}</text>;
    case 'script':
      return (
        <g>
          <circle cx={ex} cy={ey} r={5.6} fill="none" stroke="var(--surface)" strokeWidth={2.6} strokeDasharray="2.2 2.2" />
          <circle cx={ex} cy={ey} r={4.2} fill="var(--surface)" />
          <circle cx={ex} cy={ey} r={1.6} fill="var(--ink-2)" />
        </g>
      );
    case 'app':
      if ((surfaces.web ?? 0) > (surfaces.desktop ?? 0)) {
        return (
          <g fill="none" stroke="var(--surface)" strokeWidth={1.5}>
            <circle cx={ex} cy={ey} r={6.2} />
            <ellipse cx={ex} cy={ey} rx={2.6} ry={6.2} />
            <line x1={ex - 6.2} y1={ey} x2={ex + 6.2} y2={ey} />
          </g>
        );
      }
      return (
        <g fill="none" stroke="var(--surface)" strokeWidth={1.5}>
          <rect x={ex - 8} y={ey - 6} width={16} height={12} rx={1.6} />
          <line x1={ex - 8} y1={ey - 2.6} x2={ex + 8} y2={ey - 2.6} />
        </g>
      );
    case 'mixed':
      return (
        <g fill="var(--ink-2)" stroke="var(--surface)" strokeWidth={1.3}>
          <rect x={ex - 9.5} y={ey - 7} width={11} height={8} rx={1.3} />
          <rect x={ex - 5.5} y={ey - 3.5} width={11} height={8} rx={1.3} />
          <rect x={ex - 1.5} y={ey} width={11} height={8} rx={1.3} />
        </g>
      );
    default:
      return <text x={ex} y={ey + 4} fontSize={11.5} {...mono}>?</text>;
  }
}

/* ----------------------------------------------------------------- words */

const traitOf = (row: UserRow, id: string) => row.traits.find((t) => t.id === id);

/** A trait's tooltip: what the figure draws for it, the numbers that met its rule, and the rule. */
function traitTip(what: string, row: UserRow, id: string, catalogue: TraitRule[] | null | undefined): string {
  const t = traitOf(row, id);
  const rule = ruleOf(catalogue, id);
  return `${what}: ${t?.label ?? id}, ${t?.detail ?? ''}${rule ? `\nrule: ${rule}` : ''}`;
}

/** `Claude Code 7, Codex 6`. */
const typesWords = (types: [string, number][]): string => types.map(([t, n]) => `${agentLbl(t)} ${fint(n)}`).join(', ');

/** `terminal 2 · script 3`, with the unknown ones last. */
function surfacesWords(m: Partial<Record<Surface, number>>): string {
  const r = ranked(m);
  return [...r.filter(([s]) => s !== 'unknown'), ...r.filter(([s]) => s === 'unknown')]
    .map(([s, n]) => `${SURFACE_LBL[s] ?? s} ${fint(n)}`)
    .join(' · ');
}

/* ---------------------------------------------------------------- figure */

/** One user's figure. `catalogue` is the service's trait rules, for the tooltips;
 *  `tz` the service's time zone, which the hours are in. */
export function PortraitFigure({
  row,
  catalogue,
  tz,
  className,
}: {
  row: UserRow;
  catalogue?: TraitRule[] | null;
  tz?: string | null;
  className?: string;
}) {
  const l = row.leverage;
  const has = (id: string) => row.traits.some((t) => t.id === id);
  const types = ranked(row.agent_types);
  const sessionsTotal = types.reduce((a, [, n]) => a + n, 0);

  // the robots: the most sessions open at once
  const atOnce = Math.max(row.peak_open, row.sessions > 0 ? 1 : 0);
  const over = atOnce > SLOTS.length;
  const drawn = over ? SLOTS.length - 1 : atOnce;
  const cols = apportion(types, drawn);
  const lit = Math.min(row.running, drawn);
  const pod = has('sandboxed');
  const robotTip = [
    `the robots: ${atOnce > 1 ? `up to ${plural(atOnce, 'session')} open at once` : 'one session open at a time'}${
      over ? ` (${fint(drawn)} drawn, the rest +${fint(atOnce - drawn)})` : ''}`,
    types.length
      ? `coloured by agent${types.length > 1 ? `, shared out by the window’s ${plural(sessionsTotal, 'session')}` : ''}: ${typesWords(types)}`
      : null,
    row.running > 0 ? `a lit antenna: running now (${fint(row.running)})` : 'no antenna lit: none running now',
    pod ? traitTip('the dashed capsules', row, 'sandboxed', catalogue) : null,
  ]
    .filter(Boolean)
    .join('\n');

  // the laptop and the person: where sessions are driven from
  const leaning = l.mode.id === 'script';
  const a = l.autonomy;
  // `of` is the sessions it is known for; under `mixed`, `n` is only the largest place's
  const m = l.mode;
  const known = `the ${plural(m.of, 'session')} where that is known`;
  const appWhere = (row.surfaces.desktop ?? 0) && !row.surfaces.web ? 'the desktop app' : (row.surfaces.web ?? 0) && !row.surfaces.desktop ? 'the web or a phone' : MODE_TIP.app;
  const laptopTip = [
    m.id === 'mixed'
      ? `the laptop: sessions driven from several places, none of them half of ${known}`
      : m.id === 'unknown'
        ? 'the laptop: where the sessions are driven from is not known: no agent’s own command line was read'
        : `the laptop: most sessions driven from ${m.id === 'app' ? appWhere : MODE_TIP[m.id]} (${fint(m.n)} of ${known})`,
    surfacesWords(row.surfaces) || null,
    leaning ? 'leaning back: no one at a terminal while they run' : null,
  ]
    .filter(Boolean)
    .join('\n');
  const lockTip = a
    ? a.id === 'bypass'
      ? `an open padlock: ${fint(a.bypassed)} of the ${plural(a.of, 'session')} whose launch is known started with a bypass flag`
      : a.id === 'auto'
        ? `a shield: ${fint(a.auto)} of the ${plural(a.of, 'session')} whose launch is known started in auto mode`
        : null
    : null;

  // the shelf: what the agents' actions are for
  const uses = row.calls == null ? [] : l.uses.slice(0, 3);
  const shelfTip =
    row.calls == null
      ? 'the shelf: what their agents do is not read; this service read the commands of Claude Code and Codex alone'
      : !l.uses.length
        ? `the shelf: too few actions to say what they are for (${fint(l.actions)})`
        : `the shelf: what the agents’ actions are for, the top ${uses.length === 1 ? 'one' : uses.length === 2 ? 'two' : 'three'}, each icon’s area its share of ${plural(l.actions, 'action')}`;

  // the clock: active minutes by hour
  const hours = row.hours ?? [];
  const peakH = busiestHour(hours);
  const maxMin = Math.max(0, ...hours);
  const clockTip = peakH
    ? `the clock: a day of 24 hours${tz ? ` (${tz})` : ''}, each spoke the minutes in which an agent began a tool call in that hour; the hand at the busiest, ${hourSpan(peakH[0])} (${plural(peakH[1], 'minute')}). ${plural(row.active_min, 'active minute')} in all`
    : 'the clock: no tool call began in the window, so no hour is drawn';

  // the window: day or night
  const night = has('overnight');
  const windowTip = night
    ? traitTip('the moon', row, 'overnight', catalogue)
    : l.when
      ? `the sun: ${l.when.night_pct != null ? `${pctWords(l.when.night_pct)} of active minutes between 00:00 and 06:00` : 'active minutes known'}, short of the overnight rule${
          ruleOf(catalogue, 'overnight') ? `\nrule: ${ruleOf(catalogue, 'overnight')}` : ''}`
      : 'the window: when they work is not known';

  // on the desk
  const w = row.words;
  const words = (w.commit ?? 0) + (w.pr ?? 0) + (w.log ?? 0);
  const sheets = Math.min(words, SHEETS);
  const wordsTip = words
    ? `papers: ${[
        w.commit ? plural(w.commit, 'commit message') : null,
        w.pr ? plural(w.pr, 'pull request title') : null,
        w.log ? plural(w.log, 'log entry', 'log entries') : null,
      ]
        .filter(Boolean)
        .join(', ')}, in the agents’ own words${words > SHEETS ? ` (one sheet each, ${SHEETS} at most)` : ' (one sheet each)'}`
    : '';
  const sl = row.slurm;
  const showRack = !!sl && (sl.jobs > 0 || sl.rejected > 0 || sl.queries > 0);
  // one light a submission while they fit; past that the lights share them out, so
  // the red ones stay the refused share, not the refused count against a cap
  const asked = sl ? sl.jobs + sl.rejected : 0;
  const shared = asked > LIGHTS;
  const lights = shared && sl ? apportion([['job', sl.jobs], ['refused', sl.rejected]].filter(([, n]) => (n as number) > 0) as [string, number][], LIGHTS) : [];
  const jobsLit = !sl ? 0 : shared ? lights.filter((x) => x === 'job').length : sl.jobs;
  const refused = !sl ? 0 : shared ? lights.filter((x) => x === 'refused').length : sl.rejected;
  const rackTip = sl
    ? [
        `the server: ${plural(sl.jobs, 'Slurm job')} submitted${sl.rejected ? `, ${fint(sl.rejected)} refused (red)` : ''}, ${
          shared ? `its ${LIGHTS} lights shared out by them` : 'one light each'}`,
        sl.queries ? `the queue checked ${plural(sl.queries, 'time')}${sl.queries_per_job != null ? `, ${fint(sl.queries_per_job)} a job` : ''}` : null,
        sl.gpu_h != null ? `${fint(sl.gpu_h)} GPU hours asked for` : null,
      ]
        .filter(Boolean)
        .join('\n')
    : '';

  const label = `${row.user}: ${leverageHeadline(row)}`;
  // one clip a figure: a page of figures must not share an id
  const clip = `urfig${useId().replace(/[^A-Za-z0-9_-]/g, '')}`;

  return (
    <svg
      className={`ur-figsvg${className ? ` ${className}` : ''}`}
      viewBox={`0 0 ${FIG_W} ${FIG_H}`}
      role="img"
      aria-label={label}
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <clipPath id={clip}>
          <rect x={0} y={0} width={FIG_W} height={FIG_H} rx={9} />
        </clipPath>
      </defs>
      <g clipPath={`url(#${clip})`}>
        {/* the room */}
        <rect x={0} y={0} width={FIG_W} height={DESK} fill="var(--surface-2)" />
        <rect x={0} y={DESK} width={FIG_W} height={7} fill="var(--edge)" />
        <rect x={0} y={DESK + 7} width={FIG_W} height={FIG_H - DESK - 7} fill="var(--sunken)" />

        {/* the window: day or night */}
        <g data-tip={windowTip}>
          <rect x={12} y={8} width={52} height={42} rx={3} fill={night ? 'var(--ink-2)' : 'var(--surface)'} stroke="var(--edge)" strokeWidth={2} />
          {night ? (
            <g className="urf-moon">
              <circle cx={27} cy={21} r={7.5} fill="var(--surface)" />
              <circle cx={30.6} cy={18.4} r={6.6} fill="var(--ink-2)" />
              {[
                [50, 15, 1.1],
                [55, 33, 0.9],
                [44, 41, 0.8],
                [20, 40, 1],
                [45, 25, 0.7],
              ].map(([x, y, r]) => (
                <circle key={`${x}-${y}`} cx={x} cy={y} r={r} fill="var(--surface)" />
              ))}
            </g>
          ) : l.when ? (
            <g className="urf-sun" stroke="var(--warn)" strokeWidth={1.6} strokeLinecap="round">
              <circle cx={27} cy={21} r={5.6} fill="var(--warn)" stroke="none" />
              {Array.from({ length: 8 }, (_, i) => {
                const t = (i * Math.PI) / 4;
                return <line key={i} x1={27 + 8 * Math.cos(t)} y1={21 + 8 * Math.sin(t)} x2={27 + 10.6 * Math.cos(t)} y2={21 + 10.6 * Math.sin(t)} />;
              })}
            </g>
          ) : null}
          <line x1={38} y1={9} x2={38} y2={49} stroke="var(--edge)" strokeWidth={1.4} />
          <line x1={13} y1={29} x2={63} y2={29} stroke="var(--edge)" strokeWidth={1.4} />
        </g>

        {/* the shelf: what the actions are for */}
        <g data-tip={shelfTip}>
          <rect x={CX - 56} y={50} width={112} height={3.4} rx={1} fill="var(--edge)" />
          <path d={`M${CX - 44} 53.4 L${CX - 44} 58 L${CX - 38} 53.4 Z M${CX + 44} 53.4 L${CX + 44} 58 L${CX + 38} 53.4 Z`} fill="var(--edge)" />
          {row.calls == null && (
            <g className="urf-unread">
              <rect x={CX - 9} y={31} width={18} height={18} rx={3} fill="none" stroke="var(--muted)" strokeWidth={1.2} strokeDasharray="3 2" />
              <text x={CX} y={44.5} textAnchor="middle" fontSize={12} fontWeight={600} fill="var(--muted)" fontFamily="'IBM Plex Mono', monospace">?</text>
            </g>
          )}
        </g>
        {uses.map((u, i) => {
          const side = Math.max(USE_MIN, USE_MAX * Math.sqrt(u.pct / 100));
          const x = [CX, CX - 38, CX + 38][i];
          return (
            <g
              key={u.id}
              className="urf-use"
              data-use={u.id}
              transform={`translate(${x} ${50 - side / 2 - 0.5}) scale(${side / 24})`}
              data-tip={`the shelf: ${USE_VERB[u.id]}, ${pctWords(u.pct)} of the actions (${fint(u.n)} of ${fint(l.actions)})${
                side === USE_MIN && USE_MAX * Math.sqrt(u.pct / 100) < USE_MIN ? '; drawn at the smallest size that still reads' : ''}`}
            >
              <UseIcon id={u.id} />
            </g>
          );
        })}

        {/* the clock: when their agents work */}
        <g data-tip={clockTip}>
          <circle cx={CLOCK[0]} cy={CLOCK[1]} r={21} fill="var(--surface)" stroke="var(--edge)" strokeWidth={2} />
          {[0, 6, 12, 18].map((h) => {
            const [x1, y1] = onClock(h, 17.2);
            const [x2, y2] = onClock(h, 19.6);
            return <line key={h} x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--muted)" strokeWidth={1.3} />;
          })}
          {maxMin > 0 &&
            hours.map((m, h) => {
              if (!m) return null;
              const [x1, y1] = onClock(h + 0.5, 4);
              const [x2, y2] = onClock(h + 0.5, 4 + 11.5 * (m / maxMin));
              return <line key={h} className="urf-spoke" x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--s1)" strokeWidth={2.1} strokeLinecap="round" opacity={0.85} />;
            })}
          {peakH && (
            <line
              className="urf-hand"
              x1={CLOCK[0]}
              y1={CLOCK[1]}
              x2={onClock(peakH[0] + 0.5, 13)[0]}
              y2={onClock(peakH[0] + 0.5, 13)[1]}
              stroke="var(--ink)"
              strokeWidth={1.6}
              strokeLinecap="round"
            />
          )}
          <circle cx={CLOCK[0]} cy={CLOCK[1]} r={1.9} fill="var(--ink)" />
        </g>

        {/* the robots' shadows, on the desk */}
        {cols.map((_, i) => (
          <ellipse key={i} cx={SLOTS[i][0]} cy={DESK + 3.6} rx={6.5 - (DESK - SLOTS[i][1]) / 14} ry={1.4} fill="var(--ink)" opacity={0.1} />
        ))}

        {/* the person and the laptop, in their own frame */}
        <g transform={`translate(${CX - P_CX} ${DESK - P_DESK})`}>
          <g data-tip={`${row.user}: the person is drawn the same for every user, leaning back only when most sessions run from scripts. What is round them is measured`}>
            <rect x={101} y={84} width={58} height={48} rx={12} fill="var(--rule)" stroke="var(--edge)" strokeWidth={1.2} />
            <path d="M101 128 C101 112 104 101 117 98 L143 98 C156 101 159 112 159 128 Z" fill="var(--sg)" stroke="var(--ink-2)" strokeWidth={1.1} />
            {leaning && (
              <>
                <path className="urf-lean" d={ARMS_UP} fill="none" stroke="var(--sg)" strokeWidth={7} strokeLinecap="round" strokeLinejoin="round" />
                <path d={ARMS_UP} fill="none" stroke="var(--ink-2)" strokeWidth={1.1} strokeLinecap="round" strokeLinejoin="round" opacity={0.35} />
              </>
            )}
            <rect x={125} y={90} width={10} height={10} fill="var(--surface)" stroke="var(--ink-2)" strokeWidth={1.1} />
            <circle cx={113.6} cy={80.5} r={3.4} fill="var(--surface)" stroke="var(--ink-2)" strokeWidth={1.2} />
            <circle cx={146.4} cy={80.5} r={3.4} fill="var(--surface)" stroke="var(--ink-2)" strokeWidth={1.2} />
            <path d="M123 98 L130 104 L137 98" fill="none" stroke="var(--ink-2)" strokeWidth={1.1} strokeLinejoin="round" />
            <circle cx={P_CX} cy={79} r={16} fill="var(--surface)" stroke="var(--ink-2)" strokeWidth={1.4} />
            {leaning ? (
              <path d="M121.8 78.6 Q124.4 76.4 127 78.6 M133 78.6 Q135.6 76.4 138.2 78.6" fill="none" stroke="var(--ink)" strokeWidth={1.4} strokeLinecap="round" />
            ) : (
              <>
                <circle cx={124.4} cy={78} r={1.8} fill="var(--ink)" />
                <circle cx={135.6} cy={78} r={1.8} fill="var(--ink)" />
              </>
            )}
            <path d="M125.4 85.4 Q130 88.6 134.6 85.4" fill="none" stroke="var(--ink-2)" strokeWidth={1.4} strokeLinecap="round" />
          </g>
          <g className="urf-laptop" data-mode={m.id} data-tip={laptopTip}>
            <rect x={103} y={103} width={54} height={25} rx={3} fill="var(--ink-2)" />
            <rect x={96} y={P_DESK - 1} width={68} height={3.2} rx={1.5} fill="var(--muted)" />
            <Emblem l={l} surfaces={row.surfaces} />
          </g>
          {lockTip && a?.id === 'bypass' && (
            <g className="urf-lock" data-tip={lockTip}>
              <path d="M147.6 99.5 V95 a3.4 3.4 0 0 1 6.8 0 V96.4" fill="none" stroke="var(--crit)" strokeWidth={1.9} strokeLinecap="round" />
              <rect x={144.6} y={99} width={12} height={9.6} rx={1.8} fill="var(--crit)" stroke="var(--surface)" strokeWidth={0.8} />
              <circle cx={150.6} cy={103.2} r={1.3} fill="var(--surface)" />
              <rect x={150} y={103.6} width={1.2} height={2.6} fill="var(--surface)" />
            </g>
          )}
          {lockTip && a?.id === 'auto' && (
            <g className="urf-shield" data-tip={lockTip}>
              <path d="M150.6 92 L156.6 94.4 L156.6 99.4 Q156.6 104.6 150.6 107.4 Q144.6 104.6 144.6 99.4 L144.6 94.4 Z" fill="var(--warn)" stroke="var(--ink-2)" strokeWidth={0.9} />
              <text x={150.6} y={102.6} textAnchor="middle" fontSize={7.4} fontWeight={700} fill="var(--ink)" fontFamily="'IBM Plex Sans', sans-serif">A</text>
            </g>
          )}
        </g>

        {/* the robots: sessions open at once */}
        <g data-tip={robotTip}>
          {cols.map((c, i) => (
            <Robot key={i} x={SLOTS[i][0]} y={SLOTS[i][1]} col={typeCol(c)} live={i < lit} pod={pod} />
          ))}
          {over && (
            <g className="urf-more">
              <rect x={SLOTS[SLOTS.length - 1][0] - 15} y={SLOTS[SLOTS.length - 1][1] - 9} width={30} height={18} rx={9} fill="var(--surface)" stroke="var(--edge)" strokeWidth={1.2} />
              <text
                x={SLOTS[SLOTS.length - 1][0]}
                y={SLOTS[SLOTS.length - 1][1] + 4}
                textAnchor="middle"
                fontSize={10.5}
                fontWeight={600}
                fill="var(--ink-2)"
                fontFamily="'IBM Plex Mono', monospace"
              >
                +{fint(atOnce - drawn)}
              </text>
            </g>
          )}
        </g>

        {/* on the desk, left: what the agents wrote, and how long a session runs */}
        {sheets > 0 && (
          <g data-tip={wordsTip}>
            {Array.from({ length: sheets }, (_, k) => sheets - 1 - k).map((i) => {
              // the back pages stand up and to the right of the front one
              const x = 9 + i * 2.4;
              const y = DESK - 18 - i * 2.4;
              return (
                <g key={i} className="urf-sheet">
                  <path d={`M${x} ${y} H${x + 9.5} L${x + 14} ${y + 4.5} V${y + 18} H${x} Z`} fill="var(--surface)" stroke="var(--muted)" strokeWidth={0.9} strokeLinejoin="round" />
                  {i === 0 && (
                    <>
                      <path d={`M${x + 9.5} ${y} V${y + 4.5} H${x + 14}`} fill="var(--rule)" stroke="var(--muted)" strokeWidth={0.9} strokeLinejoin="round" />
                      <path d={`M${x + 2.6} ${y + 7.2} h8.6 M${x + 2.6} ${y + 10.2} h8.6 M${x + 2.6} ${y + 13.2} h5.4`} stroke="var(--muted)" strokeWidth={1} />
                    </>
                  )}
                </g>
              );
            })}
          </g>
        )}
        {has('long') && (
          <g className="urf-mug" data-tip={traitTip('a mug', row, 'long', catalogue)}>
            <path d={`M51 ${DESK - 9.4} q4.4 0 4.4 3.3 q0 3.3 -4.4 3.3`} fill="none" stroke="var(--ink-2)" strokeWidth={1.3} />
            <rect x={41} y={DESK - 12} width={11} height={12} rx={2} fill="var(--surface)" stroke="var(--ink-2)" strokeWidth={1.1} />
            <path
              d={`M44.4 ${DESK - 15} q-1.6 -2 0 -4 q1.6 -2 0 -4 M48.4 ${DESK - 15} q-1.6 -2 0 -4 q1.6 -2 0 -4`}
              fill="none"
              stroke="var(--muted)"
              strokeWidth={1}
              strokeLinecap="round"
            />
          </g>
        )}
        {has('short') && (
          <g className="urf-stopwatch" data-tip={traitTip('a stopwatch', row, 'short', catalogue)}>
            <rect x={45} y={DESK - 16} width={3} height={3} rx={0.6} fill="var(--ink-2)" />
            <circle cx={46.5} cy={DESK - 6.8} r={6.2} fill="var(--surface)" stroke="var(--ink-2)" strokeWidth={1.2} />
            <line x1={46.5} y1={DESK - 6.8} x2={49} y2={DESK - 9.8} stroke="var(--ink)" strokeWidth={1.2} strokeLinecap="round" />
          </g>
        )}

        {/* on the desk, right: waiting, and the scheduler */}
        {has('poller') && (
          <g className="urf-hourglass" data-tip={traitTip('an hourglass', row, 'poller', catalogue)}>
            <rect x={256} y={DESK - 16} width={14} height={2} rx={0.8} fill="var(--ink-2)" />
            <rect x={256} y={DESK - 2} width={14} height={2} rx={0.8} fill="var(--ink-2)" />
            <path
              d={`M258 ${DESK - 14} L268 ${DESK - 14} L264.2 ${DESK - 8} L268 ${DESK - 2} L258 ${DESK - 2} L261.8 ${DESK - 8} Z`}
              fill="var(--surface)"
              stroke="var(--ink-2)"
              strokeWidth={0.9}
            />
            <path
              d={`M259.8 ${DESK - 12.6} L266.2 ${DESK - 12.6} L263 ${DESK - 9} Z M259.4 ${DESK - 2.6} L266.6 ${DESK - 2.6} L263 ${DESK - 6.2} Z`}
              fill="var(--warn)"
            />
          </g>
        )}
        {showRack && sl && (
          <g className="urf-server" data-tip={rackTip}>
            <rect x={278} y={DESK - 30} width={16} height={30} rx={2} fill="var(--surface)" stroke="var(--ink-2)" strokeWidth={1.1} />
            {Array.from({ length: LIGHTS }, (_, i) => {
              const kind = i < jobsLit ? 'job' : i < jobsLit + refused ? 'refused' : 'off';
              const col = kind === 'job' ? 'var(--s4)' : kind === 'refused' ? 'var(--crit)' : 'var(--rule)';
              return <rect key={i} className={`urf-light ${kind}`} x={281 + (i % 2) * 5.5} y={DESK - 26.5 + Math.floor(i / 2) * 5} width={4.5} height={2.6} rx={0.6} fill={col} />;
            })}
          </g>
        )}
      </g>
    </svg>
  );
}

/** The key: what each part of a figure says. Every part says its own numbers, and the
 *  service's rule behind it, on hover. */
export const FIGURE_KEY: [string, string][] = [
  ['robots', 'the most sessions open at once, coloured by agent; a lit antenna: running now'],
  ['laptop', 'where most sessions are driven from: >_ a terminal, a gear a script, { } an IDE, a window the desktop app, a globe the web, stacked windows several places, ? not known'],
  ['leaning back', 'most sessions are driven from a script, with no one at a terminal'],
  ['padlock · shield', 'most launches had a bypass flag · were in auto mode'],
  ['capsules', 'most sessions had some or all of their tool calls sandboxed'],
  ['shelf', 'what the agents’ actions are for, the top three, area by share: a magnifier explores, a pencil edits and tests, a hammer builds, play runs programs, a rack is Slurm, a branch is git; ? not read'],
  ['clock', 'active minutes over 24 hours, a spoke an hour; the hand at the busiest'],
  ['window', 'the moon: much of their agents’ work falls between 00:00 and 06:00; the sun: it does not; empty: not known'],
  ['papers', 'commit messages, pull request titles and log entries the agents wrote, a sheet each'],
  ['mug · stopwatch', 'long sessions · many short ones'],
  ['hourglass', 'their agents poll the scheduler, or many of their tool calls only wait'],
  ['server', 'Slurm jobs submitted, a light each (shared out past ten); red: refused'],
];

/** The figure's key, folded under it. */
export function FigureKey() {
  return (
    <details className="ur-figkey">
      <summary>how to read the figure</summary>
      <p className="nm">
        Not a likeness: the person is drawn the same for everyone. Each thing round them is measured, and says its numbers and its rule on hover.
      </p>
      <dl>
        {FIGURE_KEY.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
