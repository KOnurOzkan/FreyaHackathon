"""Allow ordinary nested Python structures without changing string contents."""
from unittest.mock import patch

from builder import workflow as compiler
from builder.contracts import Contracts
from builder.inputs import InvalidInput, read_json
from tests.support import OfflineCase, ROOT
from tests.test_extraction import fake_extraction
from tests.workflow_support import fake_plan


class RuntimeEmitterTests(OfflineCase):
    def payload(self):
        contracts = Contracts(read_json(ROOT / "tools/openapi.json"))
        _, report = compiler.compile_workflow(fake_extraction(self.documents[:1], self.a_raw),
            fake_plan(self.documents[:1], False, contracts), self.documents[:1], contracts)
        return report["runtime_payload"]

    def source_with_probe(self, probe):
        payload = self.payload()
        original = compiler._definitions

        def definitions(module, *args, **kwargs):
            source = original(module, *args, **kwargs)
            if module is compiler.workflow_runtime:
                source += "\n" + probe
            return source

        with patch.object(compiler, "_definitions", side_effect=definitions):
            return compiler.runtime_source(payload, "user")

    def test_nested_brace_tokens_compile_and_keep_runtime_and_data_semantics(self):
        source = self.source_with_probe(
            "def emitter_probe():\n"
            "    number = 7\n"
            "    return {'outer': {'inner': {'number': number}}, 'text': '{unchanged}', "
            "'set': {1, 2}, 'formatted': f'{number:02d} / {number:.2f}'}\n")
        self.assertNotIn("{{", source)
        self.assertNotIn("}}", source)
        variables = {"now": "2031-04-09T12:00", "customer_id": "synthetic-emitter",
                     "fb_person_reply": 'person_yes'}
        namespace = {"vars": variables}
        exec(source, namespace)
        self.assertEqual(variables["fb_next"], "speak_identity")
        self.assertEqual(namespace["emitter_probe"](),
                         {"outer": {"inner": {"number": 7}}, "text": "{unchanged}",
                          "set": {1, 2}, "formatted": "07 / 7.00"})

    def test_literal_double_braces_are_rejected_without_rewriting_string_data(self):
        for delimiter in ("{{", "}}"):
            with self.subTest(delimiter=delimiter), self.assertRaisesRegex(InvalidInput, "unsafe template delimiters"):
                self.source_with_probe("def emitter_probe():\n    return " + repr(delimiter) + "\n")
