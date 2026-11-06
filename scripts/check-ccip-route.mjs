// Read-only validation of the official route. Never loads a signing key or sends a transaction.
import {createPublicClient,http,parseAbi} from 'viem';
import {sepolia,baseSepolia} from 'viem/chains';
const source=createPublicClient({chain:sepolia,transport:http(process.env.AGENT_SEPOLIA_RPC_URL||'https://ethereum-sepolia-rpc.publicnode.com')});
const destination=createPublicClient({chain:baseSepolia,transport:http(process.env.AGENT_BASE_SEPOLIA_RPC_URL||'https://sepolia.base.org')});
const router='0x0BF3dE8c5D3e8A2B34D2BEeB17ABfCeBaf363A59';
const destinationRouter='0xD3b06cEbF099CE7DA4AcCf578aaebFDBd6e88a93';
const token='0xFd57b4ddBf88a4e07fF4e34C487b99af2Fe82a05';
const destinationToken='0x88A2d74F47a237a62e7A51cdDa67270CE381555e';
const registry='0x95F29FEE11c5C55d26cCcf1DB6772DE953B37B82';
const selector=10344971235874465080n,sourceSelector=16015286601757825753n;
const abi=parseAbi(['function isChainSupported(uint64) view returns (bool)',
 'function getOnRamp(uint64) view returns (address)',
 'function getOffRamps() view returns ((uint64 sourceChainSelector,address offRamp)[])',
 'function typeAndVersion() view returns (string)',
 'function getPool(address) view returns (address)',
 'function getRemoteToken(uint64) view returns (bytes)',
 'function decimals() view returns (uint8)',
 'function router() view returns (address)','function token() view returns (address)',
 'function destination() view returns (uint64)']);
function check(condition,message){if(!condition)throw Error(message);}
try{
 check(await source.getChainId()===11155111,'Wrong source RPC chain');
 check(await destination.getChainId()===84532,'Wrong destination RPC chain');
 check(await source.readContract({address:router,abi,functionName:'isChainSupported',args:[selector]}),'Route disabled');
 const onRamp=await source.readContract({address:router,abi,functionName:'getOnRamp',args:[selector]});
 const version=await source.readContract({address:onRamp,abi,functionName:'typeAndVersion'});
 check(version.includes('2.0.0'),'Expected a CCIP 2.0 OnRamp; recheck the official directory');
 const pool=await source.readContract({address:registry,abi,functionName:'getPool',args:[token]});
 const remote=await source.readContract({address:pool,abi,functionName:'getRemoteToken',args:[selector]});
 check(BigInt(remote)===BigInt(destinationToken),'Pool remote token does not match Base Sepolia CCIP-BnM');
 for(const [client,address] of [[source,token],[destination,destinationToken]]){
  check(await client.readContract({address,abi,functionName:'decimals'})===18,'Expected 18 token decimals');
 }
 const ramps=await destination.readContract({address:destinationRouter,abi,functionName:'getOffRamps'});
 check(ramps.some(r=>r.sourceChainSelector===sourceSelector),'No destination OffRamp for source');
 const sender=process.env.AGENT_CCIP_SENDER_ADDRESS;
 if(sender){
  check((await source.readContract({address:sender,abi,functionName:'router'})).toLowerCase()===router.toLowerCase(),'Sender router mismatch');
  check((await source.readContract({address:sender,abi,functionName:'token'})).toLowerCase()===token.toLowerCase(),'Sender token mismatch');
  check(await source.readContract({address:sender,abi,functionName:'destination'})===selector,'Sender destination mismatch');
 }
 console.log(JSON.stringify({ok:true,route:'Ethereum Sepolia -> Base Sepolia',version,onRamp,sourceRouter:router,destinationRouter,sourceToken:token,destinationToken,sourcePool:pool,sender:sender||'not deployed yet'},null,2));
}catch{
 console.error('CCIP route check failed. Verify RPC availability, network IDs and the current Chainlink directory. No transaction was sent.');
 process.exitCode=1;
}
