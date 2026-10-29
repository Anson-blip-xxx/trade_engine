"""Small executable JS race test; not a substitute for authenticated browser QA."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_switching_view_discards_late_previous_account_response():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for frontend race test")
    script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
class Element {
  constructor(){this.value='';this.textContent='';this.children=[];this.listeners={};}
  replaceChildren(...items){this.children=items;this.value=items[0]?.value || '';}
  append(item){this.children.push(item);}
  addEventListener(kind,fn){this.listeners[kind]=fn;}
}
const elements={}, requests=[];
const get=id=>elements[id]??(elements[id]=new Element());
const ctx={document:{getElementById:get,createElement:()=>new Element()},
  Option:function(text,value){this.textContent=text;this.value=value;},
  fetch:(path)=>new Promise(resolve=>requests.push({path,resolve})), console};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const respond=(n,body)=>requests[n===0 ? 0 : n+1].resolve({ok:true,json:async()=>body});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  respond(0,{accounts:[{registry_id:'B',alias:'Beta',environment:'SANDBOX'},{registry_id:'A',alias:'Alpha',environment:'SANDBOX'}]});
  await tick();assert.equal(requests[1].path,'/api/accounts/execution/SANDBOX');
  requests[1].resolve({ok:true,json:async()=>({phase:'ACTIVE',target_registry:'A'})});
  await tick();
  assert.equal(requests[3].path,'/api/accounts/A/overview');
  respond(1,{phase:'ACTIVE',epoch:1,request_epoch:1,target_registry:'A',blockers:[]});
  await tick();assert.equal(get('route-request').disabled,false);
  assert(get('route-target').textContent.includes('Alpha'));
  get('account').value='B';get('account').listeners.change();await tick();
  assert.equal(requests[5].path,'/api/accounts/B/overview');
  respond(4,{registry_id:'B',summary:{trade_intents:22,settled_trades:2,settled_net_pnl_usdt:'-6.590414845000000000000000000000'},trades:[]});
  await tick();
  respond(2,{registry_id:'A',summary:{trade_intents:999,settled_trades:9,settled_net_pnl_usdt:'999'},trades:[]});
  await tick();assert.equal(get('summary').children[0].children[0].textContent,22);
  assert.equal(get('summary').children[2].children[0].textContent,'-6.59');
  assert.equal(get('summary').children[2].children[0].className,'negative');
  assert.equal(requests[6].path,'/api/accounts/B/verification');
  get('environment').value='LIVE';get('environment').listeners.change();await tick();
  // Browser clears a select's value when its options are removed.
  assert.equal(get('account').children.length,0);
  respond(5,{outcome:'SIGNED_READ_ACCEPTED',registry_id:'B',fresh:true});await tick();
  assert(!get('verification').textContent.includes('验证通过'));
  respond(3,{phase:'ACTIVE',epoch:1,request_epoch:1,target_registry:'A',blockers:[]});
  await tick();assert.equal(get('route-request').disabled,true);
  respond(6,{phase:'BLOCKED',epoch:0,blockers:['LIVE_DEPLOYMENT_NOT_APPROVED']});
  await tick();assert.equal(get('route-request').disabled,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    app = Path(__file__).resolve().parents[2] / "web/v2_accounts/app.js"
    subprocess.run([node, "-e", script, str(app)], check=True, timeout=10)


def test_credential_drafts_and_late_creation_do_not_replace_new_view():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required")
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
 constructor(){this.value='';this.children=[];this.listeners={};this.disabled=false;}
 replaceChildren(...items){this.children=items;this.value=items[0]?.value||'';}
 append(item){this.children.push(item);}
 addEventListener(k,f){this.listeners[k]=f;}
 querySelector(){return this.button??(this.button=new Element());}
 reset(){for(const e of Object.values(this.elements||{}))e.value='';}
}
const nodes={},requests=[],events={};let confirm=false;
const get=id=>nodes[id]??(nodes[id]=new Element());get('environment').value='SANDBOX';
for(const id of ['add','rotate'])get(id).elements={alias:{value:''},environment:{value:''},api_key:{value:''},api_secret:{value:''}};
const ctx={document:{getElementById:get,createElement:()=>new Element()},window:{confirm:()=>confirm,addEventListener:(k,f)=>events[k]=f},
 Option:function(t,v){this.textContent=t;this.value=v;},crypto:{randomUUID:()=> 'qa-request'},
 FormData:class{constructor(form){this.items=Object.entries(form.elements).map(([k,v])=>[k,v.value]);} [Symbol.iterator](){return this.items[Symbol.iterator]();}},
 fetch:(path,args)=>new Promise(resolve=>requests.push({path,args,resolve})),console};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const tick=()=>new Promise(r=>setImmediate(r)),respond=(i,data)=>requests[i].resolve({ok:true,json:async()=>data});
(async()=>{
 respond(0,{accounts:[{registry_id:'A',alias:'Alpha',environment:'SANDBOX',binding_version:1,alias_version:0},{registry_id:'B',alias:'Beta',environment:'SANDBOX',binding_version:2,alias_version:3}]});await tick();
 respond(1,{phase:'ACTIVE',target_registry:'A'});await tick();
 respond(2,{phase:'ACTIVE',epoch:1,target_registry:'A'});respond(3,{registry_id:'A',summary:{trade_intents:0,settled_trades:0,settled_net_pnl_usdt:'0'},trades:[]});await tick();
 respond(4,{outcome:'NOT_CHECKED'});await tick();assert.equal(get('summary').children[2].children[0].textContent,'—');
 get('rotate').elements.api_secret.value='qa-dummy-secret';get('account').value='B';get('account').listeners.change();
 assert.equal(get('account').value,'A');assert.equal(requests.length,5);assert.equal(get('rotate').elements.api_secret.value,'qa-dummy-secret');
 let guarded=false;events.beforeunload({preventDefault(){guarded=true;}});assert(guarded);
 get('rotate').elements.api_secret.value='';
 const form=get('add');form.elements.alias.value='new account';form.elements.environment.value='SANDBOX';form.elements.api_key.value='qa-dummy-key';form.elements.api_secret.value='qa-dummy-secret';
 form.listeners.submit({preventDefault(){},currentTarget:form});await tick();
 assert.equal(requests[5].path,'/api/accounts');const body=JSON.parse(requests[5].args.body);
 assert.equal(body.environment,'SANDBOX');assert.equal(body.api_key,'qa-dummy-key');assert.equal(form.elements.api_key.value,'');assert.equal(form.elements.api_secret.value,'');
 confirm=true;get('account').value='B';get('account').listeners.change();await tick();
 respond(6,{phase:'ACTIVE',epoch:1,target_registry:'A'});respond(7,{registry_id:'B',summary:{trade_intents:0,settled_trades:0,settled_net_pnl_usdt:'0'},trades:[]});await tick();respond(8,{outcome:'NOT_CHECKED'});await tick();
 form.elements.api_key.value='new-unsent-draft';respond(5,{registry_id:'C'});await tick();
 assert.equal(get('account').value,'B');assert.equal(form.elements.api_key.value,'new-unsent-draft');assert.equal(requests.length,9);assert(!form.querySelector().disabled);
 form.reset();get('alias').value='Renamed Beta';get('rename').listeners.submit({preventDefault(){},currentTarget:get('rename')});await tick();
 assert.equal(requests[9].path,'/api/accounts/B/alias');assert.equal(JSON.parse(requests[9].args.body).expected_version,3);
 get('account').value='A';get('account').listeners.change();await tick();respond(9,{alias_version:4});await tick();
 assert.equal(get('account').value,'A');assert.equal(get('alias').value,'Alpha');assert.equal(requests.length,12);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    app = Path(__file__).resolve().parents[2] / "web/v2_accounts/app.js"
    subprocess.run([node, "-e", script, str(app)], check=True, timeout=10)
