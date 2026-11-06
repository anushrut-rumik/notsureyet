// Reproducible focused verification; every result is labelled as local/mocked evidence.
import {spawnSync} from 'node:child_process';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
const checks=[
 ['typescript','npm',['run','lending:build']],
 ['journal-and-receipt-fault-injection',process.execPath,['--test','tests/lending-part3.test.mjs']],
 ['contract-fault-injection',process.execPath,['scripts/foundry.mjs','forge','test','--evm-version','cancun','--match-path','contracts/test/lending/part3/*.sol','-vv']],
 ['cre-sdk-fault-injection',resolve('node_modules/.bin/bun'),['test','tests/lending-part3-cre.test.ts']],
];
const directory='docs/evidence/part3';mkdirSync(directory,{recursive:true});const results=[];
for(const [name,command,args] of checks){
 const result=spawnSync(command,args,{encoding:'utf8',timeout:180000,maxBuffer:8*1024*1024});
 const output=(result.stdout??'')+(result.stderr??'');
 writeFileSync(directory+'/'+name+'.txt',output.trimEnd()+'\n');console.log(name+': '+(result.status===0?'PASS':'FAIL'));
 results.push({name,command:[command,...args],exitCode:result.status,kind:'local automated verification; not live CCIP delivery'});
 if(result.status!==0){console.error(output);break;}
}
writeFileSync(directory+'/test-results.json',JSON.stringify({checkedAt:new Date().toISOString(),results,
 liveAcceptance:{completedCrossChainDeposit:false,livePolicyRejection:false},
 limitations:['Fault-injection router is mocked; no live CCIP message created','Native CRE startup simulation is recorded separately']},null,2)+'\n');
if(results.length!==checks.length||results.some(r=>r.exitCode!==0))process.exitCode=1;
