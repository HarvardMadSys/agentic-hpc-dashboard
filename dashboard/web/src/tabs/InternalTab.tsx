/* Internal -- what we are not sure about yet (roadmap notes 37 and 45).
 *
 * Panels whose takeaway is still open sit here, apart from the tabs an
 * operator reads, so they can be discussed without crowding those tabs. A
 * panel leaves when we know what it says, or when we decide it says nothing.
 */
import type { Cls } from '../api/types';
import type { Panels } from '../lib/panels';
import { SandboxTab } from './SandboxTab';

export function InternalTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  return (
    <>
      <p className="intro">
        Panels whose takeaway we have not settled yet. They are here for us to discuss, and may
        change, move to a main tab, or go.
      </p>
      <div className="rowlabel">sandbox · did agents' commands run isolated?</div>
      <SandboxTab P={P} cls={cls} />
    </>
  );
}
