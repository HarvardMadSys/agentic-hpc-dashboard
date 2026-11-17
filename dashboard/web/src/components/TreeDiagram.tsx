/* A tree drawn as a tree (roadmap notes 35 and 39): boxes from the top down,
 * joined by lines, the way a model graph reads in Netron -- never an indented
 * table. Shared by the Agents tab (agents and their subagents) and the
 * Sessions tab (a session's processes).
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
 *
 * A box cut at the edge of the scroll box looks like a narrow box, not like
 * more tree. So an edge the tree runs past fades out (`.tdiag-wrap.more-l`,
 * `.more-r`), and holds a button that scrolls a screenful that way.
 */
import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
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

function TreeDiagramView<T>({
  nodes,
  render,
  nodeW = 184,
  nodeH = 62,
  gapX = 16,
  gapY = 30,
  selected,
  onSelect,
  focus,
  mark,
  goTo,
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
  /** a class of a box's own, by its id: what a find matched */
  mark?: (id: string) => string | undefined;
  /** a box to bring into view, across and down the page, on each new `n`:
   *  where a find went */
  goTo?: { id: string; n: number } | null;
  className?: string;
}) {
  // laid out again only when the boxes or their sizes change
  const { W, H, boxes, edges } = useMemo(
    () => treeGeometry(nodes, nodeW, nodeH, gapX, gapY),
    [nodes, nodeW, nodeH, gapX, gapY],
  );
  const box = useRef<HTMLDivElement>(null);
  const els = useRef(new Map<string, HTMLDivElement>());
  // One ref for every box. A function made per box per render was a new ref
  // each time, so React detached and attached all of them again on every
  // render. This one files a box under its id as it mounts, and what it
  // returns takes it out when the box goes (React 19's ref cleanup).
  const keep = useCallback((el: HTMLDivElement | null) => {
    if (!el) return;
    const id = el.dataset.id ?? '';
    els.current.set(id, el);
    return () => {
      if (els.current.get(id) === el) els.current.delete(id);
    };
  }, []);
  // the clicked box and where it was on screen, to put it back after the click
  const pin = useRef<{ id: string; left: number } | null>(null);
  const want = focus ?? boxes[0]?.n.id ?? null;
  // which sides the tree runs past the scroll box on
  const [more, setMore] = useState<{ l: boolean; r: boolean }>({ l: false, r: false });

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
  // Once, after the layout that draws it: a find opens the boxes above its
  // match, so the box may only be drawn a render after `goTo` came.
  const went = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (!goTo || went.current === goTo.n) return;
    const el = els.current.get(goTo.id);
    if (!el) return;
    went.current = goTo.n;
    el.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
  }, [goTo, nodes]);
  // Kept as the box scrolls and is resized, and read again when the tree is a different width. Declared after the
  // effects that scroll, so it reads where they left the box.
  useLayoutEffect(() => {
    const b = box.current;
    if (!b) return;
    const read = () => {
      const l = b.scrollLeft > 1;
      const r = b.scrollLeft + b.clientWidth < b.scrollWidth - 1;
      setMore((m) => (m.l === l && m.r === r ? m : { l, r }));
    };
    read();
    b.addEventListener('scroll', read, { passive: true });
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(read);
    ro?.observe(b);
    return () => {
      b.removeEventListener('scroll', read);
      ro?.disconnect();
    };
  }, [W]);
  const page = (dir: 1 | -1) => {
    const b = box.current;
    if (!b) return;
    const smooth = !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    b.scrollBy({ left: dir * Math.max(nodeW + gapX, b.clientWidth * 0.8), behavior: smooth ? 'smooth' : 'auto' });
  };
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
    <div className={`tdiag-wrap${more.l ? ' more-l' : ''}${more.r ? ' more-r' : ''}`}>
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
              ref={keep}
              className={`tnode${selected === p.n.id ? ' on' : ''}${click ? ' click' : ''}${mark ? ` ${mark(p.n.id) ?? ''}` : ''}`}
              data-id={p.n.id}
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
      {more.l && (
        <button type="button" className="tdiag-more l" onClick={() => page(-1)} aria-label="scroll the tree left" data-tip="more of the tree to the left">
          ‹
        </button>
      )}
      {more.r && (
        <button type="button" className="tdiag-more r" onClick={() => page(1)} aria-label="scroll the tree right" data-tip="more of the tree to the right">
          ›
        </button>
      )}
    </div>
  );
}

/** Memoized: drawn again when its boxes, its sizes, or what it is handed to
 *  draw and do with them change. Typed as the generic function it wraps. */
export const TreeDiagram = memo(TreeDiagramView) as typeof TreeDiagramView;
