// Remote Streamable HTTP only. The parent owns every network operation.
import readline from 'node:readline';
import crypto from 'node:crypto';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import Ajv from 'ajv';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const lines=readline.createInterface({input:process.stdin}),queue=[],waiters=[];
lines.on('line',line=>waiters.length?waiters.shift()(line):queue.push(line));
const next=()=>queue.length?Promise.resolve(queue.shift()):new Promise(resolve=>waiters.push(resolve));
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
globalThis.fetch=()=>{throw new Error('Ambient MCP network transport is unavailable.');};
const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
const digest=v=>crypto.createHash('sha256').update(JSON.stringify(canonical(v))).digest('hex');
const validators={getValidator(schema){
 const type=schema.$schema;
 if(type&&!['http://json-schema.org/draft-07/schema#','http://json-schema.org/draft-07/schema','https://json-schema.org/draft/2020-12/schema'].includes(type))throw new Error('Unsupported JSON Schema dialect.');
 const ajv=new (type?.includes('2020-12')?Ajv2020:Ajv)({strict:false,validateSchema:true,allErrors:false,validateFormats:true,addUsedSchema:false});
 addFormats(ajv);const check=ajv.compile(schema);
 return input=>({valid:!!check(input),errorMessage:ajv.errorsText(check.errors)});
}};
let phase='connect',attempted=false;
const fetchImpl=async(input,init={})=>{
 const url=input instanceof Request?input.url:String(input),method=init.method??'GET';
 // This standalone client consumes POST response streams, not background subscriptions.
 if(method==='GET')return new Response(null,{status:405});
 const headers=Object.fromEntries(new Headers(init.headers??{})),body=init.body??null;
 if(body!==null&&typeof body!=='string')throw new Error('Unsupported MCP HTTP body.');
 if(method==='POST'&&JSON.parse(body).method==='tools/call')attempted=true;
 emit({kind:'http',phase,url,method,headers,body});const response=JSON.parse(await next());
 if(response.error)throw new Error(response.error);
 if(response.status>=300&&response.status<400)throw new Error('MCP redirects are unavailable; register the final endpoint.');
 return new Response([204,205,304].includes(response.status)?null:Buffer.from(response.body,'base64'),{status:response.status,headers:response.headers});
};
async function main(){
 const timer=setTimeout(()=>process.exit(2),365000);let client;
 try{
  const p=JSON.parse(await next());client=new Client({name:'Penstock',version:'1.0'},{capabilities:{},jsonSchemaValidator:validators});
  client.onerror=()=>{};
  const transport=new StreamableHTTPClientTransport(new URL(p.url),{fetch:fetchImpl,reconnectionOptions:{maxRetries:0,initialReconnectionDelay:0,maxReconnectionDelay:0,reconnectionDelayGrowFactor:1}});
  await client.connect(transport,{timeout:30000});
  phase='discovery';const began=Date.now(),all=[],names=new Set(),cursors=new Set();let cursor,bytes=0,pages=0;
  do{
   if(pages>=50||Date.now()-began>=60000)throw new Error('MCP discovery did not complete within 50 pages / 60 seconds.');
   const page=await client.listTools(cursor?{cursor}:{},{timeout:Math.max(1,60000-(Date.now()-began))});pages++;
   bytes+=Buffer.byteLength(JSON.stringify(page.tools));
   if(bytes>5*1024*1024||all.length+page.tools.length>1000)throw new Error('MCP discovery exceeded 1,000 operations / 5 MiB.');
   for(const t of page.tools){
    if(names.has(t.name))throw new Error('MCP discovery returned a duplicate exact operation name.');names.add(t.name);
    all.push({name:t.name,description:t.description??'',inputSchema:t.inputSchema,...(t.outputSchema?{outputSchema:t.outputSchema}:{}),...(t.execution?{execution:t.execution}:{})});
   }
   cursor=page.nextCursor;
   if(cursor!==undefined&&typeof cursor!=='string')throw new Error('Invalid provider cursor.');
   if(cursor){if(cursors.has(cursor))throw new Error('MCP discovery repeated a provider cursor.');cursors.add(cursor);}
  }while(cursor);
  all.sort((a,b)=>a.name<b.name?-1:a.name>b.name?1:0);
  const inventory={pages,count:all.length,bytes,complete:true,sha256:digest(all),elapsedMs:Date.now()-began};
  let values;
  if(p.operation==='discover')values={operations:all};
  else if(p.operation==='list'){
   const search=(p.search??'').toLowerCase(),found=all.filter(t=>(t.name+' '+t.description).toLowerCase().includes(search));
   const remaining=found.filter(t=>!p.cursor||t.name>p.cursor),selected=remaining.slice(0,p.limit??100),more=remaining.length>selected.length;
   values={serverId:p.server,operations:selected.map(({execution,...t})=>t),nextCursor:more?selected.at(-1).name:null,hasMore:more};
  }else{
   const tool=all.find(t=>t.name===p.tool);
   if(!tool)throw new Error('Exact operation is unavailable on the selected MCP server.');
   if(tool.execution?.taskSupport==='required')throw new Error('This operation requires asynchronous MCP tasks; the installed standalone route does not support them.');
   const schema={inputSchema:tool.inputSchema,...(tool.outputSchema?{outputSchema:tool.outputSchema}:{})};
   if(p.toolSchema&&digest(p.toolSchema)!==digest(schema))throw new Error('The operation schema changed. Refresh the editor and review a new workflow version.');
   const args=p.arguments??{},checked=validators.getValidator(tool.inputSchema)(args);
   if(!checked.valid)throw new Error('Operation arguments do not match its discovered schema: '+checked.errorMessage);
   phase='operation';const response=await client.callTool({name:p.tool,arguments:args},undefined,{timeout:300000});
   if(tool.outputSchema&&!response.isError){
    if(!Object.hasOwn(response,'structuredContent'))throw new Error('Operation omitted its declared structured content.');
    const output=validators.getValidator(tool.outputSchema)(response.structuredContent);
    if(!output.valid)throw new Error('Operation returned invalid structured content: '+output.errorMessage);
   }
   values={content:response.content,...(Object.hasOwn(response,'structuredContent')?{structuredContent:response.structuredContent}:{})};
   emit({kind:'result',ok:true,values,failed:response.isError===true,inventory,toolSchema:schema,server:client.getServerVersion(),protocol:transport.protocolVersion});return;
  }
  emit({kind:'result',ok:true,values,inventory,server:client.getServerVersion(),protocol:transport.protocolVersion});
 }catch(error){emit({kind:'result',ok:false,phase,attempted,error:String(error.message??error).slice(0,2000)});}
 finally{if(client)await client.close().catch(()=>{});clearTimeout(timer);lines.close();process.stdin.destroy();}
}
main();
