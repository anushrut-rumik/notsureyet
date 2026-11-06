// Local user-run setup review. The server only reads chains and records public
// evidence. Transaction requests occur exclusively on explicit browser buttons.
import {createServer} from 'node:http';
import {readFile,writeFile,mkdir,rename,rm} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

import {buildSetupPlan,createSetupStore,storeState,clientFor,approvalData,assert,nextAction} from './lib/lending-setup.mjs';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const [owner,directory='.local/lending-metamask']=process.argv.slice(2);
assert(/^0x[\da-fA-F]{40}$/.test(owner||''),'Usage: lending-metamask-setup.mjs public-test-owner [state-directory]');
const dir=resolve(root,directory),statePath=resolve(dir,'state.json');await mkdir(dir,{recursive:true});
let lock;
try{lock=await mkdir(resolve(dir,'server.lock'));}catch{throw Error('A setup server lock exists. Keep one server running; reconcile a stopped server before removing its lock.');}
await writeFile(resolve(dir,'server.lock','pid'),String(process.pid));
const removeLock=()=>rm(resolve(dir,'server.lock'),{recursive:true,force:true});
let state;
try{state=JSON.parse(await readFile(statePath,'utf8'));assert(state.plan.owner.toLowerCase()===owner.toLowerCase(),'This setup belongs to another wallet');}
catch(e){if(e.code!=='ENOENT'){await removeLock();throw e;}
 const artifacts={pool:JSON.parse(await readFile(resolve(root,'contracts/out/MockPool.sol/MockPool.json'),'utf8')),
  wallet:JSON.parse(await readFile(resolve(root,'contracts/out/TestStrategyWallet.sol/TestStrategyWallet.json'),'utf8'))};
 assert(artifacts.pool.metadata.settings.evmVersion==='cancun'&&artifacts.wallet.metadata.settings.evmVersion==='cancun','Build with npm run lending:setup:build first');
 const nonces={};
 for(const id of ['11155111','84532']){const c=clientFor(id);assert(String(await c.getChainId())===id,'RPC chain mismatch');
  const latest=await c.getTransactionCount({address:owner,blockTag:'latest'}),pending=await c.getTransactionCount({address:owner,blockTag:'pending'});
  assert(latest===pending,'An owner transaction is pending; confirm it before preparing setup');nonces[id]=latest;
 }
 state={plan:buildSetupPlan(owner,nonces,artifacts,Math.floor(Date.now()/1000)),approval:null,records:{}};
 await storeState(statePath,state);
}
const store=createSetupStore(state,statePath);
let secret;try{secret=(await readFile(resolve(dir,'review-token.txt'),'utf8')).trim();}catch(e){if(e.code!=='ENOENT')throw e;secret=randomBytes(24).toString('hex');await writeFile(resolve(dir,'review-token.txt'),secret,{mode:0o600});}
assert(/^[a-f0-9]{48}$/.test(secret),'Invalid local review token');
const base='/setup/'+secret,port=8799,origin='http://127.0.0.1:'+port;
const atomic=async(path,value)=>{await writeFile(path+'.tmp',JSON.stringify(value,null,2)+'\n');await rename(path+'.tmp',path);};
async function exportManifest(){
 const initial=state.plan.actions.filter(a=>a.phase!=='hold');if(!initial.every(a=>state.records[a.id]?.status==='confirmed'))return false;
 const manifest={scope:'dedicated-test-only',owner:state.plan.owner,workflowId:state.plan.workflowId,chains:state.plan.chains.map(c=>({chainId:c.chainId,pool:c.pool,wallet:c.wallet,
  poolDeploymentTx:state.records[c.chainId+'-pool'].hash,walletDeploymentTx:state.records[c.chainId+'-wallet'].hash,
  fundingTxs:initial.filter(a=>a.chainId===c.chainId&&!a.creates).map(a=>state.records[a.id].hash),
  fundingCalls:initial.filter(a=>a.chainId===c.chainId&&!a.creates).map(a=>({hash:state.records[a.id].hash,to:a.to,data:a.data}))}))};
 await atomic(resolve(dir,'manifest.json'),manifest);return true;
}
async function body(req){let input='';for await(const chunk of req){input+=chunk;if(input.length>16384)throw Error('Oversized request');}return JSON.parse(input);}
function reply(res,status,data,type='application/json'){res.writeHead(status,{'Content-Type':type,'Cache-Control':'no-store',
 'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'"});res.end(type==='application/json'?JSON.stringify(data):data);}
const server=createServer(async(req,res)=>{
 try{
  assert(req.headers.host==='127.0.0.1:'+port,'Use the exact local review URL');
  const path=new URL(req.url,origin).pathname;
  if(req.method==='POST'){
   assert(req.headers.origin===origin,'Cross-origin writes are rejected');
   assert(req.headers['content-type']?.startsWith('application/json'),'JSON requests only');
   const data=await body(req);
   if(path===base+'/api/approve')await store.approve(data.signature);
   else if(path===base+'/api/claim')return reply(res,200,await store.claim(data.id));
   else if(path===base+'/api/rejected')await store.rejected(data.id);
   else if(path===base+'/api/hash')await store.attach(data.id,data.hash);
   else return reply(res,404,{error:'Unknown setup action'});
   console.log('Setup journal updated:',nextAction(state)?.id||'all confirmed');
   return reply(res,200,{ok:true});
  }
  if(req.method!=='GET')return reply(res,405,{error:'Method not allowed'});
  if(path===base+'/api/state'){
   await store.reconcile();await exportManifest();
   if(!state.moveEvidence){try{const e=JSON.parse(await readFile(resolve(dir,'move-evidence.json'),'utf8'));
    if(e.exitCode===0&&e.balancesUnchanged===true&&e.localMatchesCRE===true&&e.actual?.result?.decision?.action==='move'&&e.actual.result.context.walletAddress.toLowerCase()===state.plan.owner.toLowerCase()&&e.actual.result.context.policyVersion===state.plan.policyVersion){
     state.moveEvidence={localMatchesCRE:true,action:'move',runId:e.actual.result.runId};await storeState(statePath,state);
    }}catch(e){if(e.code!=='ENOENT')console.error('Move evidence not accepted:',e.message);}}
   return reply(res,200,{...state,approvalData:approvalData(state.plan),manifestReady:await exportManifest()});
  }
  if(path===base+'/api/review')return reply(res,200,state.plan);
  const assets=new Map([[base+'/','index.html'],[base+'/app.js','app.js'],[base+'/style.css','style.css']]);
  if(assets.has(path))return reply(res,200,await readFile(resolve(root,'integrations/lending-setup',assets.get(path)),'utf8'),path.endsWith('.js')?'text/javascript':path.endsWith('.css')?'text/css':'text/html');
  return reply(res,404,{error:'Open the exact setup URL printed by this server'});
 }catch(e){reply(res,400,{error:e.shortMessage||e.message});}
});
server.on('error',async e=>{console.error(e.message);await removeLock();process.exitCode=1;});
server.listen(port,'127.0.0.1',()=>{
 console.log('META_MASK_SETUP_URL='+origin+base+'/');
 console.log('PUBLIC_SETUP_STATE='+statePath);
 console.log('The user must connect, review, sign the plan and confirm each transaction in MetaMask. No server method signs or submits transactions.');
});
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>server.close(async()=>{await removeLock();process.exit(0);}));
