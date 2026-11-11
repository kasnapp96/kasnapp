#!/usr/bin/env python3
"""Werewolf agent for make.fun bounty werewolf-agent-01 — $0 rule-based entry.

Single file, Python 3.13 standard library only. No model calls, no wall clock
in any decision, no unseeded randomness: every choice is derived from
MAKEFUN_TRIAL_SEED (+ game / day / decision kind) via hashlib-seeded
random.Random streams, so live and replay runs agree.

Game (from the competition description):
  8 players: 2 werewolves, 1 seer, 1 doctor, 4 villagers.
  Our agent holds one seat; the other 7 are house players (claude-haiku-5-5).
  Night: wolves kill, doctor protects, seer checks a player's team.
  Day: 2 rotating cycles of statements (max 500 chars each), then a vote.
  Village wins when both wolves are dead; wolves win when they are as many
  as the rest. A game undecided after day 10 is a draw.
  Trial: 30 games (15 as wolf, 15 village: 5 seer, 5 doctor, 5 villager).
  Score = 1000 * (wolf_win_rate + village_win_rate) / 2, mean over 3 trials.
  Model calls are optional for this competition and the requirements list the
  model_call check as not applying, so a no-model entry is valid.

Env protocol (EXACT, from GET /api/competitions/werewolf-agent-01 "rules",
verified 02:00 WIB 11 Nov; kit still 409 "practice trials are not drawn yet"):
  Env at MAKEFUN_ENV_URL. Prompt file at MAKEFUN_PROMPT_FILE
  (/input/prompt.txt): the trial's games with seat/role per game, env routes,
  rules in short. Trial seed in MAKEFUN_TRIAL_SEED. Model gateway vars are
  dummy keys — no real key ever reaches the program.
  Games 1-30 open in order, at most 10 live at once; next opens when a live
  one ends. Exit once "done" is true.
  POST /observe {} -> {"games","finished","done",
    "pending":[{"game","decision","options" or "max_chars","view"}]}.
  Holds up to 20s when nothing waits. POST /game {"game": n} -> game n's view.
  POST /act {"game": n, "decision": ..., "target": ... | "text": ... |
    "pass": true}. Decisions: speak (text or pass), vote, kill, protect,
  inspect (target among options, or null to abstain / act on nobody).
  200 = taken; 400 bad_request/bad_target/empty/too_long, 409 not_pending
  change nothing.
  View holds: day, phase, seat, name, role, alive, werewolf partner,
  players (seat/name/alive/role-once-revealed), private record (seer
  inspections, doctor last protection, werewolves' night votes), public log
  (dawn, statements, passes, votes, exile, in order), outcome once over.
  Seats are 1-8 in the env; the Brain below uses 0-7 internally and the
  EnvClient translates (internal pid = env seat - 1).
  Night: wolves vote in turn (odd nights lower living wolf first, even
  nights the other; second sees first's pick; kill = seat every wolf who
  named someone agreed on; different seats = nobody dies; abstention
  doesn't count). Doctor protects a living seat other than self and other
  than last night's, or nobody. Seer inspects a living seat other than
  self, or nobody. Day: dawn, win check, debate 2 round-robin cycles
  starting at start seat + (n-1), skipping dead, statement 1-500 chars or
  pass, then vote in same order naming a living seat other than yourself
  or abstaining; unique plurality exiles and reveals role; tie/no votes =
  nobody. Village wins when no wolf alive; wolves win when living wolves
  >= living non-wolves; draw after day 10 = loss for both. Statements are
  folded to one line and the 500-char limit applies after folding.
  House players: claude-haiku-5-5, thinking effort low, max 1024 output
  tokens, no memory across games.

Local testing: sim_test.py (dev only, NOT part of the entry) imports Brain
and drives simulated games against rule-based house bots.
"""

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_STATEMENT_CHARS = 500
MAX_GAMES_PER_TRIAL = 30


# ---------------------------------------------------------------- seeded RNG

def _seeded_rng(seed, label):
    """Deterministic Random stream for (seed, label). Stable across runs."""
    digest = hashlib.sha256(
        b"werewolf-agent-01\0" + str(seed).encode("utf-8") + b"\0" + label.encode("utf-8")
    ).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _pick(rng, items):
    return items[rng.randrange(len(items))] if items else None


# ---------------------------------------------------------------- helpers

def _name(pid, names):
    if names and pid in names:
        return names[pid]
    return "P%d" % pid


def _short(text, limit=MAX_STATEMENT_CHARS):
    text = " ".join(str(text).split())
    return text[:limit]


# ---------------------------------------------------------------- Brain

class Brain:
    """Pure Werewolf logic. No I/O, no clock, no global randomness.

    `seat` is our seat (0..7). `role` is one of
    'wolf' | 'seer' | 'doctor' | 'villager' (unknown until the env tells us).
    Player ids are ints; `names` maps id -> display name.
    """

    def __init__(self, seed, seat, role=None, names=None, game_id=""):
        self.seed = str(seed)
        self.seat = seat
        self.role = role
        self.names = dict(names or {})
        self.game_id = str(game_id)
        self.alive = set(range(8))
        self.day = 1
        self.known_wolf = set()
        self.known_village = set()
        self.other_wolf = None
        self.my_checks = {}          # pid -> 'wolf' | 'village' (seer only)
        self.claims = {}             # pid -> claimed role ('seer' etc.)
        self.accusations = []        # (day, accuser, target)
        self.defenses = []           # (day, defender, target)
        self.votes = []              # (day, voter, target)
        self.deaths = []             # (day_or_night, pid, cause)
        self.statements = []         # (day, speaker, text)
        self.night_kills = []        # pid killed at night
        self.seer_revealed = False
        self.last_protected = None   # doctor: seat protected last night
        self.seer_claim_checks = {}  # speaker pid -> target pid they claim checked
        self._used_lines = set()
        self._counter = 0
        self._night_bonus = {}   # pid -> suspicion bonus from night-kill pattern

    # -- rng ---------------------------------------------------------------
    def _rng(self, kind):
        self._counter += 1
        label = "%s|%s|d%d|%s|%d" % (self.game_id, kind, self.day, self.role, self._counter)
        return _seeded_rng(self.seed, label)

    # -- state updates -----------------------------------------------------
    def set_role(self, role, other_wolf=None):
        self.role = role
        if role == "wolf" and other_wolf is not None:
            self.other_wolf = other_wolf
            self.known_wolf.add(other_wolf)
            self.known_village.discard(other_wolf)
        if role in ("seer", "doctor", "villager"):
            self.known_village.add(self.seat)

    def note_death(self, pid, cause=""):
        self.alive.discard(pid)
        self.deaths.append((self.day, pid, cause))
        if cause == "night":
            self.night_kills.append(pid)
            # Night victims are usually killed because they were a threat:
            # players the victim accused or voted for are likelier wolves.
            for (d, a, t) in self.accusations:
                if a == pid and t in self.alive:
                    self._night_bonus[t] = self._night_bonus.get(t, 0.0) + 1.5
            for (d, v, t) in self.votes:
                if v == pid and t in self.alive:
                    self._night_bonus[t] = self._night_bonus.get(t, 0.0) + 1.0

    def note_statement(self, speaker, text):
        self.statements.append((self.day, speaker, text))
        low = text.lower()
        # Very small keyword parser: accusations, seer claims, defenses.
        for pid in list(self.alive) + [p for p in range(8)]:
            nm = _name(pid, self.names).lower()
            if nm in low or ("p%d" % pid) in low:
                if "wolf" in low and ("is" in low or "vote" in low or "think" in low):
                    self.accusations.append((self.day, speaker, pid))
                if "trust" in low or "not a wolf" in low or "innocent" in low or "village" in low:
                    self.defenses.append((self.day, speaker, pid))
        if speaker != self.seat and ("i am the seer" in low or "i'm the seer" in low
                                      or "i am seer" in low):
            self.claims[speaker] = "seer"
        if speaker != self.seat and "i checked" in low:
            self.claims[speaker] = "seer"
            # "I checked X and they are a wolf": record the claimed check.
            for pid in list(self.alive) + [p for p in range(8)]:
                nm = _name(pid, self.names).lower()
                if nm and nm in low and "wolf" in low:
                    self.seer_claim_checks[speaker] = pid

    def note_vote(self, voter, target):
        self.votes.append((self.day, voter, target))

    def note_seer_check(self, pid, team):
        self.my_checks[pid] = team
        if team == "wolf":
            self.known_wolf.add(pid)
        else:
            self.known_village.add(pid)

    # -- suspicion ---------------------------------------------------------
    def suspicion(self, pid):
        """Higher = more likely a wolf. Known facts dominate; history votes."""
        if pid == self.seat or pid not in self.alive:
            return -999.0
        if pid in self.known_wolf:
            return 100.0
        if pid in self.known_village:
            return -100.0
        score = 0.0
        score += self._night_bonus.get(pid, 0.0)
        # Voting record
        for (d, voter, target) in self.votes:
            if voter == pid and target in self.known_village:
                score += 2.0
            if voter == pid and target in self.known_wolf:
                score -= 3.0
            if voter in self.known_wolf and target == pid:
                score += 1.5  # wolves vote villagers
            if voter in self.known_village and target == pid:
                score -= 0.5
        # Talk record
        for (d, accuser, target) in self.accusations:
            if accuser == pid and target in self.known_village:
                score += 2.0
            if accuser == pid and target in self.known_wolf:
                score -= 2.0
            if target == pid:
                score += 0.4
        for (d, defender, target) in self.defenses:
            if defender == pid and target in self.known_wolf:
                score += 3.0
            if defender == pid and target in self.known_village:
                score -= 1.0
        # Claimed seer whose check contradicts our own seer info
        if self.claims.get(pid) == "seer" and self.role == "seer":
            score += 4.0
        # A living seer claimant's checked wolf is worth trusting a bit:
        # a true seer names a real wolf; a fake claim costs the liar later.
        for claimant, target in self.seer_claim_checks.items():
            if target == pid and claimant in self.alive and claimant not in self.known_wolf:
                score += 2.5
        # A claimed seer with a wolf accusation that later proved right is trusted
        # (handled via known_wolf/known_village above through their votes).
        # Night-kill pattern: whoever pushed the night victim the day before
        # is slightly more likely village (wolves rarely push their own victim
        # loudly), whoever was quiet about them slightly more wolfy.
        return score

    def ranked_suspects(self):
        cands = [p for p in sorted(self.alive) if p != self.seat]
        rng = self._rng("rank")
        # Deterministic tie-break: shuffle by seeded order, then stable sort.
        order = list(cands)
        rng.shuffle(order)
        order.sort(key=lambda p: self.suspicion(p), reverse=True)
        return order

    def top_suspect(self):
        ranked = self.ranked_suspects()
        return ranked[0] if ranked else None

    def accused_me(self):
        return [a for (d, a, t) in self.accusations if t == self.seat and d == self.day]

    def votes_against(self, pid):
        return [v for (d, v, t) in self.votes if t == pid and d == self.day]

    def day_tally(self):
        tally = {}
        for (d, v, t) in self.votes:
            if d == self.day:
                tally[t] = tally.get(t, 0) + 1
        return tally

    def evidence(self, pid):
        """Concrete, citable facts about pid, for persuasive statements."""
        facts = []
        for (d, v, t) in self.votes:
            if v == pid and t in self.known_village:
                facts.append("voted for %s, who flipped village" % _name(t, self.names))
            if v == pid and t in self.known_wolf:
                facts.append("voted for %s, who flipped wolf" % _name(t, self.names))
        for (d, a, t) in self.accusations:
            if a == pid and t in self.known_village:
                facts.append("accused %s, who flipped village" % _name(t, self.names))
            if a == pid and t in self.known_wolf:
                facts.append("accused %s, who flipped wolf" % _name(t, self.names))
        for (d, df, t) in self.defenses:
            if df == pid and t in self.known_wolf:
                facts.append("defended %s, who flipped wolf" % _name(t, self.names))
        if pid in self.my_checks:
            facts.append("I checked them: %s" % self.my_checks[pid])
        return facts

    # -- decisions ---------------------------------------------------------
    def decide_vote(self):
        """Who to vote out today.

        Consolidate: a lone top suspect with no support wastes the vote
        and lets the wolves steer the lynch. If a top-3 suspect already
        has votes today, join them unless our top suspect is far ahead.
        """
        ranked = self.ranked_suspects()
        if not ranked:
            return None
        tally = self.day_tally()
        if self.role == "wolf":
            partner = self.other_wolf
            # Bus the partner only if they are clearly doomed anyway:
            # top suspect by a wide margin and already receiving votes.
            if (partner is not None and partner in self.alive
                    and ranked[0] == partner
                    and len(ranked) > 1
                    and self.suspicion(partner) - self.suspicion(ranked[1]) >= 3.0
                    and len(self.votes_against(partner)) >= 1):
                return partner
            # Otherwise vote the top villager suspect, never our partner.
            # Prefer one the village already leans toward (bandwagon).
            for pid in ranked:
                if pid != partner and tally.get(pid, 0) >= 1:
                    return pid
            for pid in ranked:
                if pid != partner:
                    return pid
            return ranked[0]
        # Village side: join a supported top-3 suspect when our leader
        # has no support and is not far ahead of the supported one.
        supported = [p for p in ranked[:3] if tally.get(p, 0) >= 1]
        if supported and tally.get(ranked[0], 0) == 0:
            if self.suspicion(ranked[0]) - self.suspicion(supported[0]) < 3.0:
                return supported[0]
        return ranked[0]

    def decide_night(self):
        """(action, target) for the night phase, by role."""
        alive_others = [p for p in sorted(self.alive) if p != self.seat]
        if not alive_others:
            return (None, None)
        if self.role == "wolf":
            # Kill the biggest threat: seer claimant, or someone who
            # suspects us / accused a wolf correctly. Spare players who are
            # pushing a villager (they help us get a mislynch).
            def threat(pid):
                s = 0.0
                if self.claims.get(pid) == "seer":
                    s += 6.0
                if pid in self.accused_me():
                    s += 2.0
                for (d, v, t) in self.votes:
                    if v == pid and t in self.known_wolf:
                        s += 3.0
                for (d, a, t) in self.accusations:
                    if a == pid and t in self.known_wolf:
                        s += 2.0
                    if a == pid and t in self.known_village:
                        s += 1.0  # vocal, leads the village
                return s
            cands = sorted(alive_others, key=lambda p: (-threat(p), p))
            rng = self._rng("kill")
            # Pick among the top 2 threats so we are not perfectly predictable.
            top = cands[:2]
            return ("kill", top[rng.randrange(len(top))])
        if self.role == "seer":
            # Check the most suspicious unknown player.
            unknown = [p for p in alive_others if p not in self.my_checks
                       and p not in self.known_wolf and p not in self.known_village]
            if not unknown:
                unknown = alive_others
            ranked = sorted(unknown, key=lambda p: (-self.suspicion(p), p))
            return ("check", ranked[0])
        if self.role == "doctor":
            # Protect a credible seer claimant first; else the most
            # threatened villager (someone who found a wolf / leads talk).
            # Never protect a known wolf; avoid protecting the same player
            # two nights running when there is an alternative.
            cands = [p for p in alive_others if p not in self.known_wolf]
            if not cands:
                cands = alive_others
            # Hard rule: cannot protect the seat protected last night.
            fresh = [p for p in cands if p != self.last_protected]
            if fresh:
                cands = fresh
            seers = [p for p in cands if self.claims.get(p) == "seer"]
            if seers:
                return ("protect", seers[0])
            def threatened(pid):
                s = 0.0
                for (d, a, t) in self.accusations:
                    if a == pid and t in self.known_wolf:
                        s += 3.0
                for (d, v, t) in self.votes:
                    if v == pid and t in self.known_wolf:
                        s += 2.0
                return s
            ranked = sorted(cands, key=lambda p: (-threatened(p), p))
            rng = self._rng("protect")
            return ("protect", ranked[0] if len(ranked) == 1 else ranked[rng.randrange(min(2, len(ranked)))])
        return (None, None)

    # -- statements --------------------------------------------------------
    def decide_statement(self):
        """One day statement, <= 500 chars. Role-aware, evidence-based."""
        rng = self._rng("say")
        me = _name(self.seat, self.names)
        suspects = self.ranked_suspects()
        top = suspects[0] if suspects else None
        accusers = self.accused_me()

        def fresh(lines):
            for line in lines:
                if line not in self._used_lines:
                    self._used_lines.add(line)
                    return line
            line = lines[rng.randrange(len(lines))]
            self._used_lines.add(line + " ")
            return line

        # 1) Seer with a wolf check: claim, but only when it matters —
        #    we found a wolf, or we are about to be voted out.
        if self.role == "seer":
            wolves_found = [p for p, t in self.my_checks.items()
                            if t == "wolf" and p in self.alive]
            if wolves_found and (self.day >= 2 or accusers or len(self.votes_against(self.seat)) >= 1):
                target = wolves_found[0]
                self.seer_revealed = True
                return _short(
                    "I am the seer. I checked %s last night and they are a wolf. "
                    "Vote %s today — this is not a guess, it is a check. Doctor, "
                    "please protect me tonight." % (_name(target, self.names),
                                                    _name(target, self.names)))
            if wolves_found and self.day == 1:
                # Day 1: steer without claiming; claiming early gets us killed.
                target = wolves_found[0]
                return _short(fresh([
                    "I am watching %s closely — their reasoning today does not add up, and their vote yesterday felt off. Let's hear them explain it." % _name(target, self.names),
                    "%s keeps steering the vote without a real reason. I think we should look there first." % _name(target, self.names),
                ]))

        # 2) Defend ourselves when accused.
        if accusers:
            acc = accusers[0]
            lines = [
                "I am not a wolf. %s is pushing me with no evidence — look at my votes, I have voted on reasoning every day, not on panic. Ask me anything." % _name(acc, self.names),
                "%s accused me but gave no reason that holds up. I have nothing to hide; check what I actually said and voted, not the accusation." % _name(acc, self.names),
                "That accusation from %s does not fit what happened. I voted with the village yesterday and I will keep doing that. Who benefits from voting me out?" % _name(acc, self.names),
            ]
            return _short(fresh(lines))

        # 3) Wolf: blend in. Echo the village consensus, suspect a villager
        #    who is already under suspicion, never lead with a wild claim.
        if self.role == "wolf":
            villager_targets = [p for p in suspects if p != self.other_wolf]
            target = villager_targets[0] if villager_targets else top
            if target is None:
                return _short("I am still listening. Nothing today convinces me yet — let's not rush a vote on thin evidence.")
            lines = [
                "I agree with the case on %s — the votes and the talk point the same way. I am voting %s." % (_name(target, self.names), _name(target, self.names)),
                "%s has been the most inconsistent today. I do not like their reasoning, and I think they are our best vote." % _name(target, self.names),
                "Listening to everyone, %s worries me most. Let's press them for a straight answer before we vote." % _name(target, self.names),
                "I am not sure yet, but %s keeps dodging the hard questions. That is where my vote is leaning." % _name(target, self.names),
            ]
            return _short(fresh(lines))

        # 4) Village / doctor / seer without a claim: accuse top suspect
        #    with a reason, or agree, or ask. Cite concrete evidence —
        #    house players are LLMs and vote on reasons, not vibes.
        if top is not None:
            ev = self.evidence(top)
            reason = (" " + ev[0][0].upper() + ev[0][1:] + ".") if ev else ""
            if self.day == 1:
                lines = [
                    "Day 1, so I am going on talk and votes only. %s is pushing hardest without a solid reason — I want to hear more from them before we vote." % _name(top, self.names),
                    "No one is proven yet. %s stands out to me today, but let's not rush — talk first, vote on what we actually hear." % _name(top, self.names),
                    "I am watching %s. Their story shifted between statements, and that is the first thing I want cleared up." % _name(top, self.names),
                ]
                return _short(fresh(lines))
            lines = [
                "My vote is %s.%s That pattern is what wolves do — they vote villagers out and defend each other." % (_name(top, self.names), reason),
                "%s is my suspect today.%s Look at their record, not just their words." % (_name(top, self.names), reason),
                "I think %s is a wolf.%s Their accusations keep landing on villagers, and they go quiet whenever the vote turns to them." % (_name(top, self.names), reason),
                "Let's focus on %s.%s If they are innocent they can explain their votes; if they cannot, we have our answer." % (_name(top, self.names), reason),
            ]
            return _short(fresh(lines))
        return _short("I am still listening. Nothing today convinces me yet — let's not rush a vote on thin evidence.")


# ---------------------------------------------------------------- env client
# Exact protocol from the competition rules (see module docstring).
# Env seats are 1-8; the Brain uses 0-7 internally (internal pid = env seat - 1).
# Only this client was rewired; the Brain above is unchanged apart from the
# doctor last-protection rule and seer-claim trust noted in its code.

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(method, url, body=None, timeout=60):
    data = None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as res:
            raw = res.read()
    except urllib.error.HTTPError as err:
        raw = err.read()
        return err.code, raw.decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return 0, ""
    return 200, raw.decode("utf-8", "replace")


def _as_json(text):
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


_ROLE_ALIASES = {"werewolf": "wolf", "wolf": "wolf", "seer": "seer",
                 "doctor": "doctor", "villager": "villager"}


def _norm_role(v):
    if isinstance(v, str):
        return _ROLE_ALIASES.get(v.strip().lower())
    return None


def _as_seat(v):
    """Env seat (1-8, int or {"seat": n}) -> internal pid (0-7), or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v - 1 if 1 <= v <= 8 else None
    if isinstance(v, dict):
        for k in ("seat", "id", "player", "target", "seat_id"):
            if k in v:
                return _as_seat(v[k])
    if isinstance(v, str) and v.isdigit():
        return _as_seat(int(v))
    return None


def _as_env_seat(pid):
    return pid + 1 if isinstance(pid, int) and 0 <= pid <= 7 else None


def _get_field(d, *names):
    for n in names:
        if isinstance(d, dict) and n in d:
            return d[n]
    return None


def _mark_dead(brain, pid, cause=""):
    """Record a death once (guards against re-processing the same view)."""
    if pid is None or pid not in brain.alive:
        return
    brain.note_death(pid, cause)


class EnvClient:
    def __init__(self, env_url, seed):
        self.base = env_url.rstrip("/")
        self.seed = str(seed)
        self.brains = {}   # game number (int) -> Brain
        self._fails = {}   # (game, decision, day, phase) -> consecutive failures

    # -- views -------------------------------------------------------------
    def get_game_view(self, game):
        status, text = _http("POST", self.base + "/game",
                             body={"game": game}, timeout=30)
        if status == 200 and text.strip():
            return _as_json(text)
        return {}

    def brain_for(self, game, view):
        brain = self.brains.get(game)
        if brain is None:
            brain = Brain(self.seed, 0, None, {}, game_id=str(game))
            self.brains[game] = brain
        self.sync_view(brain, view)
        return brain

    def sync_view(self, brain, view):
        """Sync Brain state from one game view. Views are cumulative, so the
        public log is consumed once, in order, via a per-game cursor."""
        if not isinstance(view, dict):
            return
        day = view.get("day")
        if isinstance(day, int):
            brain.day = day
        # Our seat / role / partner. Official view shape (kit RULES.txt /
        # prompt.txt, program/env/server.mjs viewOf): a "you" object
        # {"seat","name","role","alive","partner":{"seat","name","alive"}|null}.
        # Legacy top-level "seat"/"role"/"partner*" kept as fallback only.
        you = view.get("you") if isinstance(view.get("you"), dict) else {}
        seat = _as_seat(you.get("seat"))
        if seat is None:
            seat = _as_seat(view.get("seat"))
        if seat is not None:
            brain.seat = seat
        role = _norm_role(you.get("role"))
        if role is None:
            role = _norm_role(view.get("role"))
        partner = None
        pyou = you.get("partner")
        if isinstance(pyou, dict):
            partner = _as_seat(pyou.get("seat"))
        if partner is None:
            for key in ("partner", "werewolf_partner", "partner_seat"):
                if key in view:
                    partner = _as_seat(view.get(key))
                    if partner is not None:
                        break
        if partner is None and isinstance(view.get("partner_name"), str):
            pass  # name lookup happens after names are built below
        if role is not None:
            brain.set_role(role, other_wolf=partner)
        # Players: names, alive set, revealed roles.
        players = view.get("players")
        names = {}
        if isinstance(players, list):
            for p in players:
                if not isinstance(p, dict):
                    continue
                pid = _as_seat(p.get("seat"))
                if pid is None:
                    continue
                if isinstance(p.get("name"), str):
                    names[pid] = p["name"]
                prole = _norm_role(p.get("role"))
                if prole == "wolf":
                    brain.known_wolf.add(pid)
                elif prole is not None:
                    brain.known_village.add(pid)
                alive = p.get("alive")
                if alive is False:
                    _mark_dead(brain, pid, "vote" if prole else "")
            if names:
                brain.names.update(names)
        if role == "wolf" and partner is None and isinstance(view.get("partner_name"), str):
            for pid, nm in brain.names.items():
                if nm == view["partner_name"]:
                    brain.set_role("wolf", other_wolf=pid)
                    break
        # Private record: seer inspections, doctor last protection,
        # werewolves' night votes.
        priv = view.get("private")
        if isinstance(priv, dict):
            insp = priv.get("inspections", priv.get("checks"))
            if isinstance(insp, list):
                for ent in insp:
                    if not isinstance(ent, dict):
                        continue
                    t = _as_seat(ent.get("target", ent.get("seat", ent.get("player"))))
                    team = ent.get("team", ent.get("result"))
                    if t is not None and isinstance(team, str):
                        brain.note_seer_check(t, "wolf" if "wolf" in team.lower() else "village")
                    elif t is not None and isinstance(ent.get("is_wolf"), bool):
                        brain.note_seer_check(t, "wolf" if ent["is_wolf"] else "village")
            elif isinstance(insp, dict):
                for k, team in insp.items():
                    t = _as_seat(k)
                    if t is not None and isinstance(team, str):
                        brain.note_seer_check(t, "wolf" if "wolf" in team.lower() else "village")
            lastp = _get_field(priv, "last_protection", "last_protected",
                               "protected_last_night")
            lp = _as_seat(lastp)
            if lp is not None:
                brain.last_protected = lp
            votes = _get_field(priv, "night_votes", "wolf_votes", "werewolf_votes",
                               "partner_vote")
            partner_vote = None
            if isinstance(votes, dict):
                for k, v in votes.items():
                    kp = _as_seat(k)
                    vp = _as_seat(v)
                    if kp is not None and kp != brain.seat and vp is not None:
                        partner_vote = vp
                    elif kp is None and vp is not None:
                        partner_vote = vp
            elif isinstance(votes, list):
                for ent in votes:
                    if isinstance(ent, dict):
                        vp = _as_seat(ent.get("target", ent.get("vote")))
                        vs = _as_seat(ent.get("seat", ent.get("voter")))
                        if vp is not None and vs != brain.seat:
                            partner_vote = vp
            elif votes is not None:
                partner_vote = _as_seat(votes)
            brain.partner_night_vote = partner_vote
        # Official private record (viewOf): "seerResults" [{"night","seat",
        # "werewolf"}], "lastProtect" {"night","seat"}, "wolfVotes"
        # [{"night","seat","target"}]. These are top-level, not "private".
        sr = view.get("seerResults")
        if isinstance(sr, list):
            for ent in sr:
                if not isinstance(ent, dict):
                    continue
                t = _as_seat(ent.get("seat", ent.get("target")))
                w = ent.get("werewolf")
                if t is not None and isinstance(w, bool):
                    brain.note_seer_check(t, "wolf" if w else "village")
        lp2 = view.get("lastProtect")
        if isinstance(lp2, dict):
            s2 = _as_seat(lp2.get("seat"))
            if s2 is not None:
                brain.last_protected = s2
        wv = view.get("wolfVotes")
        if isinstance(wv, list):
            pv = None
            for ent in wv:
                if not isinstance(ent, dict):
                    continue
                vs = _as_seat(ent.get("seat", ent.get("voter")))
                vt = _as_seat(ent.get("target"))
                if vt is not None and vs != brain.seat:
                    pv = vt  # latest partner vote wins
            if pv is not None:
                brain.partner_night_vote = pv
        # Public log, in order, consumed once.
        log = view.get("log")
        if isinstance(log, list):
            cursor = getattr(brain, "_log_cursor", 0)
            for idx in range(cursor, len(log)):
                self._apply_log_entry(brain, log[idx])
            brain._log_cursor = len(log)

    def _apply_log_entry(self, brain, ent):
        if isinstance(ent, str):
            return
        if not isinstance(ent, dict):
            return
        kind = str(ent.get("kind") or ent.get("type") or ent.get("event") or "").lower()
        speaker = _as_seat(ent.get("speaker", ent.get("seat", ent.get("player"))))
        voter = _as_seat(ent.get("voter"))
        target = _as_seat(ent.get("target"))
        text = ent.get("text", ent.get("message", ent.get("statement", "")))
        if kind in ("statement", "speech", "speak", "say", "talk", "debate"):
            if speaker is not None and isinstance(text, str):
                brain.note_statement(speaker, text)
        elif kind in ("vote", "voted"):
            # Our own vote is already recorded in handle(); skip it here
            # so it is not counted twice in suspicion and the day tally.
            if voter is not None and target is not None and voter != brain.seat:
                brain.note_vote(voter, target)
        elif kind in ("exile", "exiled", "lynch", "lynched"):
            _mark_dead(brain, target if target is not None else speaker, "vote")
        elif kind in ("death", "died", "night_death", "killed", "murder"):
            _mark_dead(brain, target if target is not None else speaker, "night")
        elif kind in ("dawn",):
            died = _as_seat(ent.get("died", ent.get("dead", ent.get("player"))))
            if died is not None:
                _mark_dead(brain, died, "night")
            if isinstance(text, str) and text:
                pass
        elif kind in ("reveal", "revealed"):
            pid = target if target is not None else speaker
            role = _norm_role(ent.get("role"))
            if pid is not None and role == "wolf":
                brain.known_wolf.add(pid)
            elif pid is not None and role is not None:
                brain.known_village.add(pid)
        # "pass" entries carry no information; ignore.

    # -- decisions ---------------------------------------------------------
    def _valid_target(self, brain, pid, options):
        """Map an internal pid to an env seat, honouring the decision's
        options list when present. Returns env seat or None (abstain)."""
        if pid is None:
            return None
        seat = _as_env_seat(pid)
        if seat is None:
            return None
        if options:
            opt_seats = set()
            for o in options:
                s = _as_seat(o)
                if s is not None:
                    opt_seats.add(s + 1)
            if opt_seats and seat not in opt_seats:
                return None
        return seat

    def handle(self, item):
        """Answer one pending decision. Returns True on 200."""
        game = item.get("game")
        if not isinstance(game, int):
            return False
        decision = str(item.get("decision") or "").lower()
        view = item.get("view") if isinstance(item.get("view"), dict) else None
        if view is None:
            view = self.get_game_view(game)
        brain = self.brain_for(game, view)
        options = item.get("options") if isinstance(item.get("options"), list) else []
        fkey = (game, decision, brain.day, str(view.get("phase", "")))
        fails = self._fails.get(fkey, 0)

        body = {"game": game, "decision": decision}
        if decision == "speak":
            txt = _short(brain.decide_statement())
            if not txt:
                body["pass"] = True
            else:
                body["text"] = txt
        elif decision == "vote":
            target = brain.decide_vote()
            seat = self._valid_target(brain, target, options)
            if seat is None and options:
                # Fall back to the most suspicious valid option.
                ranked = brain.ranked_suspects()
                for pid in ranked:
                    seat = self._valid_target(brain, pid, options)
                    if seat is not None:
                        target = pid
                        break
            body["target"] = seat  # None = abstain
            if target is not None:
                brain.note_vote(brain.seat, target)
        elif decision == "kill":
            # Wolf coordination (hard rule): if our partner already voted,
            # match their pick — disagreeing kills nobody.
            partner_vote = getattr(brain, "partner_night_vote", None)
            target = None
            if (partner_vote is not None and partner_vote in brain.alive
                    and partner_vote not in brain.known_wolf):
                target = partner_vote
            else:
                _act, target = brain.decide_night()
            seat = self._valid_target(brain, target, options)
            body["target"] = seat  # None = abstain / nobody dies
        elif decision == "protect":
            _act, target = brain.decide_night()
            seat = self._valid_target(brain, target, options)
            body["target"] = seat  # None = protect nobody
            if target is not None and seat is not None:
                brain.last_protected = target
        elif decision == "inspect":
            _act, target = brain.decide_night()
            seat = self._valid_target(brain, target, options)
            body["target"] = seat  # None = inspect nobody
        else:
            # Unknown decision name: fall back to speaking.
            txt = _short(brain.decide_statement())
            if txt:
                body["text"] = txt
            else:
                body["pass"] = True

        status, _ = _http("POST", self.base + "/act", body=body, timeout=30)
        if status == 200:
            self._fails.pop(fkey, None)
            return True
        # Refused (400 bad_target/empty/too_long, 409 not_pending): try the
        # always-legal fallback once — pass / abstain — then let the loop
        # retry with a fresh view. After several failures, stop hammering
        # this one decision so the trial is not stalled by it.
        fails += 1
        self._fails[fkey] = fails
        if fails <= 2:
            fallback = {"game": game, "decision": decision}
            if decision == "speak":
                fallback["pass"] = True
            else:
                fallback["target"] = None
            status2, _ = _http("POST", self.base + "/act", body=fallback, timeout=30)
            if status2 == 200:
                self._fails.pop(fkey, None)
                return True
        return fails < 5


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "")
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "0")
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    prompt = ""
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read()
        except OSError:
            prompt = ""
    if not env_url:
        # No env: nothing to play. Exit 0 so a dry run does not fail loudly;
        # the grader always sets MAKEFUN_ENV_URL.
        sys.stderr.write("werewolf-agent: MAKEFUN_ENV_URL not set; nothing to play\n")
        return 0
    client = EnvClient(env_url, seed)
    if prompt:
        hints = sorted(set(re.findall(r"/[a-z][a-z0-9_/\\-]{2,40}", prompt)))
        if hints:
            sys.stderr.write("werewolf-agent: prompt endpoint hints: %s\n"
                             % ",".join(hints[:20]))
    # Observe loop: /observe holds up to 20s when nothing waits. Exit once
    # "done" is true. Consecutive observe errors are tolerated briefly.
    errors = 0
    for _ in range(10000):
        status, text = _http("POST", client.base + "/observe", body={},
                             timeout=35)
        if status != 200 or not text.strip():
            errors += 1
            if errors > 120:
                sys.stderr.write("werewolf-agent: observe failing, giving up\n")
                return 0
            time.sleep(1)
            continue
        errors = 0
        data = _as_json(text)
        if data.get("done"):
            return 0
        pending = data.get("pending")
        if not isinstance(pending, list) or not pending:
            continue
        for item in pending:
            if not isinstance(item, dict):
                continue
            try:
                client.handle(item)
            except Exception as exc:  # keep the trial alive on one bad item
                sys.stderr.write("werewolf-agent: handle failed: %r\n" % (exc,))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
