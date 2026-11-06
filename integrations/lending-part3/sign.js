const instruction=document.getElementById('instruction'),observation=document.getElementById('observation'),button=document.getElementById('sign'),status=document.getElementById('status');
let payload;
try{const response=await fetch('payload');if(!response.ok)throw Error('Public artifact unavailable');payload=await response.json();instruction.textContent=JSON.stringify(payload.unsigned.instruction,null,2);observation.textContent=JSON.stringify(payload.unsigned.observation,null,2);button.disabled=false;}catch(e){status.textContent=e.message;}
button.addEventListener('click',async()=>{
 button.disabled=true;
 try{
  const {unsigned,typed}=payload,owner=unsigned.observation.run.context.walletAddress,chainId=unsigned.instruction.sourceChainId;
  if(!['11155111','84532'].includes(chainId))throw Error('Only the two approved testnets are permitted');
  if(BigInt(unsigned.instruction.expiry)<=BigInt(Math.floor(Date.now()/1000)))throw Error('This instruction expired. Prepare a fresh review.');
  const provider=window.ethereum?.providers?.find(p=>p.isMetaMask)||window.ethereum;if(!provider?.isMetaMask)throw Error('Open in your MetaMask-enabled browser.');
  const accounts=await provider.request({method:'eth_requestAccounts'});if(accounts[0]?.toLowerCase()!==owner.toLowerCase())throw Error('Select the dedicated test wallet '+owner);
  const expected='0x'+BigInt(chainId).toString(16);await provider.request({method:'wallet_switchEthereumChain',params:[{chainId:expected}]});
  if(await provider.request({method:'eth_chainId'})!==expected)throw Error('Wallet chain mismatch');
  const current=await provider.request({method:'eth_accounts'});if(current[0]?.toLowerCase()!==owner.toLowerCase())throw Error('Selected account changed');
  const request={...typed,types:{EIP712Domain:[{name:'name',type:'string'},{name:'version',type:'string'},{name:'chainId',type:'uint256'},{name:'verifyingContract',type:'address'}],...typed.types}};
  status.textContent='Review the instruction in MetaMask.';
  const signature=await provider.request({method:'eth_signTypedData_v4',params:[owner,JSON.stringify(request)]});
  const blob=new Blob([JSON.stringify({...unsigned,signature},null,2)+'\n'],{type:'application/json'}),link=document.createElement('a');
  link.href=URL.createObjectURL(blob);link.download='signed.json';link.click();URL.revokeObjectURL(link.href);
  status.textContent='Downloaded signed.json. No transaction was sent. Admit it to the operation journal before starting CRE execution.';
 }catch(e){status.textContent=e.message;button.disabled=false;}
});
