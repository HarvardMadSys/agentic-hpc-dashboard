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
import type { CallCat, Leverage, Surface, TraitRule, UseKind, UserRow, UsersFleet } from '../api/types';
import { AGENT_TYPE_LBL } from './classes';
import { dur, fint } from './format';
import { phaseCol } from './phases';
import { APPROVAL_KNOWN, shareOfKnown } from './sandbox';
import { SURFACE_LBL } from './surfaces';
import { table } from './lookup';

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
export const CAT_TIP: Record<CallCat, string> = table({
  explore: 'read and search: ls, cat, rg, grep, find, sed -n, head, …',
  edit: 'change files from the shell: cp, mv, rm, mkdir, sed -i, a heredoc into a file, …',
  build: 'build, compile, or install: make, cmake, gcc, pip install, …',
  test: 'run tests and checks: pytest, ruff, mypy, a test script, …',
  run: 'run a program: python train.py, a script, …',
  slurm: 'talk to the scheduler: sbatch, srun, squeue, sacct, scancel, …',
  wait: 'only wait: sleep, ps, pgrep, watch, …',
  git: 'git and gh',
  unknown: 'a tool call whose command was not recorded',
});
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

/** `n` items shared out over `types` by their counts, largest remainder first: the
 *  robots over agents, the server's lights over jobs and refusals. When there are
 *  at least as many items as types, each type gets one. Ties go to the earlier
 *  type, so the same counts always share out the same way. */
export function apportion(types: [string, number][], n: number): string[] {
  const total = types.reduce((a, [, k]) => a + k, 0);
  if (n <= 0 || total <= 0) return [];
  if (n < types.length) return types.slice(0, n).map(([t]) => t);
  const q = types.map(([, k]) => (k * n) / total);
  const a = q.map((x) => Math.max(1, Math.floor(x)));
  let s = a.reduce((x, y) => x + y, 0);
  while (s < n) {
    let best = 0;
    for (let i = 1; i < a.length; i++) if (q[i] - a[i] > q[best] - a[best]) best = i;
    a[best]++;
    s++;
  }
  while (s > n) {
    let best = -1;
    for (let i = 0; i < a.length; i++) if (a[i] > 1 && (best < 0 || a[i] - q[i] > a[best] - q[best])) best = i;
    if (best < 0) break;
    a[best]--;
    s--;
  }
  return types.flatMap(([t], i) => Array<string>(a[i]).fill(t));
}

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

/* ------------------------------------------------ how they leverage agents
 * The service decides every part of it (`portraits.leverage`, by its RULES);
 * here it is only worded. Each part names the numbers it rests on, and a part
 * the numbers do not say is left out, never guessed. */

/** The way most of a user's sessions are driven, as the summary opens. */
const MODE_WORDS: Record<Leverage['mode']['id'], string> = {
  terminal: 'Works with agents interactively in a terminal',
  script: 'Runs agents from scripts, with no one at a terminal',
  extension: 'Works with agents inside an IDE',
  app: 'Drives agents from the desktop app or the web',
  mixed: 'Drives agents from several places',
  unknown: 'Runs agents',
};

/** A use, after "to": what the agents' actions are for. */
export const USE_VERB: Record<UseKind, string> = table({
  explore: 'explore code and files',
  develop: 'edit and test code',
  build: 'build and install software',
  run: 'run programs',
  hpc: 'run jobs on Slurm',
  git: 'manage code in git',
});
/** The same use, as a noun: `a mix of uses led by exploring code and running programs`. */
const USE_NOUN: Record<UseKind, string> = table({
  explore: 'exploring code',
  develop: 'editing and testing',
  build: 'building',
  run: 'running programs',
  hpc: 'Slurm jobs',
  git: 'git',
});
/** What each use counts, for a tooltip. */
export const USE_TIP: Record<UseKind, string> = table({
  explore: 'tool calls that read and search: ls, cat, rg, grep, find, sed -n, …',
  develop: 'tool calls that change files from the shell, and tool calls that run tests. The agent’s own Edit tool runs no command, so edits made with it are not here',
  build: 'tool calls that build, compile or install: make, gcc, pip install, …',
  run: 'tool calls that run a program: python train.py, a script, …',
  hpc: 'tool calls to the scheduler: sbatch, srun, squeue, sacct, scancel, …',
  git: 'tool calls to git and gh',
});

/** The one-sentence summary of how a user leverages their agents: the roster's line
 *  under them, and the head of their portrait. */
export function leverageHeadline(row: UserRow): string {
  const l = row.leverage;
  let s = MODE_WORDS[l.mode.id] ?? MODE_WORDS.unknown;
  if (l.mode.id === 'app') {
    const desk = row.surfaces.desktop ?? 0;
    const web = row.surfaces.web ?? 0;
    if (desk && !web) s = 'Drives agents from the desktop app';
    else if (web && !desk) s = 'Drives agents from the web or a phone';
  }
  const mods: string[] = [];
  if (l.parallel) mods.push(`up to ${fint(l.parallel)} at a time`);
  if (l.autonomy?.id === 'bypass') mods.push('with a bypass flag');
  else if (l.autonomy?.id === 'auto') mods.push('in auto mode');
  if (mods.length) s += `, ${mods.join(' and ')}`;
  const u = l.uses;
  const named = new Set<string>();
  if (row.calls != null && u.length) {
    if (l.mostly === 1) {
      s += `, mostly to ${USE_VERB[u[0].id]}`;
      named.add(u[0].id);
    } else if (l.mostly === 2) {
      s += `, mostly to ${USE_VERB[u[0].id]} and to ${USE_VERB[u[1].id]}`;
      named.add(u[0].id).add(u[1].id);
    } else {
      const lead = u.slice(0, 2);
      s += `, for a mix of uses led by ${lead.map((x) => USE_NOUN[x.id]).join(' and ')}`;
      lead.forEach((x) => named.add(x.id));
    }
  }
  const jobs = row.slurm?.jobs ?? 0;
  if (jobs > 0 && !named.has('hpc')) s += ', and has them submit Slurm jobs';
  s += '.';
  if (row.calls == null) s += jobs ? ' What else their agents do is not read.' : ' What their agents do is not read.';
  return s;
}

/** Each surface as a point names it, after its count: `3 from a script`. */
function wherePhrase(m: Partial<Record<Surface, number>>, n: number): string | null {
  const r = ranked(m).filter(([s]) => s !== 'unknown');
  const unknown = m.unknown ?? 0;
  if (!r.length) return unknown ? `${fint(unknown)} of ${fint(n)} from an unknown place` : null;
  const words = (s: Surface) => SURFACE_WORDS[s] ?? SURFACE_LBL[s];
  const parts = r.map(([s, k], i) => `${fint(k)}${i === 0 ? ` of ${fint(n)}` : ''} from ${words(s)}`);
  return parts.join(', ') + (unknown ? `; ${fint(unknown)} from an unknown place` : '');
}

/** One point of the summary: a short label, what the numbers say, and how they were counted. */
export interface LeveragePoint {
  k: string;
  label: string;
  text: string;
  tip?: string;
}

/** The summary point by point, each grounded in the numbers it rests on. `tz` is the
 *  service's time zone, which the hours are in. The window's medians are said beside
 *  the user's when the window holds other users. */
export function leveragePoints(row: UserRow, fleet: UsersFleet | null | undefined, tz?: string | null): LeveragePoint[] {
  const l = row.leverage;
  const n = row.sessions;
  const out: LeveragePoint[] = [];
  const types = ranked(row.agent_types);
  out.push({
    k: 'agents',
    label: 'agents',
    text: types.length === 1 ? plural(n, `${agentLbl(types[0][0])} session`) : `${plural(n, 'session')}: ${agentsPhrase(row.agent_types)}`,
    tip: 'one session is one agent run: an agent process seen at the root of its tree, its rows on two days together',
  });
  const where = wherePhrase(row.surfaces, n);
  if (where) out.push({ k: 'where', label: 'driven from', text: where, tip: 'read off each agent’s own command line' });
  const a = l.autonomy;
  if (a) {
    const lead = a.bypassed
      ? `${fint(a.bypassed)} of the ${fint(a.of)} whose launch is known started with a bypass flag`
      : `none of the ${fint(a.of)} whose launch is known started with a bypass flag`;
    out.push({
      k: 'freedom',
      label: 'freedom',
      text: lead + (a.auto ? `; ${fint(a.auto)} in auto mode` : ''),
      tip: 'approval at launch, off the agent’s own command line. A settings file, and a switch made mid-session, are not seen, so “no bypass flag” is not “asks first”',
    });
  }
  const others = !!fleet && fleet.users > 1;
  const median = others && fleet?.wall_p50_s != null ? ` (the window’s median: ${dur(fleet.wall_p50_s)})` : '';
  out.push({
    k: 'scale',
    label: 'at once',
    text: `${row.peak_open > 1 ? `up to ${fint(row.peak_open)} open together` : 'one at a time'}${
      row.wall_p50_s != null ? `; ${n === 1 ? 'open' : 'a typical session open'} ${dur(row.wall_p50_s)}${n === 1 ? '' : median}` : ''}`,
    tip: 'a session is open from its first record to its last; a typical one is the median',
  });
  if (row.calls == null) {
    out.push({ k: 'uses', label: 'used for', text: 'not read: this service read the commands of Claude Code and Codex alone' });
  } else if (!l.uses.length) {
    out.push({ k: 'uses', label: 'used for', text: `too few actions to say (${fint(l.actions)})` });
  } else {
    const shown = l.uses.slice(0, 4);
    const more = l.uses.length - shown.length;
    out.push({
      k: 'uses',
      label: 'used for',
      text: `${shown.map((x) => `${USE_VERB[x.id]} ${pctWords(x.pct)}`).join(' · ')}${more > 0 ? ` · +${more} more` : ''}, of ${plural(l.actions, 'action')}`,
      tip: 'an action is a tool call that does something, counted by what it does; waiting (sleep, ps, a polling loop) and tool calls whose command was not recorded are left out. Read from commands: the agent’s own Read and Edit tools leave none',
    });
  }
  if (l.waiting) {
    out.push({
      k: 'waiting',
      label: 'waiting',
      text: `${pctWords(l.waiting.pct)} of their tool calls only wait: sleep, ps, pgrep, a loop on the queue`,
    });
  }
  const sl = row.slurm;
  if (sl && (sl.jobs || sl.queries)) {
    const bits = [`${plural(sl.jobs, 'Slurm job')} submitted${sl.rejected ? `, ${fint(sl.rejected)} refused` : ''}`];
    if (sl.queries) bits.push(`the queue checked ${plural(sl.queries, 'time')}${sl.queries_per_job != null ? ` (${fint(sl.queries_per_job)} a job)` : ''}`);
    if (sl.polling_sessions) bits.push(`polled for ${dur(sl.polling_s)} in ${plural(sl.polling_sessions, 'session')}`);
    out.push({ k: 'hpc', label: 'on the cluster', text: bits.join('; '), tip: 'their agent sessions at the scheduler, as the HPC tab folds them' });
  }
  const w = row.words;
  const said = [
    w.commit ? plural(w.commit, 'commit message') : null,
    w.pr ? plural(w.pr, 'pull request title') : null,
    w.log ? plural(w.log, 'log entry', 'log entries') : null,
  ].filter((x): x is string => !!x);
  if (said.length) {
    const list = said.length < 3 ? said.join(' and ') : `${said.slice(0, -1).join(', ')} and ${said[said.length - 1]}`;
    out.push({ k: 'output', label: 'leaves behind', text: `${list}, in the agents’ own words`, tip: 'text the agents wrote in their commands: git commit -m, gh pr create --title, an entry written to a log or plan file' });
  }
  if (l.project) {
    out.push({
      k: 'project',
      label: 'works on',
      text: l.project.mostly
        ? `mostly ${l.project.name} (${fint(l.project.n)} of ${plural(l.project.of, 'session')})`
        : `${row.projects.map(([p, k]) => `${p} ${fint(k)}`).join(' · ')}${row.projects_total > row.projects.length ? ` · +${fint(row.projects_total - row.projects.length)} more` : ''}`,
      tip: 'the folder each session’s agent was started in, or, when that is a home folder or named for a person, the folder its commands change into most',
    });
  }
  if (l.when) {
    out.push({
      k: 'when',
      label: 'when',
      text: `busiest ${hourSpan(l.when.busiest)}${tz ? ` ${tz}` : ''}${
        l.when.night_pct != null ? `; ${pctWords(l.when.night_pct)} of active minutes between 00:00 and 06:00` : ''}`,
      tip: 'by the minutes in which one of their agents began a tool call, in the service’s time zone',
    });
  }
  return out;
}
