/* "Copy as CSV": a panel's tables, as drawn, onto the clipboard.
 *
 * The service's export was removed for what it cost (it re-read every
 * day-file it planned, and streamed up to millions of rows). What a reader
 * wants most often is the table already on screen, in a spreadsheet: that
 * costs the service nothing, so the page does it from its own DOM. The cells
 * are copied as they read -- formatted, units and all -- because that is the
 * table the reader saw; the raw numbers are in the history store's Parquet.
 */
import { useState } from 'react';

const cell = (s: string) => {
  const t = s.replace(/\s+/g, ' ').trim();
  return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
};

/** Every table under `root` as CSV, a blank line between tables. */
export function tablesCsv(root: HTMLElement | null): string {
  if (!root) return '';
  const out: string[] = [];
  root.querySelectorAll('table').forEach((t) => {
    const lines: string[] = [];
    t.querySelectorAll('tr').forEach((tr) => {
      const cells = Array.from(tr.querySelectorAll('th, td')) as HTMLElement[];
      if (!cells.length) return;
      lines.push(cells.map((c) => cell(c.innerText ?? c.textContent ?? '')).join(','));
    });
    if (lines.length) out.push(lines.join('\n'));
  });
  return out.join('\n\n');
}

/** The clipboard where the browser allows it (a secure context: https, or
 *  the 127.0.0.1 an ssh tunnel gives), else the old copy command. */
async function copy(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the old way */
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  ta.remove();
  return ok;
}

export function CopyCsvButton({ root }: { root: React.RefObject<HTMLElement | null> }) {
  const [said, setSaid] = useState<string | null>(null);
  const go = async () => {
    const csv = tablesCsv(root.current);
    const ok = csv ? await copy(csv) : false;
    setSaid(!csv ? 'no table to copy' : ok ? 'copied' : 'the browser would not copy');
    window.setTimeout(() => setSaid(null), 1800);
  };
  return (
    <button
      className="btn csvbtn about-skip"
      onClick={go}
      data-tip="copy this panel's table as CSV, as it is drawn, for a spreadsheet"
      aria-live="polite"
    >
      {said ?? 'copy CSV'}
    </button>
  );
}
