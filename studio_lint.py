"""Advisory diagnostics for Seeknal pipeline files open in the Studio editor.

Pure analysis only: YAML, SQL and Python are parsed, never executed. Seeknal is
not imported, user Python is never imported, no SQL is run and nothing is
written. Positions are LSP-style 0-based line/character offsets into the editor
buffer.
"""

from __future__ import annotations

import ast
import bisect
import difflib
import hashlib
import json
import logging
import os
import re
import stat
from pathlib import Path

import yaml

SOURCE = "seeknal-studio"
ERROR, WARNING, INFO = 1, 2, 3
INDEX_FILE_LIMIT = 300_000
INDEX_COUNT_LIMIT = 2_000
INDEX_BYTES_LIMIT = 20_000_000
YAML_NODE_LIMIT = 100_000  # nodes after alias expansion; larger buffers are not analysed
MAX_DIAGNOSTICS = 200
INFER_BUDGET = 40  # upstream schemas inferred per lint
INFER_DEPTH = 12
MESSAGE_LIMIT = 500
VALUE_LIMIT = 120

# Copy of seeknal.workflow.validators.SCHEMAS (Seeknal is not imported here;
# tests/test_diagnostics.py keeps the copy in step with the installed SDK).
SCHEMAS = {
    "source": {
        "required": ["kind", "name"],
        "optional": ["description", "owner", "tags", "source", "table", "params", "columns", "freshness"],
    },
    "transform": {
        "required": ["kind", "name"],
        "optional": ["description", "owner", "tags", "inputs", "params", "transform", "output"],
    },
    "feature_group": {
        "required": ["kind", "name", "entity", "materialization"],
        "optional": ["description", "owner", "tags", "inputs", "transform", "features", "tests"],
    },
    "model": {
        "required": ["kind", "name", "output_columns", "inputs"],
        "optional": ["description", "owner", "tags", "aggregation", "training", "tests"],
    },
    "aggregation": {
        "required": ["kind", "name", "id_col", "feature_date_col"],
        "optional": ["description", "owner", "application_date_col", "features"],
    },
    "second_order_aggregation": {
        "required": ["kind", "name", "id_col", "feature_date_col", "source", "features"],
        "optional": ["description", "owner", "application_date_col", "inputs", "tags", "materialization"],
    },
    "profile": {
        "required": ["kind", "name", "inputs"],
        "optional": ["description", "owner", "tags", "profile"],
    },
    "rule": {
        "required": ["kind", "name", "rule"],
        "optional": ["description", "owner", "params"],
    },
    "exposure": {
        "required": ["kind", "name", "type"],
        "optional": ["description", "owner", "tags", "depends_on", "url", "params"],
    },
    "semantic_model": {
        "required": ["kind", "name", "model"],
        "optional": ["description", "default_time_dimension", "entities", "dimensions", "measures", "joins", "metrics"],
    },
    "metric": {
        "required": ["kind", "name", "type"],
        "optional": ["description", "filter", "measure", "numerator", "denominator", "grain_to_date", "expr", "inputs"],
    },
}
# Keys the SDK reads for any node although SCHEMAS does not list them; they
# are never reported as unknown.
COMMON_KEYS = {
    "description", "owner", "tags", "params", "columns", "materialization", "materializations",
    "audits", "tests", "engine", "connection", "freshness", "output",
}
PYTHON_DECORATORS = {"source", "transform", "feature_group", "second_order_aggregation"}
# Same pattern as Seeknal's TransformExecutor._REF_PATTERN.
REF_PATTERN = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""")
INPUT_ALIAS = re.compile(r"input_(\d+)", re.IGNORECASE)
DUCKDB_NEAR = re.compile(r'at or near "(.*)"$')
# Parse-only DuckDB session: no file, network or extension access.
DUCKDB_CONFIG = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "errors_as_json": True,
}


def clip(value: object, limit: int = VALUE_LIMIT) -> str:
    """User-derived text for messages: only strings are shown, and shortened."""
    text = value if isinstance(value, str) else f"<{type(value).__name__}>"
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def diagnostic(start: tuple[int, int], end: tuple[int, int], severity: int, code: str,
               message: str, suggestions: list[str] | None = None) -> dict:
    item = {
        "range": {
            "start": {"line": start[0], "character": start[1]},
            "end": {"line": end[0], "character": end[1]},
        },
        "severity": severity,
        "code": code,
        "source": SOURCE,
        "message": clip(message, MESSAGE_LIMIT),
    }
    if suggestions:
        item["data"] = {"suggestions": [clip(suggestion) for suggestion in suggestions]}
    return item


def node_range(node: yaml.Node) -> tuple[tuple[int, int], tuple[int, int]]:
    return (node.start_mark.line, node.start_mark.column), (node.end_mark.line, node.end_mark.column)


def did_you_mean(message: str, suggestions: list[str]) -> str:
    return f"{message}. Did you mean '{clip(suggestions[0])}'?" if suggestions else message


class ProjectIndex:
    """Node ids of the project, read from YAML with safe_load and from Python
    decorator calls with ast; no project code is imported."""

    def __init__(self, project: Path) -> None:
        self.nodes: set[str] = set()
        # node id -> {"kind", "columns" (declared names or None), "sql", "inputs"}
        self.meta: dict[str, dict] = {}
        self.incomplete = False
        digest = hashlib.sha256()
        root = project / "seeknal"
        files = []
        for directory, folders, names in os.walk(root):
            folders.sort()
            relative_dir = Path(directory).relative_to(root).parts
            for name in sorted(names):
                path = Path(directory, name)
                # Mirrors DAGBuilder discovery: **/*.yml outside common/, and
                # **/*.py outside common/ and templates/ not starting with "_".
                if path.suffix == ".yml" and relative_dir[:1] != ("common",):
                    files.append(path)
                elif (
                    path.suffix == ".py" and not name.startswith("_")
                    and relative_dir[:1] not in {("common",), ("templates",)}
                ):
                    files.append(path)
        if len(files) > INDEX_COUNT_LIMIT:
            files, self.incomplete = files[:INDEX_COUNT_LIMIT], True
        remaining = INDEX_BYTES_LIMIT
        for path in files:
            try:
                info = path.lstat()
                if path.is_symlink() or info.st_size > min(INDEX_FILE_LIMIT, remaining):
                    self.incomplete = True
                    continue
                remaining -= info.st_size
                text = path.read_text(encoding="utf-8")
                digest.update(f"{path.relative_to(project)}\0{info.st_mtime_ns}\0{info.st_size}\0".encode())
                if path.suffix == ".py":
                    self._add_python(text)
                else:
                    self._add_yaml(text)
            except Exception:  # one unreadable or malformed file must not stop the index
                self.incomplete = True
        self.rev = digest.hexdigest()[:16]
        self.by_lower = {node.lower(): node for node in self.nodes}

    def _add_yaml(self, text: str) -> None:
        try:
            data = yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
        except yaml.YAMLError:
            self.incomplete = True
            return
        kind = data.get("kind") if isinstance(data, dict) else None
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(kind, str) and kind in SCHEMAS and isinstance(name, str):
            self.nodes.add(f"{kind}.{name}")
            sql = data.get("transform")
            self.meta[f"{kind}.{name}"] = {
                "kind": kind, "columns": declared_columns(data.get("columns")),
                "sql": sql if isinstance(sql, str) else None, "inputs": data.get("inputs"),
            }
        elif not (isinstance(kind, (str, type(None))) and isinstance(name, (str, type(None)))):
            self.incomplete = True  # a malformed node we cannot name

    def _add_python(self, text: str) -> None:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            self.incomplete = True
            return
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in function.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                target = call.func if call else decorator
                kind = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
                if kind not in PYTHON_DECORATORS:
                    continue
                name = decorator_name(call, function.name, kind)
                if name is None:
                    self.incomplete = True
                else:
                    self.nodes.add(f"{kind}.{name}")
                    self.meta[f"{kind}.{name}"] = {
                        "kind": kind, "columns": decorator_columns(call), "sql": None, "inputs": None,
                    }

    def resolve(self, name: str) -> str | None:
        return self.by_lower.get(name.lower())

    def suggest(self, name: str, extra: tuple[str, ...] = ()) -> list[str]:
        lowered = name.lower()
        exact = sorted(node for node in self.nodes if node.split(".", 1)[1].lower() == lowered)
        candidates = sorted(self.nodes | set(extra))
        close = difflib.get_close_matches(name, candidates, n=3, cutoff=0.6)
        return list(dict.fromkeys(exact + close))[:3]


def declared_columns(value: object) -> list[str] | None:
    """Column names from a node's `columns:` (Seeknal keeps it as given: a
    mapping of name -> details, or a list of names / {name: ...} items)."""
    if isinstance(value, dict):
        names = list(value)
    elif isinstance(value, list):
        names = [item.get("name") if isinstance(item, dict) else item for item in value]
    else:
        return None
    return names if names and all(isinstance(name, str) for name in names) else None


def decorator_columns(call: ast.Call | None) -> list[str] | None:
    """Literal `columns={...}` keys of a Python decorator; never evaluated."""
    for keyword in call.keywords if call else []:
        if keyword.arg == "columns" and isinstance(keyword.value, ast.Dict):
            keys = keyword.value.keys
            if keys and all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keys):
                return [key.value for key in keys]
    return None


def decorator_name(call: ast.Call | None, function_name: str, kind: str) -> str | None:
    """Node name declared by a Seeknal decorator, or None when not a literal."""
    if call is not None:
        for keyword in call.keywords:
            if keyword.arg == "name":
                value = keyword.value
                return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None
        if call.args:
            first = call.args[0]
            return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
    # @source and @second_order_aggregation require a name; the others default to the function name.
    return None if kind in {"source", "second_order_aggregation"} else function_name


def lint(project: Path, relative: str, content: str) -> dict:
    lines = content.split("\n")
    try:
        if relative.endswith(".py"):
            result = {"diagnostics": lint_python(content), "skipped": [], "index_rev": None}
        else:
            result = lint_yaml(project, relative, content)
    except Exception as error:  # advisory: e.g. RecursionError on deeply nested SQL or YAML
        result = {
            "diagnostics": [diagnostic(
                (0, 0), (0, len(lines[0])), INFO, "unanalysed",
                f"Studio could not analyse this file ({type(error).__name__})",
            )],
            "skipped": [{"code": "all", "reason": type(error).__name__}],
            "index_rev": None,
        }
    result["diagnostics"] = [utf16_range(item, lines) for item in result["diagnostics"][:MAX_DIAGNOSTICS]]
    return result


def utf16_range(item: dict, lines: list[str]) -> dict:
    """Convert code-point characters to UTF-16 code units (the LSP default and
    what the browser editor indexes by)."""
    for end in ("start", "end"):
        position = item["range"][end]
        line = lines[position["line"]] if 0 <= position["line"] < len(lines) else ""
        prefix = line[:position["character"]]
        if any(ord(character) > 0xFFFF for character in prefix):
            position["character"] += sum(ord(character) > 0xFFFF for character in prefix)
    return item


def lint_python(content: str) -> list[dict]:
    lines = content.split("\n")
    try:
        ast.parse(content)
    except SyntaxError as error:
        line = min(max((error.lineno or 1) - 1, 0), len(lines) - 1)
        character = min(max((error.offset or 1) - 1, 0), len(lines[line]))
        end_line = min(max((error.end_lineno or 0) - 1, line), len(lines) - 1)
        end_character = min(max((error.end_offset or 0) - 1, 0), len(lines[end_line]))
        if (end_line, end_character) <= (line, character):
            end_line, end_character = line, min(character + 1, len(lines[line]))
        return [diagnostic((line, character), (end_line, end_character), ERROR, "python-syntax",
                           f"Python syntax error: {error.msg}")]
    except (ValueError, RecursionError, MemoryError) as error:
        return [diagnostic((0, 0), (0, len(lines[0])), INFO, "unanalysed", f"Python could not parse this file: {error}")]
    return []


def is_node_file(relative: str) -> bool:
    return (
        (relative.startswith("seeknal/") and not relative.startswith("seeknal/common/"))
        or relative.startswith(".seeknal/drafts/")
    )


def expanded_size(root: yaml.Node, limit: int) -> int | None:
    """Node count with aliases expanded, capped just above limit, so alias
    bombs are detected without being expanded. None for a recursive alias.
    Iterative and linear in the composed document."""
    sizes: dict[int, int] = {}
    active: set[int] = set()  # nodes whose children are still being counted
    stack = [(root, False)]
    while stack:
        node, finished = stack.pop()
        if isinstance(node, yaml.SequenceNode):
            children = node.value
        elif isinstance(node, yaml.MappingNode):
            children = [child for pair in node.value for child in pair]
        else:
            children = []
        if finished:
            active.discard(id(node))
            sizes[id(node)] = min(limit + 1, 1 + sum(sizes[id(child)] for child in children))
        elif id(node) not in sizes and id(node) not in active:
            if any(id(child) in active or child is node for child in children):
                return None
            active.add(id(node))
            stack.append((node, True))
            stack.extend((child, False) for child in children if id(child) not in sizes)
    return sizes[id(root)]


def lint_yaml(project: Path, relative: str, content: str) -> dict:
    lines = content.split("\n")
    result = {"diagnostics": [], "skipped": [], "index_rev": None}
    loader = yaml.SafeLoader(content)
    try:
        root = loader.get_single_node()
        size = expanded_size(root, YAML_NODE_LIMIT) if root is not None else 0
        if size is None or size > YAML_NODE_LIMIT:
            message = (
                "YAML contains a recursive alias; the file was not analysed" if size is None else
                f"YAML aliases expand to more than {YAML_NODE_LIMIT} values; the file was not analysed"
            )
            result["diagnostics"].append(diagnostic((0, 0), (0, len(lines[0])), WARNING, "yaml-too-large", message))
            return result
        data = loader.construct_document(root) if root is not None else None
    except yaml.YAMLError as error:
        result["diagnostics"].append(yaml_error(error, lines))
        return result
    finally:
        loader.dispose()
    if not is_node_file(relative):
        return result
    if relative.startswith("seeknal/") and relative.endswith(".yaml"):
        # DAGBuilder._discover_yaml_files globs seeknal/**/*.yml only.
        result["diagnostics"].append(diagnostic(
            (0, 0), (0, len(lines[0])), INFO, "not-discovered",
            "Seeknal only discovers *.yml files; rename this file to .yml to make it part of the pipeline",
        ))
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        result["diagnostics"].append(diagnostic(
            (0, 0), (0, len(lines[0])), ERROR, "node-shape", "A Seeknal node file must be a YAML mapping",
        ))
        return result
    fields = {key.value: (key, value) for key, value in root.value if isinstance(key, yaml.ScalarNode)}
    shape = check_shape(relative, data, fields, lines)
    result["diagnostics"].extend(shape)
    if any(item["code"] in {"missing-kind", "unknown-kind"} for item in shape):
        return result
    inputs = read_inputs(fields.get("inputs"))
    result["diagnostics"].extend(inputs.notes)
    sql_entry = fields.get("transform") if isinstance(data.get("transform"), str) else None
    if not inputs.refs and sql_entry is None:
        return result
    index = ProjectIndex(project)
    result["index_rev"] = index.rev
    severity = WARNING if index.incomplete else ERROR
    for raw, node in inputs.refs:
        ref = raw.removeprefix("ref:")
        if index.resolve(ref) is None:
            suggestions = index.suggest(ref)
            result["diagnostics"].append(diagnostic(
                *node_range(node), severity, "unknown-node",
                did_you_mean(f"Unknown node '{clip(ref)}' in inputs", suggestions), suggestions,
            ))
    if sql_entry is not None:
        diagnostics, skipped = check_sql(sql_entry, lines, inputs, index, severity, data["kind"], project)
        result["diagnostics"].extend(diagnostics)
        result["skipped"].extend(skipped)
    return result


def yaml_error(error: yaml.YAMLError, lines: list[str]) -> dict:
    mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
    line = min(mark.line, len(lines) - 1) if mark else 0
    character = min(mark.column, len(lines[line])) if mark else 0
    problem = getattr(error, "problem", None) or str(error)
    context = getattr(error, "context", None)
    message = f"YAML syntax error: {problem}" + (f" ({context})" if context else "")
    return diagnostic((line, character), (line, min(character + 1, len(lines[line]))), ERROR, "yaml-syntax", message)


def check_shape(relative: str, data: dict, fields: dict, lines: list[str]) -> list[dict]:
    kind = data.get("kind")
    if "kind" not in fields:
        return [diagnostic((0, 0), (0, len(lines[0])), ERROR, "missing-kind", "Missing required field 'kind'")]
    kind_key, kind_value = fields["kind"]
    if not isinstance(kind, str) or kind not in SCHEMAS:
        suggestions = difflib.get_close_matches(kind, sorted(SCHEMAS), n=3) if isinstance(kind, str) else []
        message = did_you_mean(f"Unknown node kind '{clip(kind)}'", suggestions)
        return [diagnostic(*node_range(kind_value), ERROR, "unknown-kind", message, suggestions)]
    diagnostics = []
    schema = SCHEMAS[kind]
    missing = [field for field in schema["required"] if field not in data]
    if missing:
        diagnostics.append(diagnostic(
            *node_range(kind_key), ERROR, "missing-field",
            f"Missing required field{'s' if len(missing) > 1 else ''} for {kind}: {', '.join(missing)}",
        ))
    name = data.get("name")
    stem = Path(relative).stem
    if "name" in fields and not isinstance(name, str):
        diagnostics.append(diagnostic(
            *node_range(fields["name"][1]), ERROR, "invalid-name", "Node name must be a string",
        ))
    elif "name" in fields and relative.startswith("seeknal/") and name != stem:
        diagnostics.append(diagnostic(
            *node_range(fields["name"][1]), WARNING, "name-mismatch",
            f"Node name '{clip(name)}' does not match the file name '{clip(stem)}'",
        ))
    allowed = set(schema["required"]) | set(schema["optional"]) | COMMON_KEYS
    for key, (key_node, _) in fields.items():
        if key not in allowed:
            suggestions = difflib.get_close_matches(key, sorted(allowed), n=3)
            diagnostics.append(diagnostic(
                *node_range(key_node), WARNING, "unknown-field",
                did_you_mean(f"Unknown field '{clip(key)}' for {kind}", suggestions), suggestions,
            ))
    return diagnostics


class Inputs:
    """Declared inputs as Seeknal's executor reads them. List form resolves
    ref('x') (x matched verbatim against each item) and input_N views; mapping
    form ({name: "ref:kind.x"}) only resolves $name and {{name}} placeholders."""

    def __init__(self, form: str | None, refs: list[tuple[str, yaml.Node | None]], names: list[str],
                 notes: list[dict], count: int = 0, positions: list[str | None] | None = None,
                 mapping: dict[str, str] | None = None) -> None:
        self.form, self.refs, self.names, self.notes = form, refs, names, notes
        self.lookup = {raw for raw, _ in refs} if form == "list" else set()
        self.declared = {raw.removeprefix("ref:").lower() for raw, _ in refs}
        self.count = count  # input_N views exist for N < count (list form only)
        self.positions = positions or []  # node behind input_N, when the executor can create it
        self.mapping = mapping or {}  # $name -> node (mapping form)


def item_position(item: object) -> str | None:
    """Node the executor registers as input_N for a list item ({ref: ...} only)."""
    ref = item.get("ref") if isinstance(item, dict) else None
    return ref.removeprefix("ref:") if isinstance(ref, str) and ref else None


def inputs_from_data(value: object) -> Inputs:
    """Inputs of an upstream node from its parsed YAML (no positions needed)."""
    if isinstance(value, list):
        raws = [item.get("ref") if isinstance(item, dict) else item for item in value]
        refs = [(raw, None) for raw in raws if isinstance(raw, str) and raw]
        return Inputs("list", refs, [], [], len(value), [item_position(item) for item in value])
    if isinstance(value, dict):
        mapping = {key: ref.removeprefix("ref:") for key, ref in value.items() if isinstance(key, str) and isinstance(ref, str)}
        return Inputs("mapping", [(ref, None) for ref in mapping.values()], list(mapping), [], 0, None, mapping)
    return Inputs(None, [], [], [])


def read_inputs(entry: tuple | None) -> Inputs:
    if entry is None:
        return Inputs(None, [], [], [])
    key, node = entry
    values, names, notes = [], [], []
    form = None
    if isinstance(node, yaml.SequenceNode):
        form = "list"
        for item in node.value:
            if isinstance(item, yaml.MappingNode):
                values.extend(value for item_key, value in item.value if getattr(item_key, "value", None) == "ref")
            else:
                values.append(item)
    elif isinstance(node, yaml.MappingNode):
        form = "mapping"
        for item_key, value in node.value:
            if isinstance(item_key, yaml.ScalarNode) and isinstance(item_key.value, str):
                names.append(item_key.value)
                values.append(value)
        # DAGBuilder._extract_dependencies reports non-list inputs and adds no edges.
        notes.append(diagnostic(
            *node_range(key), WARNING, "inputs-mapping",
            "Seeknal's DAG expects inputs as a list; the mapping form adds no dependency edges "
            "and only resolves $name / {{name}} placeholders",
        ))
    refs = [
        (value.value, value) for value in values
        if isinstance(value, yaml.ScalarNode) and isinstance(value.value, str) and value.value
    ]
    if form == "list":
        positions = [item_position(yaml_scalar_mapping(item)) for item in node.value]
        return Inputs(form, refs, names, notes, len(node.value), positions)
    mapping = {
        name: value.value.removeprefix("ref:") for name, value in zip(names, values)
        if isinstance(value, yaml.ScalarNode) and isinstance(value.value, str)
    }
    return Inputs(form, refs, names, notes, 0, None, mapping)


def yaml_scalar_mapping(node: yaml.Node) -> dict | None:
    """The string entries of a YAML mapping node, without constructing it."""
    if not isinstance(node, yaml.MappingNode):
        return None
    return {
        key.value: value.value for key, value in node.value
        if isinstance(key, yaml.ScalarNode) and isinstance(value, yaml.ScalarNode)
    }


def sql_origin(node: yaml.ScalarNode, lines: list[str]) -> tuple[int, int] | None:
    """Buffer (line, column) where the SQL starts when it maps 1:1 onto buffer
    text: literal blocks and single-line plain scalars. Otherwise None."""
    sql_lines = node.value.split("\n")
    if node.style == "|":
        first = node.start_mark.line + 1
        body = next((line for line in lines[first:] if line.strip()), "")
        indent = len(body) - len(body.lstrip(" "))
    elif node.style is None and node.start_mark.line == node.end_mark.line:
        first, indent = node.start_mark.line, node.start_mark.column
    else:
        return None
    for offset, text in enumerate(sql_lines):
        if text and (first + offset >= len(lines) or lines[first + offset][indent:indent + len(text)] != text):
            return None
    return first, indent


class SqlLocator:
    """Maps character offsets in the transform SQL to editor buffer positions."""

    def __init__(self, sql: str, key: yaml.Node, value: yaml.ScalarNode, lines: list[str]) -> None:
        self.starts = [0]
        for index, character in enumerate(sql):
            if character == "\n":
                self.starts.append(index + 1)
        self.origin = sql_origin(value, lines)
        self.key_range = node_range(key)

    def position(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.starts, offset) - 1
        return line, offset - self.starts[line]

    def range(self, start: int, end: int) -> tuple[tuple[int, int], tuple[int, int], bool]:
        """Buffer range for SQL offsets [start, end); falls back to the key."""
        if self.origin is None:
            return (*self.key_range, False)
        first, indent = self.origin
        (start_line, start_column), (end_line, end_column) = self.position(start), self.position(end)
        return (first + start_line, indent + start_column), (first + end_line, indent + end_column), True

    def describe(self, offset: int) -> str:
        line, column = self.position(offset)
        return f" (SQL line {line + 1}, column {column + 1})"


def template_spans(sql: str) -> list[tuple[int, int]]:
    """Spans of {{ ... }} as Seeknal's ParameterResolver matches them
    (\\{\\{\\s*([^}]+)\\s*\\}\\}, which may span lines), found in linear time."""
    spans = []
    position, close = 0, -1
    while True:
        start = sql.find("{{", position)
        if start < 0:
            return spans
        if close < start + 2:
            close = sql.find("}", start + 2)
            if close < 0:
                return spans
        if close > start + 2 and sql.startswith("}", close + 1):
            spans.append((start, close + 2))
            position = close + 2
        elif close == start + 2:
            position = start + 1  # empty content; a later "{{" may still match
        else:
            position = close + 1  # every "{{" before this lone "}" fails the same way


def mask_sql(sql: str, names: list[str]) -> tuple[str, list[tuple[str, int, int]], list[tuple[int, int]], dict]:
    """Replace {{ }} templates, $name input placeholders and ref('x') with
    same-length placeholders so offsets stay exact. Returns the masked SQL, the
    ref calls, the masked spans and {span: ("ref" | "input", name)}."""
    masked_spans = template_spans(sql)
    placeholders = {}
    characters = list(sql)
    for start, end in masked_spans:
        characters[start:end] = "_" * (end - start)
    masked = "".join(characters)
    for name in names:
        pattern = re.compile(rf"\${re.escape(name)}\b")
        for match in pattern.finditer(masked):
            masked_spans.append(match.span())
            placeholders[match.span()] = ("input", name)
        masked = pattern.sub(lambda match: "_" * len(match.group()), masked)
    refs = [(match.group(1), match.start(), match.end()) for match in REF_PATTERN.finditer(masked)]
    for name, start, end in refs:
        masked_spans.append((start, end))
        placeholders[(start, end)] = ("ref", name)
    masked = REF_PATTERN.sub(lambda match: "_" * len(match.group()), masked)
    return masked, refs, masked_spans, placeholders


def parse_sql(sql: str):
    """Parse with sqlglot (DuckDB dialect), recording each table's source span."""
    from sqlglot import exp
    from sqlglot.dialects.duckdb import DuckDB

    logging.getLogger("sqlglot").setLevel(logging.ERROR)

    class SpanParser(DuckDB.Parser):
        def _parse_column(self):
            start = self._index
            result = super()._parse_column()
            tokens = self._tokens[start:self._index]
            for column in result.find_all(exp.Column) if result is not None else []:
                name = column.name.lower()
                token = next((token for token in reversed(tokens) if token.text.lower() == name), None)
                if token is not None and "span" not in column.meta:
                    column.meta["span"] = (token.start, token.end + 1)
            return result

        def _parse_table_parts(self, *args, **kwargs):
            start = self._curr
            table = super()._parse_table_parts(*args, **kwargs)
            end = self._prev
            if isinstance(table, exp.Table) and start is not None and end is not None:
                table.meta["span"] = (start.start, end.end + 1)
            return table

    dialect = DuckDB()
    return [tree for tree in SpanParser(dialect=dialect).parse(dialect.tokenize(sql), sql) if tree is not None]


def duckdb_syntax_error(sql: str) -> tuple[str, tuple[int, int] | None] | None:
    """DuckDB's parse-only verdict: None if it accepts the SQL, otherwise its
    message and the (offset, length) in characters it points at, if any."""
    import duckdb

    connection = duckdb.connect(":memory:", config=DUCKDB_CONFIG)
    try:
        connection.extract_statements(sql)
        return None
    except duckdb.Error as error:
        text = str(error)
    finally:
        connection.close()
    try:
        details = json.loads(text[text.index("{"):])
        message = str(details["exception_message"])
        position = int(details["position"])
    except (ValueError, KeyError, TypeError):
        return text.split("\n", 1)[0].removeprefix("Parser Error: "), None
    # DuckDB reports a byte offset into the UTF-8 SQL.
    offset = len(sql.encode("utf-8")[:position].decode("utf-8", errors="ignore"))
    near = DUCKDB_NEAR.search(message)
    length = len(near.group(1)) if near and sql[offset:offset + len(near.group(1))] == near.group(1) else 1
    return message, (offset, length)


def sqlglot_error_offset(error: Exception, sql: str) -> int | None:
    details = (getattr(error, "errors", None) or [{}])[0]
    line, column = details.get("line"), details.get("col")
    if not isinstance(line, int) or not isinstance(column, int):
        return None
    sql_lines = sql.split("\n")
    line = min(max(line - 1, 0), len(sql_lines) - 1)
    start = max(column - len(details.get("highlight") or ""), 0)
    return sum(len(item) + 1 for item in sql_lines[:line]) + min(start, len(sql_lines[line]))


def check_sql(entry: tuple, lines: list[str], inputs: Inputs, index: ProjectIndex,
              severity: int, kind: str, project: Path) -> tuple[list[dict], list[dict]]:
    """Syntax for every kind's SQL; table rules only where they mirror the
    executor. TransformExecutor resolves ref(), input_N, __THIS__ and $name.
    FeatureGroupExecutor runs the SQL as written against a view named
    `source`, alongside whatever views earlier nodes left in the shared
    session, so only ref() (never resolved there) is checked."""
    from sqlglot.errors import ParseError, TokenError

    key, value = entry
    sql = value.value
    locator = SqlLocator(sql, key, value, lines)
    masked, ref_calls, masked_spans, placeholders = mask_sql(sql, inputs.names if kind == "transform" else [])
    if kind == "transform":
        diagnostics = check_ref_calls(ref_calls, inputs, index, locator, severity)
    else:
        diagnostics = [
            diagnostic(*locator.range(start, end)[:2], ERROR, "ref-not-resolved",
                       f"ref() is not resolved in {kind} SQL; the upstream data is the table 'source'")
            for _, start, end in ref_calls
        ]
    syntax = sql_syntax_error(sql, masked, locator, key)
    if syntax is not None:
        diagnostics.append(syntax)
        return diagnostics, [{"code": "tables", "reason": "SQL has a syntax error"}]
    if kind != "transform":
        return diagnostics, [{"code": "tables", "reason": f"table names in {kind} SQL depend on the run"}]
    try:
        statements = parse_sql(masked)
    except (ParseError, TokenError):
        diagnostics.append(diagnostic(
            *node_range(key), INFO, "sql-unanalysed",
            "Studio could not analyse this SQL (DuckDB accepts it); table checks skipped",
        ))
        return diagnostics, [{"code": "tables", "reason": "sqlglot could not parse the SQL"}]
    diagnostics.extend(check_tables(statements, masked_spans, inputs, index, locator, severity))
    tables = TableResolver(statements, inputs, index, placeholders)
    diagnostics.extend(check_columns(statements, tables, SchemaResolver(project, index), locator, masked_spans))
    return diagnostics, []


def sql_syntax_error(sql: str, masked: str, locator: SqlLocator, key: yaml.Node) -> dict | None:
    """A syntax error only when DuckDB's own parser rejects the SQL."""
    from sqlglot.errors import ParseError, TokenError

    rejected = duckdb_syntax_error(masked)
    if rejected is None:
        return None
    message, span = rejected
    offset = span[0] if span else None
    if offset is None:
        try:
            parse_sql(masked)
        except (ParseError, TokenError) as error:
            offset = sqlglot_error_offset(error, masked)
    start, end, exact = locator.range(offset or 0, (offset or 0) + (span[1] if span else 1))
    # A template placeholder may not parse like the rendered value would.
    level = WARNING if template_spans(sql) else ERROR
    detail = "" if exact or offset is None else locator.describe(offset)
    return diagnostic(start, end, level, "sql-syntax", f"SQL {clip(message, 300)}{detail}")


def check_ref_calls(ref_calls: list[tuple[str, int, int]], inputs: Inputs, index: ProjectIndex,
                    locator: SqlLocator, severity: int) -> list[dict]:
    diagnostics = []
    for name, start, end in ref_calls:
        if name in inputs.lookup:
            continue
        begin, finish, exact = locator.range(start, end)
        detail = "" if exact else locator.describe(start)
        if inputs.form == "mapping":
            diagnostics.append(diagnostic(
                begin, finish, ERROR, "ref-not-resolved",
                f"ref() is not resolved when inputs is a mapping; use $name{detail}",
            ))
        elif index.resolve(name) is None:
            suggestions = index.suggest(name)
            message = did_you_mean(f"Unknown node '{clip(name)}' in ref(){detail}", suggestions)
            diagnostics.append(diagnostic(begin, finish, severity, "unknown-node", message, suggestions))
        elif f"ref:{name}" in inputs.lookup:
            # ref() matches list items verbatim, so a "ref:" prefix never resolves.
            diagnostics.append(diagnostic(
                begin, finish, ERROR, "ref-not-in-inputs",
                f"ref('{clip(name)}') does not match inputs: it is listed as 'ref:{clip(name)}'; "
                f"write it as '{clip(name)}' in inputs{detail}",
            ))
        else:
            diagnostics.append(diagnostic(
                begin, finish, ERROR, "ref-not-in-inputs",
                f"ref('{clip(name)}') is not listed in inputs{detail}",
            ))
    return diagnostics


def bare_node(name: str, index: ProjectIndex) -> str | None:
    """Node id for the bare `kind_name` table form, e.g. source_orders."""
    for kind in sorted(SCHEMAS, key=len, reverse=True):
        if name.lower().startswith(f"{kind}_"):
            node = index.resolve(f"{kind}.{name[len(kind) + 1:]}")
            if node:
                return node
    return None


def check_tables(statements: list, masked_spans: list[tuple[int, int]], inputs: Inputs,
                 index: ProjectIndex, locator: SqlLocator, severity: int) -> list[dict]:
    from sqlglot import exp

    ctes = {cte.alias_or_name.lower() for tree in statements for cte in tree.find_all(exp.CTE)}
    created, targets = set(), set()
    for tree in statements:
        for create in tree.find_all(exp.Create):
            target = create.this.this if isinstance(create.this, exp.Schema) else create.this
            if isinstance(target, exp.Table):
                created.add(target.name.lower())
                targets.add(id(target))
    aliases = [f"input_{position}" for position in range(inputs.count)]
    diagnostics = []
    for tree in statements:
        for table in tree.find_all(exp.Table):
            span = table.meta.get("span")
            if (
                span is None or id(table) in targets or table.args.get("catalog")
                or not isinstance(table.this, exp.Identifier)
                or any(start < span[1] and span[0] < end for start, end in masked_spans)
            ):
                continue
            name, schema = table.name, table.db
            if "/" in name or "." in name:
                continue  # a quoted file path such as FROM 'data/orders.csv'
            if schema:
                if schema.lower() not in SCHEMAS:
                    continue  # another catalog or attached database, not a Seeknal node
                text = f"{schema}.{name}"
                node = index.resolve(text)
            else:
                text = name
                lowered = name.lower()
                if lowered in ctes or lowered in created:
                    continue
                if lowered == "__this__":
                    # The executor creates __THIS__ only for exactly one list-form input.
                    if inputs.count != 1:
                        begin, finish, exact = locator.range(*span)
                        reason = (
                            "__THIS__ is not created when inputs is a mapping; use $name"
                            if inputs.form == "mapping" else
                            f"__THIS__ is only available when the transform has exactly one input "
                            f"(it has {inputs.count})"
                        )
                        diagnostics.append(diagnostic(
                            begin, finish, ERROR, "this-unavailable",
                            reason + ("" if exact else locator.describe(span[0])),
                        ))
                    continue
                alias = INPUT_ALIAS.fullmatch(name)
                if alias:
                    if int(alias.group(1)) >= inputs.count:
                        begin, finish, exact = locator.range(*span)
                        reason = (
                            "input_N views are not created when inputs is a mapping; use $name"
                            if inputs.form == "mapping" else
                            f"{clip(name)} is out of range: this node declares {inputs.count} list input"
                            f"{'' if inputs.count == 1 else 's'}"
                        )
                        diagnostics.append(diagnostic(
                            begin, finish, ERROR, "input-out-of-range",
                            reason + ("" if exact else locator.describe(span[0])),
                        ))
                    continue
                node = bare_node(name, index)
            begin, finish, exact = locator.range(*span)
            detail = "" if exact else locator.describe(span[0])
            if node is None:
                suggestions = index.suggest(text, (*sorted(ctes), *aliases))
                diagnostics.append(diagnostic(
                    begin, finish, severity, "unknown-table",
                    did_you_mean(f"Unknown table '{clip(text)}'{detail}", suggestions), suggestions,
                ))
            elif node.lower() not in inputs.declared:
                diagnostics.append(diagnostic(
                    begin, finish, WARNING, "not-in-inputs",
                    f"{node} is used here but not listed in inputs{detail}",
                ))
    return diagnostics


# Column names (M3). Column truth per upstream node, first available wins:
# declared `columns:` (unless a last-run Parquet schema exists, which is then
# authoritative), inferred from the upstream transform's SQL, the last run's
# Parquet footer. Seeknal does not project sources to their declared columns,
# so only complete schemas (SQL outputs over complete inputs) give errors; all
# others give warnings. A name is reported only when every table it could
# belong to has known columns; anything not modelled makes columns unknown.

ORIGIN_RANK = {"inferred": 0, "declared": 1, "last_run": 2}
SPECIAL_COLUMNS = {"rowid", "all"}  # DuckDB pseudo column; GROUP/ORDER BY ALL


class TableResolver:
    """Maps a table reference in one transform's SQL to the project node the
    executor would read, mirroring check_tables; None when it is not a node."""

    def __init__(self, statements: list, inputs: Inputs, index: ProjectIndex, placeholders: dict) -> None:
        from sqlglot import exp

        self.inputs, self.index, self.placeholders = inputs, index, placeholders
        self.created = {
            (create.this.this if isinstance(create.this, exp.Schema) else create.this).name.lower()
            for tree in statements for create in tree.find_all(exp.Create)
            if isinstance(create.this, (exp.Schema, exp.Table))
        }

    def node(self, table) -> str | None:
        from sqlglot import exp

        span = table.meta.get("span")
        if span in self.placeholders:
            form, name = self.placeholders[span]
            if form == "input":
                target = self.inputs.mapping.get(name)
            else:
                target = name if name in self.inputs.lookup else None
            return self.index.resolve(target.removeprefix("ref:")) if target else None
        if table.args.get("catalog") or not isinstance(table.this, exp.Identifier):
            return None
        name, schema = table.name, table.db
        if schema:
            return self.index.resolve(f"{schema}.{name}") if schema.lower() in SCHEMAS else None
        lowered = name.lower()
        if lowered in self.created:
            return None
        if lowered == "__this__":
            target = self.inputs.positions[0] if self.inputs.count == 1 and self.inputs.positions else None
            return self.index.resolve(target) if target else None
        alias = INPUT_ALIAS.fullmatch(name)
        if alias:
            position = int(alias.group(1))
            target = self.inputs.positions[position] if position < len(self.inputs.positions) else None
            return self.index.resolve(target) if target else None
        return bare_node(name, self.index)


class Schema:
    """Column names of a node or query scope ("" marks an unnamed position),
    where they came from, and whether a miss against them is certain."""

    def __init__(self, names: list[str] | None, origin: str | None, complete: bool = False) -> None:
        self.names, self.origin, self.complete = names, origin, complete

    def usable(self) -> "Schema":
        """Unknown when any position is unnamed."""
        return self if self.names is not None and "" not in self.names else UNKNOWN


UNKNOWN = Schema(None, None)


class Source:
    """A table in a query scope with its schema, a label for messages and the
    project node, if any."""

    def __init__(self, schema: Schema, label: str, node: str | None = None, table=None) -> None:
        schema = schema.usable()
        self.names, self.origin, self.complete = schema.names, schema.origin, schema.complete
        self.label, self.node, self.table = label, node, table
        self.lowered = {name.lower() for name in self.names} if self.names is not None else None


def weakest(origins) -> str | None:
    known = [origin for origin in origins if origin]
    return max(known, key=ORIGIN_RANK.__getitem__) if known else None


class SchemaResolver:
    """Column names of project nodes, memoised per lint, with cycle protection
    and a bounded number of inferred upstream schemas."""

    def __init__(self, project: Path, index: ProjectIndex) -> None:
        self.project, self.index = project, index
        self.memo: dict[str, Schema] = {}
        self.active: set[str] = set()
        self.budget = INFER_BUDGET

    def schema(self, node: str, depth: int = 0) -> Schema:
        if node in self.memo:
            return self.memo[node]
        if node in self.active or depth > INFER_DEPTH:
            return UNKNOWN
        meta = self.index.meta.get(node, {})
        result = UNKNOWN
        if meta.get("columns"):
            last_run = last_run_columns(self.project, node)
            result = Schema(last_run, "last_run") if last_run else Schema(meta["columns"], "declared")
        elif meta.get("kind") == "transform" and meta.get("sql") and self.budget > 0:
            self.budget -= 1
            self.active.add(node)
            try:
                result = self.infer(meta["sql"], inputs_from_data(meta.get("inputs")), depth)
            finally:
                self.active.discard(node)
        if result.names is None and not meta.get("columns"):
            last_run = last_run_columns(self.project, node)
            result = Schema(last_run, "last_run") if last_run else UNKNOWN
        self.memo[node] = result
        return result

    def infer(self, sql: str, inputs: Inputs, depth: int) -> Schema:
        """Output columns of an upstream transform's final query."""
        from sqlglot import exp
        from sqlglot.optimizer.scope import traverse_scope

        try:
            masked, _, _, placeholders = mask_sql(sql, inputs.names if inputs.form == "mapping" else [])
            statements = parse_sql(masked)
            query = next((tree for tree in reversed(statements) if isinstance(tree, exp.Query)), None)
            scopes = traverse_scope(query) or [] if query is not None else []
        except Exception:  # an upstream we cannot analyse simply has unknown columns
            return UNKNOWN
        if not scopes:
            return UNKNOWN
        tables = TableResolver(statements, inputs, self.index, placeholders)
        return analyse_scopes(scopes, tables, self, depth + 1)[1].get(id(scopes[-1]), UNKNOWN).usable()


def last_run_columns(project: Path, node: str) -> list[str] | None:
    """Top-level column names from the Parquet footer of the node's last-run
    output (the same files engine_bridge.preview reads); row data is never read."""
    kind, _, name = node.partition(".")
    if not name or kind in {"", ".", ".."} or any(character in node for character in "/\\\0"):
        return None
    root = project.resolve()
    for parts in (
        ("target", "intermediate", f"{kind}_{name}.parquet"),
        ("target", "cache", kind, f"{name}.parquet"),
    ):
        descriptor = open_inside(root, parts)
        if descriptor is not None:
            return parquet_columns(descriptor)
    return None


def open_inside(root: Path, parts: tuple[str, ...]) -> int | None:
    """Open root/parts one component at a time, refusing a symlink at every
    level, so swapping any parent directory for a link cannot redirect the read
    outside the project. Non-blocking, so a FIFO cannot hang the lint."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | nofollow, dir_fd=directory)
    except OSError:
        return None
    finally:
        os.close(directory)


PARQUET_ELEMENT_LIMIT = 100_000


def parquet_columns(source: Path | int) -> list[str] | None:
    """Top-level column names via DuckDB's parquet_schema(), which reads only
    the footer. A path is opened first without following symlinks or blocking
    (a FIFO is rejected); an already-open descriptor is taken over. DuckDB
    reads it through /dev/fd/N on a separate connection that may access that
    one descriptor and nothing else, so the file cannot be swapped between the
    check and the read. The descriptor is always closed."""
    import duckdb

    if isinstance(source, int):
        descriptor = source
    else:
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
    try:
        info = os.fstat(descriptor)
        handle = f"/dev/fd/{descriptor}"
        if not stat.S_ISREG(info.st_mode) or info.st_size < 12 or not os.path.exists(handle):
            return None
        connection = duckdb.connect(":memory:", config={
            "autoinstall_known_extensions": False, "autoload_known_extensions": False,
            "threads": 1, "memory_limit": "128MB",
        })
        try:
            connection.execute("SET allowed_paths = ?", [[handle]])
            connection.execute("SET enable_external_access = false")
            connection.execute("SET lock_configuration = true")
            rows = connection.execute(
                "SELECT name, num_children FROM parquet_schema(?)", [handle],
            ).fetchmany(PARQUET_ELEMENT_LIMIT + 1)
        finally:
            connection.close()
    except Exception:  # junk, truncated or hostile footer: unknown
        return None
    finally:
        os.close(descriptor)
    if len(rows) > PARQUET_ELEMENT_LIMIT:
        return None
    return top_level_names([(name, children or 0) for name, children in rows])


def top_level_names(elements: list[tuple[str | None, int]]) -> list[str] | None:
    """Children of the root schema element; nested groups are skipped as subtrees."""
    if not elements or any(not isinstance(children, int) or children < 0 for _, children in elements):
        return None
    names, position = [], 1
    for _ in range(elements[0][1]):
        if position >= len(elements) or not isinstance(elements[position][0], str):
            return None
        names.append(elements[position][0])
        pending = elements[position][1]
        position += 1
        while pending:
            if position >= len(elements):
                return None
            pending += elements[position][1] - 1
            position += 1
    return names or None


def analyse_scopes(scopes: list, tables: TableResolver, schemas: SchemaResolver,
                   depth: int = 0) -> tuple[dict, dict]:
    """Sources of every scope and the output schema of every scope, innermost
    first (the order traverse_scope yields)."""
    from sqlglot import exp
    from sqlglot.optimizer.scope import Scope

    sources: dict[int, dict[str, Source]] = {}
    outputs: dict[int, Schema] = {}
    for scope in scopes:
        found = {}
        for alias, value in scope.sources.items():
            if isinstance(value, exp.Table):
                node = tables.node(value)
                found[alias.lower()] = Source(schemas.schema(node, depth) if node else UNKNOWN, node or alias, node, value)
            elif isinstance(value, Scope):
                found[alias.lower()] = Source(outputs.get(id(value), UNKNOWN), alias or "the subquery")
            else:
                found[alias.lower()] = Source(UNKNOWN, alias or "the subquery")
        sources[id(scope)] = found
        outputs[id(scope)] = renamed_output(scope, scope_output(scope, found, outputs))
    return sources, outputs


def renamed_output(scope, schema: Schema) -> Schema:
    """Apply a CTE or derived-table column list, e.g. WITH r(n) AS (...)."""
    from sqlglot import exp

    parent = scope.expression.parent
    renamed = parent.alias_column_names if isinstance(parent, (exp.Subquery, exp.CTE)) else []
    if not renamed or schema.names is None:
        return schema
    return Schema(list(renamed) + schema.names[len(renamed):], schema.origin, schema.complete)


def scope_output(scope, sources: dict[str, Source], outputs: dict) -> Schema:
    from sqlglot import exp

    expression = scope.expression
    if isinstance(expression, exp.Union):
        branches = [outputs.get(id(branch), UNKNOWN) for branch in getattr(scope, "union_scopes", None) or []]
        if not branches:
            return UNKNOWN
        if not expression.args.get("by_name"):
            return branches[0]  # positional set operations take the first branch's names
        if any(branch.usable().names is None for branch in branches):
            return UNKNOWN
        merged = {name.lower(): name for branch in reversed(branches) for name in reversed(branch.names)}
        return Schema(list(merged.values())[::-1], weakest(branch.origin for branch in branches),
                      all(branch.complete for branch in branches))
    if not isinstance(expression, exp.Select) or expression.find(exp.Pivot):
        alias = expression.args.get("alias") if isinstance(expression, exp.Unnest) else None
        columns = [column.name for column in alias.columns] if alias is not None and alias.columns else None
        return Schema(columns, "inferred", True) if columns else UNKNOWN
    names, origins, complete = [], ["inferred"], True
    selected = [sources.get(alias.lower()) for alias in scope.selected_sources]
    for projection in expression.expressions:
        star = projection if isinstance(projection, exp.Star) else (
            projection.this if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star) else None
        )
        if star is None:
            names.append(projection.alias_or_name)  # "" for unnamed expressions
            continue
        expanded = [sources.get(projection.table.lower())] if isinstance(projection, exp.Column) else selected
        star_names = expand_star(star, expanded)
        if star_names is None:
            return UNKNOWN  # SELECT * expands only over known schemas and modelled modifiers
        names.extend(star_names)
        origins += ["last_run" if source.origin == "last_run" else "inferred" for source in expanded]
        complete = complete and all(source.complete for source in expanded)
    return Schema(names, weakest(origins), complete)


def expand_star(star, expanded: list) -> list[str] | None:
    """Columns of *, t.* with EXCLUDE, REPLACE and RENAME; None when unknown."""
    from sqlglot import exp

    if not expanded or any(source is None or source.names is None for source in expanded):
        return None
    if set(key for key, value in star.args.items() if value) - {"except", "replace", "rename"}:
        return None
    names = [name for source in expanded for name in source.names]
    lowered = {name.lower() for name in names}
    excluded = {column.name.lower() for column in star.args.get("except") or []}
    renames = {}
    for item in star.args.get("rename") or []:
        if not isinstance(item, exp.Alias) or not isinstance(item.this, exp.Column) or not item.alias:
            return None
        renames[item.this.name.lower()] = item.alias
    for item in star.args.get("replace") or []:
        if not isinstance(item, exp.Alias) or item.alias.lower() not in lowered:
            return None  # REPLACE must name an existing column
    if not set(renames) <= lowered:
        return None
    return [renames.get(name.lower(), name) for name in names if name.lower() not in excluded]


def scope_chain(scope) -> list:
    """The scope and the outer scopes whose tables it may also reference
    (correlated, lateral and DuckDB's implicit-lateral subqueries)."""
    from sqlglot.optimizer.scope import ScopeType

    chain = [scope]
    while chain[-1].parent is not None and chain[-1].scope_type not in {ScopeType.CTE, ScopeType.ROOT}:
        chain.append(chain[-1].parent)
    return chain


def ignored_column(column) -> bool:
    """Names that are not table columns: lambda parameters, list comprehension
    variables, keyword arguments, EXCLUDE/REPLACE lists and DuckDB pseudo columns."""
    from sqlglot import exp

    if not isinstance(column.this, exp.Identifier) or column.name.lower() in SPECIAL_COLUMNS:
        return True
    name = column.name.lower()
    node = column
    while node.parent is not None:
        parent = node.parent
        if isinstance(parent, exp.Star):
            return True
        if isinstance(parent, (exp.Kwarg, exp.PropertyEQ)) and parent.this is node:
            return True
        if not column.table and isinstance(parent, exp.Lambda) and name in {
            param.name.lower() for param in parent.expressions
        }:
            return True
        if (
            not column.table and isinstance(parent, exp.Comprehension)
            and isinstance(parent.expression, exp.Column) and parent.expression.name.lower() == name
        ):
            return True
        node = parent
    return False


def check_columns(statements: list, tables: TableResolver, schemas: SchemaResolver,
                  locator: SqlLocator, masked_spans: list[tuple[int, int]]) -> list[dict]:
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope

    diagnostics, unknown = [], {}
    for statement in statements:
        try:
            scopes = traverse_scope(statement) or []
        except Exception:  # a statement sqlglot cannot scope is not checked
            continue
        sources, _ = analyse_scopes(scopes, tables, schemas)
        owners = {}
        for scope in scopes:
            for column in scope.columns:
                owners.setdefault(id(column), (scope, column))
        for scope, column in owners.values():
            span = column.meta.get("span")
            if (
                span is None or ignored_column(column)
                or any(start < span[1] and span[0] < end for start, end in masked_spans)
            ):
                continue  # template and $name placeholders are not real columns
            chain = scope_chain(scope)
            candidates = column_sources(column, chain, sources)
            if candidates is None:
                continue
            missing = [source for source in candidates if source.names is None]
            for source in missing:
                if source.node and source.table is not None:
                    unknown.setdefault(source.node, source.table)
            if missing or not candidates:
                continue
            name = column.name.lower()
            if any(name in source.lowered for source in candidates):
                continue
            in_scope = selected_sources(chain, sources)
            if column.table and any(
                source.lowered is None or column.table.lower() in source.lowered for source in in_scope
            ):
                continue  # `t.x` may be field x of a struct column t
            # DuckDB lets select-list aliases be reused in the same SELECT, WHERE,
            # GROUP BY, HAVING and ORDER BY.
            aliases = set() if column.table else {
                projection.alias.lower() for item in chain if isinstance(item.expression, exp.Select)
                for projection in item.expression.expressions if isinstance(projection, exp.Alias)
            }
            if name in aliases:
                continue
            diagnostics.append(column_diagnostic(column.name, span, candidates, aliases, locator))
    for node, table in unknown.items():
        begin, finish, _ = locator.range(*table.meta["span"])
        diagnostics.append(diagnostic(
            begin, finish, INFO, "columns-unknown",
            f"Columns of {clip(node)} are unknown (no declared columns, no inferable SQL and no readable last-run output); "
            "its column references are not checked",
        ))
    return diagnostics


def selected_sources(chain: list, sources: dict) -> list[Source]:
    return [
        source for scope in chain for alias, source in sources[id(scope)].items()
        if alias in {selected.lower() for selected in scope.selected_sources}
    ]


def column_sources(column, chain: list, sources: dict) -> list[Source] | None:
    """Tables a column may belong to, or None when it is not checkable (an
    unknown qualifier, which DuckDB may read as struct field access)."""
    if column.catalog:
        return None
    qualifier, schema = column.table.lower(), column.db.lower()
    if not qualifier:
        return selected_sources(chain, sources) or None
    for scope in chain:
        source = sources[id(scope)].get(qualifier)
        if source is None:
            continue
        if schema:
            table = source.table
            if table is None or table.alias or table.db.lower() != schema:
                return None
        return [source]
    return None


def column_diagnostic(name: str, span: tuple[int, int], candidates: list[Source], aliases: set[str],
                      locator: SqlLocator) -> dict:
    origin = weakest(source.origin for source in candidates)
    complete = all(source.complete for source in candidates)
    known = {known.lower(): known for source in candidates for known in source.names}
    for alias in aliases:
        known.setdefault(alias, alias)
    close = difflib.get_close_matches(name.lower(), sorted(known), n=3, cutoff=0.6)
    suggestions = [known[item] for item in close]
    labels = list(dict.fromkeys(source.label for source in candidates))
    where = clip(labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f" or {labels[-1]}", 300)
    if origin == "declared" and len(labels) == 1:
        message, note = f"Column '{clip(name)}' is not in the declared columns of {where}", ""
    else:
        message = f"Column '{clip(name)}' not found in {where}"
        note = (
            " (from last run)" if origin == "last_run" else
            " (declared columns may be incomplete)" if origin == "declared" else
            "" if complete else " (inferred from declared columns, which may be incomplete)"
        )
    message = did_you_mean(message, suggestions)
    begin, finish, exact = locator.range(*span)
    if not exact:
        message += locator.describe(span[0])
    item = diagnostic(begin, finish, ERROR if complete else WARNING, "unknown-column", message + note, suggestions)
    item.setdefault("data", {})["schema_origin"] = origin
    return item
