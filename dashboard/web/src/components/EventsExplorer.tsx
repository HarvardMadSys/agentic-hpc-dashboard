/* Search / filter / export over `/api/events`.
 *
 * One query string serves three consumers: the filter bar writes it into the
 * URL, the table fetches it, and the Download button points at `/api/export`
 * with the identical string. That is how the download is guaranteed to be the
 * view -- there is no second place where a filter could be applied or dropped.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { EventsResponse, LiveEvent } from '../api/types';
import { exportUrl, getEvents } from '../api/client';
import { apiQuery, SCALAR_KEYS, countActiveScalars, windowBounds } from '../lib/filters';
import type { ScalarKey, UrlState } from '../lib/filters';
import { fint } from '../lib/format';
import { Panel } from './Panel';
import { EventTable } from '../tabs/LiveTab';

/** Field layout for the bar. `kind` only picks the input type. */
const FIELDS: { k: ScalarKey; lbl: string; kind: 'text' | 'num' | 'dt' | 'select'; opts?: string[] }[] =
  [
    { k: 'from', lbl: 'from', kind: 'dt' },
    { k: 'to', lbl: 'to', kind: 'dt' },
    { k: 'q', lbl: 'search (args)', kind: 'text' },
    { k: 'user', lbl: 'user', kind: 'text' },
    { k: 'host', lbl: 'host', kind: 'text' },
    { k: 'tool', lbl: 'tool', kind: 'text' },
    { k: 'agent_type', lbl: 'agent_type', kind: 'text' },
    { k: 'purpose', lbl: 'purpose', kind: 'text' },
    { k: 'bucket', lbl: 'bucket', kind: 'text' },
    { k: 'session_key', lbl: 'session_key', kind: 'text' },
    {
      k: 'sandbox',
      lbl: 'sandbox',
      kind: 'select',
      opts: ['sandboxed', 'unsandboxed', 'denied', 'unknown'],
    },
    { k: 'approval', lbl: 'approval', kind: 'select', opts: ['supervised', 'bypassed', 'unknown'] },
    { k: 'depth_min', lbl: 'depth ≥', kind: 'num' },
    { k: 'depth_max', lbl: 'depth ≤', kind: 'num' },
    { k: 'cpu_min', lbl: 'cpu_s ≥', kind: 'num' },
    { k: 'duration_min', lbl: 'duration_s ≥', kind: 'num' },
    { k: 'exit_code', lbl: 'exit_code', kind: 'num' },
    { k: 'signal', lbl: 'signal', kind: 'num' },
  ];

export function EventsExplorer({
  url,
  setUrl,
  windowMin,
}: {
  url: UrlState;
  setUrl: (patch: Partial<UrlState>, push?: boolean) => void;
  /** The window the rest of the page is showing, as the service resolved it. */
  windowMin?: number | null;
}) {
  /* The bar is a draft: typing must not refetch on every keystroke, so the
   * committed query is only advanced on submit (or Enter).
   *
   * The selected window enters the query as `from`/`to`, so "the download is
   * exactly the view" survives the window being narrowed -- otherwise the bench
   * would keep serving the whole feed while every chart above it showed an
   * hour. An explicit from/to typed into the bar still wins: that is the more
   * specific statement of intent, and `windowBounds` leaves it alone. */
  const query = useMemo(
    () => apiQuery(windowBounds({ ...url, cls: url.cls, scalars: url.scalars }, windowMin ?? null)),
    [url, windowMin],
  );
  const [pages, setPages] = useState<LiveEvent[][]>([]);
  const [resp, setResp] = useState<EventsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState(url.scalars);
  const committed = useRef(query);

  useEffect(() => {
    setDraft(url.scalars);
  }, [url.scalars]);

  const load = useCallback(
    async (q: string, cursor: number | string | null) => {
      setLoading(true);
      setError(null);
      try {
        const r = await getEvents(q, cursor);
        setResp(r);
        setPages((prev) => (cursor != null ? [...prev, r.rows] : [r.rows]));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        if (cursor == null) {
          setPages([]);
          setResp(null);
        }
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  /* refetch from page 1 whenever the committed query changes (incl. the class
   * filter, which is shared with every other tab) */
  useEffect(() => {
    committed.current = query;
    void load(query, null);
  }, [query, load]);

  const rows = useMemo(() => pages.flat(), [pages]);
  const apply = () =>
    setUrl({
      scalars: Object.fromEntries(
        Object.entries(draft).filter(([, v]) => v != null && v !== ''),
      ) as Partial<Record<ScalarKey, string>>,
    });
  const clear = () => {
    setDraft({});
    setUrl({ scalars: {} });
  };

  const nActive = countActiveScalars({ cls: url.cls, scalars: url.scalars });
  const dirty = SCALAR_KEYS.some((k) => (draft[k] || '') !== (url.scalars[k] || ''));

  return (
    <>
      <form
        className="fbar"
        onSubmit={(e) => {
          e.preventDefault();
          apply();
        }}
      >
        <div className="fgrid">
          {FIELDS.map((f) => (
            <div className="fld" key={f.k}>
              <label htmlFor={'f-' + f.k}>{f.lbl}</label>
              {f.kind === 'select' ? (
                <select
                  id={'f-' + f.k}
                  value={draft[f.k] ?? ''}
                  onChange={(e) => setDraft({ ...draft, [f.k]: e.target.value })}
                >
                  <option value="">any</option>
                  {f.opts!.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={'f-' + f.k}
                  type={f.kind === 'num' ? 'number' : f.kind === 'dt' ? 'text' : 'text'}
                  inputMode={f.kind === 'num' ? 'decimal' : undefined}
                  placeholder={f.kind === 'dt' ? 'YYYY-MM-DD HH:MM' : ''}
                  value={draft[f.k] ?? ''}
                  onChange={(e) => setDraft({ ...draft, [f.k]: e.target.value })}
                />
              )}
            </div>
          ))}
        </div>
        <div className="row">
          <span className="count">
            {/* The row count IS the confirmation that the download matches the
                view, so it names both what is loaded and what was scanned. */}
            <b>{fint(rows.length)}</b> rows loaded
            {resp?.total_scanned != null ? ` · ${fint(resp.total_scanned)} scanned` : ''}
            {resp?.truncated ? ' · truncated by the backend' : ''}
            {resp?.order === 'desc' ? ' \u00b7 newest first' : resp?.order === 'asc' ? ' \u00b7 oldest first' : ''}
            {resp?.event_filter_defaulted ? ' \u00b7 process exits only' : ''}
            {` · ${url.cls.length}/3 classes`}
            {nActive ? ` · ${nActive} filter${nActive > 1 ? 's' : ''}` : ''}
            {/* The window enters the query as from/to without appearing in the
                filter bar, so it is named here: an invisible bound on a row
                count is exactly the kind of thing that misleads. */}
            {windowMin != null && !url.scalars.from && !url.scalars.to
              ? ` · bounded to the selected window`
              : ''}
          </span>
          <span className="wrapctl">
            <button className="btn primary" type="submit" disabled={!dirty}>
              apply
            </button>
            <button className="btn" type="button" onClick={clear} disabled={!nActive && !dirty}>
              clear
            </button>
            <a
              className="btn"
              href={exportUrl(query)}
              download
              data-tip={`GET ${exportUrl(query)}`}
            >
              download JSONL
            </a>
          </span>
        </div>
      </form>

      <div className="grid wide" style={{ marginTop: 14 }}>
        <Panel
          title="Events"
          span
          feed="ebpfm"
          empty={!loading && !error && !rows.length}
          emptyWhat="no event matches this filter"
          header={
            <span className="pill">{loading ? 'loading' : `${fint(rows.length)} rows`}</span>
          }
        >
          {error && <div className="err">{error}</div>}
          <div className="stream" style={{ maxHeight: 620 }}>
            <EventTable rows={rows} />
          </div>
          <div className="wrapctl">
            <button
              className="btn"
              disabled={!resp?.next_cursor || loading}
              onClick={() => resp?.next_cursor && void load(committed.current, resp.next_cursor)}
            >
              {resp?.next_cursor ? 'load more' : 'no more pages'}
            </button>
            {resp?.truncated && (
              <span className="cov low">
                the backend stopped scanning early; narrow the window for a complete answer
              </span>
            )}
          </div>
        </Panel>
      </div>
    </>
  );
}
