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
import type { ProcCount, ProcEntry, ProcGroup, ProcNode } from '../api/types';
import { DASH, clock, dur, fint } from '../lib/format';
import { pcol } from '../lib/purposes';
import { TreeDiagram } from './TreeDiagram';
import type { TreeNode } from './TreeDiagram';

type Rest = { k: 'rest'; more: number; of: ProcGroup };
type Cap = { k: 'cap'; more: number; parent: string };
type Item = ProcEntry | Rest | Cap;

const MAX_KIDS = 30;

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

/* --------------------------------------------------------------- boxes */

function nodeTip(e: ProcNode): string {
  return [
    e.cmdline || e.args || e.cmd || '(no record)',
    `pid ${e.pid ?? '?'}${e.ppid != null ? ` · parent ${e.ppid}` : ''}`,
    e.start != null ? `started ${clock(e.start)}${e.end != null ? `, ${e.ev ? 'exited' : 'last seen'} ${clock(e.end)}` : ''}` : null,
    e.cwd ? `in ${e.cwd}` : null,
    e.n > 1 ? `${fint(e.n)} processes in its subtree` : null,
    e.ev == null ? 'no exit in the read: it outlived it, or left no record' : null,
  ].filter(Boolean).join('\n');
}

function failed(e: ProcNode): boolean {
  return (e.exit_code != null && e.exit_code !== 0) || e.signal != null;
}

function Box({ e, open, kids }: { e: Item; open: boolean; kids: number }) {
  const caret = kids > 0 ? <span className="pd-caret">{open ? '▾' : `▸ ${fint(kids)}`}</span> : null;
  if (e.k === 'p') {
    const bad = failed(e);
    return (
      <div className={`pd-box${bad ? ' bad' : ''}`} data-tip={nodeTip(e)} style={{ borderLeftColor: pcol(e.purpose) }}>
        <div className="pd-h">
          <b className="mono">{e.label ?? e.cmd ?? '(no record)'}</b>
          {caret}
        </div>
        <div className="pd-s">
          {e.dur != null ? dur(e.dur) : DASH}
          {e.cpu != null ? ` · CPU ${dur(e.cpu)}` : ''}
          {e.sub_cpu != null && e.cpu != null && e.sub_cpu > e.cpu * 1.05 ? ` · Σ ${dur(e.sub_cpu)}` : ''}
        </div>
        <div className="pd-s">
          {e.n > 1 ? `${fint(e.n)} processes · ` : ''}
          {e.signal != null ? <span className="bad">signal {e.signal}</span>
            : e.exit_code != null ? <span className={bad ? 'bad' : undefined}>exit {e.exit_code}</span>
            : e.ev === 'truncated' ? 'running at the end' : 'no exit seen'}
        </div>
      </div>
    );
  }
  if (e.k === 'g') {
    const fails = Object.entries(e.exits).filter(([k]) => k !== '0').reduce((a, [, n]) => a + n, 0);
    return (
      <div className="pd-box group" data-tip={`${fint(e.count)} runs of the same shape in a row\n${e.samples.map((x) => `${x.n}× ${x.cmdline ?? x.args ?? ''}`).join('\n')}`}>
        <div className="pd-h">
          <b className="mono">{e.motif.join(' › ')} ×{fint(e.reps)}</b>
          {caret}
        </div>
        <div className="pd-s">{fint(e.n)} processes{e.sub_cpu != null ? ` · CPU ${dur(e.sub_cpu)}` : ''}</div>
        <div className="pd-s">{fails ? <span className="bad">{fint(fails)} failed</span> : 'all exit 0'}{e.distinct > 1 ? ` · ${fint(e.distinct)} distinct` : ''}</div>
      </div>
    );
  }
  if (e.k === 'cap') {
    return (
      <div className="pd-box more">
        <div className="pd-h"><b>+{fint(e.more)} more</b></div>
        <div className="pd-s">click to show them all</div>
      </div>
    );
  }
  const n = e.k === 'rest' ? e.more : (e as ProcCount).n;
  const cmds = e.k === 'rest' ? [] : (e as ProcCount).cmds;
  return (
    <div className="pd-box more" data-tip={cmds.length ? cmds.map(([c, k]) => `${k}× ${c}`).join('\n') : undefined}>
      <div className="pd-h"><b>+{fint(n)} more processes</b></div>
      <div className="pd-s">{e.k === 'rest' ? 'more runs of the same shape' : 'counted, not drawn: past the read’s budget'}</div>
    </div>
  );
}

/* ---------------------------------------------------------------- tree */

export function hiddenCount(root: ProcNode, rules: string[]): number {
  let n = 0;
  const walk = (e: Item) => {
    for (const k of kidsOf(e)) {
      if (k.k === 'g') n += k.n;
      else if (k.k === 'p' && isRoutine(k, rules)) n += k.n;
      else walk(k);
    }
  };
  walk(root);
  return n;
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
  const nodes: TreeNode<{ e: Item; kids: number }>[] = [];
  const walk = (e: Item, id: string, parent: string | null) => {
    // children once, so each keeps the index the table view's ids use
    const full = kidsOf(e).map((k, i) => [k, i] as const);
    const kids = hide ? full.filter(([k]) => k.k !== 'g' && !(k.k === 'p' && isRoutine(k, rules))) : full;
    nodes.push({ id, parent, data: { e, kids: kids.length } });
    if (!kids.length || !open.has(id)) return;
    const shown = all.has(id) ? kids : kids.slice(0, MAX_KIDS);
    shown.forEach(([k, i]) => walk(k, `${id}.${i}`, id));
    if (shown.length < kids.length) {
      nodes.push({ id: `${id}.+`, parent: id, data: { e: { k: 'cap', more: kids.length - shown.length, parent: id }, kids: 0 } });
    }
  };
  walk(root, 'r', null);
  return (
    <TreeDiagram
      nodes={nodes}
      nodeW={206}
      nodeH={64}
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
      render={(n) => <Box e={n.data.e} open={open.has(n.id)} kids={n.data.kids} />}
    />
  );
}
