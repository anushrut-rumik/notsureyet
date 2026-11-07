// The caller holds the per-user kernel lock for this entire run.
// Independent chains advance together; each chain preserves its reviewed nonce order.
import {keccak256} from 'viem';
import {verifySetupExecution} from './lending-receipts.mjs';
const check=(ok,message)=>{if(!ok)throw Error(message);};
const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();

export async function advanceSetup({plan,db,account,clients,reconcileOnly=false,minedOnly=false}) {
 const transactions=[];
 if(minedOnly)check(new Set(plan.actions.map(a=>a.chainId)).size===2&&plan.actions.every(a=>['11155111','84532'].includes(a.chainId)),'Mined setup is restricted to the two testnets');
 async function chain(chainId) {
  const rpc=clients(chainId);
  check(String(await rpc.getChainId())===chainId,'Wrong chain');
  const [safe,finalized]=minedOnly?[{number:-1n},null]:await Promise.all([rpc.getBlock({blockTag:'safe'}),
   rpc.getBlock({blockTag:'finalized'}).catch(()=>null)]);
  check(!finalized||safe.number>=finalized.number,'Inconsistent network checkpoints');
  for(const action of plan.actions.filter(a=>a.chainId===chainId)) {
   const prior=db.prepare('SELECT * FROM actions WHERE id=?').get(action.id);
   if(prior) {
    check(prior.intent===JSON.stringify(action),'Saved transaction intent changed');
    let receipt;
    try { receipt=await rpc.getTransactionReceipt({hash:prior.hash}); }
    catch(e) { if(e.name!=='TransactionReceiptNotFoundError')throw e; }
    if(!receipt) {
     // A dropped or reorganized transaction is reconciled by hash, never replaced.
     db.prepare("UPDATE actions SET state='unknown' WHERE id=?").run(action.id);
     transactions.push({action:action.id,hash:prior.hash,state:'pending'});
     return;
    }
    check(receipt.status==='success','Setup transaction reverted; do not retry');
    const canonical=await rpc.getBlock({blockNumber:receipt.blockNumber});
    check(same(canonical.hash,receipt.blockHash),'Noncanonical setup receipt; reconcile before continuing');
    await verifySetupExecution(rpc,action,prior.hash);
    if(action.creates) {
     check(same(receipt.contractAddress,action.creates),'Deployment address mismatch');
     const code=await rpc.getCode({address:action.creates,blockNumber:receipt.blockNumber});
     check(code&&code!=='0x','Deployed contract code missing');
    }
    const state=finalized&&finalized.number>=receipt.blockNumber?'confirmed':safe.number>=receipt.blockNumber?'safe':'mined';
    db.prepare('UPDATE actions SET state=? WHERE id=?').run(state,action.id);
    transactions.push({action:action.id,hash:prior.hash,state});
    // A verified mined receipt admits the next same-chain dependency. Finality
    // is optional for the dedicated testnet flow.
    continue;
   }
   if(reconcileOnly)return;
   const nonces=await Promise.all(['latest','pending'].map(blockTag=>rpc.getTransactionCount({address:plan.owner,blockTag})));
   check(nonces.every(n=>n===action.nonce),'Setup nonce drift; reconcile existing transactions');
   const [gas,gasPrice,balance]=await Promise.all([
    rpc.estimateGas({account:plan.owner,to:action.to??undefined,data:action.data,value:0n}),
    rpc.getGasPrice(),rpc.getBalance({address:plan.owner,blockTag:'pending'}),
   ]);
   const limit=(gas*120n+99n)/100n;
   check(limit*gasPrice<=10000000000000000n,'Setup gas cap exceeded');
   check(balance>=limit*gasPrice,'Insufficient setup gas balance');
   const raw=await account.signTransaction({chainId:Number(chainId),nonce:action.nonce,to:action.to??undefined,
    data:action.data,value:0n,gas:limit,gasPrice,type:'legacy'}),hash=keccak256(raw);
   db.prepare('INSERT INTO actions VALUES(?,?,?,?,?)').run(action.id,hash,'unknown',JSON.stringify(action),raw);
   try { await rpc.sendRawTransaction({serializedTransaction:raw}); } catch { /* Persisted uncertainty is not a retry. */ }
   transactions.push({action:action.id,hash,state:'pending'});
   return;
  }
 }
 // Settle BOTH branches before the caller can close the shared journal on error.
 const results=await Promise.allSettled([...new Set(plan.actions.map(a=>a.chainId))].map(chain));
 const failure=results.find(r=>r.status==='rejected');
 if(failure)throw failure.reason;
 transactions.sort((a,b)=>plan.actions.findIndex(x=>x.id===a.action)-plan.actions.findIndex(x=>x.id===b.action));
 const complete=transactions.length===plan.actions.length&&transactions.every(t=>(minedOnly?['mined','safe','confirmed']:['safe','confirmed']).includes(t.state));
 if(complete)return {status:'complete',transactions,finality:minedOnly?'not_required':transactions.every(t=>t.state==='confirmed')?'finalized':'pending'};
 if(reconcileOnly)return {status:'paused',transactions};
 const pending=transactions.find(t=>t.state==='pending')??transactions.find(t=>!['safe','confirmed'].includes(t.state));
 check(pending,'No setup progress recorded');
 return {status:'pending',action:pending.action,hash:pending.hash,transactions,
  mined:transactions.filter(t=>t.state!=='pending').length,total:plan.actions.length,
  awaitingFinality:transactions.length===plan.actions.length&&transactions.every(t=>t.state!=='pending')};
}
