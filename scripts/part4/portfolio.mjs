import {testnetReadTag} from '../lib/lending-confirmation.mjs';
// Finalized, read-only binding to the repository's existing deployed testnet wallets.
import {createPublicClient,http,parseAbi,isAddress} from 'viem';
import {testnets} from '../../.local/lending-build/testnets.js';
import {tracking} from './tracking.mjs';
import {readAbi,rayToBps} from '../../.local/lending-build/observation.js';
const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();
const check=(ok,m)=>{if(!ok)throw Error(m);};
let raw='';for await(const c of process.stdin){raw+=c;if(raw.length>16384)throw Error('Oversized binding');}
try {
 const m=JSON.parse(raw);
 check(m.scope==='dedicated-test-only'&&isAddress(m.owner)&&BigInt(m.owner)>0n,'Verified dedicated owner required');
 check(m.chains?.length===2&&new Set(m.chains.map(c=>c.chainId)).size===2,'Both testnet bindings required');
 const bindings=parseAbi(['function owner() view returns(address)','function pool() view returns(address)',
  'function asset() view returns(address)','function peer() view returns(address)',
  'function protectedReserve() view returns(uint256)','function maxMove() view returns(uint256)']);
 const token=parseAbi(['function balanceOf(address) view returns(uint256)']);
 const chains=await Promise.all(m.chains.map(async c=>{
  const n=testnets.find(n=>n.chainId===c.chainId);check(n&&isAddress(c.wallet)&&isAddress(c.pool),'Invalid testnet binding');
  const client=createPublicClient({transport:http(n.rpc,{timeout:15000,retryCount:1})});
  check(String(await client.getChainId())===c.chainId,'RPC chain mismatch');
  const block=await client.getBlock({blockTag:testnetReadTag(m)});
  const read=(address,abi,functionName,args=[])=>client.readContract({address,abi,functionName,args,blockNumber:block.number});
  const [v,owner,pool,asset,peer,bnm,link,reserve,maximum]=await Promise.all([
   read(c.wallet,readAbi,'observe'),read(c.wallet,bindings,'owner'),read(c.wallet,bindings,'pool'),
   read(c.wallet,bindings,'asset'),read(c.wallet,bindings,'peer'),read(n.token,token,'balanceOf',[m.owner]),
   read(n.link,token,'balanceOf',[m.owner]),read(c.wallet,bindings,'protectedReserve'),read(c.wallet,bindings,'maxMove')]);
  check(same(owner,m.owner)&&same(pool,c.pool)&&same(asset,n.token)&&same(peer,m.chains.find(x=>x!==c).wallet),'Contract binding mismatch');
  check(v[0]===BigInt(c.chainId)&&v[1]===block.number&&v[11]===BigInt(m.owner),'Observation binding mismatch');
  return [c.chainId,{confirmation:testnetReadTag(m),chainId:c.chainId,wallet:c.wallet,pool:c.pool,blockNumber:String(block.number),blockHash:block.hash,
   observedAt:String(v[2]),principalUnits:String(v[4]),idleUnits:String(v[5]),aprBps:rayToBps(v[3]),
   strategyLinkUnits:String(v[8]),ownerNativeWei:String(v[9]),ownerBnMUnits:String(bnm),ownerLinkUnits:String(link),
   protectedReserveUnits:String(reserve),maxMoveUnits:String(maximum),mockPoolAccruesInterest:false,rateSimulated:true}];
 }));
 const progress=await tracking(m);
 console.log(JSON.stringify({...progress,mode:testnetReadTag(m)+'-testnet-read-only',owner:m.owner,chains:Object.fromEntries(chains),
  feeQuantities:'LINK and native wei are quantities, not SIM_USD valuations',earnedProfitUnits:'0'}));
} catch {console.error('Finalized testnet observation/binding failed');process.exitCode=1;}
