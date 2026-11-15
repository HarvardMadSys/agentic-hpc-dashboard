/* `virtual:build-info`, written by the build-info plugin in vite.config.ts on
 * every build, and on every page load under the dev server. */
declare module 'virtual:build-info' {
  const info: {
    /** Full SHA of HEAD; null when not built from a git checkout, or without git. */
    commit: string | null;
    /** Whether anything under dashboard/web/ differed from `commit`; null when unknown. */
    dirty: boolean | null;
    /** When the bundle was built, ISO-8601 UTC; null under the dev server, which bundles nothing. */
    builtAt: string | null;
  };
  export default info;
}
