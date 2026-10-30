"""Real-browser and real-engine acceptance checks against an already running demo."""
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18766'
SESSION = 'seeknal-studio'
results = []


def browser(js):
    output = subprocess.run(['agent-browser', '--session', SESSION, '--json', 'eval', js], capture_output=True, text=True, check=True, timeout=40)
    response = json.loads(output.stdout)
    if not response['success']:
        raise RuntimeError(response)
    return response['data']['result']


def api(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(BASE + path, data=data, headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def check(name, condition, evidence):
    results.append({'name': name, 'passed': bool(condition), 'evidence': evidence})
    if not condition:
        raise AssertionError(name + ': ' + str(evidence))


def wait_job():
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        snapshot = browser('window.studio.snapshot()')
        job = snapshot.get('activeJob')
        if job and job['status'] in {'succeeded', 'failed', 'cancelled'}:
            return job
        time.sleep(.25)
    raise AssertionError('job did not complete')


path = 'seeknal/transforms/customer_ltv.yml'
original = api('/api/file?path=' + path)['content']
try:
    browser('(async()=>{await studio.selectNode("transform.customer_ltv");return studio.snapshot()})()')
    snapshot = browser('studio.snapshot()')
    check('linked node selection', snapshot['selectedFile'] == path and snapshot['nodes'] == 6 and snapshot['edges'] == 5, snapshot)
    empty = browser('(()=>{let q=document.querySelector("#graph-search");q.value="no-such-node-zz";q.dispatchEvent(new Event("input",{bubbles:true}));return document.querySelectorAll(".graph-node").length})()')
    check('empty graph search', empty == 0, empty)
    restored = browser('(()=>{document.querySelector("#reset-graph").click();return document.querySelectorAll(".graph-node").length})()')
    check('reset restores full graph', restored == 6, restored)
    browser('(async()=>{await studio.startJob("run");return true})()')
    job = wait_job()
    check('real full run', job['status'] == 'succeeded' and job['exit_code'] == 0, job)
    time.sleep(2)
    preview = api('/api/preview?node=transform.customer_ltv')
    check('real materialized preview', preview['row_count'] == 3 and len(preview['rows']) == 3 and 'lifetime_value' in preview['columns'], preview)
    invalid = original.replace('SUM(amount)', 'SUM(no_such_column)')
    assert invalid != original
    payload = json.dumps(invalid)
    browser(f'(()=>{{let e=document.querySelector("#code-editor");e.value={payload};e.dispatchEvent(new Event("input",{{bubbles:true}}));return studio.snapshot()}})()')
    dirty = browser('studio.snapshot()')
    check('dirty buffer tracked', dirty['dirty'], dirty)
    browser('(async()=>{await studio.save();return studio.snapshot()})()')
    check('editor saved actual file', api('/api/file?path=' + path)['content'] == invalid, {'path': path})
    browser('(async()=>{await studio.startJob("run");return true})()')
    failed = wait_job()
    check('invalid SQL fails visibly', failed['status'] == 'failed' and failed['exit_code'] != 0, failed)
    browser(f'(async()=>{{let e=document.querySelector("#code-editor");e.value={json.dumps(original)};e.dispatchEvent(new Event("input",{{bubbles:true}}));await studio.save();return studio.snapshot()}})()')
    browser('(async()=>{await studio.startJob("run");return true})()')
    recovered = wait_job()
    check('restored pipeline recovers', recovered['status'] == 'succeeded' and recovered['exit_code'] == 0, recovered)
    check('agent honestly disconnected', not browser('studio.snapshot()')['agentConfigured'], api('/api/agent/status'))
finally:
    current = api('/api/file?path=' + path)
    if current['content'] != original:
        api('/api/file', {'path': path, 'content': original, 'base_hash': current['content_hash']})
    (ROOT / 'evidence/browser-smoke.json').write_text(json.dumps(results, indent=2))
print(json.dumps({'passed': len(results), 'failed': sum(not item['passed'] for item in results)}, indent=2))
