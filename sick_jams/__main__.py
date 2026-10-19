"""Generate Sick Jams mixes in Music.app.  Usage: python3 -m sick_jams [--dry-run] [--no-lookup]"""

import argparse
import json
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from .bonus import build_bonus, resolve
from .discover import format_picks, recommend_albums
from .mixer import Settings, build_mixes, listening_profile
from .music import read_library, write_playlists
from .popularity import Popularity

try:
    from .local_extras import after_album_picks   # optional personal add-on, not in the repo
except ImportError:
    after_album_picks = None

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".data"
HISTORY_DAYS = 90
PICK_REPEAT_DAYS = 28    # don't suggest the same artist again for this long


def load_config() -> dict:
    p = ROOT / "config.toml"
    return tomllib.loads(p.read_text()) if p.exists() else {}


def load_state() -> dict:
    p = DATA / "state.json"
    state = json.loads(p.read_text()) if p.exists() else {}
    for k in ("counts", "history", "mixed", "picked"):
        state.setdefault(k, {} if k == "counts" else [])
    return state


def record_plays(state: dict, tracks) -> None:
    """Music.app only keeps a total play count, so diff it against last run to build a history."""
    now = time.time()
    prev = state["counts"]
    if prev:
        delta = {t.pid: t.plays - prev.get(t.pid, 0) for t in tracks if t.plays > prev.get(t.pid, 0)}
        if delta:
            state["history"].append({"t": now, "plays": delta})
    state["history"] = [e for e in state["history"] if now - e["t"] < HISTORY_DAYS * 86400]
    state["counts"] = {t.pid: t.plays for t in tracks}


def main():
    # Windows consoles may not be UTF-8; don't crash on "·" or band names with accents.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the mixes, don't touch Music.app")
    ap.add_argument("--no-lookup", action="store_true", help="skip Deezer popularity lookups")
    ap.add_argument("--seed", type=int, help="random seed (same seed = same mixes)")
    ap.add_argument("--into", metavar="TEXT",
                    help="band, album or song to build a bonus mix around (skips the prompt)")
    ap.add_argument("--no-prompt", action="store_true", help="don't ask about a bonus mix")
    args = ap.parse_args()

    into = args.into
    if into is None and not args.no_prompt and sys.stdin.isatty():
        try:
            into = input("Into a band, album or song right now? Type it for a bonus mix, "
                         "or press Enter to skip: ").strip()
        except EOFError:
            into = ""

    cfg = load_config()
    settings = Settings(**cfg.get("mixes", {}))
    folder = cfg.get("folder", "Sick Jams")

    print("Reading your Music library...")
    tracks = read_library()
    print(f"  {len(tracks)} tracks")

    state = load_state()
    record_plays(state, tracks)

    pop = Popularity(DATA / "popularity.json")
    pop.update({t.artist for t in tracks}, enabled=not args.no_lookup and cfg.get("lookup_popularity", True))

    recently_mixed = {pid for run in state["mixed"] for pid in run}
    prof = listening_profile(tracks, pop, settings, state["history"])
    mixes = build_mixes(prof, pop, settings, recently_mixed, seed=args.seed)

    run_date = datetime.now()
    today = f"{run_date:%b} {run_date.day}"          # (%-d isn't supported on Windows)
    spec = []
    for i, m in enumerate(mixes, 1):
        name = f"Sick Jams {i} · {m.vibe}"
        desc = f"{', '.join(m.seed_artists)} and more. Made {today}."
        spec.append({"slot": str(i), "name": name, "description": desc,
                     "ids": [t.pid for t in m.tracks]})
        print(f"\n{name}  ({len(m.tracks)} tracks)\n  {desc}")
        for t in m.tracks[:12]:
            tag = "  · deep cut" if t.pid in m.deep_cuts else ""
            print(f"    {t.artist} – {t.name}{tag}")
        if len(m.tracks) > 12:
            print(f"    ...+{len(m.tracks) - 12} more")

    mixed_now = [t.pid for m in mixes for t in m.tracks]
    if into:
        seed = resolve(into, prof.tracks, pop)
        built = build_bonus(seed, prof, pop, settings, recently_mixed, args.seed) if seed else None
        if not built:
            print(f"\nCouldn't find enough to build a bonus mix from \"{into}\"; skipping it.")
        else:
            bonus, deep_ids, desc = built
            name = f"Sick Jams Bonus · {seed.label}"[:80]
            have_artist = any(t.artist == seed.artist for t in prof.tracks)
            if seed.kind == "artist" or seed.tracks:
                where = "in your library" if have_artist else \
                    "not in your library, so built from related artists you have"
            else:
                where = f"not in your library; built from {seed.artist}" + \
                    (" and related artists" if have_artist else "'s related artists")
            print(f"\n{name}  ({len(bonus)} tracks)\n  Matched {seed.kind}: {seed.label}"
                  f"{'' if seed.kind == 'artist' else ' by ' + seed.artist} ({where})\n  {desc}")
            for t in bonus[:12]:
                print(f"    {t.artist} – {t.name}{'  · deep cut' if t.pid in deep_ids else ''}")
            if len(bonus) > 12:
                print(f"    ...+{len(bonus) - 12} more")
            spec.append({"slot": "Bonus", "name": name, "description": desc,
                         "ids": [t.pid for t in bonus]})
            mixed_now += [t.pid for t in bonus]

    picks, picks_text = [], ""
    n_picks = cfg.get("album_picks", 5)
    if n_picks and not args.no_lookup:
        print("\nFinding albums to check out...")
        now = time.time()
        state["picked"] = [p for p in state["picked"] if now - p["t"] < PICK_REPEAT_DAYS * 86400]
        picks = recommend_albums(prof, pop, {p["id"] for p in state["picked"]}, n_picks)
        picks_text = format_picks(picks, f"{today}, {run_date.year}")
        print("\n" + picks_text)
        state["picked"] += [{"id": p.artist_id, "t": now} for p in picks]

    if args.dry_run:
        print("\nDry run: Music.app and Album Picks.txt not changed.")
        return

    if picks_text:
        (ROOT / "Album Picks.txt").write_text(picks_text)

    print(f"\nWriting playlists to the '{folder}' folder in Music...")
    print(write_playlists(folder, spec))
    state["mixed"] = (state["mixed"] + [mixed_now])[-2:]
    DATA.mkdir(exist_ok=True)
    (DATA / "state.json").write_text(json.dumps(state))
    print("\nDone. Sync your phone and they're there."
          + ("\nAlbum picks saved to 'Album Picks.txt'." if picks_text else ""))

    if picks and after_album_picks and not args.no_prompt:
        after_album_picks(picks)


if __name__ == "__main__":
    main()
