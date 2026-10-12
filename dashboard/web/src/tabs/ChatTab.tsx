/* Ask LLM: ask an agent about the window on screen.
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
import { getAskInfo } from '../api/client';
import type { AskInfo } from '../api/types';
import { Turn, useAskThread } from '../components/Ask';
import { Panel } from '../components/Panel';
import './chat.css';

const STARTERS = [
  'Which agents ran the most commands, and for whom?',
  'Did any agent run anything risky?',
  'Which agents wrote outside their own home directory?',
  'Summarise what Claude Code did in this window.',
];

export function ChatTab({ windowMin, windowLabel }: { windowMin: number | null; windowLabel: string | null }) {
  const [info, setInfo] = useState<AskInfo | null>(null);
  const [infoErr, setInfoErr] = useState<string | null>(null);
  // the thread kept under the key it always had, so an open chat survives
  const { turns, setTurns, send, err: sendErr, busy } = useAskThread('rc-dash-chat');
  const [draft, setDraft] = useState('');
  const endRef = useRef<HTMLDivElement>(null);

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
