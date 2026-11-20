"""Exercise the actual API adapter through the report pipeline without network."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from maia_matcher.extractor import MetaExtractor
from run_demo import load_records, main, run, write_report


class TestResponse:
    status_code = 200

    def __init__(self, raw):
        self.raw = raw

    def json(self):
        return {"status": "completed", "output": [{
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps(self.raw)}],
        }]}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.fund = {
            "fund_id": "F01", "name": "Scholarship fund", "type": "gift",
            "restriction_text": "Scholarships only.", "school": "Engineering",
            "purpose_tags": ["scholarship"], "reporting_month": "2026-11-01",
            "opening_balance": 10000, "inflows": 0, "monthly_spending": 0,
            "available_balance": 10000, "spend_by_date": None,
        }
        self.need = {
            "need_id": "N01", "name": "Tuition awards", "description": "Student scholarships",
            "school": "Engineering", "category": "scholarship", "purpose_tags": ["scholarship"],
            "amount_needed": 5000, "amount_funded": 0, "needed_by_date": "2027-01-15",
            "priority": "medium", "status": "unfunded",
        }
        self.raw = {
            "fund_id": "F01", "all_conditions_captured": True,
            "rules": [{"field": "category", "operator": "in", "values": ["scholarship"],
                       "evidence": "Scholarships only."}],
            "unresolved_conditions": [], "unrestricted_evidence": None,
        }

    def test_api_response_survives_validation_and_report_round_trip(self):
        session = Mock()
        session.post.return_value = TestResponse(self.raw)
        extractor = MetaExtractor(api_key="test-key", session=session)
        report = run([self.fund], [self.need], extractor, date(2026, 11, 18))
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(report["schema_version"], "2.0")
        self.assertEqual(report["currency"], "USD")
        self.assertNotIn("assumptions", report)
        self.assertEqual(report["summary"]["match"], 1)
        group = report["fund_results"][0]
        self.assertEqual(group["extraction"]["model"], "muse-wonder-1.0")
        self.assertEqual(group["results"][0]["rank"], 1)
        self.assertEqual(group["results"][0]["ranking_details"], {
            "priority": "medium", "needed_by_date": "2027-01-15",
        })
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested/results.json"
            write_report(output, report)
            self.assertEqual(json.loads(output.read_text()), report)
            self.assertNotIn("test-key", output.read_text())

    def test_api_failure_is_visible_and_preserves_every_pair(self):
        session = Mock()
        session.post.side_effect = RuntimeError("secret-test-key")
        extractor = MetaExtractor(api_key="secret-test-key", session=session)
        needs = [self.need, {**self.need, "need_id": "N02"}]
        report = run([self.fund], needs, extractor, date(2026, 11, 18))
        self.assertEqual(report["run_status"], "partial_failure")
        self.assertEqual(report["summary"]["needs human review"], 2)
        self.assertEqual(report["summary"]["extraction_failures"], 1)
        self.assertNotIn("secret-test-key", json.dumps(report))

    def test_missing_key_exits_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"MODEL_API_KEY": ""}):
            output = Path(directory) / "results.json"
            with redirect_stderr(io.StringIO()) as stderr:
                code = main(["--funds", "unused-funds.json", "--needs", "unused-needs.json",
                             "--output", str(output)])
            self.assertEqual(code, 1)
            self.assertIn("MODEL_API_KEY", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_reports_partial_failure_with_nonzero_exit(self):
        session = Mock()
        session.post.side_effect = RuntimeError("connection failed")
        extractor = MetaExtractor(api_key="test-key", session=session)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "funds.json").write_text(json.dumps([self.fund]))
            (root / "needs.json").write_text(json.dumps([self.need]))
            with patch.dict("os.environ", {"MODEL_API_KEY": "test-key"}), \
                 patch("run_demo.MetaExtractor", return_value=extractor), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(["--funds", str(root / "funds.json"), "--needs", str(root / "needs.json"),
                             "--output", str(root / "results.json")])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads((root / "results.json").read_text())["run_status"], "partial_failure")

    def test_duplicate_input_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "funds.json"
            path.write_text(json.dumps([self.fund, self.fund]))
            with self.assertRaisesRegex(ValueError, "unique"):
                load_records(path, "fund_id")


if __name__ == "__main__":
    unittest.main()
