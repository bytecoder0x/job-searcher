---
name: resume-to-profile
description: Turn a résumé/CV into the profile schema this agent searches by. Used by onboarding so anyone can point the agent at their own job search from their CV.
---

# Résumé → profile

Read the résumé and output ONE JSON object matching the profile schema below.
Infer, don't invent: base every field on what the résumé actually shows. Leave a
field empty/null rather than guessing. The domain is whatever the CV is about
(web3, frontend, data, devops, …) — do not force it toward any one field.

## Output schema (JSON object, these keys only)

```
{
  "focus_categories": ["job", "vc_board"],  // default both; vc_board = VC/ecosystem job boards
  "identity": {
    "name": "<full name or ''>",
    "role": "<one-line target role, e.g. 'Senior Backend Engineer (Go, distributed systems)'>",
    "seniority": "<junior | mid | mid-senior | senior | 'junior to senior'>",
    "years_experience": <integer or null>
  },
  "skills": {
    "must_have": [<core skills the person clearly has AND would search on — 6-12 items>],
    "nice_to_have": [<secondary/adjacent skills — bonus, not required>],
    "languages_spoken": [<ISO-ish codes, e.g. "en", "uk">]
  },
  "preferences": {
    "work_format": [<subset of "remote","hybrid","onsite" — infer, default all three>],
    "locations": [<cities/countries if stated, else []>],
    "relocation_ok": <true|false — default true unless the CV implies otherwise>,
    "employment": [<subset of "full-time","contract","part-time">],
    "min_salary_usd": <number or null>,       // only if the CV states a floor
    "salary_target_usd": <number or null>,    // only if inferable, else null
    "timezones_ok": [<e.g. "UTC+1..UTC+3"> or []]
  },
  "exclude": {
    "keywords": [<role words clearly NOT wanted, e.g. "intern","unpaid"; default ["intern","internship","unpaid"]>],
    "seniority_below": "<lowest acceptable seniority, e.g. 'junior'>"
  },
  "summary": "<3-5 sentence recruiter-style summary of the candidate and what they're seeking>"
}
```

## Rules
- `must_have` = the skills to actually FILTER and SCORE on. Pick the person's real
  strengths that define the roles they want — not every keyword on the page.
- `role` and `seniority` drive the role/level score; make them specific.
- Output ONLY the JSON object — no prose, no markdown fence, no commentary.
- If the résumé text is too sparse to fill a field, use "" / [] / null.
