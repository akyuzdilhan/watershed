# Watershed

Football's fine margins, measured live and explained to you.

An editorial desk for live football broadcasts: every key moment gets a win-probability swing and a reason backed by event data, a producer approves it before it goes on air, and the card is retracted or corrected when the feed changes. Approved cards feed the post-match analysis.

Status: work in progress for a hackathon submission. Synthetic data only.

## Repository layout

| Path | Content |
|---|---|
| `src/generator/` | Seeded synthetic match generator (events as JSON Lines) |
| `src/worker/` | Event consumer, metrics, agent workflow |
| `src/winprob/` | Win-probability model behind a replaceable interface |
| `src/web/` | Producer console and overlay (Dash) |
| `data/replay/` | Pre-generated synthetic match used by the public demo |
| `infra/` | Bicep and azd configuration |
| `tests/` | Unit tests |

## License

MIT, see `LICENSE`. Third-party notices in `THIRD_PARTY_NOTICES.md`.
