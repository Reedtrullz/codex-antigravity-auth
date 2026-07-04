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

    def test_required_is_normalized_to_valid_string_list(self):
        self.assertEqual(
            clean_json_schema({"type": "object", "properties": {"q": {"type": "string"}}, "required": "q"})["required"],
            ["q"],
        )

        cleaned = clean_json_schema(
            {
                "type": "object",
                "properties": {"q": {"type": "string"}, "n": {"type": "number"}},
                "required": ["q", 1, "", "q", "n"],
            }
        )

        self.assertEqual(cleaned["required"], ["q", "n"])

    def test_recursive_local_ref_schema_does_not_recurse_forever(self):
        raw_schema = {
            "type": "object",
            "$defs": {
                "node": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "child": {"$ref": "#/$defs/node"},
                    },
                }
            },
            "properties": {"root": {"$ref": "#/$defs/node"}},
        }

        cleaned = clean_json_schema(raw_schema)

        root = cleaned["properties"]["root"]
        self.assertEqual(root["type"], "object")
        self.assertEqual(root["properties"]["name"]["type"], "string")
        self.assertEqual(root["properties"]["child"], {})

if __name__ == "__main__":
    unittest.main()
