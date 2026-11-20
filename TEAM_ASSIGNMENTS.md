# MAIA Team Assignments

## Dev 1, data and backend — AYAN

- Agree the data schema with the other devs in the first hour. Suggested fields:
  - Fund ID, name, type (gift, grant, endowment), and restriction text.
  - School and purpose tags, opening balance, inflows, and monthly spending.
  - Spend-by date.
- Write a script that generates about 150 realistic funds across 8 schools, plus a list of about 40 unfunded needs. Needs include scholarships, lab equipment, and TA support, each with school and purpose tags.
- Load it into a simple database and expose endpoints for funds, utilization, and needs. Share the CSVs so the others aren't blocked.

## Dev 2, dashboard — RYAN

- Build on mock JSON today. Keep it simple and Excel-like, since your users are admins aged 45 to 65. Use tables, filters, and export.
- Screens:
  - A fund table showing utilization %, burn rate, and days to spend-by date.
  - Spending-over-time charts.
  - Accounts with the most versus least cash inflow.
- Tonight's goal: a clickable table with filters running on mock data.

## Dev 3, recommendations — SIMON

- Build the matching engine: a fund's restriction text plus a need goes in, and a ranked list of matches comes out with a plain-English reason and a confidence level.
- If the restriction is ambiguous, it must say "needs human review" instead of guessing.
- Tonight's goal: a script that runs 5 funds against 10 needs and returns structured results.

## Business 1, customer discovery — SARAH

- Make a list of 15 people to interview: grant and fund administrators, department business officers, and gift-fund managers. Send outreach today. The goal is 5 conversations by Nov 19.
- Questions to ask:
  - How do you find unspent restricted funds today, and which tools do you use?
  - What happens when funds sit unused or expire?
  - Who decides where they go?
  - What would you need to trust a recommendation?
  - What does the system of record look like? Ask, don't assume.

## Business 2, domain and test cases — NATHAN

- Write a one-page explainer of restricted versus unrestricted funds and the common restriction types.
- Pull public numbers to make the mock data realistic: USC's published financial statements, its Form 990, and federal award data on USAspending.gov.
- Start the one-pager: the problem, the buyer, and rough ROI math, such as "if X% of restricted balances sit idle, that's $Y."
