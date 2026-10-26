# Live run measures: demo-0007

Source: Cosmos DB, partition demo-0007 (cards, audit). Read by `python -m worker.measure`; nothing was written and no model was called.

| | card | insight | storyline | recap | all |
|---|---|---|---|---|---|
| Writes (first versions and rewrites) | 12 | 17 | 1 | 1 | 31 |
| Written by the model | 12/12 (100.0 %) | 0/17 (0.0 %) | 0/1 (0.0 %) | 0/1 (0.0 %) | 12/31 (38.7 %) |
| Template after model attempts (fallback) | 0 | 0 | 0 | 1 | 1 |
| Template, no model call | 0 | 17 | 1 | 0 | 18 |
| Verified at write time | 12/12 (100.0 %) | 17/17 (100.0 %) | 1/1 (100.0 %) | 1/1 (100.0 %) | 31/31 (100.0 %) |
| Model writes verified at the 1st attempt | 11 | 0 | 0 | 0 | 11 |
| Model writes verified at the 2nd attempt | 1 | 0 | 0 | 0 | 1 |
| Model texts kept from a rejected reply | 0 | 0 | 0 | 0 | 0 |
| Model writes with the duo by template | 0 | 0 | 0 | 0 | 0 |
| Model calls | 13 | 0 | 0 | 2 | 15 |
| Writes with a trace | 12 | 17 | 1 | 1 | 31 |
| Attempts traced | 13 | 0 | 0 | 2 | 15 |
| Attempts with an error | 0 | 0 | 0 | 0 | 0 |
| HTTP 429 answers | 0 | 0 | 0 | 0 | 0 |
| Latency per attempt, p50 (s) | 24.4 | n/a | n/a | 15.3 | 22.6 |
| Latency per attempt, p95 (s) | 31.5 | n/a | n/a | 17.8 | 31.5 |
| Input tokens | 139 586 | n/a | n/a | 7 961 | 147 547 |
| Output tokens (reasoning included) | 42 044 | n/a | n/a | 4 399 | 46 443 |
| Reasoning tokens | 30 949 | n/a | n/a | 1 134 | 32 083 |
| Cost (USD) | n/a (no prices) | n/a (no usage) | n/a (no usage) | n/a (no prices) | n/a (no prices) |

Model calls of the match against the cap of 40 per match: 15/40; cards, insights and storyline 13, recap 2.

No prices given: pass `--price-in` and `--price-out` (USD per million tokens) for the cost.

| Fallback cause | Writes |
|---|---|
| verifier | 1 |

| finish_reason | Attempts |
|---|---|
| stop | 15 |

| Deployment | Effort | max_tokens | prompt_sha | Writes |
|---|---|---|---|---|
| gpt-5.4-nano | medium | 5000 | 3635bc733780 | 12 |
| gpt-5.4-nano | low | 6000 | 26e54d67aab9 | 1 |
