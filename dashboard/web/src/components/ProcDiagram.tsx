/* A session's process tree, drawn as a tree (roadmap notes 38 and 39).
 *
 * The same read as the table view (ProcTree.tsx) -- the same entries, the same
 * ids ('r', 'r.0', 'r.0.3') -- drawn as boxes joined by lines, each box naming
 * its command with what it cost and how it ended, the way a model graph reads
 * in Netron. It opens collapsed: the session's process and the ones it started
 * directly; a click on a box opens what that process started.
 *
 * "Hide routine" (on by default) takes out what a reader has seen a thousand
 * times -- runs of the same command (the read's folded repeats, `pgrep ×120`)
 * and the commands on the site's routine list (`ui.routine_commands`:
 * lsb_release, getconf, git status, a shell sourcing its snapshot, ...) -- so
 * the processes that are not routine are the ones left to see. What it hid is
 * counted above the tree, never dropped silently.
 */
import type { ProcCount, ProcEntry, ProcGroup, ProcIo, ProcNode } from '../api/types';
import { ioBrief, ioTip } from '../lib/procio';
import { DASH, clock, dur, fint } from '../lib/format';
import { pcol } from '../lib/purposes';
import { TreeDiagram } from './TreeDiagram';
import type { TreeNode } from './TreeDiagram';

type Rest = { k: 'rest'; more: number; of: ProcGroup };
type Cap = { k: 'cap'; more: number; parent: string };
export type Item = ProcEntry | Rest | Cap;
/** A box: what it stands for, and how many children it would open to. */
export type PNode = TreeNode<{ e: Item; kids: number }>;

/** Children drawn under one parent before a "+N more" box stands for the rest. */
export const MAX_KIDS = 30;

function kidsOf(e: Item): Item[] {
  if (e.k === 'p') return e.kids ?? [];
  if (e.k === 'g') return e.more > 0 ? [...e.items, { k: 'rest', more: e.more, of: e }] : e.items;
  return [];
}

/* ------------------------------------------------------------- routine */

export const DEFAULT_ROUTINE = [
  'lsb_release', 'getconf', 'uname', 'sleep', 'pgrep', 'ps', 'tr', 'cut', 'dirname', 'basename',
  'readlink', 'realpath', 'which', 'hostname', 'id', 'whoami', 'date', 'tty', 'stty', 'tput',
  'locale', 'nproc', 'true', 'false', 'git status', 'git rev-parse', 'git config',
  'git symbolic-ref', 'shell-snapshot',
];

/** git's subcommand, past its global options (`git -c a=b -C dir status`). */
function gitSub(args: string): string | null {
  const t = args.split(/\s+/).filter(Boolean);
  for (let i = 1; i < t.length; i++) {
    if (t[i] === '-c' || t[i] === '-C' || t[i] === '--git-dir' || t[i] === '--work-tree') {
      i++;
      continue;
    }
    if (t[i].startsWith('-')) continue;
    return t[i];
  }
  return null;
}

export function isRoutine(e: ProcNode, rules: string[]): boolean {
  const cmd = e.eff ?? e.cmd ?? '';
  const args = e.cmdline ?? e.args ?? '';
  for (const r of rules) {
    if (r === 'shell-snapshot') {
      if (args.includes('/shell-snapshots/snapshot-')) return true;
      continue;
    }
    const [c, sub] = r.split(/\s+/, 2);
    if (c !== cmd) continue;
    if (!sub) return true;
    if (c === 'git' ? gitSub(args) === sub : args.split(/\s+/)[1] === sub) return true;
  }
  return false;
}

/** What "hide routine" takes out, with everything below it: a run of the
 *  same command, or a process on the routine list. The table view hides the
 *  same rows. */
export function isHidden(e: { k: string }, rules: string[]): boolean {
  return e.k === 'g' || (e.k === 'p' && isRoutine(e as ProcNode, rules));
}

/* --------------------------------------------------------------- boxes */

function nodeTip(e: ProcNode): string {
  return [
    e.cmdline || e.args || e.cmd || '(no record)',
    `pid ${e.pid ?? '?'}${e.ppid != null ? ` · parent ${e.ppid}` : ''}`,
    e.start != null ? `started ${clock(e.start)}${e.end != null ? `, ${e.ev ? 'exited' : 'last seen'} ${clock(e.end)}` : ''}` : null,
    e.cwd ? `in ${e.cwd}` : null,
    e.n > 1 ? `${fint(e.n)} processes in its subtree` : null,
    e.ev == null ? 'no exit in the read: it outlived it, or left no record' : null,
    ioTip(e.io, 'its own I/O'),
    ioTip(e.sub_io, 'with everything below it'),
  ].filter(Boolean).join('\n');
}

/** A box's third line, when anything moved: `Σ I/O 45 MB · disk 3 MB`. */
function ioLine(io: ProcIo | null | undefined, sum: boolean): [string, boolean][][] {
  const b = ioBrief(io);
  return b ? [[[(sum ? 'Σ ' : '') + b, false]]] : [];
}

function failed(e: ProcNode): boolean {
  return (e.exit_code != null && e.exit_code !== 0) || e.signal != null;
}

/** What a box says, as text: drawn by `Box`, and written out by the popped-out
 *  tree's SVG export, so the two say the same thing. */
export interface BoxText {
  kind: 'p' | 'g' | 'more';
  title: string;
  /** an agent's type, tagged beside its name */
  type: string | null;
  caret: string | null;
  /** the lines under the title, in pieces; a piece marked true is a failure */
  lines: [string, boolean][][];
  bad: boolean;
  /** the left rule's colour (a CSS colour, possibly a var()) */
  color: string | null;
  tip?: string;
}

export function boxText(e: Item, open: boolean, kids: number, root: boolean): BoxText {
  const caret = kids > 0 ? (open ? '▾' : `▸ ${fint(kids)}`) : null;
  if (e.k === 'p') {
    const bad = failed(e);
    // an agent's own process can be named for its runtime (`server`, `node`):
    // say which agent it is, as the table view does
    const type = (e.role === 'agent' || root) && e.agent_type ? e.agent_type : null;
    return {
      kind: 'p',
      title: e.label ?? e.cmd ?? '(no record)',
      type,
      caret,
      lines: [
        [[
          (e.dur != null ? dur(e.dur) : DASH) +
            (e.cpu != null ? ` · CPU ${dur(e.cpu)}` : '') +
            (e.sub_cpu != null && e.cpu != null && e.sub_cpu > e.cpu * 1.05 ? ` · Σ ${dur(e.sub_cpu)}` : ''),
          false,
        ]],
        [
          [e.n > 1 ? `${fint(e.n)} processes · ` : '', false],
          e.signal != null ? [`signal ${e.signal}`, true]
            : e.exit_code != null ? [`exit ${e.exit_code}`, bad]
            : [e.ev === 'truncated' ? 'running at the end' : 'no exit seen', false],
        ],
        // what it moved, with everything below it (Σ) where it has a subtree
        ...ioLine(e.sub_io ?? e.io, !!e.sub_io),
      ],
      bad,
      color: pcol(e.purpose),
      tip: nodeTip(e),
    };
  }
  if (e.k === 'g') {
    const fails = Object.entries(e.exits).filter(([k]) => k !== '0').reduce((a, [, n]) => a + n, 0);
    return {
      kind: 'g',
      title: `${e.motif.join(' › ')} ×${fint(e.reps)}`,
      type: null,
      caret,
      lines: [
        [[`${fint(e.n)} processes${e.sub_cpu != null ? ` · CPU ${dur(e.sub_cpu)}` : ''}`, false]],
        [
          fails ? [`${fint(fails)} failed`, true] : ['all exit 0', false],
          [e.distinct > 1 ? ` · ${fint(e.distinct)} distinct` : '', false],
        ],
        ...ioLine(e.sub_io, true),
      ],
      bad: false,
      color: null,
      tip: `${fint(e.count)} runs of the same shape in a row\n${e.samples.map((x) => `${x.n}× ${x.cmdline ?? x.args ?? ''}`).join('\n')}`,
    };
  }
  if (e.k === 'cap') {
    return { kind: 'more', title: `+${fint(e.more)} more`, type: null, caret: null, lines: [[['click to show them all', false]]], bad: false, color: null };
  }
  const n = e.k === 'rest' ? e.more : (e as ProcCount).n;
  const cmds = e.k === 'rest' ? [] : (e as ProcCount).cmds;
  return {
    kind: 'more',
    title: `+${fint(n)} more processes`,
    type: null,
    caret: null,
    lines: [
      [[e.k === 'rest' ? 'more runs of the same shape' : 'counted, not drawn: past the read’s budget', false]],
      ...ioLine(e.k === 'rest' ? null : (e as ProcCount).sub_io, true),
    ],
    bad: false,
    color: null,
    tip: cmds.length ? cmds.map(([c, k]) => `${k}× ${c}`).join('\n') : undefined,
  };
}

/** One box. `onCaret` makes its caret a button of its own, for a view where a
 *  click on the box selects it rather than opening it. */
export function Box({
  e,
  open,
  kids,
  root,
  onCaret,
}: {
  e: Item;
  open: boolean;
  kids: number;
  root: boolean;
  onCaret?: () => void;
}) {
  const t = boxText(e, open, kids, root);
  const caret = !t.caret ? null : onCaret ? (
    <button
      className="pd-caret pd-tog"
      aria-label={open ? 'close what it started' : `open the ${fint(kids)} it started`}
      onClick={(ev) => {
        ev.stopPropagation();
        onCaret();
      }}
      onDoubleClick={(ev) => ev.stopPropagation()}
    >
      {t.caret}
    </button>
  ) : (
    <span className="pd-caret">{t.caret}</span>
  );
  return (
    <div
      className={`pd-box${t.kind === 'g' ? ' group' : t.kind === 'more' ? ' more' : t.bad ? ' bad' : ''}`}
      data-tip={t.tip}
      style={t.color ? { borderLeftColor: t.color } : undefined}
    >
      <div className="pd-h">
        <b className={t.kind === 'more' ? undefined : 'mono'}>{t.title}</b>
        {t.type && <span className="pt-tag type">{t.type}</span>}
        {caret}
      </div>
      {t.lines.map((l, i) => (
        <div key={i} className="pd-s">
          {l.map(([s, bad], j) => (
            <span key={j} className={bad ? 'bad' : undefined}>
              {s}
            </span>
          ))}
        </div>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- tree */

export function hiddenCount(root: ProcNode, rules: string[]): number {
  let n = 0;
  const walk = (e: Item) => {
    for (const k of kidsOf(e)) {
      if (isHidden(k, rules)) n += k.k === 'g' || k.k === 'p' ? k.n : 0;
      else walk(k);
    }
  };
  walk(root);
  return n;
}

/** The boxes drawn: the open ones' children, less the routine when `hide`,
 *  at most MAX_KIDS under a parent not in `all`. Ids are the table view's. */
export function procNodes(root: ProcNode, open: Set<string>, hide: boolean, rules: string[], all: Set<string>): PNode[] {
  const nodes: PNode[] = [];
  const walk = (e: Item, id: string, parent: string | null) => {
    // children once, so each keeps the index the table view's ids use
    const full = kidsOf(e).map((k, i) => [k, i] as const);
    const kids = hide ? full.filter(([k]) => !isHidden(k, rules)) : full;
    nodes.push({ id, parent, data: { e, kids: kids.length } });
    if (!kids.length || !open.has(id)) return;
    const shown = all.has(id) ? kids : kids.slice(0, MAX_KIDS);
    shown.forEach(([k, i]) => walk(k, `${id}.${i}`, id));
    if (shown.length < kids.length) {
      nodes.push({ id: `${id}.+`, parent: id, data: { e: { k: 'cap', more: kids.length - shown.length, parent: id }, kids: 0 } });
    }
  };
  walk(root, 'r', null);
  return nodes;
}

/** What it takes to draw box `id`: the boxes to open above it, the parents
 *  whose "+N more" would cover it, and whether "hide routine" takes it out (the
 *  caller turns hiding off then, and the positions here assume it has). */
export function reveal(root: ProcNode, id: string, hide: boolean, rules: string[]): { open: string[]; all: string[]; hidden: boolean } {
  const path: [Item, number][] = [];
  let e: Item = root;
  for (const i of id.split('.').slice(1).map(Number)) {
    const k: Item | undefined = kidsOf(e)[i];
    if (!k) break;
    path.push([e, i]);
    e = k;
  }
  const hidden = hide && path.some(([p, i]) => isHidden(kidsOf(p)[i], rules));
  const still = hide && !hidden;
  const open: string[] = [];
  const all: string[] = [];
  let at = 'r';
  for (const [p, i] of path) {
    open.push(at);
    const pos = still ? kidsOf(p).slice(0, i).filter((c) => !isHidden(c, rules)).length : i;
    if (pos >= MAX_KIDS) all.push(at);
    at = `${at}.${i}`;
  }
  return { open, all, hidden };
}

export function ProcDiagram({
  root,
  open,
  setOpen,
  hide,
  rules,
  all,
  setAll,
}: {
  root: ProcNode;
  open: Set<string>;
  setOpen: (s: Set<string>) => void;
  hide: boolean;
  rules: string[];
  /** parents whose every child is shown, past the first MAX_KIDS */
  all: Set<string>;
  setAll: (s: Set<string>) => void;
}) {
  const nodes = procNodes(root, open, hide, rules, all);
  return (
    <TreeDiagram
      nodes={nodes}
      nodeW={206}
      nodeH={80}
      gapX={14}
      gapY={26}
      className="pd"
      onSelect={(id) => {
        const n = nodes.find((x) => x.id === id);
        if (!n) return;
        if (n.data.e.k === 'cap') {
          setAll(new Set([...all, n.data.e.parent]));
          return;
        }
        if (!n.data.kids) return;
        const next = new Set(open);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        setOpen(next);
      }}
      render={(n) => <Box e={n.data.e} open={open.has(n.id)} kids={n.data.kids} root={n.id === 'r'} />}
    />
  );
}
