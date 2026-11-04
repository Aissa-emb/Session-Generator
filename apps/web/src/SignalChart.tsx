import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Draft, Signal, runMinutes, shareAt, signalInfo, signalNames } from "./workflow";

/** Plot padding scales with the root font size so axis labels keep their room at larger text sizes. */
const layout = (rem: number) => ({ H: Math.max(168, Math.round(rem * 10.5)), PAD: { l: rem * 2.6, r: 12, t: 12, b: rem * 1.7 } });
const STEPS = 120;
type Rates = Pick<Draft, "rates" | "patterns" | "mode" | "duration">;

/** Round a top value up to a readable axis maximum. */
function niceMax(v: number) {
  for (const n of [10, 20, 40, 60, 80, 100]) if (v <= n) return n;
  return 100;
}
export function minutesText(m: number) {
  if (m < 1) return `${Math.round(m * 60)} s`;
  if (m >= 120 && m % 60 === 0) return `${m / 60} h`;
  return `${m < 10 ? Math.round(m * 10) / 10 : Math.round(m)} min`;
}
function series(key: Signal, d: Rates) {
  const T = runMinutes(d);
  // Sessions start before the run ends, so the last sample sits just inside the run.
  return Array.from({ length: STEPS + 1 }, (_, i) => shareAt(key, d, Math.min(i / STEPS, 0.999) * T));
}

/** Requested share of sessions across the run, one line per enabled signal, with a hover readout. */
export function RunTimeline({ draft, keys }: { draft: Rates; keys: Signal[] }) {
  const [hover, setHover] = useState<number | null>(null);
  // Draw in CSS pixels so axis text stays legible at every width.
  const box = useRef<HTMLDivElement>(null), [W, setW] = useState(600), [rem, setRem] = useState(16);
  const { H, PAD } = layout(rem);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      setW(Math.max(240, Math.round(entry.contentRect.width)));
      setRem(parseFloat(getComputedStyle(document.documentElement).fontSize) || 16);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const titleId = useId();
  const T = runMinutes(draft);
  const lines = useMemo(() => keys.map((k) => ({ key: k, values: series(k, draft) })), [keys, draft]);
  const top = niceMax(Math.max(5, ...lines.flatMap((l) => l.values)) * 1.1);
  const x = (i: number) => PAD.l + (i / STEPS) * (W - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + (1 - v / top) * (H - PAD.t - PAD.b);
  const path = (values: number[]) => values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const move = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    setHover(Math.max(0, Math.min(STEPS, Math.round(((px - PAD.l) / (W - PAD.l - PAD.r)) * STEPS))));
  };
  const at = hover === null ? null : lines.map((l) => ({ key: l.key, value: l.values[hover] }));
  return (
    <figure className="run-timeline" aria-labelledby={titleId}>
      <figcaption id={titleId} className="sr-only">
        Requested share of sessions over the {minutesText(T)} run for {keys.map((k) => signalNames[k]).join(", ")}.
      </figcaption>
      <div className="run-timeline-plot" ref={box}>
        <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-labelledby={titleId} onPointerMove={move} onPointerLeave={() => setHover(null)}>
          {[0, top / 2, top].map((v) => (
            <g key={v} className="grid">
              <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} />
              <text x={PAD.l - 8} y={y(v) + rem * 0.25} textAnchor="end">{Math.round(v)}%</text>
            </g>
          ))}
          {[0, 0.5, 1].map((f) => (
            <text key={f} className="axis" x={x(f * STEPS)} y={H - rem * 0.4} textAnchor={f === 0 ? "start" : f === 1 ? "end" : "middle"}>
              {f === 0 ? "Start" : minutesText(f * T)}
            </text>
          ))}
          {lines.map((l) => (
            <path key={l.key} d={path(l.values)} className="series" stroke={signalInfo[l.key].color} />
          ))}
          {hover !== null && (
            <g className="crosshair">
              <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={H - PAD.b} />
              {at!.map((p) => (
                <circle key={p.key} cx={x(hover)} cy={y(p.value)} r="4" fill={signalInfo[p.key].color} />
              ))}
            </g>
          )}
        </svg>
        {hover !== null && at && (
          <div className="chart-tooltip" aria-hidden="true"
            // Narrow plots pin the readout to the far edge instead of following the crosshair.
            style={W < 480 ? (hover > STEPS / 2 ? { left: 0 } : { right: 0 }) : { left: `${(x(hover) / W) * 100}%` }}
            data-side={W < 480 ? undefined : hover > STEPS / 2 ? "left" : "right"}>
            <strong>{minutesText((hover / STEPS) * T)}</strong>
            {at.map((p) => (
              <span key={p.key}>
                <i style={{ background: signalInfo[p.key].color }} />
                {signalNames[p.key]}
                <b>{p.value.toFixed(1)}%</b>
              </span>
            ))}
          </div>
        )}
      </div>
    </figure>
  );
}

/** A small area plot of one signal's share across the run, scaled to the signal's slider range. */
export function MiniCurve({ signal, draft }: { signal: Signal; draft: Rates }) {
  const values = series(signal, draft), top = signalInfo[signal].max, w = 240, h = 44;
  const pts = values.map((v, i) => `${((i / STEPS) * w).toFixed(1)},${(h - 2 - (v / top) * (h - 4)).toFixed(1)}`);
  return (
    <svg className="mini-curve" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
      <path d={`M0,${h} L${pts.join(" L")} L${w},${h} Z`} fill={signalInfo[signal].color} opacity=".18" />
      <path d={`M${pts.join(" L")}`} fill="none" stroke={signalInfo[signal].color} strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

const glyphs: Record<string, string> = {
  baseline: "M2,15 L54,15",
  spike: "M2,19 C16,19 20,3 28,3 C36,3 40,19 54,19",
  release: "M2,19 L16,19 C24,19 26,5 34,5 L54,5",
  network: "M2,18 C8,18 9,6 13,6 C17,6 18,18 24,18 C30,18 31,6 35,6 C39,6 40,18 46,18 C50,18 51,10 54,10",
  healthy: "M2,20 L54,20",
  story: "M6,12 L22,12 M34,12 L50,12",
};
/** A small line glyph that shows each profile's shape over time. */
export function ProfileGlyph({ id }: { id: string }) {
  return (
    <svg className="profile-glyph" viewBox="0 0 56 24" aria-hidden="true">
      <path d={glyphs[id] || glyphs.baseline} />
      {id === "story" && (
        <>
          <circle cx="6" cy="12" r="3" /><circle cx="28" cy="12" r="3" /><circle cx="50" cy="12" r="3" />
          <path d="M22,12 L25,12 M31,12 L34,12" />
        </>
      )}
    </svg>
  );
}
