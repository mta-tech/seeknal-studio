from pathlib import Path
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EditorGroupsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app.js").read_text()
        cls.html = (ROOT / "index.html").read_text()
        cls.css = (ROOT / "styles.css").read_text()

    def run_node(self, body: str):
        harness = textwrap.dedent(
            f"""
            const assert = require('assert');
            const {{
              createEditorGroupState, editorOpen, editorClose, editorSplit,
              editorMove, editorMoveActive, editorMerge
            }} = require({str(ROOT / 'app.js')!r});
            {body}
            """
        )
        result = subprocess.run(
            ["node"], input=harness, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_open_split_move_close_and_merge_preserve_unique_ownership(self):
        self.run_node(
            """
            const model = createEditorGroupState();
            assert.deepEqual(model.groups.left.tabs, ['graph']);
            assert.equal(model.groups.right.tabs.length, 0);
            editorOpen(model, 'yaml');
            assert.deepEqual(model.groups.left.tabs, ['graph', 'yaml']);
            editorSplit(model);
            editorOpen(model, 'repl');
            assert.equal(model.owners.repl, 'right');
            assert.equal(model.groups.left.active, 'yaml');
            assert.equal(model.groups.right.active, 'repl');
            editorMove(model, 'yaml', 'right');
            assert.equal(model.owners.yaml, 'right');
            assert.equal(model.groups.left.tabs.includes('yaml'), false);
            assert.equal(model.groups.right.tabs.filter(v => v === 'yaml').length, 1);
            assert.equal(editorClose(model, 'yaml', {dirty:true}), false);
            assert.equal(model.owners.yaml, 'right');
            assert.equal(editorClose(model, 'yaml'), true);
            assert.equal(model.owners.yaml, undefined);
            editorMerge(model);
            assert.equal(model.split, false);
            assert.deepEqual(model.groups.right.tabs, []);
            assert.equal(model.owners.repl, 'left');
            assert.equal(new Set(model.groups.left.tabs).size, model.groups.left.tabs.length);
            """
        )

    def test_move_active_opens_target_group_and_keeps_independent_active_panes(self):
        self.run_node(
            """
            const model = createEditorGroupState();
            editorOpen(model, 'yaml');
            assert.equal(editorMoveActive(model, 'right'), true);
            assert.equal(model.split, true);
            assert.equal(model.groups.left.active, 'graph');
            assert.equal(model.groups.right.active, 'yaml');
            assert.equal(model.owners.graph, 'left');
            assert.equal(model.owners.yaml, 'right');
            editorOpen(model, 'logs', 'right');
            assert.equal(model.groups.left.active, 'graph');
            assert.equal(model.groups.right.active, 'logs');
            assert.equal(editorMoveActive(model, 'left'), true);
            assert.equal(model.owners.logs, 'left');
            assert.equal(model.groups.right.active, 'yaml');
            """
        )

    def test_dom_has_two_dynamic_groups_and_singleton_tool_pages(self):
        self.assertEqual(self.html.count('class="editor-tabstrip"'), 2)
        self.assertNotIn('class="tab active"', self.html)
        self.assertNotIn('class="tab" type="button" data-view=', self.html)
        for element_id in (
            "graph-canvas",
            "code-editor",
            "job-logs",
            "repl-editor",
            "configuration-page",
        ):
            self.assertEqual(self.html.count(f'id="{element_id}"'), 1, element_id)
        self.assertIn('id="editor-group-divider"', self.html)
        self.assertIn('role="separator"', self.html)
        self.assertIn('data-group-action="split-right"', self.html)
        self.assertIn('data-group-action="move-left"', self.html)
        self.assertIn('data-group-action="move-right"', self.html)

    def test_runtime_moves_singleton_nodes_and_handles_drag_resize_and_graph_reflow(self):
        self.assertIn("content.append(element)", self.app)
        self.assertIn('setData("text/x-seeknal-view", view)', self.app)
        self.assertIn('getData("text/x-seeknal-view")', self.app)
        self.assertIn('state.editorGroups.ratio = Math.max(25, Math.min(75', self.app)
        self.assertIn('new ResizeObserver(() => requestAnimationFrame(renderEdges))', self.app)
        self.assertIn('if (view === "yaml" && state.dirty)', self.app)
        self.assertIn('function isViewVisible(view)', self.app)
        self.assertIn('ready && isViewVisible("repl")', self.app)
        self.assertIn('await openFile(file.path); setMainView("yaml", groupId)', self.app)
        self.assertIn('selectNode(node.id).then(() => setMainView("yaml", groupId))', self.app)
        self.assertIn('setMainView("repl", groupId)', self.app)
        self.assertGreaterEqual(self.app.count("const handle = event.currentTarget"), 3)
        self.assertNotIn('event.currentTarget.removeEventListener("pointermove"', self.app)
        self.assertNotIn("event.currentTarget.setPointerCapture", self.app)
        self.assertIn('close = document.createElement("button")', self.app)
        self.assertIn('tab.setAttribute("aria-controls", editorViewPanelIds[view])', self.app)
        self.assertIn('element.setAttribute("aria-labelledby", `editor-tab-${owner}-${view}`)', self.app)
        self.assertIn('["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)', self.app)
        self.assertIn('item.setAttribute("role", "presentation")', self.app)
        self.assertIn('$(`#editor-tab-${groupId}-${nextView}`)?.focus()', self.app)
        self.assertIn('.editor-tabstrip{', self.css)
        self.assertIn('overflow-x:auto', self.css)


if __name__ == "__main__":
    unittest.main()
