# Flow verification scripts

One script per flow, so a failure points at one place instead of somewhere
in a long end-to-end run. Run them in order — the cheap ones first.

All of them read `.env` from the repo root, so run from the repo root:

```bash
python scripts/verify_1_config.py
```

| # | Script | API calls | What it checks |
|---|---|---|---|
| 1 | `verify_1_config.py` | **0** | Which provider actually resolved, endpoint reachable, budget left |
| 2 | `verify_2_sources.py` | **0** | Every CSV and every SHEET is discovered; sender identity derived from folder |
| 3 | `verify_3_classification.py` | 1 per set | What a sheet contains — model opinion vs deterministic rules |
| 4 | `verify_4_column_mapping.py` | ~2 per sheet | **The messy-Excel flow.** Three opinions per column: memory / AI / dictionary |
| 5 | `verify_5_memory.py` | **0** | Memory scopes (exact → generalised → conflict) and document-shape matching |
| 6 | `verify_6_review.py` | ~2 (or 0 with `--offline`) | The human review round trip, and the guard against unoffered answers |
| 7 | `verify_7_end_to_end.py` | ~2 per set | The whole unified pass: discover → classify → map → route → stage |
| 8 | `verify_8_market_intelligence.py` | **0** (or ~1/doc with `--live`) | Market intelligence: chat, document and spreadsheet routes, and the market-vs-hazard boundary |
| — | `verify_pdf_paths.py` | ~2 | The three PDF cases: clean text, poor-quality text, no text |
| — | `verify_live_ai.py` | ~3 | The original live smoke test |

## Flags

Every script takes `--trace`, which logs the **exact prompt sent** and the
**raw, unparsed response** for every model call. Use it when an extraction
looks wrong and you need to know whether the fault is the prompt, the
model, or the parser.

The AI-using scripts take `--limit` to cap how many record sets they touch.
On a shared or metered budget, keep it low.

`verify_6_review.py --offline` exercises the review API with no model calls
at all.

`verify_8_market_intelligence.py` is the other way round: it runs OFFLINE by
default and needs `--live` to use the model at all. Its stub answers vary by
filename, so the offline run still shows the cases worth seeing — an article
that yields no signals, and one rejected for stating no date.

## Budget

Every script prints the AI usage it caused, and — on the gateway provider —
the shared budget before and after. That budget is **cumulative and shared
with everyone holding the same token**, so `verify_1_config.py` (which
costs nothing) is worth running before any batch.

## Reading the output

Two things that look like failures and are not:

- **Columns "dropped, not guessed."** A column with no confident meaning is
  dropped rather than mapped on a guess. That is the correct outcome.
- **A market signal with zero signals extracted.** Most articles state no
  change that would move a cost, a transit time, a capacity or a demand.
  An empty result is the correct answer, not a failed read.
- **A market signal rejected for having no date (R-029).** Dates are never
  defaulted to today. A two-year-old clipping stamped with today's date
  would look like this morning's news.
- **"Awaiting confirmation" on a column all three methods agreed about.**
  Anything bound for the optimizer is confirmed by a human once, regardless
  of agreement. A wrong number reaching the optimizer produces a confident
  wrong answer, which is worse than a slow one.
