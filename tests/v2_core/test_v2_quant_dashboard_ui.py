"""Execute actual dashboard rendering in a DOM stub; no HTTP or venue access."""

import subprocess
from pathlib import Path


def test_quant_source_rendering_and_html_escaping():
    script = Path(__file__).resolve().parents[2] / "web/v2_dashboard/app.js"
    result = subprocess.run(
        [
            "node",
            "-e",
            r"""
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const nodes={};
const get=id=>nodes[id] ||= {addEventListener(){},classList:{remove(){}},setAttribute(){}};
const sandbox={document:{getElementById:get,querySelectorAll:()=>[],addEventListener(){}},
  window:{addEventListener(){}},fetch:()=>new Promise(()=>{}),setInterval(){},
  AbortController,setTimeout(){},clearTimeout(){}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
vm.runInContext(`renderSignals({signals:[{source:'quant',symbol:'BTCUSDT',signal:'QUANT_DONCHIAN_UP',decisions:[]}]});
renderAccounting({signal_funnel:[{source:'quant',received:1,processed:1,intents:1,filled:1}]});`,sandbox);
assert.match(nodes['signal-list'].innerHTML,/QUANT · 量化候选/);
assert.match(nodes['signal-funnel'].innerHTML,/QUANT · 量化候选/);
vm.runInContext(`renderSignals({signals:[{source:'<img>',symbol:'BTCUSDT',signal:'test',decisions:[]}]});`,sandbox);
assert.match(nodes['signal-list'].innerHTML,/&lt;img&gt;/);
assert.ok(!nodes['signal-list'].innerHTML.includes('<img>'));
""",
            str(script),
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
