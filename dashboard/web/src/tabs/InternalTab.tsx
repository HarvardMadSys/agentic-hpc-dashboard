/* Internal -- what we are not sure about yet (roadmap notes 37 and 45).
 *
 * Panels whose takeaway is still open sit here, apart from the tabs an
 * operator reads, so they can be discussed without crowding those tabs. A
 * panel leaves when we know what it says, or when we decide it says nothing.
 * The sandbox panels left for the Security tab (tabs/SecurityTab.tsx), as its
 * confinement half.
 *
 * Also here (user, 2026-11-17): the resource and I/O panels an operator does
 * not act on -- per-class quantiles, kernel delay accounting, the page-cache
 * series -- each a researcher's table or a repeat of a Live chart. They keep
 * their titles, so a link to one still lands on it (lib/cites.ts).
 */
import { memo } from 'react';
import type { Cls } from '../api/types';
import type { Panels } from '../lib/panels';
import { ResourcesTab } from './ResourcesTab';

export const InternalTab = memo(function InternalTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  return (
    <>
      <div className="rowlabel">resources and I/O</div>
      <ResourcesTab P={P} cls={cls} part="internal" />
    </>
  );
});
