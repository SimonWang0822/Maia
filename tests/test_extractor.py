import copy
import json
import unittest
from unittest.mock import patch

from maia_matcher.extractor import (
    API_URL, ExtractionError, MetaExtractor, source_hash, validate_extraction,
)


FUND = {
    "fund_id": "F001",
    "restriction_text": "Engineering scholarships only. Travel is excluded.",
}
RAW = {
    "fund_id": "F001",
    "all_conditions_captured": True,
    "rules": [
        {"field": "school", "operator": "in", "values": ["engineering"], "evidence": "Engineering scholarships only."},
        {"field": "category", "operator": "in", "values": ["scholarship"], "evidence": "Engineering scholarships only."},
        {"field": "category", "operator": "not_in", "values": ["travel"], "evidence": "Travel is excluded."},
    ],
    "unresolved_conditions": [],
    "unrestricted_evidence": None,
}


class FakeResponse:
    def __init__(self, body=None, status=200):
        self.status_code = status
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def response_body(text=None):
    return {"status": "completed", "output": [{
        "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": json.dumps(RAW) if text is None else text}],
    }]}


class ExtractorTests(unittest.TestCase):
    def extract_body(self, body):
        session = FakeSession(FakeResponse(body))
        return MetaExtractor(api_key="test-secret", session=session).extract(FUND)

    def test_user_api_format_and_boundaries(self):
        session = FakeSession(FakeResponse(response_body()))
        result = MetaExtractor(api_key="test-secret", session=session).extract(FUND)
        url, args = session.calls[0]
        self.assertEqual(url, API_URL)
        self.assertEqual(args["headers"]["Authorization"], "Bearer test-secret")
        self.assertEqual(args["json"]["model"], "muse-ember-1.0")
        self.assertFalse(args["json"]["stream"])
        self.assertEqual(args["json"]["input"][0]["role"], "user")
        self.assertEqual(args["json"]["input"][0]["content"][0]["type"], "input_text")
        self.assertIn("untrusted data", args["json"]["input"][0]["content"][0]["text"])
        self.assertEqual(args["timeout"], (5, 45))
        self.assertFalse(args["allow_redirects"])
        self.assertEqual(result["source_sha256"], source_hash(FUND["restriction_text"]))
        self.assertEqual(result["extraction_source"], "meta_api")
        self.assertNotIn("test-secret", json.dumps(result))

    def test_env_key(self):
        session = FakeSession(FakeResponse(response_body()))
        with patch.dict("os.environ", {"MODEL_API_KEY": "env-secret"}):
            MetaExtractor(session=session).extract(FUND)
        self.assertEqual(session.calls[0][1]["headers"]["Authorization"], "Bearer env-secret")

    def test_missing_key_does_not_call_api(self):
        session = FakeSession()
        with self.assertRaisesRegex(ExtractionError, "MODEL_API_KEY"):
            MetaExtractor(api_key="", session=session).extract(FUND)
        self.assertEqual(session.calls, [])

    def test_network_error_redaction_and_no_retry(self):
        session = FakeSession(error=RuntimeError("Bearer test-secret private body"))
        with self.assertRaises(ExtractionError) as caught:
            MetaExtractor(api_key="test-secret", session=session).extract(FUND)
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertNotIn("private body", str(caught.exception))
        self.assertEqual(len(session.calls), 1)

    def test_http_error_redaction(self):
        for status in (302, 401, 429, 500):
            with self.subTest(status=status):
                session = FakeSession(FakeResponse({"error": "test-secret"}, status))
                with self.assertRaisesRegex(ExtractionError, str(status)) as caught:
                    MetaExtractor(api_key="test-secret", session=session).extract(FUND)
                self.assertNotIn("test-secret", str(caught.exception))

    def test_incomplete_and_refusal_rejected(self):
        for body in (
            {"status": "incomplete", "output_text": json.dumps(RAW)},
            {"status": "failed", "output_text": json.dumps(RAW)},
            {"error": {"message": "test-secret"}},
            {"output": [{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "No"}]}], "output_text": json.dumps(RAW)},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ExtractionError) as caught:
                    self.extract_body(body)
                self.assertNotIn("test-secret", str(caught.exception))

    def test_json_fence_and_top_level_text(self):
        self.assertEqual(self.extract_body({"output_text": "```json\n" + json.dumps(RAW) + "\n```"})["fund_id"], "F001")

    def test_bad_json_rejected(self):
        for text in ("Explanation: " + json.dumps(RAW), json.dumps(RAW) + json.dumps(RAW), "[]", '{"fund_id":"F001","fund_id":"F002"}', '{"x":NaN}'):
            with self.subTest(text=text):
                with self.assertRaises(ExtractionError):
                    self.extract_body(response_body(text))

    def test_bad_response_json_redaction(self):
        session = FakeSession(FakeResponse(ValueError("test-secret")))
        with self.assertRaises(ExtractionError) as caught:
            MetaExtractor(api_key="test-secret", session=session).extract(FUND)
        self.assertNotIn("test-secret", str(caught.exception))

    def test_malformed_response_shapes_fail_closed(self):
        bodies = [None, [], {"output": {}}, {"output": [None]},
                  {"output": [{"type": "message", "role": "assistant", "content": {}}]},
                  {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": []}]}]},
                  {"output_text": json.dumps(RAW), "incomplete_details": {"reason": "max_output_tokens"}}]
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(ExtractionError):
                    self.extract_body(body)
        session = FakeSession(response=None)
        with self.assertRaisesRegex(ExtractionError, "HTTP unknown"):
            MetaExtractor(api_key="test-secret", session=session).extract(FUND)

    def test_missing_clause_fails_coverage(self):
        raw = copy.deepcopy(RAW)
        raw["rules"].pop()
        with self.assertRaisesRegex(ExtractionError, "not accounted"):
            validate_extraction(FUND, raw)

    def test_bad_evidence_and_ids(self):
        raw = copy.deepcopy(RAW)
        raw["rules"][0]["evidence"] = "Invented restriction"
        with self.assertRaisesRegex(ExtractionError, "exact"):
            validate_extraction(FUND, raw)
        raw = copy.deepcopy(RAW)
        raw["fund_id"] = "F002"
        with self.assertRaisesRegex(ExtractionError, "ID"):
            validate_extraction(FUND, raw)

    def test_unsupported_and_extra_keys(self):
        for change in ({"field": "approval"}, {"field": "purpose"}, {"field": "beneficiary_level"},
                       {"field": []}, {"operator": {}}, {"operator": "maybe"}, {"values": []}, {"confidence": 0.99}):
            raw = copy.deepcopy(RAW)
            raw["rules"][0].update(change)
            with self.subTest(change=change):
                with self.assertRaises(ExtractionError):
                    validate_extraction(FUND, raw)

    def test_contradiction_rejected(self):
        raw = copy.deepcopy(RAW)
        raw["rules"][2]["values"] = ["scholarship"]
        with self.assertRaisesRegex(ExtractionError, "contradict"):
            validate_extraction(FUND, raw)

    def test_validated_extraction_round_trip(self):
        first = self.extract_body(response_body())
        self.assertEqual(validate_extraction(FUND, first), first)
        self.assertEqual(validate_extraction(FUND, validate_extraction(FUND, RAW)), validate_extraction(FUND, RAW))

    def test_stale_source_hash_rejected(self):
        extraction = validate_extraction(FUND, RAW)
        extraction["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ExtractionError, "hash"):
            validate_extraction(FUND, extraction)

    def test_model_cannot_supply_metadata(self):
        raw = copy.deepcopy(RAW)
        raw["source_sha256"] = source_hash(FUND["restriction_text"])
        with self.assertRaisesRegex(ExtractionError, "fields"):
            self.extract_body(response_body(json.dumps(raw)))

    def test_ambiguity_preserved(self):
        fund = {"fund_id": "F002", "restriction_text": "Support deserving students."}
        raw = {"fund_id": "F002", "all_conditions_captured": True, "rules": [], "unresolved_conditions": [{"text": fund["restriction_text"], "question": "What criteria define deserving?"}], "unrestricted_evidence": None}
        result = validate_extraction(fund, raw)
        self.assertEqual(result["unresolved_conditions"], raw["unresolved_conditions"])
        self.assertFalse(result["rules"])

    def test_only_contract_marker_can_claim_unrestricted(self):
        for text in ("No restrictions on school, purpose, or beneficiary level.", "Unrestricted."):
            fund = {"fund_id": "F003", "restriction_text": text}
            raw = {"fund_id": "F003", "all_conditions_captured": True, "rules": [],
                   "unresolved_conditions": [], "unrestricted_evidence": text}
            with self.subTest(text=text), self.assertRaisesRegex(ExtractionError, "unrestricted"):
                validate_extraction(fund, raw)

    def test_student_qualification_is_preserved_for_review(self):
        text = "Engineering scholarships for undergraduate students only."
        fund = {"fund_id": "F004", "restriction_text": text}
        raw = {
            "fund_id": "F004", "all_conditions_captured": True,
            "rules": [
                {"field": "school", "operator": "in", "values": ["engineering"], "evidence": text},
                {"field": "category", "operator": "in", "values": ["scholarship"], "evidence": text},
            ],
            "unresolved_conditions": [{"text": text, "question": "Are the recipients undergraduate students?"}],
            "unrestricted_evidence": None,
        }
        session = FakeSession(FakeResponse(response_body(json.dumps(raw))))
        result = MetaExtractor(api_key="test-key", session=session).extract(fund)
        self.assertEqual(result["unresolved_conditions"], raw["unresolved_conditions"])
        prompt = session.calls[0][1]["json"]["input"][0]["content"][0]["text"]
        self.assertIn("Supported fields: school, category.", prompt)
        self.assertIn("never drop that qualifier", prompt)
        self.assertNotIn("as arts_sciences", prompt)

    def test_school_name_is_not_replaced_with_an_alias(self):
        text = "Arts and Sciences scholarships only."
        fund = {"fund_id": "F005", "restriction_text": text}
        raw = {
            "fund_id": "F005", "all_conditions_captured": True,
            "rules": [{"field": "school", "operator": "in", "values": ["arts_and_sciences"], "evidence": text}],
            "unresolved_conditions": [], "unrestricted_evidence": None,
        }
        self.assertEqual(validate_extraction(fund, raw)["rules"][0]["values"], ["arts_and_sciences"])

    def test_none_marker_is_explicit_and_requires_no_api_call(self):
        for text in ("None", "none", " NONE ", "\tNoNe\n"):
            with self.subTest(text=text):
                fund = {"fund_id": "F003", "restriction_text": text}
                session = FakeSession(error=RuntimeError("Must not call the API"))
                result = MetaExtractor(api_key="", session=session).extract(fund)
                self.assertEqual(session.calls, [])
                self.assertEqual(result["unrestricted_evidence"], text)
                self.assertEqual(result["source_sha256"], source_hash(text))
                self.assertEqual(result["extraction_source"], "data_contract")
                self.assertNotIn("model", result)
                self.assertEqual(result["rules"], [])
                self.assertEqual(result["unresolved_conditions"], [])
                self.assertTrue(result["all_conditions_captured"])
                self.assertEqual(validate_extraction(fund, result), result)

    def test_blank_null_or_missing_restriction_is_not_unrestricted(self):
        funds = [{"fund_id": "F003", "restriction_text": value} for value in ("", "  ", None)]
        funds.append({"fund_id": "F003"})
        for fund in funds:
            with self.subTest(fund=fund):
                session = FakeSession()
                with self.assertRaisesRegex(ExtractionError, "restriction text"):
                    MetaExtractor(api_key="test-key", session=session).extract(fund)
                self.assertEqual(session.calls, [])

    def test_none_with_qualifier_must_use_api_and_cannot_be_unrestricted(self):
        text = "None except travel is excluded."
        fund = {"fund_id": "F003", "restriction_text": text}
        raw = {
            "fund_id": "F003", "all_conditions_captured": True,
            "rules": [{"field": "category", "operator": "not_in", "values": ["travel"], "evidence": text}],
            "unresolved_conditions": [], "unrestricted_evidence": None,
        }
        session = FakeSession(FakeResponse(response_body(json.dumps(raw))))
        result = MetaExtractor(api_key="test-key", session=session).extract(fund)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(result["extraction_source"], "meta_api")
        self.assertIsNone(result["unrestricted_evidence"])
        raw["rules"] = []
        raw["unrestricted_evidence"] = text
        with self.assertRaisesRegex(ExtractionError, "unrestricted"):
            validate_extraction(fund, raw)

    def test_none_marker_cannot_combine_with_rules_or_unresolved_conditions(self):
        fund = {"fund_id": "F003", "restriction_text": "None"}
        raw = MetaExtractor(api_key="").extract(fund)
        raw["rules"] = [{"field": "school", "operator": "in", "values": ["engineering"], "evidence": "None"}]
        with self.assertRaisesRegex(ExtractionError, "conflicting"):
            validate_extraction(fund, raw)
        raw["rules"] = []
        raw["unresolved_conditions"] = [{"text": "None", "question": "Which school?"}]
        with self.assertRaisesRegex(ExtractionError, "conflicting"):
            validate_extraction(fund, raw)


if __name__ == "__main__":
    unittest.main()
