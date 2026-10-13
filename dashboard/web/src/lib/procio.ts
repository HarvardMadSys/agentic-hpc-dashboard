/* A process's I/O in the trajectory tree (rc_dashboard/proctree.py `io` and
 * `sub_io`), in words: one short line for a box or a cell, every figure in the
 * hover. "I/O" is what passed through read() and write() -- pipes and
 * terminals included -- "disk" what reached the disk, "net" the network. */
import type { ProcIo } from '../api/types';
import { mb } from './format';

/** `I/O 45 MB · disk 3 MB · net 1 MB`, the zero parts left out; null when
 *  nothing moved. */
export function ioBrief(io: ProcIo | null | undefined): string | null {
  if (!io) return null;
  const parts = [
    ['I/O', io.rchar_mb + io.wchar_mb],
    ['disk', io.rd_mb + io.wr_mb],
    ['net', io.tx_mb + io.rx_mb],
  ] as const;
  const shown = parts.filter(([, v]) => v > 0).map(([k, v]) => `${k} ${mb(v)}`);
  return shown.length ? shown.join(' · ') : null;
}

/** Every figure, one per line, for a hover. */
export function ioTip(io: ProcIo | null | undefined, whose: string): string | null {
  if (!io) return null;
  return [
    `${whose}:`,
    `  read ${mb(io.rchar_mb)} through read(), ${mb(io.rd_mb)} of it from the disk`,
    `  wrote ${mb(io.wchar_mb)} through write(), ${mb(io.wr_mb)} of it to the disk`,
    `  sent ${mb(io.tx_mb)}, received ${mb(io.rx_mb)} over the network`,
  ].join('\n');
}

/** Syscall bytes, read and written: the one number a table cell has room for. */
export const ioTotal = (io: ProcIo | null | undefined): number | null =>
  io ? io.rchar_mb + io.wchar_mb : null;
