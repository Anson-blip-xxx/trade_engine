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
  replaceChildren(...items){this.children=items;if(items[0]?.value)this.value=items[0].value;}
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
  respond(0,{accounts:[{registry_id:'A',alias:'Alpha'},{registry_id:'B',alias:'Beta'}]});
  await tick();assert.equal(requests[1].path,'/api/accounts/A/overview');
  get('account').value='B';get('account').listeners.change();await tick();
  assert.equal(requests[2].path,'/api/accounts/B/overview');
  respond(2,{registry_id:'B',summary:{trade_intents:22,settled_trades:2,settled_net_pnl_usdt:'8'},trades:[]});
  await tick();
  respond(1,{registry_id:'A',summary:{trade_intents:999,settled_trades:9,settled_net_pnl_usdt:'999'},trades:[]});
  await tick();assert.equal(get('summary').children[0].children[0].textContent,22);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    app = Path(__file__).resolve().parents[2] / "web/v2_accounts/app.js"
    subprocess.run([node, "-e", script, str(app)], check=True, timeout=10)
