---
name: parse-vacancy
description: Canonical schema for normalizing a single job/position into a concise JSON object. Used by the fetcher agent when extracting positions from source pages — so that all boards yield the same data shape for dedup and matching.
---

Normalize each position into exactly this schema (JSON object). Missing field →
`null`. Do not add fields outside the schema, do not nest objects.

```json
{
  "title":    "string — role title as on the site (e.g. 'Senior Backend Engineer')",
  "company":  "string | null — the hiring organization",
  "location": "string | null — city/country or 'Remote'",
  "remote":   "boolean | null — true if explicitly remote; null if not stated",
  "salary":   "string | null — range as on the site, e.g. '$120k–160k' or '120-160k USD'",
  "url":      "string — DIRECT link to the position (not to a list/filter)",
  "posted":   "string | null — posting date, ISO or as on the site ('2d ago')",
  "tags":     "array<string> — stack/key tokens (e.g. Go, Postgres, Kubernetes…)",
  "source":   "string — name of the source from resources.yaml",
  "category": "string — job | vc_board | bounty | contest | grant | hackathon"
}
```

## Fill-in rules

- **url** must lead to a specific position. If there is only a link to a
  list/filter — better to skip the position than to give a non-targeted link
  (matcher will not be able to put a working link in the digest).
- **company**: the employer is the real hiring organization. A board aggregator
  (Web3 Career) is NOT the company — it goes in `source`.
- **tags**: short technical tokens, not sentences. At most ~8. Extract exactly
  the stack and specialty — the score relies on them. Prefer specific stack/
  specialty terms over broad umbrella terms when there is something specific.
- **remote**: `true` only with an explicit cue (Remote / Worldwide / "work from
  anywhere"). Hybrid or in-city office → `false`. Not stated → `null`.
- **salary**: copy as is, do not convert currency and do not invent a range.
- Do not translate role and company names; do not rewrite them "more nicely".
