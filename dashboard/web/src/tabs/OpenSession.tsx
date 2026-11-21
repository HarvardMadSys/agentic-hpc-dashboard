/* The session open in the inspector (TrajectoryInspector.tsx), on whichever tab opened it: the Agents tab, beside
 * its list of every session in the window (TrajectoriesTab.tsx), or the Users tab, under a portrait's own sessions.
 * Which session is open is the URL's (`?traj=`), so an inspection is a link like every other view.
 *
 * An agent that ran past midnight is a row to a day, and the inspector of either lists the whole session's tasks. A
 * task or phase picked there that the other day's row holds opens that row (`jumpTo`), with where to go in it
 * (`pending`), which that row's new inspector goes to once it has read itself. It opens it whenever the window holds
 * it, by that row's own last record against the window's start, whether or not the list ranks it. Where the window
 * does not hold that row, it opens nothing, and the pick is made on the open row.
 */
import { useEffect, useState } from 'react';
import type { PhaseDay, TrajOrder, TrajRow } from '../api/types';
import { serverNow } from '../lib/clock';
import { dayInWindow, windowStart } from '../lib/tasks';
import { TrajectoryInspector } from './TrajectoryInspector';
import type { DayJump } from './TrajectoryInspector';

/** How the URL opens a session, closes it, and pages its read (App.tsx), handed to a tab that opens one in place. */
export interface SessionNav {
  /** the sid open, or null */
  sel: string | null;
  select: (sid: string | null) => void;
  tfrom: number | null;
  setTfrom: (t: number | null) => void;
  torder: TrajOrder;
  setTorder: (o: TrajOrder) => void;
  routine: string[] | null;
}

export function OpenSession({
  sel,
  row,
  win,
  windowMin,
  select,
  tfrom,
  setTfrom,
  torder = 'oldest',
  setTorder = () => {},
  routine = null,
  scrollOnOpen = true,
}: {
  /** the sid open in the inspector */
  sel: string;
  /** its ranked row, when the ranking holds it: the inspector reads its own otherwise */
  row: TrajRow | null;
  /** the window as the URL has it: the inspector reads the same one */
  win: number | null;
  /** the window's span as the service resolved it, which says whether it holds another day's row */
  windowMin: number | null | undefined;
  /** open another session, or with null close this one */
  select: (sid: string | null) => void;
  tfrom: number | null;
  setTfrom: (t: number | null) => void;
  /** which end of a session the inspector lists first and draws in full */
  torder?: TrajOrder;
  setTorder?: (o: TrajOrder) => void;
  /** the site's routine commands, which the inspector can hide */
  routine?: string[] | null;
  /** it scrolls itself into view as it opens, unless the page is about to go to it (App.tsx `land`) */
  scrollOnOpen?: boolean;
}) {
  // A task or phase picked in the inspector that the other day's row of the same session holds: that row is opened, and it is handed
  // where to go in it (`pendingJump`), which it does once it has read itself. The inspector is keyed on its session, so the other row
  // is a new inspector, and this is what outlives the old. It is for its own row and lasts only while that row is the open one: any
  // other way to a row drops it, so going back to the first and forward again does not make the jump twice.
  const [pending, setPending] = useState<DayJump | null>(null);
  useEffect(() => {
    if (pending && pending.sid !== sel) setPending(null);
  }, [pending, sel]);
  // A day row is in the window when its last record is at or after the window's start (`dayInWindow`), whether or not the list ranks
  // it among the rows it lists: on a busy day most of a session's day rows are not listed, and each opens all the same. One the window
  // does not hold opens as the panel that says it is not in this window. So there is no jump to it, which the inspector is told, and it
  // makes the pick on the row it has, as a pick of that row's own day is made.
  const jumpTo = (day: PhaseDay, t0: number, t1: number, what?: string): boolean => {
    if (!dayInWindow(day, windowStart(windowMin, serverNow()))) return false;
    const { sid } = day;
    setPending({ sid, t0, t1, what });
    select(sid);
    return true;
  };
  return (
    <TrajectoryInspector
      key={sel}
      sid={sel}
      row={row}
      win={win}
      from={tfrom}
      setFrom={setTfrom}
      order={torder}
      setOrder={setTorder}
      onClose={() => select(null)}
      onJump={jumpTo}
      pendingJump={pending?.sid === sel ? pending : null}
      routine={routine}
      scrollOnOpen={scrollOnOpen}
    />
  );
}
