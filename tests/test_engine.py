"""Behavioral checks for eligibility, review routing, and ranking."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from maia_matcher.engine import evaluate_fund
from maia_matcher.extractor import MetaExtractor
from run_demo import run


AS_OF = date(2026, 11, 18)
RESTRICTION = (
    "Funds may only support engineering scholarships. "
    "Equipment purchases are excluded."
)


def make_fund():
    return {
        "fund_id": "F001",
        "name": "Engineering Scholarship Fund",
        "type": "gift",
        "school": "engineering",
        "purpose_tags": ["scholarship"],
        "reporting_month": "2026-11-01",
        "opening_balance": 50000,
        "inflows": 0,
        "monthly_spending": 0,
        "restriction_text": RESTRICTION,
        "available_balance": 50000,
        "spend_by_date": None,
    }


def make_need(need_id="N001", **overrides):
    need = {
        "need_id": need_id,
        "name": "Engineering undergraduate scholarships",
        "description": "Scholarships for undergraduate engineering students.",
        "school": "engineering",
        "category": "scholarship",
        "purpose_tags": ["scholarship"],
        "amount_needed": 10000,
        "amount_funded": 0,
        "needed_by_date": "2027-02-01",
        "priority": "medium",
        "status": "unfunded",
    }
    need.update(overrides)
    return need


def make_extraction():
    return {
        "fund_id": "F001",
        "all_conditions_captured": True,
        "rules": [
            {
                "field": field,
                "operator": operator,
                "values": values,
                "evidence": RESTRICTION,
            }
            for field, operator, values in [
                ("school", "in", ["engineering"]),
                ("category", "in", ["scholarship"]),
                ("category", "not_in", ["lab_equipment"]),
            ]
        ],
        "unresolved_conditions": [],
        "unrestricted_evidence": None,
    }


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.fund = make_fund()
        self.extraction = make_extraction()

    def evaluate(self, need=None):
        return evaluate_fund(
            self.fund, [need or make_need()], self.extraction, AS_OF
        )["results"][0]

    def assert_review(self, result):
        self.assertEqual(result["status"], "needs human review")
        self.assertIsNone(result["rank"])
        self.assertEqual(result["confidence"], "low")
        self.assertTrue(result["reason"])
        self.assertTrue(result["unresolved_questions"])

    def test_clear_candidate_includes_explanation_and_evidence(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "match")
        self.assertEqual(result["rank"], 1)
        self.assertEqual(result["fund_id"], "F001")
        self.assertEqual(result["need_id"], "N001")
        self.assertEqual(result["confidence"], "medium")
        self.assertTrue(result["reason"])
        self.assertIn(RESTRICTION, result["restriction_evidence"])
        self.assertEqual(result["unresolved_questions"], [])
        self.assertTrue(result["checks"])

    def test_relevance_does_not_override_explicit_restrictions(self):
        for field, value in [
            ("school", "medicine"),
            ("category", "lab_equipment"),
        ]:
            with self.subTest(field=field):
                result = self.evaluate(make_need(**{field: value, "priority": "high"}))
                self.assertEqual(result["status"], "no_match")
                self.assertIsNone(result["rank"])
                self.assertTrue(result["reason"])

    def test_explicit_exclusion_works_without_a_positive_category_rule(self):
        text = "Equipment purchases are excluded."
        self.fund["restriction_text"] = text
        self.extraction["rules"] = [{
            "field": "category",
            "operator": "not_in",
            "values": ["lab_equipment"],
            "evidence": text,
        }]
        self.assertEqual(
            self.evaluate(make_need(category="lab_equipment"))["status"],
            "no_match",
        )

    def test_missing_or_malformed_required_attributes_need_review(self):
        for field in ("school", "category"):
            for value in (None, "", [], 123):
                with self.subTest(field=field, value=value):
                    self.assert_review(self.evaluate(make_need(**{field: value})))

    def test_tags_cannot_override_restriction_text(self):
        self.fund["school"] = "medicine"
        self.fund["purpose_tags"] = ["lab_equipment"]
        self.assertEqual(self.evaluate()["status"], "match")
        result = self.evaluate(make_need(school="medicine", category="lab_equipment"))
        self.assertEqual(result["status"], "no_match")

    def test_ambiguous_fund_is_review_even_for_an_apparent_mismatch(self):
        self.fund["restriction_text"] = "Support deserving engineering students."
        self.extraction["rules"] = []
        self.extraction["unresolved_conditions"] = [{
            "text": self.fund["restriction_text"],
            "question": "How does the donor define deserving students?",
        }]
        self.assert_review(self.evaluate(make_need(school="medicine")))

    def test_incomplete_extraction_cannot_produce_a_positive_match(self):
        self.extraction["all_conditions_captured"] = False
        self.assert_review(self.evaluate())

    def test_invented_evidence_routes_to_review(self):
        self.extraction["rules"][0]["evidence"] = "Any school is eligible."
        self.assert_review(self.evaluate())

    def test_insufficient_balance_requires_partial_funding_decision(self):
        self.assert_review(self.evaluate(make_need(amount_needed=50001)))

    def test_invalid_or_nonfinite_amounts_cannot_be_candidates(self):
        for value in (None, -1, float("nan"), float("inf"), "not money", True):
            with self.subTest(field="amount_needed", value=value):
                self.assert_review(self.evaluate(make_need(amount_needed=value)))
            with self.subTest(field="available_balance", value=value):
                self.fund["available_balance"] = value
                self.assert_review(self.evaluate())
                self.fund = make_fund()
        self.assert_review(self.evaluate(make_need(amount_needed=0)))

    def test_extra_fields_do_not_override_the_contract(self):
        result = self.evaluate(make_need(
            purpose="lab_equipment", amount_requested=999999,
            currency="EUR", beneficiary_level="graduate", planned_spend_date="2020-01-01"
        ))
        self.assertEqual(result["status"], "match")

    def test_legacy_need_shape_cannot_bypass_required_fields(self):
        self.assert_review(self.evaluate({
            "need_id": "N001", "school": "engineering", "purpose": "scholarship",
            "amount_requested": 10000, "priority": 5,
        }))

    def test_full_balance_is_allowed(self):
        result = self.evaluate(make_need(amount_needed=50000))
        self.assertEqual(result["status"], "match")

    def test_decimal_amounts_do_not_use_binary_float_arithmetic(self):
        self.fund.update(opening_balance="10000.10", available_balance="10000.10")
        result = self.evaluate(make_need(amount_needed="10000.10"))
        self.assertEqual(result["status"], "match")
        self.assert_review(self.evaluate(make_need(amount_needed="10000.11")))

    def test_unexpired_fund_deadline_requires_timing_confirmation(self):
        self.fund["spend_by_date"] = "2027-06-30"
        for needed_by in ("2027-01-01", "2027-06-30", "2027-07-01", None):
            with self.subTest(needed_by=needed_by):
                self.assert_review(self.evaluate(make_need(needed_by_date=needed_by)))

    def test_already_expired_fund_is_not_a_match(self):
        self.fund["spend_by_date"] = "2026-11-17"
        self.assertEqual(self.evaluate()["status"], "no_match")

    def test_missing_or_invalid_dates_require_review(self):
        for field in ("spend_by_date", "needed_by_date"):
            for value in ("2027-02-30", "soon", 20270101):
                with self.subTest(field=field, value=value):
                    if field == "spend_by_date":
                        self.fund[field] = value
                        self.assert_review(self.evaluate())
                        self.fund = make_fund()
                    else:
                        self.assert_review(self.evaluate(make_need(**{field: value})))
        del self.fund["spend_by_date"]
        self.assert_review(self.evaluate())
        self.fund = make_fund()
        self.assert_review(self.evaluate(make_need(needed_by_date="2026-11-17")))

    def test_explicitly_no_deadline_allows_an_otherwise_valid_candidate(self):
        for value in (None, "", "none"):
            self.fund["spend_by_date"] = value
            self.assertEqual(self.evaluate()["status"], "match")

    def test_only_matches_are_ranked_and_equal_priority_date_share_rank(self):
        needs = [
            make_need("N005", priority="low", needed_by_date="2026-12-01"),
            make_need("N004", priority="high", needed_by_date="2027-03-01"),
            make_need("N003", priority="high", needed_by_date="2027-02-01"),
            make_need("N002", priority="high", needed_by_date="2027-02-01"),
            make_need("N006", school="medicine", priority="high"),
            make_need("N007", school=None, priority="high"),
        ]
        results = evaluate_fund(self.fund, needs, self.extraction, AS_OF)["results"]
        ranked = [item for item in results if item["rank"] is not None]
        self.assertEqual([item["need_id"] for item in ranked], ["N002", "N003", "N004", "N005"])
        self.assertEqual([item["rank"] for item in ranked], [1, 1, 2, 3])
        self.assertEqual(ranked[0]["ranking_details"], {
            "priority": "high", "needed_by_date": "2027-02-01",
        })
        self.assertEqual(len(results), len(needs))
        for item in results:
            if item["status"] != "match":
                self.assertIsNone(item["rank"])
                self.assertIsNone(item["ranking_details"])

    def test_missing_deadlines_sort_last_within_priority(self):
        needs = [make_need("N002", needed_by_date=None), make_need("N001")]
        results = evaluate_fund(self.fund, needs, self.extraction, AS_OF)["results"]
        self.assertEqual([item["need_id"] for item in results], ["N001", "N002"])

    def test_numeric_or_unknown_priorities_cannot_be_ranked(self):
        for priority in (5, "urgent", "", None):
            with self.subTest(priority=priority):
                self.assert_review(self.evaluate(make_need(priority=priority)))

    def test_candidate_rankings_do_not_allocate_or_mutate_balances(self):
        needs = [make_need("N001", amount_needed=40000),
                 make_need("N002", amount_needed=40000)]
        original = deepcopy((self.fund, needs, self.extraction))
        report = evaluate_fund(self.fund, needs, self.extraction, AS_OF)
        self.assertTrue(all(item["status"] == "match" for item in report["results"]))
        self.assertEqual((self.fund, needs, self.extraction), original)

    def test_supplied_five_by_ten_demo_has_expected_candidate_sets(self):
        """A hand-reviewed interpretation checks all 50 sample comparisons."""
        data = Path(__file__).resolve().parents[1] / "data"
        funds = json.loads((data / "funds.json").read_text())
        needs = json.loads((data / "needs.json").read_text())
        self.assertEqual((len(funds), len(needs)), (5, 10))
        intended_rules = {
            "F001": [("school", "in", ["engineering"]),
                     ("category", "in", ["scholarship"]),
                     ("category", "not_in", ["lab_equipment"])],
            "F002": [("school", "in", ["engineering"]),
                     ("category", "in", ["lab_equipment"]),
                     ("category", "not_in", ["travel"])],
            "F003": [("school", "in", ["arts_and_sciences"]),
                     ("category", "in", ["ta_support"])],
            "F004": [("category", "in", ["scholarship", "ta_support"])],
            "F005": [],
        }
        expected_candidates = {
            "F001": set(),
            "F002": set(),
            "F003": set(),
            "F004": {"N001", "N003", "N004", "N005", "N006", "N010"},
            "F005": set(),
        }
        all_results = []
        extractions = {}
        for fund in funds:
            fund_id = fund["fund_id"]
            extraction = {
                "fund_id": fund_id,
                "all_conditions_captured": True,
                "rules": [{
                    "field": field, "operator": operator, "values": values,
                    "evidence": fund["restriction_text"],
                } for field, operator, values in intended_rules[fund_id]],
                "unresolved_conditions": [{
                    "text": fund["restriction_text"],
                    "question": "Confirm the student eligibility criteria or donor conditions not represented in the schema.",
                }] if fund_id in {"F001", "F003", "F005"} else [],
                "unrestricted_evidence": None,
            }
            extractions[fund_id] = extraction
            results = evaluate_fund(fund, needs, extraction, AS_OF)["results"]
            with self.subTest(fund_id=fund_id):
                self.assertEqual(len(results), 10)
                self.assertEqual(
                    {row["need_id"] for row in results if row["status"] == "match"},
                    expected_candidates[fund_id],
                )
            all_results.extend(results)
        self.assertEqual(len(all_results), 50)
        self.assertEqual(sum(row["status"] == "match" for row in all_results), 6)
        self.assertEqual(sum(row["status"] == "no_match" for row in all_results), 11)
        self.assertEqual(sum(row["status"] == "needs human review" for row in all_results), 33)

        # Exercise the request/response adapter and concurrent report path too.
        # These explicit test responses are not a runtime fallback or live model output.
        def respond(url, **kwargs):
            prompt = kwargs["json"]["input"][0]["content"][0]["text"]
            source = json.loads(prompt.split("Source JSON follows:\n", 1)[1])
            response = Mock(status_code=200)
            response.json.return_value = {
                "status": "completed", "output_text": json.dumps(extractions[source["fund_id"]]),
            }
            return response

        session = Mock()
        session.post.side_effect = respond
        report = run(funds, needs, MetaExtractor(api_key="test-key", session=session), AS_OF)
        self.assertEqual(session.post.call_count, 5)
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(report["summary"]["pairs"], 50)
        self.assertEqual(report["summary"]["match"], 6)
        self.assertEqual(
            [row for group in report["fund_results"] for row in group["results"]], all_results
        )
        self.assertNotIn("test-key", json.dumps(report, allow_nan=False))


if __name__ == "__main__":
    unittest.main()
