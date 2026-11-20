"""The fund/need schema through the extractor and engine boundary."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest

from maia_matcher.data import load_records
from maia_matcher.engine import evaluate_fund
from maia_matcher.extractor import MetaExtractor


AS_OF = date(2026, 11, 19)


class SchemaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fund = {
            "fund_id": "FUND-001", "name": "Test gift", "type": "gift",
            "restriction_text": "None", "school": "Engineering",
            "purpose_tags": "scholarship;undergraduate_support",
            "reporting_month": "2026-11-01", "opening_balance": 5000,
            "inflows": 1000, "monthly_spending": 2000, "available_balance": 4000,
            "spend_by_date": "",
        }
        self.need = {
            "need_id": "NEED-001", "name": "Test need", "description": "Equipment",
            "category": "lab_equipment", "school": "Medicine", "purpose_tags": "lab_equipment",
            "amount_needed": 5000, "amount_funded": 2000,
            "needed_by_date": "2027-02-01", "priority": "high", "status": "partially_funded",
        }

    def evaluate(self, needs=None):
        extraction = MetaExtractor(api_key="").extract(self.fund)
        return evaluate_fund(self.fund, needs or [self.need], extraction, AS_OF)

    def test_none_means_unrestricted_even_when_ownership_tags_differ(self):
        result = self.evaluate()["results"][0]
        self.assertEqual(result["status"], "match")
        self.assertEqual(result["restriction_evidence"], ["None"])
        self.assertIn("3,000.00 USD remaining", result["reason"])

    def test_funded_and_inconsistent_statuses_are_not_recommended(self):
        self.need.update(amount_funded=5000, status="funded")
        self.assertEqual(self.evaluate()["results"][0]["status"], "no_match")
        self.need.update(amount_funded=2000, status="funded")
        result = self.evaluate()["results"][0]
        self.assertEqual(result["status"], "needs human review")
        self.assertTrue(any("status disagrees" in question for question in result["unresolved_questions"]))

    def test_inconsistent_monthly_balance_cannot_be_a_match(self):
        self.fund["available_balance"] = 4001
        result = self.evaluate()["results"][0]
        self.assertEqual(result["status"], "needs human review")
        self.assertTrue(any("available_balance must equal" in item for item in result["unresolved_questions"]))

    def test_needed_by_does_not_establish_spending_date(self):
        self.fund["spend_by_date"] = "2026-12-31"
        result = self.evaluate()["results"][0]
        self.assertEqual(result["status"], "needs human review")
        self.assertTrue(result["unresolved_questions"])
        # A need deadline on either side of fund expiry does not establish spending timing.
        self.need["needed_by_date"] = "2026-12-01"
        self.assertEqual(self.evaluate()["results"][0]["status"], "needs human review")

    def test_past_need_deadline_requires_confirmation(self):
        self.need["needed_by_date"] = "2026-11-18"
        self.assertEqual(self.evaluate()["results"][0]["status"], "needs human review")

    def test_text_priority_then_need_deadline_controls_rank(self):
        needs = [
            {**self.need, "need_id": "LOW", "priority": "low", "needed_by_date": "2026-12-01"},
            {**self.need, "need_id": "HIGH-LATE", "priority": "high", "needed_by_date": "2027-02-01"},
            {**self.need, "need_id": "HIGH-EARLY-B", "priority": "high", "needed_by_date": "2027-01-01"},
            {**self.need, "need_id": "HIGH-EARLY-A", "priority": "high", "needed_by_date": "2027-01-01"},
            {**self.need, "need_id": "MEDIUM", "priority": "medium"},
        ]
        results = self.evaluate(needs)["results"]
        self.assertEqual([row["need_id"] for row in results],
                         ["HIGH-EARLY-A", "HIGH-EARLY-B", "HIGH-LATE", "MEDIUM", "LOW"])
        self.assertEqual([row["rank"] for row in results], [1, 1, 2, 3, 4])
        self.assertEqual(results[0]["ranking_details"], results[1]["ranking_details"])

    def test_supplied_csv_and_json_produce_same_decisions(self):
        data = Path(__file__).resolve().parents[1] / "data"
        reports = []
        for suffix in ("json", "csv"):
            funds = load_records(data / f"funds.{suffix}", "fund_id")
            needs = load_records(data / f"needs.{suffix}", "need_id")
            # Exercise identical recorded data against an explicit no-restrictions fund.
            fund = deepcopy(funds[3])
            fund["restriction_text"] = "None"
            extraction = MetaExtractor(api_key="").extract(fund)
            reports.append(evaluate_fund(fund, needs, extraction, AS_OF))
        self.assertEqual(reports[0], reports[1])


if __name__ == "__main__":
    unittest.main()
