/* A part of the page that throws while it is drawn says so, and the rest stays.
 *
 * React unmounts everything under the nearest boundary when a component throws, and with none
 * above App a single bad value blanked the whole page. There is one around the page (main.tsx),
 * one around each tab (`TabBoundary`, lib/lazyTab.tsx), and one around each panel's body
 * (Panel.tsx): what failed reads "… could not be drawn: <why> · reload", in the frame of the
 * missing family. A chunk that would not load (a deploy replaced it under an open page) is one
 * such failure: reload fetches the new one.
 *
 * `resetOn` tries again when it changes: a panel handed new data draws afresh.
 */
import { Component } from 'react';
import type { ReactNode } from 'react';

/** What a thrown value says, never itself a throw. */
export function errorText(e: unknown): string {
  try {
    if (e instanceof Error) return e.message || e.name;
    return String(e);
  } catch {
    return 'an error that cannot be shown';
  }
}

/** "this tab could not be drawn: <why> · reload" */
export function DrawFailed({ what, error }: { what: string; error: unknown }) {
  return (
    <div className="notreturned notreduced" role="alert">
      <span className="pill">not drawn</span>
      <span>
        {what} could not be drawn: {errorText(error)}
        {' · '}
        <button type="button" className="linkbtn" onClick={() => window.location.reload()}>
          reload
        </button>
      </span>
    </div>
  );
}

interface State {
  failed: boolean;
  error: unknown;
  /** `resetOn` as it was when it failed */
  at: unknown;
}

export class Boundary extends Component<{ what: string; resetOn?: unknown; children?: ReactNode }, State> {
  state: State = { failed: false, error: null, at: undefined };

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { failed: true, error };
  }

  static getDerivedStateFromProps(props: { resetOn?: unknown }, state: State): Partial<State> | null {
    // new data since it failed: draw it afresh
    if (state.failed && !Object.is(props.resetOn, state.at)) return { failed: false, error: null, at: props.resetOn };
    if (!state.failed && !Object.is(props.resetOn, state.at)) return { at: props.resetOn };
    return null;
  }

  render() {
    return this.state.failed ? <DrawFailed what={this.props.what} error={this.state.error} /> : this.props.children;
  }
}
