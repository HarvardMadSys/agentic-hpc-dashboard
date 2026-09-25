/* The time-window control.
 *
 * Presets come from the SERVICE, not from this file: the offered list is
 * filtered server-side against `live.retention_min`, so the picker can never
 * offer a span the process does not hold. A custom entry beyond retention is
 * clamped by the backend and the clamp is shown here -- asking for 30 days and
 * silently getting 7 is exactly the kind of quiet substitution the rest of this
 * dashboard refuses to make.
 *
 * Two costs are surfaced rather than hidden, because they are the reason a
 * reader might choose a narrower window:
 *   - a wider window REBUILDS the historical reducers (they hold no time index),
 *     so the build state and its percentage are part of the control, and
 *   - a built window keeps ingesting, so its drift past its own span is shown
 *     once it is material.
 */
import { useEffect, useState } from 'react';
import type { LiveResponse, WindowStatus, WindowsResponse } from '../api/types';

/** `90m`, `4h`, `2d`, or a bare number of minutes. Null when unparseable. */
export function parseWindow(raw: string): number | null {
  const m = /^\s*(\d+(?:\.\d+)?)\s*([mhdw]?)\s*$/i.exec(raw);
  if (!m) return null;
  const n = Number(m[1]);
  if (!Number.isFinite(n) || n <= 0) return null;
  const mult = { m: 1, h: 60, d: 1440, w: 10080, '': 1 }[m[2].toLowerCase()] ?? 1;
  return Math.round(n * mult);
}

function human(minutes: number): string {
  if (minutes % 1440 === 0 && minutes >= 2880) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${minutes}m`;
}

export function WindowPicker({
  value,
  onChange,
  windows,
  resolved,
  status,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  windows: WindowsResponse | null;
  resolved?: LiveResponse['window'];
  status?: WindowStatus | null;
}) {
  const presets = windows?.presets ?? [];
  const active = resolved?.minutes ?? value ?? windows?.default_minutes ?? null;
  const isPreset = presets.some((p) => p.minutes === active);
  const [draft, setDraft] = useState('');
  const [bad, setBad] = useState(false);

  // A window chosen elsewhere (a pasted link, the back button) must show up in
  // the custom box, or the control would contradict the chart beside it. And a
  // preset clears it, so the box cannot keep reading `90m` next to a lit `6h`.
  useEffect(() => {
    if (active == null) return;
    setDraft(isPreset ? '' : human(active));
  }, [active, isPreset]);

  const commit = (raw: string) => {
    if (!raw.trim()) {
      setBad(false);
      // Blurring an empty box must not silently discard a chosen window: only
      // an explicitly CLEARED custom value means "back to the default".
      if (!isPreset && value != null) onChange(null);
      return;
    }
    const m = parseWindow(raw);
    setBad(m == null);
    if (m != null) onChange(m);
  };

  const cap = windows?.retention_minutes;
  const building = status && status.state !== 'ready' ? status : null;
  const drift =
    status && status.drift_s != null && status.drift_s > status.drift_budget_s / 2
      ? status
      : null;

  return (
    <div className="winpick">
      <span className="eyebrow">window</span>
      <div className="seg">
        {presets.map((p) => (
          <button
            key={p.minutes}
            aria-pressed={p.minutes === active}
            onClick={() => onChange(p.minutes)}
            data-tip={`the last ${p.label}`}
          >
            {p.label}
          </button>
        ))}
      </div>
      <input
        className={`wininput${bad ? ' bad' : ''}`}
        value={draft}
        placeholder="custom"
        aria-label="custom window, e.g. 90m, 4h, 2d"
        size={7}
        onChange={(e) => {
          setDraft(e.target.value);
          setBad(false);
        }}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit((e.target as HTMLInputElement).value);
        }}
        data-tip={
          cap
            ? `90m, 4h, 2d — up to ${human(cap)}, which is all this service retains`
            : '90m, 4h, 2d'
        }
      />
      {bad && <span className="winnote bad">unreadable — try 90m, 4h or 2d</span>}

      {/* The backend clamped what was asked for. Never silent. */}
      {resolved?.clamped && resolved.note && (
        <span className="winnote bad" data-tip={resolved.note}>
          asked {resolved.requested_minutes != null ? human(resolved.requested_minutes) : '?'} ·
          showing {resolved.label ?? human(resolved.minutes)}
        </span>
      )}

      {/* The live bins do not reach back as far as the window does. */}
      {resolved?.retained_minutes != null &&
        resolved.retained_minutes + 2 < resolved.minutes &&
        !resolved.clamped && (
          <span
            className="winnote"
            data-tip={`the retained bins start at ${resolved.retained_from}; older bins are a gap, not a measured zero`}
          >
            filled {human(resolved.retained_minutes)} of {human(resolved.minutes)}
          </span>
        )}

      {building && (
        <span
          className="winnote build"
          data-tip={`the historical panels hold no time index, so this window is being reduced from the rows: ${building.progress.files} file(s), ${building.progress.records.toLocaleString()} records so far`}
        >
          rebuilding {building.label} · {building.progress.pct}%
        </span>
      )}

      {!building && drift && (
        <span className="winnote" data-tip={drift.covers_note ?? ''}>
          +{Math.round((drift.drift_s ?? 0) / 60)}m past the window
        </span>
      )}
    </div>
  );
}
