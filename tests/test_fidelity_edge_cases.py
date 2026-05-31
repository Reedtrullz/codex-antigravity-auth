import unittest
import time
from codex_antigravity_auth.accounts import AccountManager
from codex_antigravity_auth.transform import resolve_backend_model
from codex_antigravity_auth.schema import clean_json_schema

class TestTransformationEdgeCases(unittest.TestCase):
    def test_resolve_backend_model_aliases(self):
        # Bare gemini models
        self.assertEqual(resolve_backend_model("gemini-3.5-flash-high"), "gemini-3-flash-agent")
        self.assertEqual(resolve_backend_model("gemini-3.5-flash-medium"), "gemini-3.5-flash-low")
        self.assertEqual(resolve_backend_model("gemini-3.1-pro-high"), "gemini-3.1-pro-high")
        # Claude models
        self.assertEqual(resolve_backend_model("claude-3.5-sonnet"), "claude-sonnet-4-6")
        self.assertEqual(resolve_backend_model("claude-opus-4-6"), "claude-opus-4-6-thinking")
        # Pre-fixed or unknown models passthrough
        self.assertEqual(resolve_backend_model("openai-responses/gemini-3.5-flash-high"), "gemini-3-flash-agent")
        self.assertEqual(resolve_backend_model("unknown-custom-model"), "unknown-custom-model")

    def test_clean_json_schema_edge_cases(self):
        # Deeply nested object schemas
        nested_schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 3},
                        "settings": {
                            "type": "object",
                            "properties": {
                                "theme": {"type": "string", "enum": ["dark", "light"]}
                            }
                        }
                    }
                }
            }
        }
        cleaned = clean_json_schema(nested_schema)
        
        # Verify minLength constraint is recursively removed
        user_props = cleaned["properties"]["user"]["properties"]
        self.assertNotIn("minLength", user_props["name"])
        
        # Verify placeholder injection on empty nested object schemas
        # settings has theme so it has properties and doesn't get a placeholder,
        # but cleaned outer schemas have properties and required is injected
        self.assertIn("_placeholder", cleaned["required"])

if __name__ == "__main__":
    unittest.main()
