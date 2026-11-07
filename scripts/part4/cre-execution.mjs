import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {homedir} from 'node:os';
import {resolve} from 'node:path';
import {writeFile} from 'node:fs/promises';

// Return fixed diagnostic text: CLI output can contain RPC URLs or other private configuration.
export function executionFailure(output,reason=null){
 const cases=[
  [/a private key is required for --broadcast mode/,'CRE_SIGNER_UNAVAILABLE','CRE stopped during initialization because the approved signing account was unavailable. No transaction was sent.'],
  [/ChainRead.CallLimit|capability call limit exceeded/,'CRE_READ_LIMIT','CRE exceeded its contract-read limit.'],
  [/Current balance\/rate\/capacity changed|Actual router fee changed|Policy or wallet binding changed/,'CRE_STATE_CHANGED','The pool, balance, policy or fee changed during validation. A fresh observation is required.'],
  [/Signed instruction expired/,'CRE_INSTRUCTION_EXPIRED','The signed instruction expired before submission.'],
  [/already pending or consumed/,'CRE_OPERATION_PENDING','An existing operation must be reconciled first.'],
  [/Submission outcome requires reconciliation|execution reverted/,'CRE_REPORT_FAILED','The report could not be executed. Check the source receipt before retrying.'],
  [/LimitExceeded/,'CRE_RESOURCE_LIMIT','CRE exceeded an execution limit.'],
  [/no route to host|network is unreachable|connection reset|fetch failed|RPC health check failed|HTTP request failed/i,'RPC_UNAVAILABLE','The testnet RPC connection failed. Existing transactions must be reconciled before another send.'],
  [/timeout|timed out|deadline exceeded/i,'CRE_TIMEOUT','CRE execution timed out.'],
  [/401|unauthorized|not logged in|authentication/i,'CRE_AUTHENTICATION','CRE authentication failed.'],
 ];
 const hit=cases.find(([pattern])=>pattern.test(output));
 return hit?{code:hit[1],message:hit[2]}:{code:reason??'CRE_EXECUTION_FAILED',message:'CRE did not return a verified execution result.'};
}

export function parseExecutionOutput(output,exitCode,{operationId,broadcast=false,reason=null}={}){
 const plain=output.replace(/\u001b\[[0-9;]*m/g,'');
 const lines=plain.split(/\r?\n/).flatMap(line=>{
  const match=/PART3_EXECUTION_RESULT=(\{.*\})$/.exec(line);
  if(!match)return [];
  try{return [JSON.parse(match[1])];}catch{return [];}
 });
 const record=lines.length===1?lines[0]:null;
 const validHash=record?.operationId===operationId&&record?.submitted===true&&
  /^0x[\da-fA-F]{64}$/.test(record.sourceTransactionHash??'')&&BigInt(record.sourceTransactionHash)!==0n;
 const valid=broadcast?validHash:record?.operationId===operationId&&record?.preflight===true&&record?.validated===true;
 // A hash from a non-broadcast simulation is never an on-chain transaction.
 const outcome={ok:exitCode===0&&valid,broadcast,exitCode,error:null};
 if(broadcast&&validHash)outcome.sourceTransactionHash=record.sourceTransactionHash;
 if(!outcome.ok)outcome.error=executionFailure(plain,reason);
 // This exact CLI rejection happens in ResolveKey, before clients, workflow
 // execution or a signer exist. Other failures remain uncertain after launch.
 if(broadcast&&exitCode===1&&!record&&outcome.error?.code==='CRE_SIGNER_UNAVAILABLE'&&
    !/\[SIMULATION\]|Workflow compiled|Compiling workflow|PART3_EXECUTION_RESULT/.test(plain))
  outcome.notSubmitted=true;
 return outcome;
}

export async function runExecutionWorkflow(path,{key,operationId,broadcast=false,timeoutMs=120000}={}){
 const cli=existsSync(resolve(homedir(),'.cre/bin/cre'))?resolve(homedir(),'.cre/bin/cre'):'cre';
 const args=['workflow','simulate','part3','--target',broadcast?'staging-settings':'execution-preflight','--non-interactive',
  '--trigger-index','0','--http-payload',path,
  // CRE overloads shell variables from .env and .env.public. The broker owns
  // the exact signer; inherited project files must never replace that signer.
  '--env','/dev/null','--public-env','/dev/null'];
 if(broadcast)args.push('--broadcast');
 return new Promise(done=>{
  let output='',reason=null,finished=false;
  const child=spawn(cli,args,{cwd:resolve('cre-lending'),stdio:['ignore','pipe','pipe'],
   env:{...process.env,...(key?{CRE_ETH_PRIVATE_KEY:key}:{}),PATH:resolve('node_modules/.bin')+':'+process.env.PATH}});
  const finish=async exitCode=>{if(finished)return;finished=true;clearTimeout(timer);
   // Keep the cause inspectable on this host, even when it has no known error
   // classification yet. Never persist the injected wallet key or RPC URL.
   let diagnostic=output;
   if(key)diagnostic=diagnostic.replaceAll(key,'[REDACTED KEY]').replaceAll(key.replace(/^0x/,''),'[REDACTED KEY]');
   diagnostic=diagnostic.replace(/https?:\/\/[^\s"'<>]+/g,'[RPC URL]');
   try{await writeFile(path+(broadcast?'.broadcast.log':'.preflight.log'),diagnostic,{mode:0o600});}catch{/* Preserve the execution result if diagnostic storage fails. */}
   done(parseExecutionOutput(output,exitCode,{operationId,broadcast,reason}));};
  const timer=setTimeout(()=>{reason='CRE_TIMEOUT';child.kill('SIGKILL');finish(null);},timeoutMs);
  const append=chunk=>{output+=chunk.toString();if(output.length>2000000){reason='CRE_OUTPUT_LIMIT';child.kill('SIGKILL');finish(null);}};
  child.stdout.on('data',append);child.stderr.on('data',append);
  child.on('error',()=>{reason='CRE_UNAVAILABLE';finish(null);});
  child.on('close',finish);
 });
}
