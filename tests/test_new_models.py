"""Gemini 4 Argon and Claude 5.5 model registration and anti skill table parity."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from codex_antigravity_auth.models import (
    NATIVE_MODELS,
    canonical_model_id,
    native_model_catalog,
    native_model_family,
    resolve_backend_model,
)
from codex_antigravity_auth.unified import classify_route

ANTI_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "codex_antigravity_auth"
    / "skills"
    / "anti"
    / "scripts"
    / "anti.py"
)

NEW_MODELS = {
    "gemini-4-argon": ("gemini-4-argon-high", "gemini"),
    "claude-sonnet-5-5": ("claude-sonnet-5-5", "claude"),
    "claude-opus-5-5-thinking": ("claude-opus-5-5-thinking", "claude"),
}


def load_anti():
    spec = importlib.util.spec_from_file_location("anti_skill_models_parity", ANTI_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestNewNativeModels(unittest.TestCase):
    def test_canonical_ids_and_backend_ids(self):
        for model_id, (backend_id, family) in NEW_MODELS.items():
            with self.subTest(model=model_id):
                self.assertEqual(canonical_model_id(model_id), model_id)
                self.assertEqual(resolve_backend_model(model_id), backend_id)
                self.assertEqual(native_model_family(model_id), family)

    def test_dotted_and_short_aliases(self):
        cases = {
            "claude-sonnet-5.5": "claude-sonnet-5-5",
            "sonnet-5.5": "claude-sonnet-5-5",
            "claude-opus-5.5": "claude-opus-5-5-thinking",
            "claude-opus-5-5": "claude-opus-5-5-thinking",
            "opus-5.5": "claude-opus-5-5-thinking",
            "argon": "gemini-4-argon",
        }
        for alias, canonical in cases.items():
            with self.subTest(alias=alias):
                self.assertEqual(canonical_model_id(alias), canonical)

    def test_argon_effort_variants_keep_their_wire_id(self):
        self.assertEqual(resolve_backend_model("gemini-4-argon-high"), "gemini-4-argon-high")
        self.assertEqual(resolve_backend_model("gemini-4-argon-medium"), "gemini-4-argon-medium")
        self.assertEqual(canonical_model_id("gemini-4-argon-medium"), "gemini-4-argon")

    def test_bare_family_aliases_still_point_at_4_6(self):
        self.assertEqual(canonical_model_id("sonnet"), "claude-sonnet-4-6")
        self.assertEqual(canonical_model_id("opus"), "claude-opus-4-6-thinking")

    def test_catalog_lists_new_models(self):
        ids = [entry["id"] for entry in native_model_catalog()]
        for model_id in NEW_MODELS:
            self.assertIn(model_id, ids)

    def test_builtin_identifiers_do_not_collide(self):
        owners: dict[str, str] = {}
        for model in NATIVE_MODELS:
            for value in (model.id, *model.aliases):
                owner = owners.setdefault(value.lower(), model.id)
                self.assertEqual(owner, model.id, f"{value} shadows {owner}")

    def test_new_models_route_to_antigravity(self):
        for model_id in NEW_MODELS:
            with self.subTest(model=model_id):
                self.assertEqual(classify_route(model_id, unified_enabled=True), "antigravity")


class TestAntiSkillTablesCoverNewModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.anti = load_anti()

    def test_aliases_resolve_to_gateway_models(self):
        gateway_ids = {entry["id"] for entry in native_model_catalog()}
        for alias in ("argon", "argon-high", "argon-medium", "sonnet-5.5", "opus-5.5", "claude-opus-5.5"):
            with self.subTest(alias=alias):
                target = self.anti.resolve_model(alias, default="x")
                self.assertIn(canonical_model_id(target), gateway_ids)

    def test_tables_define_every_new_model(self):
        for model in (
            "gemini-4-argon",
            "gemini-4-argon-high",
            "gemini-4-argon-medium",
            "claude-sonnet-5-5",
            "claude-opus-5-5-thinking",
        ):
            with self.subTest(model=model):
                self.assertIn(model, self.anti.MODEL_CAPABILITIES)
                self.assertEqual(self.anti.MODEL_COST_TIER[model], "quota")
                self.assertGreater(self.anti.MODEL_QUALITY_RANK[model], 0)

    def test_existing_defaults_are_unchanged(self):
        self.assertEqual(self.anti.resolve_model("sonnet", default="x"), "claude-sonnet-4-6")
        self.assertEqual(self.anti.resolve_model("opus", default="x"), "claude-opus-4-6-thinking")


if __name__ == "__main__":
    unittest.main()
