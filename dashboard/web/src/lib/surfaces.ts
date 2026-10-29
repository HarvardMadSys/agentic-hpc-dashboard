/* Where an agent session is driven from: its launch surface, read off the
 * agent's own command line (`rc_dashboard/normalize.py: surface_of`).
 *
 * Five surfaces in two groups -- CLI: a terminal or a script; app: a desktop
 * app, an IDE extension, or the web -- and `unknown` where the command line
 * does not say, which is never filled in with a guess: a Codex app-server
 * daemon serves whichever client attaches to it.
 */
import type { Surface } from '../api/types';
import { fint } from './format';

export type SurfaceGroup = 'CLI' | 'app';

export const SURFACE_ORDER: Surface[] = ['terminal', 'script', 'desktop', 'extension', 'web', 'unknown'];

export const SURFACE_GROUP: Record<Surface, SurfaceGroup | null> = {
  terminal: 'CLI',
  script: 'CLI',
  desktop: 'app',
  extension: 'app',
  web: 'app',
  unknown: null,
};

/** The surface as a legend or a count names it. */
export const SURFACE_LBL: Record<Surface, string> = {
  terminal: 'terminal',
  script: 'script',
  desktop: 'desktop app',
  extension: 'IDE extension',
  web: 'web / mobile',
  unknown: 'launcher unknown',
};

/** What a chip names after its group: the IDE, where the extension's path says which. */
export function surfaceShort(s: Surface, detail?: string | null): string {
  switch (s) {
    case 'desktop':
      return 'desktop';
    case 'extension':
      return detail ?? 'IDE';
    case 'web':
      return 'web';
    default:
      return SURFACE_LBL[s];
  }
}

/* What each detail the service sends means, in the reader's words. */
const DETAIL_TIP: Record<string, string> = {
  interactive: 'a person at the agent’s own interactive screen, in a terminal (tmux and an IDE’s terminal included)',
  'background session': 'a background session that `claude daemon` keeps for its user, who attaches to it from a terminal',
  'claude -p': 'a program runs `claude -p`, with no one at a terminal',
  '-p': 'a program runs it with `-p`, with no one at a terminal',
  'Agent SDK': 'a program drives it through the Agent SDK (stream-json), with no one at a terminal',
  'MCP server': 'it serves another program as an MCP server',
  'codex exec': 'a program runs `codex exec`, with no one at a terminal',
  'Codex SDK': 'a program drives it through the Codex SDK (`codex exec --experimental-json`)',
  'Claude desktop app': 'the Claude desktop app’s Code tab, over SSH: the programs it installs under `~/.claude/remote/`',
  'Codex app': 'the Codex app, over SSH: an app server on a unix socket, started with code mode on',
  'Claude Remote Control': 'Remote Control: driven from claude.ai or the Claude app on a phone',
  'Codex Remote Control': 'Codex Remote Control: driven from another device',
  'shared app server':
    'a Codex app-server daemon serves whichever client attaches to it, so its own command line names none, and no terminal of its user started it',
  'app server': 'a Codex app server on stdio, started by a program whose own record is not here',
};

/** Sessions by group in words -- "12 from a CLI · 5 from an app" -- and each
 *  surface's count for the hover; null when there are none to count. */
export function surfaceSplit(
  by: Partial<Record<Surface, number>> | null | undefined,
): { text: string; tip: string } | null {
  const n = (s: Surface) => by?.[s] ?? 0;
  const cli = n('terminal') + n('script');
  const app = n('desktop') + n('extension') + n('web');
  const unknown = n('unknown');
  if (!cli && !app && !unknown) return null;
  const parts = [`${fint(cli)} from a CLI`, `${fint(app)} from an app`];
  if (unknown) parts.push(`${fint(unknown)} launcher unknown`);
  const lines = SURFACE_ORDER.filter((s) => n(s)).map((s) => {
    const g = SURFACE_GROUP[s];
    return `${g ? `${g} · ` : ''}${SURFACE_LBL[s]}: ${fint(n(s))}`;
  });
  return {
    text: parts.join(' · '),
    tip: ['agent sessions by where each is driven from, read off the agent’s own command line', ...lines].join('\n'),
  };
}

/** A chip's tooltip: its group and surface, and what that means here. */
export function surfaceTip(s: Surface, detail?: string | null): string {
  const g = SURFACE_GROUP[s];
  const name = s === 'extension' ? `${detail ?? 'IDE'} extension` : SURFACE_LBL[s];
  const why =
    s === 'extension'
      ? `the ${detail ?? 'IDE'} extension’s own bundled program runs it`
      : s === 'unknown' && !detail
        ? 'the agent’s own record was not read, so nothing says where it was driven from'
        : (detail && DETAIL_TIP[detail]) || '';
  return `${g ? `${g} · ` : ''}${name}${why ? `: ${why}` : ''}\nread off the agent’s own command line`;
}
