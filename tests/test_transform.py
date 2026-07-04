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

    def test_google_generation_options_are_forwarded(self):
        req = {
            "model": "gemini-3.5-flash-high",
            "input": "Write a short answer.",
            "temperature": 0.2,
            "top_p": 0.7,
            "max_output_tokens": 123,
            "stop": ["END", "STOP"],
        }

        res = transform_request(req)

        self.assertEqual(
            res["request"]["generationConfig"],
            {
                "temperature": 0.2,
                "topP": 0.7,
                "maxOutputTokens": 123,
                "stopSequences": ["END", "STOP"],
            },
        )
        
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
        # No tools in request → no toolConfig key
        self.assertNotIn("toolConfig", res["request"])

    def test_claude_thinking_budget_stays_below_explicit_max_output_tokens(self):
        req = {
            "model": "claude-3.5-sonnet",
            "input": "Hello",
            "max_output_tokens": 4096,
        }

        res = transform_request(req)

        generation_config = res["request"]["generationConfig"]
        self.assertEqual(generation_config["maxOutputTokens"], 4096)
        self.assertEqual(generation_config["thinkingConfig"]["thinking_budget"], 4095)

    def test_claude_xhigh_reasoning_budget_is_not_lower_than_high(self):
        high = transform_request(
            {"model": "claude-opus-4-6", "input": "Hello", "reasoning": {"effort": "high"}}
        )
        xhigh = transform_request(
            {"model": "claude-opus-4-6", "input": "Hello", "reasoning": {"effort": "xhigh"}}
        )

        self.assertEqual(high["request"]["generationConfig"]["thinkingConfig"]["thinking_budget"], 16000)
        self.assertEqual(xhigh["request"]["generationConfig"]["thinkingConfig"]["thinking_budget"], 32000)

    def test_claude_omits_thinking_config_when_token_cap_cannot_exceed_budget(self):
        req = {
            "model": "claude-3.5-sonnet",
            "input": "Hello",
            "max_output_tokens": 1024,
        }

        res = transform_request(req)

        self.assertEqual(res["request"]["generationConfig"]["maxOutputTokens"], 1024)
        self.assertNotIn("thinkingConfig", res["request"]["generationConfig"])

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

    def test_response_transformation_sums_total_tokens_when_backend_omits_total(self):
        gemini_resp = {
            "candidates": [{"content": {"role": "assistant", "parts": [{"text": "hello"}]}}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
        }

        res = transform_response(gemini_resp, "gemini-3.5-flash-high")

        self.assertEqual(res["usage"], {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})

if __name__ == "__main__":
    unittest.main()
