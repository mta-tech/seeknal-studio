# Native CLI drafts and agent-panel fixes

Studio now resolves the native CLI from its managed Seeknal 2.14.0 runtime. It does not discover `~/project/mta/signal` by default and does not silently fall back to the editable Seeknal 2.12.0 checkout. An explicit runtime override is supported only when it proves the same 2.14.0 distribution and module origin. See [Seeknal 2.14 managed runtime](runtime-2.14.md).

Select **New draft** in the explorer’s **Drafts** section, or **File → New pipeline node…**, to open the native scaffold form. Choose a node type from the installed Seeknal runtime, enter a name and optional description, and select YAML or a supported Python template. Python dependencies are passed as a single CLI value.

Studio executes the actual command, for example:

```sh
seeknal draft transform paid_orders --description='Paid order totals'
```

The command runs in the selected project and honors its Seeknal templates. Studio never adds `--force`. It refuses existing root or managed draft names and places successful output in `.seeknal/drafts/`. Creating a scaffold never applies a node or runs its pipeline.

1. Open the generated file from the explorer’s **Drafts** section.
2. Edit the YAML.
3. Select **Dry run** in the contextual editor footer. Studio saves the working draft and executes the real `seeknal dry-run` command.
4. Inspect the result, then select **Apply draft** for the exact checked content. The editor follows the applied file and shows its active state.
5. Select **Run pipeline** separately when ready.

Native Seeknal dry-run can report a failed transform preview while returning exit zero. Studio captures the SDK preview result and blocks validation approval for an unavailable transform preview. The native exit code remains recorded separately. Source schema-only checks follow the SDK contract and do not claim that input rows were previewed. The tested positive example uses the declared first input as `input_0` and produces three actual preview rows. Existing named-reference support depends on the installed Seeknal version.

Generating a CSV source definition does not create its input CSV. The observed `UserSourceData`/`source.data` definition still points to the missing `data/data.csv`; that user-data issue is independent of the SDK upgrade. The input must be supplied or the table path corrected before a successful pipeline run. Studio did not invent or replace project data.

Contextual SQL utilities remain available through the editor’s Tools menu. Draft creation belongs in the explorer. Malformed or unsupported draft files are now listed with their errors rather than hiding the entire draft list.

## Agent panel

- A final answer is rendered once per turn, even when it arrives as both an event and the terminal response.
- Repeated polling and repeated completion callbacks do not duplicate the answer or terminal status.
- Large tool output is collapsed but remains available.
- Long CSV/YAML/text wraps inside the panel. The feed owns scrolling; the composer and local actions remain visible.
- Presentation changes do not deduplicate or mutate source data rows.

The reported screenshot was reproduced using isolated synthetic UUID/CSV content. The earlier live chat had already been exited, so no recovered conversation is claimed. The saved source/run-log diagnostic is recorded separately in `evidence/cli-draft/user-error-inspection.json`.

The original CLI/draft receipts and screenshots are under `evidence/cli-draft/`. Final managed-runtime acceptance retains the native YAML and Python scaffolds, save, three-row dry-run preview, exact apply, explicit run, persistent REPL, project menu, post-Apply worker rebind, and agent-answer deduplication behavior on Seeknal 2.14.0. The deterministic end-to-end receipt passes 7/7 checks.

One real DeepSeek turn on the managed 2.14.0 runtime is preserved in `evidence/seeknal-2.14/real-agent-turn-observation.json`. Final replay used that observed event shape without another provider request; it does not claim a new full model turn after the final source hash and does not recover the closed earlier chat.

The rebuilt app remains a local, unsigned development application. Its bundle copies a managed-runtime configuration that points to the external `.runtime/venv`; it does not contain a portable virtual environment. Native package QA passes 2/2 checks: 12 sources plus the runtime configuration match the frozen source, the managed 2.14.0 backend launches, and closing the launcher stops it. See `evidence/seeknal-2.14/package-receipt.json`.
