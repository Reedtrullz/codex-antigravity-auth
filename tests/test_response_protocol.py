import unittest

from fastapi import HTTPException

from codex_antigravity_auth.response_protocol import (
    AttemptOutcome,
    CapabilityError,
    ProviderCapabilities,
    ProviderResult,
    ProviderTerminal,
    ProtocolStateError,
    ResponseEventBuilder,
    TerminalKind,
    classify_terminal,
    normalize_usage,
    refusal_item,
    validate_capabilities,
)


def message_item(text: str) -> dict:
    return {
        "type": "message",
        "id": "msg_1",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


class TestTerminalClassification(unittest.TestCase):
    def test_classifies_meaningful_output_as_completed(self):
        cases = {
            "text": [message_item("hello")],
            "reasoning": [{"type": "reasoning", "id": "rs_1", "step_by_step_summary": "because"}],
            "function": [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": "{}"}],
            "refusal": [refusal_item({"blockReason": "SAFETY"})],
        }

        for label, output in cases.items():
            with self.subTest(label=label):
                terminal = classify_terminal(output=output, finish_reason="STOP", safety_block=None)
                self.assertEqual(terminal.kind, TerminalKind.COMPLETED)

    def test_classifies_token_limit_as_incomplete(self):
        for reason in ("MAX_TOKENS", "length", "max_output_tokens"):
            with self.subTest(reason=reason):
                terminal = classify_terminal(output=[message_item("partial")], finish_reason=reason, safety_block=None)
                self.assertEqual(terminal.kind, TerminalKind.INCOMPLETE)
                self.assertEqual(terminal.incomplete_reason, "max_output_tokens")

    def test_classifies_empty_clean_response_as_failed(self):
        terminal = classify_terminal(output=[], finish_reason="STOP", safety_block=None)

        self.assertEqual(terminal.kind, TerminalKind.FAILED)
        self.assertEqual(terminal.error_code, "empty_response")

    def test_classifies_malformed_response_as_failed(self):
        terminal = classify_terminal(output=[message_item("ignored")], finish_reason="STOP", safety_block=None, malformed=True)

        self.assertEqual(terminal.kind, TerminalKind.FAILED)
        self.assertEqual(terminal.error_code, "malformed_provider_response")

    def test_safety_block_requires_explicit_refusal_output(self):
        terminal = classify_terminal(output=[], finish_reason="SAFETY", safety_block={"blockReason": "SAFETY"})

        self.assertEqual(terminal.kind, TerminalKind.FAILED)
        self.assertEqual(terminal.error_code, "blocked_without_refusal")

    def test_refusal_does_not_echo_untrusted_provider_detail(self):
        item = refusal_item({"blockReason": "SAFETY secret-sentinel\nraw-policy"})

        refusal = item["content"][0]["refusal"]
        self.assertNotIn("secret-sentinel", refusal)
        self.assertNotIn("raw-policy", refusal)


class TestResponseEventBuilder(unittest.TestCase):
    def setUp(self):
        self.builder = ResponseEventBuilder(response_id="resp_test", model="test-model", created_at=123)

    def test_emits_monotonic_sequence_numbers_and_stable_item_identity(self):
        events = [self.builder.created()]
        events.extend(self.builder.add_output_item(message_item("hello")))
        result = ProviderResult(
            output=(message_item("hello"),),
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            terminal=ProviderTerminal(TerminalKind.COMPLETED, "stop"),
        )
        events.append(self.builder.terminal(result))

        self.assertEqual([event["sequence_number"] for event in events], list(range(len(events))))
        added, done = events[1:3]
        self.assertEqual(added["output_index"], 0)
        self.assertEqual(done["output_index"], 0)
        self.assertEqual(added["item"]["id"], done["item"]["id"])
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(self.builder.done_marker(), "[DONE]")

    def test_preserves_unique_function_call_indices(self):
        self.builder.created()
        first = self.builder.add_output_item({"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "one", "arguments": "{}"})
        second = self.builder.add_output_item({"type": "function_call", "id": "fc_2", "call_id": "call_2", "name": "two", "arguments": "{}"})

        self.assertEqual(first[0]["output_index"], 0)
        self.assertEqual(second[0]["output_index"], 1)
        self.assertEqual(first[0]["item"]["id"], "fc_1")
        self.assertEqual(second[0]["item"]["id"], "fc_2")

    def test_rejects_duplicate_terminal_output_after_terminal_and_duplicate_done(self):
        self.builder.created()
        result = ProviderResult(output=(), usage=normalize_usage(), terminal=ProviderTerminal(TerminalKind.FAILED, "empty"))
        self.builder.terminal(result)

        with self.assertRaises(ProtocolStateError):
            self.builder.terminal(result)
        with self.assertRaises(ProtocolStateError):
            self.builder.add_output_item(message_item("late"))
        self.assertEqual(self.builder.done_marker(), "[DONE]")
        with self.assertRaises(ProtocolStateError):
            self.builder.done_marker()

    def test_requires_created_before_output_and_terminal_before_done(self):
        with self.assertRaises(ProtocolStateError):
            self.builder.add_output_item(message_item("early"))
        with self.assertRaises(ProtocolStateError):
            self.builder.done_marker()


class TestCapabilityValidation(unittest.TestCase):
    def setUp(self):
        self.full = ProviderCapabilities(
            native_responses=True,
            parallel_tool_calls=True,
            structured_output=True,
            stop_sequences=True,
            reasoning=True,
            streaming_usage=True,
        )
        self.tool = {"type": "function", "name": "lookup", "description": "Look up a value", "parameters": {"type": "object"}}

    def test_accepts_all_provider_neutral_tool_choice_forms(self):
        choices = (
            "auto",
            "none",
            "required",
            {"type": "function", "name": "lookup"},
            {"type": "function", "function": {"name": "lookup"}},
        )
        for choice in choices:
            with self.subTest(choice=choice):
                validate_capabilities({"tools": [self.tool], "tool_choice": choice}, self.full)

    def test_rejects_function_choice_not_advertised(self):
        with self.assertRaisesRegex(CapabilityError, "not advertised"):
            validate_capabilities({"tools": [self.tool], "tool_choice": {"type": "function", "name": "missing"}}, self.full)

    def test_rejects_parallel_tool_control_when_route_cannot_honor_it(self):
        limited = ProviderCapabilities(
            native_responses=False,
            parallel_tool_calls=False,
            structured_output=False,
            stop_sequences=False,
            reasoning=False,
            streaming_usage=False,
        )

        with self.assertRaisesRegex(CapabilityError, "parallel_tool_calls"):
            validate_capabilities({"parallel_tool_calls": False}, limited)

    def test_rejects_unsupported_tool_choice_mode(self):
        limited = ProviderCapabilities(
            native_responses=False,
            parallel_tool_calls=True,
            structured_output=False,
            stop_sequences=False,
            reasoning=False,
            streaming_usage=False,
            tool_choice_modes=frozenset({"auto", "none"}),
        )

        with self.assertRaisesRegex(CapabilityError, "required"):
            validate_capabilities({"tools": [self.tool], "tool_choice": "required"}, limited)

    def test_required_tool_choice_needs_an_advertised_function(self):
        with self.assertRaisesRegex(CapabilityError, "advertised function"):
            validate_capabilities({"tool_choice": "required"}, self.full)

    def test_gateway_rejects_unadvertised_forced_function_before_routing(self):
        from codex_antigravity_auth.server import validate_response_request_body

        with self.assertRaises(HTTPException) as raised:
            validate_response_request_body(
                {
                    "tools": [self.tool],
                    "tool_choice": {"type": "function", "name": "missing"},
                }
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("not advertised", raised.exception.detail)


class TestProtocolValueObjects(unittest.TestCase):
    def test_normalizes_usage_to_non_negative_integer_totals(self):
        self.assertEqual(
            normalize_usage(input_tokens="2", output_tokens=-4, total_tokens=None),
            {"input_tokens": 2, "output_tokens": 0, "total_tokens": 2},
        )

    def test_attempt_outcome_rejects_invalid_scope_and_retry_delay(self):
        with self.assertRaises(ValueError):
            AttemptOutcome(scope="provider", category="transport")
        with self.assertRaises(ValueError):
            AttemptOutcome(scope="family", category="rate_limit", retry_after_seconds=-1)


if __name__ == "__main__":
    unittest.main()
