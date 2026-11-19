/* Tables that a string from outside the page indexes: the URL, the service, the collector.
 *
 * A plain object answers for keys it never held: `t['__proto__']` is Object.prototype and
 * `t['constructor']` a function. Rendered, such a value throws ("Objects are not valid as a
 * React child") and the page goes white: `?atype=__proto__` did that through the filter's
 * label. A table made by `table()` has no prototype, so any key it does not hold reads
 * undefined, and the caller's `?? fallback` holds; `own()` reads any other object the same way.
 */

/** `entries` as a table with no prototype: `t[k]` is undefined for every key it does not hold. */
export function table<K extends string, V>(entries: Record<K, V>): Record<K, V> {
  return Object.assign(Object.create(null) as Record<K, V>, entries);
}

/** `t[key]` when `t` holds `key` itself, never a property it inherits; undefined otherwise. */
export function own<V>(t: Readonly<Record<string, V>> | null | undefined, key: string | null | undefined): V | undefined {
  return t != null && key != null && Object.hasOwn(t, key) ? t[key] : undefined;
}
