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

    def test_diagnostic_decorations_and_stale_responses(self):
        script = r"""
          const assert = require('node:assert/strict');
          const {
            diagnosticsResponseCurrent, nextDiagnosticsState, diagnosticDecorations, splitDecoratedPieces,
            diagnosticLineSeverities, diagnosticAt, offsetToPosition, positionToOffset,
            pointerPosition, diagnosticSummary,
          } = require('./app.js');
          const at = (line, character) => ({ line, character });
          const diag = (start, end, severity = 1, message = 'm') => ({ range: { start, end }, severity, message });
          const content = 'kind: transform\nSELECT a\nFROM b\n\nend';

          // A range over three lines covers the end of the first, all of the middle and the start of the last.
          assert.deepEqual(diagnosticDecorations(content, [diag(at(0, 6), at(2, 4))]), [
            { line: 0, start: 6, end: 15, severity: 1 },
            { line: 1, start: 0, end: 8, severity: 1 },
            { line: 2, start: 0, end: 4, severity: 1 },
          ]);

          // Overlaps split into segments that keep the most severe level; equal neighbours merge.
          assert.deepEqual(diagnosticDecorations(content, [diag(at(1, 0), at(1, 6), 2), diag(at(1, 3), at(1, 8), 1)]), [
            { line: 1, start: 0, end: 3, severity: 2 },
            { line: 1, start: 3, end: 8, severity: 1 },
          ]);
          assert.deepEqual(diagnosticDecorations(content, [diag(at(1, 0), at(1, 4), 3), diag(at(1, 0), at(1, 8), 3)]), [
            { line: 1, start: 0, end: 8, severity: 3 },
          ]);

          // Past the end of a line or the buffer: clamped; empty ranges cover one character.
          assert.deepEqual(diagnosticDecorations(content, [diag(at(2, 2), at(2, 99))]), [{ line: 2, start: 2, end: 6, severity: 1 }]);
          assert.deepEqual(diagnosticDecorations(content, [diag(at(2, 40), at(2, 40))]), [{ line: 2, start: 5, end: 6, severity: 1 }]);
          assert.deepEqual(diagnosticDecorations(content, [diag(at(1, 2), at(1, 2))]), [{ line: 1, start: 2, end: 3, severity: 1 }]);
          assert.deepEqual(diagnosticDecorations(content, [diag(at(3, 0), at(3, 0)), diag(at(9, 0), at(9, 3))]), []);
          assert.deepEqual(diagnosticDecorations(content, [diag(at(4, 1), at(12, 0))]), [{ line: 4, start: 1, end: 3, severity: 1 }]);

          // Syntax pieces are cut at segment boundaries without losing text.
          const pieces = [['SELECT', 'syntax-keyword'], [' a', '']];
          const fragments = splitDecoratedPieces(pieces, [{ line: 1, start: 3, end: 7, severity: 1 }]);
          assert.deepEqual(fragments, [
            { text: 'SEL', className: 'syntax-keyword', segment: -1 },
            { text: 'ECT', className: 'syntax-keyword', segment: 0 },
            { text: ' ', className: '', segment: 0 },
            { text: 'a', className: '', segment: -1 },
          ]);
          assert.equal(fragments.map(item => item.text).join(''), 'SELECT a');

          const severities = diagnosticLineSeverities([diag(at(0, 0), at(1, 1), 2), diag(at(1, 0), at(1, 1), 1), diag(at(3, 0), at(30, 0), 3)], 5);
          assert.deepEqual([...severities.entries()].sort(), [[0, 2], [1, 1], [3, 3], [4, 3]]);

          const items = [diag(at(1, 0), at(1, 6), 2, 'warn'), diag(at(1, 3), at(1, 8), 1, 'err')];
          assert.equal(diagnosticAt(items, at(1, 4)).message, 'err');
          assert.equal(diagnosticAt(items, at(1, 1)).message, 'warn');
          assert.equal(diagnosticAt(items, at(1, 8)).message, 'err');
          assert.equal(diagnosticAt(items, at(2, 0)), null);
          assert.equal(diagnosticAt(items, at(-1, 0)), null);

          assert.deepEqual(offsetToPosition(content, 18), at(1, 2));
          assert.equal(positionToOffset(content, at(1, 2)), 18);
          assert.equal(positionToOffset(content, at(1, 99)), 24);
          assert.equal(positionToOffset(content, at(99, 0)), content.lastIndexOf('\n') + 1);
          assert.deepEqual(pointerPosition({ x: 54, y: 50 }, { left: 10, top: 5, lineHeight: 20, charWidth: 8 }), at(2, 5));
          assert.equal(diagnosticSummary([diag(at(0, 0), at(0, 1), 1), diag(at(0, 0), at(0, 1), 1), diag(at(0, 0), at(0, 1), 2)]), '2 errors · 1 warning');

          // A response is used only for the buffer version and path it was requested for.
          const current = { path: 'seeknal/transforms/a.yml', version: 4 };
          assert.equal(diagnosticsResponseCurrent({ path: 'seeknal/transforms/a.yml', version: 4, diagnostics: [] }, current), true);
          assert.equal(diagnosticsResponseCurrent({ path: 'seeknal/transforms/a.yml', version: 3, diagnostics: [] }, current), false);
          assert.equal(diagnosticsResponseCurrent({ path: 'seeknal/transforms/b.yml', version: 4, diagnostics: [] }, current), false);
          assert.equal(diagnosticsResponseCurrent(null, current), false);

          // "unavailable" keeps the previous diagnostics of the same file and records the reason;
          // they are stale only when they describe different content.
          const previous = { path: current.path, items: [diag(at(0, 0), at(0, 4))], contentHash: 'h1', unavailable: null };
          const kept = nextDiagnosticsState(previous, { path: current.path, version: 4, diagnostics: [], content_sha256: 'h2', unavailable: 'lint timed out after 5 s' }, current);
          assert.deepEqual(kept, { path: current.path, items: previous.items, contentHash: 'h1', stale: true, unavailable: 'lint timed out after 5 s' });
          assert.equal(nextDiagnosticsState(previous, { path: current.path, version: 4, content_sha256: 'h1', unavailable: 'busy' }, current).stale, false);
          assert.equal(nextDiagnosticsState(previous, { path: current.path, version: 4, unavailable: 'request failed' }, current).stale, true);
          const otherFile = nextDiagnosticsState({ ...previous, path: 'seeknal/transforms/b.yml' }, { path: current.path, version: 4, unavailable: 'busy' }, current);
          assert.deepEqual([otherFile.items, otherFile.stale], [[], false]);
          const fresh = nextDiagnosticsState(kept, { path: current.path, version: 4, diagnostics: [], content_sha256: 'h3' }, current);
          assert.deepEqual(fresh, { path: current.path, items: [], contentHash: 'h3', stale: false, unavailable: null });
          assert.equal(nextDiagnosticsState(previous, { path: current.path, version: 3, unavailable: 'busy' }, current), null);
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
