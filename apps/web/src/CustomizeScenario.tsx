import { useRef, useState } from 'react';
import { api, Json } from './api';
import { Button, Field, Panel } from './ui';
import { Draft, Signal, available, defaults, flowNames, signalNames, scenario, restoreDraft } from './workflow';
import DistributionEditor from './DistributionEditor';
import ReleaseEditor from './ReleaseEditor';
const stories=[['healthy','Healthy account review'],['signin-outage','Sign-in pool timeout'],['slow-history','Slow transaction history'],['contract-regression','Transaction detail contract'],['transfer-response-loss','Transfer response lost'],['worker-backlog','Transfer worker backlog'],['frontend-regression','Client view-state failure'],['connectivity','Connectivity transition'],['travel-provider','Travel provider degradation']];
const signalKeys=Object.keys(signalNames) as Signal[];
function Weights({values,change,label}:{values:Json;change:(v:Json)=>void;label:string}){
 const total=Object.values(values).reduce((a:number,b)=>a+Number(b),0);
 return <fieldset className="weight-editor"><legend>{label}</legend><p className="hint">Relative weights · normalized to 100%.</p>{Object.entries(values).map(([key,value])=><label key={key}><span>{key.replaceAll('_',' ')}</span><input aria-label={`${label}: ${key}`} type="number" min="0" value={value} onChange={e=>change({...values,[key]:Number(e.target.value)})}/><small>{total?Math.round(Number(value)/total*100):0}%</small></label>)}</fieldset>;
}
export default function CustomizeScenario({draft,update,caps}:{draft:Draft;update:(d:Partial<Draft>)=>void;caps:Json}){
 const [editor,setEditor]=useState('Population'),[index,setIndex]=useState(0),[quantity,setQuantity]=useState('reading_ms'),[message,setMessage]=useState('');
 const file=useRef<HTMLInputElement>(null),meta=caps.modeling;
 const model=draft.modeling||{version:'1.0',bindings:[],experiments:[]};
 const bindings:Json[]=model.bindings||[],active=bindings[Math.min(index,bindings.length-1)];
 const setModel=(v:Json)=>update({modeling:v,modelingPreset:'custom'});
 const patch=(b:Json)=>setModel({...model,bindings:bindings.map((v,i)=>i===Math.min(index,bindings.length-1)?b:v)});
 const signal=draft.story==='regression'?draft.regression as Signal:undefined;
 const chosen=draft.story==='causal'?'causal:'+draft.causalScenario:draft.modelingPreset?draft.modelingPreset:draft.story==='regression'?'fault:'+draft.regression:signalKeys.some(k=>draft.signals[k]!==defaults.signals[k] || draft.signals[k] && draft.rates[k]!==defaults.rates[k])?'custom':'healthy';
 function preset(id:string){
  const clean={signals:{...defaults.signals},rates:{...draft.rates,network:defaults.rates.network},modeling:undefined,modelingPreset:undefined} as Partial<Draft>;
  if(id.startsWith('causal:')){update({...clean,story:'causal',causalScenario:id.slice(7)});setEditor('Scenario');}
  else if(id.startsWith('fault:')){const k=id.slice(6) as Signal;update({...clean,story:'regression',regression:k,signals:{...defaults.signals,[k]:true}});}
  else if(id==='tail'||id==='network-profile'){
   const q=id==='tail'?'backend_work_ms':'network_rtt_ms',d=structuredClone(meta.defaults[q]);
   d.parameters={mu:Math.log(id==='tail'?50:150),sigma:id==='tail'?.8:.65};
   update({...clean,story:'standard',modelingPreset:id,modeling:{version:'1.0',bindings:[{id:'scenario-performance',quantity:q,distribution:d,share:id==='tail'?.5:1,...(id==='tail'?{effect:{kind:'tail',factor:4,knee:.8}}:{})}],experiments:[]}});setIndex(0);
  }else update({...clean,story:'standard'});
 }
 return <>
 <Panel title="Scenario" className="scenario-picker">
  <div className="field-grid"><Field label="User flow"><select value={draft.flow} disabled={draft.story==='causal'} onChange={e=>update({flow:e.target.value})}>{Object.entries(flowNames).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></Field>
   <Field label="Scenario"><select value={chosen} onChange={e=>preset(e.target.value)}><option value="healthy">Standard population</option><option value="tail">Tail-only slowdown</option><option value="network-profile">Constrained network</option><optgroup label="Failures">{signalKeys.filter(k=>available(k,draft)).map(k=><option key={k} value={'fault:'+k}>{signalNames[k]}</option>)}</optgroup><optgroup label="Connected diagnostic stories">{stories.map(([k,v])=><option key={k} value={'causal:'+k}>{v}</option>)}</optgroup>{chosen==='custom'&&<option value="custom">Custom combination</option>}</select></Field>
  </div>
  <p className="hint">{chosen==='tail'?'Occasional slow gateway work; ordinary requests retain their baseline.':chosen==='network-profile'?'Slower request transit across a varied visitor population.':draft.story==='causal'?'A connected sequence of client actions and backend operations.':'Varied visits, compatible devices, and meaningful business outcomes.'}</p>
  {draft.story!=='causal'&&<p className="hint">{draft.signals.network?`Network failures · ${draft.rates.network}% of sessions`:'Network failures off'}</p>}
  {signal&&draft.signals[signal]&&<div className="compact-field"><Field label="Affected sessions (%)" hint="Share of eligible visits; journey steering may be used."><input type="number" min="1" max="100" value={draft.rates[signal]} onChange={e=>update({rates:{...draft.rates,[signal]:Number(e.target.value)}})}/></Field></div>}
  {chosen==='tail'&&bindings[0]&&<div className="field-grid"><Field label="Affected sessions (%)" hint="Visits reaching gateway work; probability-based selection."><input type="number" min="0" max="100" value={bindings[0].share*100} onChange={e=>update({modeling:{...model,bindings:[{...bindings[0],share:Number(e.target.value)/100}]}})}/></Field><Field label="Tail severity"><input type="number" min="1" max="20" value={bindings[0].effect.factor} onChange={e=>update({modeling:{...model,bindings:[{...bindings[0],effect:{...bindings[0].effect,factor:Number(e.target.value)}}]}})}/></Field></div>}
  {draft.modelingPreset==='custom'&&<p className="hint">Custom performance settings · preserved in Fine-tune.</p>}
  <Button onClick={()=>preset(chosen==='custom'?'healthy':chosen)}>Reset scenario</Button>
 </Panel>
 <details className="panel fine-tune"><summary>Fine-tune</summary>
  <div className="editor-toolbar"><Field label="Edit settings"><select value={draft.story==='causal'?'Scenario':editor==='Scenario'?'Population':editor} onChange={e=>setEditor(e.target.value)}>{(draft.story==='causal'?['Scenario']:['Population','Performance','Journey','Experiments','Release','Faults']).map(v=><option key={v}>{v}</option>)}</select></Field><span className="hint">Optional · defaults are ready</span></div>
  {['Population','Scenario'].includes(editor)&&draft.story!=='causal'&&<>
   <div className="field-grid"><Field label="Visit patterns"><select value={draft.population} onChange={e=>update({population:e.target.value as Draft['population']})}><option value="varied">Varied visits</option><option value="legacy">Previous distribution</option></select></Field><Field label="Returning visitors %" hint="After the visitor pool warms up."><input type="number" min="0" max="100" value={draft.returning} onChange={e=>update({returning:Number(e.target.value)})}/></Field>
    {draft.apps.some(a=>a.platform!=='web')&&<><Field label="Cold starts on return %"><input type="number" min="0" max="100" value={draft.coldStarts} onChange={e=>update({coldStarts:Number(e.target.value)})}/></Field><Field label="App interruptions %"><input type="number" min="0" max="100" value={draft.interruptions} onChange={e=>update({interruptions:Number(e.target.value)})}/></Field></>}
   </div>
   <Button onClick={()=>update({populationOverrides:{...draft.populationOverrides,network_mix:draft.populationOverrides?.network_mix?undefined:{wifi:45,'fast-4g':25,'slow-4g':20,'fast-3g':8,'slow-3g':2}}})}>{draft.populationOverrides?.network_mix?'Use device-based network defaults':'Customize network weights'}</Button>
   {draft.populationOverrides?.network_mix&&<Weights label="Network weights" values={draft.populationOverrides.network_mix} change={v=>update({populationOverrides:{...draft.populationOverrides,network_mix:v}})}/>}
  </>}
  {editor==='Journey'&&draft.story!=='causal'&&<><p className="hint">Intent weights guide real paths. A purchase still requires successful preceding operations.</p>{Object.entries(caps.population_model||{}).filter(([lob])=>draft.flow==='all'||draft.flow===lob).map(([lob,values])=><Weights key={lob} label={lob+' intent weights'} values={draft.populationOverrides?.intent_mix?.[lob]||values as Json} change={v=>update({population:'varied',populationOverrides:{...draft.populationOverrides,intent_mix:{...draft.populationOverrides?.intent_mix,[lob]:v}}})}/>)}</>}
  {editor==='Performance'&&draft.story!=='causal'&&meta&&<>
   <div className="editor-toolbar"><Field label="Quantity"><select value={quantity} onChange={e=>setQuantity(e.target.value)}>{Object.entries(meta.quantities).map(([k,v])=><option key={k} value={k}>{(v as Json).name}</option>)}</select></Field><Button onClick={()=>{setModel({...model,bindings:[...bindings,{id:quantity+'-'+Date.now().toString(36),quantity,priority:Math.max(0,...bindings.map(b=>b.priority||0))+1,distribution:structuredClone(meta.defaults[quantity])}]});setIndex(bindings.length);}}>Add override</Button></div>
   {active?<><Field label="Performance override"><select value={Math.min(index,bindings.length-1)} onChange={e=>setIndex(Number(e.target.value))}>{bindings.map((b,i)=><option key={b.id} value={i}>{meta.quantities[b.quantity]?.name} · {i+1}</option>)}</select></Field>
    <DistributionEditor binding={active} metadata={meta} change={patch}/>
    <details><summary>Audience and effect</summary><div className="field-grid">
     <Field label="Audience platform"><select value={active.cohort?.platforms?.[0]||''} onChange={e=>patch({...active,cohort:{...active.cohort,platforms:e.target.value?[e.target.value]:[]}})}><option value="">All selected platforms</option>{draft.apps.map(a=><option key={a.platform}>{a.platform}</option>)}</select></Field>
     <Field label="Audience device tier"><select value={active.cohort?.device_tiers?.[0]||''} onChange={e=>patch({...active,cohort:{...active.cohort,device_tiers:e.target.value?[e.target.value]:[]}})}><option value="">All devices</option><option value="flagship">Flagship</option><option value="mid">Mid-range</option><option value="budget">Budget</option></select></Field>
     {['networks','intents','app_versions','screens',...(['backend_work_ms','network_rtt_ms'].includes(active.quantity)?['endpoints']:[])].map(key=><Field key={key} label={'Audience '+key.replaceAll('_',' ')} hint="Comma-separated; empty means all."><input value={(active.cohort?.[key]||[]).join(', ')} onChange={e=>patch({...active,cohort:{...active.cohort,[key]:e.target.value.split(',').map(v=>v.trim()).filter(Boolean)}})}/></Field>)}
     <Field label="Audience visits"><select value={active.cohort?.returning===undefined?'all':String(active.cohort.returning)} onChange={e=>{const cohort={...active.cohort};if(e.target.value==='all')delete cohort.returning;else cohort.returning=e.target.value==='true';patch({...active,cohort});}}><option value="all">New and returning</option><option value="true">Returning</option><option value="false">New</option></select></Field>
     {(model.experiments||[]).map((e:Json)=><Field key={e.id} label={'Exposed to '+e.id}><select value={active.cohort?.experiments?.[e.id]?.[0]||''} onChange={event=>{const experiments={...(active.cohort?.experiments||{})};if(event.target.value)experiments[e.id]=[event.target.value];else delete experiments[e.id];patch({...active,cohort:{...active.cohort,experiments}});}}><option value="">Any assignment</option>{Object.keys(e.variants).map(v=><option key={v}>{v}</option>)}</select></Field>)}
     <Field label="Effect"><select value={active.effect?.kind||'none'} onChange={e=>{const next={...active};if(e.target.value==='none')delete next.effect;else next.effect={kind:e.target.value,factor:4,knee:.8};patch(next);}}><option value="none">Baseline distribution</option><option value="scale">Scale the whole distribution</option><option value="tail">Worsen the tail only</option></select></Field>
     {active.effect&&chosen!=='tail'&&<><Field label="Effect factor"><input type="number" step=".1" min="1" max="20" value={active.effect.factor} onChange={e=>patch({...active,effect:{...active.effect,factor:Number(e.target.value)}})}/></Field><Field label="Affected visits (%)"><input type="number" min="0" max="100" value={(active.share??1)*100} onChange={e=>patch({...active,share:Number(e.target.value)/100})}/></Field></>}
     {active.effect?.kind==='tail'&&<Field label="Tail begins at percentile"><input type="number" min="50" max="99" value={(active.effect.knee??.8)*100} onChange={e=>patch({...active,effect:{...active.effect,knee:Number(e.target.value)/100}})}/></Field>}
     <Field label="Override priority" hint="Higher matching priority wins."><input type="number" min="0" max="1000" value={active.priority||0} onChange={e=>patch({...active,priority:Number(e.target.value)})}/></Field>
    </div></details><Button onClick={()=>setModel({...model,bindings:bindings.filter(b=>b!==active)})}>Remove override</Button></>:<p className="hint">Using existing device and operation defaults.</p>}
  </>}
  {editor==='Release'&&draft.story!=='causal'&&<ReleaseEditor apps={draft.apps} catalog={caps.release_catalog} curves={model.releases||[]} change={releases=>setModel({...model,releases})}/>}
  {editor==='Experiments'&&draft.story!=='causal'&&<><p className="hint">Device-sticky assignment. Exposure starts at a matching request. Target variants under Performance → Audience.</p>
   {(model.experiments||[]).map((e:Json,i:number)=>{const change=(v:Json)=>setModel({...model,experiments:model.experiments.map((x:Json,j:number)=>i===j?{...x,...v}:x)});return <div className="experiment-editor" key={i}><div className="field-grid"><Field label="Experiment ID"><input value={e.id} onChange={event=>change({id:event.target.value})}/></Field><Field label="Enrollment (%)"><input type="number" min="0" max="100" value={(e.enrollment??1)*100} onChange={event=>change({enrollment:Number(event.target.value)/100})}/></Field><Field label="Exposure endpoints" hint="For example get_product or add_to_cart."><input value={e.exposure_endpoints.join(', ')} onChange={event=>change({exposure_endpoints:event.target.value.split(',').map(v=>v.trim()).filter(Boolean)})}/></Field><Field label="Assignment version"><input value={e.version||'1'} onChange={event=>change({version:event.target.value})}/></Field></div><Weights label={e.id+' variant weights'} values={e.variants} change={v=>change({variants:v})}/><Button onClick={()=>setModel({...model,experiments:model.experiments.filter((_:Json,j:number)=>j!==i)})}>Remove experiment</Button></div>})}
   <Button onClick={()=>setModel({...model,experiments:[...(model.experiments||[]),{id:'feature-'+((model.experiments||[]).length+1),version:'1',enrollment:1,variants:{control:50,treatment:50},exposure_endpoints:['get_product']}]})}>Add experiment</Button>
  </>}
  {editor==='Faults'&&draft.story!=='causal'&&<><p className="hint">Share of eligible visits. Network failures default to 15%.</p><div className="signal-list">{signalKeys.map(key=><div className="signal-row" key={key}><label><input type="checkbox" checked={draft.signals[key]&&available(key,draft)} disabled={!available(key,draft)} onChange={e=>update({signals:{...draft.signals,[key]:e.target.checked}})}/><strong>{signalNames[key]}</strong></label>{draft.signals[key]&&key!==signal&&<input aria-label={signalNames[key]+' affected percentage'} type="number" min="1" max="100" value={draft.rates[key]} onChange={e=>update({rates:{...draft.rates,[key]:Number(e.target.value)}})}/>}</div>)}</div>{draft.signals.pages&&<Field label="Slow page LCP p75 (ms)"><input type="number" min="2500" max="30000" value={draft.lcp} onChange={e=>update({lcp:Number(e.target.value)})}/></Field>}</>}
  {draft.story==='causal'&&<div className="field-grid">

                    <Field label="Cohort"><select value={draft.causalVariant} onChange={e => update({causalVariant:e.target.value as Draft["causalVariant"]})}>
                      <option value="affected">Affected</option><option value="control">Healthy control</option><option value="recovered">Recovered</option><option value="scheduled">Scheduled incident</option>
                    </select></Field>
                    <Field label="Traffic mix"><select value={draft.causalMode} onChange={e=>update({causalMode:e.target.value as Draft["causalMode"]})}>
                      <option value="single">Selected story only</option><option value="mixed">Mixed traffic</option>
                    </select></Field>
                    <Field label="Cache"><select value={draft.causalCache} onChange={e=>update({causalCache:e.target.value as Draft["causalCache"]})}>
                      <option value="cold">Cold account cache</option><option value="warm">Warm account cache</option>
                    </select></Field>
                    <Field label="Network"><select value={draft.causalNetwork} onChange={e=>update({causalNetwork:e.target.value as Draft["causalNetwork"]})}>
                      <option value="wifi">Wi-Fi</option><option value="cellular">Cellular</option><option value="constrained">Constrained</option>
                    </select></Field>
                    <Field label="Seed"><input type="number" value={draft.causalSeed} onChange={e=>update({causalSeed:Number(e.target.value)})}/></Field>
                    {draft.causalScenario === "contract-regression" && <Field label="Client failure"><select value={draft.causalFailure} onChange={e=>update({causalFailure:e.target.value as Draft["causalFailure"]})}>
                      <option value="handled">Handled decoding error</option><option value="fatal">Unsafe native mapper · fatal</option>
                    </select></Field>}
                    {draft.causalScenario === "connectivity" && <Field label="Failure boundary"><select value={draft.causalConnectivity} onChange={e=>update({causalConnectivity:e.target.value as Draft["causalConnectivity"]})}>
                      <option value="pre_send">Before reaching server</option><option value="post_send">After server response</option>
                    </select></Field>}
<div className="field-grid">
                  <Field label="Backend region"><select value={draft.causalRegion} onChange={e=>update({causalRegion:e.target.value})}>
                    <option>us-east-1</option><option>eu-west-1</option><option>ap-southeast-2</option>
                  </select></Field>
                  <Field label="Affected service release"><input value={draft.causalRelease} onChange={e=>update({causalRelease:e.target.value})}/></Field>
                  <Field label="Timing"><select value={String(draft.causalReference)} onChange={e=>update({causalReference:e.target.value==="true"})}>
                    <option value="false">Seeded variation</option><option value="true">Exact reference timings</option>
                  </select></Field>
                  {draft.apps.some(a=>a.platform==="web") && <Field label="Optional web provider"><select value={draft.causalProvider} onChange={e=>update({causalProvider:e.target.value})}>
                    <option value="none">None</option><option value="analytics">Consented analytics</option><option value="support">Opened support widget</option>
                  </select></Field>}
                  {draft.causalScenario === "frontend-regression" && draft.apps.every(a=>a.platform==="web") && <Field label="Frontend variant"><select value={String(draft.causalWidget)} onChange={e=>update({causalWidget:e.target.value==="true"})}>
                    <option value="false">View-state failure</option><option value="true">Blocking support interaction</option>
                  </select></Field>}
                  {draft.causalVariant === "scheduled" && <>
                    <Field label="Incident starts"><input type="datetime-local" value={draft.causalStart} onChange={e=>update({causalStart:e.target.value})}/></Field>
                    <Field label="Incident minutes"><input type="number" min="1" max="120" value={draft.causalMinutes} onChange={e=>update({causalMinutes:Number(e.target.value)})}/></Field>
                    <Field label="Affected cohort %"><input type="number" min="0" max="100" value={draft.causalShare} onChange={e=>update({causalShare:Number(e.target.value)})}/></Field>
                  </>}
                  {draft.causalMode === "mixed" && ["Healthy","Selected story","Slow dependency","Recovered"].map((name,i)=><Field key={name} label={name+" weight"}>
                    <input type="number" min="0" max="100" value={draft.causalMix[i]} onChange={e=>update({causalMix:draft.causalMix.map((n,j)=>i===j?Number(e.target.value):n)})}/>
                  </Field>)}
</div>  </div>}
  <div className="configuration-actions">
   <Button onClick={()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({format:'vantra-workflow',version:1,draft},null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='vantra-configuration.json';a.click();URL.revokeObjectURL(url);}}>Save configuration</Button>
   <Button onClick={()=>file.current?.click()}>Open configuration</Button><input ref={file} hidden type="file" accept="application/json,.json" onChange={async e=>{const input=e.target;try{const f=input.files?.[0];if(!f)return;if(f.size>1048576)throw new Error('Configuration exceeds 1 MiB.');const value=JSON.parse(await f.text());if(value.format!=='vantra-workflow'||value.version!==1||!value.draft)throw new Error('Choose an exported Vantra configuration.');const d=restoreDraft(value.draft);const checked=await api('/specs/validate',scenario(caps,d));if(!checked.valid)throw new Error(checked.issues.map((x:Json)=>x.message).join(' '));update(d);setMessage('Configuration opened.');}catch(err){setMessage((err as Error).message);}finally{input.value='';}}}/>{message&&<p role="status" className="hint">{message}</p>}
  </div>
 </details>
 </>;
}
