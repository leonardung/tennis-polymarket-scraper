import unittest

from polymarket.evidence_chain import capture_payout_slots

CONDITION = "0x" + "12" * 32
BLOCK = {"number": "0x123", "hash": "0x" + "ab" * 32}


class ChainEvidenceTests(unittest.TestCase):
    def run_capture(self, replies):
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            return replies.pop(0)

        result = capture_payout_slots(rpc, CONDITION)
        return result, calls

    def test_resolved_vector_pins_every_call_and_does_not_map_tokens(self):
        result, calls = self.run_capture(
            [
                {"result": "0x89"},
                {"result": BLOCK},
                {"result": "0x" + f"{1:064x}"},
                {"result": "0x" + f"{0:064x}"},
                {"result": "0x" + f"{1:064x}"},
            ]
        )
        self.assertEqual(result["numerators"], [0, 1])
        self.assertEqual(result["denominator"], 1)
        self.assertEqual(result["block_hash"], BLOCK["hash"])
        self.assertEqual(result["token_mapping"], "unverified")
        self.assertTrue(
            all(params[1] == BLOCK["number"] for method, params in calls if method == "eth_call")
        )
        self.assertEqual(calls[1], ("eth_getBlockByNumber", ["finalized", False]))

    def test_wrong_chain_stops_before_contract_reads(self):
        result, calls = self.run_capture([{"result": "0x1"}])
        self.assertEqual(result["status"], "wrong_chain")
        self.assertEqual(len(calls), 1)

    def test_split_resolution_preserves_exact_numerators(self):
        result, _ = self.run_capture([
            {"result": "0x89"}, {"result": BLOCK},
            {"result": "0x" + f"{2:064x}"},
            {"result": "0x" + f"{1:064x}"},
            {"result": "0x" + f"{1:064x}"},
        ])
        self.assertEqual(result["status"], "resolved_slots")
        self.assertEqual(result["denominator"], 2)
        self.assertEqual(result["numerators"], [1, 1])

    def test_unresolved_stops_without_inventing_payout(self):
        result, calls = self.run_capture(
            [
                {"result": "0x89"},
                {"result": BLOCK},
                {"result": "0x" + "0" * 64},
            ]
        )
        self.assertEqual(result["status"], "unresolved")
        self.assertNotIn("numerators", result)
        self.assertEqual(len(calls), 3)

    def test_errors_and_malformed_headers_are_not_evidence(self):
        for reply in (None, {"error": {"code": -1}}, {"result": {"number": "latest"}}):
            result, _ = self.run_capture([{"result": "0x89"}, reply])
            self.assertEqual(result["status"], "invalid_block")

    def test_short_contract_word_is_refused(self):
        result, _ = self.run_capture([{"result": "0x89"}, {"result": BLOCK}, {"result": "0x1"}])
        self.assertEqual(result["status"], "invalid_denominator")

    def test_inconsistent_binary_vector_is_refused(self):
        result, _ = self.run_capture(
            [
                {"result": "0x89"},
                {"result": BLOCK},
                {"result": "0x" + f"{1:064x}"},
                {"result": "0x" + f"{1:064x}"},
                {"result": "0x" + f"{1:064x}"},
            ]
        )
        self.assertEqual(result["status"], "invalid_vector")

    def test_invalid_condition_never_requests(self):
        def unexpected(*args):
            self.fail("invalid condition reached RPC")

        self.assertEqual(capture_payout_slots(unexpected, "invalid")["status"], "invalid_condition")


if __name__ == "__main__":
    unittest.main()
