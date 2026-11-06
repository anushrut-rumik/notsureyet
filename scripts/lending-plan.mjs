// Produces public user-review artifacts only. NEVER invokes a signing or sending tool.
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {keccak256,toBytes} from 'viem';
import {z} from 'zod';
import {uint,createRunInput,costCategories} from '../.local/lending-build/schema.js';
import {networkConfig} from '../.local/lending-build/networks.js';
import {testnets} from '../.local/lending-build/testnets.js';
const id=z.string().min(1).max(128),positive=uint.refine(s=>BigInt(s)>0n);
const schema=z.object({scope:z.literal('dedicated-test-only'),owner:z.string().regex(/^0x[\da-fA-F]{40}$/).refine(s=>BigInt(s)>0n),
 userId:id,runId:id,workflowId:z.string().regex(/^0x[\da-fA-F]{64}$/).refine(s=>BigInt(s)>0n),policyVersion:id,
 approvedAt:positive,deadline:positive,principalUnits:positive,protectedIdleReserveUnits:uint,maxMovableUnits:positive,
 poolCapacityUnits:positive,minNetBenefitMicrounits:uint,maxFeeMicrounits:uint,maxObservationAgeSeconds:positive,
 maxObservationSkewSeconds:uint,cooldownSeconds:uint,
 fictionalPrices:z.object({unit:z.literal('SIM_USD'),bnmMicrounitsPerToken:positive,linkMicrounitsPerToken:positive,ethMicrounitsPerToken:positive}).strict(),
 controlledSourceRateRay:uint,controlledDestinationRateRay:uint,estimatedSourceGasWei:positive,
 estimatedDestinationApprovalGasWei:uint,exitIfStayMicrounits:uint,exitIfMoveMicrounits:uint,
 sourceLinkFundingUnits:positive,destinationLinkFundingUnits:positive}).strict();
try{
 const [path,out]=process.argv.slice(2);if(!path||!out)throw Error('Usage: lending-plan.mjs completed-plan.json new-review-directory');
 const p=schema.parse(JSON.parse(await readFile(path,'utf8')));
 if(BigInt(p.deadline)<=BigInt(p.approvedAt)||BigInt(p.principalUnits)>BigInt(p.maxMovableUnits)||BigInt(p.principalUnits)>BigInt(p.poolCapacityUnits))throw Error('Invalid deadline, amount or capacity');
 const source=networkConfig.networks[0],destination=networkConfig.networks[1];
 const location=n=>({chainId:n.chainId,poolId:n.poolId,tokenId:n.tokenId});
 const evidence=label=>({observedAt:p.approvedAt,source:'controlled-test-plan:'+label,simulated:true});
 const seed=createRunInput({interfaceVersion:'1',runId:p.runId,networkConfigVersion:networkConfig.version,
  context:{userId:p.userId,walletAddress:p.owner,walletBindingId:'manual-test-wallet-binding',policyVersion:p.policyVersion},
  observation:{snapshotId:p.runId+'-snapshot',assembledAt:p.approvedAt,reference:p.runId+'-evidence'},operationId:p.runId+'-operation',
  policy:{version:p.policyVersion,approvalId:'manual-test-approval',userId:p.userId,walletAddress:p.owner,approvedAt:p.approvedAt,
   deadline:p.deadline,valuationUnit:'SIM_USD',allowSimulatedInputs:true,approvedPools:[location(source),location(destination)],
   protectedIdleReserveUnits:p.protectedIdleReserveUnits,maxMovableUnits:p.maxMovableUnits,minNetBenefitMicrounits:p.minNetBenefitMicrounits,
   maxFeeMicrounits:p.maxFeeMicrounits,maxObservationAgeSeconds:p.maxObservationAgeSeconds,maxObservationSkewSeconds:p.maxObservationSkewSeconds,
   cooldownSeconds:p.cooldownSeconds,lastMoveCompletedAt:null},
  snapshot:{id:p.runId+'-snapshot',valuation:{unit:'SIM_USD',decimals:6,tokenDecimals:18,priceMicrounitsPerToken:p.fictionalPrices.bnmMicrounitsPerToken,evidence:evidence('fictional-BnM-price')},
   position:{id:p.userId+'-position',userId:p.userId,walletAddress:p.owner,location:location(source),investedUnits:p.principalUnits,movableUnits:p.principalUnits,evidence:evidence('UNOBSERVED-principal')},
   reserve:{chainId:source.chainId,tokenId:source.tokenId,idleBalanceUnits:p.protectedIdleReserveUnits,committedUnits:'0',evidence:evidence('UNOBSERVED-reserve')},
   currentPool:{location:location(source),aprBps:String(BigInt(p.controlledSourceRateRay)/10n**23n),withdrawalAvailableUnits:p.principalUnits,evidence:evidence('UNOBSERVED-source-rate')},
   destinationPool:{location:location(destination),aprBps:String(BigInt(p.controlledDestinationRateRay)/10n**23n),depositCapacityUnits:p.poolCapacityUnits,evidence:evidence('UNOBSERVED-destination-rate')},
   costs:{components:costCategories.map(category=>{
    const dest=['destinationApprovalGas','destinationGas','depositFee'].includes(category),gas=['sourceGas','destinationApprovalGas'].includes(category);
    return {category,chainId:dest?destination.chainId:source.chainId,assetId:gas?'ETH_GAS':'LINK',assetDecimals:18,
     amountUnits:category==='sourceGas'?p.estimatedSourceGasWei:category==='destinationApprovalGas'?p.estimatedDestinationApprovalGasWei:'0',
     priceMicrounitsPerToken:gas?p.fictionalPrices.ethMicrounitsPerToken:p.fictionalPrices.linkMicrounitsPerToken,
     evidence:evidence(category==='bridgeFee'?'UNOBSERVED-router-quote':category==='destinationApprovalGas'&&p.estimatedDestinationApprovalGasWei==='0'?'destination-approval-included-in-bridgeFee':gas?'estimated-'+category:category==='destinationGas'?'destination-execution-included-in-bridgeFee':'mock-no-fee-or-no-approval-needed-'+category)};
   }),exitIfStay:{numeratorMicrounits:p.exitIfStayMicrounits,denominator:'1',evidence:evidence('estimated-exit-if-stay')},
    exitIfMove:{numeratorMicrounits:p.exitIfMoveMicrounits,denominator:'1',evidence:evidence('estimated-exit-if-move')}},
   feeBalances:[source,destination].flatMap(n=>['LINK','ETH_GAS'].map(assetId=>({chainId:n.chainId,assetId,assetDecimals:18,availableUnits:'0',reservedUnits:'0',evidence:evidence('UNOBSERVED-funding')}))),
   accounting:{initialDepositCostMicrounits:'0',realizedWithdrawalCostMicrounits:'0',priorRebalanceCostMicrounits:'0'},pendingOperation:null,model:{rates:'unchanged',transferDowntimeSeconds:'0'}}});
 const deployments=testnets.map((n,i)=>{
  const rate=i?p.controlledDestinationRateRay:p.controlledSourceRateRay;
  if(BigInt(rate)%10n**23n!==0n)throw Error('RAY rates must be exact whole basis points');
  const tuple=`(${p.owner},${p.workflowId},${keccak256(toBytes(p.policyVersion))},${p.protectedIdleReserveUnits},${p.maxMovableUnits},${rate},${p.poolCapacityUnits})`;
  return {network:n,owner:p.owner,script:'contracts/script/lending/DeployLending.s.sol:DeployLending',signature:'run((address,bytes32,bytes32,uint256,uint256,uint256,uint256))',argument:tuple,
   controlledRateRay:rate,expectedEffect:'Deploy one new pool and one dedicated per-user strategy wallet on this chain only',
   principalUnits:i?'0':p.principalUnits,idleUnits:p.protectedIdleReserveUnits,linkFundingUnits:i?p.destinationLinkFundingUnits:p.sourceLinkFundingUnits};
 });
 const dir=resolve(out);await mkdir(dir,{recursive:false});
 await writeFile(dir+'/seed-run.json',JSON.stringify(seed,null,2)+'\n',{flag:'wx'});
 await writeFile(dir+'/deployment-review.json',JSON.stringify({scope:p.scope,signer:p.owner,deployments},null,2)+'\n',{flag:'wx'});
 await writeFile(dir+'/manifest.json',JSON.stringify({scope:p.scope,owner:p.owner,workflowId:p.workflowId,chains:testnets.map(n=>({chainId:n.chainId,pool:null,wallet:null,poolDeploymentTx:null,walletDeploymentTx:null,fundingTxs:[]}))},null,2)+'\n',{flag:'wx'});
 console.log('Created public review artifacts at '+dir+'. Review and sign only in your own wallet-controlled environment. No transaction was sent.');
}catch(e){console.error(e.message);process.exitCode=1;}
