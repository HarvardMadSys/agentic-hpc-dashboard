/* Asking an LLM, shared by the Ask LLM tab and the panels (roadmap notes 40
 * and 51).
 *
 * A panel with an `ask` prop gets an "ask" button; it opens a small thread in
 * the panel, whose questions the service answers from THAT panel's data only
 * -- or, for the session inspector, from that one session's process tree. The
 * service builds the data itself from what the panel names (aggregator.ask),
 * over the window and the user / agent-type filter on screen; the page never
 * posts what it drew. The general chat (ChatTab.tsx) is the same thread with
 * no context: the whole window.
 *
 * A thread is kept per browser tab (sessionStorage), so leaving a tab and
 * coming back does not lose it.
 */
import { createContext, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ApiError, getAskJob, postAsk } from '../api/client';
import type { AskContext, AskInfo, AskJob, AskPart, Cls } from '../api/types';
import { refsOf } from '../lib/cites';
import type { Who } from '../lib/filters';
import { CiteMark, Sources, citeCtx } from './Cite';
import type { CiteCtx } from './Cite';
import '../tabs/chat.css';

/* ----------------------------------------------------------- the scope */

/** What every panel's question is asked over: the agent that answers, the
 * window, the filter, and the classes on screen. Provided by App. */
export interface AskScope {
  info: AskInfo | null;
  windowMin: number | null;
  windowLabel: string | null;
  who: Who | null;
  cls: Cls[];
  /** the tab's label, for the prompt: "the Resources tab" */
  tab: string;
}

export const AskScopeCtx = createContext<AskScope>({
  info: null,
  windowMin: null,
  windowLabel: null,
  who: null,
  cls: [],
  tab: '',
});

export const useAskScope = () => useContext(AskScopeCtx);

/** Whether a question can be asked here at all: turned on, and the agent found. */
export const canAsk = (info: AskInfo | null) => !!info && info.enabled && info.available;

/* ------------------------------------------------------------ markdown */

/* A small, safe markdown subset -- paragraphs, bullet and numbered lists,
 * **bold**, and `code` -- built as elements, never as HTML. With `cite`, a
 * bracket of refs (`[b7]`, `[b7, c2]`) is a chip per source (Cite.tsx);
 * without, an answer from before citations reads as it was written. */
function inline(text: string, cite?: CiteCtx | null): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\*\*([^*]+)\*\*|`([^`]+)`|\[([a-z]{1,2}\d{1,5}(?:\s*[,;]\s*[a-z]{1,2}\d{1,5})*)\]/g;
  let at = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m[3] != null && !cite) continue;
    if (m.index > at) out.push(text.slice(at, m.index));
    if (m[1] != null) out.push(<b key={m.index}>{inline(m[1], cite)}</b>);
    else if (m[2] != null) out.push(<code key={m.index}>{m[2]}</code>);
    else {
      out.push(
        <span key={m.index} className="cites">
          {refsOf(m[3]).map((r) => (
            <CiteMark key={r} r={r} ctx={cite!} />
          ))}
        </span>,
      );
    }
    at = m.index + m[0].length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

/** One line of an answer, its citations as chips: a suggestion's fields. */
export function CiteText({ text, cite }: { text: string; cite?: CiteCtx | null }) {
  return <>{inline(text, cite)}</>;
}

export function Md({ text, cite }: { text: string; cite?: CiteCtx | null }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) blocks.push(<p key={blocks.length}>{inline(para.join(' '), cite)}</p>);
    para = [];
  };
  const flushList = () => {
    if (list) {
      const items = list.items.map((t, i) => <li key={i}>{inline(t, cite)}</li>);
      blocks.push(list.ordered ? <ol key={blocks.length}>{items}</ol> : <ul key={blocks.length}>{items}</ul>);
    }
    list = null;
  };
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd();
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const num = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || num) {
      flushPara();
      const ordered = !!num;
      if (!list || list.ordered !== ordered) {
        flushList();
        list = { ordered, items: [] };
      }
      list.items.push((bullet ?? num)![1]);
    } else if (!line.trim()) {
      flushPara();
      flushList();
    } else {
      flushList();
      para.push(line.replace(/^#+\s*/, ''));
    }
  }
  flushPara();
  flushList();
  return <div className="md">{blocks}</div>;
}

/* ------------------------------------------------------------- threads */

function load(key: string): AskJob[] {
  try {
    return JSON.parse(sessionStorage.getItem(key) || '[]') as AskJob[];
  } catch {
    return [];
  }
}
function save(key: string, turns: AskJob[]) {
  try {
    sessionStorage.setItem(key, JSON.stringify(turns.slice(-30)));
  } catch {
    /* storage refused (private window, quota): the thread still works, unsaved */
  }
}

/** One conversation: its turns, kept under `key`, each polled until answered.
 * `context` narrows every question in it to one panel or one session. */
export function useAskThread(key: string) {
  const [turns, setTurns] = useState<AskJob[]>(() => load(key));
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    save(key, turns);
  }, [key, turns]);

  // Poll every question still out, once a second, until it is answered.
  const pending = turns.filter((t) => t.state === 'queued' || t.state === 'running').map((t) => t.id);
  const pendingKey = pending.join(',');
  useEffect(() => {
    if (!pending.length) return;
    const timer = setInterval(() => {
      for (const id of pending) {
        getAskJob(id)
          .then((j) => setTurns((ts) => ts.map((t) => (t.id === id ? j : t))))
          .catch((e) => {
            // the service restarted and forgot it: say so rather than spin
            if (e instanceof ApiError && e.status === 404) {
              setTurns((ts) =>
                ts.map((t) => (t.id === id ? { ...t, state: 'error', error: 'the service restarted before answering' } : t)),
              );
            }
          });
      }
    }, 1000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingKey]);

  const send = async (q: string, windowMin: number | null, context?: AskContext, who?: Who | null) => {
    const question = q.trim();
    if (!question) return false;
    setErr(null);
    const history = turns
      .filter((t) => t.state === 'done' && t.answer)
      .slice(-3)
      .map((t) => ({ q: t.question, a: t.answer! }));
    try {
      const j = await postAsk(question, windowMin, history, context, who);
      setTurns((ts) => [...ts, j]);
      return true;
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      return false;
    }
  };
  return { turns, setTurns, send, err, busy: pending.length > 0 };
}

/** One question and its answer, as the thread shows it: the answer's
 *  citations as chips, and its sources under it. */
export function Turn({ t, scope }: { t: AskJob; scope?: string }) {
  const cite = t.state === 'done' ? citeCtx(t, [t.answer]) : null;
  return (
    <div className="turn">
      <div className="q">{t.question}</div>
      <div className="a">
        {t.state === 'done' ? (
          <>
            <Md text={t.answer ?? ''} cite={cite} />
            {cite && <Sources ctx={cite} />}
          </>
        ) : t.state === 'error' ? (
          <span className="chat-err">{t.error}</span>
        ) : (
          <span className="chat-wait">
            <i className="ld" />
            {t.state === 'queued' ? 'waiting for the agent to be free' : t.progress ?? 'thinking'} ·{' '}
            {Math.round(t.elapsed_s)} s
          </span>
        )}
      </div>
      {t.state === 'done' && (
        <div className="chat-meta">
          {t.agent} · {t.elapsed_s} s · {t.snapshot_kb} KB of {scope ?? 'data'} · the {t.window} window
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------- a panel's ask */

/** What a panel's ask is about: some of the service's panel data, or one session. */
export type AskRef = AskPart[] | { session: string };

const PANEL_STARTERS = [
  'What stands out here?',
  'How do I read this panel?',
  'Which row is the outlier, and why?',
];
const SESSION_STARTERS = [
  'What was this session doing?',
  'Why did its commands fail?',
  'What cost the most CPU or time?',
];

/** The thread inside a panel: asked about that panel's data and nothing else. */
export function AskBox({ what, title, threadKey, onClose }: {
  what: AskRef;
  /** the panel's title as the reader sees it */
  title: string;
  threadKey: string;
  onClose: () => void;
}) {
  const scope = useAskScope();
  const session = !Array.isArray(what);
  const { turns, setTurns, send, err, busy } = useAskThread(`rc-dash-ask:${threadKey}`);
  const [draft, setDraft] = useState('');
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'nearest' });
  }, [turns.length]);

  const context: AskContext = session
    ? { session: (what as { session: string }).session, title }
    : { parts: what as AskPart[], title, tab: scope.tab, classes: scope.cls };
  const ask = async (q: string) => {
    if (await send(q, scope.windowMin, context, session ? null : scope.who)) setDraft('');
  };
  const scopeLbl = session ? 'this session’s process tree' : 'this panel’s data';
  return (
    <div className="askbox chat">
      <div className="askbox-h">
        <span className="chat-who">
          ask <b>{scope.info?.agent ?? '…'}</b>
          {scope.info?.model ? <> · {scope.info.model}</> : null} about {session ? 'this session' : 'this panel'}
          {' · '}
          <span data-tip={session
            ? 'the service sends the model a digest of this session: its totals, the commands it ran in order, its failures, and its process tree as an outline. Names are masked as on the page.'
            : 'the service sends the model this panel’s data, over the window and filter on screen, and nothing else from the dashboard. Names are masked as on the page.'}>
            it sees {scopeLbl} only
          </span>
        </span>
        <span className="askbox-ctl">
          {turns.length > 0 && (
            <button className="linkbtn" onClick={() => setTurns([])} disabled={busy}>
              clear
            </button>
          )}
          <button className="linkbtn" onClick={onClose}>
            close
          </button>
        </span>
      </div>
      {turns.length === 0 && (
        <div className="chat-starters">
          {(session ? SESSION_STARTERS : PANEL_STARTERS).map((s) => (
            <button key={s} className="u-pick" onClick={() => void ask(s)}>
              {s}
            </button>
          ))}
        </div>
      )}
      {turns.map((t) => (
        <Turn key={t.id} t={t} scope={scopeLbl} />
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
          rows={1}
          value={draft}
          placeholder={session ? 'Ask about this session…' : 'Ask about this panel…'}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void ask(draft);
            }
          }}
        />
        <button className="btn primary" type="submit" disabled={!draft.trim()}>
          {busy ? 'ask next' : 'ask'}
        </button>
      </form>
      {err && <div className="chat-err">{err}</div>}
    </div>
  );
}
