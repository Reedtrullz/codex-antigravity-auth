import unittest
import json
from codex_antigravity_auth.transform import transform_request, transform_response

class TestTransform(unittest.TestCase):
    def test_string_input_transformation(self):
        req = {
            "model": "gemini-3.5-flash-high",
            "input": "What is the meaning of life?",
            "max_output_tokens": 1000
        }
        res = transform_request(req)
        self.assertEqual(res["model"], "gemini-3-flash-agent")
        self.assertEqual(res["request"]["contents"][0]["parts"][0]["text"], "What is the meaning of life?")
        
    def test_structured_input_transformation(self):
        req = {
            "model": "claude-3.5-sonnet",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Hello, how are you?"
                        }
                    ]
                }
            ]
        }
        res = transform_request(req)
        self.assertEqual(res["model"], "claude-sonnet-4-6")
        self.assertEqual(res["request"]["contents"][0]["parts"][0]["text"], "Hello, how are you?")
        self.assertEqual(res["request"]["toolConfig"]["functionCallingConfig"]["mode"], "VALIDATED")

    def test_response_transformation(self):
        gemini_resp = {
            "candidates": [
                {
                    "content": {
                        "role": "assistant",
                        "parts": [
                            {"text": "The meaning of life is 42."}
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15
            }
        }
        res = transform_response(gemini_resp, "gemini-3.5-flash-high")
        self.assertEqual(res["output"][0]["content"][0]["text"], "The meaning of life is 42.")
        self.assertEqual(res["usage"]["total_tokens"], 15)

if __name__ == "__main__":
    unittest.main()
