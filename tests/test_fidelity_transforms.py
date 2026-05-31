import unittest
import json
from codex_antigravity_auth.transform import transform_response, transform_gemini_candidate

class TestFidelityTransforms(unittest.TestCase):
    def test_fidelity_response_with_model_role_mapped_to_assistant(self):
        gemini_resp = {
            "response": {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "Hello world!"}]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 15
                }
            }
        }
        res = transform_response(gemini_resp, "gemini-3.5-flash-high")
        self.assertEqual(res["output"][0]["role"], "assistant")
        self.assertEqual(res["output"][0]["content"][0]["text"], "Hello world!")

    def test_thought_signature_extracted_to_reasoning(self):
        candidate = {
            "content": {
                "role": "model",
                "parts": [
                    {
                        "thoughtSignature": "dummy_sig",
                        "text": "Self-reflection thought process."
                    },
                    {
                        "text": "Actual answer."
                    }
                ]
            }
        }
        transformed = transform_gemini_candidate(candidate)
        self.assertIn("reasoning", transformed)
        self.assertEqual(transformed["reasoning"]["step_by_step_summary"], "Self-reflection thought process.")
        self.assertEqual(transformed["message"]["content"][0]["text"], "Actual answer.")

if __name__ == "__main__":
    unittest.main()
