---
name: format-digest
description: Format contract for the digest built from the matcher's output. Guarantees it stays readable where nothing renders Markdown — a terminal, a log, a pasted message — so no ** or ##, only emoji, indentation, and direct links.
---

The digest is rendered **in Python** (`_render_digest` in `src/digest.py`) from
the matcher's scored JSON and printed as plain text. It has to be readable where
nothing renders Markdown — a terminal, a log file, a message pasted somewhere —
so `**`, `##` and link syntax would show up as literal characters. This file is
the contract the renderer follows (and the matcher's `reason`/`score` fields feed
it).

## Hard rules

- **No Markdown emphasis:** no `**bold**`, no `*`, no `#`/`##` headings, no
  `` `code` ``, no `[text](url)`. Structure comes only from text, emoji, and
  indentation.
- Links — a **bare URL** on its own line, so it stays clickable anywhere: `🔗 https://…`.
- One block = one position. A blank line between blocks.
- Concise: no intros, no summaries, no list of what was rejected, no reasoning.

## Format of a single block

Blocks are **numbered from 1**, best score first, so a position can be referred
to by its number.

```
{n}. {score} | {Role} @ {Company} — {Location/Remote}
   💰 {range, if any}   🗓 {date, if any}
   ✅ why: {matched must_have — 1 line, no fluff}
   🔗 {direct link}
```

**Only relevant matches go in the message** (score >= threshold, capped at
`digest_top_n`). Do NOT dump every scored position: a full dump ran to 140+
entries and buried the good roles under sales/PM/IT-support noise. Weaker ones
are counted in one closing line and remain available via `/export`.

Fill-in examples:
- `88 | Senior Backend Engineer @ Stripe — Remote`
- `   ✅ why: Go + Postgres + Kubernetes`

## Header and empty case

- First line — a short header: `🎯 Top {N} over the last {days} days (from {sources} sources)`
  or more simply `🎯 Relevant positions:` if there are no metrics.
- Empty after filtering → exactly one line: `🔍 No new relevant positions.`
- Block order — by score descending (matcher already guarantees this).
