/* Which code this page is -- the commit it was built from and when that build
 * ran, stamped by vite.config.ts on each build -- in words, for the hover of
 * the rail's status line (App.tsx).
 *
 * It describes the bundle on screen. A page left open across a rebuild keeps
 * its stamp until it is reloaded, as it keeps the code. */
import info from 'virtual:build-info';
import { DASH, ago } from './format';

/** `YYYY-MM-DD HH:MM` in local time, as the page's other clocks read. */
function local(d: Date): string {
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16)
    .replace('T', ' ');
}

/** Two lines: the commit, then the build. A page built without git, or by the
 *  dev server, says so rather than guess. */
export function buildStamp(): string {
  const { commit, dirty, builtAt } = info;
  const built = builtAt ? new Date(builtAt) : null;
  const commitLine =
    commit == null
      ? `commit ${DASH} (not built from a git checkout, or no git on the build host)`
      : `commit ${commit.slice(0, 7)}${dirty ? ' + uncommitted edits under dashboard/web/' : ''}`;
  const builtLine = built
    ? `built ${local(built)} (${ago(Date.now() - built.getTime())})`
    : `built ${DASH} (the dev server: no bundle)`;
  return `${commitLine}\n${builtLine}`;
}
