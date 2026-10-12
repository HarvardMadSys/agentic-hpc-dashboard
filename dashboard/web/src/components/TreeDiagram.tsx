/* A tree drawn as a tree (roadmap notes 35 and 39): boxes from the top down,
 * joined by lines, the way a model graph reads in Netron -- never an indented
 * table. Shared by the Agents tab (agents and their subagents) and the
 * Trajectories tab (a session's processes).
 *
 * The layout is a tidy tree, not a general graph: a process tree is a tree. A
 * leaf takes one slot; a parent takes the slots of its children and sits over
 * their middle. That is enough to keep siblings apart and lines from crossing,
 * with no layout library. What is shown is the caller's choice: folding a
 * subtree is passing fewer nodes.
 *
 * Boxes are HTML laid over an SVG of the connectors, so a box can carry text,
 * badges, and hover tips like any other cell on the page.
 */
import type { ReactNode } from 'react';

export interface TreeNode<T> {
  id: string;
  /** the id of the box above; null (or an id not in the list) makes it a root */
  parent: string | null;
  data: T;
}

interface Placed<T> {
  n: TreeNode<T>;
  x: number;
  y: number;
}

export function layoutTree<T>(nodes: TreeNode<T>[]): { placed: Placed<T>[]; slots: number; depth: number } {
  const ids = new Set(nodes.map((n) => n.id));
  const kids = new Map<string, TreeNode<T>[]>();
  const roots: TreeNode<T>[] = [];
  for (const n of nodes) {
    if (n.parent != null && ids.has(n.parent) && n.parent !== n.id) {
      const a = kids.get(n.parent);
      if (a) a.push(n);
      else kids.set(n.parent, [n]);
    } else roots.push(n);
  }
  const width = new Map<string, number>();
  const measure = (n: TreeNode<T>, seen: Set<string>): number => {
    if (seen.has(n.id)) return 1; // a cycle in bad data: draw it once
    seen.add(n.id);
    const ks = kids.get(n.id) ?? [];
    const w = ks.length ? ks.reduce((a, k) => a + measure(k, seen), 0) : 1;
    width.set(n.id, w);
    return w;
  };
  const seen = new Set<string>();
  let slots = 0;
  for (const r of roots) slots += measure(r, seen);
  const placed: Placed<T>[] = [];
  let depth = 0;
  const place = (n: TreeNode<T>, left: number, d: number, done: Set<string>) => {
    if (done.has(n.id)) return;
    done.add(n.id);
    const w = width.get(n.id) ?? 1;
    placed.push({ n, x: left + w / 2, y: d });
    depth = Math.max(depth, d);
    let at = left;
    for (const k of kids.get(n.id) ?? []) {
      place(k, at, d + 1, done);
      at += width.get(k.id) ?? 1;
    }
  };
  let at = 0;
  const done = new Set<string>();
  for (const r of roots) {
    place(r, at, 0, done);
    at += width.get(r.id) ?? 1;
  }
  return { placed, slots: Math.max(1, slots), depth };
}

export function TreeDiagram<T>({
  nodes,
  render,
  nodeW = 184,
  nodeH = 62,
  gapX = 16,
  gapY = 30,
  selected,
  onSelect,
  className,
}: {
  nodes: TreeNode<T>[];
  render: (n: TreeNode<T>) => ReactNode;
  nodeW?: number;
  nodeH?: number;
  gapX?: number;
  gapY?: number;
  selected?: string | null;
  onSelect?: (id: string) => void;
  className?: string;
}) {
  const { placed, slots, depth } = layoutTree(nodes);
  const sx = nodeW + gapX;
  const sy = nodeH + gapY;
  const W = slots * sx;
  const H = (depth + 1) * sy - gapY;
  const at = new Map(placed.map((p) => [p.n.id, p]));
  // a box's left edge: centred on its slots' middle
  const cx = (p: Placed<T>) => p.x * sx - nodeW / 2;
  return (
    <div className={`tdiag ${className ?? ''}`}>
      <div className="tdiag-canvas" style={{ width: W, height: H }}>
        <svg width={W} height={H} className="tdiag-edges" aria-hidden="true">
          {placed.map((p) => {
            const up = p.n.parent != null ? at.get(p.n.parent) : undefined;
            if (!up) return null;
            const x1 = cx(up) + nodeW / 2;
            const y1 = up.y * sy + nodeH;
            const x2 = cx(p) + nodeW / 2;
            const y2 = p.y * sy;
            const mid = y1 + (y2 - y1) / 2;
            return <path key={p.n.id} d={`M${x1},${y1} V${mid} H${x2} V${y2}`} />;
          })}
        </svg>
        {placed.map((p) => (
          <div
            key={p.n.id}
            className={`tnode${selected === p.n.id ? ' on' : ''}${onSelect ? ' click' : ''}`}
            style={{ left: cx(p), top: p.y * sy, width: nodeW, height: nodeH }}
            onClick={onSelect ? () => onSelect(p.n.id) : undefined}
            role={onSelect ? 'button' : undefined}
            tabIndex={onSelect ? 0 : undefined}
            onKeyDown={onSelect ? (e) => e.key === 'Enter' && onSelect(p.n.id) : undefined}
          >
            {render(p.n)}
          </div>
        ))}
      </div>
    </div>
  );
}
