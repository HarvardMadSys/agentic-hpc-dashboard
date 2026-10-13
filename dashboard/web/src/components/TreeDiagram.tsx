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
 *
 * A wide tree scrolls inside its own box, and a parent sits over the middle of
 * its children -- so the box a reader is after can be a screen or more to the
 * right. The diagram scrolls to it: to `focus` (the first root unless the
 * caller names one) when first drawn and when the focus changes, and a box
 * that was clicked stays where it was on screen while the tree re-lays out
 * around what it opened.
 */
import { useLayoutEffect, useRef } from 'react';
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

/** Where everything goes, in pixels: each box's top-left corner and each
 *  connector's path. The inline diagram, the popped-out one, its minimap, and
 *  its export all draw from this, so they agree box for box. */
export interface TreeGeo<T> {
  W: number;
  H: number;
  nodeW: number;
  nodeH: number;
  boxes: { n: TreeNode<T>; x: number; y: number }[];
  at: Map<string, { x: number; y: number }>;
  edges: { id: string; d: string }[];
}

export function treeGeometry<T>(
  nodes: TreeNode<T>[],
  nodeW: number,
  nodeH: number,
  gapX: number,
  gapY: number,
): TreeGeo<T> {
  const { placed, slots, depth } = layoutTree(nodes);
  const sx = nodeW + gapX;
  const sy = nodeH + gapY;
  // a box's left edge: centred on its slots' middle
  const boxes = placed.map((p) => ({ n: p.n, x: p.x * sx - nodeW / 2, y: p.y * sy }));
  const at = new Map(boxes.map((b) => [b.n.id, { x: b.x, y: b.y }]));
  const edges: { id: string; d: string }[] = [];
  for (const b of boxes) {
    const up = b.n.parent != null ? at.get(b.n.parent) : undefined;
    if (!up) continue;
    const x1 = up.x + nodeW / 2;
    const y1 = up.y + nodeH;
    const x2 = b.x + nodeW / 2;
    const y2 = b.y;
    const mid = y1 + (y2 - y1) / 2;
    // an elbow with rounded corners: one fork per parent, not a grid
    const r = Math.min(6, Math.abs(x2 - x1) / 2, (y2 - y1) / 2);
    const dir = x2 > x1 ? 1 : -1;
    const d = Math.abs(x2 - x1) < 0.5
      ? `M${x1},${y1} V${y2}`
      : `M${x1},${y1} V${mid - r} Q${x1},${mid} ${x1 + dir * r},${mid} ` +
        `H${x2 - dir * r} Q${x2},${mid} ${x2},${mid + r} V${y2}`;
    edges.push({ id: b.n.id, d });
  }
  return { W: slots * sx, H: (depth + 1) * sy - gapY, nodeW, nodeH, boxes, at, edges };
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
  focus,
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
  /** the box to scroll into view; the first root when not given */
  focus?: string | null;
  className?: string;
}) {
  const { W, H, boxes, edges } = treeGeometry(nodes, nodeW, nodeH, gapX, gapY);
  const box = useRef<HTMLDivElement>(null);
  const els = useRef(new Map<string, HTMLDivElement>());
  // the clicked box and where it was on screen, to put it back after the click
  const pin = useRef<{ id: string; left: number } | null>(null);
  const want = focus ?? boxes[0]?.n.id ?? null;

  useLayoutEffect(() => {
    const p = pin.current;
    pin.current = null;
    const el = els.current.get(p?.id ?? '');
    if (p && el && box.current) box.current.scrollLeft += el.getBoundingClientRect().left - p.left;
  });
  useLayoutEffect(() => {
    const el = want != null ? els.current.get(want) : undefined;
    const b = box.current;
    if (!el || !b) return;
    const r = b.getBoundingClientRect();
    const e = el.getBoundingClientRect();
    if (e.left >= r.left && e.right <= r.right) return; // already in view
    b.scrollLeft += e.left + e.width / 2 - (r.left + r.width / 2);
  }, [want]);
  const click = onSelect
    ? (id: string) => {
        const el = els.current.get(id);
        if (el) pin.current = { id, left: el.getBoundingClientRect().left };
        onSelect(id);
        // a click that changed nothing must not move a later redraw
        requestAnimationFrame(() => (pin.current = null));
      }
    : undefined;
  return (
    <div className={`tdiag ${className ?? ''}`} ref={box}>
      <div className="tdiag-canvas" style={{ width: W, height: H }}>
        <svg width={W} height={H} className="tdiag-edges" aria-hidden="true">
          {edges.map((e) => (
            <path key={e.id} d={e.d} />
          ))}
        </svg>
        {boxes.map((p) => (
          <div
            key={p.n.id}
            ref={(el) => {
              if (el) els.current.set(p.n.id, el);
              else els.current.delete(p.n.id);
            }}
            className={`tnode${selected === p.n.id ? ' on' : ''}${click ? ' click' : ''}`}
            style={{ left: p.x, top: p.y, width: nodeW, height: nodeH }}
            onClick={click ? () => click(p.n.id) : undefined}
            role={click ? 'button' : undefined}
            tabIndex={click ? 0 : undefined}
            onKeyDown={click ? (e) => e.key === 'Enter' && click(p.n.id) : undefined}
          >
            {render(p.n)}
          </div>
        ))}
      </div>
    </div>
  );
}
