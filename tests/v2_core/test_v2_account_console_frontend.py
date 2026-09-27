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
const respond=(n,body)=>requests[n].resolve({ok:true,json:async()=>body});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  respond(0,{accounts:[{registry_id:'A',alias:'Alpha',environment:'SANDBOX'},{registry_id:'B',alias:'Beta',environment:'SANDBOX'}]});
  await tick();assert.equal(requests[1].path,'/api/accounts/execution/SANDBOX');
  assert.equal(requests[2].path,'/api/accounts/A/overview');
  respond(1,{phase:'ACTIVE',epoch:1,request_epoch:1,target_registry:'A',blockers:[]});
  await tick();assert.equal(get('route-request').disabled,false);
  assert(get('route-target').textContent.includes('Alpha'));
  get('account').value='B';get('account').listeners.change();await tick();
  assert.equal(requests[4].path,'/api/accounts/B/overview');
  respond(4,{registry_id:'B',summary:{trade_intents:22,settled_trades:2,settled_net_pnl_usdt:'8'},trades:[]});
  await tick();
  respond(2,{registry_id:'A',summary:{trade_intents:999,settled_trades:9,settled_net_pnl_usdt:'999'},trades:[]});
  await tick();assert.equal(get('summary').children[0].children[0].textContent,22);
  assert.equal(requests[5].path,'/api/accounts/B/verification');
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
