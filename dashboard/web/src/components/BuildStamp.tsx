/* The last lines of the rail: which code this page is -- the commit it was
 * built from and when that build ran, stamped by vite.config.ts on each build.
 *
 * It describes the bundle on screen. A page left open across a rebuild keeps
 * its stamp until it is reloaded, as it keeps the code. */
import info from 'virtual:build-info';
import { DASH, ago } from '../lib/format';

/** `YYYY-MM-DD HH:MM` in local time, as the page's other clocks read. */
function local(d: Date): string {
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16)
    .replace('T', ' ');
}

export function BuildStamp() {
  const { commit, dirty, builtAt } = info;
  const built = builtAt ? new Date(builtAt) : null;
  return (
    <div className="build">
      <span
        data-tip={
          commit == null
            ? 'not built from a git checkout, or git was not on the build host: the page cannot say which commit it is'
            : dirty
              ? `built from ${commit}\nwith uncommitted changes under dashboard/web/: the page is that commit plus those edits`
              : `the git commit this page was built from\n${commit}`
        }
      >
        commit <span style={{ color: 'var(--ink-2)' }}>{commit ? commit.slice(0, 7) : DASH}</span>
        {dirty && <span style={{ color: 'var(--ink-2)' }}> + edits</span>}
      </span>
      <br />
      {built ? (
        <span data-tip={`when this page's bundle was built (vite build)\n${builtAt} · ${ago(Date.now() - built.getTime())}`}>
          built <span style={{ color: 'var(--ink-2)' }}>{local(built)}</span>
        </span>
      ) : (
        <span data-tip="Vite's dev server serves src/ from the working tree, with hot reload: there is no bundle, so no build time">
          built <span style={{ color: 'var(--ink-2)' }}>{DASH}</span> (dev server)
        </span>
      )}
    </div>
  );
}
