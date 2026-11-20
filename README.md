# MAIA

Match funds to needs using restriction text, with ranked results, plain-English
reasons, source evidence, and qualitative confidence.

## Run

Python 3.10 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Export `MODEL_API_KEY`, then supply your input files explicitly:

```bash
python run_demo.py --funds path/to/funds.csv --needs path/to/needs.csv
```

Both inputs accept CSV or JSON arrays. Results go to `output/results.json`.
Use `--output`, `--model`, or `--as-of YYYY-MM-DD` to override those settings;
otherwise deadline checks use today's local date.

The files in `data/` are the original synthetic examples (5 funds, 10 needs).
To exercise those examples:

```bash
python run_demo.py --funds data/funds.csv --needs data/needs.csv --as-of 2026-11-19
```

## Input schema

All listed fields are required. Dates use `YYYY-MM-DD`. A nullable date must be
present but may contain blank text, `none`, or JSON null. CSV tags use semicolons;
JSON tags use arrays. Money is USD, without currency symbols or separators.

| Fund fields | Use |
| --- | --- |
| `fund_id`, `name` | Unique ID and display name |
| `type` | `gift`, `grant`, or `endowment` |
| `restriction_text` | Rules; standalone text `None` means unrestricted |
| `school`, `purpose_tags` | Ownership and descriptive tags; these do not create restrictions |
| `reporting_month` | Date of the monthly reporting snapshot |
| `opening_balance`, `inflows`, `monthly_spending` | Monthly accounting values |
| `available_balance` | Spendable balance: opening balance + inflows − monthly spending |
| `spend_by_date` | Nullable spending deadline |

| Need fields | Use |
| --- | --- |
| `need_id`, `name`, `description` | Unique ID, display name, and description |
| `school`, `category`, `purpose_tags` | School, spending category, and descriptive tags |
| `amount_needed`, `amount_funded` | Total cost and funding received; their difference is the remaining request |
| `needed_by_date` | Nullable need deadline |
| `priority` | `low`, `medium`, or `high` |
| `status` | `unfunded`, `partially_funded`, or `funded`; must agree with amounts |

School/category normalization handles case, spaces, and hyphens. It does not
invent synonyms. Extra input fields are ignored. Supplied values are validated;
inconsistent balances or funding statuses are not repaired. Money calculations
use decimal cents, allowing only negligible floating-point noise. Inputs must
contain unique IDs and one snapshot per fund. Duplicate JSON keys and malformed
CSV rows are rejected.

## Matching

1. The Meta Responses API extracts explicit restrictions into rules on `school`
   and `category`, with exact source quotes. Conditions outside those fields,
   ambiguity, and missing qualifications remain unresolved.
2. Python validates the extraction and checks each need against its rules, the
   remaining amount, the available balance, and the supplied dates.
3. Only `match` results are ranked. `no_match` identifies a known violation;
   uncertain cases say `needs human review` and include clarification questions.

An ambiguous or incomplete extraction sends every need for that fund to review.
For a clear extraction, a known violation can reject a need even if another check
is uncertain. Fully funded needs are excluded. Insufficient balances require a
partial-funding decision. Expired funds cannot match; future reporting months and
past need deadlines require review.

`needed_by_date` is not evidence of when money will actually be spent. An
unexpired fund with a finite spending deadline therefore requires timing
confirmation. Student-level qualifications are also unresolved because the input
schema has no student-level field. Descriptions and tags are not used to invent
those facts.

## Ranking and confidence

Within each fund, eligible needs are ordered by:

1. Supplied priority: `high`, then `medium`, then `low`.
2. Earlier `needed_by_date`; unspecified dates last.

Identical priority/date combinations share a rank (1, 1, 2). IDs only stabilize
presentation. Each match includes `ranking_details` so the order is inspectable.
This is a business ordering rule, not a measured relevance score. In particular,
a high-priority need always precedes a low-priority need, regardless of deadline.
Accuracy has not been measured against administrator-reviewed examples.

Confidence is `low` for review cases and `medium` otherwise. These labels are
fixed qualitative assessments, not model probabilities or validated accuracy.
Source quotes and format checks cannot establish that an interpretation is right.
Each candidate is evaluated independently; the list does not allocate funds or
reserve balances across needs.

## API and output

The client uses `https://api.meta.ai/v1/responses`, model `muse-wonder-1.0`, and
`Authorization: Bearer $MODEL_API_KEY`. It sends the fund ID and restriction text
in a nonstreaming `input_text` request. There is at most one call per fund, with
up to three concurrent calls. Standalone `None` is handled as the schema's explicit
unrestricted marker. There is no offline runtime mode.

Requests have a 5-second connection timeout and 45-second read timeout, with no
automatic retries. API failures or invalid extractions produce review records for
all affected pairs. No API key is written to the report.

Output schema version `2.0` includes `currency`, `ranking_basis`, `summary`, and
`fund_results`. Each pair has IDs, `status`, nullable `rank`, `ranking_details`,
`confidence`, `reason`, `restriction_evidence`, `unresolved_questions`, and `checks`.
Exit codes: 0 for a completed run, 1 for setup/input/write errors, 2 for extraction
failures or invalid CLI arguments.

## Code and tests

- `run_demo.py`: CLI, concurrent extraction, JSON report writing.
- `maia_matcher/data.py`: file loading, normalization, schema validation.
- `maia_matcher/extractor.py`: API client and extraction validation.
- `maia_matcher/engine.py`: eligibility, explanations, ranking.

```bash
python3 -m unittest discover -s tests -v
```

Tests simulate API responses and validate Python behavior, including all 50 sample
pairs. They do not establish live model interpretation quality.
