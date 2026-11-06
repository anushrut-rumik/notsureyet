// Read-only receipt verification for direct transactions and wallet relays.
// A relay is accepted only when a canonical execution trace proves the exact
// successful CALL from the reviewed owner. A matching balance alone is not proof.
const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();
const check=(ok,message)=>{if(!ok)throw Error(message);};

export function matchesTransaction(action,tx){
 check(same(tx.from,action.from),'Transaction sender differs from the reviewed owner');
 check((tx.to===null&&action.to===null)||same(tx.to,action.to),'Transaction target differs from the reviewed target');
 check(same(tx.input,action.data),'Transaction calldata differs from the reviewed action');
 check(BigInt(tx.value)===0n,'Setup action must not attach ETH');
 if(action.nonce!==undefined)check(Number(tx.nonce)===action.nonce,'Transaction nonce differs from the reviewed action');
}

export async function verifySetupExecution(client,action,hash){
 const tx=await client.getTransaction({hash});
 if(tx.chainId!==undefined)check(String(tx.chainId)===action.chainId,'Transaction chain differs from the reviewed network');
 if(tx.hash!==undefined)check(same(tx.hash,hash),'RPC returned a different transaction');
 if(same(tx.from,action.from)){
  matchesTransaction(action,tx);
  return {kind:'direct',ownerNonceConsumed:1};
 }
 check(action.to!==null&&!action.creates,'Contract deployment must be sent directly by the reviewed owner');
 check(BigInt(tx.value)===0n,'Relayed setup must not attach ETH');
 const receipt=await client.getTransactionReceipt({hash});
 check(receipt.status==='success','Relayed transaction did not succeed');
 const block=await client.getBlock({blockNumber:receipt.blockNumber});
 check(same(block.hash,receipt.blockHash),'Relayed receipt is not on the canonical chain');
 check(same(tx.blockHash,receipt.blockHash)&&tx.blockNumber===receipt.blockNumber,'Relayed transaction and receipt disagree');
 const traces=await client.request({method:'trace_transaction',params:[hash]});
 check(Array.isArray(traces)&&traces.length>0,'RPC did not provide an execution trace; keep the hash and do not resend');
 const paths=new Set();
 for(const t of traces){
  check(same(t.transactionHash,hash)&&same(t.blockHash,receipt.blockHash)&&BigInt(t.blockNumber)===receipt.blockNumber,'Trace belongs to a different transaction or block');
  check(Array.isArray(t.traceAddress)&&t.traceAddress.every(n=>Number.isSafeInteger(n)&&n>=0),'Invalid trace path');
  const path=JSON.stringify(t.traceAddress);check(!paths.has(path),'Duplicate trace path');paths.add(path);
 }
 const ownerCalls=traces.filter(t=>t.type==='call'&&same(t.action?.from,action.from)&&t.action?.callType!=='staticcall');
 check(ownerCalls.length===1,'Relay must contain exactly one reviewed call from the owner');
 const call=ownerCalls[0];
 check(call.action.callType==='call'&&same(call.action.to,action.to)&&same(call.action.input,action.data)&&BigInt(call.action.value)===0n,'Relayed owner call differs from the reviewed action');
 for(let depth=0;depth<=call.traceAddress.length;depth++){
  const path=JSON.stringify(call.traceAddress.slice(0,depth));
  const ancestor=traces.find(t=>JSON.stringify(t.traceAddress)===path);
  check(ancestor&&!ancestor.error&&ancestor.result,'Relayed call or an enclosing execution reverted');
 }
 // Only support relays that leave the native owner nonce untouched. Account
 // upgrades or other owner transactions in the same block need reconciliation.
 const [before,after]=await Promise.all([
  client.getTransactionCount({address:action.from,blockNumber:receipt.blockNumber-1n}),
  client.getTransactionCount({address:action.from,blockNumber:receipt.blockNumber}),
 ]);
 check(before===after,'Owner nonce changed in the relay block; reconcile before continuing');
 if(action.nonce!==undefined)check(before===action.nonce,'Relayed execution used a different owner nonce position');
 return {kind:'relayed',ownerNonceConsumed:0,traceAddress:call.traceAddress,relayer:tx.from};
}
