/* The page's icons: one monoline set drawn here, on a 20-unit grid, in the
 * current text colour. No icon library: the runtime is React and nothing else
 * (README, "Dependencies"), and a set this small is cheaper as paths.
 *
 * An icon is chrome, never data. None of them is drawn in a series colour, so
 * none can be read as a class: crimson stays the agent's.
 */
import type { ReactNode } from 'react';

const dot = (cx: number, cy: number, r = 1) => <circle cx={cx} cy={cy} r={r} fill="currentColor" stroke="none" />;

const PATHS = {
  // tabs
  live: <polyline points="2,10.5 6,10.5 8.4,4.5 11.6,15.5 14,10.5 18,10.5" />,
  overview: (
    <>
      <rect x="3" y="3" width="6" height="6" rx="1.4" />
      <rect x="11" y="3" width="6" height="6" rx="1.4" />
      <rect x="3" y="11" width="6" height="6" rx="1.4" />
      <rect x="11" y="11" width="6" height="6" rx="1.4" />
    </>
  ),
  trend: (
    <>
      <path d="M3 16.5h14" />
      <polyline points="3.5,13 7.5,9.5 10.5,11.5 16.5,5" />
      <polyline points="13,5 16.5,5 16.5,8.5" />
    </>
  ),
  users: (
    <>
      <circle cx="10" cy="7" r="3.2" />
      <path d="M3.8 17c.9-3.2 3.3-4.8 6.2-4.8s5.3 1.6 6.2 4.8" />
    </>
  ),
  agents: (
    <>
      <rect x="4" y="6.5" width="12" height="9" rx="2.4" />
      <path d="M10 6.5V3.8M2.6 10v2.5M17.4 10v2.5" />
      {dot(10, 3.2, 0.9)}
      {dot(7.6, 11)}
      {dot(12.4, 11)}
    </>
  ),
  trajectories: (
    <>
      <circle cx="4.6" cy="15.4" r="1.8" />
      <circle cx="15.4" cy="4.6" r="1.8" />
      <path d="M6.4 15.4H12a2.7 2.7 0 0 0 0-5.4H8a2.7 2.7 0 0 1 0-5.4h5.6" />
    </>
  ),
  resources: (
    <>
      <path d="M3.4 14.6a6.6 6.6 0 1 1 13.2 0" />
      <path d="M10 14.6l3.4-4.4" />
      {dot(10, 14.6, 1.2)}
    </>
  ),
  risk: (
    <>
      <path d="M10 2.6l6 2.4v4.7c0 3.8-2.6 6.4-6 7.7-3.4-1.3-6-3.9-6-7.7V5z" />
      <path d="M10 6.8v4" />
      {dot(10, 13.4, 0.95)}
    </>
  ),
  // Security: a padlock, so it is not read as the Risks shield
  security: (
    <>
      <rect x="4.2" y="8.8" width="11.6" height="8.4" rx="1.8" />
      <path d="M6.8 8.8V6.4a3.2 3.2 0 0 1 6.4 0v2.4" />
      {dot(10, 12.4, 1.05)}
      <path d="M10 12.9v1.7" />
    </>
  ),
  // HPC: a compute chip, for the cluster the scheduler hands out
  hpc: (
    <>
      <rect x="5" y="5" width="10" height="10" rx="1.6" />
      <rect x="8" y="8" width="4" height="4" rx="0.6" />
      <path d="M7.6 2.6v2.4M12.4 2.6v2.4M7.6 15v2.4M12.4 15v2.4M2.6 7.6h2.4M2.6 12.4h2.4M15 7.6h2.4M15 12.4h2.4" />
    </>
  ),
  // the login nodes: a rack of two hosts
  nodes: (
    <>
      <rect x="3.4" y="3.4" width="13.2" height="5.6" rx="1.5" />
      <rect x="3.4" y="11" width="13.2" height="5.6" rx="1.5" />
      <path d="M10 6.2h3.8M10 13.8h3.8" />
      {dot(6.4, 6.2, 0.95)}
      {dot(6.4, 13.8, 0.95)}
    </>
  ),
  // the scheduler: a queue of jobs
  queue: (
    <>
      <path d="M8 5.5h8.5M8 10h8.5M8 14.5h8.5" />
      {dot(4.4, 5.5, 1.1)}
      {dot(4.4, 10, 1.1)}
      {dot(4.4, 14.5, 1.1)}
    </>
  ),
  chat: (
    <>
      <path d="M4.2 4h11.6A1.7 1.7 0 0 1 17.5 5.7v6.9a1.7 1.7 0 0 1-1.7 1.7H9.4L5.4 17.2v-2.9H4.2a1.7 1.7 0 0 1-1.7-1.7V5.7A1.7 1.7 0 0 1 4.2 4z" />
      {dot(7, 9.2, 0.95)}
      {dot(10, 9.2, 0.95)}
      {dot(13, 9.2, 0.95)}
    </>
  ),
  internal: (
    <>
      <path d="M7.6 3h4.8M8.6 3v4.7l-4.3 7.5A1.6 1.6 0 0 0 5.7 17.6h8.6a1.6 1.6 0 0 0 1.4-2.4l-4.3-7.5V3" />
      <path d="M6.1 12.6h7.8" />
    </>
  ),
  feeds: (
    <>
      <ellipse cx="10" cy="5" rx="6" ry="2.3" />
      <path d="M4 5v10c0 1.3 2.7 2.3 6 2.3s6-1 6-2.3V5" />
      <path d="M4 10c0 1.3 2.7 2.3 6 2.3s6-1 6-2.3" />
    </>
  ),
  // controls
  refresh: (
    <>
      <path d="M16.2 10.2a6.2 6.2 0 1 1-1.9-4.6" />
      <path d="M16.4 3.4v3.4H13" />
    </>
  ),
  menu: <path d="M3.5 5.5h13M3.5 10h13M3.5 14.5h13" />,
  close: <path d="M5 5l10 10M15 5L5 15" />,
  collapse: (
    <>
      <rect x="2.8" y="3.6" width="14.4" height="12.8" rx="2.2" />
      <path d="M7.6 3.6v12.8M13.4 8l-2 2 2 2" />
    </>
  ),
  expand: (
    <>
      <rect x="2.8" y="3.6" width="14.4" height="12.8" rx="2.2" />
      <path d="M7.6 3.6v12.8M11.4 8l2 2-2 2" />
    </>
  ),
  search: (
    <>
      <circle cx="8.8" cy="8.8" r="5.2" />
      <path d="M12.6 12.6L17 17" />
    </>
  ),
  section: <path d="M5 6h10M5 10h10M5 14h6" />,
  info: (
    <>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 9v5M10 6.2v.1" />
    </>
  ),
  window: (
    <>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6v4.2l2.8 1.8" />
    </>
  ),
  pointLeft: <path d="M16 10H4.5M9 5.5L4.5 10 9 14.5" />,
  hosts: (
    <>
      <rect x="3" y="4" width="14" height="9.5" rx="1.6" />
      <path d="M7.5 16.5h5M10 13.5v3" />
    </>
  ),
  // sign out: a door, and an arrow that leaves it
  signout: (
    <>
      <path d="M8.5 3.5H5.3a1.5 1.5 0 0 0-1.5 1.5v10a1.5 1.5 0 0 0 1.5 1.5h3.2" />
      <path d="M12.6 6.4L16.2 10l-3.6 3.6M16.2 10H8" />
    </>
  ),
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 18, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      className={className ? `icon ${className}` : 'icon'}
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
