'use strict';
const $=id=>document.getElementById(id),base=location.pathname.replace(/\/$/,'');
let state,selected=null,p=null,busy=false,lastRender='',lastError='';
const short=s=>s.slice(0,8)+'…'+s.slice(-6);
async function api(path,body){const r=await fetch(base+'/api/'+path,{headers:{'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});const d=await r.json();if(!r.ok)throw Error(d.error||'The setup check failed.');return d;}
function provider(){const providers=window.ethereum?.providers||[window.ethereum];const mm=providers.find(v=>v?.isMetaMask);if(!mm)throw Error('Open this page in Chrome with your MetaMask extension enabled.');return mm;}
async function account(){p=provider();const accounts=await p.request({method:'eth_requestAccounts'});selected=accounts[0];if(selected?.toLowerCase()!==state.plan.owner.toLowerCase())throw Error('Select the funded test account '+state.plan.owner+' in MetaMask.');$('connection').textContent='Connected '+short(selected);return selected;}
async function network(id){const expected='0x'+Number(id).toString(16);await p.request({method:'wallet_switchEthereumChain',params:[{chainId:expected}]});if((await p.request({method:'eth_chainId'})).toLowerCase()!==expected)throw Error('MetaMask is on a different network.');const accounts=await p.request({method:'eth_accounts'});if(accounts[0]?.toLowerCase()!==state.plan.owner.toLowerCase())throw Error('The selected MetaMask account changed. Reconnect the test account.');}
async function perform(fn){if(busy)return;busy=true;lastError='';let failure='';controls();try{await fn();}catch(e){failure=e.message||'This step did not complete. Check MetaMask Activity before retrying.';}finally{busy=false;await refresh().catch(e=>{failure=failure||e.message;});if(failure){lastError=failure;$('message').textContent=failure;}controls();}}
function current(){return state.plan.actions.find(a=>state.records[a.id]?.status!=='confirmed');}
function controls(){if(!state)return;const a=current(),record=a&&state.records[a.id];const holdLocked=a?.phase==='hold'&&!state.moveEvidence;
 $('connect').disabled=busy;$('approve').disabled=busy||!selected||!$('reviewed').checked||!!state.approval;
 $('send').disabled=busy||!state.approval||!selected||!a||holdLocked||!!record&&!['rejected'].includes(record.status);
 $('send').hidden=!a;$('recover').hidden=!a||!['claimed','submitted'].includes(record?.status);
}
async function refresh(){
 state=await api('state');
 const fingerprint=JSON.stringify({records:state.records,approval:!!state.approval,move:state.moveEvidence});
 if(fingerprint!==lastRender){lastRender=fingerprint;lastError='';render();}
 const a=current(),stored=a&&localStorage.getItem('lending-'+state.approvalData.message.planHash+'-'+a.id);
 if(a&&stored&&state.records[a.id]?.status==='claimed'){
  if(document.activeElement!==$('hash'))$('hash').value=stored;
  $('receipt').textContent='Hash saved in this browser: '+stored;
  try{await api('hash',{id:a.id,hash:stored});$('message').textContent='Saved the transaction hash from this browser. Waiting for its receipt.';}catch(e){$('message').textContent='Transaction hash saved locally. '+e.message;}
 }
 if(lastError)$('message').textContent=lastError;
 controls();
}
function detail(label,value){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;return [dt,dd];}
function render(){
 $('wallet').textContent='Test owner: '+state.plan.owner;
 $('deadline').textContent='Fixed policy deadline: '+new Date(Number(state.plan.deadline)*1000).toLocaleString();
 $('approval').textContent=state.approval?'Exact plan signed by your test account. Each transaction still needs its own MetaMask confirmation.':'Signing this public plan sends no transaction and grants no token allowance.';
 const done=state.plan.actions.filter(a=>state.records[a.id]?.status==='confirmed').length;
 $('progress').textContent=done+' / '+state.plan.actions.length+' confirmed';
 const a=current();$('steps').replaceChildren();
 for(const action of state.plan.actions){const li=document.createElement('li'),label=document.createElement('div'),status=document.createElement('span');label.textContent=action.title;const r=state.records[action.id];status.textContent=action.network+' · '+(r?.status||'waiting');li.append(label,status);li.className=r?.status==='confirmed'?'done':a?.id===action.id?'active':'';$('steps').append(li);}
 if(!a){$('action-title').textContent='Wallet setup transactions complete';$('description').textContent='The receipts are saved. Return to the chat for finalized balance verification and the final CRE evidence.';$('details').replaceChildren();$('raw').textContent='';$('receipt').textContent='';return;}
 $('action-title').textContent=a.title;$('description').textContent=a.description;
 $('details').replaceChildren(...detail('Network',a.network+' · chain '+a.chainId),...detail('From',a.from),...detail(a.creates?'New contract address':'Target contract',a.creates||a.to),...detail('ETH attached','0 ETH; MetaMask shows the gas estimate'));
 $('raw').textContent=JSON.stringify({from:a.from,to:a.to,value:a.value,nonce:a.nonce,data:a.data},null,2);
 const r=state.records[a.id];$('receipt').textContent=r?.hash?'Transaction: '+r.hash:'';
 if(a.phase==='hold'&&!state.moveEvidence)$('message').textContent='Initial setup is complete. Waiting for the deployed-contract move dry run before enabling the hold-rate transaction. Return to the chat; keep this page open.';
 else if(r?.status==='claimed')$('message').textContent='A signing request has started. Finish it in MetaMask, or recover the existing hash from Activity. Do not send another transaction.';
 else if(r?.status==='submitted')$('message').textContent='Transaction submitted. Waiting for a successful receipt…';
 else if(r?.status==='failed')$('message').textContent=r.error;
 else $('message').textContent=state.approval?'Ready for this setup action. Review it in MetaMask.':'Connect the test account and sign the reviewed plan first.';
 if(r?.hash&&document.activeElement!==$('hash'))$('hash').value=r.hash;
}
$('connect').onclick=()=>perform(account);$('reviewed').onchange=controls;
$('approve').onclick=()=>perform(async()=>{
 await account();await network('11155111');
 const typed={...state.approvalData,types:{EIP712Domain:[{name:'name',type:'string'},{name:'version',type:'string'},{name:'chainId',type:'uint256'}],...state.approvalData.types}};
 $('message').textContent='Review the setup-plan signature in MetaMask.';
 const signature=await p.request({method:'eth_signTypedData_v4',params:[state.plan.owner,JSON.stringify(typed)]});
 await api('approve',{signature});
});
$('send').onclick=()=>perform(async()=>{
 await account();const a=current();await network(a.chainId);
 const tx=await api('claim',{id:a.id});let hash;
 try{hash=await p.request({method:'eth_sendTransaction',params:[tx]});}
 catch(e){if(e.code===4001)await api('rejected',{id:a.id});throw e;}
 localStorage.setItem('lending-'+state.approvalData.message.planHash+'-'+a.id,hash);
 await api('hash',{id:a.id,hash});
});
$('recover').onsubmit=e=>{e.preventDefault();perform(async()=>{const a=current();await api('hash',{id:a.id,hash:$('hash').value.trim()});});};
refresh().catch(e=>$('message').textContent=e.message);
setInterval(()=>{if(!busy)refresh().catch(e=>$('message').textContent=e.message);},4000);
