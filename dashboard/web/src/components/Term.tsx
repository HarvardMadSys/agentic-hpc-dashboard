/* A term a reader may not know, with a small "?" that explains it on hover.
 *
 * The line comes from `lib/glossary.ts`, so a word reads the same everywhere;
 * `tip` overrides it where a place needs a sharper line. Without either, the
 * children render bare: a "?" that explains nothing is noise.
 */
import type { ReactNode } from 'react';
import { GLOSS } from '../lib/glossary';

export function Term({ k, tip, children }: { k?: string; tip?: string; children: ReactNode }) {
  const t = tip ?? (k ? GLOSS[k] : undefined);
  if (!t) return <>{children}</>;
  return (
    <span className="term" data-tip={t}>
      {children}
      <sup className="qm" aria-hidden="true">?</sup>
    </span>
  );
}
