'use strict';
const $=id=>document.getElementById(id);
const token=location.pathname.split('/').at(-1);
const base='/api/link/'+encodeURIComponent(token);
let state;
async function api(action,body){
 const r=await fetch(base+(action?'/'+action:''),{headers:{'Content-Type':'application/json'},
  ...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
 const d=await r.json();if(!r.ok)throw Error(d.detail||'This step failed. Return to WhatsApp to check its saved state.');return d;
}
function provider(){
 const providers=window.ethereum?.providers||[window.ethereum];
 const p=providers.find(p=>p?.isMetaMask);if(!p)throw Error('Open this same link in the MetaMask app using the button above.');return p;
}
async function step(stage,error){
 $('message').textContent=({network_requested:'Select Ethereum Sepolia for this connection.',permissions_requested:'Approve fresh access to your Ethereum account and include Ethereum Sepolia in the allowed networks.',accounts_requested:'Approve the account connection in MetaMask.',accounts_returned:'Wallet selected. Preparing the connection message.',signature_requested:'Review and sign the connection message in MetaMask.',signature_returned:'Saving your wallet connection…'})[stage]||$('message').textContent;
 try{await api('progress',{stage,error_code:Number.isInteger(error?.code)?error.code:0});}catch{}
}
async function account(){
 const p=provider();await step('accounts_requested');
 const accounts=await p.request({method:'eth_requestAccounts'});
 const selected=Array.isArray(accounts)?accounts.find(value=>typeof value==='string'&&/^0x[0-9a-fA-F]{40}$/.test(value)):null;
 if(!selected)throw Error('MetaMask did not return an Ethereum account. Select your Ethereum wallet and reopen this link.');
 await step('accounts_returned');return {p,address:selected.toLowerCase()};
}
async function sepolia(p){
 try{await p.request({method:'wallet_switchEthereumChain',params:[{chainId:'0xaa36a7'}]});}
 catch(e){if(e.code!==4902)throw e;await p.request({method:'wallet_addEthereumChain',params:[{chainId:'0xaa36a7',chainName:'Ethereum Sepolia',nativeCurrency:{name:'Sepolia Ether',symbol:'ETH',decimals:18},rpcUrls:['https://ethereum-sepolia-rpc.publicnode.com'],blockExplorerUrls:['https://sepolia.etherscan.io']}]});}
 if((await p.request({method:'eth_chainId'})).toLowerCase()!=='0xaa36a7')throw Error('Select Ethereum Sepolia in MetaMask.');
}
async function rabdi(p){
 try{await p.request({method:'wallet_switchEthereumChain',params:[{chainId:'0x76edaf'}]});}
 catch(e){if(e.code!==4902)throw e;await p.request({method:'wallet_addEthereumChain',params:[{chainId:'0x76edaf',chainName:'Arkiv Rabdi',nativeCurrency:{name:'Test Golem',symbol:'tGLM',decimals:18},rpcUrls:['https://rpc.rabdi.db-chain.testnet.arkiv.network'],blockExplorerUrls:['https://rabdi.explorer.arkiv.network']}]});}
 if((await p.request({method:'eth_chainId'})).toLowerCase()!=='0x76edaf')throw Error('Select Arkiv Rabdi in MetaMask.');
}
async function connect(){
 await step('connect_clicked');
 const activeProvider=provider();
 await step('network_requested');await sepolia(activeProvider);
 // eth_requestAccounts can return an account allowed on another chain. A fresh
 // permission request is needed before relying on that account for signing.
 await step('permissions_requested');
 await activeProvider.request({method:'wallet_requestPermissions',params:[{eth_accounts:{}}]});
 await step('permissions_returned');
 const {p,address}=await account();
 const permitted=await p.request({method:'eth_accounts'});
 if(!Array.isArray(permitted)||!permitted.some(value=>typeof value==='string'&&value.toLowerCase()===address))throw Error('MetaMask has not granted this site access to the selected account. Reconnect that account and allow Ethereum Sepolia.');
 const chain=Number.parseInt(await p.request({method:'eth_chainId'}),16);
 if(chain!==11155111)throw Error('The wallet network changed. Select Ethereum Sepolia and connect again.');
 const challenge=await api('challenge',{address,chain_id:chain});await step('challenge_received');
 const encoded='0x'+Array.from(new TextEncoder().encode(challenge.message),b=>b.toString(16).padStart(2,'0')).join('');
 const signer=challenge.address.toLowerCase();
 if(!/^0x[0-9a-f]{40}$/.test(signer))throw Error('The wallet connection address is invalid. Reopen the link from WhatsApp.');
 await step('signature_requested');
 let signature;
 try{signature=await p.request({method:'personal_sign',params:[encoded,signer]});}
 catch(e){if(e.code===-32602)e.message='MetaMask rejected account access for signing on Sepolia. Disconnect this site in MetaMask, then reopen the WhatsApp link and allow this account on Ethereum Sepolia. No wallet connection has been saved.';throw e;}
 await step('signature_returned');
 const result=await api('connect',{signature});await step('completed');
 $('message').textContent='Connected '+result.address+'. Return to WhatsApp; Ira has sent the confirmation there.';
 $('action').hidden=true;
}
async function attach(hash){await api('result',{tx_hash:hash});localStorage.removeItem('ira-tx-'+token);$('message').textContent='Transaction reference saved. Return to WhatsApp for confirmation and your receipt.';$('recover').hidden=true;}
async function sign(){
 const p=provider();await (state.payment.chain_id===7794095?rabdi:sepolia)(p);
 await p.request({method:'wallet_requestPermissions',params:[{eth_accounts:{}}]});
 const {address}=await account();
 if(address.toLowerCase()!==state.payment.sender.toLowerCase())throw Error('Select the connected account '+state.payment.sender+' in MetaMask.');
 const request=await api('claim',{});
 const current=await p.request({method:'eth_accounts'});
 if(!current.some(a=>a.toLowerCase()===address)||Number.parseInt(await p.request({method:'eth_chainId'}),16)!==state.payment.chain_id)throw Error('The wallet account or network changed. Check this existing request before continuing.');
 $('action').hidden=true;$('recover').hidden=false;
 // A claim is one-shot. A failed or lost wallet response never makes a second send request.
 let hash;
 try{hash=await p.request({method:'eth_sendTransaction',params:[request.transaction]});}
 catch(e){$('message').textContent=e.code===4001?'MetaMask declined the request. Ask Ira about this task before preparing another payment.':'The wallet response is uncertain. Check MetaMask activity and save its existing transaction hash below.';return;}
 localStorage.setItem('ira-tx-'+token,hash);$('hash').value=hash;await attach(hash);
}
async function run(fn){$('action').disabled=true;try{await fn();}catch(e){await step('wallet_error',e);$('message').textContent=e.message||'The wallet request failed.';}finally{$('action').disabled=false;}}
function detail(label,value){const dt=document.createElement('dt');dt.textContent=label;const dd=document.createElement('dd');dd.textContent=value;return [dt,dd];}
async function init(){
 state=await api('');await step('page_loaded');
 $('open').href='https://link.metamask.io/dapp/'+location.host+location.pathname;
 $('open').hidden=!!window.ethereum?.isMetaMask;
 if(state.kind==='connect'){
  $('intro').textContent='Connect your existing wallet to your Ira WhatsApp chat. This message does not grant spending permission.';
  if(state.expected_address){const p=document.createElement('p');p.textContent='Expected account: '+state.expected_address;$('details').append(p);}
  $('action').textContent='Connect and sign message';$('action').onclick=()=>run(connect);
  if(state.completed){$('message').textContent='This wallet link is already connected. Return to WhatsApp.';$('action').hidden=true;}
 }else{
  const storage=state.kind==='arkiv';
  $('title').textContent=storage?'Save task to Arkiv':'Review payment';$('intro').textContent=storage?'Approve an encrypted task checkpoint on Arkiv Rabdi. This uses tGLM for the network fee. Records last 30 days.':'You approved this payment in chat. Confirm the same details in MetaMask to send it.';
  const p=state.payment;const dl=document.createElement('dl');
  for(const [label,value] of [['Network',p.chain_id===7794095?'Arkiv Rabdi':'Ethereum Sepolia'],['Amount',p.amount+' '+p.asset],['From',p.sender],[storage?'Arkiv storage operation':'Recipient',p.recipient],['Maximum fee (wei)',p.max_fee_wei],['Status',p.status]])dl.append(...detail(label,value));
  if(p.kind==='ccip'){for(const [label,value] of [['Step',p.phase==='allowance'?'Approve exact token allowance':'Send cross-chain transfer'],['Destination',p.destination],['Sender contract / allowance spender',p.sender_contract],['CCIP fee quote',p.ccip_fee_eth+' ETH'],['Maximum CCIP fee',p.ccip_fee_cap_eth+' ETH (unused portion refunded)']])dl.append(...detail(label,value));}
  $('details').append(dl);$('action').textContent='Review and send in MetaMask';$('action').onclick=()=>run(sign);
  if(state.claimed||p.status!==(storage?'external_prepared':'awaiting_signature')){$('action').hidden=true;$('recover').hidden=storage?p.status==='verified':!!p.tx_hash;$('message').textContent=p.tx_hash?'Transaction reference: '+p.tx_hash:'A signing request already started. Check MetaMask activity; do not send again.';if(p.tx_hash)$('hash').value=p.tx_hash;}
  const saved=localStorage.getItem('ira-tx-'+token);if(saved){$('hash').value=saved;$('recover').hidden=false;}
 }
 $('action').disabled=false;
}
$('recover').onsubmit=e=>{e.preventDefault();run(()=>attach($('hash').value.trim()));};
init().catch(e=>{$('message').textContent=e.message;$('action').hidden=true;});
