/* Ask LLM: ask an agent about the window on screen.
 *
 * "Suggestions for the operator" asks it one fixed question about the whole
 * window on screen (its filter included): what should this cluster's operator
 * do? The answer comes back as JSON, which the service checks and the page
 * lays out as cards, the most urgent first, each with the numbers behind it
 * and a link to whom it concerns. It runs only when the button is pressed.
 * A suggestion marked done is kept by the service, for everyone, until undone:
 * it leaves the cards, waits in a "marked done" list, and the next pass is told
 * to leave it out.
 *
 * Each question runs the agent the service is configured with (`ask` in its
 * config; by default the service user's own `codex`) over a compact snapshot
 * of this window's panels, and the page polls for the answer. The last few
 * turns ride along, so a follow-up can say "and for Codex?". The conversation
 * is kept per browser tab (sessionStorage), so switching tabs does not lose it.
 * A question about one panel, or one session, is asked from that panel
 * instead (components/Ask.tsx).
 */
import { useEffect, useRef, useState } from 'react';
import { ApiError, getAskInfo, getDone, markDone, undoDone } from '../api/client';
import type { AskInfo, AskJob, DoneSuggestion, OperatorSuggestion } from '../api/types';
import { fullClock } from '../charts/scale';
import { Md, Turn, canAsk, useAskScope, useAskThread } from '../components/Ask';
import { Panel } from '../components/Panel';
import { clock } from '../lib/format';
import './chat.css';

const SUGGEST_Q = 'What should the cluster operator do about what ran in this window?';
const SEV: Record<OperatorSuggestion['priority'], string> = { high: 'HIGH', medium: 'med', low: 'low' };

/** The operator's suggestions, as cards: what to do, the numbers behind it,
 * and a link to each account it names. */
function SuggestionList({ job, openUserTraj, goto, followUp, isDone, onDone }: {
  job: AskJob;
  openUserTraj: (u: string) => void;
  goto: (tab: string) => void;
  followUp: (x: OperatorSuggestion) => void;
  isDone: (index: number) => boolean;
  onDone: (index: number) => void;
}) {
  const got = job.suggestions;
  if (!got) {
    // not the JSON it was asked for: the answer as written, never nothing
    return <Md text={job.answer ?? ''} />;
  }
  // each keeps its place in the answer: a mark names that place
  const open = got.suggestions.map((x, i) => [x, i] as const).filter(([, i]) => !isDone(i));
  return (
    <>
      {got.summary && <p className="sugg-sum">{got.summary}</p>}
      {got.suggestions.length === 0 ? (
        <p className="nm">nothing in this window needs the operator, by the model’s reading of it</p>
      ) : open.length === 0 ? (
        <p className="nm">every suggestion in this answer is marked done</p>
      ) : (
        <ol className="sugg-list">
          {open.map(([x, i]) => (
            <li key={i} className={`sugg ${x.priority}`}>
              <div className="sugg-h">
                <span className={`sev ${SEV[x.priority]}`}>{x.priority}</span>
                <b>{x.title}</b>
              </div>
              {x.action && <div className="sugg-a">{x.action}</div>}
              {x.evidence && (
                <div className="sugg-e">
                  <span className="nm">the numbers · </span>
                  {x.evidence}
                </div>
              )}
              <div className="sugg-f">
                {x.users.map((u) => (
                  <button key={u} className="linkbtn mono" onClick={() => openUserTraj(u)}
                    data-tip="open this account's sessions on the Trajectories tab">
                    {u} →
                  </button>
                ))}
                {x.panel && x.tab ? (
                  <button className="linkbtn" onClick={() => goto(x.tab!)} data-tip={`open the ${x.panel} tab`}>
                    see {x.panel} →
                  </button>
                ) : x.panel ? (
                  <span className="nm">shown on: {x.panel}</span>
                ) : null}
                <button className="linkbtn" onClick={() => followUp(x)}
                  data-tip="put a question about this suggestion in the chat below">
                  ask about this ↓
                </button>
                <button className="btn sugg-done-btn" onClick={() => onDone(i)}
                  data-tip="it leaves the list for everyone who opens this dashboard, and the next suggestions leave it out, until someone undoes it">
                  ✓ mark as done
                </button>
              </div>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

/** "Suggest actions": one press, one run over the window on screen. */
function Suggestions({ info, openUserTraj, goto, followUp }: {
  info: AskInfo | null;
  openUserTraj: (u: string) => void;
  goto: (tab: string) => void;
  followUp: (x: OperatorSuggestion) => void;
}) {
  const scope = useAskScope();
  const { turns, setTurns, send, err, busy } = useAskThread('rc-dash-suggest');
  const job = turns[turns.length - 1] ?? null;
  // what anyone marked done, as the service keeps it
  const [done, setDone] = useState<DoneSuggestion[]>([]);
  const [doneErr, setDoneErr] = useState<string | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    getDone(ac.signal).then((d) => setDone(d.items)).catch(() => {});
    return () => ac.abort();
  }, [job?.id]);
  const change = (p: Promise<{ items: DoneSuggestion[] }>) => {
    setDoneErr(null);
    p.then((d) => setDone(d.items)).catch((e) =>
      setDoneErr(e instanceof ApiError ? e.message : String(e)));
  };
  const narrowed = [...(scope.who?.users ?? []), ...(scope.who?.atypes ?? [])];
  const run = async () => {
    setTurns([]); // one set of suggestions at a time
    await send(SUGGEST_Q, scope.windowMin, { suggest: true, classes: scope.cls }, scope.who);
  };
  return (
    <Panel
      title="Suggestions for the operator"
      span
      header={
        canAsk(info) ? (
          <button className="btn primary" onClick={() => void run()} disabled={busy}>
            {busy ? 'suggesting…' : job ? 'suggest again' : 'suggest actions'}
          </button>
        ) : undefined
      }
    >
      <div className="chat sugg-box">
        <div className="chat-who">
          asks <b>{info?.agent ?? '…'}</b> what this cluster’s operator should do, from the{' '}
          <b>{scope.windowLabel ?? 'current'}</b> window on screen
          {narrowed.length ? <> · narrowed to <b>{narrowed.join(', ')}</b></> : null} · each suggestion names
          the numbers behind it · it runs only when you press the button
          {done.length ? <> · <b>{done.length}</b> marked done are left out</> : null}
        </div>
        {job == null ? null : job.state === 'done' ? (
          <>
            <SuggestionList job={job} openUserTraj={openUserTraj} goto={goto} followUp={followUp}
              isDone={(i) => done.some((d) => d.job === job.id && d.index === i)}
              onDone={(i) => change(markDone(job.id, i))} />
            <div className="chat-meta">
              {job.agent} · {job.elapsed_s} s · {job.snapshot_kb} KB of the window’s panels · the {job.window} window
              {job.submitted ? ` · asked at ${clock(job.submitted)}` : ''}
            </div>
          </>
        ) : job.state === 'error' ? (
          <span className="chat-err">{job.error}</span>
        ) : (
          <span className="chat-wait">
            <i className="ld" />
            {job.state === 'queued' ? 'waiting for the agent to be free' : job.progress ?? 'thinking'} ·{' '}
            {Math.round(job.elapsed_s)} s
          </span>
        )}
        {err && <div className="chat-err">{err}</div>}
        {doneErr && <div className="chat-err">{doneErr}</div>}
        {done.length > 0 && (
          <details className="sugg-done">
            <summary>marked done · {done.length}</summary>
            <ul>
              {done.map((d) => (
                <li key={d.id}>
                  <span className={`sev ${SEV[d.priority] ?? 'low'}`}>{d.priority}</span>
                  <span className="sugg-done-t">{d.title}</span>
                  {d.users.length > 0 && <span className="mono nm">{d.users.join(', ')}</span>}
                  <span className="nm">
                    done {fullClock(d.done_at)}
                    {d.window ? ` · from the ${d.window} window` : ''}
                  </span>
                  <button className="linkbtn" onClick={() => change(undoDone(d.id))}
                    data-tip="bring it back: it may be suggested again">
                    undo
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </Panel>
  );
}

const STARTERS = [
  'Which agents ran the most commands, and for whom?',
  'Did any agent run anything risky?',
  'Which agents wrote outside their own home directory?',
  'Summarise what Claude Code did in this window.',
];

export function ChatTab({ windowMin, windowLabel, openUserTraj = () => {}, goto = () => {} }: {
  windowMin: number | null;
  windowLabel: string | null;
  /** open an account's sessions on the Trajectories tab */
  openUserTraj?: (u: string) => void;
  /** open a tab */
  goto?: (tab: string) => void;
}) {
  const [info, setInfo] = useState<AskInfo | null>(null);
  const [infoErr, setInfoErr] = useState<string | null>(null);
  // the thread kept under the key it always had, so an open chat survives
  const { turns, setTurns, send, err: sendErr, busy } = useAskThread('rc-dash-chat');
  const [draft, setDraft] = useState('');
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // a suggestion's "ask about this": a question in the box, ready to send
  const followUp = (x: OperatorSuggestion) => {
    setDraft(`Tell me more about this suggestion: “${x.title}”${x.evidence ? ` (${x.evidence})` : ''}. `);
    inputRef.current?.focus();
    inputRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };

  useEffect(() => {
    const ac = new AbortController();
    getAskInfo(ac.signal).then(setInfo).catch((e) => !ac.signal.aborted && setInfoErr(String(e)));
    return () => ac.abort();
  }, []);
  // braces, not a bare expression: Chrome's scrollIntoView now returns a
  // Promise, and an effect that returns one breaks React's clean-up
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'nearest' });
  }, [turns.length]);

  const ask = async (q: string) => {
    if (await send(q, windowMin)) setDraft('');
  };

  const off = info && (!info.enabled || !info.available);
  return (
    <div className="grid wide">
      {!infoErr && !off && <Suggestions info={info} openUserTraj={openUserTraj} goto={goto} followUp={followUp} />}
      <Panel title="Ask about the whole window" span>
        {infoErr ? (
          <div className="none">could not reach the chat service: {infoErr}</div>
        ) : off ? (
          <div className="none">
            {!info!.enabled ? 'chat is turned off on this dashboard' : `the agent cannot run here: ${info!.error}`}
          </div>
        ) : (
          <div className="chat">
            <div className="chat-who">
              answering with <b>{info?.agent ?? '…'}</b>
              {info?.model ? <> · {info.model}</> : null} · about the <b>{windowLabel ?? 'current'}</b> window ·
              for one panel or one session, use the <b>ask</b> button on it: it sends that panel’s data only
            </div>
            {turns.length === 0 && (
              <div className="chat-starters">
                {STARTERS.map((s) => (
                  <button key={s} className="u-pick" onClick={() => void ask(s)} disabled={!info}>
                    {s}
                  </button>
                ))}
              </div>
            )}
            {turns.map((t) => (
              <Turn key={t.id} t={t} scope="the window’s panels" />
            ))}
            <div ref={endRef} />
            <form
              className="chat-input"
              onSubmit={(e) => {
                e.preventDefault();
                void ask(draft);
              }}
            >
              <textarea
                ref={inputRef}
                rows={2}
                value={draft}
                placeholder="Ask about this window…"
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    void ask(draft);
                  }
                }}
              />
              <div className="chat-actions">
                <button className="btn primary" type="submit" disabled={!draft.trim() || !info}>
                  {busy ? 'ask next' : 'ask'}
                </button>
                {turns.length > 0 && (
                  <button className="btn" type="button" onClick={() => setTurns([])} disabled={busy}>
                    new chat
                  </button>
                )}
              </div>
            </form>
            {sendErr && <div className="chat-err">{sendErr}</div>}
          </div>
        )}
      </Panel>
    </div>
  );
}
