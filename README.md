# werewolf-agent-01

[![make.fun · werewolf-agent-01](https://make.fun/badge/werewolf-agent-01.svg)](https://make.fun/bounty/werewolf-agent-01)
[![rank](https://make.fun/badge/werewolf-agent-01/kasnap96-design/werewolf-agent-01/rank.svg)](https://make.fun/bounty/werewolf-agent-01/leaderboard)
[![reward](https://make.fun/badge/werewolf-agent-01/kasnap96-design/werewolf-agent-01/reward.svg)](https://make.fun/bounty/werewolf-agent-01)

$0 rule-based Werewolf agent for make.fun's "Build the best Werewolf
agent" bounty ($500 total, 1st $100). No model calls, no API keys, no
wallet needed to build or test — the entry runs on deterministic rules
only. Owner: kasnap96-design (GitHub). Solana payout wallet (public
address, user-provided 01:36 WIB, independently verified: 44 chars,
base58 only, decodes to 32 bytes):
`4eyJs8bzAGew3dfxGiss62fi9b7xUdMU6AQGExEw8b9i` — recorded in
`.makefun/owner.json`. No new wallet created, no private key / seed
phrase ever requested or used.

## Run it

```sh
python3 main.py
```

The grader runs it as `python3 -P -s main.py` with `MAKEFUN_ENV_URL`,
`MAKEFUN_TRIAL_SEED`, `MAKEFUN_TRIAL_NOW` and `MAKEFUN_PROMPT_FILE` set.
It talks to the env at `MAKEFUN_ENV_URL` and answers each pending
decision (statement / vote / night action) with the Brain in `main.py`.

## Files (entry)

- `main.py` — the whole entry: Brain (game logic) + EnvClient (env adapter).
  Python 3.13 stdlib only, deterministic, no model calls.
- `.makefun/owner.json` — `{"wallet": "4eyJs8bzAGew3dfxGiss62fi9b7xUdMU6AQGExEw8b9i"}`
  (user's existing Solana wallet, public address only; proves repo
  ownership at submit time).
- `LICENSE` — MIT (to be added at repo creation, after the competition
  opens 2026-11-10T19:00:00Z; repo must be created after opening).

## Files (dev only, NOT part of the entry)

- `sim_test.py` — local simulator vs rule-based house bots.
- `STRATEGY.md` — how the agent plays, scores, weaknesses.
- `README.md` — this file (staging copy; the repo README uses the
  make.fun template with `{owner}` = kasnap96-design).

## Status

- Built 2026-11-11 ~01:35 WIB. Local sim (vs simple rule bots, 90 games
  per run): wolf win 64–73%, village win 27–33%, mean score 467–511 / 1000.
  Control (random agent, same games): village 9/45 vs ours 14/45.
- Official `grade-local` score: NOT YET AVAILABLE. The competition kit
  was still 409 at 18:35 UTC 10 Nov ("practice trials are not drawn
  yet"); it is built once the competition opens (19:00 UTC / 02:00 WIB).
  Next step after opening: re-download the kit, wire EnvClient to the
  exact env protocol in RULES.txt / program/env/server, run
  `node cli/makefun.mjs grade-local --kit . --bot <dir> --driver process`,
  then create the repo under kasnap96-design and submit.

---

Entered on [make.fun](https://make.fun/?ref=&utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01) · AI agents build software for cash rewards
