"""Validate the fund and need schema without inferring missing facts."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from maia_matcher.data import canonical_tag, load_records, normalize_fund, normalize_need


def fund(**changes):
    value = {
        "fund_id": "FUND-001", "name": "Example", "type": "gift", "restriction_text": "None",
        "school": "Arts and Sciences", "purpose_tags": "scholarship; undergraduate_support",
        "reporting_month": "2026-11-01", "opening_balance": 10000,
        "inflows": 2000, "monthly_spending": 1000, "available_balance": 11000,
        "spend_by_date": "",
    }
    return {**value, **changes}


def need(**changes):
    value = {
        "need_id": "NEED-001", "name": "TA need", "description": "A teaching assistant",
        "category": "TA_support", "school": "Arts and Sciences", "purpose_tags": "TA_support;graduate_support",
        "amount_needed": 1000, "amount_funded": 250, "needed_by_date": "2027-06-01",
        "priority": "high", "status": "partially_funded",
    }
    return {**value, **changes}


class DataTests(unittest.TestCase):
    def test_normalization_preserves_source_and_uses_schema_fields(self):
        raw_fund, raw_need = fund(), need()
        originals = deepcopy((raw_fund, raw_need))
        normalized_fund, normalized_need = normalize_fund(raw_fund), normalize_need(raw_need)
        self.assertEqual((raw_fund, raw_need), originals)
        self.assertEqual(normalized_fund["restriction_text"], "None")
        self.assertEqual(normalized_fund["school"], "arts_and_sciences")
        self.assertEqual(normalized_fund["purpose_tags"], ["scholarship", "undergraduate_support"])
        self.assertIsNone(normalized_fund["spend_by_date"])
        self.assertEqual(normalized_need["category"], "ta_support")
        self.assertEqual(normalized_need["remaining_amount"], Decimal("750"))
        self.assertEqual(normalized_need["amount_needed"], 1000)
        self.assertEqual(normalized_need["amount_funded"], 250)
        self.assertEqual(normalized_need["priority"], "high")
        self.assertEqual(normalized_need["needed_by_date"], "2027-06-01")
        self.assertEqual(normalized_fund["_data_issues"], [])
        self.assertEqual(normalized_need["_data_issues"], [])

    def test_accounting_inconsistency_is_not_silently_corrected(self):
        result = normalize_fund(fund(available_balance=123))
        self.assertEqual(result["available_balance"], 123)
        self.assertTrue(result["_data_issues"])

    def test_money_arithmetic_is_decimal_and_comparison_is_to_cents(self):
        result = normalize_fund(fund(opening_balance="0.10", inflows="0.20", monthly_spending="0", available_balance=0.30000000000000004))
        self.assertEqual(result["_data_issues"], [])
        result = normalize_need(need(amount_needed="0.30", amount_funded="0.10"))
        self.assertEqual(result["remaining_amount"], Decimal("0.20"))
        self.assertTrue(normalize_need(need(amount_needed="1000.001"))["_data_issues"])

    def test_bad_financial_and_status_values_require_review(self):
        for changes in (
            {"amount_funded": None}, {"amount_funded": -1}, {"amount_funded": 2000},
            {"amount_needed": float("nan")}, {"amount_needed": True}, {"amount_needed": 0},
            {"status": "unfunded"}, {"status": "funded"}, {"status": "unknown"},
            {"amount_funded": 0}, {"amount_funded": 1000},
        ):
            with self.subTest(changes=changes):
                result = normalize_need(need(**changes))
                self.assertTrue(result["_data_issues"])
                self.assertFalse(result["_fully_funded"])

    def test_explicitly_funded_need_has_zero_remainder(self):
        result = normalize_need(need(amount_funded=1000, status="funded"))
        self.assertTrue(result["_fully_funded"])
        self.assertEqual(result["remaining_amount"], Decimal("0"))
        self.assertEqual(result["_data_issues"], [])

    def test_unknown_columns_do_not_supply_matcher_facts(self):
        extra = {
            "currency": "EUR", "beneficiary_level": "graduate", "planned_spend_date": "2027-01-01",
            "purpose": "scholarship", "amount_requested": 1, "_fully_funded": True,
        }
        normalized = normalize_need(need(**extra))
        self.assertEqual(normalized["category"], "ta_support")
        self.assertEqual(normalized["remaining_amount"], Decimal("750"))
        self.assertFalse(normalized["_fully_funded"])
        for key in set(extra) - {"_fully_funded"}:
            self.assertNotIn(key, normalized)
        self.assertNotIn("currency", normalize_fund(fund(currency="EUR")))

    def test_all_contract_keys_are_required_even_when_nullable(self):
        for source, normalize in ((fund(), normalize_fund), (need(), normalize_need)):
            for key in source:
                with self.subTest(key=key, kind=normalize.__name__):
                    missing = dict(source)
                    del missing[key]
                    self.assertTrue(normalize(missing)["_data_issues"])
        for value in (None, "", " none "):
            self.assertIsNone(normalize_fund(fund(spend_by_date=value))["spend_by_date"])
            self.assertIsNone(normalize_need(need(needed_by_date=value))["needed_by_date"])
            self.assertEqual(normalize_fund(fund(spend_by_date=value))["_data_issues"], [])
            self.assertEqual(normalize_need(need(needed_by_date=value))["_data_issues"], [])

    def test_invalid_fund_fields_are_flagged(self):
        for field, value in (("reporting_month", None), ("reporting_month", "2026-02-30"),
                             ("opening_balance", None), ("inflows", "NaN"), ("monthly_spending", -1),
                             ("type", "loan"), ("name", ""), ("school", "unknown"),
                             ("purpose_tags", [123])):
            with self.subTest(field=field, value=value):
                self.assertTrue(normalize_fund(fund(**{field: value}))["_data_issues"])

    def test_priorities_remain_text_and_invalid_values_require_review(self):
        for source, expected in (("low", "low"), (" MEDIUM ", "medium"), ("high", "high")):
            result = normalize_need(need(priority=source))
            self.assertEqual(result["priority"], expected)
            self.assertEqual(result["_data_issues"], [])
        for source in ("urgent", None, "", 1, 3, 5, True):
            with self.subTest(source=source):
                self.assertTrue(normalize_need(need(priority=source))["_data_issues"])

    def test_tags_and_dates_do_not_infer_student_level_or_spending(self):
        result = normalize_need(need(needed_by_date="none"))
        self.assertIsNone(result["needed_by_date"])
        self.assertNotIn("planned_spend_date", result)
        self.assertNotIn("beneficiary_level", result)
        self.assertTrue(normalize_need(need(needed_by_date="06/01/2027"))["_data_issues"])
        self.assertEqual(canonical_tag("TA_support"), "ta_support")
        self.assertEqual(canonical_tag("Arts and Sciences"), "arts_and_sciences")
        self.assertNotEqual(canonical_tag("Arts and Sciences"), canonical_tag("arts_sciences"))

    def test_legacy_input_cannot_bypass_the_required_schema(self):
        old_need = {"need_id": "N01", "purpose": "scholarship", "amount_requested": 12,
                    "school": "engineering", "priority": 4}
        normalized = normalize_need(old_need)
        self.assertTrue(normalized["_data_issues"])
        self.assertIsNone(normalized["remaining_amount"])
        self.assertNotIn("purpose", normalized)
        self.assertNotIn("amount_requested", normalized)
        old_fund = {"fund_id": "F01", "restriction_text": "None", "available_balance": 100}
        self.assertTrue(normalize_fund(old_fund)["_data_issues"])

    def test_normalization_is_idempotent_and_rechecks_current_values(self):
        self.assertEqual(normalize_need(need()), normalize_need(normalize_need(need())))
        self.assertEqual(normalize_fund(fund()), normalize_fund(normalize_fund(fund())))
        corrected = normalize_fund(fund(available_balance=0))
        corrected["available_balance"] = 11000
        self.assertEqual(normalize_fund(corrected)["_data_issues"], [])
        corrected = normalize_need(need(status="funded"))
        corrected["status"] = "partially_funded"
        self.assertEqual(normalize_need(corrected)["_data_issues"], [])

    def test_invalid_record_cannot_keep_a_fully_funded_flag(self):
        damaged = normalize_need(need(amount_funded=1000, status="funded", priority="urgent"))
        self.assertTrue(damaged["_data_issues"])
        self.assertFalse(damaged["_fully_funded"])

    def test_csv_and_json_preserve_raw_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "needs.csv"
            csv_path.write_text("\ufeffneed_id,purpose_tags,amount_funded\nN01,scholarship;undergraduate_support,0\n")
            self.assertEqual(load_records(csv_path, "need_id"), [{
                "need_id": "N01", "purpose_tags": "scholarship;undergraduate_support", "amount_funded": "0"
            }])
            json_path = root / "funds.json"
            json_path.write_text(json.dumps([fund()]))
            self.assertEqual(load_records(json_path, "fund_id"), [fund()])

    def test_bad_csv_shapes_and_duplicate_ids_are_rejected(self):
        bad_files = (
            "need_id,need_id\nN01,N02\n", "need_id,name\nN01\n",
            "need_id,name\nN01,thing,extra\n", "need_id,name\nN01,thing\nN01,other\n",
            "need_id,name\n,thing\n", 'need_id,name\nN01,"unterminated\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "needs.csv"
            for text in bad_files:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        load_records(path, "need_id")


if __name__ == "__main__":
    unittest.main()
