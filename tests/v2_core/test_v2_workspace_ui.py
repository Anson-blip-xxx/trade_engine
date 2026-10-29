"""Shared application shell contracts, navigation and URL-only context."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("page", ["dashboard", "accounts", "strategy"])
def test_pages_use_one_shared_shell(page):
    html = (ROOT / "web" / ("v2_" + page) / "index.html").read_text()
    assert f'data-page="{page}"' in html
    assert html.count('id="atlas-nav"') == html.count('id="atlas-header"') == 1
    assert html.count('href="/accounts/workspace.css"') == 1
    assert html.count('src="/accounts/workspace.js"') == 1
    assert html.index("workspace.js") < html.index(
        'src="/app.js"'
        if page == "dashboard"
        else f'src="/accounts/{"strategy/" if page == "strategy" else ""}app.js"'
    )


def test_context_navigation_tabs_and_untrusted_values():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
 constructor(){this.children=[];this.dataset={};this.attributes={};this.listeners={};this.textContent='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
 setAttribute(k,v){this.attributes[k]=v;}
 addEventListener(k,v){this.listeners[k]=v;}
 focus(){this.focused=true;}
 set innerHTML(_){throw new Error('unsafe HTML');}
}
function boot(url,page='accounts'){
 const nodes={},events={},get=id=>nodes[id]??(nodes[id]=new Element());
 const collect=(el)=>[el,...el.children.flatMap(collect)];
 const window={location:new URL(url),addEventListener:(key,fn)=>events[key]=fn};
 window.history={replaceState:(_,__,next)=>window.location=new URL(next,window.location)};
 const ctx={window,URLSearchParams,document:{body:{dataset:{page}},getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>Object.values(nodes).flatMap(collect).filter(e=>e.dataset.workspacePath)}};
 vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
 return {ctx,get,window,events,all:()=>Object.values(nodes).flatMap(collect)};
}
const id='00000000-0000-4000-8000-000000000001',second='00000000-0000-4000-8000-000000000002';
const a=boot(`https://example.invalid/accounts?account=${id}&environment=LIVE&days=7#credentials`);
assert.equal(a.window.Atlas.requested.account,id);
assert.equal(a.all().filter(x=>x.dataset.workspacePath==='/accounts/strategy').length,2); // desktop + mobile
for(const link of a.all().filter(x=>x.dataset.workspacePath)) {const u=new URL(link.href,'https://example.invalid');assert.equal(u.searchParams.get('account'),id);assert.equal(u.searchParams.get('environment'),'LIVE');assert.equal(u.searchParams.get('days'),'7');}
a.window.Atlas.describe('<img onerror=attack>','LIVE');assert(a.get('atlas-context').textContent.includes('<img onerror=attack>'));
a.window.Atlas.setContext({environment:'SANDBOX',account:second,days:'30'});
assert.equal(a.window.location.searchParams.get('account'),second);assert.equal(a.window.location.hash,'#credentials');
a.window.Atlas.tabs(['overview','execution','credentials'],'overview');
assert(!a.get('panel-credentials').hidden);assert.equal(a.get('tab-credentials').attributes['aria-selected'],'true');
a.get('tab-credentials').listeners.keydown({key:'Home',preventDefault(){}});assert(a.get('panel-credentials').hidden);assert(a.get('tab-overview').focused);assert.equal(a.window.location.searchParams.get('account'),second);
const route=a.window.Atlas.href('/accounts/strategy'),b=boot(new URL(route,'https://example.invalid').href,'strategy');
assert.equal(b.window.Atlas.requested.account,second);assert.equal(b.window.Atlas.requested.days,'30');
const invalid=boot('https://example.invalid/accounts?account=bad%3Cscript%3E&environment=EVIL&days=999&api_key=not-real');
assert(invalid.window.Atlas.requested.invalid);assert.equal(invalid.window.Atlas.requested.account,'');
invalid.window.Atlas.setContext({account:second});
assert(!invalid.window.location.search.includes('api_key'));assert(!invalid.window.location.search.includes('EVIL'));
assert(!fs.readFileSync(process.argv[1],'utf8').includes('localStorage'));assert(!fs.readFileSync(process.argv[1],'utf8').includes('sessionStorage'));
"""
    subprocess.run(
        ["node", "-e", script, str(ROOT / "web/v2_workspace/app.js")],
        check=True,
        timeout=15,
    )
