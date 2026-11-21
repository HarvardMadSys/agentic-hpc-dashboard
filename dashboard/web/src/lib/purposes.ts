/* Purpose colours: the service's purposes, drawn as three kinds in three of the
 * page's series slots and one grey `other` -- never a generated hue.
 *
 * The service names a process's purpose finely (`_BUILTIN_PURPOSE` in
 * rc_dashboard/normalize.py: `poll`, `slurm_mon`, `slurm_sub`, `build`,
 * `compute`, `filesearch`, `data`, `runtime`, `git`, `editor`, else `other`), and
 * the filters, the exports and a session's waiting count read those words. The
 * page colours the kind a purpose is, not each purpose: nine swatches were more
 * key than a ribbon needs, and two of their pairs could not be told apart under
 * colour-blind simulation (build and slurm monitor, file & search and compute).
 * So squeue and sbatch are both `slurm`, make and python both `build & compute`,
 * rg and rsync both `files & data`, and polling joins git, the agent's runtime
 * and the editors in `other`: a poll loop of a thousand pgreps is most of a
 * ribbon however little of the work it was, and it no longer paints it crimson.
 * A run, a process and a tooltip still name the purpose itself (`plbl`); only
 * the swatch is the kind's.
 *
 * A kind keeps its phase's colour (lib/phases.ts) where it can: building blue,
 * exploring files green. Slurm takes the violet, not the phases' purple, which
 * is ΔE 3.4 from the blue under deuteranopia. Any two runs can touch, so the
 * three are validated on every pair: worst colour-blind ΔE 11.4 and worst
 * normal-vision ΔE 17.0 (both blue and violet), each at least 3:1 against the
 * surface.
 */
import { table } from './lookup';

/** A kind a purpose is drawn as. */
export type PurposeKind = 'slurm' | 'compute' | 'files' | 'other';

/** The kinds, in the order they stack and are keyed: each its label, colour, the service's purposes it holds and
 *  what its swatch's tooltip says they are. `other` holds every purpose the others do not. */
export const KINDS: [kind: PurposeKind, label: string, colour: string, holds: string[], tip: string][] = [
  [
    'slurm',
    'slurm',
    'var(--s5)',
    ['slurm_mon', 'slurm_sub'],
    'slurm monitor (squeue, sacct, sinfo, …) and slurm submit (sbatch, srun, salloc, scancel)',
  ],
  [
    'compute',
    'build & compute',
    'var(--s2)',
    ['build', 'compute'],
    'build / compile (make, gcc, nvcc, cargo, …) and compute (python, R, julia, …)',
  ],
  [
    'files',
    'files & data',
    'var(--s3)',
    ['filesearch', 'data'],
    'file & search (grep, rg, find, ls, cat, sed, …) and data move (cp, mv, rsync, tar, curl, …)',
  ],
  [
    'other',
    'other (git, poll, agent runtime, …)',
    'var(--sg)',
    [],
    'git, poll / introspection (ps, pgrep, top, nvidia-smi, …), agent runtime (node, claude, codex, bwrap, …), ' +
      'editors, and every program with no purpose of its own',
  ],
];

/** The words for a purpose the service names, as a run or a process is labelled. The rest (`git`, `editor`, `other`)
 *  read as the service's word. */
const FINE: Record<string, string> = table({
  poll: 'poll / introspection',
  slurm_mon: 'slurm monitor',
  slurm_sub: 'slurm submit',
  build: 'build / compile',
  compute: 'compute (py/R)',
  filesearch: 'file & search',
  data: 'data move',
  runtime: 'agent runtime',
});

// tables with no prototype: a purpose is the service's word (lib/lookup.ts)
const KIND_OF: Record<string, PurposeKind> = table({});
const KIND_COL: Record<string, string> = table({});
const KIND_LBL: Record<string, string> = table({});
for (const [kind, l, c, holds] of KINDS) {
  KIND_OF[kind] = kind;
  for (const p of holds) KIND_OF[p] = kind;
  KIND_COL[kind] = c;
  KIND_LBL[kind] = l;
}

/** The kind a purpose (or a kind) is drawn as: anything the table does not hold is `other`. */
export const pkind = (k: string | null | undefined): PurposeKind => KIND_OF[k ?? ''] ?? 'other';
/** Colour for a purpose: its kind's. */
export const pcol = (k: string | null | undefined): string => KIND_COL[pkind(k)];
/** The words for a purpose itself, not its kind. */
export const plbl = (k: string | null | undefined): string => FINE[k ?? ''] ?? (k || 'other');
/** The words for a kind. */
export const klbl = (kind: PurposeKind): string => KIND_LBL[kind];

export const purposeLegend = () => KINDS.map(([, l, c, , tip]) => ({ k: l, c, tip }));
