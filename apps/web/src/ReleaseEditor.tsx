import { Json } from './api';
import { AppTarget } from './workflow';
import { Button, Field } from './ui';

export default function ReleaseEditor({apps,catalog,curves,change}:{apps:AppTarget[];catalog:Json;curves:Json[];change:(v:Json[])=>void}) {
 return <><p className="hint">Shares interpolate between points. Native devices keep their build until an upgrade or explicit rollback.</p>
  {apps.map(app=>{
   const entry=catalog[app.platform],curve=curves.find(c=>c.target===entry.target);
   const versions=entry.versions.map((v:Json)=>v.version);
   const update=(v:Json)=>change(curves.map(c=>c===curve?{...c,...v}:c));
   return <div className="release-editor" key={app.platform}><h3>{app.platform==='ios'?'iOS':app.platform==='web'?'Web':'Android'}</h3>
    {!curve?<Button onClick={()=>change([...curves,{target:entry.target,start:'run',points:[{minute:0,shares:{[versions[0]]:10,[versions[1]]:90}},{minute:20,shares:{[versions[0]]:80,[versions[1]]:20}}]}])}>Set adoption curve</Button>:<>
     <div className="field-grid"><Field label={app.platform+' rollout starts'} hint="Leave empty to start when the run starts."><input type="datetime-local" value={curve.start==='run'?'':new Date(new Date(curve.start).getTime()-new Date(curve.start).getTimezoneOffset()*60000).toISOString().slice(0,16)} onChange={e=>update({start:e.target.value?new Date(e.target.value).toISOString():'run'})}/></Field>
      {app.platform!=='web'&&<Field label="Native rollback"><select value={String(curve.allow_rollback||false)} onChange={e=>update({allow_rollback:e.target.value==='true'})}><option value="false">Keep upgraded builds</option><option value="true">Allow build rollback</option></select></Field>}
     </div>
     <div className="table-scroll"><table><caption>Requested shares · each row must total 100%</caption><thead><tr><th>Minute</th>{versions.map((v:string)=><th key={v}>{v}</th>)}<th/></tr></thead><tbody>{curve.points.map((p:Json,i:number)=><tr key={i}>
      <td><input aria-label={`${app.platform} point ${i+1} minute`} type="number" min="0" max="10080" value={p.minute} onChange={e=>update({points:curve.points.map((v:Json,j:number)=>j===i?{...v,minute:Number(e.target.value)}:v)})}/></td>
      {versions.map((v:string)=><td key={v}><input aria-label={`${app.platform} point ${i+1} version ${v} share`} type="number" min="0" max="100" value={p.shares[v]||0} onChange={e=>update({points:curve.points.map((x:Json,j:number)=>j===i?{...x,shares:{...x.shares,[v]:Number(e.target.value)}}:x)})}/></td>)}
      <td><Button disabled={curve.points.length<=2} aria-label={`Remove ${app.platform} point ${i+1}`} onClick={()=>update({points:curve.points.filter((_:Json,j:number)=>j!==i)})}>Remove</Button></td>
     </tr>)}</tbody></table></div>
     <div className="configuration-actions"><Button disabled={curve.points.length>=20} onClick={()=>{const last=curve.points.at(-1);update({points:[...curve.points,{minute:last.minute+20,shares:{...last.shares}}]});}}>Add point</Button><Button onClick={()=>change(curves.filter(c=>c!==curve))}>Use fixed release mix</Button></div>
    </>}
   </div>;
  })}
 </>;
}
