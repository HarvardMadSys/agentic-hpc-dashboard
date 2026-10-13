/* Sandbox and approval: one place for what each state key is CALLED, and for the
 * one rule every share obeys.
 *
 * The keys are the payload's and the filters' (`/api/events?sandbox=...`), so a
 * key keeps its meaning and only its label says what is measured:
 *
 *   sandboxed   -> "namespace-isolated": /proc/<pid>/ns/* differs from pid 1's.
 *                  Landlock and seccomp-only sandboxes are invisible to it, and a
 *                  container or a systemd unit counts.
 *   supervised  -> "no bypass flag at launch": argv cannot see settings files,
 *                  config.toml or a mid-session mode switch.
 *   denied      -> "exited before read": the collector is root; the namespace
 *                  links were simply gone.
 *
 * A share is ALWAYS a state over the KNOWN verdicts, with coverage (known/total)
 * beside it. Dividing by every state put 9,330 unknown trees under 40 known ones
 * and printed "0% sandboxed", and it made `denied` deflate the rate exactly as
 * folding it into "not isolated" would.
 */
import { fint } from './format';

export const SB_LABEL: Record<string, string> = {
  sandboxed: 'namespace-isolated',
  unsandboxed: 'not isolated',
  denied: 'exited before read',
  unknown: 'unknown',
};

/** Session grain: the outcome of a session's tool calls, not its root's own verdict. */
export const SESSION_LABEL: Record<string, string> = {
  sandboxed: 'all tool calls isolated',
  partial: 'some tool calls isolated',
  failed: 'sandbox attempts failed',
  unsandboxed: 'tool calls not isolated',
  unknown: 'tool calls unreadable',
  no_calls: 'no tool calls seen',
};

export const CALL_LABEL: Record<string, string> = {
  isolated: 'isolated',
  not_isolated: 'not isolated',
  failed: 'attempt failed (no payload ran)',
  no_payload: 'wrapper ran nothing (probe)',
  unknown: 'payload unreadable',
};

export const AP_LABEL: Record<string, string> = {
  bypassed: 'bypass flag at launch',
  auto: 'auto mode (classifier approves)',
  supervised: 'no bypass flag at launch',
  unobservable: 'not observable from argv',
  na: 'n/a (not an agent session)',
  unknown: 'unknown',
};

/** The same states, short enough for a table cell; the full label goes in its tooltip. */
export const AP_SHORT: Record<string, string> = {
  bypassed: 'bypass flag',
  auto: 'auto mode',
  supervised: 'no bypass flag',
  unobservable: 'not observable',
  na: 'n/a',
  unknown: 'unknown',
};

/** The agent sessions a plate should worry about, as one line under its
 *  number: launched with a bypass flag, in auto mode, and (where it was
 *  measured) running tool calls outside a sandbox. Each reads even at 0: none
 *  is a finding. "No bypass flag" is left out: argv cannot vouch for it. */
export function riskLine(
  launch: Partial<Record<string, number>> | null | undefined,
  unsandboxed?: number | null,
): string | undefined {
  if (!launch) return undefined;
  return [
    `${fint(launch.bypassed ?? 0)} with a bypass flag`,
    `${fint(launch.auto ?? 0)} in auto mode`,
    unsandboxed != null ? `${fint(unsandboxed)} unsandboxed` : null,
  ]
    .filter(Boolean)
    .join(' · ');
}

const HATCH_UNKNOWN = 'url(#hatchUnknown)';
const HATCH_DENIED = 'url(#hatchDenied)';

/** Fills for charts; the legend twin uses a flat colour for the hatched ones. */
export const SB_COL: Record<string, string> = {
  sandboxed: 'var(--s5)',
  unsandboxed: 'var(--s6)',
  denied: HATCH_DENIED,
  unknown: HATCH_UNKNOWN,
  partial: 'var(--s8)',
  failed: 'var(--s4)',
  no_calls: 'var(--muted)',
  isolated: 'var(--s5)',
  not_isolated: 'var(--s6)',
  no_payload: 'var(--muted)',
};
export const AP_COL: Record<string, string> = {
  bypassed: 'var(--crit)',
  auto: 'var(--warn)',
  supervised: 'var(--s2)',
  unobservable: 'var(--sg)',
  na: 'var(--muted)',
  unknown: HATCH_UNKNOWN,
};

/** A legend swatch cannot show a pattern, so the hatched states get their base hue. */
export function legendCol(c: string | undefined): string {
  if (c === HATCH_UNKNOWN) return 'var(--edge)';
  if (c === HATCH_DENIED) return 'var(--sg)';
  return c ?? 'var(--sg)';
}

export function label(map: Record<string, string>, k: string): string {
  return map[k] ?? k;
}

export interface Share {
  /** state / known, in percent; null when nothing is known */
  pct: number | null;
  /** known / total, in percent; null when there is nothing at all */
  coverage: number | null;
  known: number;
  total: number;
}

/** `state` as a share of the KNOWN verdicts, with the coverage that qualifies it. */
export function shareOfKnown(
  m: Record<string, number> | undefined | null,
  states: string | string[],
  known: string[],
): Share {
  const want = Array.isArray(states) ? states : [states];
  const vals = m ?? {};
  const total = Object.values(vals).reduce((a, b) => a + b, 0);
  const k = known.reduce((a, s) => a + (vals[s] ?? 0), 0);
  const v = want.reduce((a, s) => a + (vals[s] ?? 0), 0);
  return {
    pct: k ? (100 * v) / k : null,
    coverage: total ? (100 * k) / total : null,
    known: k,
    total,
  };
}

export const SANDBOX_KNOWN = ['sandboxed', 'unsandboxed'];
export const SESSION_KNOWN = ['sandboxed', 'partial', 'failed', 'unsandboxed'];
export const CALL_KNOWN = ['isolated', 'not_isolated', 'failed'];
export const APPROVAL_KNOWN = ['bypassed', 'auto', 'supervised'];

export const SANDBOX_ORDER = ['sandboxed', 'unsandboxed', 'denied', 'unknown'];
export const SESSION_ORDER = ['sandboxed', 'partial', 'failed', 'unsandboxed', 'unknown', 'no_calls'];
export const CALL_ORDER = ['isolated', 'not_isolated', 'failed', 'unknown', 'no_payload'];
export const APPROVAL_ORDER = ['bypassed', 'auto', 'supervised', 'unobservable', 'na', 'unknown'];
