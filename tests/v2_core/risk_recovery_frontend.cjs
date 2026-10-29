const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
  constructor(){this.value='';this.textContent='';this.children=[];this.listeners={};this.checked=false;}
  addEventListener(k,f){this.listeners[k]=f;}
  replaceChildren(...v){this.children=v;}
  append(v){this.children.push(v);}
}
const nodes={},events={},requests=[];
const get=id=>nodes[id]??(nodes[id]=new Element());
let confirmations=0, sequence=0;
const ctx={document:{getElementById:get,createElement:()=>new Element()},
  window:{addEventListener:(k,f)=>events[k]=f,confirm:()=>{confirmations++;return true;}},
  crypto:{randomUUID:()=>`request-${++sequence}`},
  fetch:(path,args)=>new Promise(resolve=>requests.push({path,args,resolve})),console,Date};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),ctx);
const tick=()=>new Promise(r=>setImmediate(r));
const response=(i,data,ok=true)=>requests[i].resolve({ok,json:async()=>data});
const view=(id,alias)=>events['atlas-account-view']({detail:{id,alias}});
const preview=(id,eligible=true)=>({registry_id:id,eligible,blockers:eligible?[]:['NOT_CURRENT_EXECUTOR'],
  health:{base:'977',peak:'1040',drawdown:'.06',recovery:{mode:'HALTED'}},proof:{epoch:1,account:id},
  probe_factor:'.2',max_losses:2,loss_fraction:'.005',halt_drawdown:'.1',min_settlements:3,
  active_drawdown:'.03',checked_at_ms:1000,expires_at_ms:Date.now()+300000,history:[]});
const submit=()=>get('risk-form').listeners.submit({preventDefault(){}});
(async()=>{
  view('A','Alpha');assert.equal(requests.length,1);
  view('B','Beta');response(1,preview('B',false));await tick();
  response(0,preview('A'));await tick();
  assert(get('risk-status').textContent.includes('Beta'));assert(get('risk-form').hidden);
  await submit();assert.equal(requests.length,2); // stale eligible response cannot authorize other account
  view('A','<img src=x onerror=alert(1)>');response(2,preview('A'));await tick();
  assert(!get('risk-form').hidden);assert(get('risk-status').textContent.includes('<img'));
  assert.equal(get('risk-status').children.length,0); // text-only alias
  get('risk-reason').value='QA explicit authorization';await submit();assert.equal(requests.length,3);
  get('risk-ack').checked=true;void submit();await tick();assert.equal(requests.length,4);
  assert.equal(confirmations,1);void submit();assert.equal(requests.length,4); // click lock
  const first=JSON.parse(requests[3].args.body);
  assert.equal(requests[3].args.headers['X-V2-Action'],'account-management');
  response(3,{error:'ACCOUNT_SERVICE_UNAVAILABLE'},false);await tick();
  get('risk-reason').value='changed text after network failure';void submit();await tick();
  assert.deepEqual(JSON.parse(requests[4].args.body),first);assert.equal(confirmations,1);
  // While POST is in flight change view; an old success must not overwrite the new account.
  view('B','Beta');response(5,preview('B',false));await tick();
  response(4,{status:'PROBE_AUTHORIZED',at_ms:1000});await tick();
  assert(get('risk-status').textContent.includes('Beta'));assert.equal(get('risk-result').textContent,'');
  assert(get('risk-apply').disabled);
  view('A','Alpha');response(6,preview('A'));await tick();
  get('risk-reason').value='QA another confirmation';get('risk-ack').checked=true;
  void submit();await tick();response(7,{status:'PROBE_AUTHORIZED',at_ms:1000});await tick();
  assert(get('risk-result').textContent.includes('没有直接创建订单'));assert.equal(requests[8].args.method,'GET');
  response(8,{...preview('A',false),health:{...preview('A').health,recovery:{mode:'PROBE'}},blockers:['NOT_HALTED']});await tick();
  assert(get('risk-form').hidden);assert(get('risk-status').textContent.includes('试运行中'));
  view('A','Alpha');
  response(9,{...preview('A',false),health:{...preview('A').health,recovery:{mode:'OBSERVE_ONLY'}},blockers:['NOT_HALTED']});await tick();
  assert(get('risk-status').textContent.includes('回撤仅记录'));
  assert(get('risk-budget').textContent.includes('单笔止损'));
  assert(!get('risk-budget').textContent.includes('再次硬停'));
  assert(get('risk-form').hidden);
})().catch(e=>{console.error(e);process.exitCode=1;});
