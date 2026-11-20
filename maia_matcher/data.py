"""Read and validate the fund and need schema."""

import csv
from datetime import date
from decimal import Decimal, DecimalException, ROUND_HALF_UP
import json
from pathlib import Path
import re


PRIORITIES = {"low": 0, "medium": 1, "high": 2}
FUND_FIELDS = (
    "fund_id", "name", "type", "restriction_text", "school", "purpose_tags",
    "reporting_month", "opening_balance", "inflows", "monthly_spending",
    "available_balance", "spend_by_date",
)
NEED_FIELDS = (
    "need_id", "name", "description", "category", "school", "purpose_tags",
    "amount_needed", "amount_funded", "needed_by_date", "priority", "status",
)
MISSING_TAGS = {"none", "null", "unknown", "n/a", "tbd"}
CENT = Decimal("0.01")
FLOAT_TOLERANCE = Decimal("0.000000001")


def canonical_tag(value):
    if not isinstance(value, str) or not value.strip():
        return None
    tag = re.sub(r"[\s-]+", "_", value.strip().lower())
    if tag in MISSING_TAGS:
        return None
    return tag


def parse_json(text):
    """Reject duplicate keys and nonfinite numbers instead of silently replacing data."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key.")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Nonfinite JSON number.")

    try:
        return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant,
                          parse_float=Decimal)
    except RecursionError:
        raise ValueError("JSON nesting is too deep.") from None


def validate_records(records, id_field, label="Input"):
    """Require one nonempty object list with unique, nonempty IDs."""
    if not isinstance(records, list) or not records:
        raise ValueError(f"{label} must contain a nonempty list of records.")
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"{label} contains a record that is not an object.")
        identifier = record.get(id_field)
        if not isinstance(identifier, str) or not identifier.strip() or identifier.strip() in seen:
            raise ValueError(f"{label} requires unique, nonempty {id_field} values.")
        seen.add(identifier.strip())


def load_records(path, id_field):
    """Read records without interpreting monetary/date blanks as safe defaults."""
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        if path.suffix.lower() == ".csv":
            try:
                reader = csv.DictReader(handle, strict=True)
                headers = reader.fieldnames
                if not headers or any(not header.strip() for header in headers) or len(set(headers)) != len(headers):
                    raise ValueError(f"{path.name} requires unique, nonempty CSV headers.")
                records = list(reader)
                if any(None in row or any(value is None for value in row.values()) for row in records):
                    raise ValueError(f"{path.name} contains a CSV row with the wrong number of columns.")
            except csv.Error as exc:
                raise ValueError(f"{path.name} contains invalid CSV: {exc}") from None
        elif path.suffix.lower() == ".json":
            records = parse_json(handle.read())
        else:
            raise ValueError("Input files must use .csv or .json extensions.")
    validate_records(records, id_field, path.name)
    return records


def parse_money(value, *, positive=False):
    """Read USD cents, tolerating only negligible binary floating-point noise."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return None
        rounded = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        if abs(amount - rounded) > FLOAT_TOLERANCE or (positive and rounded == 0):
            return None
    except DecimalException:
        return None
    return rounded


def _normalize_optional_date(value):
    if value is None or isinstance(value, str) and value.strip().lower() in {"", "none"}:
        return None
    return value.strip() if isinstance(value, str) else value


def parse_date(value):
    """Return a valid ISO calendar date, or None for missing/invalid input."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _base(raw, fields):
    record = {key: raw[key] for key in fields if key in raw}
    issues = [f"Provide the required {key} field." for key in fields if key not in raw]
    for key in ("name", "description", "restriction_text"):
        if key in fields and (not isinstance(raw.get(key), str) or not raw[key].strip()):
            issues.append(f"Provide nonempty {key} text.")
    if "school" in record:
        record["school"] = canonical_tag(record["school"])
    if not record.get("school"):
        issues.append("Provide the school.")
    if "purpose_tags" in record:
        tags = record["purpose_tags"]
        tags = tags.split(";") if isinstance(tags, str) else tags
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            issues.append("Supply purpose_tags as a list or semicolon-separated text.")
        else:
            record["purpose_tags"] = list(dict.fromkeys(tag for value in tags if (tag := canonical_tag(value))))
    for key in ("spend_by_date", "needed_by_date"):
        if key in record:
            record[key] = _normalize_optional_date(record[key])
    return record, issues


def normalize_fund(raw):
    """Keep the supplied balance and flag inconsistent month-end accounting."""
    fund, issues = _base(raw, FUND_FIELDS)
    if raw.get("type") not in ("gift", "grant", "endowment"):
        issues.append("Provide type as gift, grant, or endowment.")
    if parse_date(fund.get("reporting_month")) is None:
        issues.append("Provide a valid reporting_month in YYYY-MM-DD format.")
    if fund.get("spend_by_date") is not None and parse_date(fund["spend_by_date"]) is None:
        issues.append("Provide spend_by_date as YYYY-MM-DD, blank, or none.")
    values = {key: parse_money(raw.get(key)) for key in (
        "opening_balance", "inflows", "monthly_spending", "available_balance"
    )}
    for key, value in values.items():
        if value is None:
            issues.append(f"Provide a valid nonnegative {key} in dollars and cents for the reporting month.")
    if all(value is not None for value in values.values()):
        try:
            expected = values["opening_balance"] + values["inflows"] - values["monthly_spending"]
            if expected < 0 or expected != values["available_balance"]:
                issues.append("available_balance must equal opening_balance + inflows - monthly_spending to the cent.")
        except DecimalException:
            issues.append("The monthly amounts cannot be reconciled; verify their numeric values.")
    fund["_data_issues"] = list(dict.fromkeys(issues))
    return fund


def normalize_need(raw):
    """Use the unfunded remainder; needed-by dates are not planned spending dates."""
    need, issues = _base(raw, NEED_FIELDS)
    need["category"] = canonical_tag(raw.get("category"))
    if need["category"] is None:
        issues.append("Provide the need's category.")
    need["priority"] = canonical_tag(raw.get("priority"))
    if need["priority"] not in PRIORITIES:
        issues.append("Provide priority as low, medium, or high.")
    needed = parse_money(raw.get("amount_needed"), positive=True)
    funded = parse_money(raw.get("amount_funded"))
    if needed is None:
        issues.append("Provide a valid positive amount_needed in dollars and cents.")
    if funded is None:
        issues.append("Provide a valid nonnegative amount_funded in dollars and cents.")
    status = canonical_tag(raw.get("status"))
    need["status"] = status
    if status not in {"unfunded", "partially_funded", "funded"}:
        issues.append("Provide status as unfunded, partially_funded, or funded.")
    need["remaining_amount"] = None
    need["_fully_funded"] = False
    if needed is not None and funded is not None:
        try:
            need["remaining_amount"] = needed - funded
        except DecimalException:
            issues.append("The funding amounts cannot be reconciled; verify their numeric values.")
        if funded > needed:
            issues.append("amount_funded exceeds amount_needed; confirm the record.")
        elif (status == "unfunded" and funded != 0
              or status == "partially_funded" and not 0 < funded < needed
              or status == "funded" and funded != needed):
            issues.append("The need's status disagrees with amount_needed and amount_funded.")
        elif status == "funded" and funded == needed:
            need["_fully_funded"] = True
    if need.get("needed_by_date") is not None and parse_date(need["needed_by_date"]) is None:
        issues.append("Provide needed_by_date as YYYY-MM-DD, blank, or none.")
    need["_data_issues"] = list(dict.fromkeys(issues))
    # A damaged record must not bypass review through a funded flag.
    if issues:
        need["_fully_funded"] = False
    return need
