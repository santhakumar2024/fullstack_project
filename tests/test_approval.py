import unittest

from mcp_guard.approval import ApprovalQueue
from mcp_guard.stdio_wrapper import denied_response, is_tool_call


class ApprovalQueueTests(unittest.TestCase):
    def test_concurrent_requests_receive_independent_decisions(self) -> None:
        approvals = ApprovalQueue()
        first = approvals.submit("server-a", "Claude Desktop", "tools/call", {"name": "read"})
        second = approvals.submit("server-b", "Claude Web", "tools/call", {"name": "write"})

        self.assertTrue(approvals.decide(first.id, "allow"))
        self.assertTrue(approvals.decide(second.id, "block"))
        self.assertEqual(approvals.wait(first.id), "allow")
        self.assertEqual(approvals.wait(second.id), "block")
        approvals.finish(first.id)
        approvals.finish(second.id)
        self.assertEqual(approvals.pending, {})

    def test_denied_decision_cannot_match_an_other_request(self) -> None:
        approvals = ApprovalQueue()
        request = approvals.submit("server", "Claude Desktop", "tools/call", {"name": "delete"})
        approvals.decide(request.id, "block")

        self.assertEqual(approvals.wait(request.id), "block")
        self.assertFalse(approvals.decide("missing", "allow"))

    def test_close_window_style_decision_is_blocked(self) -> None:
        approvals = ApprovalQueue()
        request = approvals.submit("server", "Claude Desktop", "tools/call", {})
        approvals.decide(request.id, "block")
        self.assertEqual(approvals.wait(request.id), "block")


class RequestParsingTests(unittest.TestCase):
    def test_tool_call_is_detected_and_name_is_captured(self) -> None:
        approvals = ApprovalQueue()
        request = approvals.submit("server", "Claude Desktop", "tools/call", {"name": "search"})
        self.assertEqual(request.tool, "search")
        self.assertTrue(is_tool_call({"method": "tools/call"}))
        self.assertFalse(is_tool_call({"method": "tools/list"}))

    def test_denial_has_stable_json_rpc_shape(self) -> None:
        response = denied_response("abc")
        self.assertEqual(response["id"], "abc")
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["error"]["code"], -32001)


if __name__ == "__main__":
    unittest.main()
