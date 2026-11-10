# STRATEGY.md — Werewolf agent (werewolf-agent-01, $0 rule-based)

## Kit / protocol status (updated 2026-11-11 02:15 WIB)

- Competition: werewolf-agent-01, $500 over 20 places (1st $100, 2nd $70,
  3rd $50, 4th–10th $20, 11th–20th $14). Deadline Thu 12 Nov 02:00 WIB
  (2026-11-11T19:00:00Z). 0 submissions at last check (01:27 WIB).
- Must beat house baseline claude-opus-5-5 asked plainly (90% paired
  interval) to qualify for a paid place.
- Requirements (GET /api/competitions/werewolf-agent-01/requirements):
  main.py at repo root, MIT/Apache LICENSE, repo created after
  2026-11-10T19:00:00Z, transcripts bundle required, max 5 submissions/day
  and 10 total, run limits 1800s / 1 CPU / 512 MB / 64 pids, deterministic
  (2 runs, same action log). `model_call` check: applies=false, and the
  description says "Model calls are optional" — a no-model entry is valid.
  Payout rail is manual (escrow null), paid in USDC on Solana.
- REWIRED 02:05–02:15 WIB to the EXACT protocol from the competition's
  "rules" field (GET /api/competitions/werewolf-agent-01, verified live):
  POST /observe {} -> pending [{game, decision, options|max_chars, view}],
  POST /game {"game": n}, POST /act {"game", "decision", "target"|"text"|
  "pass"}. Decisions speak/vote/kill/protect/inspect; seats 1-8; exit when
  "done" is true. main.py is now 887 lines, py_compile OK.
- Kit STILL 409 at 19:01, 19:04, 19:06, 19:13–19:16 UTC ("practice trials
  are not drawn yet") — the server-side draw has not run even ~16 min
  after opening. So the exact JSON field shapes in views (partner key,
  private-record keys, log entry shapes) are still unconfirmed: EnvClient
  parses them defensively (multiple key aliases) and must be checked
  against RULES.txt / prompt.txt / env server once the kit lands, then
  grade-local must run. Reproduce command from the API:
  `node cli/makefun.mjs grade-local --kit . --bot <dir> --driver docker`.
  No Docker on this VM — try `--driver process` first.

## How the Brain plays

Pure logic in main.py, no model, no clock, no unseeded randomness.
All choices come from hashlib-seeded random.Random streams keyed by
(seed, game, day, decision kind, counter).

- Suspicion score per living player: +2 vote for a flipped villager,
  +3 defend a flipped wolf, +2 accuse a flipped villager, −3 vote for a
  flipped wolf, +1.5 if a known wolf voted for them, +4 if they claim
  seer while we are the seer, +1.5/+1.0 night-kill bonus (the victim's
  accusers/vote targets are likelier wolves). Known wolf = 100, known
  villager = −100.
- Vote: top suspect, with consolidation — join a supported top-3 suspect
  when our leader has no support and is not far ahead, so the vote is
  not wasted on a lone suspect.
- Wolf: blend in, echo consensus, vote top villager (bandwagon when one
  has votes), bus partner only if partner is top suspect by ≥3, already
  receiving votes, and clearly doomed. Night kill: biggest threat
  (seer claimant +6, accused us, voted/accused a wolf correctly),
  pick among top 2 threats.
- Seer: check most suspicious unknown each night. Claim only with a wolf
  check, from day 2 (or when about to be voted out); day 1 steer without
  claiming so the doctor can still protect us later.
- Doctor: protect a credible seer claimant first, else the most
  threatened villager (found a wolf / leads talk), rotate among top 2.
  Hard rule enforced: never protect yourself, never protect the seat
  protected last night.
- Wolf night coordination (hard rule): if our partner already voted,
  match their pick — disagreeing kills nobody. If we vote first, pick
  the biggest threat.
- A living seer claimant's checked wolf gets +2.5 suspicion (a true
  seer names a real wolf; a fake claim costs the liar later).
- Statements: ≤500 chars, English, cite concrete evidence
  ("voted for P5, who flipped village") — house players are LLMs and
  vote on reasons. Never repeat an exact line in a game. Defend when
  accused; as wolf, never lead with a wild claim.

## Mock-env validation of the rewired client (/tmp/mock_env.py, dev only)

A mock env implementing the official protocol (observe/game/act, seats
1-8, at most 10 live games, night wolf-vote order, doctor no-repeat rule,
day-10 draw) drove the rewired main.py through full 30-game trials.
House bots are rule-based (a floor, not Haiku). Results, fixed role
schedule 15 wolf / 5 seer / 5 doctor / 5 villager:

| seed | wolf | village | mean /1000 |
|------|------|---------|------------|
| 12345 | 11/15 (73%) | 5/15 (33%) | 533 |
| 777   | 12/15 (80%) | 8/15 (53%) | 667 |
| 999   | 10/15 (67%) | 4/15 (27%) | 467 |

Mean over 3 seeds: wolf 73%, village 38%, score 556 (sim_test.py mean
was 485). Per-role over 3 trials: seer 5/15, doctor 6/15, villager 6/15.
Determinism verified: seed 12345 re-run reproduces exactly 533 with the
identical per-role split. Agent exits 0 once "done" is true; all 30 games
finish; no 400/409 errors from the mock. One bug found and fixed during
this: our own vote was counted twice (handle() + log sync) — log sync now
skips votes by our own seat.

## Local sim results (sim_test.py, rule-based house bots — a floor, not Haiku)

House bots vote ~75% top suspicion + noise, accuse randomly, and listen
to seer claims (+5 suspicion). They are much weaker teammates/opponents
than claude-haiku-5-5, so village rates here are pessimistic.

| run | wolf | village | mean /1000 |
|-----|------|---------|------------|
| 3 trials, seed 12345 | 66.7% | 26.7% | 467 |
| 3 trials, seed 777   | 64.4% | 31.1% | 478 |
| 3 trials, seed 999   | 73.3% | 28.9% | 511 |

Control, same games: random-voting agent — wolf 31/45, village 9/45,
score 444. Ours — wolf 30/45, village 14/45, score 489. So the Brain
adds real value on the village side; wolf side is about even with
random in this sim because night kills + weak village bots dominate.

## Biggest weaknesses (honest)

1. Village win rate is the weak side (27–33% in sim). One agent is 1 of
   8 votes; if Haiku teammates mislynch, we cannot carry alone.
2. Beating the Opus baseline is NOT proven. Opus "asked plainly" is a
   strong reasoner; our edge is consistency, no hallucinations, and
   clean voting — not deeper deduction. The 90% paired interval is a
   high bar. Plausible to place (top 20 pays $14+) if wolf rate stays
   high and village rate vs Haiku is better than vs sim bots; 1st–3rd
   ($50–$100) is a stretch.
3. Env protocol is rewired from the rules field, but the exact JSON
   shapes are still unconfirmed (kit 409). grade-local has NOT run.
   This is the single biggest delivery risk, not strategy.
4. Statements are template-based (~4 variants per branch). Within one
   game they do not repeat, but a sharp Haiku player may notice the
   phrasing pattern across a trial's 30 games (each game is fresh, so
   this is a minor risk).

## Still needed before submit

1. Kit (STILL 409 as of 19:16 UTC): keep retrying
   `curl -sSL "https://make.fun/c/werewolf-agent-01/kit" | tar xz`;
   read RULES.txt + env server + prompt.txt, verify EnvClient's field
   names against them, run grade-local (`node cli/makefun.mjs
   grade-local --kit . --bot ~/workspace/makefun/werewolf-agent-01
   --driver process`, then `--driver docker` if Python needs Docker —
   no Docker on this VM, report if it is missing).
2. GitHub repo under kasnap96-design, created AFTER 2026-11-10T19:00:00Z,
   public, MIT LICENSE, main.py at root, README from make.fun template,
   repo About set from requirements repo_about, `.makefun/owner.json`
   already prepared in the staging dir with wallet
   4eyJs8bzAGew3dfxGiss62fi9b7xUdMU6AQGExEw8b9i (verified: 44 chars,
   base58 only, decodes to 32 bytes).
3. User's Solana wallet PUBLIC address is now known
   (4eyJs8bzAGew3dfxGiss62fi9b7xUdMU6AQGExEw8b9i). No new wallet is
   needed and none was created; never ask for private key / seed phrase.
   Link it to the make.fun account, get API key, bundle transcripts
   (sessions that built main.py — note: this subagent's sessions are
   the build sessions; bundling is a parent/submit-step concern),
   `check` then `submit` before Thu 12 Nov 02:00 WIB. Max 5/day, 10 total.
4. If grade-local shows the env protocol differs (e.g. different
   endpoints/fields), fix EnvClient only — Brain does not change.
