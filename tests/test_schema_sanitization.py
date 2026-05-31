import unittest
from codex_antigravity_auth.schema import clean_json_schema

class TestSchemaSanitization(unittest.TestCase):
    def test_schema_cleaning_strips_unsupported_keys(self):
        raw_schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 100,
                    "format": "email",
                    "pattern": "^[a-zA-Z]+$"
                }
            },
            "required": ["query"]
        }
        
        cleaned = clean_json_schema(raw_schema)
        # Should recursively strip minLength, maxLength, format, and pattern
        props = cleaned["properties"]["query"]
        self.assertNotIn("minLength", props)
        self.assertNotIn("maxLength", props)
        self.assertNotIn("format", props)
        self.assertNotIn("pattern", props)
        self.assertEqual(props["type"], "string")

    def test_validated_mode_injects_placeholder_for_empty_required(self):
        raw_schema = {
            "type": "object",
            "properties": {
                "optional_field": {"type": "string"}
            },
            "required": []
        }
        
        cleaned = clean_json_schema(raw_schema)
        self.assertIn("_placeholder", cleaned["properties"])
        self.assertIn("_placeholder", cleaned["required"])
        self.assertEqual(cleaned["properties"]["_placeholder"]["type"], "boolean")

if __name__ == "__main__":
    unittest.main()
