from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function_body(source: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {re.escape(name)}\([^)]*\) \{{", source)
    if not match:
        raise AssertionError(f"missing function {name}")
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
    raise AssertionError(f"unterminated function {name}")


class SavedWorkspacesFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app.js").read_text()
        cls.html = (ROOT / "index.html").read_text()

    def run_node(self, source: str):
        result = subprocess.run(["node"], input=source, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workspace_current_and_open_reasons_come_from_registry_state(self):
        self.run_node(f"""
const assert = require('assert');
const state = {{project:{{project_path:'/fallback'}},workspaces:{{editingId:null,forgetId:null,formSnapshot:null}}}};
{function_body(self.app, 'normalizeWorkspaces')}
{function_body(self.app, 'workspaceIsCurrent')}
{function_body(self.app, 'workspaceOpenReason')}
state.workspaces = normalizeWorkspaces({{current:{{id:'a',path:'/a'}},entries:[],managed:true,can_switch:true}});
assert.equal(workspaceIsCurrent({{id:'a',path:'/old'}}), true);
assert.equal(workspaceOpenReason({{id:'b',path:'/gone',available:false,reason:'Moved',can_switch:false}}), 'Moved');
state.workspaces.managed = false; state.workspaces.can_switch = false;
assert.match(workspaceOpenReason({{id:'b',path:'/b',available:true,can_switch:false}}), /managed Studio launcher/);
state.workspaces.managed = true; state.workspaces.can_switch = true; state.workspaces.savePending = true;
assert.match(workspaceOpenReason({{id:'b',path:'/b',available:true,can_switch:true}}), /being saved/);
state.workspaces.savePending = false; state.workspaces.forgetPending = 'x';
assert.match(workspaceOpenReason({{id:'b',path:'/b',available:true,can_switch:true}}), /being forgotten/);
""")

    def test_busy_work_is_a_hard_block_before_any_discard_confirmation(self):
        self.run_node(f"""
const assert = require('assert');
let active = true;
const hasActiveWork = () => active;
{function_body(self.app, 'workspaceSwitchBlockReason')}
assert.match(workspaceSwitchBlockReason(), /Finish or cancel/);
active = false; assert.equal(workspaceSwitchBlockReason(), '');
""")

    def test_dirty_warning_collection_preserves_workspace_form(self):
        self.run_node(f"""
const assert = require('assert');
const elements = {{'#workspace-form':{{hidden:false}},'#workspace-id':{{value:'w'}},'#workspace-name-input':{{value:'Renamed'}},'#workspace-path-input':{{value:'/a'}}}};
const $ = selector => elements[selector];
const state = {{dirty:false,workspaces:{{formSnapshot:{{id:'w',name:'Original',path:'/a'}}}}}};
const hasUnsavedEnvironmentInputs = () => false;
{function_body(self.app, 'hasUnsavedWorkspaceForm')}
{function_body(self.app, 'workspaceDiscardWarnings')}
assert.deepEqual(workspaceDiscardWarnings(), ['The workspace name or project path form has unsaved changes.']);
assert.equal(elements['#workspace-name-input'].value, 'Renamed');
""")

    def test_open_posts_registry_id_and_restarts_at_backend_path(self):
        self.assertIn('id="workspace-menu-trigger"', self.html)
        self.assertIn('class="configuration-card workspaces-card"', self.html)
        self.run_node(f"""
const assert = require('assert');
let posted, restarted;
const state={{workspaces:{{openPending:null,savePending:false,forgetPending:null}}}}, renderWorkspaces=()=>{{}}, setWorkspaceFormDisabled=()=>{{}};
const workspaceStatus=()=>{{}}, setStatus=()=>{{}}, loadWorkspaces=async()=>{{}};
const api=async(path,options)=>{{posted={{path,body:options.body}};return {{workspace:{{name:'Canonical',path:'/canonical'}}}};}};
const awaitProjectRestart=path=>{{restarted=path;}}, setTimeout=callback=>callback();
{function_body(self.app, 'workspaceOperationPending')}
{function_body(self.app, 'admitWorkspaceOpen')}
(async()=>{{await admitWorkspaceOpen({{id:'opaque-id',name:'Typed',path:'/typed'}});assert.deepEqual(posted,{{path:'/api/workspaces/open',body:{{id:'opaque-id'}}}});assert.equal(restarted,'/canonical');}})().catch(error=>{{console.error(error);process.exit(1);}});
""")

    def test_switch_dialog_and_menu_keyboard_contract(self):
        self.assertIn('id="workspace-switch-dialog"', self.html)
        self.assertIn('aria-labelledby="workspace-switch-title"', self.html)
        self.assertIn('value="confirm"', self.html)
        navigation = function_body(self.app, "navigateWorkspaceMenu")
        submenu = function_body(self.app, "navigateSavedWorkspaceSubmenu")
        for key in ("ArrowDown", "ArrowUp", "Home", "End", "Escape"):
            self.assertIn(key, navigation)
            self.assertIn(key, submenu)
        self.assertIn('closeWorkspaceMenu({ restoreFocus: true })', navigation)
        self.assertIn('aria-haspopup="menu" aria-expanded="false">Saved Workspaces', self.html)
        self.assertIn('id="workspace-name-input" type="text" autocomplete="off" maxlength="100"', self.html)

    def test_open_reservation_rejects_second_admission(self):
        self.run_node(f"""
const assert = require('assert');
let posts=0, release;
const state={{workspaces:{{openPending:null,savePending:false,forgetPending:null}}}}, renderWorkspaces=()=>{{}}, setWorkspaceFormDisabled=()=>{{}}, workspaceStatus=()=>{{}}, setStatus=()=>{{}}, loadWorkspaces=async()=>{{}};
const api=()=>{{posts+=1;return new Promise(resolve=>{{release=resolve;}});}}, setTimeout=()=>{{}}, awaitProjectRestart=()=>{{}};
{function_body(self.app, 'workspaceOperationPending')}
{function_body(self.app, 'admitWorkspaceOpen')}
(async()=>{{state.workspaces.savePending=true;await admitWorkspaceOpen({{id:'blocked-save',name:'Blocked',path:'/blocked'}});assert.equal(posts,0);state.workspaces.savePending=false;state.workspaces.forgetPending='w';await admitWorkspaceOpen({{id:'blocked-forget',name:'Blocked',path:'/blocked'}});assert.equal(posts,0);state.workspaces.forgetPending=null;const first=admitWorkspaceOpen({{id:'b',name:'B',path:'/b'}});const second=admitWorkspaceOpen({{id:'c',name:'C',path:'/c'}});await Promise.resolve();assert.equal(posts,1);assert.equal(state.workspaces.openPending.id,'b');release({{workspace:{{id:'b',name:'B',path:'/b'}}}});await Promise.all([first,second]);}})().catch(error=>{{console.error(error);process.exit(1);}});
""")

    def test_timeout_unload_authorization_and_submenu_escape_runtime(self):
        restart = function_body(self.app, "awaitProjectRestart")
        unload = function_body(self.app, "guardWorkspaceUnload")
        submenu = function_body(self.app, "navigateSavedWorkspaceSubmenu")
        self.run_node(f"""
const assert = require('assert');
const state={{dirty:true,workspaces:{{openPending:{{id:'b',discardConfirmed:true}},unloadAuthorized:false}}}};
let mode='success', reloads=0, disabled=null, rendered=0, shown='';
const api=async()=>{{if(mode==='timeout')throw new Error('offline');return {{current:{{path:'/b'}}}};}};
const window={{location:{{reload:()=>{{reloads+=1;}}}}}}, setTimeout=()=>{{}}, setWorkspaceFormDisabled=value=>{{disabled=value;}}, renderWorkspaces=()=>{{rendered+=1;}}, showError=value=>{{shown=String(value);}};
const hasUnsavedEnvironmentInputs=()=>false, hasUnsavedWorkspaceForm=()=>true;
{restart}
{unload}
(async()=>{{
  await awaitProjectRestart('/b',0,'b'); assert.equal(reloads,1); assert.equal(state.workspaces.unloadAuthorized,true);
  let prevented=0; const event={{preventDefault:()=>{{prevented+=1;}},returnValue:null}};
  assert.equal(guardWorkspaceUnload(event),false); assert.equal(prevented,0); assert.equal(state.workspaces.unloadAuthorized,false);
  assert.equal(guardWorkspaceUnload(event),true); assert.equal(prevented,1);
  mode='timeout'; state.workspaces.openPending={{id:'b',discardConfirmed:true}}; state.workspaces.unloadAuthorized=true;
  await awaitProjectRestart('/b',120,'b'); assert.equal(state.workspaces.openPending,null); assert.equal(state.workspaces.unloadAuthorized,false); assert.equal(disabled,false); assert.equal(rendered,1); assert.match(shown,/could not reconnect/);
  const menu={{hidden:false}}, parent={{expanded:'true',focused:false,setAttribute:(key,value)=>{{parent.expanded=value;}},focus:()=>{{parent.focused=true;}}}};
  const $=selector=>selector==='#recent-project-menu'?menu:parent, $$=()=>[];
  let stopped=0, defaulted=0;
  {submenu}
  navigateSavedWorkspaceSubmenu({{key:'Escape',preventDefault:()=>{{defaulted+=1;}},stopPropagation:()=>{{stopped+=1;}}}});
  assert.equal(stopped,1); assert.equal(defaulted,1); assert.equal(menu.hidden,true); assert.equal(parent.expanded,'false'); assert.equal(parent.focused,true);
}})().catch(error=>{{console.error(error);process.exit(1);}});
""")

    def test_save_and_forget_have_per_operation_reservations(self):
        save = function_body(self.app, "saveWorkspace")
        forget = function_body(self.app, "requestForgetWorkspace")
        self.assertIn("if (workspaceOperationPending()) return", save)
        self.assertIn("state.workspaces.savePending = true", save)
        self.assertIn("setWorkspaceFormDisabled(true)", save)
        self.assertIn("if (workspaceOperationPending()) return", forget)
        self.run_node(f"""
const assert = require('assert');
let calls=0, release;
const state={{workspaces:{{forgetPending:null,forgetId:'w',savePending:false,openPending:null}}}};
const renderWorkspaces=()=>{{}}, setWorkspaceFormDisabled=()=>{{}}, workspaceStatus=()=>{{}}, loadWorkspaces=async()=>{{}};
const api=()=>{{calls+=1;return new Promise(resolve=>{{release=resolve;}});}};
{function_body(self.app, 'workspaceOperationPending')}
{forget}
(async()=>{{const workspace={{id:'w',name:'W'}};const first=requestForgetWorkspace(workspace);const second=requestForgetWorkspace(workspace);await Promise.resolve();assert.equal(calls,1);assert.equal(state.workspaces.forgetPending,'w');release({{removed:true,id:'w'}});await Promise.all([first,second]);assert.equal(state.workspaces.forgetPending,null);}})().catch(error=>{{console.error(error);process.exit(1);}});
""")

    def test_workspace_refresh_preserves_visible_form_values(self):
        load = function_body(self.app, "loadWorkspaces")
        activate = function_body(self.app, "activateEditorView")
        toggle = function_body(self.app, "toggleWorkspaceMenu")
        self.assertIn("void loadWorkspaces()", activate)
        self.assertIn("await loadWorkspaces()", toggle)
        self.run_node(f"""
const assert = require('assert');
const fields={{name:'Typed name',path:'/typed/path'}};
const state={{workspaces:{{editingId:'w',forgetId:null,formSnapshot:{{id:'w',name:'Old',path:'/old'}},openPending:null,confirmTarget:null,savePending:false,forgetPending:null}}}};
const api=async()=>({{current:{{id:'w',name:'Backend',path:'/backend'}},entries:[],managed:true,can_switch:true}});
const workspaceStatus=()=>{{}}, renderWorkspaces=()=>{{}};
{function_body(self.app, 'normalizeWorkspaces')}
{load}
(async()=>{{await loadWorkspaces();assert.deepEqual(fields,{{name:'Typed name',path:'/typed/path'}});assert.equal(state.workspaces.formSnapshot.name,'Old');}})().catch(error=>{{console.error(error);process.exit(1);}});
""")


if __name__ == "__main__":
    unittest.main()
