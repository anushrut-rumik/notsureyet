// Actual non-broadcast CRE checks. No deployment, signing or transaction submission.
import {spawnSync} from 'node:child_process';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {homedir} from 'node:os';
const cli=existsSync(resolve(homedir(),'.cre/bin/cre'))?resolve(homedir(),'.cre/bin/cre'):'cre';
for(const [name,target,extra] of [
 ['native-startup','local-simulation',[]],
 ['native-report-preview','staging-settings',['--config','config.report-preview.json']],
]){
 const args=['workflow','simulate','part3','--target',target,...extra,'--non-interactive','--trigger-index','0','--http-payload',name==='native-report-preview'?resolve('tests/helpers/part3-report-vector.json'):'{}'];
 const run=spawnSync(cli,args,{cwd:resolve('cre-lending'),encoding:'utf8',timeout:180000,
  env:{...process.env,PATH:resolve('node_modules/.bin')+':'+process.env.PATH},maxBuffer:4*1024*1024});
 mkdirSync('docs/evidence/part3',{recursive:true});
 writeFileSync('docs/evidence/part3/'+name+'.json',JSON.stringify({command:['cre',...args],cwd:'cre-lending',checkedAt:new Date().toISOString(),exitCode:run.status,
  kind:name==='native-startup'?'Native WASM startup only; no contracts read':'Native report metadata preview only; no report submitted',
  broadcast:false,sourceTransactionSent:false,output:(run.stdout??'')+(run.stderr??'')},null,2)+'\n');
 console.log(name+': '+(run.status===0?'PASS':'FAIL'));
 if(run.status!==0){console.error(run.stdout,run.stderr);process.exitCode=1;break;}
}
