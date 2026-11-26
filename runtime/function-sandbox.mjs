import fs from 'node:fs';
import variant from '@jitl/quickjs-singlefile-cjs-release-sync';
import {newQuickJSWASMModuleFromVariant} from 'quickjs-emscripten-core';

// Only this host wrapper sees Node. No host function, filesystem, network or module loader enters QuickJS.
async function execute(input){
  if(input.language!=='javascript'||typeof input.code!=='string'||input.code.length>16000)throw Error('Unsupported bounded Function code.');
  if(!Number.isInteger(input.timeoutMs)||input.timeoutMs<50||input.timeoutMs>10000)throw Error('Invalid execution deadline.');
  if(![16,32,64].includes(input.memoryMiB))throw Error('Invalid memory limit.');
  const QuickJS=await newQuickJSWASMModuleFromVariant(variant);
  const runtime=QuickJS.newRuntime();
  runtime.setMemoryLimit(input.memoryMiB*1024*1024);
  runtime.setMaxStackSize(512*1024);
  const deadline=Date.now()+input.timeoutMs;
  runtime.setInterruptHandler(()=>Date.now()>=deadline);
  const vm=runtime.newContext();
  let promise;
  try{
    const code='(async()=>{\n'+
      'const stringify=JSON.stringify.bind(JSON), finite=Number.isFinite, logs=[]; let logSize=0;\n'+
      'const log=(...items)=>{const line=items.map(v=>typeof v==="string"?v:stringify(v)).join(" ");logSize+=line.length+1;if(logSize>8192)throw Error("Console output exceeds 8,192 characters.");logs.push(line);};\n'+
      'globalThis.console={log,info:log,warn:log,error:log,debug:log};\n'+
      'const result=await (async function(params){"use strict";\n'+input.code+'\n})(JSON.parse('+JSON.stringify(JSON.stringify(input.params))+'));\n'+
      'return stringify({result:result===undefined?null:result,stdout:logs.join("\\n"),files:[]},(key,value)=>{if(typeof value==="number"&&!finite(value))throw Error("Return finite JSON values.");return value;});\n'+
      '})()';
    const evaluated=vm.evalCode(code,'function.js');
    if(evaluated.error){const error=vm.dump(evaluated.error);evaluated.error.dispose();throw Error(error.message||'Function compilation failed.');}
    promise=evaluated.value;
    for(;;){
      const state=vm.getPromiseState(promise);
      if(state.type==='fulfilled'){
        const raw=vm.getString(state.value);state.value.dispose();
        if(Buffer.byteLength(raw,'utf8')>32768)throw Error('Function output exceeds 32 KiB.');
        return {...JSON.parse(raw),engine:'QuickJS WebAssembly 0.32.0',memoryMiB:input.memoryMiB,timeoutMs:input.timeoutMs};
      }
      if(state.type==='rejected'){
        const error=vm.dump(state.error);state.error.dispose();throw Error(error?.message||'Function rejected its result.');
      }
      if(Date.now()>=deadline)throw Error('Function execution deadline reached.');
      if(!runtime.hasPendingJob())throw Error('Function left an unresolved promise. External asynchronous services are unavailable.');
      const jobs=runtime.executePendingJobs(1);
      if(jobs.error){const error=vm.dump(jobs.error);jobs.error.dispose();throw Error(error?.message||'Function job failed.');}
    }
  }finally{
    promise?.dispose();vm.dispose();runtime.dispose();
  }
}
async function main(){
  try{
    const input=JSON.parse(fs.readFileSync(0,'utf8'));
    process.stdout.write(JSON.stringify({ok:true,...await execute(input)}));
  }catch(error){
    process.stdout.write(JSON.stringify({ok:false,error:String(error?.message||error).slice(0,2000)}));
  }
}
main();
