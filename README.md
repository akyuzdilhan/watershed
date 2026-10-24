# Watershed

Football's fine margins, measured live and explained to you.

An editorial desk for live football broadcasts: every key moment gets a win-probability swing and a reason backed by event data, a producer approves it before it goes on air, and the card is retracted or corrected when the feed changes. Approved cards feed the post-match analysis.

Status: work in progress for a hackathon submission. Synthetic data only.

## Repository layout

| Path | Content |
|---|---|
| `src/generator/` | Seeded synthetic match engine: event feed (JSON Lines) and hidden ground truth |
| `src/worker/` | Event consumer, metrics, agent workflow |
| `src/winprob/` | Win-probability model behind a replaceable interface |
| `src/web/` | Producer console and overlay (Dash) |
| `data/replay/` | Pre-generated synthetic match used by the public demo |
| `infra/` | Bicep and azd configuration |
| `tests/` | Unit tests |
| `spikes/` | Throwaway technical trials (Agent Framework workflow, human approval) |

## Run locally

```
python -m venv .venv
.venv\Scripts\activate            # Windows; source .venv/bin/activate elsewhere
pip install -e ".[azure,agents,dev]"
pytest
python -m generator --story demo --out data/replay      # the demo match and its hidden ground truth
python -m generator --calibrate 200                     # compare 200 unscripted matches with real orders of magnitude
python -m generator.publish data/replay/demo-0007.jsonl.gz --dry --speed 0
```

## Synthetic match feed

The generator is a small match engine, stepped like a state machine: `Clock` (match clock and broadcast clock), `Ball` and `MatchState`, `Laws` (the referee), `ActionModel` (what the player on the ball does), `Shape` (where the 22 players stand), `Director` (the script of the demo), then `Feed` and `TruthRecorder`.

- **Events.** Actions use SPADL-style types, results and body parts, on a 105 x 68 m pitch seen from the acting team. Each one carries a 360-style freeze frame: the position of the 22 players when it happens. Around them, the feed sends match information, periods, added time, substitutions, injuries, VAR checks and corrections. Every message has the same envelope: schema and generator versions, match id, message id, provider sequence number, broadcast clock and emission time.
- **Laws.** Every restart is played from the right spot. Fouls in the box give penalties, a second yellow is a red, offside is judged on the positions in the freeze frame, play stops during a VAR check, and a team makes up to five substitutions in three windows. The added time comes from the time lost, with the Premier League rule since 2024/25: a goal celebration only counts beyond its first 30 seconds.
- **The demo story.** United lead 2-0 at half-time (the second goal is credited to a team-mate two minutes later, after a deflection). Rovers bring on their number 20, who scores at 52' and 57', then again at 63': that card goes on air during the VAR check, the goal is ruled out for an offside of 0.25 m, and the card must be retracted. He scores at 66' anyway, United's equaliser at 69' is ruled out by VAR for handball (a second card to retract), Rovers' keeper saves at 72', United's centre-back gets a second yellow card at 78', and the substitute's fourth goal (83') and a last one at 88', confirmed by VAR, make it 5-2. Any other seed has no script: goals come from shots and their xG, and VAR overturns come from offsides the assistant missed.
- **What a real feed suffers.** Latency, late (out-of-order) messages, duplicates, and corrections that void, reassign or update an earlier action, each with a version number and a cause.
- **Ground truth.** It lists the planted moments with the expected action on a card already on air (retract, rewrite or none), every shot with its xG and context, the VAR checks, offsides, cards, corrections and network incidents, so the pipeline can be scored against it.

**Common Data Format.** The generator also writes each match in the [Common Data Format](https://github.com/UnravelSports/common-data-format-validator) (CDF), an open format for football match data (Anzer et al., 2025): `data/replay/cdf/` holds the demo match sheet, metadata, events and tracking frames, with corrections applied. The tests check them against the official CDF schemas, version 0.3.2, in the validator's strictest mode. The live feed keeps its own envelope (sequence numbers, versions, corrections), which CDF, a format for data at rest, does not define. The feed itself is gzipped JSON Lines.

The probabilities are hand-tuned, not fitted. Means over 200 unscripted matches, per match and for both teams:

| | Generator | Real reference |
|---|---|---|
| Goals | 3.00 | 2.87, Premier League 2024/25 |
| Expected goals (xG) | 2.97 | 2.89, Premier League 2024/25 (FotMob) |
| Shots | 25.1 | about 26, Premier League 2024/25 (Opta) |
| Shots from outside the box | 33 % | 31.7 %, Premier League 2024/25 (Opta) |
| Passes, throw-ins excluded | 979 | about 893, Premier League 2024/25 (FootballCritic, FotMob) |
| Pass completion | 84 % | 83.7 %, Premier League 2024/25 (FotMob) |
| Pass length | 20.8 m | 20.5 m, our computation on 38 matches of StatsBomb open data, Premier League 2015/16 |
| Fouls | 21.4 | 25, same StatsBomb sample |
| Yellow cards | 3.4 | 4.2, Premier League 2023/24 (Opta) |
| Tackles | 41.1 | 35 (FotMob 2024/25) to 42 (StatsBomb 2015/16) |
| Interceptions | 14.5 | 16 (FotMob 2024/25) to 24 (StatsBomb 2015/16) |
| Added time | 9.4 min (3.4 + 6.0) | 9:44 (2024/25) and 10:35 (2025/26, first seven matchdays): average match length minus 90 minutes (Opta) |

Corners (9.0) and offsides (3.8) are only checked against approximate figures, and the split of added time between the halves is an estimate. `tests/test_generator.py` checks the laws, the clocks and these bands on the same 200 matches, and that the committed demo file is exactly what the generator produces.

## Worker

`python -m worker data/replay/demo-0007.jsonl.gz --truth data/replay/demo-0007.truth.json` runs the desk on the replay file and scores it against the ground truth; `python -m worker --live --cosmos` reads the Service Bus subscription and writes events, cards and the audit trail to Cosmos DB.

- **Intake.** Messages are deduplicated by message id and put back in provider order, with a 3-second buffer for late ones. The match state (score, red cards, announced added time, final whistle) is derived from the messages, so a late or duplicated message never corrupts it. Messages with an unknown schema go to the dead-letter queue.
- **Win probability.** Before and after each action, from the Skellam model and the pre-match expected goals of `match_info`; the time left includes the added time still to come.
- **Spotter.** Every goal and red card gets a card. A shot that does not go in gets one if its xG times the swing it would have caused reaches 8 points, so a late half-chance does not outweigh a real chance; a save is credited to the keeper.
- **Corrections.** A VAR check holds the card, a goal that stands releases it, a voided goal retracts it, a reassigned goal rewrites it. On the demo match the worker finds all 11 moments of the ground truth with no extra card, and takes the three card actions it expects; on 20 unscripted matches it finds every goal and takes the right action on every corrected one (`tests/test_worker.py`).

## Writer and desk

`python -m worker data/replay/demo-0007.jsonl.gz --writer foundry --auto-approve` writes every card with the Writer agent; `--writer template` does the same without Azure.

- **Writer.** An Agent Framework agent on Microsoft Foundry (gpt-5.4-nano, gpt-5-mini as fallback) gets the facts of a card through a function tool, `get_moment`, and writes it in English, French and Spanish (shown as machine-translated). The code computes every number and names it explicitly; the model computes nothing and lists, for each number it writes, the field it comes from.
- **Verifier.** Deterministic: every number in the text must be claimed, every claim must match its field, a percentage must come from a percentage field, and no decimal probability is allowed. A failed draft gets one retry, then a fixed template takes over, so a card is never blocked by the model. Win probabilities are never shown as 0% or 100% before the final whistle.
- **Desk.** A written card waits for a producer (`pending`), who approves it, edits it or rejects it; every step goes to the audit trail. A correction retracts a card (and says whether it was on air) or has it rewritten as a new version. In auto-approve mode, used for judging, a verified card goes on air without a click.

On the demo match, the live run wrote and verified all 12 texts (11 cards and one rewrite) on the first attempt, in 11 to 17 seconds per card for three languages.

## Deploy

```
az login
azd auth login
azd up
```

Service Bus and Cosmos DB have key-based authentication disabled: the apps and the developer account get Azure roles on them and authenticate with Microsoft Entra ID.

## License

MIT, see `LICENSE`. Third-party notices in `THIRD_PARTY_NOTICES.md`.
