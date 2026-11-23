import { AGENT_TYPE_LBL } from './classes';

/** A session's name in words, from its collector key. `apid:561705` is "the
 * agent process with pid 561705", `sess:282650` the login session (systemd
 * session-282650.scope), `tty:alice@pts/3` a terminal -- vocabulary from the
 * collector's internals that a reader should never have to know. */
export function sessionName(r: { sid: string; key_type?: string | null; agent_type?: string | null }): {
  date: string;
  key: string;
} {
  const parts = r.sid.split('|');
  if (parts.length < 4) return { date: '', key: r.sid };
  const date = (parts[1] ?? '').slice(5);
  const raw = parts.slice(3).join('|');
  const i = raw.indexOf(':');
  const kt = r.key_type ?? raw.slice(0, i);
  const kv = i >= 0 ? raw.slice(i + 1) : raw;
  const key =
    kt === 'apid'
      ? `${AGENT_TYPE_LBL[r.agent_type ?? ''] ?? 'agent'} · pid ${kv}`
      : kt === 'sess'
        ? `login session ${kv}`
        : kt === 'tty'
          ? `terminal ${kv.slice(kv.indexOf('@') + 1)}`
          : kt === 'usr'
            ? `everything ${kv} ran`
            : raw;
  return { date, key };
}

/** A session's title in a list: whose it is and what ran it -- `wen · VS Code`,
 *  `tim · login session` -- with the collector's key (a pid, a session number)
 *  left for the detail line. Titled by its key alone, two users' sessions
 *  read alike: two `VS Code · pid 680003` rows, one wen's and one xin's. */
export function sessionTitle(r: {
  sid: string;
  key_type?: string | null;
  agent_type?: string | null;
  user?: string | null;
  users?: string[];
}): { title: string; detail: string; date: string } {
  const nm = sessionName(r);
  const who = r.user ?? (r.users ?? [])[0] ?? null;
  const parts = r.sid.split('|');
  const raw = parts.length >= 4 ? parts.slice(3).join('|') : '';
  const kt = r.key_type ?? raw.slice(0, Math.max(0, raw.indexOf(':')));
  const kv = raw.indexOf(':') >= 0 ? raw.slice(raw.indexOf(':') + 1) : raw;
  const what =
    kt === 'apid'
      ? AGENT_TYPE_LBL[r.agent_type ?? ''] ?? 'agent'
      : kt === 'sess'
        ? 'login session'
        : kt === 'tty'
          ? 'terminal'
          : null;
  if (!who || !what) return { title: nm.key, detail: '', date: nm.date };
  const detail = kt === 'apid' ? `pid ${kv}` : kt === 'sess' ? `session ${kv}` : kv.slice(kv.indexOf('@') + 1);
  return { title: `${who} · ${what}`, detail, date: nm.date };
}
