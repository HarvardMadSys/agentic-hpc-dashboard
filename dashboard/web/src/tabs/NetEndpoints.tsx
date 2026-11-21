/* The Network bytes panel's endpoint list: the remote ends the processes
 * exchanged the most send and receive calls with, or the most bytes, each row
 * split by class.
 *
 * A row is one name: reverse DNS from the dashboard's host, else a name a
 * process's command line gave, else the address (rc_dashboard/endpoints.py).
 * The service sends the top ten for every mix of classes, so ranking the
 * classes the page shows here is exact. Counted as each process exited, like
 * the per-class table beside it, and narrowed by the page-wide who filter the
 * same way.
 */
import { useState } from 'react';
import type { EndpointCover, EndpointList, EndpointRow, EndpointUse, IoClassBlock } from '../api/types';
import { Segmented } from '../components/ClassFilter';
import { ShareCell } from '../components/ShareCell';
import { SortTh } from '../components/SortTh';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, mb, pc } from '../lib/format';
import { useSort } from '../lib/useSort';
import { useNodeWords } from '../lib/nodes';
import type { NodeWords } from '../lib/nodes';
import { shareOf, wholeOf } from '../lib/shares';

export type EndpointBy = 'calls' | 'bytes';
const BYS: { k: EndpointBy; lbl: string; tip: string }[] = [
  { k: 'calls', lbl: 'most network calls', tip: 'the endpoints the processes made the most send and receive calls to' },
  { k: 'bytes', lbl: 'most bytes', tip: 'the endpoints the processes sent and received the most bytes to and from' },
];

const sum = (r: EndpointRow, cls: readonly string[], f: (u: EndpointUse) => number) =>
  cls.reduce((a, c) => a + (r.by_class[c] ? f(r.by_class[c]) : 0), 0);
const callsOf = (r: EndpointRow, cls: readonly string[]) => sum(r, cls, (u) => u.calls);
const txOf = (r: EndpointRow, cls: readonly string[]) => sum(r, cls, (u) => u.tx);
const rxOf = (r: EndpointRow, cls: readonly string[]) => sum(r, cls, (u) => u.rx);
const bytesOf = (r: EndpointRow, cls: readonly string[]) => txOf(r, cls) + rxOf(r, cls);
const MB = 1e6;

/** The service's own ranking (endpoints.fold): the most of `by` over `cls`,
 *  then the other measure, then the name, outbound before inbound. An endpoint
 *  none of `cls` reached is not ranked. */
export function rankEndpoints(rows: EndpointRow[], cls: readonly string[], by: EndpointBy, top: number): EndpointRow[] {
  const val = (r: EndpointRow): [number, number] =>
    by === 'calls' ? [callsOf(r, cls), bytesOf(r, cls)] : [bytesOf(r, cls), callsOf(r, cls)];
  return rows
    .map((r) => ({ r, v: val(r) }))
    .filter((x) => x.v[0] > 0)
    .sort((a, b) =>
      b.v[0] - a.v[0] || b.v[1] - a.v[1]
      || (a.r.name < b.r.name ? -1 : a.r.name > b.r.name ? 1 : 0)
      || Number(a.r.inbound) - Number(b.r.inbound))
    .slice(0, top)
    .map((x) => x.r);
}

function whence(r: EndpointRow, rdns: boolean): string {
  if (r.pending) return 'name lookup in progress: listed by what is known so far';
  if (r.source === 'dns') return 'named by reverse DNS';
  if (r.source === 'argv') {
    return rdns
      ? 'named by a process’s command line: reverse DNS has no name for it'
      : 'named by a process’s command line';
  }
  return rdns ? 'no name: reverse DNS has none, and no command line named it' : 'no name: no command line named it';
}

function rowTip(r: EndpointRow, rdns: boolean, cls: readonly string[], nodes: NodeWords): string {
  const more = r.n_addrs - r.addrs.length;
  return [
    r.name,
    whence(r, rdns),
    `${r.n_addrs === 1 ? 'address' : `${fint(r.n_addrs)} addresses`}: ${r.addrs.join(', ')}${more > 0 ? `, and ${fint(more)} more` : ''}`,
    r.ports.length ? `${r.inbound ? `this ${nodes.one}’s port` : 'port'}: ${r.ports.join(', ')}` : '',
    r.inbound ? `inbound: a client that reached this ${nodes.one}` : '',
    r.external ? '' : 'a private or loopback address',
    r.provider ? `provider: ${r.provider}` : '',
    `${fint(callsOf(r, cls))} calls · sent ${mb(txOf(r, cls) / MB)} · received ${mb(rxOf(r, cls) / MB)}`,
    r.n_addrs === 1
      ? `listed by ${fint(r.procs)} process${r.procs === 1 ? '' : 'es'}, of every class`
      : `listed by up to ${fint(r.procs)} processes, of every class: one that reached several of its addresses counts once for each`,
  ].filter(Boolean).join('\n');
}

type Col = 'rank' | 'name' | 'calls' | 'tx' | 'rx' | string;

export function NetEndpoints({ ep, have, byClass }: {
  ep: EndpointList;
  /** the classes the page shows, in its order */
  have: string[];
  byClass: Record<string, IoClassBlock>;
}) {
  const nodes = useNodeWords();
  const [by, setBy] = useState<EndpointBy>('calls');
  // its own order is the rank; a header re-sorts the ten, a third click restores it
  const s = useSort<Col>('rank', 'asc');
  const cls = have.filter((c) => ep.classes.includes(c));
  const ranked = rankEndpoints(ep.rows, cls, by, ep.top);
  const rank = new Map(ranked.map((r, i) => [r, i + 1]));
  const of = (r: EndpointRow, c: string) =>
    by === 'calls' ? r.by_class[c]?.calls ?? 0 : (r.by_class[c]?.tx ?? 0) + (r.by_class[c]?.rx ?? 0);
  const rows = s.sort(ranked, (r, k) =>
    k === 'rank' ? rank.get(r) ?? null
    : k === 'name' ? r.name
    : k === 'calls' ? callsOf(r, cls)
    : k === 'tx' ? txOf(r, cls)
    : k === 'rx' ? rxOf(r, cls)
    : of(r, k));

  // What the endpoints cover of the bytes the per-class table counts, over the
  // classes the page shows -- a class with no endpoint at all included, for its losses.
  const cover = (f: (v: EndpointCover) => number) => have.reduce((a, c) => a + (ep.cover[c] ? f(ep.cover[c]) : 0), 0);
  const listed = cover((v) => v.tx + v.rx);
  const lost = cover((v) => v.lost_tx + v.lost_rx);
  const missing = cover((v) => v.missing);
  const panel = have.reduce((a, c) => a + ((byClass[c]?.net_tx_mb?.total ?? 0) + (byClass[c]?.net_rx_mb?.total ?? 0)) * MB, 0);

  return (
    <div className="net-ends">
      <div className="net-ends-ctl">
        <span className="eyebrow" data-tip="the remote ends of the network bytes: one row per name">
          top {ep.top} endpoints
        </span>
        <Segmented value={by} onChange={setBy} options={BYS} />
      </div>
      {rows.length ? (
        <div className="scroller">
          <table className="shares ep-table">
            <thead>
              <tr>
                <SortTh s={s} k="rank" num first="asc" tip={`the rank by ${by === 'calls' ? 'calls' : 'bytes'}`}>#</SortTh>
                <SortTh s={s} k="name" first="asc" tip="a name from reverse DNS or a command line, else the address; hover for its addresses and ports">endpoint</SortTh>
                <SortTh s={s} k="calls" num tip="send and receive calls">calls</SortTh>
                <SortTh s={s} k="tx" num tip="bytes sent to it">sent</SortTh>
                <SortTh s={s} k="rx" num tip="bytes received from it">received</SortTh>
                {cls.map((c) => (
                  <SortTh key={c} s={s} k={c} className="cls" style={{ color: CLS_COL[c] }}
                    tip={`${CLS_LBL[c] ?? c}'s share of each endpoint's ${by === 'calls' ? 'calls' : 'bytes'}`}>
                    {CLS_LBL[c] ?? c}
                  </SortTh>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                // each class's share of the endpoint: out of every class that
                // reached it, whatever the chips show (lib/shares.ts)
                const tot = wholeOf(Object.keys(r.by_class), (c) => of(r, c));
                return (
                  <tr key={`${r.name}\u0000${r.inbound}`}>
                    <td className="num nm">{rank.get(r)}</td>
                    <td className="ep-name" data-tip={rowTip(r, ep.rdns, cls, nodes)}>
                      <span className="mono">{r.name}</span>
                      {r.inbound && <span className="ep-tag">inbound</span>}
                      {r.pending && <span className="ep-tag">looking up</span>}
                    </td>
                    <td className="num">{fint(callsOf(r, cls))}</td>
                    <td className="num">{mb(txOf(r, cls) / MB)}</td>
                    <td className="num">{mb(rxOf(r, cls) / MB)}</td>
                    {cls.map((c) => {
                      const v = of(r, c);
                      return (
                        <td key={c}>
                          <ShareCell pct={shareOf(v, tot)}
                            sub={v ? (by === 'calls' ? fint(v) : mb(v / MB)) : DASH} c={CLS_COL[c]} />
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="nm">no endpoint was listed for the selected classes</div>
      )}
      <div className="nm net-ends-note">
        {`${fint(ep.names)} endpoints from ${fint(ep.addresses)} addresses in the window.`}
        {panel > 0 && ` Endpoints are known for ${pc((100 * listed) / panel)} of the bytes in the per-class table.`}
        {lost > 0 && ` The collector listed no endpoint for ${mb(lost / MB)}.`}
        {missing > 0 && ` ${fint(missing)} process${missing === 1 ? '' : 'es'} moved bytes without an endpoint list.`}
        {ep.pending > 0 && ` ${fint(ep.pending)} name${ep.pending === 1 ? ' is' : 's are'} still being looked up.`}
        {!ep.rdns && ' Reverse DNS is off, so only command lines name endpoints.'}
      </div>
    </div>
  );
}
