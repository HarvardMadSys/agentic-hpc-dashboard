import type { AuthInfo, AuthMethod } from '../api/types';

/** Whether the service asks for a password (rc_dashboard/auth.py), read from
 *  /api/config: the hash shows as "(hidden)" when it is set and "" when not, and the
 *  hash file -- the way `rc-dashboard init` and the deploy kits set it -- by its path.
 *  Either one is a password, so the rail offers to sign out. */
export function asksPassword(config: Record<string, unknown> | null | undefined): boolean {
  const auth = (config?.server as Record<string, unknown> | undefined)?.auth as
    | Record<string, { _value?: unknown } | undefined>
    | undefined;
  return Boolean(auth?.password_hash?._value || auth?.password_hash_file?._value);
}

/** What the page shows of how its reader got in (`_meta.auth`, api/types.ts `AuthInfo`):
 *  `user`, the name the head shows when the site's single sign-on signed them in; and
 *  Sign out -- the service's own form after the password, a link to the proxy's
 *  sign-out where the site named one (`server.auth.proxy_logout_url`), and none
 *  otherwise: an open page has no one to sign out, and the dashboard alone cannot end
 *  a session the proxy keeps. */
export interface SignIn {
  method: AuthMethod;
  user: string | null;
  signOut: { kind: 'form' } | { kind: 'link'; href: string } | null;
}

export function signIn(config: Record<string, unknown> | null | undefined): SignIn {
  const meta = config?._meta as Record<string, { _value?: unknown } | undefined> | undefined;
  const a = meta?.auth?._value as Partial<AuthInfo> | null | undefined;
  if (a?.method === 'proxy') {
    const href = typeof a.logout_url === 'string' && linkable(a.logout_url) ? a.logout_url : null;
    return {
      method: 'proxy',
      user: typeof a.user === 'string' && a.user ? a.user : null,
      signOut: href ? { kind: 'link', href } : null,
    };
  }
  if (a?.method === 'password' || a?.method === 'open') {
    return { method: a.method, user: null, signOut: a.method === 'password' ? { kind: 'form' } : null };
  }
  // A service from before `_meta.auth` (or no config yet): a password set is the form's.
  const pw = asksPassword(config);
  return { method: pw ? 'password' : 'open', user: null, signOut: pw ? { kind: 'form' } : null };
}

/** A path on this site, or an http(s) URL: what the service takes for the setting
 *  (auth.py `_logout_ok`), and the only link the page draws from it. */
function linkable(u: string): boolean {
  if (/[\s\\]/.test(u)) return false;
  if (u.startsWith('/')) return !u.startsWith('//');
  return /^https?:\/\/[^/]/i.test(u);
}
