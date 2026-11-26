// Pinned A2A SDK. All HTTP crosses the parent's bounded transport for configured endpoints.
import readline from 'node:readline';
import {AGENT_CARD_PATH,Role,taskStateToJSON} from '@a2a-js/sdk';
import {ClientFactory,DefaultAgentCardResolver,JsonRpcTransportFactory,RestTransportFactory} from '@a2a-js/sdk/client';

const lines=readline.createInterface({input:process.stdin});
const queue=[],waiters=[];
lines.on('line',line=>waiters.length?waiters.shift()(line):queue.push(line));
const next=()=>queue.length?Promise.resolve(queue.shift()):new Promise(resolve=>waiters.push(resolve));
// Avoid any ambient global network transport, including SDK fallback paths.
globalThis.fetch=()=>{throw new Error('Ambient network transport is unavailable.');};
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
let phase='discovery';
const fetchImpl=async(input,init={})=>{
 const url=input instanceof Request?input.url:String(input);
 const method=init.method??(input instanceof Request?input.method:'GET');
 const headers=Object.fromEntries(new Headers(init.headers??(input instanceof Request?input.headers:{})));
 const body=init.body??(input instanceof Request?await input.text():null);
 if(body!==null&&typeof body!=='string')throw new Error('Unsupported A2A request body.');
 emit({kind:'http',phase,url,method,headers,body});
 const reply=JSON.parse(await next());
 if(reply.error)throw new Error(reply.error);
 return new Response([204,205,304].includes(reply.status)?null:Buffer.from(reply.body,'base64'),{status:reply.status,headers:reply.headers});
};
const text=message=>(message?.parts??[]).filter(p=>p.content?.$case==='text').map(p=>p.content.value).filter(Boolean).join('\n');
const state=task=>taskStateToJSON(task.status?.state??0).replace(/^TASK_STATE_/,'').toLowerCase().replaceAll('_','-');
const taskValues=t=>({content:text(t.history.filter(m=>m.role===Role.ROLE_AGENT).at(-1)??t.status?.message),taskId:t.id,contextId:t.contextId,state:state(t),artifacts:t.artifacts.map(a=>({name:a.name,description:a.description,content:text(a)}))});
async function main(){
const timer=setTimeout(()=>process.exit(2),335000);
try{
 const p=JSON.parse(await next()),resolver=new DefaultAgentCardResolver({fetchImpl});
 let card,last;
 for(const path of [AGENT_CARD_PATH,'/.well-known/agent.json','']){
  try{card=await resolver.resolve(p.agentUrl,path);break;}catch(e){last=e;}
 }
 if(!card)throw last??new Error('No agent card found.');
 const factory=new ClientFactory({transports:[new JsonRpcTransportFactory({fetchImpl}),new RestTransportFactory({fetchImpl})],cardResolver:resolver});
 const client=await factory.createFromAgentCard(card);
 let values;
 if(p.operation==='a2a_get_agent_card'){
  const i=card.supportedInterfaces.at(0);
  values={name:card.name,description:card.description,url:i?.url??p.agentUrl,version:card.version,protocolVersion:i?.protocolVersion??'',capabilities:{streaming:card.capabilities?.streaming??false,pushNotifications:card.capabilities?.pushNotifications??false,extendedAgentCard:card.capabilities?.extendedAgentCard??false},skills:card.skills.map(s=>({id:s.id,name:s.name,description:s.description})),defaultInputModes:card.defaultInputModes,defaultOutputModes:card.defaultOutputModes};
 }else{
  phase='operation';
  if(p.operation==='a2a_send_message'){
   const parts=[{content:{$case:'text',value:p.message},filename:'',mediaType:''}];
   if(Object.hasOwn(p,'data'))parts.push({content:{$case:'data',value:p.data},filename:'',mediaType:''});
   const result=await client.sendMessage({tenant:'',message:{messageId:p.messageId,contextId:p.contextId??'',taskId:p.taskId??'',role:Role.ROLE_USER,parts,extensions:[],referenceTaskIds:[]}});
   values='status' in result?taskValues(result):{content:text(result),taskId:result.taskId,contextId:result.contextId,state:'completed',artifacts:[]};
  }else if(p.operation==='a2a_get_task')values=taskValues(await client.getTask({tenant:'',id:p.taskId,historyLength:p.historyLength}));
  else if(p.operation==='a2a_cancel_task'){
   const t=await client.cancelTask({tenant:'',id:p.taskId});values={taskId:t.id,state:state(t),canceled:state(t)==='canceled'};
  }else throw new Error('Unknown A2A operation.');
 }
 emit({kind:'result',ok:true,values,failed:p.operation==='a2a_send_message'&&['failed','rejected'].includes(values.state),agent:{name:card.name,version:card.version,interfaces:card.supportedInterfaces.map(i=>({url:i.url,protocol:i.protocolBinding,version:i.protocolVersion}))}});
}catch(e){emit({kind:'result',ok:false,phase,error:String(e.message??e).slice(0,2000)});}
finally{clearTimeout(timer);lines.close();process.stdin.destroy();}
}
main();
