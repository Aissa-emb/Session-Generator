import {readFileSync, writeFileSync} from 'node:fs';
import {jsonSchemaToZod} from 'json-schema-to-zod';
const source = JSON.parse(readFileSync(new URL('../../../spec/scenario.schema.json', import.meta.url)));
function resolve(node) {
  if (Array.isArray(node)) return node.map(resolve);
  if (!node || typeof node !== 'object') return node;
  if (node.$ref) {
    if (!node.$ref.startsWith('#/')) throw new Error('Only local schema references are allowed');
    return resolve(node.$ref.slice(2).split('/').reduce((n,k)=>n[k],source));
  }
  return Object.fromEntries(Object.entries(node).filter(([k])=>k!=='$defs').map(([k,v])=>[k,resolve(v)]));
}
const output = '// Generated from spec/scenario.schema.json; run npm run schema.\nimport {z} from "zod";\nexport const ScenarioSpecSchema = ' + jsonSchemaToZod(resolve(source)) + ';\n';
const path = new URL('../src/schema.ts', import.meta.url);
if (process.argv.includes('--check')) {
  if (readFileSync(path,'utf8')!==output) throw new Error('Generated Zod schema drift; run npm run schema.');
} else writeFileSync(path,output);
