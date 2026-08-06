import unittest
import time
import tempfile
from pathlib import Path
from unittest.mock import patch
from codex_antigravity_auth.accounts import AccountManager
from codex_antigravity_auth.models import canonical_model_id
from codex_antigravity_auth.transform import (
    INTERNAL_PLACEHOLDER_ARGUMENT,
    clean_function_call_args,
    resolve_backend_model,
    transform_request,
)
from codex_antigravity_auth.schema import clean_json_schema
from tests.conftest import _legacy_transform_response as transform_response

class TestTransformationEdgeCases(unittest.TestCase):
    def test_colon_form_reserved_prefixes_resolve_like_slash_form(self):
        # `openai:sonnet` is the natural colon spelling (used by every other
        # provider prefix) and must resolve the same way `openai/sonnet` does.
        self.assertEqual(canonical_model_id("openai:sonnet"), canonical_model_id("openai/sonnet"))
        self.assertEqual(canonical_model_id("openai:sonnet"), "claude-3.5-sonnet")
        self.assertEqual(canonical_model_id("openai-responses:opus"), "claude-opus-4-6")
        # Non-reserved colon prefixes (BYOK ids) must be preserved verbatim.
        self.assertEqual(canonical_model_id("deepseek:deepseek-chat"), "deepseek:deepseek-chat")

    def test_placeholder_marker_is_stripped_alongside_real_arguments(self):
        # The schema-injected _placeholder marker must never reach Codex, even
        # when the model emitted legitimate arguments in the same call.
        self.assertEqual(
            clean_function_call_args({INTERNAL_PLACEHOLDER_ARGUMENT: True, "q": "x"}),
            {"q": "x"},
        )
        self.assertEqual(clean_function_call_args({INTERNAL_PLACEHOLDER_ARGUMENT: True}), {})
        self.assertEqual(clean_function_call_args({"q": "x"}), {"q": "x"})

    def test_input_image_with_only_file_id_is_not_silently_dropped(self):
        req = {
            "model": "gemini-3.5-flash-high",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": "img_abc123"},
                        {"type": "input_text", "text": "what is this?"},
                    ],
                }
            ],
        }
        res = transform_request(req)
        parts = res["request"]["contents"][0]["parts"]
        self.assertTrue(
            any("img_abc123" in part.get("text", "") for part in parts),
            "file_id-only image part must survive as a text fallback, not vanish",
        )

    def test_empty_conversation_produces_well_formed_contents(self):
        req = {"model": "gemini-3.5-flash-high", "input": []}
        res = transform_request(req)
        self.assertTrue(res["request"]["contents"], "empty input must not produce empty contents")

    def test_unknown_function_call_output_call_id_is_dropped(self):
        req = {
            "model": "gemini-3.5-flash-high",
            "input": [
                {"type": "function_call_output", "call_id": "call_unknown", "output": "result"},
            ],
        }
        res = transform_request(req)
        contents = res["request"]["contents"]
        self.assertFalse(
            any("functionResponse" in part for content in contents for part in content.get("parts", [])),
            "orphan call ids must not be emitted under a fabricated function name",
        )

    def test_response_part_with_text_and_function_call_keeps_both(self):
        gemini_resp = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "Calling now.",
                                "functionCall": {"name": "lookup_code", "args": {"code": "alpha"}},
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        output = transform_response(gemini_resp, "gemini-3.5-flash-high")
        output_texts = [
            part.get("text")
            for item in output["output"]
            if isinstance(item.get("content"), list)
            for part in item["content"]
            if isinstance(part, dict)
        ]
        self.assertIn("Calling now.", output_texts)
        self.assertTrue(
            any(item["type"] == "function_call" and item["name"] == "lookup_code" for item in output["output"]),
            "function call must survive when the part also carries text",
        )

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

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "codex_antigravity_auth.accounts.get_accounts_json_path",
                return_value=Path(tmp) / "missing.json",
            ):
            # Mark failure once
                manager.mark_failure(email, "Simulated network failure")
                cd1 = manager._cooldowns[email]["account"]
                duration1 = cd1 - time.time()
                self.assertTrue(110 <= duration1 <= 130, f"Expected ~120s cooldown, got {duration1}")

                # Mark failure twice
                manager.mark_failure(email, "Simulated network failure")
                cd2 = manager._cooldowns[email]["account"]
                duration2 = cd2 - time.time()
                self.assertTrue(230 <= duration2 <= 250, f"Expected ~240s cooldown, got {duration2}")

if __name__ == "__main__":
    unittest.main()
