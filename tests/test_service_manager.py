import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_antigravity_auth.service_manager import ServiceResult, ServiceState, observed_service_result
from codex_antigravity_auth.service import install_service, uninstall_service


class TestServiceResult(unittest.TestCase):
    def test_classifies_observed_service_states(self):
        cases = (
            ({"installed": False, "active": False, "reachable": False}, ServiceState.NOT_INSTALLED),
            ({"installed": True, "active": False, "reachable": False}, ServiceState.INSTALLED_INACTIVE),
            ({"installed": True, "active": True, "reachable": False}, ServiceState.ACTIVE_UNREACHABLE),
            ({"installed": True, "active": True, "reachable": True}, ServiceState.READY),
        )
        for values, expected in cases:
            with self.subTest(expected=expected):
                result = observed_service_result(action="status", changed=False, **values)
                self.assertEqual(result.state, expected)

    def test_error_always_produces_failed_state(self):
        result = observed_service_result(
            action="install",
            installed=True,
            active=False,
            reachable=False,
            changed=True,
            error="bootstrap failed",
        )
        self.assertEqual(result.state, ServiceState.FAILED)
        self.assertEqual(result.error, "bootstrap failed")

    def test_serialized_result_preserves_legacy_boolean_fields(self):
        result = ServiceResult(
            action="install",
            state=ServiceState.READY,
            installed=True,
            active=True,
            reachable=True,
            changed=True,
            commands=({"command": "launchctl bootstrap", "returncode": 0},),
        )
        payload = result.to_dict()
        self.assertTrue(payload["installed"])
        self.assertTrue(payload["active"])
        self.assertEqual(payload["state"], "ready")

    def test_macos_bootstrap_failure_is_reported_from_observed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gateway.plist"
            results = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 1, "", "bootstrap failed"),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 1, "", "not loaded"),
            ]
            with patch("codex_antigravity_auth.service.macos_launch_agent_path", return_value=path):
                with patch("codex_antigravity_auth.service._run", side_effect=results):
                    result = install_service(51122, "127.0.0.1", platform_name="macos")

        self.assertTrue(result["installed"])
        self.assertFalse(result["active"])
        self.assertEqual(result["state"], "failed")

    def test_windows_uninstall_failure_is_reported_when_task_still_exists(self):
        results = [
            subprocess.CompletedProcess([], 1, "", "delete failed"),
            subprocess.CompletedProcess([], 0, "exists", ""),
        ]
        with patch("codex_antigravity_auth.service._run", side_effect=results):
            result = uninstall_service(51122, platform_name="windows")

        self.assertTrue(result["installed"])
        self.assertEqual(result["state"], "failed")


if __name__ == "__main__":
    unittest.main()
