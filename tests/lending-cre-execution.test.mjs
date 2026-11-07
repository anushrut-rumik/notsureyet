import test from 'node:test';
import assert from 'node:assert/strict';
import {parseExecutionOutput} from '../scripts/part4/cre-execution.mjs';
import {liveFailure} from '../scripts/part4/live.mjs';
const operationId='0x'+'11'.repeat(32),hash='0x'+'22'.repeat(32);
const log=(changes={})=>'[USER LOG] PART3_EXECUTION_RESULT='+JSON.stringify({operationId,submitted:true,sourceTransactionHash:hash,...changes});
test('connection failures retain a safe diagnosis and never expose provider details',()=>{
 const privateMessage='HTTP request failed. URL: https://secret.example/key';
 assert.equal(parseExecutionOutput(privateMessage,1,{operationId}).error.code,'RPC_UNAVAILABLE');
 const failure=liveFailure(Object.assign(Error(privateMessage),{name:'HttpRequestError'}));
 assert.equal(failure.failureCode,'RPC_UNAVAILABLE');
 assert.ok(!JSON.stringify(failure).includes('secret.example'));
});
test('native CRE quota failures retain a specific diagnosis without exposing raw output',()=>{
 const result=parseExecutionOutput('rpc=https://secret.example/key capability call limit exceeded for evm.CallContract: PerWorkflow.ChainRead.CallLimit',1,{operationId,broadcast:true});
 assert.equal(result.ok,false);assert.equal(result.error.code,'CRE_READ_LIMIT');
 assert.ok(!JSON.stringify(result).includes('secret.example'));assert.ok(!result.sourceTransactionHash);
});
test('a preflight simulation hash is never recorded as an on-chain submission',()=>{
 const output='PART3_EXECUTION_RESULT='+JSON.stringify({preflight:true,validated:true,operationId});
 const result=parseExecutionOutput(output,0,{operationId});assert.equal(result.ok,true);assert.ok(!result.sourceTransactionHash);
 assert.equal(parseExecutionOutput(log(),0,{operationId}).ok,false);
});
test('broadcast result keeps its hash for reconciliation even if the CLI later exits unsuccessfully',()=>{
 for(const code of [0,1]){
  const result=parseExecutionOutput(log(),code,{operationId,broadcast:true});
  assert.equal(result.sourceTransactionHash,hash);assert.equal(result.ok,code===0);
 }
});
test('missing, ambiguous, mismatched and zero hashes cannot prove a submission',()=>{
 for(const output of ['',log()+'\n'+log(),log({operationId:hash}),log({sourceTransactionHash:'0x'+'00'.repeat(32)})]){
  const result=parseExecutionOutput(output,0,{operationId,broadcast:true});assert.equal(result.ok,false);assert.ok(!result.sourceTransactionHash);
 }
});
test('only the early missing-signer rejection proves no broadcast was possible',()=>{
 const missing='Initializing...\nLoading settings...\na private key is required for --broadcast mode.';
 assert.equal(parseExecutionOutput(missing,1,{operationId,broadcast:true}).notSubmitted,true);
 for(const text of ['timeout',missing+'\n[SIMULATION] Running workflow',missing+'\n'+log()])
  assert.ok(!parseExecutionOutput(text,1,{operationId,broadcast:true}).notSubmitted);
});
