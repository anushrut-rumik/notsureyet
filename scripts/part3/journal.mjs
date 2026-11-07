// Single-host durable execution cache. Never a replacement for Arkiv/user approval authority.
// Node 22's SQLite uses cross-process transactions; no stale lock-file deletion or timeout unlocks.
import {DatabaseSync} from 'node:sqlite';
import {mkdirSync,chmodSync} from 'node:fs';
import {dirname} from 'node:path';
import {canonicalJson,inputHash} from '../../.local/lending-build/part3/report.js';
const terminal=new Set(['verified_deposit','cancelled','recovered_idle']);
const transitions={prepared:['cancelled','submission_unknown'],submission_unknown:['submitted','source_confirmed','failed'],
 submitted:['source_confirmed','failed'],source_confirmed:['in_transit','destination_executed','destination_failed'],
 in_transit:['destination_executed','destination_failed'],destination_failed:['destination_executed'],
 destination_executed:['idle_tokens','verified_deposit'],idle_tokens:['verified_deposit','recovered_idle'],
 failed:[]};
export class OperationJournal {
 constructor(path){
  mkdirSync(dirname(path),{recursive:true,mode:0o700});this.db=new DatabaseSync(path);chmodSync(path,0o600);
  this.db.exec(`PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
   CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, wallet TEXT NOT NULL, state TEXT NOT NULL,
    immutable TEXT NOT NULL, immutable_hash TEXT NOT NULL, details TEXT NOT NULL);
   CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_per_owner ON operations(wallet)
    WHERE state NOT IN ('verified_deposit','cancelled','recovered_idle');
   CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY, id TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL);`);
 }
 close(){this.db.close();}
 transaction(fn){this.db.exec('BEGIN IMMEDIATE');try{const r=fn();this.db.exec('COMMIT');return r;}catch(e){this.db.exec('ROLLBACK');throw e;}}
 get(id){const row=this.db.prepare('SELECT * FROM operations WHERE id=?').get(id);if(!row)return null;
  const immutable=JSON.parse(row.immutable);if(inputHash(immutable)!==row.immutable_hash)throw Error('Operation integrity check failed');
  return {id:row.id,wallet:row.wallet,state:row.state,immutable,details:JSON.parse(row.details)};}
 list(){return this.db.prepare('SELECT id FROM operations ORDER BY rowid').all().map(r=>this.get(r.id));}
 pending(wallet){return this.list().find(r=>r.wallet===wallet.toLowerCase()&&!terminal.has(r.state))??null;}
 prepare(immutable){return this.transaction(()=>{
  const id=immutable.instruction.operationId,wallet=immutable.observation.run.context.walletAddress.toLowerCase();
  if(this.get(id))throw Error('Operation ID already exists; reconcile it instead of preparing again');
  const details={createdAt:new Date().toISOString(),sourceHash:null,messageId:null,destinationHash:null,
   recoveryStatus:'none',failureReason:null,inTransitSince:null,verifiedDestinationPrincipal:null};
  this.db.prepare('INSERT INTO operations VALUES(?,?,?,?,?,?)').run(id,wallet,'prepared',canonicalJson(immutable),inputHash(immutable),JSON.stringify(details));
  this.db.prepare('INSERT INTO history(id,state,record) VALUES(?,?,?)').run(id,'prepared',JSON.stringify(details));
  return this.get(id);
 });}
 update(id,state,patch={}){return this.transaction(()=>{
  const r=this.get(id);if(!r)throw Error('Unknown operation');
  if(state!==r.state&&!transitions[r.state]?.includes(state))throw Error('Unsafe operation transition: '+r.state+' -> '+state);
  if(terminal.has(r.state))throw Error('A terminal operation cannot be rewritten');
  for(const k of ['sourceHash','messageId'])if(r.details[k]&&patch[k]&&r.details[k]!==patch[k])throw Error('Cannot replace '+k);
  const details={...r.details,...patch,updatedAt:new Date().toISOString()};
  this.db.prepare('UPDATE operations SET state=?,details=? WHERE id=?').run(state,JSON.stringify(details),id);
  this.db.prepare('INSERT INTO history(id,state,record) VALUES(?,?,?)').run(id,state,JSON.stringify(details));return this.get(id);
 });}
 claim(id,submissionContext={}){return this.transaction(()=>{
  const r=this.get(id);if(r?.state!=='prepared')throw Error('Signing/submission already started; reconcile first');
  const details={...r.details,submissionContext,submissionStartedAt:new Date().toISOString(),failureReason:'Submission may have happened; never blindly resend'};
  this.db.prepare('UPDATE operations SET state=?,details=? WHERE id=?').run('submission_unknown',JSON.stringify(details),id);
  this.db.prepare('INSERT INTO history(id,state,record) VALUES(?,?,?)').run(id,'submission_unknown',JSON.stringify(details));return this.get(id);
 });}
 cancelAfterExpiryProof(id,proof){return this.transaction(()=>{
  const r=this.get(id);if(!r||!['submission_unknown','submitted','failed'].includes(r.state))throw Error('Operation is not an unresolved source submission');
  const mined=proof.confirmation==='mined';
  if(mined&&(!['11155111','84532'].includes(proof.sourceChainId)||proof.sourceChainId!==r.immutable.instruction.sourceChainId))throw Error('Mined no-send proof is testnet-only');
  if(proof.operationId!==id||BigInt(mined?proof.blockTimestamp:proof.finalizedTimestamp)<BigInt(r.immutable.instruction.expiry)||BigInt(proof.sentMessageId)!==0n||BigInt(proof.activeOperation)!==0n||proof.ownerHasPendingTransaction!==false)
   throw Error('Missing finalized no-send proof');
  const details={...r.details,noSendProof:proof,failureReason:'Instruction expired on the '+(mined?'canonical mined':'finalized')+' source chain without a token send',recoveryStatus:'verified-no-send',updatedAt:new Date().toISOString()};
  this.db.prepare('UPDATE operations SET state=?,details=? WHERE id=?').run('cancelled',JSON.stringify(details),id);
  this.db.prepare('INSERT INTO history(id,state,record) VALUES(?,?,?)').run(id,'cancelled',JSON.stringify(details));return this.get(id);
 });}
 cancelBeforeBroadcast(id,execution){return this.transaction(()=>{
  const r=this.get(id);
  if(r?.state!=='submission_unknown'||r.details.sourceHash||r.details.messageId||
     execution?.notSubmitted!==true||execution.exitCode!==1||execution.error?.code!=='CRE_SIGNER_UNAVAILABLE')
   throw Error('Missing proof that CLI initialization stopped before submission');
  const details={...r.details,execution,broadcastAttempted:false,
   recoveryStatus:'verified-before-broadcast',failureReason:execution.error.message,updatedAt:new Date().toISOString()};
  this.db.prepare('UPDATE operations SET state=?,details=? WHERE id=?').run('cancelled',JSON.stringify(details),id);
  this.db.prepare('INSERT INTO history(id,state,record) VALUES(?,?,?)').run(id,'cancelled',JSON.stringify(details));return this.get(id);
 });}
}
