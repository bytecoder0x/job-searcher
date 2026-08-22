# Job matching rules

Hard criteria and the scoring rubric. Read by the matcher agent together with
[profile.yaml](../../profile.yaml). Keep them short and unambiguous — this saves
tokens and keeps scoring stable across runs.

> The digest output format is NOT described here — that is presentation, not
> policy; its only home is the `format-digest` skill. This file only covers what
> to filter, how to weight, and what to prioritize.

## 1. Hard filter (applied BEFORE the expensive model, in Python)

A job is **dropped immediately**, pre-model, if any of the following holds:

- Category is not in `profile.focus_categories` — enforced structurally: only
  resources tagged with a focus category are fetched at all.
- The job was already shown (present in the dedup store) — checked first, so a
  repeat never even reaches the keyword filter.
- Title/company/tags contain a whole-word match of `profile.exclude.keywords`.
- Title or URL is missing (malformed extraction).

Seniority (`profile.exclude.seniority_below`) and work format
(`profile.preferences.work_format`) are **not** hard-filtered — a heuristic
pre-model drop risks a false negative (e.g. seniority stated only in the JD
body, or a hybrid role worth surfacing anyway). They are judged by the matcher,
with full context, as the "Role/level" and "Format/location" components in §2.

## 1a. Eligibility gate (matcher-applied — firm exclusion, not Python)

A role must be one the candidate can actually be **hired** for. The policy
itself is per-candidate and lives in the profile, not here — read
`profile.preferences.location_policy` together with
`profile.preferences.locations` and apply it as a **firm disqualification**
(drop — do not send, regardless of skill match), using the full JD context.

- `location_policy` is free text (work authorisation, visa, EOR/B2B, regions).
  Honour it literally: a role the policy rules out is dropped even on a perfect
  skill match; a role it explicitly allows is kept.
- **Empty `location_policy` → no eligibility exclusion at all.** Never invent a
  visa or authorisation constraint the profile does not state.
- Location unstated or ambiguous in the JD → **not** disqualified (judge
  normally; the soft signal is handled by the Format/location component in §2).
- An exclusion the policy implies can still be overridden by the JD itself —
  e.g. a region-restricted posting that explicitly offers contractor/B2B, EOR
  or out-of-region hiring.

This gate is applied by the matcher, not the Python hard filter in §1 —
eligibility is usually stated in the JD body, not the title/tags, so a pre-model
drop would risk false negatives.

## 2. Relevance scoring (score 0–100, expensive model)

Only jobs that survived the hard filter reach this step. Score by components:

| Component | Weight | What to consider |
|---|---:|---|
| Skill match | 40 | How many `must_have` are present; `nice_to_have` is a bonus |
| Role/level | 25 | Fit against `identity.role` and `seniority` |
| Format/location | 15 | remote/timezone/location compatible with preferences |
| Compensation | 10 | A range exists and it is ≥ `min_salary_usd` (no range → 5/10) |
| Quality/signal | 10 | Known/reputable employer, freshness, clear JD |

Final `score` = sum. Digest threshold: **score ≥ 60**.

Score bands (rationale shorthand, aligned with the digest one-liner):

| Score | Meaning |
|---|---|
| 80–100 | near-perfect fit |
| 60–79 | good fit |
| 40–59 | partial fit |
| < 40 | poor fit — not sent |

## 2a. Priority roles (the candidate's active target)

`profile.priority_roles` lists the roles the candidate is actively hunting. It is
a **preference, not a requirement** — a role absent from that list is scored
normally and never disqualified. An empty list simply switches this section off.

When a posting clearly matches one of them (by responsibilities, not just by a
buzzword in the tags):

- Score **Role/level at the top of its band** — a priority role is by definition
  the role the candidate wants, so "not the exact seniority word" must not sink it.
- Add **up to +8** to the final score, capped at 100. Reserve the full +8 for an
  unmistakable match: the posting's core responsibility IS the priority role.
- Say which priority role it hit in the one-line `reason`.

Adjacency counts. Specialisations that share the same day-to-day work are one
target, not separate niches — judge by what the job actually does, using your own
knowledge of the field, rather than by whether the title matches the wording in
`priority_roles`.

## 3. Digest prioritization

Sort by `score` descending, top-N from `resources.yaml → meta.digest_top_n`.
On a score tie — fresher first. Do not duplicate the same job across boards
(the same JD is often aggregated by several sites — treat as a duplicate by the
pair "normalized title + company").
