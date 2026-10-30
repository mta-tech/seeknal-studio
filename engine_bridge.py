#!/usr/bin/env python3
"""Small JSON bridge executed with Seeknal's Python environment."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import hashlib
from pathlib import Path
from typing import Any


def draft_options() -> dict:
    """Return draft capabilities from the installed Seeknal runtime."""
    from seeknal.ask.agents.tools.apply_draft import _KIND_DIR_MAP
    from seeknal.workflow.draft import NODE_TYPES, PYTHON_TEMPLATE_FILES

    # NODE_TYPES maps CLI aliases (feature-group, feature_group) to one kind;
    # offer each kind once, under its hyphenated CLI spelling when it has one.
    spellings: dict[str, str] = {}
    for value, normalized in sorted(NODE_TYPES.items()):
        preferred = normalized.replace("_", "-")
        if normalized not in spellings or value == preferred:
            spellings[normalized] = value
    supported = []
    excluded = []
    for value, normalized in sorted((value, normalized) for normalized, value in spellings.items()):
        item = {
            "value": value,
            "normalized": normalized,
            "python_supported": normalized in PYTHON_TEMPLATE_FILES,
        }
        if normalized in _KIND_DIR_MAP:
            supported.append(item)
        else:
            excluded.append({
                **item,
                "reason": "installed draft resolver cannot map this kind to a managed target",
            })
    return {
        "node_types": supported,
        "excluded_node_types": excluded,
    }


def graph(project: Path) -> dict:
    from seeknal.cli.main import _build_manifest_from_dag
    from seeknal.workflow.dag import DAGBuilder
    from seeknal.workflow.state import load_state

    builder = DAGBuilder(project_path=project)
    builder.build()
    manifest = _build_manifest_from_dag(builder, project.name)
    state = load_state(project / "target" / "run_state.json")
    states = state.nodes if state else {}
    nodes = []
    for node_id, node in manifest.nodes.items():
        item_state = states.get(node_id)
        if not node.file_path:
            raise ValueError(f"manifest node has no file path: {node_id}")
        path = Path(node.file_path).resolve().relative_to(project).as_posix()
        nodes.append({
            "id": node_id,
            "name": node.name,
            "kind": node.node_type.value,
            "path": path,
            "description": node.description,
            "columns": list(node.columns),
            "row_count": item_state.row_count if item_state else None,
            "status": item_state.status if item_state else "not_run",
        })
    return {
        "nodes": sorted(nodes, key=lambda item: item["id"]),
        "edges": [
            {"source": edge.from_node, "target": edge.to_node}
            for edge in manifest.edges
        ],
    }


def preview(project: Path, node_id: str) -> dict:
    import duckdb

    kind, separator, name = node_id.partition(".")
    if not separator or not kind.replace("_", "").isalnum() or not name.replace("_", "").isalnum():
        raise ValueError("invalid node identifier")
    candidates = [
        project / "target" / "intermediate" / f"{kind}_{name}.parquet",
        project / "target" / "cache" / kind / f"{name}.parquet",
    ]
    target = next(
        (
            path
            for path in candidates
            if path.is_file()
            and not path.is_symlink()
            and project in path.resolve().parents
        ),
        None,
    )
    if target is None:
        raise FileNotFoundError(f"no output for {node_id}; run the pipeline first")
    escaped = str(target).replace("'", "''")
    connection = duckdb.connect(":memory:")
    try:
        cursor = connection.execute(f"SELECT * FROM read_parquet('{escaped}') LIMIT 100")
        columns = [entry[0] for entry in cursor.description]
        rows = cursor.fetchall()
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM read_parquet('{escaped}')"
        ).fetchone()
        if count_row is None:
            raise RuntimeError("preview row count query returned no result")
        row_count = count_row[0]
    finally:
        connection.close()
    return {
        "node": node_id,
        "columns": columns,
        "rows": [[str(value) if value is not None else None for value in row] for row in rows],
        "row_count": row_count,
        "output_path": target.relative_to(project).as_posix(),
    }


def _canonical_project_path(project: Path, lexical: Path, message: str) -> Path:
    """Reject symlinked lexical paths before returning a canonical project path."""
    project = project.resolve()
    if lexical.is_symlink():
        raise ValueError(message)
    resolved = lexical.resolve()
    if project not in resolved.parents or resolved != lexical.absolute():
        raise ValueError(message)
    return resolved


def draft_details(project: Path, relative: str) -> dict:
    from seeknal.ask.agents.tools.apply_draft import _resolve_python_target, _resolve_yaml_target

    draft_lexical = project / relative
    draft = _canonical_project_path(
        project, draft_lexical, "draft path is outside .seeknal/drafts or uses a symlink",
    )
    drafts_root = (project / ".seeknal" / "drafts").resolve()
    if drafts_root not in draft.parents or draft.suffix not in {".yml", ".yaml", ".py"}:
        raise ValueError("draft path is outside .seeknal/drafts")
    if not draft.is_file():
        raise ValueError("draft file is missing or unsafe")
    content = draft.read_text()
    if draft.suffix == ".py":
        kind, name, target = _resolve_python_target(draft, content, project)
    else:
        kind, name, target = _resolve_yaml_target(content, project)
    target = _canonical_project_path(
        project, target, "resolved target is outside project or uses a symlink",
    )
    return {"kind": kind, "name": name, "destination": target.relative_to(project).as_posix()}


def editor_details(project: Path, relative: str, content: str) -> dict:
    """Resolve editor content with the SDK's canonical draft target rules."""
    from seeknal.ask.agents.tools.apply_draft import _resolve_python_target, _resolve_yaml_target

    source_lexical = project / relative
    source = _canonical_project_path(
        project, source_lexical, "editor path is outside supported Seeknal files or uses a symlink",
    )
    active_root = (project / "seeknal").resolve()
    drafts_root = (project / ".seeknal" / "drafts").resolve()
    if (
        (active_root not in source.parents and drafts_root not in source.parents)
        or source.suffix not in {".yml", ".yaml", ".py"}
    ):
        raise ValueError("editor path is outside supported Seeknal files")
    if source.suffix == ".py":
        kind, name, target = _resolve_python_target(source, content, project)
    else:
        kind, name, target = _resolve_yaml_target(content, project)
    target = _canonical_project_path(
        project, target, "resolved target is outside project or uses a symlink",
    )
    return {"kind": kind, "name": name, "destination": target.relative_to(project).as_posix()}


def validate_draft(project: Path, relative: str) -> dict:
    """Run the SDK YAML dry-run while retaining its structured preview result."""
    import seeknal.workflow.dry_run as native_dry_run

    details = draft_details(project, relative)
    draft = _canonical_project_path(project, project / relative, "draft source became unsafe")
    if draft.suffix not in {".yml", ".yaml"}:
        raise ValueError("structured draft validation currently requires YAML")

    captured: dict[str, object] = {}
    native_execute_preview = native_dry_run.execute_preview

    def capture_preview(*args: Any, **kwargs: Any) -> Any:
        result = native_execute_preview(*args, **kwargs)
        captured["result"] = result
        return result

    setattr(native_dry_run, "execute_preview", capture_preview)
    try:
        native_dry_run.dry_run_yaml(draft, relative, 10, 30, False)
    finally:
        setattr(native_dry_run, "execute_preview", native_execute_preview)

    result = captured.get("result")
    kind = details["kind"]

    def nonnegative_number(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0

    if not isinstance(result, dict):
        preview_available = False
        validation_passed = False
        preview_status = "unavailable"
        reason = "The SDK returned no trustworthy preview outcome"
        message = None
    elif kind == "transform" and isinstance(result.get("preview_available"), bool):
        preview_available = result["preview_available"] is True
        validation_passed = preview_available
        preview_status = "available" if preview_available else "unavailable"
        reason = None if preview_available else "The SDK could not execute this pipeline preview"
        message = None
    elif kind == "source" and isinstance(result.get("schema_only"), bool):
        preview_available = result["schema_only"] is False
        validation_passed = True
        preview_status = "available" if preview_available else "schema_only"
        reason = None
        message = None if preview_available else "Definition checked; no data preview"
    elif kind == "profile" and isinstance(result.get("preview_available"), bool):
        preview_available = result["preview_available"] is True
        validation_passed = True
        preview_status = "available" if preview_available else "definition_only"
        reason = None
        message = None if preview_available else "Definition checked; no data preview"
    else:
        definition_markers = {
            "feature_group": "feature_count",
            "model": "model_info",
            "aggregation": "aggregation_info",
            "rule": "rule_info",
            "exposure": "exposure_info",
        }
        marker = definition_markers.get(kind)
        common_shape = (
            nonnegative_number(result.get("duration"))
            and nonnegative_number(result.get("row_count"))
        )
        marker_valid = (
            nonnegative_number(result.get(marker))
            if marker == "feature_count"
            else marker is not None and result.get(marker) is True
        )
        preview_available = False
        validation_passed = common_shape and marker_valid
        preview_status = "definition_only" if validation_passed else "unavailable"
        reason = None if validation_passed else "The SDK returned an unrecognized preview outcome"
        message = "Definition checked; no data preview" if validation_passed else None
    return {
        "kind": kind,
        "preview_available": preview_available,
        "preview_status": preview_status,
        "validation_passed": validation_passed,
        "validation_error": reason,
        "validation_message": message,
        "preview": result,
    }


def extract_sql(project: Path, relative: str, content: str) -> dict:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ParseError
    from sqlglot.optimizer.scope import Scope, traverse_scope
    import yaml

    target = (project / relative).resolve()
    seeknal_root = (project / "seeknal").resolve()
    if (
        seeknal_root not in target.parents
        or target.suffix not in {".yml", ".yaml"}
        or target.is_symlink()
    ):
        raise ValueError("transform path is unsafe")
    value = yaml.safe_load(content)
    if not isinstance(value, dict) or value.get("kind") != "transform":
        raise ValueError("editor content is not a Seeknal transform")
    if value.get("name") != target.stem:
        raise ValueError("transform name does not match the editor file")
    sql = value.get("transform")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("transform SQL is missing")
    inputs = value.get("inputs", [])
    if not isinstance(inputs, list):
        raise ValueError("transform inputs must be a list")
    refs = []
    for item in inputs:
        ref = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(ref, str) or not re.fullmatch(r"[a-z_]+\.[A-Za-z0-9_-]+", ref):
            raise ValueError("transform input ref is invalid")
        refs.append(ref)
    replacements = {ref: ref.replace(".", "_") for ref in refs}
    replacements.update({f"input_{index}": ref.replace(".", "_") for index, ref in enumerate(refs)})
    try:
        expression = sqlglot.parse_one(sql, read="duckdb")
    except ParseError as error:
        raise ValueError(f"transform SQL could not be parsed: {error}") from error
    scopes = traverse_scope(expression)
    if scopes is None:
        raise ValueError("transform SQL must be a query")
    rewritten = []
    seen = set()
    for scope in scopes:
        for table in scope.tables or []:
            if isinstance(scope.sources.get(table.alias_or_name), Scope):
                continue
            original = f"{table.db}.{table.name}" if table.db else table.name
            replacement = replacements.get(original)
            if replacement is None:
                continue
            if not table.alias:
                for column in scope.columns:
                    qualifier_matches = (
                        column.table == table.name
                        and (
                            (bool(table.db) and column.db == table.db)
                            or (not table.db and not column.db)
                        )
                    )
                    if qualifier_matches:
                        column.set("table", exp.to_identifier(replacement, quoted=True))
                        column.set("db", None)
                        column.set("catalog", None)
            table.set("this", exp.to_identifier(replacement, quoted=True))
            table.set("db", None)
            table.set("catalog", None)
            if (original, replacement) not in seen:
                seen.add((original, replacement))
                rewritten.append({"from": original, "to": replacement})
    return {
        "sql": sql.strip(),
        "repl_sql": expression.sql(dialect="duckdb"),
        "rewritten_refs": rewritten,
    }


def diagnostics(project: Path, payload: str) -> dict:
    """Lint editor content; parse-only, never imports Seeknal or project code."""
    import studio_lint

    request = json.loads(payload)
    if not isinstance(request, dict) or not isinstance(request.get("path"), str) or not isinstance(
        request.get("content"), str
    ):
        raise ValueError("diagnostics request needs path and content")
    return studio_lint.lint(project, request["path"], request["content"])


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_draft(
    project: Path, relative: str, expected_source_hash: str,
    expected_destination_hash: str | None,
) -> dict:
    details = draft_details(project, relative)
    source_lexical = project / relative
    target_lexical = project / details["destination"]
    source = _canonical_project_path(project, source_lexical, "draft source became unsafe")
    target = _canonical_project_path(project, target_lexical, "draft destination became unsafe")
    if _hash(source) != expected_source_hash:
        raise ValueError("draft changed after review")
    current_destination_hash = _hash(target) if target.is_file() else None
    if current_destination_hash != expected_destination_hash:
        raise ValueError("destination changed after review")
    target.parent.mkdir(parents=True, exist_ok=True)
    if (
        _canonical_project_path(project, source_lexical, "draft source became unsafe") != source
        or _canonical_project_path(project, target_lexical, "draft destination became unsafe") != target
    ):
        raise ValueError("draft source or destination changed after review")
    # Drafts are private (0600) while under review; the applied pipeline file must
    # stay readable to everyone the project is shared with, so it keeps the mode of
    # the file it replaces, or gets the normal mode for a new file.
    mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else _new_file_mode()
    shutil.move(str(source), str(target))
    os.chmod(target, mode)
    return {**details, "applied": True}


def _new_file_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: engine_bridge.py <graph|preview> <project> [node]")
    action = sys.argv[1]
    project = Path(sys.argv[2]).resolve()
    if action == "draft-options":
        result = draft_options()
    elif action == "graph":
        result = graph(project)
    elif action == "preview" and len(sys.argv) == 4:
        result = preview(project, sys.argv[3])
    elif action == "draft-details" and len(sys.argv) == 4:
        result = draft_details(project, sys.argv[3])
    elif action == "editor-details" and len(sys.argv) == 4:
        result = editor_details(project, sys.argv[3], sys.stdin.read())
    elif action == "validate-draft" and len(sys.argv) == 5:
        result = validate_draft(project, sys.argv[3])
    elif action == "apply-draft" and len(sys.argv) == 6:
        result = apply_draft(
            project, sys.argv[3], sys.argv[4], None if sys.argv[5] == "-" else sys.argv[5]
        )
    elif action == "extract-sql" and len(sys.argv) == 4:
        result = extract_sql(project, sys.argv[3], sys.stdin.read())
    elif action == "diagnostics":
        result = diagnostics(project, sys.stdin.read())
    else:
        raise ValueError("unsupported bridge action")
    if action == "validate-draft":
        print(f"__SEEKNAL_STUDIO_VALIDATION_{sys.argv[4]}__={json.dumps(result)}")
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
