"""Regression coverage for data and extraction issues found in code review."""

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from maia_matcher.data import load_records, normalize_need
from maia_matcher.engine import evaluate_fund
from maia_matcher.extractor import ExtractionError, MetaExtractor, validate_extraction
from run_demo import run


AS_OF = date(2026, 11, 19)


def fund(**changes):
    return {
        "fund_id": "F1", "name": "Test gift", "type": "gift",
        "restriction_text": "None", "school": "Engineering",
        "purpose_tags": ["scholarship"], "reporting_month": "2026-11-01",
        "opening_balance": 10000, "inflows": 2000, "monthly_spending": 1000,
        "available_balance": 11000, "spend_by_date": None, **changes,
    }


def need(**changes):
    return {
        "need_id": "N1", "name": "Test scholarship", "description": "Scholarship",
        "school": "Engineering", "category": "scholarship",
        "purpose_tags": ["scholarship"], "amount_needed": 1000,
        "amount_funded": 0, "needed_by_date": "2027-06-01", "priority": "high",
        "status": "unfunded", **changes,
    }


def extraction(source, *rules):
    return {
        "fund_id": source["fund_id"], "all_conditions_captured": True,
        "rules": [{"field": field, "operator": operator, "values": values,
                   "evidence": source["restriction_text"]}
                  for field, operator, values in rules],
        "unresolved_conditions": [], "unrestricted_evidence": None,
    }


def evaluate(source=None, request=None, rules=None):
    source = fund() if source is None else source
    request = need() if request is None else request
    if rules is None:
        rules = MetaExtractor(api_key="").extract(source)
    return evaluate_fund(source, [request], rules, AS_OF)["results"][0]


class ReviewRegressionTests(unittest.TestCase):
    def test_duplicate_restriction_keys_cannot_replace_restricted_with_unrestricted(self):
        text = ('[{"fund_id":"F1",'
                '"restriction_text":"Engineering scholarships only.",'
                '"restriction_text":"None"}]')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "funds.json"
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                load_records(path, "fund_id")

    def test_nonfinite_input_json_is_rejected_before_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "needs.json"
            for value in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(value=value):
                    path.write_text('[{"need_id":"N1","amount_needed":' + value + '}]')
                    with self.assertRaisesRegex(ValueError, "Nonfinite"):
                        load_records(path, "need_id")

    def test_missing_value_markers_cannot_satisfy_an_exclusion(self):
        source = fund(restriction_text="Lab equipment is excluded.")
        rules = extraction(source, ("category", "not_in", ["lab_equipment"]))
        for marker in ("unknown", "None", "null", "N/A", "TBD"):
            with self.subTest(marker=marker):
                result = evaluate(source, need(category=marker), rules)
                self.assertEqual(result["status"], "needs human review")
                self.assertIsNone(result["rank"])
                self.assertTrue(result["unresolved_questions"])

    def test_float_artifacts_do_not_leave_a_fully_funded_need_recommendable(self):
        for needed, funded in ((0.3, 0.1 + 0.2), (0.1 + 0.2, 0.3)):
            with self.subTest(needed=needed, funded=funded):
                request = need(amount_needed=needed, amount_funded=funded, status="funded")
                self.assertEqual(normalize_need(request)["_data_issues"], [])
                result = evaluate(request=request)
                self.assertEqual(result["status"], "no_match")
                self.assertIn("fully funded", result["reason"])

    def test_genuine_fractional_cents_require_review(self):
        for amount in (0.001, "0.001"):
            with self.subTest(amount=amount):
                result = evaluate(request=need(amount_needed=amount))
                self.assertEqual(result["status"], "needs human review")
                self.assertIsNone(result["rank"])

    def test_correcting_a_normalized_record_clears_stale_data_issues(self):
        normalized = normalize_need(need(amount_funded=100, status="unfunded"))
        self.assertTrue(normalized["_data_issues"])
        normalized["amount_funded"] = 0
        corrected = normalize_need(normalized)
        self.assertEqual(corrected["_data_issues"], [])
        self.assertEqual(evaluate(request=corrected)["status"], "match")

    def test_future_balance_snapshot_cannot_support_a_current_match(self):
        result = evaluate(source=fund(reporting_month="2026-12-01"))
        self.assertEqual(result["status"], "needs human review")
        self.assertIn("future reporting month", result["reason"])

    def test_contradictory_school_rules_cannot_pass_validation(self):
        source = fund(restriction_text=(
            "Arts and sciences scholarships only. Arts and sciences are excluded."
        ))
        rules = extraction(source,
            ("school", "in", ["arts_and_sciences"]),
            ("school", "not_in", ["arts_and_sciences"]),
        )
        with self.assertRaisesRegex(ExtractionError, "contradict"):
            validate_extraction(source, rules)

    def test_full_sentence_evidence_does_not_hide_omitted_donor_approval(self):
        source = fund(restriction_text="Scholarships only, subject to donor approval.")
        rules = extraction(source, ("category", "in", ["scholarship"]))
        result = evaluate(source, need(), rules)
        self.assertEqual(result["status"], "needs human review")
        self.assertIsNone(result["rank"])
        self.assertTrue(result["unresolved_questions"])

    def test_invalid_unicode_source_raises_a_controlled_extraction_error(self):
        source = fund(restriction_text="Scholarships only.\ud800")
        rules = extraction(source, ("category", "in", ["scholarship"]))
        with self.assertRaisesRegex(ExtractionError, "Unicode"):
            validate_extraction(source, rules)

    def test_empty_api_error_object_cannot_be_ignored_because_text_is_present(self):
        source = fund(restriction_text="Scholarships only.")
        rules = extraction(source, ("category", "in", ["scholarship"]))
        response = Mock(status_code=200)
        response.json.return_value = {"error": {}, "output_text": json.dumps(rules)}
        session = Mock()
        session.post.return_value = response
        with self.assertRaisesRegex(ExtractionError, "error"):
            MetaExtractor(api_key="test-key", session=session).extract(source)
        session.post.assert_called_once()

    def test_direct_pipeline_rejects_empty_or_duplicate_inputs_before_extraction(self):
        cases = (
            ([], [need()]),
            ([fund()], []),
            ([fund(), fund()], [need()]),
            ([fund()], [need(), need()]),
        )
        for funds, needs in cases:
            with self.subTest(funds=len(funds), needs=len(needs)):
                extractor = Mock(model="test-model")
                with self.assertRaises(ValueError):
                    run(funds, needs, extractor, AS_OF)
                extractor.extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
