/* Purpose colours: eight validated series slots, the tail folded into one grey
 * `other` -- never a generated ninth hue.
 *
 * The eight slots are ported verbatim from the archived template. The backend's
 * purpose vocabulary is wider than eight (`git`, `editor`, `env`, `guard`, ...),
 * so anything outside the table folds to `other` rather than earning a colour.
 */

export const PURP: [key: string, label: string, colour: string, short: string][] = [
  ['poll', 'poll / introspection', 'var(--s1)', 'poll'],
  ['runtime', 'agent runtime', 'var(--s7)', 'runtime'],
  ['filesearch', 'file & search', 'var(--s3)', 'files'],
  ['slurm_mon', 'slurm monitor', 'var(--s4)', 'squeue'],
  ['slurm_sub', 'slurm submit', 'var(--s5)', 'sbatch'],
  ['build', 'build / compile', 'var(--s2)', 'build'],
  ['compute', 'compute (py/R)', 'var(--s6)', 'compute'],
  ['data', 'data move', 'var(--s8)', 'data'],
  ['other', 'other (git, editor, env, …)', 'var(--sg)', 'other'],
];

const PURP_COL: Record<string, string> = {};
const PURP_LBL: Record<string, string> = {};
const PURP_SHORT: Record<string, string> = {};
for (const [k, l, c, s] of PURP) {
  PURP_COL[k] = c;
  PURP_LBL[k] = l;
  PURP_SHORT[k] = s;
}

/** Colour for a purpose. Anything off the eight-slot table gets the grey tail. */
export const pcol = (k: string | null | undefined): string => PURP_COL[k ?? ''] ?? 'var(--sg)';
export const plbl = (k: string | null | undefined): string => PURP_LBL[k ?? ''] ?? (k || 'other');

export const purposeLegend = () => PURP.map(([, l, c]) => ({ k: l, c }));
