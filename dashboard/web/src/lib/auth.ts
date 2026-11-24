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
