import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EditorFlowChecks(unittest.TestCase):
    def test_runtime_model_covers_active_and_draft_lifecycle(self):
        script = r"""
          const assert = require('node:assert/strict');
          const {
            createEditorFlowState, editorFlowAction, editorFlowTransition,
            nextRequestGeneration, matchesValidationContext, matchesApplyContext,
            rebaseEditorAfterApply, rebaseDirtyDraftAfterPrepare,
            captureDraftContext,
          } = require('./app.js');

          let active = { ...createEditorFlowState(), kind: 'active', buffer: 'saved' };
          assert.deepEqual(editorFlowAction(active, false), { label: 'Dry run changes', action: null });

          active = editorFlowTransition(active, 'edit', { buffer: 'changed', dirty: true });
          assert.equal(active.phase, 'dirty');
          assert.deepEqual(editorFlowAction(active, true), { label: 'Dry run changes', action: 'dry-run' });

          const approval = { path: '.seeknal/drafts/.editor-a.yml', contentHash: 'h1', buffer: 'changed' };
          active = editorFlowTransition(active, 'validation-start', { approval });
          assert.equal(active.phase, 'validating');
          assert.equal(editorFlowAction(active, true).action, null);

          active = editorFlowTransition(active, 'validation-finish', { success: true, buffer: 'changed' });
          assert.equal(active.phase, 'passed');
          assert.deepEqual(editorFlowAction(active, true), { label: 'Save changes', action: 'apply' });

          const saving = editorFlowTransition(active, 'apply-start');
          assert.deepEqual(editorFlowAction(saving, true), { label: 'Saving changes…', action: null });

          const admissionRejected = editorFlowTransition({ ...active, busy: true }, 'failure');
          assert.equal(admissionRejected.phase, 'failed');
          assert.equal(admissionRejected.approval, null);
          assert.equal(admissionRejected.busy, false);
          assert.equal(editorFlowAction(admissionRejected, true).action, 'dry-run');

          active = editorFlowTransition(active, 'edit', { buffer: 'changed again', dirty: true });
          assert.equal(active.approval, null);
          assert.equal(active.phase, 'dirty');
          assert.equal(editorFlowAction(active, true).action, 'dry-run');

          active = editorFlowTransition(active, 'validation-start', { approval: { ...approval, buffer: 'changed again' } });
          active = editorFlowTransition(active, 'validation-finish', { success: false, buffer: 'changed again', message: 'Transform preview failed' });
          assert.equal(active.phase, 'failed');
          assert.equal(active.approval, null);
          assert.equal(active.validationMessage, 'Transform preview failed');
          assert.equal(editorFlowAction(active, true).action, 'dry-run');

          let draft = { ...createEditorFlowState(), kind: 'draft', buffer: 'draft body' };
          assert.deepEqual(editorFlowAction(draft, false), { label: 'Dry run', action: 'dry-run' });
          draft = editorFlowTransition(draft, 'validation-start', { approval: { path: '.seeknal/drafts/new.yml', contentHash: 'd1', buffer: 'draft body' } });
          draft = editorFlowTransition(draft, 'validation-finish', { success: true, buffer: 'draft body', message: 'Definition checked; no data preview' });
          assert.equal(draft.validationMessage, 'Definition checked; no data preview');
          assert.deepEqual(editorFlowAction(draft, false), { label: 'Apply draft', action: 'apply' });
          assert.deepEqual(editorFlowAction(editorFlowTransition(draft, 'apply-start'), false), { label: 'Applying draft…', action: null });

          const generation = { value: 0 };
          assert.equal(nextRequestGeneration(generation), 1);
          assert.equal(nextRequestGeneration(generation), 2);
          assert.equal(nextRequestGeneration(generation), 3);

          const validation = { sourcePath: 'seeknal/a.yml', preparedPath: '.seeknal/drafts/.editor-a.yml', contentHash: 'h1' };
          const validatingFlow = { approval: { path: validation.preparedPath, contentHash: 'h1', buffer: 'changed' } };
          assert.equal(matchesValidationContext(validation, validatingFlow, 'seeknal/a.yml', 'changed'), true);
          assert.equal(matchesValidationContext(validation, validatingFlow, 'seeknal/b.yml', 'changed'), false);
          assert.equal(matchesValidationContext(validation, validatingFlow, 'seeknal/a.yml', 'newer'), false);

          const applied = { sourcePath: '.seeknal/drafts/new.yml', bufferAtApply: 'draft body' };
          assert.equal(matchesApplyContext(applied, '.seeknal/drafts/new.yml', 'draft body'), true);
          assert.equal(matchesApplyContext(applied, 'seeknal/other.yml', 'draft body'), false);

          const rebased = rebaseEditorAfterApply({ destination: 'seeknal/transforms/new.yml', bufferAtApply: 'applied' }, 'newer edit');
          assert.equal(rebased.selectedFile, 'seeknal/transforms/new.yml');
          assert.equal(rebased.originalContent, 'applied');
          assert.equal(rebased.dirty, true);
          assert.equal(rebased.flow.kind, 'active');
          assert.equal(rebased.flow.approval, null);

          assert.deepEqual(
            rebaseDirtyDraftAfterPrepare('draft', 'prepared body', 'newer body'),
            { originalContent: 'prepared body', dirty: true, buffer: 'newer body' },
          );
          assert.equal(rebaseDirtyDraftAfterPrepare('active', 'prepared body', 'newer body'), null);

          const groups = { activeGroup: 'left' };
          const draftContext = captureDraftContext(groups, 'seeknal/a.yml');
          groups.activeGroup = 'right';
          assert.deepEqual(draftContext, { groupId: 'left', selectedFile: 'seeknal/a.yml' });
        """
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
