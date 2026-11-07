import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {keccak256} from 'viem';
import {advanceSetup} from '../scripts/lib/lending-setup-runner.mjs';
const owner='0x'+'11'.repeat(20),blockHash='0x'+'ab'.repeat(32);
function fixture(t) {
 const db=new DatabaseSync(':memory:');t.after(()=>db.close());
 db.exec('CREATE TABLE actions(id TEXT PRIMARY KEY,hash TEXT,state TEXT,intent TEXT,raw TEXT)');
 const actions=['11155111','84532'].flatMap(chainId=>[0,1,2].map(nonce=>({id:chainId+'-'+nonce,chainId,nonce,from:owner,
  to:nonce<2?null:owner,data:'0x1234',...(nonce<2?{creates:owner}:{})})));
 const plan={owner,actions},receipts=new Map(),txs=new Map(),sent=[],signed=[],clients={};
 let final=0n,safe=0n;
 for(const chainId of ['11155111','84532'])clients[chainId]={
  getChainId:async()=>Number(chainId),getBlock:async({blockTag})=>({number:blockTag==='finalized'?final:blockTag==='safe'?safe:10n,hash:blockHash}),
  getTransactionReceipt:async({hash})=>{if(receipts.has(hash))return receipts.get(hash);throw Object.assign(Error('pending'),{name:'TransactionReceiptNotFoundError'});},
  getTransaction:async({hash})=>txs.get(hash),getCode:async()=>'0x6000',
  getTransactionCount:async()=>actions.filter(a=>a.chainId===chainId&&[...txs.values()].some(tx=>tx.chainId===Number(chainId)&&tx.nonce===a.nonce&&receipts.has(tx.hash))).length,
  estimateGas:async()=>100000n,getGasPrice:async()=>1n,getBalance:async()=>10n**18n,
  sendRawTransaction:async({serializedTransaction:raw})=>{sent.push(raw);return keccak256(raw);},
 };
 const account={signTransaction:async tx=>{
  signed.push(tx);const raw='0x'+(BigInt(tx.chainId)*100n+BigInt(tx.nonce)).toString(16).padStart(16,'0'),hash=keccak256(raw);
  txs.set(hash,{...tx,from:owner,to:tx.to??null,input:tx.data,hash});return raw;
 }};
 const run=(extra={})=>advanceSetup({plan,db,account,clients:id=>clients[id],...extra});
 function mine(){for(const hash of txs.keys())receipts.set(hash,{status:'success',blockNumber:10n,blockHash,contractAddress:owner});}
 return {db,plan,clients,receipts,txs,sent,signed,run,mine,markSafe:()=>{safe=10n;},finalize:()=>{safe=10n;final=10n;}};
}
test('both chains start concurrently; mined receipts unlock dependencies before finality',async t=>{
 const f=fixture(t);let started=0,release;const gate=new Promise(r=>release=r);
 for(const c of Object.values(f.clients))c.getChainId=async()=>{if(++started===2)release();await gate;return c===f.clients['11155111']?11155111:84532;};
 let p=await f.run();assert.equal(started,2);assert.equal(f.sent.length,2);assert.equal(p.mined,0);
 f.mine();p=await f.run();assert.equal(f.sent.length,4);assert.equal(p.mined,2);
 f.mine();p=await f.run();assert.equal(f.sent.length,6);assert.equal(p.mined,4);
 f.mine();p=await f.run();assert.equal(p.status,'pending');assert.equal(p.awaitingFinality,true);assert.equal(p.mined,6);
 f.markSafe();p=await f.run();assert.equal(p.status,'complete');assert.equal(p.finality,'pending');
 f.finalize();p=await f.run();assert.equal(p.status,'complete');assert.equal(f.sent.length,6);
 await f.run();assert.equal(f.sent.length,6);
 assert.deepEqual(f.signed.filter(t=>t.chainId===11155111).map(t=>t.nonce),[0,1,2]);
});
test('uncertain broadcasts and missing receipts never create replacement transactions',async t=>{
 const f=fixture(t);for(const c of Object.values(f.clients))c.sendRawTransaction=async()=>{throw Error('timeout');};
 await f.run();await f.run();assert.equal(f.signed.length,2);assert.equal(f.db.prepare('SELECT count(*) n FROM actions').get().n,2);
});
test('pause reconciles mined receipts without signing next steps',async t=>{
 const f=fixture(t);await f.run();f.mine();assert.equal((await f.run({reconcileOnly:true})).status,'paused');assert.equal(f.signed.length,2);
});
test('reverted, noncanonical, wrong-address and empty-code receipts block dependent sends',async t=>{
 for(const defect of ['reverted','reorg','address','code']){
  const f=fixture(t);await f.run();f.mine();
  for(const r of f.receipts.values()){
   if(defect==='reverted')r.status='reverted';
   if(defect==='reorg')r.blockHash='0xdead';
   if(defect==='address')r.contractAddress='0xdead';
  }
  if(defect==='code')for(const c of Object.values(f.clients))c.getCode=async()=>'0x';
  await assert.rejects(f.run());assert.equal(f.signed.length,2);
 }
});
test('a previously mined receipt disappearing prevents further dependencies',async t=>{
 const f=fixture(t);await f.run();f.mine();await f.run();f.receipts.clear();
 await f.run();assert.equal(f.signed.length,4);assert.equal(f.db.prepare("SELECT count(*) n FROM actions WHERE state='confirmed'").get().n,0);
});
test('existing confirmed journals are rechecked and preserved across upgrade',async t=>{
 const f=fixture(t);await f.run();f.mine();f.finalize();
 f.db.prepare("UPDATE actions SET state='confirmed'").run();
 await f.run();assert.equal(f.signed.length,4);assert.equal(f.db.prepare("SELECT count(*) n FROM actions WHERE state='confirmed'").get().n,2);
});
test('wrong nonce and gas cap prevent signing',async t=>{
 for(const defect of ['nonce','gas']){
  const f=fixture(t);
  for(const c of Object.values(f.clients)){
   if(defect==='nonce')c.getTransactionCount=async()=>99;
   else c.getGasPrice=async()=>10n**18n;
  }
  await assert.rejects(f.run());assert.equal(f.signed.length,0);
 }
});
test('an error on one chain waits for the other branch before returning',async t=>{
 const f=fixture(t);let settled=false;
 f.clients['11155111'].getChainId=async()=>{throw Error('RPC unavailable');};
 f.clients['84532'].getChainId=async()=>{await new Promise(r=>setTimeout(r,20));settled=true;return 84532;};
 await assert.rejects(f.run(),/RPC unavailable/);assert.equal(settled,true);assert.equal(f.signed.length,1);
});

test('safe readiness is checked again without signing; lost receipts cannot stay ready',async t=>{
 const f=fixture(t);
 for(let i=0;i<3;i++){await f.run();f.mine();}
 f.markSafe();assert.equal((await f.run({reconcileOnly:true,account:null})).status,'complete');
 f.receipts.clear();assert.notEqual((await f.run({reconcileOnly:true,account:null})).status,'complete');
 assert.equal(f.signed.length,6);
});

test('safe read policy is restricted to the dedicated two-testnet setup',async()=>{
 const {setupReadTag}=await import('../scripts/lib/lending-confirmation.mjs');
 assert.equal(setupReadTag({}), 'finalized');
 const m={scope:'dedicated-test-only',setupConfirmation:'safe',chains:[{chainId:'11155111'},{chainId:'84532'}]};
 assert.equal(setupReadTag(m),'safe');
 assert.throws(()=>setupReadTag({...m,chains:[{chainId:'1'},{chainId:'8453'}]}));
});

test('unavailable finality RPC does not block verified safe readiness',async t=>{
 const f=fixture(t);
 for(let i=0;i<3;i++){await f.run();f.mine();}
 f.markSafe();
 for(const c of Object.values(f.clients)){
  const get=c.getBlock;
  c.getBlock=async args=>{if(args.blockTag==='finalized')throw Error('unavailable');return get(args);};
 }
 const result=await f.run({reconcileOnly:true,account:null});
 assert.equal(result.status,'complete');assert.equal(result.finality,'pending');assert.equal(f.signed.length,6);
});

test('dedicated testnet setup becomes ready from mined receipts without checkpoint RPCs',async t=>{
 const f=fixture(t);
 for(const c of Object.values(f.clients)){
  const get=c.getBlock;c.getBlock=async args=>{
   assert.ok(!['safe','finalized'].includes(args.blockTag));return get(args);
  };
 }
 for(let i=0;i<3;i++){await f.run({minedOnly:true});f.mine();}
 const result=await f.run({minedOnly:true});
 assert.equal(result.status,'complete');assert.equal(result.finality,'not_required');
 assert.equal(f.signed.length,6);assert.ok(result.transactions.every(t=>t.state==='mined'));
 await f.run({minedOnly:true});assert.equal(f.signed.length,6);
});
test('latest reads cannot be enabled for mainnet or an unscoped manifest',async()=>{
 const {testnetReadTag}=await import('../scripts/lib/lending-confirmation.mjs');
 assert.equal(testnetReadTag({scope:'dedicated-test-only',chains:[{chainId:'11155111'},{chainId:'84532'}]}),'latest');
 assert.equal(testnetReadTag({scope:'dedicated-test-only',chains:[{chainId:'1'},{chainId:'8453'}]}),'finalized');
 assert.equal(testnetReadTag({chains:[{chainId:'11155111'},{chainId:'84532'}]}),'finalized');
});
