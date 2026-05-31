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

    def test_thought_signature_text_appears_in_output(self):
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
        # thoughtSignature parts now stay in output as regular text
        content_texts = [c["text"] for c in transformed["message"]["content"]]
        self.assertIn("Self-reflection thought process.", content_texts)
        self.assertIn("Actual answer.", content_texts)
        self.assertNotIn("reasoning", transformed)  # not misclassified as reasoning

if __name__ == "__main__":
    unittest.main()
