/* Portraits: how each user works with coding agents (`rc_dashboard/portraits.py`), in
 * the words the Users tab uses for them.
 *
 * The service holds every threshold: a trait comes with the numbers that met its
 * rule, and the rule itself is in the catalogue it sends. What is here only words
 * what the service counted -- the plurality, a share and what it is a share of --
 * so a sentence never claims more than its numbers. A share is always over the
 * KNOWN values (lib/sandbox.ts), and a count the service sends as null (calls an
 * agent type's commands do not let it read) is said to be unread, never 0.
 */
import type { CallCat, Surface, TraitRule, UserRow, UsersFleet } from '../api/types';
import { AGENT_TYPE_LBL } from './classes';
import { dur, fint } from './format';
import { phaseCol } from './phases';
import { APPROVAL_KNOWN, shareOfKnown } from './sandbox';
import { SURFACE_LBL } from './surfaces';

/** The call categories, in the order the service lists them, with the words a
 *  share of calls says them in, and the colour of the phase of the same name. */
export const CALL_CATS: [CallCat, string][] = [
  ['explore', 'explore'],
  ['edit', 'edit'],
  ['build', 'build'],
  ['test', 'test'],
  ['run', 'run'],
  ['slurm', 'Slurm'],
  ['wait', 'wait'],
  ['git', 'git'],
  ['unknown', 'not recorded'],
];
const CAT_LBL = Object.fromEntries(CALL_CATS);
/** What a category's calls do, for a tooltip. */
export const CAT_TIP: Record<CallCat, string> = {
  explore: 'read and search: ls, cat, rg, grep, find, sed -n, head, …',
  edit: 'change files from the shell: cp, mv, rm, mkdir, sed -i, a heredoc into a file, …',
  build: 'build, compile, or install: make, cmake, gcc, pip install, …',
  test: 'run tests and checks: pytest, ruff, mypy, a test script, …',
  run: 'run a program: python train.py, a script, …',
  slurm: 'talk to the scheduler: sbatch, srun, squeue, sacct, scancel, …',
  wait: 'only wait: sleep, ps, pgrep, watch, …',
  git: 'git and gh',
  unknown: 'a call whose command was not recorded',
};
export const catLbl = (c: string): string => CAT_LBL[c] ?? c;
export const catCol = (c: string): string => phaseCol(c);

/** The agent as people call it. */
export const agentLbl = (t: string | null | undefined): string => (t ? AGENT_TYPE_LBL[t] ?? t : 'untagged');

const sum = (m: Partial<Record<string, number>> | null | undefined): number =>
  Object.values(m ?? {}).reduce<number>((a, b) => a + (b ?? 0), 0);

/** `[key, n]` of a count map, the most first, with no zeros. */
export function ranked<K extends string>(m: Partial<Record<K, number>> | null | undefined): [K, number][] {
  return (Object.entries(m ?? {}) as [K, number][]).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
}

/** `12 sessions`, `1 session`. */
export const plural = (n: number, one: string, many = `${one}s`): string => `${fint(n)} ${n === 1 ? one : many}`;

/** A percentage as a sentence says it: `54%`, `8.3%`, `<1%`. */
export function pctWords(p: number): string {
  if (p > 0 && p < 1) return '<1%';
  return p < 10 ? `${p.toFixed(1).replace(/\.0$/, '')}%` : `${Math.round(p)}%`;
}

/** The share of `n` in `of`, in percent; null when `of` is 0. */
export const share = (n: number, of: number): number | null => (of > 0 ? (100 * n) / of : null);

/** Sessions with a known surface, and the ones driven from `which`. */
export function surfaceShare(m: Partial<Record<Surface, number>> | null | undefined, which: Surface[]) {
  const known = sum(m) - (m?.unknown ?? 0);
  const n = which.reduce((a, s) => a + (m?.[s] ?? 0), 0);
  return { n, known, pct: share(n, known) };
}

/** Sessions launched with a bypass flag, of those whose launch is known. */
export const bypassShare = (m: Record<string, number> | null | undefined) => shareOfKnown(m, 'bypassed', APPROVAL_KNOWN);

/** The calls the logs hold, all categories. */
export const loggedCalls = (row: Pick<UserRow, 'cats'>): number => sum(row.cats);

/** The busiest local hour: `[hour, minutes]`, or null with none. */
export function busiestHour(hours: number[] | null | undefined): [number, number] | null {
  if (!hours?.length) return null;
  let best = -1;
  hours.forEach((n, h) => {
    if (n > 0 && (best < 0 || n > hours[best])) best = h;
  });
  return best < 0 ? null : [best, hours[best]];
}

/** `14:00–15:00`. */
export const hourSpan = (h: number): string => `${String(h).padStart(2, '0')}:00–${String((h + 1) % 24).padStart(2, '0')}:00`;

/** The rule behind a trait, from the catalogue the service sends with it. */
export function ruleOf(catalogue: TraitRule[] | null | undefined, id: string): string | undefined {
  return catalogue?.find((t) => t.id === id)?.rule;
}


/* ------------------------------------------------------------ the summary */

/** `Claude Code 10 and Codex 2`, the most first. */
function agentsPhrase(types: Record<string, number>): string {
  const r = ranked(types);
  if (r.length === 1) return agentLbl(r[0][0]);
  const parts = r.map(([t, n]) => `${agentLbl(t)} ${fint(n)}`);
  return parts.length < 3 ? parts.join(' and ') : `${parts.slice(0, -1).join(', ')}, and ${parts[parts.length - 1]}`;
}

/** Each surface as a sentence names it, after "driven from". */
const SURFACE_WORDS: Partial<Record<Surface, string>> = {
  terminal: 'a terminal',
  script: 'a script',
  desktop: 'the desktop app',
  extension: 'an IDE extension',
  web: 'the web or a phone',
};

/** `8 of 12 driven from a terminal and 4 from a script`: each surface, the most first. */
function surfacePhrase(m: Partial<Record<Surface, number>>, n: number): string | null {
  const r = ranked(m).filter(([s]) => s !== 'unknown');
  if (!r.length) return null;
  const words = (s: Surface) => SURFACE_WORDS[s] ?? SURFACE_LBL[s];
  const [first, ...rest] = r;
  const lead = `${fint(first[1])} of ${fint(n)} driven from ${words(first[0])}`;
  return rest.length ? `${lead}, ${rest.map(([s, k]) => `${fint(k)} from ${words(s)}`).join(', ')}` : lead;
}

/** The portrait in a few plain sentences, each from numbers the service counted.
 *  `win` is how the page names the window: `24h`. The window's medians are said
 *  beside the user's when the window holds more than this one user. */
export function summaryLines(row: UserRow, fleet: UsersFleet | null | undefined, win: string | null): string[] {
  const out: string[] = [];
  const n = row.sessions;
  const types = ranked(row.agent_types);
  const span = win ? ` in the last ${win}` : '';
  out.push(
    types.length === 1
      ? `${plural(n, `${agentLbl(types[0][0])} session`)}${span}.`
      : `${plural(n, 'agent session')}${span}: ${agentsPhrase(row.agent_types)}.`,
  );
  const where = surfacePhrase(row.surfaces, n);
  const bp = bypassShare(row.approval as Record<string, number>);
  const launch = bp.known
    ? row.approval.bypassed
      ? `${fint(row.approval.bypassed)} of the ${fint(bp.known)} whose launch is known started with a bypass flag`
      : `none of the ${fint(bp.known)} whose launch is known started with a bypass flag`
    : null;
  if (where || launch) out.push(`${[where, launch].filter(Boolean).join('; ')}.`.replace(/^./, (c) => c.toUpperCase()));
  const others = fleet && fleet.users > 1;
  if (row.wall_p50_s != null) {
    const median = others && fleet?.wall_p50_s != null ? ` (the window’s median: ${dur(fleet.wall_p50_s)})` : '';
    const open = row.peak_open > 1 ? `, and up to ${fint(row.peak_open)} were open at once` : '';
    out.push(`${n === 1 ? 'It was' : 'A typical session was'} open ${dur(row.wall_p50_s)}${n === 1 ? '' : median}${open}.`);
  }
  const logged = loggedCalls(row);
  if (row.calls == null) {
    out.push(`Their agents’ calls are not read: the service reads the commands of Claude Code and Codex, and not of ${agentsPhrase(row.agent_types)}.`);
  } else if (logged) {
    const top = ranked(row.cats).filter(([c]) => c !== 'unknown').slice(0, 2);
    if (top.length) {
      const what = top.map(([c, k]) => `${catLbl(c)} (${pctWords((100 * k) / logged)})`).join(' and ');
      out.push(`Of ${plural(row.calls, 'tool call')}, their agents’ commands mostly ${what}.`);
    }
  }
  const w = row.words;
  const said = [
    w.commit ? plural(w.commit, 'commit message') : null,
    w.pr ? plural(w.pr, 'pull request title') : null,
    w.log ? plural(w.log, 'log entry', 'log entries') : null,
  ].filter(Boolean);
  if (said.length) out.push(`In their own words, the agents wrote ${said.join(', ')}.`);
  const sl = row.slurm;
  if (sl && (sl.jobs || sl.queries)) {
    const per = sl.queries_per_job != null ? ` (${fint(sl.queries_per_job)} a job)` : '';
    out.push(`At the scheduler they submitted ${plural(sl.jobs, 'job')} and checked the queue ${plural(sl.queries, 'time')}${per}.`);
  }
  return out;
}
