// This process receives only data, never evaluates a user program, and exits after one check.
import fs from 'node:fs';
import {RE2JS} from 're2js';

const whitespace=String.raw`\t\n\v\f\r\x{2028}\x{2029}\x{feff}\p{Zs}`;
function nativePattern(pattern){
  let converted='',inside=false;
  for(let at=0;at<pattern.length;at++){
    const char=pattern[at];
    if(char!=='\\'){
      if(char==='[')inside=true;
      if(char===']')inside=false;
      converted+=char;continue;
    }
    const escape=pattern[++at];
    if(escape==='u'&&/^[a-f0-9]{4}$/i.test(pattern.slice(at+1,at+5))){
      converted+='\\x{'+pattern.slice(at+1,at+5)+'}';at+=4;
    }else if(escape==='s')converted+=inside?whitespace:'['+whitespace+']';
    else if(escape==='S'&&!inside)converted+='[^'+whitespace+']';
    else converted+='\\'+(escape??'');
  }
  return converted;
}
function check(request){
  if(Number(process.versions.node.split('.')[0])<20)throw Error('Guardrails requires Node 20 or later.');
  if(typeof request.input!=='string'||request.input.length>16000)throw Error('Invalid bounded input.');
  if(request.validationType==='json'){
    try{JSON.parse(request.input);return {passed:true};}
    catch(error){return {passed:false,error:'Invalid JSON: '+error.message};}
  }
  if(request.validationType!=='regex'||typeof request.regex!=='string'||request.regex.length>4096)throw Error('Invalid validation configuration.');
  if(!request.regex)return {passed:false,error:'Regex pattern is required.'};
  try{new RegExp(request.regex);}
  catch(error){return {passed:false,error:'Invalid regex pattern: '+error.message};}
  let compiled;
  try{compiled=RE2JS.compile(nativePattern(request.regex));}
  catch{return {passed:false,error:'Regex syntax is unsupported by RE2. Lookaround and backreferences are not supported.'};}
  return compiled.matcher(request.input).find()?{passed:true}:{passed:false,error:'Input does not match regex pattern'};
}
try{
  // The parent writes at most a bounded 64 KiB request and enforces a three-second deadline.
  const request=JSON.parse(fs.readFileSync(0,'utf8'));
  const result=check(request);
  process.stdout.write(JSON.stringify({...result,engine:request.validationType==='json'?'JSON.parse':'RE2JS 2.9.1',node:process.versions.node}));
}catch(error){
  process.stdout.write(JSON.stringify({failure:error.message}));process.exitCode=1;
}
