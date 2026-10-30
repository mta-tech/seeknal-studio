"""Run the real local macOS bundle and prove owned window/service shutdown."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request,urlopen

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from local_access import TOKEN_HEADER,link_secret,read_access_link  # noqa: E402
executable=root/'dist/Seeknal Studio.app/Contents/MacOS/Seeknal Studio'
base='http://127.0.0.1:18808'
process=subprocess.Popen([str(executable),'--port','18808'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
receipt={'bundle':str(executable.parent.parent.parent),'launcher_pid':process.pid}
access={}
startup=''

def request(path,payload=None):
    data=json.dumps(payload).encode() if payload is not None else None
    with urlopen(Request(base+path,data=data,headers={'Content-Type':'application/json',**access}),timeout=10) as response:
        return json.load(response)

try:
    # The launcher names a private file holding its one-time link; it never prints the secret.
    for line in iter(process.stdout.readline,''):
        startup+=line
        link=read_access_link(startup)
        if link:
            access[TOKEN_HEADER]=link_secret(link)
            break
    assert access,'bundle launcher printed no access link file'
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        assert process.poll() is None,'bundle launcher exited during startup'
        try:
            receipt['state']=request('/api/state')
            break
        except (URLError,TimeoutError):
            time.sleep(.2)
    assert 'state' in receipt,'bundle service did not start'
    job=request('/api/jobs',{'action':'run'})
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        job=request('/api/jobs/'+job['id'])
        if job['status'] in {'succeeded','failed','cancelled'}:break
        time.sleep(.25)
    assert job['status']=='succeeded' and job['exit_code']==0,job
    receipt['demo_run']=job
    receipt['preview']=request('/api/preview?node=transform.customer_ltv')
    assert receipt['preview']['row_count']==3 and receipt['preview']['fresh'] is True
    processes=subprocess.check_output(['ps','-axo','pid=,ppid=,comm='],text=True).splitlines()
    children=[line.strip().split(None,2) for line in processes if len(line.strip().split(None,2))==3 and line.strip().split(None,2)[1]==str(process.pid)]
    windows=[int(fields[0]) for fields in children if fields[2].endswith('/webframe')]
    assert len(windows)==1,children
    receipt['window_pid']=windows[0]
    os.kill(windows[0],signal.SIGTERM)
    process.wait(timeout=15)
    assert process.returncode==0,process.returncode
    receipt['launcher_exit']=process.returncode
    try:
        request('/api/state')
        raise AssertionError('owned backend still listening after window closed')
    except URLError:
        receipt['backend_stopped']=True
    print('macOS bundle: native window + real six-node run + three-row preview; closing window stops owned backend.')
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=15)
    receipt['launcher_output']=startup+process.stdout.read()
    process.stdout.close()
    (root/'evidence/mac-app-smoke.json').write_text(json.dumps(receipt,indent=2))
