import { ReactNode, useEffect, useState } from 'react';
import { api, Json, number } from './api';
import { Field, RangeField } from './ui';

export function Histogram({ data }: { data: Json }) {
  const peak = Math.max(1, ...data.histogram.flatMap((b: Json) => [b.baseline, b.affected]));
  return <div className="distribution-plot">
    <svg viewBox="0 0 600 120" role="img" aria-label="Distribution of baseline and affected durations">
      {data.histogram.map((b: Json, i: number) => <g key={i}>
        <rect x={i * 25} y={110 - b.baseline / peak * 100} width="23" height={b.baseline / peak * 100} fill="var(--chart-2)" opacity=".45" />
        <rect x={i * 25 + 5} y={110 - b.affected / peak * 100} width="13" height={b.affected / peak * 100} fill="var(--mint)" opacity=".85" />
      </g>)}
    </svg>
    <div className="distribution-axis"><span>{number(Math.round(data.baseline.min))} ms</span><span>{number(Math.round(Math.max(data.baseline.max, data.affected.max)))} ms</span></div>
    <div className="distribution-numbers"><span>Typical <strong>{number(Math.round(data.affected.p50))} ms</strong></span><span>Slow end <strong>{number(Math.round(data.affected.p95))} ms</strong></span><span>{number(data.count)} draws</span></div>
  </div>;
}
export type Scale = { noun: string; verb: string; min: number; max: number };
const fmt = (ms: number) => ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
const Z95 = 1.6448536269514722;

/** A log-scale slider: equal steps from milliseconds to minutes. */
function LogRange({ label, value, min, max, onChange, hint }: { label: string; value: number; min: number; max: number; onChange: (v: number) => void; hint?: string }) {
  const lo = Math.log(min), hi = Math.log(max), pos = Math.round(1000 * (Math.log(Math.min(max, Math.max(min, value))) - lo) / (hi - lo));
  return <RangeField label={label} hint={hint} min={0} max={1000} value={pos} readout={fmt(value)} valueText={fmt(value)}
    onChange={(p) => onChange(Math.round(Math.exp(lo + (p / 1000) * (hi - lo)) * 1000) / 1000)} />;
}

export default function DistributionEditor({binding, metadata, change, scale, children}: {binding:Json; metadata:Json; change:(b:Json)=>void; scale?: Scale; children?: ReactNode}) {
  const [data,setData] = useState<Json|null>(null), [error,setError] = useState('');
  const config = binding.distribution, family = metadata.families[config.family];
  const encoded = JSON.stringify({distribution:config,quantity:binding.quantity,effect:binding.effect});
  useEffect(()=>{
    let current=true; setData(null); setError('');
    const timer=setTimeout(()=>{api('/distributions/preview',JSON.parse(encoded)).then(d=>{if(current)setData(d);}).catch(e=>{if(current)setError(e.message);});},250);
    return ()=>{current=false;clearTimeout(timer);};
  },[encoded]);
  const param=(key:string,value:number)=>change({...binding,distribution:{...config,parameters:{...config.parameters,[key]:value}}});
  const bounds=config.bounds || {low:0,high:metadata.quantities[binding.quantity].max,policy:'truncate'};
  const log=config.family==='lognormal';
  const median=log?Math.exp(config.parameters.mu):0, p95=log?Math.exp(config.parameters.mu+Z95*config.parameters.sigma):0;
  // Typical is the median and "slowest 5%" the 95th percentile of a log-normal distribution.
  function friendly(typical:number,slow:number){const t=Math.max(.001,typical), sl=Math.max(t*1.01,slow);change({...binding,distribution:{...config,parameters:{mu:Math.log(t),sigma:Math.log(sl/t)/Z95}}});}
  const sc = scale || {noun:'values',verb:'take',min:1,max:metadata.quantities[binding.quantity].max};
  const shown = data ? data.affected : log ? {p50: median, p95} : null;
  return <div className="distribution-editor">
    {log ? <div className="range-stack">
      <LogRange label="Typical" hint={`Half of all ${sc.noun} are faster than this.`} value={median} min={sc.min} max={sc.max} onChange={v=>friendly(v,Math.max(p95,v*1.01))}/>
      <LogRange label="Slowest 5%" hint={`1 in 20 ${sc.noun} take longer than this.`} value={p95} min={sc.min} max={sc.max} onChange={v=>friendly(Math.min(median,v/1.01),v)}/>
    </div> : <p className="hint">This change uses a custom distribution shape; adjust it under Expert.</p>}
    {shown && <p className="speed-summary" aria-live="polite">Most {sc.noun} {sc.verb} about <strong>{fmt(shown.p50)}</strong>; 1 in 20 {sc.verb} over <strong>{fmt(shown.p95)}</strong>.</p>}
    {error ? <p role="alert" className="form-error">{error}</p> : data ? <Histogram data={data}/> : <p className="hint" role="status">Updating distribution…</p>}
    <p className="hint">Blue: usual values. Mint: with this change.</p>
    <details className="tab-disclosure"><summary>Expert</summary><div className="field-grid">
      <Field label="Distribution family"><select value={config.family} onChange={e=>change({...binding,distribution:{...config,family:e.target.value,parameters:Object.fromEntries(Object.entries(metadata.families[e.target.value].parameters).map(([k,v])=>[k,(v as Json).default]))}})}>{Object.entries(metadata.families).map(([k,v])=><option key={k} value={k}>{(v as Json).name}</option>)}</select></Field>
      {Object.entries(family.parameters).map(([key,v])=><Field key={key} label={(v as Json).label}><input type="number" step="any" value={config.parameters[key]} onChange={e=>param(key,Number(e.target.value))}/></Field>)}
      <Field label="Minimum (ms)"><input type="number" step="any" value={bounds.low} onChange={e=>change({...binding,distribution:{...config,bounds:{...bounds,low:Number(e.target.value)}}})}/></Field>
      <Field label="Maximum (ms)"><input type="number" step="any" value={bounds.high} onChange={e=>change({...binding,distribution:{...config,bounds:{...bounds,high:Number(e.target.value)}}})}/></Field>
      <Field label="Outside bounds"><select value={bounds.policy} onChange={e=>change({...binding,distribution:{...config,bounds:{...bounds,policy:e.target.value}}})}><option value="truncate">Resample within bounds</option><option value="clip">Clip to boundary</option></select></Field>
    </div><p className="hint">{family.description} Upper-bound values do not imply a timeout.</p>
    {data && <p className="hint">p75 {data.affected.p75.toFixed(1)} · p90 {data.affected.p90.toFixed(1)} · p99 {data.affected.p99.toFixed(1)} ms. {data.resampled} resamples · {data.bounded} bounded draws.</p>}
    {children}</details>
  </div>;
}
