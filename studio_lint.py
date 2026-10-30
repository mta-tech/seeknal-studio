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
import os
import re
from pathlib import Path

import yaml

SOURCE = "seeknal-studio"
ERROR, WARNING, INFO = 1, 2, 3
INDEX_FILE_LIMIT = 300_000
INDEX_COUNT_LIMIT = 2_000
INDEX_BYTES_LIMIT = 20_000_000
YAML_NODE_LIMIT = 100_000  # nodes after alias expansion; larger buffers are not analysed
MAX_DIAGNOSTICS = 200
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

    def resolve(self, name: str) -> str | None:
        return self.by_lower.get(name.lower())

    def suggest(self, name: str, extra: tuple[str, ...] = ()) -> list[str]:
        lowered = name.lower()
        exact = sorted(node for node in self.nodes if node.split(".", 1)[1].lower() == lowered)
        candidates = sorted(self.nodes | set(extra))
        close = difflib.get_close_matches(name, candidates, n=3, cutoff=0.6)
        return list(dict.fromkeys(exact + close))[:3]


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
        diagnostics, skipped = check_sql(sql_entry, lines, inputs, index, severity, data["kind"])
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

    def __init__(self, form: str | None, refs: list[tuple[str, yaml.Node]], names: list[str],
                 notes: list[dict], count: int = 0) -> None:
        self.form, self.refs, self.names, self.notes = form, refs, names, notes
        self.lookup = {raw for raw, _ in refs} if form == "list" else set()
        self.declared = {raw.removeprefix("ref:").lower() for raw, _ in refs}
        self.count = count  # input_N views exist for N < count (list form only)


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
    return Inputs(form, refs, names, notes, len(node.value) if form == "list" else 0)


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


def mask_sql(sql: str, names: list[str]) -> tuple[str, list[tuple[str, int, int]], list[tuple[int, int]]]:
    """Replace {{ }} templates, $name input placeholders and ref('x') with
    same-length placeholders so offsets stay exact. Returns the masked SQL, the
    ref calls and the masked spans."""
    masked_spans = template_spans(sql)
    characters = list(sql)
    for start, end in masked_spans:
        characters[start:end] = "_" * (end - start)
    masked = "".join(characters)
    for name in names:
        pattern = re.compile(rf"\${re.escape(name)}\b")
        masked_spans += [match.span() for match in pattern.finditer(masked)]
        masked = pattern.sub(lambda match: "_" * len(match.group()), masked)
    refs = [(match.group(1), match.start(), match.end()) for match in REF_PATTERN.finditer(masked)]
    masked_spans += [(start, end) for _, start, end in refs]
    masked = REF_PATTERN.sub(lambda match: "_" * len(match.group()), masked)
    return masked, refs, masked_spans


def parse_sql(sql: str):
    """Parse with sqlglot (DuckDB dialect), recording each table's source span."""
    from sqlglot import exp
    from sqlglot.dialects.duckdb import DuckDB

    class SpanParser(DuckDB.Parser):
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
              severity: int, kind: str) -> tuple[list[dict], list[dict]]:
    """Syntax for every kind's SQL; table rules only where they mirror the
    executor. TransformExecutor resolves ref(), input_N, __THIS__ and $name.
    FeatureGroupExecutor runs the SQL as written against a view named
    `source`, alongside whatever views earlier nodes left in the shared
    session, so only ref() (never resolved there) is checked."""
    from sqlglot.errors import ParseError, TokenError

    key, value = entry
    sql = value.value
    locator = SqlLocator(sql, key, value, lines)
    masked, ref_calls, masked_spans = mask_sql(sql, inputs.names if kind == "transform" else [])
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
