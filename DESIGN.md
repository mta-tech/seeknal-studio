# Design

## Source of truth
Status: Active. Refreshed 2026-10-29. Primary surface: desktop Seeknal Studio.
Evidence: design/reference.png approved by user with “lets build this IDE”; prior workbench and Seeknal integration evaluation.

## Brand
Precise, calm, technical. Trust through real files, jobs, outputs and explicit provenance. Avoid fake connected states, decorative dashboards and neon gradients.

## Product goals
Prompt-assisted pipeline creation and first-class code, lineage and execution tools in one workspace. First release local-only. Success means actual file save, engine execution, inspected data, cancel/recover and understandable errors. Signed distribution and LSP are future work.

## Personas and jobs
Data engineers inspect dependencies, edit YAML/SQL, validate, execute, inspect output, and collaborate with an agent.

## Information architecture
Five singleton tools can open as editor tabs: Graph, YAML, Logs, SQL REPL, and Configuration. Graph is the only initial tab. The activity rail launches a tool into the active editor group instead of duplicating a permanent navigation row. A horizontal split supports independent active tabs on the left and right, so Graph can remain visible beside YAML, Logs, or SQL REPL. Moving and dragging a tab transfers ownership of the same live DOM tool; it never clones editor buffers, REPL sessions, job authority, or element IDs. The right agent workspace remains available across groups.

## Design principles
Files remain source of truth. Proposed work differs from executed work. Context follows graph selection. Real runtime state wins over illustrative mockup content.

## Visual language
Charcoal #181b1d / #222729; offwhite type; mint #69dbb0 active/success; amber #f4bd59 pending; red errors. Avenir/Segoe UI and native monospace. 4px spacing scale, 6px radii, thin neutral borders. Small functional inline SVG icons, subtle dotted canvas; minimal motion.

## Components
Titlebar, activity rail, dynamic per-group tabstrips, resizable editor-group divider, resizable file tree, movable graph cards and edges, focusable YAML editor, job/log browser, resizable SQL/result workbench, configuration cards, agent message/activity list, and composer. CSS variables own tokens. Explicit loading/empty/error/dirty/stale/connected states. Closing modified YAML is blocked until it is saved; tab moves preserve the live buffer.

Graph nodes can be moved by pointer or keyboard and their positions persist per canonical project path. Fit reframes saved positions; Auto layout clears manual positions; selecting a node updates context, while Open YAML (or double click) moves to its real source file.

Materialized data is not embedded in the pipeline editor. The project-bound Seeknal SQL REPL is the single data-exploration surface; its query/results divider is resizable and results can take full focus.

Environment editing is field-level: the browser receives keys and `has_value`, never stored values. Blank secret fields preserve existing values, deletion is explicit, and saves use the current content hash so concurrent changes fail closed.

The desktop shell also owns a conventional File/View/Run menu, a saved-workspace switcher, and a first-class project-bound SQL REPL tab. A workspace is a saved display name plus one canonical local Seeknal project directory. The titlebar switcher provides Quick Open; Configuration owns adding, renaming, repairing paths, opening, and forgetting registrations. Editing a registration never switches the current project. Forget removes only Studio metadata and explicitly says that project files remain. Unavailable directories stay visible with their backend diagnostic and an Edit path action.

Workspace identity and switch authority come from the backend registry. The browser carries the opaque workspace ID returned by that registry and never constructs or trusts a project identity from a typed path. Managed Studio can open a saved available workspace through the launcher and waits for the restarted backend to report the expected canonical path before reloading. Standalone Studio can manage registrations but disables Open with a reason. Modified YAML, unsaved environment fields, and project-bound jobs, agent turns, or REPL operations block switching with a precise status; backend busy checks remain authoritative.

Pipeline creation belongs in the Explorer: Pipeline files and Drafts are separate sections, malformed drafts remain visible with their errors, and New draft opens the real Seeknal CLI scaffold dialog. Opening a draft goes directly to the editor; a diff modal is optional review context, never the required editing path. AI SQL assistance and Send SQL to REPL are contextual editor utilities, not creation or lifecycle stages.

The YAML editor uses one guided workflow with explicit document identity and content-bound approval:

- An unchanged active pipeline file is quiet: `Saved`, focus, and contextual utilities only.
- Editing an active pipeline file shows a sticky `Unsaved changes` callout whose primary action is `Dry run changes`. Dry run first prepares an immutable draft snapshot through the backend, then starts the existing native validation job. The active file is not written.
- A successful validation is authorized only for the exact prepared path and content hash. It changes the primary action to `Save changes`, which applies that hash-bound snapshot through the existing apply job. Failure, cancellation, or any later edit clears approval while preserving the buffer.
- An unchanged draft is labeled `Draft · not applied` and offers `Dry run`. A modified draft is persisted through the same prepare boundary with `original_content` as an optimistic-concurrency gate, then validated natively.
- A successfully validated draft offers `Apply draft`. After apply, the destination file becomes the selected editor document, its tab and Explorer selection synchronize, and the editor returns to `Saved · Active` without a stale Apply action.
- Non-pipeline files retain ordinary Save. ⌘S follows the guided action for active pipeline YAML (dry run when unvalidated, save changes when the exact buffer passed) and never writes an unvalidated pipeline buffer directly.

Validation is never inferred from parsing, time, or a button click. The browser records `validating`, `passed`, `failed`, and `cancelled` only from the real job lifecycle, with the pending prepared path and hash latched together. Busy action reservations prevent duplicate prepare, validate, and apply submissions.

## Accessibility
Target WCAG AA: labeled controls, visible focus, keyboard save/escape, usable contrast, semantic tables/status, reduced motion support. Graph nodes are keyboard buttons.

## Responsive behavior
Desktop 1280–1600 main target; below 1200 the agent panel moves behind its toggle and configuration cards stack. Editor groups, the YAML explorer, and REPL query/results support pointer and keyboard resizing. Split tools compact their headers and use intentional horizontal overflow at their useful minimum widths instead of clipping actions. YAML and REPL results offer explicit focus modes, and Escape always restores the full workspace.

## Interaction states
Loading holds clear progress; empty is explicit; errors preserve useful logs; success requires exit zero; previous REPL results are labeled stale after edits or output changes. Disconnected agent remains usable for connection diagnosis but cannot pretend to generate.

## Content voice
Short factual labels: Validate, Run pipeline, Cancel, Preview, Review changes. Never claim graph/code sync or agent connectivity until backed by engine response.

## Implementation constraints
Existing stdlib Python and vanilla browser stack, no new frontend dependencies. Loopback service, fixed selected project root, validated paths, server-side credentials, job lifecycle independent of HTTP. Browser + native Webframe smoke checks and screenshot evidence required.

## Open questions
- Signed/notarized installers and automatic updates are beyond this local delivery.
- Full language-server support needs a separate component decision.
