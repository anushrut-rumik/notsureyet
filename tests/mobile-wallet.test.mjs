import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const wallet='0x66051Eef2e7DD1b410C6E68EeDF8ABBddB783Ee1';
function boot(accounts=[wallet],options={}){
 const nodes=new Map();const calls=[];const api=[];
 function element(){return {textContent:'',hidden:false,disabled:false,children:[],append(...items){this.children.push(...items);},value:''};}
 const document={getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},createElement:element};
 let chain='0x76edaf',freshPermission=false;
 const ethereum={isMetaMask:true,async request(request){
  calls.push(request);
  if(request.method==='wallet_switchEthereumChain'){chain=request.params[0].chainId;return null;}
  if(request.method==='wallet_requestPermissions'){
   assert.equal(chain,options.kind==='arkiv'?'0x76edaf':'0xaa36a7','select the signing chain before refreshing account access');
   if(options.permissionError)throw options.permissionError;
   freshPermission=true;return [{parentCapability:'eth_accounts'}];
  }
  if(request.method==='eth_requestAccounts')return accounts;
  if(request.method==='eth_accounts')return options.permitted??(freshPermission?accounts:[]);
  if(request.method==='personal_sign'){
   if(!freshPermission)throw Object.assign(Error('Invalid parameters: must provide an Ethereum address.'),{code:-32602});
   if(options.signError)throw options.signError;
   return '0xsignature';
  }
  if(request.method==='eth_chainId')return chain;
  if(request.method==='eth_sendTransaction')return '0x'+'ab'.repeat(32);
  throw Error('Unexpected RPC '+request.method);
 }};
 const context={document,window:{ethereum},location:{pathname:'/connect/test',host:'wallet.example'},TextEncoder,
  localStorage:{getItem(){return null;},setItem(){},removeItem(){}},
  async fetch(url,fetchOptions){const body=fetchOptions.body?JSON.parse(fetchOptions.body):null;api.push([url,body]);
   let data=options.kind==='arkiv'?{kind:'arkiv',claimed:false,payment:{sender:wallet,recipient:'0x'+'44'.repeat(20),amount:'0',asset:'tGLM',chain_id:7794095,max_fee_wei:'1000000000000000',status:'external_prepared'}}:{kind:'connect',completed:false,expected_address:wallet};
   if(options.kind==='ccip')data={kind:'payment',claimed:false,payment:{kind:'ccip',phase:'send',sender:wallet,recipient:'0x'+'12'.repeat(20),amount:'1',asset:'CCIP-BnM',chain_id:11155111,max_fee_wei:'100000000000000',status:'awaiting_signature',destination:'Base Sepolia',sender_contract:'0x'+'45'.repeat(20),ccip_fee_eth:'0.0001',ccip_fee_cap_eth:'0.00011'}};
   if(options.kind==='ccip_deployment')data={kind:'payment',claimed:false,payment:{kind:'ccip_deployment',sender:wallet,recipient:'0x'+'12'.repeat(20),amount:'0',asset:'ETH',chain_id:11155111,max_fee_wei:'100000000000000',status:'awaiting_signature'}};
   if(url.endsWith('/challenge'))data={message:'Connect this wallet to Ira',address:wallet};
   else if(url.endsWith('/connect'))data={connected:true,address:wallet};
   else if(url.endsWith('/progress'))data={recorded:true};
   else if(url.endsWith('/claim'))data={transaction:{from:wallet,...(options.kind==='ccip_deployment'?{}:{to:'0x'+'44'.repeat(20)}),chainId:['ccip','ccip_deployment'].includes(options.kind)?'0xaa36a7':'0x76edaf',data:'0x1234'}};
   else if(url.endsWith('/result'))data={saved:true};
   return {ok:true,json:async()=>data};
  }};
 vm.createContext(context);vm.runInContext(readFileSync('agent/wallet_web/mobile-wallet.js','utf8'),context);
 return {context,nodes,calls,api};
}
test('mobile connection refreshes permissions on Sepolia before signing the exact message',async()=>{
 const app=boot();
 await vm.runInContext('init()',app.context);
 await app.nodes.get('action').onclick();
 const sign=app.calls.find(c=>c.method==='personal_sign');
 assert.equal(sign.params[1],wallet.toLowerCase());
 assert.equal(sign.params[0],'0x'+Buffer.from('Connect this wallet to Ira').toString('hex'));
 const methods=app.calls.map(c=>c.method);
 assert.ok(methods.indexOf('wallet_switchEthereumChain')<methods.indexOf('wallet_requestPermissions'));
 assert.ok(methods.indexOf('wallet_requestPermissions')<methods.indexOf('eth_requestAccounts'));
 assert.ok(methods.indexOf('eth_accounts')<methods.indexOf('personal_sign'));
 assert.equal(app.api.find(([url])=>url.endsWith('/challenge'))[1].chain_id,11155111);
 assert.ok(app.api.some(([url])=>url.endsWith('/connect')));
 assert.match(app.nodes.get('message').textContent,/Connected/);
});
test('CCIP deployment is explicit contract creation on Sepolia using the existing signer',async()=>{
 const app=boot([wallet],{kind:'ccip_deployment'});
 await vm.runInContext('init()',app.context);
 assert.equal(app.nodes.get('title').textContent,'Deploy CCIP sender');
 assert.match(app.nodes.get('intro').textContent,/deployment gas/);
 assert.match(app.nodes.get('action').textContent,/contract deployment/);
 await app.nodes.get('action').onclick();
 const sends=app.calls.filter(c=>c.method==='eth_sendTransaction');
 assert.equal(sends.length,1);
 assert.equal(sends[0].params[0].chainId,'0xaa36a7');
 assert.equal(sends[0].params[0].to,undefined);
 assert.equal(sends[0].params[0].data,'0x1234');
});
test('an account returned by connect cannot sign without current account access',async()=>{
 const app=boot([wallet],{permitted:[]});
 await vm.runInContext('init()',app.context);await app.nodes.get('action').onclick();
 assert.equal(app.calls.filter(c=>c.method==='personal_sign').length,0);
 assert.equal(app.api.filter(([url])=>url.endsWith('/connect')).length,0);
 assert.match(app.nodes.get('message').textContent,/has not granted/);
});
test('permission rejection stops setup without signing or saving a connection',async()=>{
 const app=boot([wallet],{permissionError:Object.assign(Error('User rejected account access'),{code:4001})});
 await vm.runInContext('init()',app.context);await app.nodes.get('action').onclick();
 assert.equal(app.calls.filter(c=>c.method==='personal_sign').length,0);
 assert.equal(app.api.filter(([url])=>url.endsWith('/connect')).length,0);
 assert.match(app.nodes.get('message').textContent,/User rejected/);
});
test('signing permission error records failure and never pretends the wallet connected',async()=>{
 const app=boot([wallet],{signError:Object.assign(Error('Invalid parameters: must provide an Ethereum address.'),{code:-32602})});
 await vm.runInContext('init()',app.context);await app.nodes.get('action').onclick();
 assert.equal(app.calls.filter(c=>c.method==='personal_sign').length,1);
 assert.equal(app.api.filter(([url])=>url.endsWith('/connect')).length,0);
 assert.match(app.nodes.get('message').textContent,/Disconnect this site/);
 assert.ok(app.api.some(([url,body])=>url.endsWith('/progress')&&body.stage==='wallet_error'&&body.error_code===-32602));
});
test('malformed wallet account never reaches personal_sign',async()=>{
 const app=boot(['not-an-address']);
 await vm.runInContext('init()',app.context);
 await app.nodes.get('action').onclick();
 assert.equal(app.calls.filter(c=>c.method==='personal_sign').length,0);
 assert.match(app.nodes.get('message').textContent,/did not return an Ethereum account/);
});
test('Arkiv review selects Rabdi, refreshes permissions and sends only the prepared checkpoint',async()=>{
 const app=boot([wallet],{kind:'arkiv'});
 await vm.runInContext('init()',app.context);await app.nodes.get('action').onclick();
 const send=app.calls.find(c=>c.method==='eth_sendTransaction');
 assert.ok(send,app.nodes.get('message').textContent);assert.equal(send.params[0].chainId,'0x76edaf');
 assert.equal(send.params[0].from,wallet);
 assert.equal(app.nodes.get('title').textContent,'Save task to Arkiv');
 const methods=app.calls.map(c=>c.method);
 assert.ok(methods.indexOf('wallet_switchEthereumChain')<methods.indexOf('wallet_requestPermissions'));
 assert.equal(methods.filter(m=>m==='eth_sendTransaction').length,1);
 assert.ok(app.api.some(([url])=>url.endsWith('/result')));
});
test('Arkiv account permission failure prevents any wallet send',async()=>{
 const app=boot([wallet],{kind:'arkiv',permitted:[]});
 await vm.runInContext('init()',app.context);await app.nodes.get('action').onclick();
 assert.ok(!app.calls.some(c=>c.method==='eth_sendTransaction'));
});

test('CCIP review shows amount, recipient, destination and native fees before the single signature',async()=>{
 const app=boot([wallet],{kind:'ccip',permitted:[wallet]});
 await vm.runInContext('init()',app.context);
 const text=app.nodes.get('details').children.flatMap(dl=>dl.children.map(n=>n.textContent)).join(' ');
 for(const value of ['1 CCIP-BnM','0x'+'12'.repeat(20),'Base Sepolia','0.0001 ETH','0.00011 ETH','Send cross-chain transfer'])assert.ok(text.includes(value),text);
 assert.equal(app.calls.filter(c=>c.method==='eth_sendTransaction').length,0);
 await app.nodes.get('action').onclick();
 const sends=app.calls.filter(c=>c.method==='eth_sendTransaction');
 assert.equal(sends.length,1);assert.equal(sends[0].params[0].data,'0x1234');
 assert.equal(sends[0].params[0].chainId,'0xaa36a7');
});
