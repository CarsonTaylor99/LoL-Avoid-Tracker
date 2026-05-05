"""
LoL Avoid Tracker — backend.

All Riot API, persistence, and tracking logic lives here.
No UI code. This module is what app.py imports and exposes
over pywebview's JS bridge.
"""

import json
import math
import os
import sys
import threading
import time
from collections import Counter
from typing import Optional, List, Dict, Any

import requests


# ── File paths ────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    # Frozen exe: store data next to the .exe
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # Dev mode: share data with dist/ so both modes see the same JSONs
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(script_dir, "dist")
    BASE_DIR = dist_dir if os.path.isdir(dist_dir) else script_dir

CONFIG_FILE     = os.path.join(BASE_DIR, "config.json")
PLAYERS_FILE    = os.path.join(BASE_DIR, "players.json")
TEAMS_FILE      = os.path.join(BASE_DIR, "teams.json")
CHAMPIONS_FILE  = os.path.join(BASE_DIR, "champions.json")
# Append-only log of completed matches per player. Designed as a
# forward-compatible schema so future predictive features ("when is X
# likely to log on?", "who does X premade with?") can consume it
# without reshuffling stored data. See HISTORY_SCHEMA_VERSION below.
HISTORY_FILE    = os.path.join(BASE_DIR, "history.json")

HISTORY_SCHEMA_VERSION = 1
HISTORY_MAX_MATCHES_PER_PLAYER = 500   # safety cap


# ── Tunables ──────────────────────────────────────────────────────────────────
MAX_PLAYERS     = 20
ACTIVE_STATUSES = {"IN GAME", "QUEUING LIKELY", "RECENTLY DONE"}

# ── Per-status polling cadence (seconds) ──────────────────────────────────────
# Each status gets its own poll interval. The scheduler picks whichever
# player is currently overdue by the largest margin and checks them.
#
# Hierarchy (requested):
#   QUEUING LIKELY  >  IN GAME  >  PLAYED THIS HOUR  >  SAFE
#
# Rationale:
#   QUEUING LIKELY   hot — detecting "they just popped a queue" fast matters
#   IN GAME          watching so we catch the transition back to "just ended"
#   RECENTLY DONE    interpolated between QUEUING LIKELY and PLAYED THIS HOUR
#   PLAYED THIS HOUR slow — any change here is unlikely within a minute
#   CHECKING         fast once for new/resumed players so first data lands soon
#   ERROR            slow — retries rarely help, don't burn the budget
#   DISABLED         never polled
#   SAFE             slowest — nothing to react to
#
# Note: LOADING was removed — the champ-select / pre-game phase now folds
# into IN GAME (same cadence) since distinguishing them gave no extra
# actionable info and slow-polling them just delayed detection of the
# transition to "game actually running".
CADENCE_SEC = {
    "QUEUING LIKELY":    15,   # hot — detect the queue pop fast
    "IN GAME":          120,   # default; dynamically tightened after 8m (see below)
    "RECENTLY DONE":     75,
    "PLAYED THIS HOUR": 150,
    "CHECKING":          15,
    "ERROR":            240,
    "SAFE":             360,
}
DEFAULT_CADENCE_SEC = 120

# Once a live game has run this long, tighten polling so we catch the
# "game just ended" transition quickly.
IN_GAME_HOT_AFTER_SEC  = 480   # 8 minutes in
IN_GAME_HOT_CADENCE    = 20    # then poll every 20s


def cadence_for_player(player: dict) -> int:
    """Current polling cadence for this player, in seconds.

    Most statuses use the static CADENCE_SEC table. IN GAME is dynamic:
    early in the match we poll slowly (game can't end for a while); once
    past IN_GAME_HOT_AFTER_SEC, we poll fast to catch the ending.
    """
    status = player.get("status", "")
    if status == "IN GAME":
        start = player.get("live_game_start_ts")
        if start:
            elapsed = time.time() - start
            if elapsed >= IN_GAME_HOT_AFTER_SEC:
                return IN_GAME_HOT_CADENCE
    return CADENCE_SEC.get(status, DEFAULT_CADENCE_SEC)


def due_at(player: dict) -> float:
    """Unix timestamp at which this player should next be polled.

    Returns +inf for disabled players (they never poll).
    Returns 0.0 for never-checked players (they poll immediately).
    """
    if not player.get("enabled", True) or player.get("status") == "DISABLED":
        return float("inf")
    last = player.get("last_checked_ts")
    if not last:
        return 0.0
    return last + cadence_for_player(player)


# ── Queue ID → human-readable mode name ───────────────────────────────────────
QUEUE_NAMES = {
    0:    "Custom",
    400:  "Normal Draft",
    420:  "Ranked Solo",
    430:  "Normal Blind",
    440:  "Ranked Flex",
    450:  "ARAM",
    480:  "Swiftplay",
    490:  "Quickplay",
    700:  "Clash",
    720:  "ARAM Clash",
    830:  "Co-op vs AI (Intro)",
    840:  "Co-op vs AI (Beginner)",
    850:  "Co-op vs AI (Intermediate)",
    900:  "ARURF",
    1020: "One for All",
    1300: "Nexus Blitz",
    1400: "Ultimate Spellbook",
    1700: "Arena",
    1710: "Arena",
    1900: "URF",
    2400: "Special Mode",
    2000: "Tutorial",
    2010: "Tutorial",
    2020: "Tutorial",
}


def queue_name(qid) -> str:
    if qid is None:
        return ""
    try:
        qid = int(qid)
    except (TypeError, ValueError):
        return ""
    return QUEUE_NAMES.get(qid, f"Queue {qid}")


# ── Champion ID → name (Data Dragon, cached to disk) ──────────────────────────
class ChampionResolver:
    """Maps championId → champion name using Riot's public Data Dragon CDN.

    Ships no API key requirement; fetched once at startup in a background
    thread and cached to champions.json so offline launches still work.
    """
    def __init__(self):
        self._map: Dict[int, str] = {}
        # Read cached mapping directly — load_json is defined later in the
        # module, and this class is instantiated at import-time.
        try:
            with open(CHAMPIONS_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict):
                self._map = {int(k): v for k, v in cached.items()}
        except (FileNotFoundError, json.JSONDecodeError,
                TypeError, ValueError):
            self._map = {}
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        try:
            vers = requests.get(
                "https://ddragon.leagueoflegends.com/api/versions.json",
                timeout=10,
            ).json()
            if not vers:
                return
            latest = vers[0]
            data = requests.get(
                f"https://ddragon.leagueoflegends.com/cdn/{latest}"
                f"/data/en_US/champion.json",
                timeout=15,
            ).json()
            new_map: Dict[int, str] = {}
            for champ in (data.get("data") or {}).values():
                try:
                    new_map[int(champ["key"])] = champ["name"]
                except (KeyError, TypeError, ValueError):
                    continue
            if new_map:
                self._map = new_map
                try:
                    with open(CHAMPIONS_FILE, "w", encoding="utf-8") as f:
                        json.dump({str(k): v for k, v in new_map.items()},
                                  f, indent=2)
                except OSError:
                    pass
        except Exception:
            # Silent fail — keep whatever we had cached.
            pass

    def name(self, cid) -> str:
        if cid is None:
            return ""
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return ""
        return self._map.get(cid) or f"Champion #{cid}"


# Module-global so both check_player paths can share the cache.
_champions = ChampionResolver()


def extract_participants(live: dict, my_puuid: str) -> List[dict]:
    """Flatten a Spectator-V5 response to a simple per-participant list.

    Returns entries with fields the UI can render directly; unknown fields
    are tolerated so a Riot API change can't crash the tracker.
    """
    out: List[dict] = []
    for p in live.get("participants", []) or []:
        # Prefer Riot ID (new API) over legacy summonerName
        riot_name = (p.get("riotId")
                     or f"{p.get('gameName','')}#{p.get('tagLine','')}".strip("#")
                     or p.get("summonerName")
                     or "")
        out.append({
            "puuid":        p.get("puuid") or "",
            "team_id":      p.get("teamId"),
            "champion_id":  p.get("championId"),
            "champion":     _champions.name(p.get("championId")),
            "riot_id":      riot_name,
            "bot":          bool(p.get("bot")),
            "is_target":    bool(my_puuid) and p.get("puuid") == my_puuid,
        })
    return out


def my_team_id(participants: List[dict], my_puuid: str):
    for p in participants:
        if p.get("puuid") == my_puuid:
            return p.get("team_id")
    return None


def calc_refresh(n_enabled: int) -> int:
    """
    Baseline cycle length. Leaves headroom for the QUEUING LIKELY
    hot-poll tier so Riot's 100 req / 120s spectator budget holds.

    Budget per 120s spectator window:
        baseline          80  (N players × 120/(1.5N))
        hot (queuing)    ≤15  (capped by 8 * K_q self-scaling interval)
        ─────────────────
        total           ≤95 / 120s  (Riot cap = 100)

    Floor at 30s for small N.
    """
    if n_enabled <= 0:
        return 30
    return max(30, math.ceil(n_enabled * 1.5))


# ── Platform routing ──────────────────────────────────────────────────────────
PLATFORM_TO_REGIONAL = {
    "NA1":  "americas", "BR1":  "americas", "LA1":  "americas", "LA2": "americas",
    "EUW1": "europe",   "EUN1": "europe",   "TR1":  "europe",   "RU":  "europe",
    "KR":   "asia",     "JP1":  "asia",
    "OC1":  "sea",      "SG2":  "sea",      "TH2":  "sea",
    "TW2":  "sea",      "VN2":  "sea",      "PH2":  "sea",
}
ALL_PLATFORMS = sorted(PLATFORM_TO_REGIONAL.keys())


# ── JSON helpers ──────────────────────────────────────────────────────────────
def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Riot API wrapper ──────────────────────────────────────────────────────────
class RiotAPI:
    def __init__(self, api_key: str, platform: str):
        self.platform = platform.upper()
        self.regional = PLATFORM_TO_REGIONAL.get(self.platform, "americas")
        self._hdrs = {"X-Riot-Token": api_key}

    def _get(self, url: str, **params):
        r = requests.get(url, headers=self._hdrs,
                         params=params or None, timeout=10)
        r.raise_for_status()
        return r.json()

    def account(self, game_name: str, tag: str):
        return self._get(
            f"https://{self.regional}.api.riotgames.com"
            f"/riot/account/v1/accounts/by-riot-id"
            f"/{requests.utils.quote(game_name)}/{requests.utils.quote(tag)}"
        )

    def recent_match_ids(self, puuid: str, start_time: int, count: int = 5):
        return self._get(
            f"https://{self.regional}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
            count=count, startTime=start_time,
        )

    def match_details(self, match_id: str):
        return self._get(
            f"https://{self.regional}.api.riotgames.com"
            f"/lol/match/v5/matches/{match_id}"
        )

    def active_game(self, puuid: str):
        """Return the current live game for a PUUID, or None if not in game.

        Endpoint requires a production / standard API key (or RSO). Dev keys
        will receive 401/403 here; we surface that as 'None' to fall through
        to match-history detection.
        """
        try:
            return self._get(
                f"https://{self.platform.lower()}.api.riotgames.com"
                f"/lol/spectator/v5/active-games/by-summoner/{puuid}"
            )
        except requests.HTTPError as exc:
            # 404 = not in a game (expected, hot path). 401/403 = no access.
            if exc.response.status_code == 404:
                return None
            raise


# ── Spectator-only fast check (used by hot-poll loop) ────────────────────────
def check_player_live_only(api: RiotAPI, player: dict) -> Optional[dict]:
    """Spectator-only fast check for QUEUING LIKELY players.

    Returns an updated player dict only if the player has entered champ
    select / a live game; returns None otherwise so the caller can leave
    the existing QUEUING LIKELY state untouched (and cheap).

    This spends ONLY spectator-v5 budget — no match-v5 calls — so it can
    safely run at a higher cadence than the full refresh cycle.
    """
    riot_id = player.get("riot_id", "")
    if "#" not in riot_id or not player.get("puuid"):
        return None
    try:
        live = api.active_game(player["puuid"])
    except requests.HTTPError as exc:
        # 401/403 = no spectator access; 429/5xx = transient — in all
        # cases just bail silently; the baseline cycle will recover.
        return None
    except Exception:
        return None
    if not live:
        return None

    qid       = live.get("gameQueueConfigId")
    mode      = queue_name(qid)
    length_s  = int(live.get("gameLength", 0) or 0)
    now_ts    = time.time()
    # Collapsed LOADING → IN GAME: champ-select / pre-game is still
    # "they're in a game right now" for avoid-tracking purposes.
    if length_s <= 0:
        status   = "IN GAME"
        detail   = "Loading · awaiting game start"
        start_ts = None
    else:
        status   = "IN GAME"
        detail   = f"Live · {length_s // 60}m {length_s % 60:02d}s into game"
        start_ts = now_ts - length_s
    participants = extract_participants(live, player.get("puuid") or "")
    live_game_id = str(live.get("gameId", ""))
    return {**player,
            "status": status,
            "status_detail": detail,
            "last_match_id": live_game_id,
            "last_queue_id": qid,
            "last_mode": mode,
            "live_game_start_ts": start_ts,
            "live_seen_match_id": live_game_id,
            "live_seen_ts": now_ts,
            "live_participants": participants,
            "my_team_id": my_team_id(participants, player.get("puuid") or ""),
            "last_checked_ts": now_ts}


NON_LIVE_RESET = {
    "live_participants": None,
    "my_team_id": None,
    "live_game_start_ts": None,
}


# ── Transient-error tolerance ─────────────────────────────────────────────────
# Riot's gateway returns 502/503/504 fairly routinely, and dev connections
# occasionally drop. Without tolerance, a single blip wipes out an IN GAME
# row (status → ERROR, live_participants cleared, match-id cleared) even
# though the player is almost certainly still in the game. We absorb up to
# TRANSIENT_TOLERANCE consecutive failures by preserving the existing row
# and retrying on the normal cadence; only after that threshold do we flip
# to a visible ERROR state.
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
TRANSIENT_TOLERANCE  = 3


def _transient_retry(player: dict, tries: int) -> dict:
    """Preserve the row; just bump the retry counter + last_checked_ts."""
    return {**player,
            "last_checked_ts": time.time(),
            "transient_error_count": tries}


# ══════════════════════════════════════════════════════════════════════════════
# HISTORY LOG — persistent per-match record for future predictive modeling.
#
# Schema design goals:
#   - Stable keying:  by puuid (not riot_id) so name changes don't split a
#                     player into multiple records.
#   - Free dedup:     matches keyed by match_id inside each player's dict.
#   - Forward-compat: top-level "version" sentinel + an "extras" sub-dict on
#                     each match for future fields added without migration.
#   - Analysis-ready: calendar fields pre-computed (local + UTC) so notebook
#                     work doesn't need to re-derive time-of-day / weekday
#                     patterns from raw epoch values each run.
#
# Fields captured per match:
#   match_id, queue_id, mode, outcome ("W"/"L"/"?"),
#   start_ts/end_ts (epoch seconds, UTC),
#   duration_s,
#   start_local / end_local: { iso, weekday (0=Mon), hour, minute },
#   start_utc   / end_utc  : same,
#   teammates  (puuids on the same side, excluding self),
#   opponents  (puuids on the other side),
#   observed_live (bool: did we catch them via Spectator-V5 during the game),
#   first_seen_ts (when WE first recorded this match),
#   last_seen_ts  (last time WE re-touched this record).
#
# Why these: the combination of calendar fields + mode + duration is enough
# to train a simple "probability they play during hour H on weekday W" model,
# and teammates enables premade-detection without another API roundtrip.
# ══════════════════════════════════════════════════════════════════════════════

_history_lock = threading.Lock()


def _calendar_fields(epoch_seconds: float) -> dict:
    """Return {'iso','weekday','hour','minute'} for local AND UTC."""
    if not epoch_seconds:
        return {}
    try:
        t_local = time.localtime(epoch_seconds)
        t_utc   = time.gmtime(epoch_seconds)
    except (OSError, ValueError, OverflowError):
        return {}

    def pack(t):
        return {
            "iso":     time.strftime("%Y-%m-%dT%H:%M:%S", t),
            "weekday": t.tm_wday,   # Monday=0 … Sunday=6
            "hour":    t.tm_hour,
            "minute":  t.tm_min,
        }
    return {"local": pack(t_local), "utc": pack(t_utc)}


def _split_rosters(match_info: dict, my_puuid: str):
    """Return (teammates_puuids, opponents_puuids) excluding `my_puuid`.

    Tolerant of missing fields — unknown rosters just come back empty.
    """
    parts = (match_info or {}).get("participants", []) or []
    if not parts or not my_puuid:
        return [], []
    my_team = None
    for p in parts:
        if p.get("puuid") == my_puuid:
            my_team = p.get("teamId")
            break
    if my_team is None:
        return [], []
    teammates, opponents = [], []
    for p in parts:
        pid = p.get("puuid")
        if not pid or pid == my_puuid:
            continue
        (teammates if p.get("teamId") == my_team else opponents).append(pid)
    return teammates, opponents


def record_match(puuid: str,
                 riot_id: str,
                 *,
                 match_id: str,
                 queue_id,
                 mode: str,
                 start_ts: float,
                 end_ts: float,
                 duration_s: int,
                 outcome: str = "?",
                 teammates: Optional[List[str]] = None,
                 opponents: Optional[List[str]] = None,
                 observed_live: bool = False) -> None:
    """Append / merge a completed match into history.json.

    Idempotent on (puuid, match_id) — calling repeatedly just refreshes the
    `last_seen_ts` and any newly-known fields; existing fields are
    preserved so we don't lose data on a partial re-check.
    """
    if not puuid or not match_id:
        return
    now = time.time()

    with _history_lock:
        h = load_json(HISTORY_FILE, {})
        if not isinstance(h, dict):
            h = {}
        h.setdefault("version", HISTORY_SCHEMA_VERSION)
        players = h.setdefault("players", {})

        p_entry = players.setdefault(puuid, {
            "riot_id":           riot_id or "",
            "first_recorded_ts": now,
            "matches":           {},
        })
        if riot_id:
            p_entry["riot_id"] = riot_id
        p_entry["last_updated_ts"] = now
        matches = p_entry.setdefault("matches", {})

        existing = matches.get(match_id, {})
        cal_start = _calendar_fields(start_ts)
        cal_end   = _calendar_fields(end_ts)
        merged = {
            "match_id":      match_id,
            "queue_id":      queue_id if queue_id is not None else existing.get("queue_id"),
            "mode":          mode or existing.get("mode", ""),
            "start_ts":      start_ts or existing.get("start_ts"),
            "end_ts":        end_ts   or existing.get("end_ts"),
            "duration_s":    duration_s or existing.get("duration_s"),
            "outcome":       (outcome if outcome != "?" else existing.get("outcome", "?")),
            "start_local":   cal_start.get("local") or existing.get("start_local"),
            "start_utc":     cal_start.get("utc")   or existing.get("start_utc"),
            "end_local":     cal_end.get("local")   or existing.get("end_local"),
            "end_utc":       cal_end.get("utc")     or existing.get("end_utc"),
            "teammates":     teammates if teammates is not None else existing.get("teammates", []),
            "opponents":     opponents if opponents is not None else existing.get("opponents", []),
            "observed_live": observed_live or existing.get("observed_live", False),
            "first_seen_ts": existing.get("first_seen_ts", now),
            "last_seen_ts":  now,
            # Preserve any future-added extras verbatim.
            "extras":        existing.get("extras", {}),
        }
        matches[match_id] = merged

        # Safety cap: retain only the most recent N matches by end_ts.
        if len(matches) > HISTORY_MAX_MATCHES_PER_PLAYER:
            sorted_items = sorted(
                matches.items(),
                key=lambda kv: kv[1].get("end_ts") or 0,
                reverse=True,
            )[:HISTORY_MAX_MATCHES_PER_PLAYER]
            p_entry["matches"] = dict(sorted_items)

        save_json(HISTORY_FILE, h)


def load_history() -> dict:
    """Public read-only accessor; returns a copy of the current history."""
    with _history_lock:
        return load_json(HISTORY_FILE, {
            "version": HISTORY_SCHEMA_VERSION, "players": {}
        })


# ── Single-player status check ────────────────────────────────────────────────
def check_player(api: RiotAPI, player: dict) -> dict:
    """Return a new player dict with updated status fields."""
    riot_id = player.get("riot_id", "")
    if "#" not in riot_id:
        return {**player, **NON_LIVE_RESET,
                "status": "ERROR",
                "status_detail": "Bad format (need Name#TAG)",
                "last_match_id": None,
                "last_queue_id": None,
                "last_mode": "",
                "transient_error_count": 0,
                "last_checked_ts": time.time()}

    game_name, tag = riot_id.split("#", 1)
    player = dict(player)
    # Snapshot the prior failure count so transient-error branches below
    # can increment it; success paths simply omit the field (reset to 0).
    prior_transient = int(player.get("transient_error_count") or 0)
    try:
        if not player.get("puuid"):
            player["puuid"] = api.account(game_name, tag)["puuid"]

        # ── LIVE GAME CHECK (requires standard API key) ───────────────
        try:
            live = api.active_game(player["puuid"])
        except requests.HTTPError as exc:
            # 401/403 = key lacks spectator access — skip, not fatal
            if exc.response.status_code in (401, 403):
                live = None
            else:
                raise

        if live:
            qid       = live.get("gameQueueConfigId")
            mode      = queue_name(qid)
            length_s  = int(live.get("gameLength", 0) or 0)
            now_ts    = time.time()
            # Riot's gameLength is negative/zero during champ select + loading;
            # flips positive once the game actually starts. We fold both into
            # IN GAME (LOADING as a separate status was removed — it only
            # slowed detection of the transition to "game running").
            if length_s <= 0:
                status   = "IN GAME"
                detail   = "Loading · awaiting game start"
                start_ts = None
            else:
                status   = "IN GAME"
                detail   = f"Live · {length_s // 60}m {length_s % 60:02d}s into game"
                start_ts = now_ts - length_s
            participants = extract_participants(live, player["puuid"])
            # Remember we saw this game live — consumed below when the
            # match shows up in match-v5 history so we can flag
            # observed_live=True in the persistent log.
            live_game_id = str(live.get("gameId", ""))
            return {**player,
                    "status": status,
                    "status_detail": detail,
                    "last_match_id": live_game_id,
                    "last_queue_id": qid,
                    "last_mode": mode,
                    "live_game_start_ts": start_ts,
                    "live_seen_match_id": live_game_id,
                    "live_seen_ts": now_ts,
                    "live_participants": participants,
                    "my_team_id": my_team_id(participants, player["puuid"]),
                    "transient_error_count": 0,
                    "last_checked_ts": now_ts}

        now_ts = int(time.time())
        # Riot's startTime filter is on game START time, not end time. A 50-min
        # game that ended 5 min ago started 55 min ago — fine. But that same
        # game 11 min after ending started 61 min ago and would drop out of a
        # 3600 s window, flashing the player to SAFE while still well within
        # the "played this hour" cool-down. Widen to 2 h so long games stay in
        # `ids` long enough to age through RECENTLY DONE / PLAYED THIS HOUR;
        # the mins_ago >= 60 cutoff below handles the SAFE transition by end
        # time instead.
        ids = api.recent_match_ids(player["puuid"], now_ts - 7200, count=5)

        # ── Live-sighting reconciliation + indexing-gap guard ─────────
        # Riot's match-v5 indexes completed games with a 30 s – 5 min lag
        # (worse for Special / Arena modes). That causes two bugs if we
        # naively compute status from the top match in `ids`:
        #   (a) `ids` comes back empty → we'd flash SAFE even though the
        #       player just finished a game.
        #   (b) `ids` comes back non-empty but still missing the most
        #       recent game → an OLDER match sits at ids[0], making us
        #       misreport mode & time (e.g. "PLAYED THIS HOUR · ARAM"
        #       when the real last game was a Special Mode still pending).
        #
        # We bridge both cases using `live_seen_match_id` — the Spectator
        # game-id we last caught live. It persists across restarts in
        # players.json, so the guard survives a reboot in the middle of
        # an indexing window.
        #
        # Two supporting behaviours keep this robust:
        #   - SELF-HEAL: once the live game finally appears in `ids`, we
        #     clear the tracker so a later stale reference never fires.
        #   - BOOTSTRAP: records written by older builds carry
        #     live_seen_match_id but no live_seen_ts. We start the grace
        #     clock at first observation; if the sighting was truly old
        #     the 10-min window simply expires and we move on.
        live_mid = player.get("live_seen_match_id") or ""
        live_suf = live_mid.split("_")[-1] if live_mid else ""
        id_suffixes = {str(i).split("_")[-1] for i in ids}

        if live_suf and live_suf in id_suffixes:
            # Reconciled — the live game is now in match-v5 history.
            player = {**player, "live_seen_match_id": "", "live_seen_ts": 0}
            live_mid, live_suf = "", ""

        prev_live_ts = float(player.get("live_seen_ts") or 0)
        if live_suf and not prev_live_ts:
            prev_live_ts = time.time()    # legacy-data bootstrap

        # Grace expired without reconciliation: match-v5 never indexed the
        # game and the by-id fallback (below) never landed it either. Drop
        # the live-sighting markers so subsequent ticks don't keep the row
        # pinned on "post-game" past the 10-min cap; they'll fall through
        # to the regular `ids` flow or SAFE like any other player.
        if live_suf and time.time() - prev_live_ts >= 600:
            player = {**player, "live_seen_match_id": "", "live_seen_ts": 0}
            live_mid, live_suf = "", ""

        if live_suf and time.time() - prev_live_ts < 600:
            # Attempt to enrich the detail string with the same
            # "(W, 28m) · N games this hour" format the regular path uses
            # so QUEUING LIKELY rows look identical regardless of which
            # branch produced them. The match-v5 by-id endpoint frequently
            # has the game before the matchlist endpoint catches up — a
            # direct fetch closes that indexing gap.
            mv5_id = f"{api.platform}_{live_mid}"
            try:
                info = api.match_details(mv5_id)["info"]
                end_ms = info.get("gameEndTimestamp") or \
                         (info["gameCreation"] + info["gameDuration"] * 1000)
                outcome = next(
                    ("W" if p["win"] else "L")
                    for p in info.get("participants", [])
                    if p.get("puuid") == player["puuid"]
                ) if info.get("participants") else "?"
                dur_mins = info.get("gameDuration", 0) // 60
                qid      = info.get("queueId")
                mode     = queue_name(qid)
                mins_ago = max(0, int((time.time() - end_ms / 1000) / 60))
                n_total  = max(1, len(ids))
                time_str  = "Ended <1m ago" if mins_ago < 1 else f"Ended {mins_ago}m ago"
                games_str = f"{n_total} game{'s' if n_total != 1 else ''} this hour"
                detail    = f"{time_str}  ({outcome}, {dur_mins}m)  ·  {games_str}"
                return {**player, **NON_LIVE_RESET,
                        "status": "QUEUING LIKELY",
                        "status_detail": detail,
                        "last_match_id": mv5_id,
                        "last_queue_id": qid,
                        "last_mode": mode,
                        "last_match_end_ms": end_ms,
                        "last_match_outcome": outcome,
                        "last_match_dur_mins": dur_mins,
                        "live_seen_match_id": live_mid,
                        "live_seen_ts": prev_live_ts,
                        "transient_error_count": 0,
                        "last_checked_ts": time.time()}
            except (requests.HTTPError, requests.ConnectionError,
                    requests.Timeout, StopIteration, KeyError):
                # Match-v5 by-id also doesn't have it yet (or we couldn't
                # reach it). Fall back to the bare "post-game" detail —
                # next tick will retry.
                pass

            mins_ago = max(0, int((time.time() - prev_live_ts) / 60))
            time_str = "Ended <1m ago" if mins_ago < 1 else f"Ended {mins_ago}m ago"
            return {**player, **NON_LIVE_RESET,
                    "status": "QUEUING LIKELY",
                    "status_detail": f"{time_str}  ·  post-game",
                    "live_seen_match_id": live_mid,
                    "live_seen_ts": prev_live_ts,
                    "transient_error_count": 0,
                    "last_checked_ts": time.time()}

        if not ids:
            return {**player, **NON_LIVE_RESET,
                    "status": "SAFE",
                    "status_detail": "No games in the last hour",
                    "last_match_id": None,
                    "last_queue_id": None,
                    "last_mode": "",
                    "transient_error_count": 0,
                    "last_checked_ts": time.time()}

        # ── Conditional match-details fetch ───────────────────────────
        # If the top match ID hasn't changed since last check, reuse the
        # stored end-time / outcome / duration / queue — saves a call
        # (roughly half of all steady-state traffic).
        reused = (ids[0] == player.get("last_match_id")
                  and player.get("last_match_end_ms"))
        if reused:
            end_ms   = player["last_match_end_ms"]
            outcome  = player.get("last_match_outcome", "?")
            dur_mins = player.get("last_match_dur_mins", 0)
            qid      = player.get("last_queue_id")
            mode     = player.get("last_mode") or queue_name(qid)
            info     = None
        else:
            info = api.match_details(ids[0])["info"]
            end_ms = info.get("gameEndTimestamp") or \
                     (info["gameCreation"] + info["gameDuration"] * 1000)
            outcome = next(
                ("W" if p["win"] else "L")
                for p in info.get("participants", [])
                if p.get("puuid") == player["puuid"]
            ) if info.get("participants") else "?"
            dur_mins = info.get("gameDuration", 0) // 60
            qid      = info.get("queueId")
            mode     = queue_name(qid)

        # ── Persist to history.json ───────────────────────────────────
        # We only record on the FIRST time we see a completed match
        # (i.e. the non-reused path when ids[0] is new, OR when reused
        # is True and we simply refresh last_seen_ts to keep the entry
        # warm). Idempotent on match_id — safe to call repeatedly.
        try:
            dur_s = (info.get("gameDuration") if info
                     else dur_mins * 60) or 0
            start_epoch = (end_ms / 1000.0) - dur_s if end_ms else 0
            teammates, opponents = ([], [])
            if info:
                teammates, opponents = _split_rosters(info, player["puuid"])
            # observed_live only credits a match we actually caught live
            # (via Spectator-V5) during this session — compared by
            # numeric suffix since match-v5 ids carry a platform prefix.
            seen_live_id = player.get("live_seen_match_id") or ""
            def _same_suffix(a: str, b: str) -> bool:
                if not a or not b:
                    return False
                return a.split("_")[-1] == b.split("_")[-1]
            was_live = _same_suffix(ids[0], seen_live_id)
            record_match(
                puuid=player["puuid"],
                riot_id=player.get("riot_id", ""),
                match_id=ids[0],
                queue_id=qid,
                mode=mode,
                start_ts=start_epoch,
                end_ts=(end_ms / 1000.0) if end_ms else 0,
                duration_s=int(dur_s),
                outcome=outcome,
                teammates=teammates if info else None,   # preserve prior value on reused path
                opponents=opponents if info else None,
                observed_live=was_live,
            )
        except Exception:
            # Logging is best-effort — never let it break status checks.
            pass

        mins_ago = max(0, int((now_ts - end_ms / 1000) / 60))

        # SAFE by end time. The 7200 s API window above intentionally
        # over-fetches so long games stay visible while they age through
        # RECENTLY DONE / PLAYED THIS HOUR. The flip side: matches whose
        # END is already past the 60-min cool-down still appear in `ids`
        # and would otherwise be mislabelled as PLAYED THIS HOUR. Cut
        # them off here so the player goes cleanly to SAFE.
        if mins_ago >= 60:
            return {**player, **NON_LIVE_RESET,
                    "status": "SAFE",
                    "status_detail": "No games in the last hour",
                    "last_match_id": None,
                    "last_queue_id": None,
                    "last_mode": "",
                    "transient_error_count": 0,
                    "last_checked_ts": time.time()}

        n_total  = len(ids)
        time_str  = "Ended <1m ago" if mins_ago < 1 else f"Ended {mins_ago}m ago"
        games_str = f"{n_total} game{'s' if n_total != 1 else ''} this hour"
        detail    = f"{time_str}  ({outcome}, {dur_mins}m)  ·  {games_str}"

        # QUEUING LIKELY classification.
        #
        # We widen the window beyond a simple "finished <8m ago" because:
        #   (a) Spectator-V5 can fail silently on niche queues (Special
        #       Mode, Arena variants, regional outages), so the very first
        #       time we SEE a finished match might be several minutes
        #       past the true end — the game we never caught live.
        #   (b) Special / event modes tend to be shorter, so folks are
        #       much more likely to re-queue right after.
        #
        # Strategy:
        #   - baseline window: <10 min since game ended → QUEUING LIKELY
        #   - if this match id is newly observed (i.e. we've never
        #     recorded it before, which is almost always a "fresh finish"
        #     signal rather than stale data), stretch the window to 15 min
        #     so we don't miss a re-queue right after the Post-Game lobby.
        prev_match_id = player.get("last_match_id") or ""
        # Match-V5 ids look like "NA1_4812345678"; Spectator ids are bare
        # numeric — do a suffix-based comparison so a live→ended transition
        # is correctly recognised as "same match we were just watching".
        def _same_match(a: str, b: str) -> bool:
            if not a or not b:
                return False
            if a == b:
                return True
            return a.split("_")[-1] == b.split("_")[-1]
        is_new_match = not _same_match(ids[0], prev_match_id)
        ql_window = 15 if is_new_match else 10

        if mins_ago < ql_window:
            status = "QUEUING LIKELY"
        elif mins_ago < 25:
            status = "RECENTLY DONE"
        else:
            status = "PLAYED THIS HOUR"

        return {**player, **NON_LIVE_RESET,
                "status": status,
                "status_detail": detail,
                "last_match_id": ids[0],
                "last_queue_id": qid,
                "last_mode": mode,
                "last_match_end_ms": end_ms,
                "last_match_outcome": outcome,
                "last_match_dur_mins": dur_mins,
                "transient_error_count": 0,
                "last_checked_ts": time.time()}

    except StopIteration:
        return {**player, **NON_LIVE_RESET,
                "status": "ERROR",
                "status_detail": "Participant not found in match",
                "last_match_id": None,
                "last_queue_id": None,
                "last_mode": "",
                "transient_error_count": 0,
                "last_checked_ts": time.time()}
    except requests.HTTPError as exc:
        code = exc.response.status_code
        # Transient server / rate-limit errors: preserve the row so a
        # single 502 from Riot's gateway doesn't wipe out an IN GAME
        # state (the player is almost certainly still in the game).
        # Only surface ERROR after TRANSIENT_TOLERANCE consecutive failures.
        if code in TRANSIENT_HTTP_CODES:
            tries = prior_transient + 1
            if tries < TRANSIENT_TOLERANCE:
                return _transient_retry(player, tries)
        msg = {429: "Rate limited",
               403: "API key invalid or expired",
               404: "Summoner not found"}.get(code, f"HTTP {code}")
        return {**player, **NON_LIVE_RESET,
                "status": "ERROR",
                "status_detail": msg,
                "last_match_id": None,
                "last_queue_id": None,
                "last_mode": "",
                "transient_error_count": 0,
                "last_checked_ts": time.time()}
    except (requests.ConnectionError, requests.Timeout):
        # Same reasoning as transient HTTP codes — the network blinked,
        # keep whatever the row was showing and retry next tick.
        tries = prior_transient + 1
        if tries < TRANSIENT_TOLERANCE:
            return _transient_retry(player, tries)
        return {**player, **NON_LIVE_RESET,
                "status": "ERROR",
                "status_detail": "Network error",
                "last_match_id": None,
                "last_queue_id": None,
                "last_mode": "",
                "transient_error_count": 0,
                "last_checked_ts": time.time()}
    except Exception as exc:
        return {**player, **NON_LIVE_RESET,
                "status": "ERROR",
                "status_detail": str(exc)[:80],
                "last_match_id": None,
                "last_queue_id": None,
                "last_mode": "",
                "transient_error_count": 0,
                "last_checked_ts": time.time()}


# ── Team status logic ─────────────────────────────────────────────────────────
#
# Priority ladder (highest severity first):
#   TEAM IN GAME   — ≥2 members live, ideally in the same game right now
#   TEAM QUEUING   — ≥2 members QUEUING LIKELY together (shared prior match)
#                    OR ≥3 members active (mixed), indicating a re-queue
#   TEAM PARTIAL   — exactly 1 active member
#   TEAM RECENT    — ≥2 members PLAYED THIS HOUR (warm heads-up)
#   TEAM SAFE      — nothing's happening
#
# Match-id suffix comparison is used throughout because live gameIds from
# Spectator-V5 are bare numeric strings while match-v5 ids carry a platform
# prefix (e.g. "NA1_4812345678"). Matching on the trailing numeric segment
# recognises that both refer to the same game.
def _match_suffix(mid) -> str:
    if not mid:
        return ""
    return str(mid).split("_")[-1]


def _cluster_top(players_group: List[dict]):
    """Return (cluster_size, suffix) — how many of `players_group` share
    the most-common last_match_id (suffix-compared). Empty → (0, "")."""
    suffixes = [_match_suffix(p.get("last_match_id")) for p in players_group]
    suffixes = [s for s in suffixes if s]
    if not suffixes:
        return 0, ""
    suf, count = Counter(suffixes).most_common(1)[0]
    return count, suf


def _dominant_mode(group: List[dict]) -> str:
    """Most common non-empty last_mode across a group of players."""
    modes = [p.get("last_mode", "") for p in group if p.get("last_mode")]
    if not modes:
        return ""
    return Counter(modes).most_common(1)[0][0]


def compute_team_status(team: dict, players: List[dict]) -> (str, str, str):
    """Return (status, detail, mode).

    `mode` is surfaced separately so the UI's Mode column can render it
    alongside the folder, matching how individual player rows already
    split detail text from the mode chip. Embedding it in the detail
    string duplicated information and made the Mode column inconsistent.
    """
    player_map = {p["riot_id"]: p for p in players}
    members = [player_map[r] for r in team["members"]
               if r in player_map and player_map[r].get("enabled", True)]
    n_total = len(members)
    if not members:
        return "TEAM SAFE", "No members tracked", ""

    in_game    = [p for p in members if p.get("status") == "IN GAME"]
    queuing    = [p for p in members if p.get("status") == "QUEUING LIKELY"]
    recent     = [p for p in members if p.get("status") == "RECENTLY DONE"]
    this_hour  = [p for p in members if p.get("status") == "PLAYED THIS HOUR"]
    active     = in_game + queuing + recent
    n_active   = len(active) + len(this_hour)   # any non-SAFE member

    # Shared-match cluster sizes — the premade signal. A folder elevates
    # into an active tier ONLY when ≥3 members cluster on the same game.
    n_same_live,     _ = _cluster_top(in_game)
    n_shared_queuing, _ = _cluster_top(queuing)
    n_shared_recent,  _ = _cluster_top(recent)

    # Mode for each tier is derived from the cluster members.
    def _cluster_mode(group: List[dict], suf_count_group: List[dict]) -> str:
        """Best-effort mode for a tier: first match the shared suffix, else dominant."""
        _, top_suf = _cluster_top(suf_count_group)
        hit = next(
            (p.get("last_mode") for p in group
             if _match_suffix(p.get("last_match_id")) == top_suf),
            "",
        )
        return hit or _dominant_mode(group)

    # ── Team severity ladder ─────────────────────────────────────────────
    # Rule: a bucket elevates the folder only when ≥3 members cluster on
    # the same match. Fewer than 3 shared (or a lone live/queuing member)
    # falls through to TEAM PARTIAL so the pill doesn't overstate.

    # TEAM IN GAME — 3+ members currently live in the SAME match.
    if n_same_live >= 5:
        return ("TEAM IN GAME",
                f"Full 5-stack live together  ·  {n_active}/{n_total} active",
                _cluster_mode(in_game, in_game))
    if n_same_live >= 3:
        return ("TEAM IN GAME",
                f"{n_same_live}-stack live in same game"
                f"  ·  {n_active}/{n_total} active",
                _cluster_mode(in_game, in_game))

    # TEAM QUEUING — 3+ members QUEUING LIKELY sharing a just-finished match.
    if n_shared_queuing >= 3:
        return ("TEAM QUEUING",
                f"{n_shared_queuing}-stack just finished  ·  likely re-queuing"
                f"  ·  {n_active}/{n_total} active",
                _cluster_mode(queuing, queuing))

    # TEAM RECENT — 3+ members RECENTLY DONE sharing the same match.
    if n_shared_recent >= 3:
        return ("TEAM RECENT",
                f"{n_shared_recent}-stack finished same game recently"
                f"  ·  {n_active}/{n_total} active",
                _cluster_mode(recent, recent))

    # TEAM RECENT — 3+ members in the PLAYED THIS HOUR cool-down bucket.
    # No shared-match requirement here: spread across 25–60 min, the fact
    # of repeated play is itself the signal.
    if len(this_hour) >= 3:
        return ("TEAM RECENT",
                f"{len(this_hour)}/{n_total} played this hour",
                _dominant_mode(this_hour))

    # TEAM PARTIAL — some activity, but nothing clusters at 3+. Used for
    # lone-wolf live members, duos, scattered queuing, etc. Keeps the
    # folder visible without implying a premade signal that isn't there.
    if n_active >= 1:
        # Pick the most severe individual status present for mode hint.
        if in_game:
            mode = _dominant_mode(in_game)
        elif queuing:
            mode = _dominant_mode(queuing)
        elif recent:
            mode = _dominant_mode(recent)
        else:
            mode = _dominant_mode(this_hour)
        return ("TEAM PARTIAL",
                f"{n_active}/{n_total} active  (need 3+ in same game to flag)",
                mode)

    # ── Default: nobody active ───────────────────────────────────────────
    return ("TEAM SAFE",
            f"All clear  ·  {n_total} member{'s' if n_total != 1 else ''}",
            "")


# ── Tracker — single in-memory source of truth ────────────────────────────────
class Tracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.players: List[dict] = load_json(PLAYERS_FILE, [])
        self.teams:   List[dict] = load_json(TEAMS_FILE,   [])
        self.config:  dict = load_json(CONFIG_FILE, {})
        self.api: Optional[RiotAPI] = None
        self.next_check_ts: float = 0.0
        self._stop_evt = threading.Event()

        if self.config.get("api_key") and self.config.get("platform"):
            self.api = RiotAPI(self.config["api_key"], self.config["platform"])

    # ── Public state snapshot (called from JS) ────────────────────────────
    def get_state(self) -> dict:
        now_snapshot = time.time()
        with self._lock:
            players = [dict(p) for p in self.players]
        # Attach per-player scheduling info so the UI can render an individual
        # "next check" countdown per row.
        for p in players:
            d = due_at(p)
            p["next_check_sec"] = (-1 if d == float("inf")
                                   else max(0, int(round(d - now_snapshot))))
            p["cadence_sec"] = cadence_for_player(p)
        with self._lock:
            teams = []
            for t in self.teams:
                ts, td, tmode = compute_team_status(t, players)
                teams.append({
                    "name": t["name"],
                    "members": list(t["members"]),
                    "status": ts,
                    "status_detail": td,
                    "last_mode": tmode,
                })

        enabled_players = [p for p in players if p.get("enabled", True)]
        active = sum(1 for p in enabled_players if p.get("status") in ACTIVE_STATUSES)
        safe   = sum(1 for p in enabled_players if p.get("status") == "SAFE")
        now    = time.time()
        countdown = max(0, int(self.next_check_ts - now)) if self.next_check_ts else 0

        return {
            "configured": bool(self.api),
            "platform": self.config.get("platform", ""),
            "api_key_set": bool(self.config.get("api_key")),
            "players": players,
            "teams": teams,
            "max_players": MAX_PLAYERS,
            "refresh_sec": min(CADENCE_SEC.values()),
            "countdown": countdown,
            "active_count": active,
            "safe_count": safe,
            "unchecked_count": len(players) - active - safe,
        }

    # ── Config ────────────────────────────────────────────────────────────
    def set_config(self, api_key: str, platform: str) -> dict:
        api_key = (api_key or "").strip()
        if not api_key:
            return {"ok": False, "error": "API key is required"}
        if platform not in PLATFORM_TO_REGIONAL:
            return {"ok": False, "error": f"Unknown platform: {platform}"}
        with self._lock:
            self.config = {"api_key": api_key, "platform": platform}
            save_json(CONFIG_FILE, self.config)
            self.api = RiotAPI(api_key, platform)
            # Force all puuids to re-resolve in case region changed
            for p in self.players:
                p["puuid"] = None
        self.refresh_now()
        return {"ok": True}

    # ── Players ───────────────────────────────────────────────────────────
    def add_player(self, riot_id: str, team_name: Optional[str] = None) -> dict:
        riot_id = (riot_id or "").strip()
        if not self.api:
            return {"ok": False, "error": "Configure API key first"}
        if "#" not in riot_id:
            return {"ok": False, "error": "Must be Name#TAG"}
        with self._lock:
            if len(self.players) >= MAX_PLAYERS:
                return {"ok": False, "error": f"Max {MAX_PLAYERS} players"}
            if any(p["riot_id"].lower() == riot_id.lower() for p in self.players):
                return {"ok": False, "error": f"{riot_id} already tracked"}
            new_p = {
                "riot_id": riot_id, "puuid": None,
                "status": "CHECKING", "status_detail": "Checking…",
                "last_match_id": None, "last_queue_id": None, "last_mode": "",
                "last_checked_ts": None,
                "enabled": True,
            }
            self.players.append(new_p)
            if team_name:
                for t in self.teams:
                    if t["name"] == team_name and riot_id not in t["members"]:
                        t["members"].append(riot_id)
                        break
            save_json(PLAYERS_FILE, self.players)
            save_json(TEAMS_FILE,   self.teams)

        self._check_one_async(new_p)
        return {"ok": True}

    def remove_player(self, riot_id: str) -> dict:
        with self._lock:
            self.players = [p for p in self.players if p["riot_id"] != riot_id]
            for t in self.teams:
                t["members"] = [m for m in t["members"] if m != riot_id]
            save_json(PLAYERS_FILE, self.players)
            save_json(TEAMS_FILE,   self.teams)
        return {"ok": True}

    def rename_player(self, old_rid: str, new_rid: str) -> dict:
        new_rid = (new_rid or "").strip()
        if "#" not in new_rid:
            return {"ok": False, "error": "Must be Name#TAG"}
        if new_rid == old_rid:
            return {"ok": True}
        with self._lock:
            if any(p["riot_id"].lower() == new_rid.lower()
                   for p in self.players if p["riot_id"] != old_rid):
                return {"ok": False, "error": f"{new_rid} already tracked"}
            target = None
            for p in self.players:
                if p["riot_id"] == old_rid:
                    p.update({"riot_id": new_rid, "puuid": None,
                              "last_match_id": None, "status": "CHECKING",
                              "status_detail": "Checking…", "last_checked_ts": None})
                    target = p
                    break
            for t in self.teams:
                t["members"] = [new_rid if m == old_rid else m for m in t["members"]]
            save_json(PLAYERS_FILE, self.players)
            save_json(TEAMS_FILE,   self.teams)
        if target:
            self._check_one_async(target)
        return {"ok": True}

    def set_player_enabled(self, riot_id: str, enabled: bool) -> dict:
        """Enable or disable API checks for a player without removing them."""
        target = None
        with self._lock:
            for p in self.players:
                if p["riot_id"] == riot_id:
                    p["enabled"] = bool(enabled)
                    if not enabled:
                        # Freeze a clear "off" marker so stale active alerts
                        # don't keep pulsing after disable.
                        p["status"] = "DISABLED"
                        p["status_detail"] = "Checks paused"
                        p["last_mode"] = ""
                        p["live_participants"] = None
                        p["my_team_id"] = None
                        p["live_game_start_ts"] = None
                    else:
                        p["status"] = "CHECKING"
                        p["status_detail"] = "Resuming checks…"
                    target = p
                    break
            save_json(PLAYERS_FILE, self.players)
        if target and enabled:
            self._check_one_async(target)
        return {"ok": True}

    def move_player(self, riot_id: str, team_name: Optional[str],
                    before_rid: Optional[str] = None) -> dict:
        """Move a player to a team (or to ungrouped if team_name is None).

        Optional `before_rid` — when given, insert the moved player
        immediately before that riot_id in the destination section:
          * team destination: inserted at that index in the team's
            `members` list.
          * ungrouped destination: `self.players` is reordered so the
            moved player sits before `before_rid` in the master list
            (that's what drives render order for the ungrouped section).

        Same-section reorders work through the same path — we detach
        from every team, then reinsert at the requested position.
        `before_rid == None` means "append to end" (the prior default).
        """
        with self._lock:
            # Detach from every team first so reorders can reuse this
            # path without special-casing same-team moves.
            for t in self.teams:
                t["members"] = [m for m in t["members"] if m != riot_id]

            if team_name:
                for t in self.teams:
                    if t["name"] == team_name:
                        if before_rid and before_rid in t["members"]:
                            idx = t["members"].index(before_rid)
                            t["members"].insert(idx, riot_id)
                        else:
                            t["members"].append(riot_id)
                        break
            else:
                # Ungrouped: rearrange the master player list so render
                # order follows the intended insertion point.
                src_idx = next(
                    (i for i, p in enumerate(self.players)
                     if p["riot_id"] == riot_id),
                    -1,
                )
                if src_idx >= 0:
                    player_obj = self.players.pop(src_idx)
                    if before_rid:
                        dst_idx = next(
                            (i for i, p in enumerate(self.players)
                             if p["riot_id"] == before_rid),
                            -1,
                        )
                        if dst_idx >= 0:
                            self.players.insert(dst_idx, player_obj)
                        else:
                            self.players.append(player_obj)
                    else:
                        self.players.append(player_obj)
                    save_json(PLAYERS_FILE, self.players)

            save_json(TEAMS_FILE, self.teams)
        return {"ok": True}

    # ── Teams ─────────────────────────────────────────────────────────────
    def add_team(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Name required"}
        with self._lock:
            if any(t["name"].lower() == name.lower() for t in self.teams):
                return {"ok": False, "error": f'"{name}" already exists'}
            self.teams.append({"name": name, "members": []})
            save_json(TEAMS_FILE, self.teams)
        return {"ok": True}

    def remove_team(self, name: str) -> dict:
        with self._lock:
            self.teams = [t for t in self.teams if t["name"] != name]
            save_json(TEAMS_FILE, self.teams)
        return {"ok": True}

    def rename_team(self, old_name: str, new_name: str) -> dict:
        new_name = (new_name or "").strip()
        if not new_name:
            return {"ok": False, "error": "Name required"}
        if new_name == old_name:
            return {"ok": True}
        with self._lock:
            if any(t["name"].lower() == new_name.lower() for t in self.teams):
                return {"ok": False, "error": f'"{new_name}" already exists'}
            for t in self.teams:
                if t["name"] == old_name:
                    t["name"] = new_name
                    break
            save_json(TEAMS_FILE, self.teams)
        return {"ok": True}

    # ── History log ───────────────────────────────────────────────────────
    def get_history(self) -> dict:
        """Return the full persistent match history.

        Structure is a forward-compatible dict (see `record_match`).
        Useful for future predictive features (day/hour play patterns,
        premade detection) — not currently surfaced in the UI.
        """
        return load_history()

    # ── Refresh machinery ─────────────────────────────────────────────────
    def refresh_now(self) -> dict:
        """Force every enabled player to be due immediately."""
        with self._lock:
            for p in self.players:
                if p.get("enabled", True):
                    p["last_checked_ts"] = None
        self.next_check_ts = time.time()
        return {"ok": True}

    def _check_one_async(self, player: dict):
        def _work():
            if not self.api:
                return
            updated = check_player(self.api, player)
            with self._lock:
                for i, p in enumerate(self.players):
                    if p["riot_id"] == updated["riot_id"]:
                        self.players[i] = updated
                        break
                save_json(PLAYERS_FILE, self.players)
        threading.Thread(target=_work, daemon=True).start()

    def _check_and_save(self, player: dict):
        """Run a full check on one player and persist the result."""
        updated = check_player(self.api, player)
        with self._lock:
            for i, existing in enumerate(self.players):
                if existing["riot_id"] == updated["riot_id"]:
                    self.players[i] = updated
                    break
            save_json(PLAYERS_FILE, self.players)

    def _next_due_ts(self) -> float:
        """Soonest time any enabled player is due, or +inf if none."""
        with self._lock:
            candidates = [due_at(p) for p in self.players
                          if p.get("enabled", True)]
        return min(candidates, default=float("inf"))

    def _tick(self):
        """One scheduler tick: check every player whose cadence has elapsed.

        Players are checked in priority order (hottest cadence first), with
        a small spacing between calls to stay gentle on the API.
        """
        if not self.api:
            return
        now = time.time()
        with self._lock:
            due = [dict(p) for p in self.players
                   if p.get("enabled", True) and due_at(p) <= now]
        # Sort so hottest statuses get serviced first when multiple are due.
        due.sort(key=lambda p: cadence_for_player(p))
        for p in due:
            if self._stop_evt.is_set():
                return
            self._check_and_save(p)
            time.sleep(0.2)

    def start_background_refresh(self):
        """Spawn the per-player scheduler loop. Called once on startup."""
        def _loop():
            # Nudge the first check slightly after startup so the UI
            # has a chance to render the initial state first.
            time.sleep(1)
            while not self._stop_evt.is_set():
                self._tick()
                # Sleep until the next player is due (capped at 5s so
                # we stay responsive to state changes and refresh_now).
                now = time.time()
                next_due = self._next_due_ts()
                self.next_check_ts = next_due if next_due != float("inf") \
                                     else now + 30
                wait = max(0.5, min(self.next_check_ts - now, 5.0))
                self._stop_evt.wait(wait)
        threading.Thread(target=_loop, daemon=True).start()

    def stop(self):
        self._stop_evt.set()
