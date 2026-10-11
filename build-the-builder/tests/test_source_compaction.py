"""Whitespace compaction must preserve syntax, literal text and runtime results."""
import ast
from copy import deepcopy
import io
import json
import tokenize
import unittest
from unittest.mock import patch

from builder.inputs import InvalidInput
from builder.source_compaction import compact_source
from builder.workflow import runtime_source


class SourceCompactionTests(unittest.TestCase):
    def assert_equivalent(self, source):
        result = compact_source(source)
        self.assertEqual(ast.dump(ast.parse(source)), ast.dump(ast.parse(result)))
        original_values, compact_values = {}, {}
        exec(source, original_values)
        exec(result, compact_values)
        self.assertEqual(original_values.get("answer"), compact_values.get("answer"))
        self.assertEqual(compact_source(result), result)
        self.assertNotIn("{{", result)
        self.assertNotIn("}}", result)
        return result

    def assert_pooled_equivalent(self, source):
        original = compact_source(source)
        result = compact_source(source, pool_literals=True)
        original_values, actual_values = {}, {}
        exec(original, original_values)
        exec(result, actual_values)
        self.assertEqual(original_values.get("answer"), actual_values.get("answer"))
        self.assertEqual(original_values.get("__doc__"), actual_values.get("__doc__"))
        self.assertLess(len(json.dumps(result)), len(json.dumps(original)))
        self.assertEqual(compact_source(result, pool_literals=True), result)
        # Independently expand the only new binding to check the compiler's
        # round-trip guarantee for these scope/literal counterexamples.
        original_tree, expanded = ast.parse(original), ast.parse(result)
        original_names = {node.id for node in ast.walk(original_tree) if isinstance(node, ast.Name)}
        pools = [(index, node) for index, node in enumerate(expanded.body)
                 if isinstance(node, ast.Assign) and len(node.targets) == 1
                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id not in original_names
                 and isinstance(node.value, ast.Tuple)]
        self.assertEqual(len(pools), 1)
        index, definition = pools[0]
        name, values = definition.targets[0].id, ast.literal_eval(definition.value)
        expanded.body.pop(index)

        class Expand(ast.NodeTransformer):
            def visit_Subscript(self, node):
                if isinstance(node.value, ast.Name) and node.value.id == name:
                    return ast.Constant(values[node.slice.value])
                return self.generic_visit(node)

        self.assertEqual(ast.dump(Expand().visit(expanded)), ast.dump(original_tree))
        self.assertNotIn("{{", result)
        self.assertNotIn("}}", result)
        return result, name

    def test_indentation_comments_blank_lines_and_continuations(self):
        source = '''# compiler-only comment
def compute(values):
    total = 0

    for value in values:
        if value > 0:
            total += (value  # comment inside continuation
                      * 2)
    return total

answer = compute([1, -2, 3])
'''
        result = self.assert_equivalent(source)
        self.assertIn("\n for value in values:\n  if value>0:total+=", result)
        self.assertNotIn("#", result)
        self.assertNotIn("\n\n", result)
        self.assertLess(len(result), len(source))

    def test_operator_number_and_keyword_boundaries(self):
        source = '''x = 8
x //= 2
x **= 3
x >>= 2
answer = (x, 1 .real, 1e-3, 3j, ...,
          not False, x is not None, 4 not in [3],
          2 ** 3, 2 * -3, 8 // -2,
          {'a': {'b': 9}}, {x for x in range(3)})
'''
        self.assert_equivalent(source)

    def test_multiline_raw_byte_unicode_and_adjacent_strings_remain_literal(self):
        source = '''text = """first line
    literal indentation # not a comment

  last line"""
answer = (text, r"\\n\\t", b"\\x41", "özel söz", "one" "two")
'''
        result = self.assert_equivalent(source)
        original_literals = [t.string for t in tokenize.generate_tokens(io.StringIO(source).readline)
                             if t.type == tokenize.STRING]
        result_literals = [t.string for t in tokenize.generate_tokens(io.StringIO(result).readline)
                           if t.type == tokenize.STRING]
        self.assertEqual(result_literals, original_literals)

    def test_fstrings_keep_debug_whitespace_formats_and_nested_source(self):
        source = '''value = 7
width = 4
answer = (f"{ value = }", f"{value:{width}d}", f"{f'{value:04d}'}",
          f"""first
    { value + 1 = }
last""")
'''
        result = self.assert_equivalent(source)
        for literal in ('f"{ value = }"', 'f"{value:{width}d}"', 'f"{f\'{value:04d}\'}"',
                        'f"""first\n    { value + 1 = }\nlast"""'):
            self.assertIn(literal, result)

    def test_fstring_source_offsets_ignore_unicode_line_separators_in_literals(self):
        source = 'prefix = "one\u2028two"\nvalue = 7\nanswer = f"{ value = }"\n'
        result = self.assert_equivalent(source)
        self.assertIn('f"{ value = }"', result)

    def test_literal_template_delimiters_fail_instead_of_rewriting_content(self):
        for source in ('answer = "{{variable}}"\n', 'answer = f"{{{{literal}}}}"\n'):
            with self.subTest(source=source), self.assertRaisesRegex(InvalidInput, "template delimiters"):
                compact_source(source)

    def test_ast_equality_gate_rejects_accidental_semantic_changes(self):
        tokens = list(tokenize.generate_tokens(io.StringIO("answer=2\n").readline))
        altered = [t._replace(string="3") if t.type == tokenize.NUMBER else t for t in tokens]
        with patch("builder.source_compaction._literal_tokens", return_value=iter(altered)):
            with self.assertRaisesRegex(InvalidInput, "changed the Python AST"):
                compact_source("answer=2\n")

    def test_runtime_emission_compacts_wrapper_and_keeps_full_ast(self):
        payload = {"synthetic": "exact payload text and braces {a}", "numbers": [1, 3, 7]}
        for mode in ("user", "result", "continue", "finalize"):
            with self.subTest(mode=mode):
                with patch("builder.workflow.compact_source", side_effect=lambda source, **options: source):
                    original = runtime_source(payload, mode)
                emitted = runtime_source(payload, mode)
                self.assertEqual(ast.dump(ast.parse(original)), ast.dump(ast.parse(compact_source(original))))
                self.assertEqual(emitted, compact_source(original, pool_literals=True))
                self.assertLess(len(emitted), len(original) * .9)
                self.assertNotIn("{{", emitted)
                self.assertNotIn("}}", emitted)
                self.assertIn("\ntry:\n import json;import re;", emitted)

    def test_emitted_payload_omits_only_unused_fields_without_mutation(self):
        payload = {"policy": {"wrong_answer_limit": 7}, "bindings": {"kept": "verbatim"},
                   "requests": {"tool": {"request": "unchanged"}},
                   "plan": {"stage_instructions": {"person": "Keep the caller safe."},
                            "confirmation": "combined", "product_order": "returned_order",
                            "first_persuasion": "last_business_day_this_or_next_week",
                            "second_persuasion": "floor_on_deadline",
                            "hardship_priority": "before_persuasion_and_restructuring",
                            "messages": {"opening": "Keep the caller safe.", "farewell": "Goodbye."},
                            "speech_templates": {"offer": "For [[product]], [[amount]]."},
                            "person_confirmation_attempts": 2}}
        before = deepcopy(payload)
        source = runtime_source(payload, "user")
        invocation = next(node for node in ast.walk(ast.parse(source))
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                          and node.func.id == "workflow_step")
        actual = json.loads(ast.literal_eval(invocation.args[1].args[0]))
        expected = deepcopy(payload)
        for key in ("stage_instructions", "confirmation", "product_order", "first_persuasion",
                    "second_persuasion", "hardship_priority"):
            del expected["plan"][key]
        del expected["plan"]["messages"]["opening"]
        self.assertEqual(actual, expected)
        self.assertEqual(payload, before)
        self.assertEqual(actual["plan"]["messages"], {"farewell": "Goodbye."})
        self.assertEqual(actual["plan"]["speech_templates"], payload["plan"]["speech_templates"])

    def test_payload_encoder_preserves_brace_runs_backslash_literals_and_unicode(self):
        payload = {"literal": r"\x7b literal \x7d", "quote": "Both ' and \" quotes.",
                   "unicode": "Ödeme günü", "nested": {"value": {"more": "unchanged"}},
                   "runs": ["{" * size + "name" + "}" * size for size in range(1, 9)]}
        source = runtime_source(payload, "user")
        invocation = next(node for node in ast.walk(ast.parse(source))
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                          and node.func.id == "workflow_step")
        actual = json.loads(ast.literal_eval(invocation.args[1].args[0]))
        self.assertEqual(actual, payload)
        self.assertNotIn("{{", source)
        self.assertNotIn("}}", source)

    def test_schema_projection_preserves_named_description_fields_and_constraints(self):
        schema = {"type": "object", "description": "Root documentation", "required": ["description"],
                  "additionalProperties": False, "properties": {
                      "description": {"type": "string", "description": "Field documentation",
                                      "pattern": "^[a-z]+$", "minLength": 1, "maxLength": 20,
                                      "enum": ["yes", "no"], "default": "no", "title": "Retained title"},
                      "events": {"type": "array", "description": "Array documentation", "maxItems": 3,
                                 "items": {"type": "object", "description": "Item documentation",
                                           "properties": {"description": {"type": "string"},
                                                          "day": {"type": "string", "format": "date"}}}}}}
        payload = {"requests": {"synthetic_tool": schema}}
        before = deepcopy(payload)
        source = runtime_source(payload, "user")
        invocation = next(node for node in ast.walk(ast.parse(source))
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                          and node.func.id == "workflow_step")
        actual = json.loads(ast.literal_eval(invocation.args[1].args[0]))
        expected = deepcopy(payload)
        projected = expected["requests"]["synthetic_tool"]
        del projected["description"]
        del projected["properties"]["description"]["description"]
        del projected["properties"]["events"]["description"]
        del projected["properties"]["events"]["items"]["description"]
        self.assertEqual(actual, expected)
        self.assertEqual(payload, before)

    def test_simple_suite_joining_preserves_clause_and_nested_scope_boundaries(self):
        source = '''events = []
for value in range(4):
    try:
        if value == 1:
            events.append("one")
            continue
        elif value == 2:
            raise ValueError("two")
        else:
            events.append(value)
    except ValueError:
        events.append("caught")
        events.append("after catch")
    else:
        events.append("ok")
    finally:
        events.append("finally")
else:
    events.append("loop end")
answer = events
'''
        result = self.assert_equivalent(source)
        self.assertIn('if value==1:events.append("one");continue', result)
        self.assertIn('except ValueError:events.append("caught");events.append("after catch")', result)

    def test_joining_preserves_decorators_docstrings_and_multiline_literal_contents(self):
        source = '''def decorator(fn):
    return fn
@decorator
def example():
    """Exact documentation.
    Including indentation."""
    text = """line one
    line two"""
    number = 3;
    return text, number
answer = (example(), example.__doc__)
'''
        result = self.assert_equivalent(source)
        self.assertIn("\n@decorator\ndef example():", result)
        self.assertIn('"""line one\n    line two"""', result)

    def test_match_cases_and_keyword_string_boundaries_remain_valid(self):
        source = '''def choose(value):
    match value:
        case "a":
            return r"one\\two"
        case _:
            return "" "x"
answer = (choose("a"), choose("other"), "value" in ["value"])
'''
        self.assert_equivalent(source)

    def test_pool_avoids_class_method_argument_alias_and_exception_name_collisions(self):
        source = '''import json as C5
C = 10
class C4:
    def value(self, C0, *C1, **C2):
        try:
            raise ValueError("ignored")
        except ValueError as C3:
            value = "A repeated exact controller message with meaningful punctuation."
        return (value, "A repeated exact controller message with meaningful punctuation.",
                "A repeated exact controller message with meaningful punctuation.", C0, C)
answer = C4().value(7)
'''
        _, name = self.assert_pooled_equivalent(source)
        self.assertNotIn(name, {"C", "C0", "C1", "C2", "C3", "C4", "C5"})

    def test_pool_preserves_fstrings_docstrings_and_match_literal_patterns(self):
        source = '''"""A repeated exact controller message with meaningful punctuation."""
def choose(value):
    """A repeated exact controller message with meaningful punctuation."""
    match value:
        case "A repeated exact controller message with meaningful punctuation.":
            return f"A repeated exact controller message with meaningful punctuation."
        case _:
            return f"literal {value!r}"
message = "A repeated exact controller message with meaningful punctuation."
answer = (choose(message), choose.__doc__, "A repeated exact controller message with meaningful punctuation.",
          "A repeated exact controller message with meaningful punctuation.")
'''
        result, _ = self.assert_pooled_equivalent(source)
        self.assertEqual([ast.dump(node) for node in ast.walk(ast.parse(result)) if isinstance(node, ast.JoinedStr)],
                         [ast.dump(node) for node in ast.walk(ast.parse(source)) if isinstance(node, ast.JoinedStr)])
        self.assertTrue(all(isinstance(node.value, ast.Constant) for node in ast.walk(ast.parse(result))
                            if isinstance(node, ast.MatchValue)))

    def test_pool_keeps_escaped_brace_lexemes_and_utf8_source_offsets(self):
        source = r'''prefix = "Önizleme"
answer = (prefix, '\x7b\x7bexact_template\x7d\x7d and a long literal suffix',
          '\x7b\x7bexact_template\x7d\x7d and a long literal suffix',
          '\x7b\x7bexact_template\x7d\x7d and a long literal suffix',
          r"\x7b literal backslash sequence retained exactly", r"\x7b literal backslash sequence retained exactly",
          r"\x7b literal backslash sequence retained exactly")
'''
        result, _ = self.assert_pooled_equivalent(source)
        self.assertIn(r"'\x7b\x7bexact_template\x7d\x7d and a long literal suffix'", result)
        self.assertIn(r'r"\x7b literal backslash sequence retained exactly"', result)

    def test_pool_follows_module_docstring_and_future_import_without_changing_annotations(self):
        source = '''"""Preserved module documentation."""
from __future__ import annotations
@(lambda fn: fn)
def annotated(value: "A repeated exact controller message with meaningful punctuation.") -> "A repeated exact controller message with meaningful punctuation.":
    return ("A repeated exact controller message with meaningful punctuation.",
            "A repeated exact controller message with meaningful punctuation.",
            "A repeated exact controller message with meaningful punctuation.")
answer = (annotated("value"), annotated.__annotations__)
'''
        result, _ = self.assert_pooled_equivalent(source)
        tree = ast.parse(result)
        self.assertEqual(ast.get_docstring(tree), "Preserved module documentation.")
        self.assertIsInstance(tree.body[1], ast.ImportFrom)
        self.assertEqual(tree.body[1].module, "__future__")

    def test_pool_skips_dynamic_namespace_inspection(self):
        source = '''before = tuple(globals())
answer = ("A repeated exact controller message with meaningful punctuation.",
          "A repeated exact controller message with meaningful punctuation.",
          "A repeated exact controller message with meaningful punctuation.")
'''
        self.assertEqual(compact_source(source, pool_literals=True), compact_source(source))

    def test_unused_birth_date_helper_is_not_bundled(self):
        source = runtime_source({"synthetic": "payload"}, "user")
        self.assertNotIn("numeric_birth_date", {node.name for node in ast.walk(ast.parse(source))
                                                if isinstance(node, ast.FunctionDef)})
        self.assertIn("normalize_birth_date", {node.name for node in ast.walk(ast.parse(source))
                                                if isinstance(node, ast.FunctionDef)})


if __name__ == "__main__":
    unittest.main()
