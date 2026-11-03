import { useEffect, useState } from 'react';
import { api, Json, number } from './api';
import { Button, Field } from './ui';

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
export default function DistributionEditor({binding, metadata, change}: {binding:Json; metadata:Json; change:(b:Json)=>void}) {
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
  const median=log?Math.exp(config.parameters.mu):0, p95=log?Math.exp(config.parameters.mu+1.6448536269514722*config.parameters.sigma):0;
  function friendly(typical:number,slow:number){change({...binding,distribution:{...config,parameters:{mu:Math.log(Math.max(.001,typical)),sigma:Math.log(Math.max(.001,slow)/Math.max(.001,typical))/1.6448536269514722}}});}
  return <div className="distribution-editor">
    <p className="hint">{metadata.quantities[binding.quantity].description}</p>
    {log && <div className="field-grid"><Field label="Typical duration (ms)"><input type="number" min=".001" step="any" value={Number(median.toFixed(3))} onChange={e=>friendly(Number(e.target.value),p95)}/></Field><Field label="Slow-end duration · p95 (ms)"><input type="number" min=".001" step="any" value={Number(p95.toFixed(3))} onChange={e=>friendly(median,Number(e.target.value))}/></Field></div>}
    {error ? <p role="alert" className="form-error">{error}</p> : data ? <Histogram data={data}/> : <p className="hint" role="status">Updating distribution…</p>}
    <p className="hint">Raw modeled values, before journey constraints. Blue: baseline. Mint: affected.</p>
    <details><summary>Distribution details</summary><div className="field-grid">
      <Field label="Distribution family"><select value={config.family} onChange={e=>change({...binding,distribution:{...config,family:e.target.value,parameters:Object.fromEntries(Object.entries(metadata.families[e.target.value].parameters).map(([k,v])=>[k,(v as Json).default]))}})}>{Object.entries(metadata.families).map(([k,v])=><option key={k} value={k}>{(v as Json).name}</option>)}</select></Field>
      {Object.entries(family.parameters).map(([key,v])=><Field key={key} label={(v as Json).label}><input type="number" step="any" value={config.parameters[key]} onChange={e=>param(key,Number(e.target.value))}/></Field>)}
      <Field label="Minimum (ms)"><input type="number" step="any" value={bounds.low} onChange={e=>change({...binding,distribution:{...config,bounds:{...bounds,low:Number(e.target.value)}}})}/></Field>
      <Field label="Maximum (ms)"><input type="number" step="any" value={bounds.high} onChange={e=>change({...binding,distribution:{...config,bounds:{...bounds,high:Number(e.target.value)}}})}/></Field>
      <Field label="Outside bounds"><select value={bounds.policy} onChange={e=>change({...binding,distribution:{...config,bounds:{...bounds,policy:e.target.value}}})}><option value="truncate">Resample within bounds</option><option value="clip">Clip to boundary</option></select></Field>
    </div><p className="hint">{family.description} Upper-bound values do not imply a timeout.</p>
    {data && <p className="hint">p75 {data.affected.p75.toFixed(1)} · p90 {data.affected.p90.toFixed(1)} · p99 {data.affected.p99.toFixed(1)} ms. {data.resampled} resamples · {data.bounded} bounded draws.</p>}</details>
  </div>;
}
