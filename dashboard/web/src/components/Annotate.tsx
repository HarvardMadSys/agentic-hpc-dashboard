/* "Annotate purposes": what each tool call of the open session was for, in a
 * model's words (rc_dashboard/annotate.py).
 *
 * The inspector opens with what is already known -- any reader's earlier
 * answers, kept by the service -- and asks the model about the rest only when
 * the reader presses the button (or the site set `annotate.auto`). Nothing is
 * posted but the session's name and read: the service takes the calls from its
 * own read of the session, as it served it. Every purpose is marked as a
 * model's and named for the model that wrote it. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getAnnotateInfo, getAnnotateJob, postAnnotate } from '../api/client';
import type { AnnotateInfo, AnnotateJob, AnnotateResults, TrajOrder } from '../api/types';
import type { LlmPurposeSet } from '../lib/annotate';
import { fint } from '../lib/format';

const POLL_MS = 1500;

export interface AnnotateState {
  info: AnnotateInfo | null;
  set: LlmPurposeSet | null;
  /** distinct tool calls taken, and how many the session has */
  calls: number;
  total: number;
  job: AnnotateJob | null;
  err: string | null;
  busy: boolean;
  run: () => void;
}

let infoOnce: Promise<AnnotateInfo | null> | null = null;
/** Asked once per page: it changes only with the service's config. */
function annotateInfo(): Promise<AnnotateInfo | null> {
  if (!infoOnce) infoOnce = getAnnotateInfo().catch(() => null);
  return infoOnce;
}

/** `ready` is the read on screen (a key that changes with it), or null while
 *  none is: the calls are that read's. */
export function useAnnotate(
  sid: string,
  win: number | null,
  from: number | null,
  to: number | null,
  order: TrajOrder,
  ready: string | null,
): AnnotateState {
  const [info, setInfo] = useState<AnnotateInfo | null>(null);
  const [by, setBy] = useState<AnnotateResults>({});
  const [counts, setCounts] = useState({ calls: 0, total: 0 });
  const [job, setJob] = useState<AnnotateJob | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const live = useRef<AbortController | null>(null);

  useEffect(() => {
    let on = true;
    annotateInfo().then((i) => on && setInfo(i));
    return () => {
      on = false;
    };
  }, []);

  // jobs still followed: busy until the last of them ends
  const pending = useRef(0);
  const ended = useCallback(() => {
    pending.current = Math.max(0, pending.current - 1);
    if (!pending.current) setBusy(false);
  }, []);

  /** Follow a job to its end. `main` is the one the status line reports;
   *  another request's job, followed alongside, only adds its answers. */
  const poll = useCallback((j: AnnotateJob | string, ac: AbortController, main: boolean) => {
    pending.current += 1;
    const step = (cur: AnnotateJob) => {
      if (main) setJob(cur);
      setBy((b) => ({ ...b, ...cur.results }));
      if (cur.state === 'done' || cur.state === 'error') {
        if (cur.state === 'error' && main) setErr(cur.error);
        ended();
        return;
      }
      setTimeout(() => {
        if (ac.signal.aborted) return;
        getAnnotateJob(cur.id, ac.signal)
          .then(step)
          .catch((e) => {
            if (ac.signal.aborted) return;
            if (main) setErr(String(e?.message ?? e));
            ended();
          });
      }, POLL_MS);
    };
    if (typeof j === 'string') {
      getAnnotateJob(j, ac.signal)
        .then(step)
        .catch(() => !ac.signal.aborted && ended());
    } else step(j);
  }, [ended]);

  const ask = useCallback(
    (run: boolean) => {
      live.current?.abort();
      const ac = new AbortController();
      live.current = ac;
      pending.current = 0;
      setErr(null);
      if (run) setBusy(true);
      postAnnotate({ sid, window_min: win, from, to, order, run }, ac.signal)
        .then((r) => {
          if (ac.signal.aborted) return;
          setInfo(r.info);
          setCounts({ calls: r.calls, total: r.total });
          setBy((b) => ({ ...b, ...r.known }));
          // this request's job, or the one already asking about these calls,
          // and any other asking about some of them: their answers reach
          // this reader too
          if (r.job || r.also?.length) {
            setBusy(true);
            if (r.job) poll(r.job, ac, true);
            for (const id of r.also ?? []) poll(id, ac, !r.job);
          } else setBusy(false);
        })
        .catch((e) => {
          if (ac.signal.aborted) return;
          setErr(String(e?.message ?? e));
          setBusy(false);
        });
    },
    [sid, win, from, to, order, poll],
  );

  // a new session or read starts over; what is known is fetched at once, and
  // asked for when the site annotates on opening
  useEffect(() => {
    setBy({});
    setJob(null);
    setErr(null);
    setBusy(false);
    setCounts({ calls: 0, total: 0 });
    if (!ready || !info?.enabled) return;
    ask(!!info.auto && info.available);
    return () => live.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, sid, info?.enabled]);

  const model = job?.label ?? (info ? `${info.provider}:${info.model ?? 'default'}` : '');
  const set = useMemo<LlmPurposeSet | null>(
    () => (Object.keys(by).length ? { by, model } : null),
    [by, model],
  );
  return { info, set, calls: counts.calls, total: counts.total, job, err, busy, run: () => ask(true) };
}

/** The button and what it has done. Nothing when the site has it off. */
export function AnnotateControl({ a }: { a: AnnotateState }) {
  const { info, set, job, err, busy } = a;
  if (!info?.enabled) return null;
  const known = set ? Object.keys(set.by).length : 0;
  const left = Math.max(0, a.calls - known);
  const where = info.endpoint ? ` at ${info.endpoint}` : '';
  const tip =
    `ask ${info.provider}${info.model ? ` (${info.model})` : ''}${where} what each tool call was for. ` +
    `It is sent each call's command line as this page shows it -- masked -- and the folder it ran in, nothing else. ` +
    `Answers are kept, so a call is asked about once. A purpose is a model's reading, marked ✦, never a measurement` +
    (a.total > a.calls ? `\nthe first ${fint(a.calls)} of ${fint(a.total)} distinct calls are asked about (annotate.max_calls)` : '');
  let status: string | null = null;
  if (busy && job) status = `asking ${job.label}: ${fint(job.done)} of ${fint(job.total)}`;
  else if (busy) status = 'reading the session…';
  else if (a.calls) status = `${fint(known)} of ${fint(a.calls)} calls have a purpose`;
  if (!busy && job?.state === 'done' && job.unlabelled) status += ` · ${fint(job.unlabelled)} left unlabelled by the model`;
  return (
    <span className="pd-annotate">
      <button
        className="btn"
        onClick={a.run}
        disabled={busy || !info.available || (a.calls > 0 && left === 0)}
        data-tip={info.available ? tip : `cannot annotate here: ${info.error ?? 'the annotator is not available'}`}
      >
        ✦ annotate purposes
      </button>
      {status && <span className="nm">{status}</span>}
      {err && <span className="nm bad">{err}</span>}
    </span>
  );
}
