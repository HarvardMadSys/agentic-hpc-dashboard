/* Chat with LLM: ask an agent about the window on screen.
 *
 * Each question runs the agent the service is configured with (`ask` in its
 * config; by default the service user's own `codex`) over a compact snapshot
 * of this window's panels, and the page polls for the answer. The last few
 * turns ride along, so a follow-up can say "and for Codex?". The conversation
 * is kept per browser tab (sessionStorage), so switching tabs does not lose it.
 */
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ApiError, getAskInfo, getAskJob, postAsk } from '../api/client';
import type { AskInfo, AskJob } from '../api/types';
import { Panel } from '../components/Panel';
import './chat.css';

const KEY = 'rc-dash-chat';
const STARTERS = [
  'Which agents ran the most commands, and for whom?',
  'Did any agent run anything risky?',
  'Which agents wrote outside their own home directory?',
  'Summarise what Claude Code did in this window.',
];

function load(): AskJob[] {
  try {
    return JSON.parse(sessionStorage.getItem(KEY) || '[]') as AskJob[];
  } catch {
    return [];
  }
}
function save(turns: AskJob[]) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(turns.slice(-30)));
  } catch {
    /* storage refused (private window, quota): the chat still works, unsaved */
  }
}

/* A small, safe markdown subset -- paragraphs, bullet and numbered lists,
 * **bold**, and `code` -- built as elements, never as HTML. */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let at = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > at) out.push(text.slice(at, m.index));
    out.push(m[1] != null ? <b key={m.index}>{m[1]}</b> : <code key={m.index}>{m[2]}</code>);
    at = m.index + m[0].length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

function Md({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) blocks.push(<p key={blocks.length}>{inline(para.join(' '))}</p>);
    para = [];
  };
  const flushList = () => {
    if (list) {
      const items = list.items.map((t, i) => <li key={i}>{inline(t)}</li>);
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

export function ChatTab({ windowMin, windowLabel }: { windowMin: number | null; windowLabel: string | null }) {
  const [info, setInfo] = useState<AskInfo | null>(null);
  const [infoErr, setInfoErr] = useState<string | null>(null);
  const [turns, setTurns] = useState<AskJob[]>(load);
  const [draft, setDraft] = useState('');
  const [sendErr, setSendErr] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const ac = new AbortController();
    getAskInfo(ac.signal).then(setInfo).catch((e) => !ac.signal.aborted && setInfoErr(String(e)));
    return () => ac.abort();
  }, []);
  useEffect(() => {
    save(turns);
  }, [turns]);

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
  // braces, not a bare expression: Chrome's scrollIntoView now returns a
  // Promise, and an effect that returns one breaks React's clean-up
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'nearest' });
  }, [turns.length]);

  const busy = pending.length > 0;
  const send = async (q: string) => {
    const question = q.trim();
    if (!question) return;
    setSendErr(null);
    const history = turns
      .filter((t) => t.state === 'done' && t.answer)
      .slice(-3)
      .map((t) => ({ q: t.question, a: t.answer! }));
    try {
      const j = await postAsk(question, windowMin, history);
      setTurns((ts) => [...ts, j]);
      setDraft('');
    } catch (e) {
      setSendErr(e instanceof Error ? e.message : String(e));
    }
  };

  const off = info && (!info.enabled || !info.available);
  return (
    <div className="grid wide">
      <Panel title="Chat with LLM" span>
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
              {info?.model ? <> · {info.model}</> : null} · about the <b>{windowLabel ?? 'current'}</b> window
            </div>
            {turns.length === 0 && (
              <div className="chat-starters">
                {STARTERS.map((s) => (
                  <button key={s} className="u-pick" onClick={() => send(s)} disabled={!info}>
                    {s}
                  </button>
                ))}
              </div>
            )}
            {turns.map((t) => (
              <div key={t.id} className="turn">
                <div className="q">{t.question}</div>
                <div className="a">
                  {t.state === 'done' ? (
                    <Md text={t.answer ?? ''} />
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
                    {t.agent} · {t.elapsed_s} s · the {t.window} window
                  </div>
                )}
              </div>
            ))}
            <div ref={endRef} />
            <form
              className="chat-input"
              onSubmit={(e) => {
                e.preventDefault();
                void send(draft);
              }}
            >
              <textarea
                rows={2}
                value={draft}
                placeholder="Ask about this window…"
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    void send(draft);
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
