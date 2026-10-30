"""Exercise Studio's actual frontend in native Webframe, not Chromium."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
binary = root.parent / 'webframe-evaluation' / 'bin' / 'webframe'
commands = [
    'new Promise((resolve,reject)=>{let n=0;let check=()=>{if(window.studio&&studio.snapshot().nodes===6)resolve(studio.snapshot());else if(n++>100)reject(new Error("Studio readiness timeout"));else setTimeout(check,200)};check()})',
    'studio.selectNode("transform.customer_ltv").then(()=>({selected:studio.snapshot().selectedNode,file:studio.snapshot().selectedFile,previewRows:studio.state.preview?.rows.length,previewFresh:studio.state.preview?.fresh}))',
    'studio.startJob("validate").then(()=>({job:studio.state.activeJob.id,status:studio.state.activeJob.status}))',
    'new Promise((resolve,reject)=>{let n=0;let check=()=>{let j=studio.state.activeJob;if(j&&["succeeded","failed","cancelled"].includes(j.status))resolve({status:j.status,exit:j.exit_code,logs:j.logs});else if(n++>200)reject(new Error("job timeout"));else setTimeout(check,200)};check()})',
]
result = subprocess.run([str(binary), '-c', 'http://127.0.0.1:18766'], input='\n'.join(commands)+'\n', capture_output=True, text=True, timeout=65)
receipt = {'binary':str(binary),'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
(root/'evidence/native-smoke.json').write_text(json.dumps(receipt,indent=2))
assert result.returncode == 0, receipt
lines = result.stdout.splitlines()
assert len(lines) == 4, receipt
parsed = [json.loads(line) for line in lines]
assert parsed[0]['nodes'] == 6 and parsed[0]['edges'] == 5, parsed
assert parsed[1]['previewRows'] == 3 and parsed[1]['previewFresh'] is True, parsed
assert parsed[-1]['status'] == 'succeeded' and parsed[-1]['exit'] == 0, parsed
print('Native Webframe: loaded 6-node graph, linked real 3-row output, validated pipeline successfully.')
