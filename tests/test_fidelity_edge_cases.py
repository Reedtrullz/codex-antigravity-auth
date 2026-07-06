import unittest
import time
from unittest.mock import patch
from codex_antigravity_auth.accounts import AccountManager
from codex_antigravity_auth.transform import resolve_backend_model
from codex_antigravity_auth.schema import clean_json_schema

class TestTransformationEdgeCases(unittest.TestCase):
    def test_resolve_backend_model_aliases(self):
        # Bare gemini models
        self.assertEqual(resolve_backend_model("gemini-3.5-flash-high"), "gemini-3-flash-agent")
        self.assertEqual(resolve_backend_model("gemini-3.5-flash-medium"), "gemini-3.5-flash-low")
        self.assertEqual(resolve_backend_model("gemini-3.5-flash-low"), "gemini-3.5-flash-low")
        self.assertEqual(resolve_backend_model("gemini-3.1-pro-high"), "gemini-3.1-pro-high")
        # Claude models
        self.assertEqual(resolve_backend_model("claude-3.5-sonnet"), "claude-sonnet-4-6")
        self.assertEqual(resolve_backend_model("claude-opus-4-6"), "claude-opus-4-6-thinking")
        self.assertEqual(resolve_backend_model("sonnet"), "claude-sonnet-4-6")
        self.assertEqual(resolve_backend_model("opus"), "claude-opus-4-6-thinking")
        self.assertEqual(resolve_backend_model("claude-sonnet"), "claude-sonnet-4-6")
        self.assertEqual(resolve_backend_model("claude-opus"), "claude-opus-4-6-thinking")
        self.assertEqual(resolve_backend_model("gemini-3.1-pro"), "gemini-3.1-pro-low")
        # Pre-fixed or unknown models passthrough
        self.assertEqual(resolve_backend_model("openai-responses/gemini-3.5-flash-high"), "gemini-3-flash-agent")
        self.assertEqual(resolve_backend_model("openai-responses/sonnet"), "claude-sonnet-4-6")
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
        self.assertIn("_placeholder", cleaned["required"])

    def test_account_rotation_consecutive_failure_backoff(self):
        manager = AccountManager()
        email = "fail-acc@gmail.com"

        with patch.object(manager, "_save_state_to_storage"):
            # Mark failure once
            manager.mark_failure(email, "Simulated network failure")
            cd1 = manager._cooldowns[email]
            duration1 = cd1 - time.time()
            self.assertTrue(110 <= duration1 <= 130, f"Expected ~120s cooldown, got {duration1}")

            # Mark failure twice
            manager.mark_failure(email, "Simulated network failure")
            cd2 = manager._cooldowns[email]
            duration2 = cd2 - time.time()
            self.assertTrue(230 <= duration2 <= 250, f"Expected ~240s cooldown, got {duration2}")

if __name__ == "__main__":
    unittest.main()
