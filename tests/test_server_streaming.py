import unittest
import json
import httpx
from unittest.mock import patch, MagicMock
from codex_antigravity_auth.server import app
from fastapi.testclient import TestClient

class TestServerStreaming(unittest.TestCase):
    def test_sse_generator_translation_output(self):
        with TestClient(app) as test_client:
            import codex_antigravity_auth.server as server
            
            fake_account = {
                "email": "test@gmail.com",
                "accessToken": "dummy_access",
                "fingerprint": {
                    "deviceId": "dev_123",
                    "sessionToken": "session_123",
                    "userAgent": "Antigravity/2.0.0",
                    "apiClient": "google-cloud-sdk"
                }
            }
            
            server.account_manager.select_active_account = MagicMock(return_value=fake_account)
            
            codex_payload = {
                "model": "gemini-3.5-flash-high",
                "input": "Write a short story about AI",
                "stream": True
            }
            
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            
            google_sse_chunks = [
                'data: {"candidates": [{"content": {"parts": [{"text": "Once"}]}}]}\n',
                'data: {"candidates": [{"content": {"parts": [{"text": " upon"}]}}]}\n',
                'data: {"candidates": [{"content": {"parts": [{"text": " a time"}]}}]}\n',
                'data: [DONE]\n'
            ]
            
            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)
            
            mock_response.aiter_text = MagicMock(return_value=AsyncAiterText(google_sse_chunks))
            
            class StreamContext:
                async def __aenter__(self):
                    return mock_response
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            class MockClientInstance:
                def stream(self, *args, **kwargs):
                    return StreamContext()
                
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass
                async def __aenter__(self):
                    return MockClientInstance()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                response = test_client.post("/v1/responses", json=codex_payload)
                self.assertEqual(response.status_code, 200)
                
                lines = response.text.split("\n")
                
                created_lines = [l for l in lines if "response.created" in l]
                delta_lines = [l for l in lines if "response.output_text.delta" in l]
                done_lines = [l for l in lines if "response.completed" in l]
                
                self.assertTrue(len(created_lines) > 0, "Missing response.created event")
                self.assertTrue(len(delta_lines) > 0, "Missing response.output_text.delta event")
                self.assertTrue(len(done_lines) > 0, "Missing response.completed event")

    def test_sse_generator_handling_wrapped_responses(self):
        with TestClient(app) as test_client:
            import codex_antigravity_auth.server as server
            fake_account = {
                "email": "test@gmail.com",
                "accessToken": "dummy_access",
                "fingerprint": {
                    "deviceId": "dev_123",
                    "sessionToken": "session_123",
                    "userAgent": "Antigravity/2.0.0",
                    "apiClient": "google-cloud-sdk"
                }
            }
            server.account_manager.select_active_account = MagicMock(return_value=fake_account)
            
            codex_payload = {
                "model": "gemini-3.5-flash-high",
                "input": "Write a short story about AI",
                "stream": True
            }
            
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            
            google_sse_chunks = [
                'data: {"response": {"candidates": [{"content": {"parts": [{"text": "Hello stream"}]}}]}}\n',
                'data: [DONE]\n'
            ]
            
            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)
            
            mock_response.aiter_text = MagicMock(return_value=AsyncAiterText(google_sse_chunks))
            
            class StreamContext:
                async def __aenter__(self):
                    return mock_response
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            class MockClientInstance:
                def stream(self, *args, **kwargs):
                    return StreamContext()
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass
                async def __aenter__(self):
                    return MockClientInstance()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                response = test_client.post("/v1/responses", json=codex_payload)
                self.assertEqual(response.status_code, 200)
                
                lines = response.text.split("\n")
                delta_lines = [l for l in lines if "response.output_text.delta" in l]
                self.assertTrue(len(delta_lines) > 0, "Missing response.output_text.delta event for nested wrapped response")
                self.assertIn("Hello stream", delta_lines[0])

if __name__ == "__main__":
    unittest.main()
