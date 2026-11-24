/* The page while there is nothing to show: no feed holds a record.
 *
 * Before this, an instance pointed at the wrong folder drew every tab in full:
 * twenty panels each saying "empty" in the same words, a green "nothing
 * flagged" above them, and a green streaming light beside -- a first run that
 * looked like a quiet cluster. Now the tabs give way to one screen that says
 * the dashboard has no data, where it looked (each folder, and what it found
 * there), and the three steps that fix it. The Feeds tab still opens, since
 * that is where the detail is; the moment any feed holds a record the tabs
 * return, and an empty panel means what it always meant.
 */
import type { FeedReport, FeedsResponse } from '../api/types';
import { feedLabel } from '../lib/feeds';

/** No feed has a record: every one missing or empty, none of it the demo's. */
export function noData(feeds: FeedsResponse | null | undefined): boolean {
  if (!feeds) return false;
  const all = Object.values(feeds);
  return all.length > 0 && all.every((f) => (f.status === 'missing' || f.status === 'empty') && !f.synthetic_hosts?.length);
}

type Where = { path: string; state: string; note?: string | null };

/** Where a feed looked, folder by folder: the service's own account of it when
 *  it gives one (`candidates`), else what the report's lists say. */
function looked(f: FeedReport): Where[] {
  if (f.candidates?.length) return f.candidates;
  const seen = new Set<string>();
  const out: Where[] = [];
  for (const p of f.resolved ?? []) {
    seen.add(p);
    out.push({ path: p, state: 'empty' });
  }
  for (const p of [...(f.missing ?? []), ...(f.configured ?? [])]) {
    if (seen.has(p)) continue;
    seen.add(p);
    out.push({ path: p, state: 'missing' });
  }
  return out;
}

const STATE_WORDS: Record<string, string> = {
  found: 'found',
  empty: 'there, but holds no day-files',
  missing: 'no such folder',
  denied: 'permission denied',
};

export function SetupScreen({ feeds, goFeeds }: { feeds: FeedsResponse; goFeeds: () => void }) {
  const list = Object.values(feeds).sort((a, b) => Number(b.required) - Number(a.required));
  return (
    <section className="panel setup span2" id="sec-no-data" aria-labelledby="setup-h">
      <header>
        <div>
          <h3 id="setup-h">No data yet</h3>
        </div>
        <div className="wrapctl">
          <span className="pill stale">nothing measured</span>
        </div>
      </header>
      <div className="setup-body">
        <p className="setup-lede">
          The dashboard is running, but none of the collector&rsquo;s day-files are where it looks, so there is
          nothing to show. Every tab returns as soon as one feed holds a record.
        </p>

        <h4>Where it looked</h4>
        {list.map((f) => (
          <div className="setup-feed" key={f.name}>
            <div className="setup-feed-h">
              <b>{f.label || feedLabel(f.name)}</b>
              <span className="mono"> {f.name}</span>
              {f.required ? <span className="setup-req"> · required</span> : <span className="setup-opt"> · optional</span>}
            </div>
            <ul className="setup-paths mono">
              {looked(f).map((w) => (
                <li key={w.path} className={`st-${w.state}`}>
                  <span className="setup-path">{w.path}</span>
                  <span className="setup-state">
                    {' '}
                    — {STATE_WORDS[w.state] ?? w.state}
                    {w.note ? ` (${w.note})` : ''}
                  </span>
                </li>
              ))}
              {!looked(f).length && <li className="st-missing">no folder configured</li>}
            </ul>
          </div>
        ))}

        <h4>What to do</h4>
        <ol className="setup-steps">
          <li>
            On each login node, check that the collector is running, and see where it writes:
            <pre className="mono">sudo ./collector/ebpfm.sh status</pre>
            (<code>sudo ebpfm status</code> where the RPM is installed). Not installed yet:{' '}
            <code>sudo ./collector/ebpfm.sh install</code>
          </li>
          <li>
            Point the dashboard at those folders, then start it again. <code>rc-dashboard init</code> finds them
            and writes the config for you; or set them yourself:
            <pre className="mono">RC_DASH_EBPF_ROOTS=/scratch/ebpfm RC_DASH_NODE_ROOTS=/scratch/ebpfm-login rc-dashboard serve</pre>
          </li>
          <li>
            See what it finds, folder by folder:
            <pre className="mono">rc-dashboard check-feeds</pre>
          </li>
        </ol>
        <p className="setup-foot">
          Only looking around? <code>rc-dashboard demo</code> serves this page over made-up data. Every feed and
          setting, and what each resolved to:{' '}
          <button className="linkbtn" onClick={goFeeds}>
            Feeds →
          </button>{' '}
          · More causes and fixes: <code>docs/troubleshooting.md</code> in the repository.
        </p>
      </div>
    </section>
  );
}
