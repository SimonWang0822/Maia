"""Check extracted restrictions, then rank independently fundable candidates.

These are recommendations from supplied data, not spending authorizations.
LLM-derived eligibility confidence is qualitative and never a probability.
"""

from datetime import date

from .data import PRIORITIES, canonical_tag, normalize_fund, normalize_need, parse_date, parse_money
from .extractor import ExtractionError, validate_extraction


REVIEW = "needs human review"
RANKING_BASIS = [
    "Eligible needs only; priority high before medium before low",
    "Need deadline, earliest first (unspecified dates last)",
    "Equal priority and deadline share a rank; need ID stabilizes display only",
]


def _result(fund, need, status, reason, *, checks=None, evidence=None, questions=None):
    return {
        "fund_id": fund["fund_id"],
        "need_id": need["need_id"],
        "status": status,
        "rank": None,
        "ranking_details": None,
        "confidence": "low" if status == REVIEW else "medium",
        "reason": reason,
        "restriction_evidence": list(dict.fromkeys(evidence or [])),
        "unresolved_questions": questions or [],
        "checks": checks or [],
    }


def review_fund(fund, needs, reason):
    """Return review records when extraction failed; never manufacture rules."""
    return {
        "fund_id": fund["fund_id"],
        "extraction_status": "failed",
        "extraction": None,
        "results": [
            _result(fund, need, REVIEW, reason, questions=[reason]) for need in needs
        ],
    }


def _evaluate_pair(fund, need, extraction, as_of):
    checks = []
    evidence = [rule["evidence"] for rule in extraction["rules"]]
    if extraction.get("unrestricted_evidence"):
        evidence.append(extraction["unrestricted_evidence"])

    def check(field, status, detail):
        checks.append({"field": field, "status": status, "detail": detail})

    for issue in fund.get("_data_issues", []) + need.get("_data_issues", []):
        check("source_data", "unknown", issue)
    reporting_month = parse_date(fund.get("reporting_month"))
    if reporting_month and (reporting_month.year, reporting_month.month) > (as_of.year, as_of.month):
        check("reporting_month", "unknown", "The balance is from a future reporting month; provide a current snapshot.")
    if need.get("_fully_funded"):
        return _result(fund, need, "no_match", "This need is already fully funded.",
                       checks=[{"field": "funding", "status": "fail", "detail": "No unfunded amount remains."}])

    for rule in extraction["rules"]:
        field = rule["field"]
        actual = canonical_tag(need.get(field))
        allowed = {canonical_tag(value) for value in rule["values"]}
        label = field.replace("_", " ")
        values = ", ".join(value.replace("_", " ") for value in rule["values"])
        if actual is None:
            check(field, "unknown", f"Provide the need's {label} to check the restriction.")
        elif rule["operator"] == "in":
            passed = actual in allowed
            check(field, "pass" if passed else "fail",
                  f"The need's {label} is {actual.replace('_', ' ')}; permitted: {values}.")
        else:
            passed = actual not in allowed
            check(field, "pass" if passed else "fail",
                  f"The need's {label} is {actual.replace('_', ' ')}; excluded: {values}.")

    balance = parse_money(fund.get("available_balance"))
    amount = parse_money(need.get("remaining_amount"), positive=True)
    if balance is None:
        check("available_balance", "unknown", "Provide a valid nonnegative spendable balance in dollars and cents.")
    if amount is None:
        check("remaining_amount", "unknown", "Provide valid funding amounts with a positive unfunded remainder.")
    if balance is not None and amount is not None:
        if amount > balance:
            check("funding", "unknown",
                  f"The request ({amount:,.2f} USD) exceeds the spendable balance "
                  f"({balance:,.2f} USD). Confirm whether partial funding is permitted.")
        else:
            check("funding", "pass",
                  f"The {amount:,.2f} USD remaining request is within the "
                  f"{balance:,.2f} USD spendable balance.")

    deadline_value = fund.get("spend_by_date")
    deadline = parse_date(deadline_value)
    if "spend_by_date" not in fund:
        check("spend_by_date", "unknown", "Confirm the fund's spend-by date, or explicitly supply null for no deadline.")
    elif deadline_value is None:
        check("spend_by_date", "pass", "The supplied fund data explicitly specifies no spending deadline.")
    elif deadline is None:
        check("spend_by_date", "unknown", "Provide a valid fund spend-by date in YYYY-MM-DD format.")
    elif deadline < as_of:
        check("spend_by_date", "fail", f"The fund's {deadline} spending deadline has passed as of {as_of}.")
    else:
        check("spend_by_date", "unknown",
              f"Confirm the expense can be incurred by {deadline}; a needed-by date is not a spending date.")

    needed_value = need.get("needed_by_date")
    needed = parse_date(needed_value)
    if needed_value is not None and needed is None:
        check("needed_by_date", "unknown", "Provide a valid needed-by date in YYYY-MM-DD format, or leave it blank.")
    elif needed is not None and needed < as_of:
        check("needed_by_date", "unknown", "The need's deadline has passed; confirm it is still active.")

    failures = [item["detail"] for item in checks if item["status"] == "fail"]
    questions = [item["detail"] for item in checks if item["status"] == "unknown"]
    if failures:
        return _result(fund, need, "no_match", " ".join(failures),
                       checks=checks, evidence=evidence, questions=questions)
    if questions:
        return _result(fund, need, REVIEW, " ".join(questions),
                       checks=checks, evidence=evidence, questions=questions)
    reason = " ".join(item["detail"] for item in checks if item["status"] == "pass")
    return _result(fund, need, "match", reason, checks=checks, evidence=evidence)


def evaluate_fund(fund, needs, extraction, as_of):
    """Validate one extraction, evaluate every need, and rank only matches.

    Any ambiguous/incomplete extraction sends the entire fund to human review.
    Balances are checked independently: ranking is not a combined allocation.
    """
    fund = normalize_fund(fund)
    needs = [normalize_need(need) for need in needs]
    try:
        validated = validate_extraction(fund, extraction)
    except ExtractionError as exc:
        return review_fund(fund, needs, f"Restriction extraction could not be validated: {exc}")

    questions = [item["question"] for item in validated["unresolved_conditions"]]
    if not validated["all_conditions_captured"]:
        questions.append("Confirm all restrictions; the extractor could not capture every condition.")
    if questions:
        evidence = [item["text"] for item in validated["unresolved_conditions"]]
        return {
            "fund_id": fund["fund_id"],
            "extraction_status": "needs human review",
            "extraction": validated,
            "results": [
                _result(fund, need, REVIEW, " ".join(questions), evidence=evidence, questions=questions)
                for need in needs
            ],
        }

    results = [_evaluate_pair(fund, need, validated, as_of) for need in needs]
    needs_by_id = {need["need_id"]: need for need in needs}
    matches = [result for result in results if result["status"] == "match"]
    def ranking_key(result):
        need = needs_by_id[result["need_id"]]
        return -PRIORITIES[need["priority"]], parse_date(need.get("needed_by_date")) or date.max

    matches.sort(key=lambda result: (*ranking_key(result), result["need_id"]))
    rank, previous_key = 0, None
    for result in matches:
        key = ranking_key(result)
        if key != previous_key:
            rank += 1
            previous_key = key
        result["rank"] = rank
        need = needs_by_id[result["need_id"]]
        result["ranking_details"] = {
            "priority": need["priority"], "needed_by_date": need.get("needed_by_date"),
        }
    # The JSON array itself presents matches in rank order, followed by review/rejections.
    results.sort(key=lambda result: (
        {"match": 0, REVIEW: 1, "no_match": 2}[result["status"]],
        result["rank"] or 0,
        result["need_id"],
    ))
    return {"fund_id": fund["fund_id"], "extraction_status": "validated", "extraction": validated, "results": results}
