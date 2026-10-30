"""Exercise explicit diff/validate/apply UI using a local fixture draft, not an LLM."""
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18766'
TARGET = ROOT / 'demo/seeknal/transforms/customer_ltv.yml'
DRAFT = ROOT / 'demo/.seeknal/drafts/draft_transform_customer_ltv.yml'
checks = []


def browser(js):
    result = subprocess.run(['agent-browser','--session','seeknal-studio','--json','eval',js],text=True,capture_output=True,check=True,timeout=40)
    response = json.loads(result.stdout)
    assert response['success'], response
    return response['data']['result']


def wait_job():
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        job=browser('studio.snapshot().activeJob')
        if job and job['status'] in {'succeeded','failed','cancelled'}:
            return job
        time.sleep(.25)
    raise AssertionError('job timeout')


original=TARGET.read_text()
assert not DRAFT.exists(), 'refusing to replace an existing draft'
DRAFT.parent.mkdir(parents=True,exist_ok=True)
DRAFT.write_text(original+'\n# Studio draft review acceptance check\n')
try:
    diff=browser('(async()=>{await openDraftReview();return {diff:studio.state.currentDiff,applyDisabled:document.querySelector("#apply-draft").disabled}})()')
    assert diff['applyDisabled'] and 'Studio draft review acceptance check' in diff['diff']['diff'],diff
    checks.append({'name':'real diff; apply blocked before validation','passed':True,'evidence':diff})
    browser('(async()=>{await validateCurrentDraft();return true})()')
    job=wait_job()
    assert job['status']=='succeeded',job
    checks.append({'name':'real engine draft validation','passed':True,'evidence':job})
    deadline=time.monotonic()+10
    while browser('document.querySelector("#apply-draft").disabled') and time.monotonic()<deadline:
        time.sleep(.25)
    assert not browser('document.querySelector("#apply-draft").disabled')
    browser('(async()=>{document.querySelector("#apply-draft").click();return true})()')
    time.sleep(.4)
    job=wait_job()
    assert job['action']=='apply-draft' and job['status']=='succeeded',job
    assert 'Studio draft review acceptance check' in TARGET.read_text() and not DRAFT.exists()
    checks.append({'name':'explicit apply moved reviewed draft','passed':True,'evidence':job})
finally:
    if DRAFT.exists(): DRAFT.unlink()
    TARGET.write_text(original)
    browser('(()=>{document.querySelector("#draft-dialog").close();return true})()')
    (ROOT/'evidence/draft-browser-smoke.json').write_text(json.dumps({'provenance':'locally seeded draft; no LLM/provider run','checks':checks},indent=2))
print(f'Draft review UI: {len(checks)} checks passed; original file restored.')
