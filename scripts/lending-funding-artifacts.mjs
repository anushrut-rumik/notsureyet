// Creates unsigned public transactions. NEVER signs or sends them.
import {readFile,writeFile} from 'node:fs/promises';
import {encodeFunctionData,parseAbi} from 'viem';
import {testnets} from '../.local/lending-build/testnets.js';
const abi=parseAbi(['function setPeer(address)','function transfer(address,uint256) returns (bool)',
 'function depositToPool(uint256)','function setCurrentLiquidityRate(uint256)']);
const address=s=>typeof s==='string'&&/^0x[\da-fA-F]{40}$/.test(s)&&BigInt(s)!==0n;
try{
 const [manifestPath,reviewPath,out]=process.argv.slice(2);
 if(!manifestPath||!reviewPath||!out)throw Error('Usage: lending-funding-artifacts.mjs manifest.json deployment-review.json new-output.json');
 const manifest=JSON.parse(await readFile(manifestPath,'utf8')),review=JSON.parse(await readFile(reviewPath,'utf8'));
 if(manifest.scope!=='dedicated-test-only'||review.scope!=='dedicated-test-only'||!address(manifest.owner)||manifest.owner!==review.signer)throw Error('Dedicated test signer required');
 if(manifest.chains.length!==2||new Set(manifest.chains.map(c=>c.chainId)).size!==2||!manifest.chains.every(c=>testnets.some(n=>n.chainId===c.chainId)&&address(c.pool)&&address(c.wallet)))throw Error('Fill both owned deployments first');
 const chains=testnets.map(n=>{
  const d=manifest.chains.find(c=>c.chainId===n.chainId),peer=manifest.chains.find(c=>c.chainId!==n.chainId);
  const plan=review.deployments.find(p=>p.network.chainId===n.chainId);
  if(!plan||plan.owner!==manifest.owner||plan.network.token!==n.token||plan.network.link!==n.link)throw Error('Review/network mismatch');
  const principal=BigInt(plan.principalUnits),idle=BigInt(plan.idleUnits),link=BigInt(plan.linkFundingUnits);
  if(principal<0n||idle<0n||link<=0n)throw Error('Invalid funding amount');
  const tx=(to,functionName,args,effect)=>({chainId:n.chainId,from:manifest.owner,to,value:'0',data:encodeFunctionData({abi,functionName,args}),functionName,args:args.map(String),effect});
  return {chainId:n.chainId,wallet:d.wallet,pool:d.pool,
   expectedInitialBnMUnits:String(principal+idle),expectedInvestedUnits:String(principal),expectedIdleUnits:String(idle),
   transactions:[tx(d.wallet,'setPeer',[peer.wallet],'Bind this user\'s opposite-chain wallet'),
    tx(n.token,'transfer',[d.wallet,principal+idle],'Fund only the reviewed principal plus idle reserve'),
    tx(n.link,'transfer',[d.wallet,link],'Fund test LINK separately'),
    ...(principal>0n?[tx(d.wallet,'depositToPool',[principal],'Make the initial principal deposit; retain idle reserve')]:[])],
   controlledHoldScenario:{...tx(d.pool,'setCurrentLiquidityRate',[BigInt(review.deployments.find(p=>p.network.chainId==='11155111').controlledRateRay)],'Set displayed mock APR equal to reviewed source APR; owner-only user-run setup transaction'),execute:'Only on Base Sepolia after recording the move-rate scenario; do not change policy limits.'}};
 });
 await writeFile(out,JSON.stringify({scope:manifest.scope,signer:manifest.owner,
  instructionDomain:{name:'LendingTestInstruction',version:'1',chainId:'chain-specific',verifyingContract:'chain-specific wallet'},
  instructionType:'Instruction(bytes32 operationId,uint256 sourceChainId,address consumer,uint64 destinationSelector,address destination,address token,uint256 amount,bytes32 policyVersion,uint256 expiry)',
  policyVersionNote:'Use keccak256(UTF-8 policy version), not a numeric chain selector or workflow name',chains},null,2)+'\n',{flag:'wx'});
 console.log('Created unsigned funding/configuration transactions. Inspect gas estimates, use only faucet funds, and execute from your dedicated wallet. No transaction was sent.');
}catch(e){console.error(e.message);process.exitCode=1;}
