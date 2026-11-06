// Actual CRE CLI verification with ARITHMETIC fixtures. No onchain reads or writes.
import {mkdir,writeFile} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
import {homedir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {exampleRun,FIXTURE_NOW} from '../.local/lending-build/fixtures.js';
import {createRunInput} from '../.local/lending-build/schema.js';
import {evaluateRun} from '../.local/lending-build/run.js';
import {readLoggedResult} from '../.local/lending-build/cre-evidence.js';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const cli=process.env.CRE_CLI_PATH||(existsSync(resolve(homedir(),'.cre/bin/cre'))?resolve(homedir(),'.cre/bin/cre'):'cre');
const save=(path,value)=>writeFile(path,JSON.stringify(value,null,2)+'\n',{flag:'wx'});
async function execute(args){
 return new Promise((resolveResult,reject)=>{
  const child=spawn(cli,args,{cwd:resolve(root,'cre-lending'),env:{...process.env,PATH:resolve(root,'node_modules/.bin')+':'+process.env.PATH},stdio:['ignore','pipe','pipe']});
  let output='';child.stdout.on('data',data=>{output+=data;process.stdout.write(data);});child.stderr.on('data',data=>{output+=data;process.stderr.write(data);});
  child.on('error',reject);child.on('close',exitCode=>resolveResult({exitCode,output}));
 });
}
try{
 const directory=process.argv[2];if(!directory)throw Error('Usage: lending-cre-arithmetic.mjs new-evidence-directory');
 const out=resolve(directory);await mkdir(out,{recursive:false});
 for(const [days,expected] of [[60,'move'],[30,'hold']]){
  const input=JSON.parse(JSON.stringify(exampleRun(days))),now=BigInt(Math.floor(Date.now()/1000)),delta=now-BigInt(FIXTURE_NOW);
  function rebase(value){if(!value||typeof value!=='object')return;for(const [key,v] of Object.entries(value)){
   if(['observedAt','assembledAt','approvedAt','deadline'].includes(key))value[key]=String(BigInt(v)+delta);else rebase(v);
  }}rebase(input);
  input.runId=`cre-arithmetic-${expected}-${now}`;input.observation.snapshotId=input.snapshot.id=input.runId+'-snapshot';
  input.observation.reference=input.runId;input.operationId=input.runId+'-operation';
  const admitted=createRunInput(input),inputPath=resolve(out,`${expected}-input.json`);await save(inputPath,admitted);
  const args=['workflow','simulate','arithmetic-check','--target','local-simulation','--non-interactive','--trigger-index','0','--http-payload',inputPath];
  const {exitCode,output}=await execute(args);
  let actual=null,local=null,error=null;
  try{
   actual=readLoggedResult(output);
   local=evaluateRun(admitted,()=>actual.result.decision.evaluatedAt);
   if(exitCode!==0 || actual.scope!=='arithmetic-only' || actual.deployedContractsRead!==false || actual.broadcast!==false ||
     actual.result.decision.action!==expected || JSON.stringify(actual.result)!==JSON.stringify(local))throw Error('Native CRE/local arithmetic mismatch');
  }catch(e){error=e.message;}
  await save(resolve(out,`${expected}-evidence.json`),{scope:'arithmetic-only',command:'cre '+args.join(' '),cwd:resolve(root,'cre-lending'),
   exitCode,deployedContractsRead:false,broadcast:false,expected,localMatchesCRE:error===null,actual,local,error,output});
  if(error)throw Error(error);
 }
 console.log('Both arithmetic decisions ran in the actual CRE simulator and matched local evaluation. Deployed-contract acceptance is still separate.');
}catch(e){console.error(e.message);process.exitCode=1;}
