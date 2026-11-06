// FAULT-INJECTION ONLY. The fixture addresses are not deployed contracts.
import {payloadFixture} from './lending-payload.mjs';
import {buildInstruction} from '../../.local/lending-build/part3/report.js';
import {FIXTURE_NOW} from '../../.local/lending-build/fixtures.js';
import {encodeEventTopics,encodeAbiParameters,parseAbiParameters} from 'viem';
import {abi} from '../../scripts/part3/monitor.mjs';
export const H='0x'+'ab'.repeat(32),M='0x'+'cd'.repeat(32),D='0x'+'ef'.repeat(32);
export function executionFixture(days=60){
 const observation=payloadFixture(days);
 const gas=observation.run.snapshot.costs.components.find(c=>c.category==='sourceGas');
 Object.assign(gas,{assetId:'ETH_GAS',amountUnits:'1500000',priceMicrounitsPerToken:'1000000'});
 const instruction=buildInstruction(observation,String(BigInt(FIXTURE_NOW)+600n));
 return {observation,instruction,signature:'0x'+'11'.repeat(65)};
}
export function log(address,eventName,indexed,types,data){return {address,topics:encodeEventTopics({abi,eventName,args:indexed}),data:encodeAbiParameters(parseAbiParameters(types),data)};}
export function sourceReceipt(p,changes={}){
 const i=p.instruction;
 return {status:'success',from:p.observation.run.context.walletAddress,transactionHash:H,blockNumber:20n,blockHash:H,
  logs:[log(i.consumer,'SourceSubmitted',{operationId:i.operationId,messageId:M},'address,address,uint256,uint256',[i.destination,i.token,BigInt(i.amount),BigInt(i.maxLinkFee)])],...changes};
}
export function destinationReceipt(p,fail=false){
 const i=p.instruction,d=p.observation.chains.find(c=>c.chainId!==i.sourceChainId),token='0x88A2d74F47a237a62e7A51cdDa67270CE381555e';
 return {status:'success',transactionHash:D,blockNumber:30n,blockHash:D,logs:fail?
  [log(d.wallet,'DepositFailed',{messageId:M,operationId:i.operationId},'uint256,bytes',[BigInt(i.amount),'0x1234'])]:
  [log(d.wallet,'PoolDeposited',{messageId:M,operationId:i.operationId},'address,uint256,uint256',[token,BigInt(i.amount),BigInt(i.amount)]),
   log(token,'Transfer',{from:d.wallet,to:d.pool},'uint256',[BigInt(i.amount)])]};
}
export function messageFixture(p,status='SUCCESS'){
 return {messageId:M,status,sender:p.instruction.consumer,receiver:p.instruction.destination,sendTransactionHash:H,receiptTransactionHash:D,
  sourceNetworkInfo:{chainSelector:'16015286601757825753',environment:'testnet'},destNetworkInfo:{chainSelector:'10344971235874465080',environment:'testnet'},
  tokenAmounts:[{sourceTokenAddress:p.instruction.token,destTokenAddress:'0x88A2d74F47a237a62e7A51cdDa67270CE381555e',amount:p.instruction.amount}]};
}
