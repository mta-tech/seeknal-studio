from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import studio_lint  # noqa: E402

CLEAN_ORDERS = "seeknal/transforms/clean_orders.yml"
TRANSFORM = """kind: transform
name: x
inputs:
  - ref: source.orders
  - ref: source.customers
transform: |
"""


def clean_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ATLAS_")}
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        environment.pop(name, None)
    return environment


def copy_demo(destination: Path) -> Path:
    project = destination / "demo"
    shutil.copytree(ROOT / "demo", project, ignore=shutil.ignore_patterns("logs", ".omc", "target"))
    (project / "target").mkdir()
    (project / "target" / "run_state.json").write_text("{}")
    return project


def tree_state(project: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(project).as_posix(): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in project.rglob("*")
    }


def brief(result: dict) -> list[tuple]:
    return [
        (item["severity"], item["code"], item["range"]["start"]["line"], item["range"]["start"]["character"],
         item["range"]["end"]["line"], item["range"]["end"]["character"])
        for item in result["diagnostics"]
    ]


class LintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = copy_demo(Path(cls.temporary.name))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def lint(self, content: str, path: str = "seeknal/transforms/x.yml") -> dict:
        return studio_lint.lint(self.project, path, content)

    def sql(self, sql: str) -> dict:
        return self.lint(TRANSFORM + "".join(f"  {line}\n" for line in sql.split("\n")))

    def test_clean_demo_files_have_no_diagnostics(self):
        for path in sorted((self.project / "seeknal").rglob("*.yml")):
            relative = path.relative_to(self.project).as_posix()
            with self.subTest(relative):
                self.assertEqual([], self.lint(path.read_text(), relative)["diagnostics"])

    def test_yaml_syntax_error_uses_problem_mark(self):
        result = self.lint("kind: transform\nname: x: y\n")
        self.assertEqual([(1, "yaml-syntax", 1, 7, 1, 8)], brief(result))
        self.assertIn("mapping values are not allowed here", result["diagnostics"][0]["message"])

    def test_missing_kind_and_required_fields(self):
        self.assertEqual([(1, "missing-kind", 0, 0, 0, 7)], brief(self.lint("name: x\n")))
        result = self.lint("kind: feature_group\nname: x\n", "seeknal/feature_groups/x.yml")
        self.assertEqual([(1, "missing-field", 0, 0, 0, 4)], brief(result))
        self.assertIn("entity, materialization", result["diagnostics"][0]["message"])
        unknown = self.lint("kind: transfrom\nname: x\n")
        self.assertEqual([(1, "unknown-kind", 0, 6, 0, 15)], brief(unknown))
        self.assertEqual(["transform"], unknown["diagnostics"][0]["data"]["suggestions"])

    def test_name_must_match_file_name_but_not_for_drafts(self):
        content = (self.project / CLEAN_ORDERS).read_text().replace("name: clean_orders", "name: paid_orders")
        self.assertEqual([(2, "name-mismatch", 1, 6, 1, 17)], brief(self.lint(content, CLEAN_ORDERS)))
        self.assertEqual([], self.lint(content, ".seeknal/drafts/draft_transform_x.yml")["diagnostics"])

    def test_unknown_field_is_a_warning(self):
        result = self.lint("kind: transform\nname: x\ntransfrom: SELECT 1\n")
        self.assertEqual([(2, "unknown-field", 2, 0, 2, 9)], brief(result))
        self.assertEqual("transform", result["diagnostics"][0]["data"]["suggestions"][0])

    def test_sql_typo_in_block_scalar_is_marked_at_the_exact_buffer_position(self):
        content = (self.project / CLEAN_ORDERS).read_text().replace("  SELECT\n", "  SELEC\n", 1)
        self.assertEqual("  SELEC", content.split("\n")[4])
        result = self.lint(content, CLEAN_ORDERS)
        self.assertEqual([(1, "sql-syntax", 4, 2, 4, 7)], brief(result))
        self.assertIn("SELEC", result["diagnostics"][0]["message"])

    def test_sql_rejected_only_by_sqlglot_is_not_an_error(self):
        result = self.sql("SELECT order_id FROM source.orders,")
        self.assertEqual([(3, "sql-unanalysed", 5, 0, 5, 9)], brief(result))
        self.assertEqual([{"code": "tables", "reason": "sqlglot could not parse the SQL"}], result["skipped"])

    def test_sql_rejected_by_duckdb_after_sqlglot_accepts_is_an_error(self):
        self.assertEqual([(1, "sql-syntax", 6, 40, 6, 41)], brief(self.sql("SELECT * FROM source.orders WHERE x = = 1")))

    def test_templates_keep_positions_and_soften_syntax_errors(self):
        self.assertEqual([], self.sql("SELECT * FROM source.orders WHERE d > '{{ start }}' AND n = {{ n }}")["diagnostics"])
        self.assertEqual([], self.sql("SELECT * FROM source_{{ env }}")["diagnostics"])
        self.assertEqual(
            [(1, "unknown-table", 6, 48, 6, 60)],
            brief(self.sql("SELECT '{{ a }}' AS x FROM source.orders JOIN source.ordrs USING (x)")),
        )
        self.assertEqual([(2, "sql-syntax", 6, 2, 6, 7)], brief(self.sql("SELEC {{ cols }} FROM source.orders")))

    def test_folded_and_quoted_sql_are_reported_on_the_key_line(self):
        content = "kind: transform\nname: x\ninputs:\n  - ref: source.orders\ntransform: >\n  SELECT * FROM\n  source.ordrs\n"
        result = self.lint(content)
        self.assertEqual([(1, "unknown-table", 4, 0, 4, 9)], brief(result))
        self.assertIn("SQL line 1, column 15", result["diagnostics"][0]["message"])
        quoted = self.lint('kind: transform\nname: x\ntransform: "SELEC 1"\n')
        self.assertEqual([(1, "sql-syntax", 2, 0, 2, 9)], brief(quoted))

    def test_single_line_plain_sql_maps_exactly(self):
        content = "kind: transform\nname: x\ninputs:\n  - ref: source.orders\ntransform: SELECT * FROM source.ordrs\n"
        self.assertEqual([(1, "unknown-table", 4, 25, 4, 37)], brief(self.lint(content)))

    def test_python_syntax_error_position(self):
        result = self.lint("s = 'é'\nprint(s +)\n", "seeknal/pipelines/a.py")
        self.assertEqual([(1, "python-syntax", 1, 9, 1, 10)], brief(result))
        self.assertEqual([], self.lint("x = 1\n", "seeknal/pipelines/a.py")["diagnostics"])

    def test_unknown_input_ref_with_suggestion(self):
        content = TRANSFORM.replace("source.customers", "source.custmers") + "  SELECT * FROM input_0\n"
        result = self.lint(content)
        self.assertEqual([(1, "unknown-node", 4, 9, 4, 24)], brief(result))
        self.assertEqual("source.customers", result["diagnostics"][0]["data"]["suggestions"][0])

    def test_unknown_sql_tables_with_suggestions(self):
        result = self.sql("SELECT * FROM source.ordrs JOIN orders USING (order_id)")
        self.assertEqual(
            [(1, "unknown-table", 6, 16, 6, 28), (1, "unknown-table", 6, 34, 6, 40)], brief(result),
        )
        self.assertEqual("source.orders", result["diagnostics"][0]["data"]["suggestions"][0])
        self.assertIn("Did you mean 'source.orders'?", result["diagnostics"][1]["message"])
        self.assertEqual([(1, "input-out-of-range", 6, 16, 6, 23)], brief(self.sql("SELECT * FROM input_2")))

    def test_ref_calls_must_be_listed_in_inputs(self):
        self.assertEqual([], self.sql("SELECT * FROM ref('source.orders')")["diagnostics"])
        self.assertEqual(
            [(1, "ref-not-in-inputs", 6, 16, 6, 45)], brief(self.sql("SELECT * FROM ref('transform.clean_orders')")),
        )
        unknown = self.sql("SELECT * FROM ref('source.ordrs')")
        self.assertEqual([(1, "unknown-node", 6, 16, 6, 35)], brief(unknown))
        self.assertEqual("source.orders", unknown["diagnostics"][0]["data"]["suggestions"][0])

    def test_existing_node_missing_from_inputs_is_a_warning(self):
        self.assertEqual(
            [(2, "not-in-inputs", 6, 16, 6, 38), (2, "not-in-inputs", 6, 44, 6, 66)],
            brief(self.sql("SELECT * FROM transform.clean_orders JOIN transform_customer_ltv USING (customer_id)")),
        )

    def test_valid_table_forms_are_not_flagged(self):
        cases = [
            "WITH paid AS (SELECT * FROM source.orders) SELECT p.order_id FROM paid AS p",
            "SELECT o.order_id FROM source.orders AS o JOIN source.customers c USING (customer_id)",
            "SELECT * FROM input_0 JOIN input_1 USING (customer_id)",
            "SELECT * FROM source_orders JOIN source_customers USING (customer_id)",
            "SELECT * FROM \"source\".\"orders\"",
            "SELECT * FROM 'data/orders.csv' JOIN read_csv('data/customers.csv') r ON true",
            "SELECT * FROM source.orders, unnest([1, 2]) AS t(x)",
            "SELECT * FROM (VALUES (1)) v(a), range(3)",
            "CREATE TABLE paid AS SELECT * FROM source.orders;\nSELECT * FROM paid",
            # Cases from tests/test_sql_draft_routes.py.
            "WITH input_0 AS (SELECT 99 AS n)\nSELECT 'input_0' AS literal_name, n FROM input_0",
            "SELECT nested.order_id\nFROM (SELECT order_id FROM source.orders) AS input_0\n"
            "JOIN (SELECT 1 AS order_id) AS nested ON nested.order_id = input_0.order_id",
            "SELECT input_0.order_id FROM input_0",
            "SELECT source.orders.order_id FROM source.orders",
            "SELECT orders.order_id FROM source.orders AS orders",
        ]
        for sql in cases:
            with self.subTest(sql):
                self.assertEqual([], self.sql(sql)["diagnostics"])

    def test_python_nodes_are_indexed_without_executing_them(self):
        marker = Path(self.temporary.name) / "imported.marker"
        pipelines = self.project / "seeknal" / "pipelines"
        pipelines.mkdir(exist_ok=True)
        node = pipelines / "enrich.py"
        node.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('imported')\n"
            "from seeknal.pipeline import transform\n\n"
            "@transform(name='enriched', inputs=['source.orders'])\n"
            "def enrich(ctx):\n    return ctx.ref('source.orders')\n"
        )
        try:
            content = TRANSFORM.replace("source.customers", "transform.enriched") + "  SELECT * FROM input_1\n"
            self.assertEqual([], self.lint(content)["diagnostics"])
            self.assertEqual([], self.lint(node.read_text(), "seeknal/pipelines/enrich.py")["diagnostics"])
            self.assertFalse(marker.exists())
        finally:
            node.unlink()

    def test_alias_bomb_is_bounded(self):
        bomb = "a: &a [x,x,x,x,x,x,x,x,x,x]\n" + "".join(
            f"{name}: &{name} [{', '.join([f'*{previous}'] * 10)}]\n"
            for previous, name in zip("abcdefg", "bcdefgh")
        )
        for tail in ("kind: *h\nname: t\n", "kind: transform\nname: *h\n", "kind: transform\nname: t\ninputs: *h\n"):
            with self.subTest(tail):
                started = time.perf_counter()
                result = self.lint(bomb + tail)
                self.assertLess(time.perf_counter() - started, 2)
                self.assertEqual(["yaml-too-large"], [item["code"] for item in result["diagnostics"]])
        # A bomb in another project file is indexed without expanding it.
        bad = self.project / "seeknal" / "sources" / "bomb.yml"
        bad.write_text(bomb + "kind: *h\nname: *h\n")
        try:
            started = time.perf_counter()
            self.assertEqual([], self.sql("SELECT * FROM source.orders")["diagnostics"])
            self.assertLess(time.perf_counter() - started, 2)
        finally:
            bad.unlink()

    def test_recursive_aliases_are_reported_quickly(self):
        for content in (
            "kind: transform\nname: t\nx: &a [*a]\n",
            "kind: transform\nname: t\nx: &a {k: *a}\n",
            "&r {kind: transform, name: t, self: *r}\n",
            "kind: transform\nname: t\nx: &a [&b [*a]]\ny: *b\n",
            "kind: transform\nname: t\nx: &a {k: &b {j: [*a, *b]}}\n",
        ):
            with self.subTest(content):
                started = time.perf_counter()
                result = self.lint(content)
                self.assertLess(time.perf_counter() - started, 0.5)
                self.assertEqual(["yaml-too-large"], [item["code"] for item in result["diagnostics"]])
                self.assertIn("recursive alias", result["diagnostics"][0]["message"])
        shared = self.lint("kind: transform\nname: x\ndescription: &a [1]\ntags: [*a, *a]\n")
        self.assertEqual([], shared["diagnostics"])  # shared, non-recursive aliases are fine
        recursive = self.project / "seeknal" / "sources" / "recursive.yml"
        recursive.write_text("kind: &k [*k]\nname: &n {self: *n}\n")
        try:
            started = time.perf_counter()
            self.assertEqual([], self.sql("SELECT * FROM source.orders")["diagnostics"])
            self.assertLess(time.perf_counter() - started, 1)
        finally:
            recursive.unlink()

    def test_long_brace_runs_lint_in_linear_time(self):
        for sql in ("{" * 300_000, "{{" + "{" * 300_000 + "}x"):
            started = time.perf_counter()
            self.lint(TRANSFORM + "  SELECT 1 -- " + sql + "\n")
            self.assertLess(time.perf_counter() - started, 1)
        # Same matches as Seeknal's ParameterResolver.PARAM_PATTERN.
        seeknal_pattern = re.compile(r"\{\{\s*([^}]+)\s*\}\}")
        for text in ("{{ a }}x {{{ b\n c }}", "{{}}{{ a }}", "{{ a } b }} {{ }", "{{{{x}}}}", "{ {{a}} }}", "{{a}"):
            with self.subTest(text):
                expected = [match.span() for match in seeknal_pattern.finditer(text)]
                self.assertEqual(expected, studio_lint.template_spans(text))

    def test_feature_group_sql_reads_the_source_view(self):
        feature_group = (
            "kind: feature_group\nname: x\nentity: customer\nmaterialization: {}\n"
            "inputs:\n  - ref: transform.clean_orders\ntransform: |\n"
        )
        result = studio_lint.lint(self.project, "seeknal/feature_groups/x.yml", feature_group + "  SELECT * FROM source\n")
        self.assertEqual([], result["diagnostics"])
        self.assertEqual([{"code": "tables", "reason": "table names in feature_group SQL depend on the run"}], result["skipped"])
        for sql in ("SELECT * FROM input_0", "SELECT * FROM __THIS__", "SELECT * FROM transform.clean_orders"):
            with self.subTest(sql):
                self.assertEqual([], studio_lint.lint(self.project, "seeknal/feature_groups/x.yml", feature_group + f"  {sql}\n")["diagnostics"])
        ref = studio_lint.lint(self.project, "seeknal/feature_groups/x.yml", feature_group + "  SELECT * FROM ref('transform.clean_orders')\n")
        self.assertEqual([(1, "ref-not-resolved", 7, 16, 7, 45)], brief(ref))
        typo = studio_lint.lint(self.project, "seeknal/feature_groups/x.yml", feature_group + "  SELEC * FROM source\n")
        self.assertEqual([(1, "sql-syntax", 7, 2, 7, 7)], brief(typo))

    def test_messages_and_diagnostic_counts_are_capped(self):
        long_name = "t" * 5000
        result = self.lint(f"kind: {long_name}\nname: x\n")
        self.assertLessEqual(len(result["diagnostics"][0]["message"]), studio_lint.MESSAGE_LIMIT)
        result = self.sql(f"SELECT * FROM {long_name}")
        self.assertLessEqual(len(result["diagnostics"][0]["message"]), studio_lint.MESSAGE_LIMIT)
        many = self.sql("SELECT * FROM " + ", ".join(f"missing_{index}" for index in range(300)))
        self.assertEqual(studio_lint.MAX_DIAGNOSTICS, len(many["diagnostics"]))

    def test_malformed_index_file_is_skipped_and_downgrades_unknowns(self):
        bad = self.project / "seeknal" / "sources" / "bad.yml"
        bad.write_text("kind: [source]\nname: bad\n")
        try:
            result = self.sql("SELECT * FROM source.orders JOIN source.missing USING (x)")
            self.assertEqual([(2, "unknown-table", 6, 35, 6, 49)], brief(result))
        finally:
            bad.unlink()

    def test_index_byte_budget_marks_index_incomplete(self):
        original = studio_lint.INDEX_BYTES_LIMIT
        studio_lint.INDEX_BYTES_LIMIT = 10
        try:
            self.assertEqual(
                [(2, "unknown-node", 3, 9, 3, 22), (2, "unknown-node", 4, 9, 4, 25), (2, "unknown-table", 6, 16, 6, 29)],
                brief(self.sql("SELECT * FROM source.orders")),
            )
        finally:
            studio_lint.INDEX_BYTES_LIMIT = original

    def test_only_yml_files_are_discovered(self):
        extra = self.project / "seeknal" / "sources" / "extra.yaml"
        extra.write_text("kind: source\nname: extra\n")
        try:
            self.assertEqual("unknown-table", self.sql("SELECT * FROM source.extra")["diagnostics"][0]["code"])
            result = self.lint(extra.read_text(), "seeknal/sources/extra.yaml")
            self.assertEqual([(3, "not-discovered", 0, 0, 0, 12)], brief(result))
            self.assertEqual([], self.lint(extra.read_text(), ".seeknal/drafts/extra.yaml")["diagnostics"])
        finally:
            extra.unlink()

    def test_internal_editor_snapshot_paths_are_drafts(self):
        content = (self.project / CLEAN_ORDERS).read_text()
        self.assertEqual([], self.lint(content, ".seeknal/drafts/.editor-0123abcd.yml")["diagnostics"])

    def test_mapping_inputs_follow_the_executor(self):
        mapping = "kind: transform\nname: x\ninputs:\n  o: ref:source.orders\ntransform: "
        note = (2, "inputs-mapping", 2, 0, 2, 6)
        self.assertEqual([note], brief(self.lint(mapping + "SELECT * FROM $o\n")))
        self.assertEqual([note], brief(self.lint(mapping + "SELECT * FROM {{o}}\n")))
        self.assertEqual([note, (1, "input-out-of-range", 4, 25, 4, 32)], brief(self.lint(mapping + "SELECT * FROM input_0\n")))
        self.assertEqual(
            [note, (1, "ref-not-resolved", 4, 25, 4, 45)],
            brief(self.lint(mapping + "SELECT * FROM ref('source.orders')\n")),
        )
        self.assertEqual([note, (1, "unknown-node", 3, 5, 3, 21)], brief(self.lint(mapping.replace("orders", "ordrs") + "SELECT 1\n")))

    def test_list_input_ref_prefix_is_matched_verbatim_by_ref_calls(self):
        string_item = "kind: transform\nname: x\ninputs:\n  - ref:source.orders\ntransform: "
        prefixed = self.lint(string_item + "SELECT * FROM ref('source.orders')\n")
        self.assertEqual([(1, "ref-not-in-inputs", 4, 25, 4, 45)], brief(prefixed))
        self.assertIn("listed as 'ref:source.orders'; write it as 'source.orders'", prefixed["diagnostics"][0]["message"])
        self.assertEqual([], self.lint(string_item + "SELECT * FROM ref('ref:source.orders')\n")["diagnostics"])
        self.assertEqual([], self.lint(string_item + "SELECT * FROM input_0\n")["diagnostics"])

    def test_this_exists_only_for_exactly_one_list_input(self):
        single = "kind: transform\nname: x\ninputs:\n  - ref: source.orders\ntransform: SELECT * FROM __THIS__\n"
        self.assertEqual([], self.lint(single)["diagnostics"])
        result = self.sql("SELECT * FROM __THIS__")
        self.assertEqual([(1, "this-unavailable", 6, 16, 6, 24)], brief(result))
        self.assertIn("exactly one input (it has 2)", result["diagnostics"][0]["message"])
        none = self.lint("kind: transform\nname: x\ntransform: SELECT * FROM __THIS__\n")
        self.assertIn("(it has 0)", none["diagnostics"][0]["message"])
        mapping = self.lint("kind: transform\nname: x\ninputs:\n  o: ref:source.orders\ntransform: SELECT * FROM __THIS__\n")
        self.assertEqual(["inputs-mapping", "this-unavailable"], [item["code"] for item in mapping["diagnostics"]])
        self.assertIn("not created when inputs is a mapping", mapping["diagnostics"][1]["message"])

    def test_positions_with_tabs_accents_and_emoji(self):
        self.assertEqual([(1, "sql-syntax", 6, 18, 6, 25)], brief(self.sql("SELECT\t'a'\tFORM input_0")))
        self.assertEqual([(1, "sql-syntax", 6, 22, 6, 26)], brief(self.sql("SELECT 'héllo' AS a FORM input_0")))
        # Characters are UTF-16 code units: the emoji counts as two.
        self.assertEqual([(1, "sql-syntax", 6, 24, 6, 28)], brief(self.sql("SELECT 'héllo😀' AS a FORM input_0")))
        self.assertEqual(
            [(1, "unknown-table", 6, 24, 6, 36)], brief(self.sql("SELECT '😀' AS a FROM source.ordrs")),
        )
        python = self.lint("print('😀' +)\n", "seeknal/pipelines/a.py")
        self.assertEqual([(1, "python-syntax", 0, 12, 0, 13)], brief(python))

    def test_multiline_templates_are_masked(self):
        result = self.sql("SELECT * FROM input_0 WHERE d > {{\n start }} AND x IN (SELECT 1 FROM nope)")
        line = " start }} AND x IN (SELECT 1 FROM nope)"
        start = 2 + line.index("nope")
        self.assertEqual([(1, "unknown-table", 7, start, 7, start + 4)], brief(result))

    def test_deep_nesting_reports_info_instead_of_failing(self):
        for content in (
            TRANSFORM + "  SELECT " + "(" * 3000 + "1" + ")" * 3000 + "\n",
            "a: " + "[" * 5000 + "]" * 5000 + "\n",
        ):
            result = self.lint(content)
            self.assertEqual([(3, "unanalysed", 0, 0, 0, len(content.split("\n")[0]))], brief(result))

    def test_duckdb_parse_session_has_no_external_access(self):
        import duckdb

        connection = duckdb.connect(":memory:", config=studio_lint.DUCKDB_CONFIG)
        try:
            settings = connection.execute(
                "SELECT current_setting('enable_external_access'), current_setting('autoload_known_extensions'),"
                " current_setting('autoinstall_known_extensions')"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((False, False, False), settings)

    def test_seeknal_schema_copy_matches_installed_sdk(self):
        try:
            from seeknal.workflow.dag import DAGBuilder
            from seeknal.workflow.validators import SCHEMAS
        except ImportError as error:
            self.skipTest(f"Seeknal SDK unavailable: {error}")
        self.assertEqual(SCHEMAS, studio_lint.SCHEMAS)
        self.assertEqual(set(DAGBuilder.KIND_MAP), set(studio_lint.SCHEMAS))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = copy_demo(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def bridge(self, path: str, content: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(ROOT / "engine_bridge.py"), "diagnostics", str(self.project)],
            cwd=self.project, env=clean_environment(), input=json.dumps({"path": path, "content": content}),
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_linting_never_executes_project_code_or_writes(self):
        marker = Path(self.temporary.name) / "imported.marker"
        node = self.project / "seeknal" / "pipelines" / "evil.py"
        node.parent.mkdir()
        node.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n")
        before = tree_state(self.project)
        content = (self.project / CLEAN_ORDERS).read_text().replace("  SELECT\n", "  SELEC\n", 1)
        self.assertEqual([(1, "sql-syntax", 4, 2, 4, 7)], brief(self.bridge(CLEAN_ORDERS, content)))
        self.assertEqual([], self.bridge("seeknal/pipelines/evil.py", node.read_text())["diagnostics"])
        self.assertFalse(marker.exists())
        self.assertEqual(before, tree_state(self.project))

    def test_bridge_lint_latency(self):
        content = (self.project / CLEAN_ORDERS).read_text()
        timings = []
        for _ in range(20):
            started = time.perf_counter()
            self.assertEqual([], self.bridge(CLEAN_ORDERS, content)["diagnostics"])
            timings.append(time.perf_counter() - started)
        timings.sort()
        p95 = timings[int(len(timings) * 0.95) - 1]
        print(f"\ndiagnostics bridge latency: p50 {timings[len(timings) // 2] * 1000:.0f} ms, "
              f"p95 {p95 * 1000:.0f} ms", file=sys.stderr)
        self.assertLess(p95, 1.0)


def load_server():
    spec = importlib.util.spec_from_file_location("studio_diagnostics_server", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class DiagnosticsRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = copy_demo(Path(cls.temporary.name))
        cls.previous = os.environ.get("SEEKNAL_STUDIO_PROJECT")
        os.environ["SEEKNAL_STUDIO_PROJECT"] = str(cls.project)
        cls.server = load_server()
        cls.httpd = cls.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server.HOSTS = {f"127.0.0.1:{cls.port}"}
        cls.server.ORIGINS = {f"http://127.0.0.1:{cls.port}"}
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server.REPL.close()
        if cls.previous is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = cls.previous
        cls.temporary.cleanup()

    def post(self, payload: dict | bytes) -> tuple[int, dict]:
        request = Request(
            f"http://127.0.0.1:{self.port}/api/diagnostics",
            data=payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def fake_bridge(self, body: str) -> Path:
        script = Path(self.temporary.name) / "fake_bridge.py"
        script.write_text(body)
        return script

    def test_response_shape_and_version_echo(self):
        content = (self.project / CLEAN_ORDERS).read_text().replace("  SELECT\n", "  SELEC\n", 1)
        status, result = self.post({"path": CLEAN_ORDERS, "content": content, "version": 7})
        self.assertEqual(200, status, result)
        self.assertEqual(CLEAN_ORDERS, result["path"])
        self.assertEqual(7, result["version"])
        self.assertRegex(result["content_sha256"], "^[0-9a-f]{64}$")
        self.assertIsInstance(result["elapsed_ms"], int)
        self.assertNotIn("unavailable", result)
        self.assertEqual([(1, "sql-syntax", 4, 2, 4, 7)], brief(result))
        self.assertEqual("seeknal-studio", result["diagnostics"][0]["source"])

    def test_invalid_requests_are_rejected(self):
        # The server refuses an oversized body without reading it, so the client may
        # see the 400 or a reset connection depending on timing; both are a refusal.
        try:
            status, _ = self.post({"path": CLEAN_ORDERS, "content": "x" * 300_001, "version": 1})
        except (ConnectionResetError, BrokenPipeError, URLError):
            status = 400
        self.assertEqual(400, status)
        status, _ = self.post({"path": "../outside.yml", "content": "", "version": 1})
        self.assertEqual(400, status)
        status, _ = self.post({"path": CLEAN_ORDERS, "content": "", "version": "1"})
        self.assertEqual(400, status)

    def test_internal_editor_snapshot_path_is_accepted(self):
        snapshot = self.project / ".seeknal" / "drafts" / ".editor-0123abcd.yml"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        content = (self.project / CLEAN_ORDERS).read_text()
        snapshot.write_text(content)
        try:
            status, result = self.post({"path": ".seeknal/drafts/.editor-0123abcd.yml", "content": content, "version": 5})
        finally:
            snapshot.unlink()
        self.assertEqual((200, []), (status, result["diagnostics"]))

    def test_bridge_failure_and_timeout_report_unavailable(self):
        original = (self.server.BRIDGE, self.server.DIAGNOSTICS_TIMEOUT)
        try:
            self.server.BRIDGE = self.fake_bridge("import sys\nsys.exit('bridge exploded')\n")
            status, result = self.post({"path": CLEAN_ORDERS, "content": "kind: transform\n", "version": 2})
            self.assertEqual((200, 2, []), (status, result["version"], result["diagnostics"]))
            self.assertIn("bridge exploded", result["unavailable"])
            self.server.BRIDGE = self.fake_bridge("import time\ntime.sleep(10)\n")
            self.server.DIAGNOSTICS_TIMEOUT = 0.5
            status, result = self.post({"path": CLEAN_ORDERS, "content": "kind: transform\n", "version": 3})
            self.assertEqual((200, []), (status, result["diagnostics"]))
            self.assertIn("timed out", result["unavailable"])
        finally:
            self.server.BRIDGE, self.server.DIAGNOSTICS_TIMEOUT = original

    def test_busy_lint_does_not_queue(self):
        original = self.server.DIAGNOSTICS_WAIT
        self.server.DIAGNOSTICS_WAIT = 0.05
        self.server.DIAGNOSTICS_LOCK.acquire()
        try:
            status, result = self.post({"path": CLEAN_ORDERS, "content": "kind: transform\n", "version": 4})
        finally:
            self.server.DIAGNOSTICS_LOCK.release()
            self.server.DIAGNOSTICS_WAIT = original
        self.assertEqual((200, []), (status, result["diagnostics"]))
        self.assertIn("another lint", result["unavailable"])


if __name__ == "__main__":
    unittest.main()
