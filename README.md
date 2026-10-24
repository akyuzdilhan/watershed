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
pip install -e ".[azure,dev]"
pytest
python -m generator --story demo --out data/replay      # the demo match and its hidden ground truth
python -m generator --calibrate 200                     # compare 200 unscripted matches with real orders of magnitude
python -m generator.publish data/replay/demo-0007.jsonl --dry --speed 0
```

## Synthetic match feed

The generator is a small match engine, stepped like a state machine: `Clock` (match clock and broadcast clock), `Ball` and `MatchState`, `Laws` (the referee), `ActionModel` (what the player on the ball does), `Shape` (where the 22 players stand), `Director` (the script of the demo), then `Feed` and `TruthRecorder`.

- **Events.** Actions use SPADL-style types, results and body parts, on a 105 x 68 m pitch seen from the acting team. Each one carries a 360-style freeze frame of the 22 players (the four keys of StatsBomb 360 frames, plus synthetic player ids). Around them, the feed sends match information, periods, added time, substitutions, injuries, VAR checks and corrections. Every message has the same envelope: schema and generator versions, match id, message id, provider sequence number, broadcast clock and emission time.
- **Laws.** Every restart is played from the right spot. Fouls in the box give penalties, a second yellow is a red, offside is judged on the positions in the freeze frame, play stops during a VAR check, a team makes up to five substitutions in three windows, and the added time comes from the time lost.
- **The demo story.** Rovers 1-2 United. Rovers score at 23' (the goal is credited to a team-mate two minutes later, after a deflection), United equalise at 54', and Rovers score again at 67'. That card goes on air during the VAR check, then the goal is ruled out for an offside of 0.25 m and the card must be retracted. A United save at 74', a Rovers red card at 81', and a United goal at 88' that VAR confirms. Any other seed has no script: goals come from shots and their xG, and VAR overturns come from offsides the assistant missed.
- **What a real feed suffers.** Latency, late (out-of-order) messages, duplicates, and corrections that void, reassign or update an earlier action, each with a version number and a cause.
- **Ground truth.** It lists the planted moments with the expected action on a card already on air (retract, rewrite or none), every shot with its xG and context, the VAR checks, offsides, cards, corrections and network incidents, so the pipeline can be scored against it.

The probabilities are hand-tuned, not fitted. Means over 200 unscripted matches, per match and for both teams:

| | Generator | Real reference |
|---|---|---|
| Goals | 3.17 | 2.87, Premier League 2024/25 |
| Expected goals (xG) | 3.07 | 2.89, Premier League 2024/25 (FotMob) |
| Shots | 25.9 | about 26, Premier League 2024/25 (Opta) |
| Shots from outside the box | 33 % | 31.7 %, Premier League 2024/25 (Opta) |
| Passes, throw-ins excluded | 983 | about 893, Premier League 2024/25 (FootballCritic, FotMob) |
| Pass completion | 84 % | 83.7 %, Premier League 2024/25 (FotMob) |
| Pass length | 20.8 m | 20.5 m, our computation on 38 matches of StatsBomb open data, Premier League 2015/16 |
| Fouls | 21.6 | 25, same StatsBomb sample |
| Yellow cards | 3.3 | 4.2, Premier League 2023/24 (Opta) |
| Tackles | 40.7 | 35 (FotMob 2024/25) to 42 (StatsBomb 2015/16) |
| Interceptions | 14.5 | 16 (FotMob 2024/25) to 24 (StatsBomb 2015/16) |

Corners (8.9), offsides (4.0) and added time (10.6 minutes) are only checked against approximate figures. `tests/test_generator.py` checks the laws, the clocks and these bands on the same 200 matches, and that the committed demo file is exactly what the generator produces.

## Deploy

```
az login
azd auth login
azd up
```

Service Bus and Cosmos DB have key-based authentication disabled: the apps and the developer account get Azure roles on them and authenticate with Microsoft Entra ID.

## License

MIT, see `LICENSE`. Third-party notices in `THIRD_PARTY_NOTICES.md`.
