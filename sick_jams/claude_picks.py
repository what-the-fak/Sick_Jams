"""Claude's pick: Claude Haiku reads what you've been playing and builds something new.

One API call per run. Haiku sees your recent plays, today's mixes (so it steers somewhere
different), the artists in your library, and a numbered list of candidate songs from your library.
It answers with a themed playlist (by those numbers, so every song is one you own and can sync)
plus album suggestions, each checked against Deezer, then MusicBrainz, so made-up ones are dropped.

Needs the Anthropic SDK (`pip3 install anthropic`) and an API key: set ANTHROPIC_API_KEY or run
`python3 -m sick_jams --set-api-key`. A run costs a fraction of a cent with Haiku.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .discover import AlbumPick
from .mixer import Profile, hit_scores
from .popularity import CURL, Popularity, norm

DEFAULT_MODEL = "claude-haiku-5-5"
# $ per million tokens (input, output) for the cost line; prompts here stay well under 100K tokens.
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}
CANDIDATES_PER_ARTIST = 3
MAX_PER_ARTIST = 3

SYSTEM = """You're a music curator building one playlist and a couple of album suggestions for a \
listener, from their own music library.

The playlist should take them somewhere new: a coherent direction, mood or scene that their \
recent listening points toward but that today's mixes don't already cover. Think "if you've been \
into X lately, you'll love this corner of your library." Lean mostly on strong, well-known songs, \
with a few lesser-known ones that fit the theme. Order the songs so the playlist flows well. Only \
use songs from the numbered candidate list, by number. Use at most 3 songs per artist.

For albums: suggest real, released studio albums the listener doesn't own that fit their taste \
and the new direction. Prefer artists who aren't in their library. Rank the best first. Give the \
exact album title and artist as they're commonly listed, and a one-sentence reason that names \
artists they actually play."""


class AlbumRec(BaseModel):
    artist: str
    album: str
    why: str


class ClaudeMix(BaseModel):
    playlist_name: str      # short, evocative; no "Sick Jams" prefix
    theme: str              # one or two sentences on the direction and why it fits
    track_numbers: list[int]
    albums: list[AlbumRec]


@dataclass
class ClaudeResult:
    name: str
    theme: str
    tracks: list
    albums: list[tuple[AlbumPick, str]]     # (pick, why)
    cost: float


def key_file(data_dir: Path) -> Path:
    return data_dir / "anthropic_api_key"


def load_key(data_dir: Path) -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    f = key_file(data_dir)
    return f.read_text().strip() if f.exists() else None


def _clipboard() -> str:
    cmd = (["powershell", "-NoProfile", "-Command", "Get-Clipboard"] if sys.platform == "win32"
           else ["pbpaste"])
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout
    except OSError:
        return ""


def save_key(data_dir: Path) -> None:
    """Prompt for a key, check it works, store it in .data/ (git-ignored)."""
    import anthropic

    # Pasting into a terminal is fragile (hidden prompts can refuse it, and a copied line break
    # submits an empty answer), so the clipboard can be read directly instead.
    raw = input("Copy your API key, then just press Enter (or paste it here and press Enter): ")
    if not raw.strip():
        raw = _clipboard()
        print("Reading the key from your clipboard...")
    # Terminals can wrap pastes in invisible markers (ESC[200~ ... ESC[201~); drop those and
    # any spaces or line breaks that came along with the copy.
    key = re.sub(r"\x1b\[20[01]~", "", raw)
    key = "".join(ch for ch in key if ch.isprintable() and not ch.isspace())
    if not key:
        print("No key found (the clipboard was empty?). Copy the key and try again; nothing saved.")
        return
    if key != raw.strip():
        print("(Removed hidden characters that came along with the paste.)")
    # Only the non-secret prefix and the length are shown, never the key itself.
    print(f"Checking a {len(key)}-character key starting with {key[:10]}...")
    if "..." in key or "\u2026" in key:
        print("That's the shortened preview from the key list (it has '...' in it). The full key "
              "is only shown once, when you create it; create a new one and copy it from that "
              "popup. Nothing saved.")
        return
    if not key.startswith("sk-ant-"):
        print("That doesn't look like an API key: they start with sk-ant-. Create one in the "
              "Claude Console (platform.claude.com) under API Keys; nothing saved.")
        return
    if key.startswith("sk-ant-admin"):
        print("That's an Admin key, which can't send messages. Create a regular key under "
              "API Keys; nothing saved.")
        return
    try:
        anthropic.Anthropic(api_key=key).models.retrieve(DEFAULT_MODEL)
    except anthropic.AuthenticationError as e:
        print(f"The API rejected the key ({e.message}).")
        print("Check that you copied the whole key (it's only shown once when created; if in "
              "doubt, create a new one) and that it hasn't been deleted. Nothing saved.")
        return
    except anthropic.PermissionDeniedError as e:
        print(f"The key works but isn't allowed to use {DEFAULT_MODEL} ({e.message}); saving it.")
    except anthropic.NotFoundError:
        print(f"The key works, but {DEFAULT_MODEL} wasn't found for this account; saving it.")
    except anthropic.APIConnectionError:
        print("Couldn't reach the API to check the key (no internet?); saving it anyway.")
    data_dir.mkdir(exist_ok=True)
    f = key_file(data_dir)
    f.write_text(key + "\n")
    try:
        f.chmod(0o600)
    except OSError:
        pass
    print(f"Saved. Claude's pick will run on the next run. (Stored in {f}, which git ignores.)")


def _days(dt, now):
    return (now - dt).days if dt else None


def _build_prompt(prof: Profile, pop: Popularity, mixes, recent_limit=60):
    now = datetime.now(timezone.utc)
    recent = sorted((t for t in prof.tracks if t.last_played and _days(t.last_played, now) <= 45),
                    key=lambda t: t.last_played, reverse=True)[:recent_limit]
    recent_lines = [f"- {t.artist} – {t.name} ({t.plays} plays, {_days(t.last_played, now)}d ago)"
                    for t in recent]

    mix_lines = [f"- {m.vibe}: {', '.join(m.seed_artists)} (e.g. "
                 f"{'; '.join(f'{t.artist} – {t.name}' for t in m.tracks[:4])})" for m in mixes]

    counts = Counter(t.artist for t in prof.tracks)
    catalog = [f"{a} ({counts[a]}, {prof.vibes.get(a, '?')})"
               for a in sorted(counts, key=lambda a: a.lower())]

    # Candidates: each artist's biggest songs you haven't played in the last two weeks.
    hit = hit_scores(prof.tracks, pop)
    by_artist = defaultdict(list)
    for t in prof.tracks:
        d = _days(t.last_played, now)
        if d is None or d > 14:
            by_artist[t.artist].append(t)
    candidates = []
    for a in sorted(by_artist, key=lambda a: a.lower()):
        seen = set()
        for t in sorted(by_artist[a], key=lambda t: hit[t.pid], reverse=True):
            if norm(t.name) in seen:
                continue
            seen.add(norm(t.name))
            candidates.append(t)
            if len(seen) == CANDIDATES_PER_ARTIST:
                break
    cand_lines = [f"{i}. {t.artist} – {t.name}" + (f" ({t.year})" if t.year else "")
                  for i, t in enumerate(candidates)]

    user = ("## Recently played (newest first)\n" + ("\n".join(recent_lines) or "(nothing recent)")
            + "\n\n## Today's mixes (don't repeat these directions)\n" + "\n".join(mix_lines)
            + "\n\n## Artists in their library (song count, vibe)\n" + "; ".join(catalog)
            + "\n\n## Candidate songs (use these numbers)\n" + "\n".join(cand_lines)
            + "\n\nBuild a playlist of about {n} songs, and suggest {a} albums ranked best first.")
    return user, candidates


MB_AGENT = "SickJams/1.0 (https://github.com/what-the-fak/Sick_Jams)"


def _musicbrainz_album(artist: str, album: str) -> dict | None:
    """MusicBrainz knows nearly every release, including ones streaming services lack."""
    q = f'releasegroup:"{album}" AND artist:"{artist}"'.replace("\\", "")
    url = "https://musicbrainz.org/ws/2/release-group/?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": 5})
    groups = None
    for attempt in range(4):
        time.sleep(1.1 + 2 * attempt)          # MusicBrainz allows ~1 request/second
        try:
            if CURL:
                r = subprocess.run([CURL, "-sS", "-m", "15", "-A", MB_AGENT, url],
                                   capture_output=True)
                body = r.stdout.decode("utf-8")
            else:
                req = urllib.request.Request(url, headers={"User-Agent": MB_AGENT})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8")
            data = json.loads(body)
        except Exception:
            continue
        if "error" not in data:                # "server is busy": wait and retry
            groups = data.get("release-groups", [])
            break
    if groups is None:
        return None
    for g in groups:
        credit = (g.get("artist-credit") or [{}])[0].get("name", "")
        if norm(g["title"]) == norm(album) and norm(credit) == norm(artist):
            return g
    return None


def _verify_album(rec: AlbumRec, prof: Profile, pop: Popularity) -> AlbumPick | None:
    """The album as Deezer or MusicBrainz lists it; None if it doesn't exist or you have it."""
    owned = {(norm(t.artist), norm(t.album)) for t in prof.tracks}
    if (norm(rec.artist), norm(rec.album)) in owned:
        return None
    res = pop._get("/search/album", q=f"{rec.artist} {rec.album}", limit=10) or {}
    for d in res.get("data", []):
        if norm(d["artist"]["name"]) == norm(rec.artist) and norm(d["title"]) == norm(rec.album):
            if (norm(d["artist"]["name"]), norm(d["title"])) in owned:
                return None
            full = pop._get(f"/album/{d['id']}") or {}
            year = (full.get("release_date") or "")[:4]
            return AlbumPick(d["artist"]["name"], d["title"], year, d.get("link", ""), [],
                             d["artist"]["id"])
    g = _musicbrainz_album(rec.artist, rec.album)
    if g:
        credit = g["artist-credit"][0]["name"]
        return AlbumPick(credit, g["title"], (g.get("first-release-date") or "")[:4],
                         f"https://musicbrainz.org/release-group/{g['id']}", [], 0)
    return None


def claude_mix(prof: Profile, pop: Popularity, mixes, key: str, model: str = DEFAULT_MODEL,
               n_tracks: int = 30, n_albums: int = 2,
               exclude=()) -> ClaudeResult | None:
    """exclude: (artist, album) pairs already suggested elsewhere this run."""
    try:
        import anthropic
    except ImportError:
        print("  Claude's pick needs the Anthropic SDK: pip3 install anthropic")
        return None

    user, candidates = _build_prompt(prof, pop, mixes)
    user = user.replace("{n}", str(n_tracks)).replace("{a}", str(n_albums + 2))
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_format=ClaudeMix,
        )
    except anthropic.AuthenticationError:
        print("  Claude's pick: the API key was rejected. Run with --set-api-key to replace it.")
        return None
    except anthropic.PermissionDeniedError:
        print("  Claude's pick: the API key isn't allowed to use this model.")
        return None
    except anthropic.RateLimitError:
        print("  Claude's pick: rate limited by the API; skipping this run.")
        return None
    except anthropic.APIStatusError as e:
        if e.status_code == 400 and "credit" in str(e.message).lower():
            print("  Claude's pick: your API account is out of credit "
                  "(add some at platform.claude.com → Billing).")
        else:
            print(f"  Claude's pick: API error {e.status_code}: {e.message}")
        return None
    except anthropic.APIConnectionError:
        print("  Claude's pick: couldn't reach the API (no internet?).")
        return None

    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        print(f"  Claude's pick: no usable answer this time (stop reason: {resp.stop_reason}).")
        return None
    out: ClaudeMix = resp.parsed_output

    per_artist, tracks, seen = Counter(), [], set()
    for i in out.track_numbers:
        if 0 <= i < len(candidates) and i not in seen:
            t = candidates[i]
            if per_artist[t.artist] < MAX_PER_ARTIST:
                per_artist[t.artist] += 1
                seen.add(i)
                tracks.append(t)
    if len(tracks) < 10:
        print(f"  Claude's pick: only {len(tracks)} usable songs came back; skipping the playlist.")
        tracks = []

    skip = {(norm(a), norm(b)) for a, b in exclude}
    albums = []
    for rec in out.albums:
        if len(albums) == n_albums:
            break
        pick = _verify_album(rec, prof, pop)
        if pick and (norm(pick.artist), norm(pick.title)) not in skip:
            skip.add((norm(pick.artist), norm(pick.title)))
            albums.append((pick, rec.why))

    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    cost = (resp.usage.input_tokens * p_in + resp.usage.output_tokens * p_out) / 1e6
    name = out.playlist_name.replace("Sick Jams", "").strip(" ·-:") or "Something New"
    return ClaudeResult(name, out.theme.strip(), tracks, albums, cost)


def format_claude_albums(albums: list[tuple[AlbumPick, str]]) -> str:
    lines = ["", "Claude's picks", ""]
    for i, (p, why) in enumerate(albums, 1):
        year = f" ({p.year})" if p.year else ""
        lines += [f"{i}. {p.artist} – {p.title}{year}", f"   {why}", f"   {p.link}", ""]
    return "\n".join(lines)
