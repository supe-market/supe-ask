import unittest

from supe_ask.services.prompts import ASK_RESPONSE_JSON_SCHEMA, build_codegen_system_prompt


class CodegenPromptSchemaTests(unittest.TestCase):
    def test_artifact_plan_schema_no_longer_requires_key_highlights(self):
        artifact_plan = ASK_RESPONSE_JSON_SCHEMA["schema"]["properties"]["artifact_plan"]

        self.assertNotIn("key_highlights", artifact_plan["properties"])
        self.assertEqual(artifact_plan["required"], ["report_sections", "suggested_next_questions", "artifacts"])

    def test_codegen_system_prompt_requires_runtime_highlights_artifact(self):
        prompt = build_codegen_system_prompt()

        self.assertIn("emit_highlights(...)", prompt)
        self.assertIn("Final highlight values must come from executed code", prompt)
        self.assertNotIn("artifact_plan.key_highlights field must contain", prompt)


if __name__ == "__main__":
    unittest.main()
