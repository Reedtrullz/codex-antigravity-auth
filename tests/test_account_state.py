import copy
import unittest

from codex_antigravity_auth.account_state import AccountState, migrate_account_state, scoped_cooldown_expiry
from codex_antigravity_auth.response_protocol import AttemptOutcome


class TestAccountStateMigration(unittest.TestCase):
    def test_scoped_cooldown_reader_combines_account_and_selected_family(self):
        value = {"account": 1_700_000_100, "claude": 1_700_000_200, "gemini": 1_700_000_050}
        self.assertEqual(scoped_cooldown_expiry(value, "claude"), 1_700_000_200)
        self.assertEqual(scoped_cooldown_expiry(value, "gemini"), 1_700_000_100)
        self.assertEqual(scoped_cooldown_expiry(1_700_000_300_000, "claude"), 1_700_000_300)

    def test_migrates_legacy_account_wide_state_and_millisecond_timestamps(self):
        data = {
            "accounts": [{"email": "person@example.com"}],
            "accountState": {
                "failures": {"person@example.com": 2},
                "cooldowns": {"person@example.com": 1_700_000_100_000},
                "counters": {
                    "person@example.com": {
                        "claude": {"total_requests": 3, "successes": 2, "failures": 1}
                    }
                },
            },
        }

        migrated, changed = migrate_account_state(data, now=1_700_000_000)

        state = migrated["accountState"]
        self.assertTrue(changed)
        self.assertEqual(state["schemaVersion"], 2)
        self.assertEqual(state["failures"]["person@example.com"]["account"], 2)
        self.assertEqual(state["cooldowns"]["person@example.com"]["account"], 1_700_000_100)
        self.assertEqual(state["failures"]["person@example.com"], {"account": 2})
        self.assertEqual(state["cooldowns"]["person@example.com"], {"account": 1_700_000_100})
        self.assertEqual(state["counters"]["person@example.com"]["claude"]["total_requests"], 3)

    def test_migration_is_idempotent(self):
        original = {
            "accounts": [{"email": "person@example.com"}],
            "accountState": {
                "failures": {"person@example.com": 1},
                "cooldowns": {"person@example.com": 1_700_000_100},
            },
        }
        first, first_changed = migrate_account_state(copy.deepcopy(original), now=1_700_000_000)
        second, second_changed = migrate_account_state(copy.deepcopy(first), now=1_700_000_000)

        self.assertTrue(first_changed)
        self.assertFalse(second_changed)
        self.assertEqual(first, second)

    def test_drops_expired_non_finite_zero_and_unknown_account_state(self):
        data = {
            "accounts": [{"email": "person@example.com"}],
            "accountState": {
                "failures": {"person@example.com": float("nan"), "missing@example.com": 9},
                "cooldowns": {"person@example.com": 0, "missing@example.com": 1_800_000_000},
                "counters": {"missing@example.com": {"claude": {"total_requests": 8}}},
            },
        }

        migrated, _changed = migrate_account_state(data, now=1_700_000_000)

        state = migrated["accountState"]
        self.assertEqual(state["failures"], {})
        self.assertEqual(state["cooldowns"], {})
        self.assertEqual(state["counters"], {})


class TestScopedAccountState(unittest.TestCase):
    def setUp(self):
        self.now = 1_700_000_000.0
        self.data = {
            "accounts": [
                {"email": "primary@example.com", "accessToken": "one"},
                {"email": "secondary@example.com", "accessToken": "two"},
            ],
            "activeIndexByFamily": {"claude": 0, "gemini": 0},
            "accountState": {"schemaVersion": 2, "failures": {}, "cooldowns": {}, "counters": {}},
        }

    def test_family_cooldown_does_not_block_other_family(self):
        self.data["accountState"]["cooldowns"] = {
            "primary@example.com": {"claude": self.now + 300}
        }
        state = AccountState(self.data, now=lambda: self.now)

        claude = state.acquire("claude")
        state.release(claude)
        gemini = state.acquire("gemini")

        self.assertEqual(claude.account["email"], "secondary@example.com")
        self.assertEqual(gemini.account["email"], "primary@example.com")

    def test_account_auth_cooldown_blocks_every_family(self):
        self.data["accountState"]["cooldowns"] = {
            "primary@example.com": {"account": self.now + 300}
        }
        state = AccountState(self.data, now=lambda: self.now)

        self.assertEqual(state.acquire("claude").account["email"], "secondary@example.com")
        state.release_email("secondary@example.com")
        self.assertEqual(state.acquire("gemini").account["email"], "secondary@example.com")

    def test_active_indices_and_in_flight_counts_are_family_aware(self):
        self.data["activeIndexByFamily"] = {"claude": 1, "gemini": 0}
        state = AccountState(self.data, now=lambda: self.now)

        claude = state.acquire("claude")
        gemini = state.acquire("gemini")

        self.assertEqual(claude.account["email"], "secondary@example.com")
        self.assertEqual(gemini.account["email"], "primary@example.com")
        self.assertEqual(state.in_flight("secondary@example.com"), 1)
        state.release(claude)
        self.assertEqual(state.in_flight("secondary@example.com"), 0)

    def test_records_one_attempt_and_scopes_failures(self):
        state = AccountState(self.data, now=lambda: self.now)
        lease = state.acquire("claude")
        state.record(
            lease,
            AttemptOutcome(scope="family", category="rate_limit", retry_after_seconds=60),
        )

        persisted = self.data["accountState"]
        counter = persisted["counters"]["primary@example.com"]["claude"]
        self.assertEqual(counter["total_requests"], 1)
        self.assertEqual(counter["failures"], 1)
        self.assertEqual(counter["rate_limits"], 1)
        self.assertEqual(persisted["failures"]["primary@example.com"]["claude"], 1)
        self.assertGreater(persisted["cooldowns"]["primary@example.com"]["claude"], self.now)

        state.record(lease, AttemptOutcome(scope="account", category="auth"))
        self.assertEqual(counter["total_requests"], 2)
        self.assertEqual(persisted["failures"]["primary@example.com"]["account"], 1)


if __name__ == "__main__":
    unittest.main()
