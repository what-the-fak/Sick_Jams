"""Popularity ("is this a hit?") from Deezer's free public API, cached on disk.

Per artist we fetch: the artist's fan count, their top ~100 tracks with Deezer's rank score, and
the genre of one of their albums. That's ~3 requests per artist instead of one per song, and the
cache means later runs only look up artists you've added since.
"""

import difflib
import json
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.deezer.com"
CACHE_VERSION = 1
REFRESH_DAYS = 60
# macOS/Windows curl use the system certificate store; python.org builds on macOS often ship
# without CA certs, so prefer curl and fall back to urllib.
CURL = shutil.which("curl")
SKIP_ARTISTS = {"", "various artists", "various", "misc", "unknown artist", "one hit wonders"}


def _fetch(url: str) -> str:
    if CURL:
        r = subprocess.run([CURL, "-sS", "-m", "15", url], capture_output=True)
        if r.returncode != 0:
            raise OSError(r.stderr.decode(errors="replace").strip())
        return r.stdout.decode("utf-8")
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read().decode("utf-8")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)            # (Remastered 2004), [Live], (feat. X)
    s = re.sub(r"\s+-\s+.*(remaster|live|version|edit|mix|mono|stereo).*$", "", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", "", s)
    s = s.replace("&", "and")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


class _RateLimiter:
    """Deezer allows 50 requests / 5 seconds; stay under it."""

    def __init__(self, per_sec: float = 8.0):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        time.sleep(max(0.0, t - now))


class Popularity:
    def __init__(self, cache_path: Path):
        self.path = cache_path
        self.limiter = _RateLimiter()
        self.errors = 0
        self.last_error = ""
        self.data = {"version": CACHE_VERSION, "artists": {}}
        if cache_path.exists():
            d = json.loads(cache_path.read_text())
            if d.get("version") == CACHE_VERSION:
                self.data = d

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))

    def _get(self, path: str, **params):
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(4):
            self.limiter.wait()
            try:
                d = json.loads(_fetch(url))
                if isinstance(d, dict) and d.get("error"):
                    if d["error"].get("code") == 4:      # quota hit: back off and retry
                        time.sleep(2 + attempt * 2)
                        continue
                    return None
                return d
            except Exception as e:
                self.errors += 1
                self.last_error = str(e)
                time.sleep(1 + attempt)
        return None

    def _lookup_artist(self, name: str) -> dict:
        entry = {"fetched": time.time(), "found": False}
        # "DJ Khaled Feat Akon ..." -> "DJ Khaled"
        primary = re.split(r"\s+(?:feat\.?|ft\.?|featuring|with|x)\s+|[,;/]", name, flags=re.I)[0].strip()
        res = self._get("/search/artist", q=primary, limit=5)
        cands = (res or {}).get("data", [])
        want = norm(primary)
        match = next((a for a in cands if norm(a["name"]) == want), None)
        if not match:   # typos like "Eifel 65"
            match = next((a for a in cands
                          if difflib.SequenceMatcher(None, norm(a["name"]), want).ratio() >= 0.85), None)
        if not match:
            return entry
        entry.update(found=True, id=match["id"], fans=match.get("nb_fan", 0))
        top = self._get(f"/artist/{match['id']}/top", limit=100)
        tracks = (top or {}).get("data", [])
        ranks: dict[str, int] = {}
        for t in tracks:
            k = norm(t.get("title_short") or t["title"])
            ranks[k] = max(ranks.get(k, 0), t.get("rank", 0))
        entry["top"] = ranks
        if tracks:
            album = self._get(f"/album/{tracks[0]['album']['id']}")
            genres = ((album or {}).get("genres") or {}).get("data", [])
            entry["genre"] = genres[0]["name"] if genres else None
        return entry

    def update(self, artists: set[str], enabled: bool = True):
        if not enabled:
            return
        cutoff = time.time() - REFRESH_DAYS * 86400
        todo = [a for a in artists
                if a.lower() not in SKIP_ARTISTS
                and self.data["artists"].get(a, {}).get("fetched", 0) < cutoff]
        if not todo:
            return
        probe = self._get("/search/artist", q="Green Day", limit=1)
        if not probe:
            print(f"  ! Can't reach Deezer ({self.last_error or 'no response'}); "
                  "using cached popularity only.")
            return
        print(f"  Looking up popularity for {len(todo)} artists on Deezer "
              f"(~{len(todo) * 3 // 8 // 60 + 1} min, cached after this)...")
        done = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            for name, entry in zip(todo, ex.map(self._lookup_artist, todo)):
                self.data["artists"][name] = entry
                done += 1
                if done % 25 == 0 or done == len(todo):
                    print(f"    {done}/{len(todo)}", flush=True)
                    self.save()
        self.save()

    def related(self, artist: str) -> list[dict]:
        """Similar artists per Deezer, most similar first: [{"id", "name", "fans"}]."""
        a = self.artist(artist)
        return self.related_by_id(a["id"]) if a.get("found") else []

    def related_by_id(self, artist_id: int) -> list[dict]:
        cache = self.data.setdefault("related", {})
        key = str(artist_id)
        if key not in cache:
            res = self._get(f"/artist/{artist_id}/related", limit=25)
            cache[key] = [{"id": r["id"], "name": r["name"], "fans": r.get("nb_fan", 0)}
                          for r in (res or {}).get("data", [])]
        return cache[key]

    def search(self, query: str) -> dict | None:
        """Best Deezer guess for free text: {"kind": artist|album|track, "artist", "artist_id", "title"}."""
        want = norm(query)
        found = {k: (self._get(f"/search/{k}", q=query, limit=5) or {}).get("data", [])
                 for k in ("artist", "album", "track")}
        # Exact matches of any kind beat near-misses ("...And Out Come the Wolves" is the album,
        # not the band "Out Came the Wolves").
        for exact in (True, False):
            for kind, results in found.items():
                for r in results:
                    name = r["name"] if kind == "artist" else r["title"]
                    full = name if kind == "artist" else f"{r['artist']['name']} {name}"
                    if (norm(name) == want or norm(full) == want) if exact else (
                            difflib.SequenceMatcher(None, norm(name), want).ratio() >= 0.85):
                        a = r if kind == "artist" else r["artist"]
                        return {"kind": kind, "artist": a["name"], "artist_id": a["id"],
                                "title": None if kind == "artist" else name}
        res = self._get("/search", q=query, limit=1) or {}       # fall back to Deezer's top hit
        if res.get("data"):
            r = res["data"][0]
            return {"kind": "track", "artist": r["artist"]["name"], "artist_id": r["artist"]["id"],
                    "title": r["title"]}
        return None

    def best_album(self, artist_id: int) -> dict | None:
        """The studio album holding the most of an artist's popular songs."""
        cache = self.data.setdefault("albums", {})
        key = str(artist_id)
        entry = cache.get(key)
        if entry and entry.get("fetched", 0) > time.time() - REFRESH_DAYS * 86400:
            return entry.get("album")
        top = (self._get(f"/artist/{artist_id}/top", limit=50) or {}).get("data", [])
        score: dict[int, float] = {}
        for t in top:
            alb = t.get("album") or {}
            if re.search(r"greatest|best of|hits|collection|anthology|live|essential|playlist",
                         alb.get("title", ""), re.I):
                continue
            score[alb["id"]] = score.get(alb["id"], 0) + t.get("rank", 0)
        album = None
        for album_id in sorted(score, key=score.get, reverse=True)[:3]:
            d = self._get(f"/album/{album_id}")
            if d and d.get("record_type") == "album" and d.get("nb_tracks", 0) >= 6:
                album = {"title": d["title"], "year": (d.get("release_date") or "")[:4],
                         "link": d.get("link"), "fans": d.get("fans", 0),
                         "genre": ((d.get("genres") or {}).get("data") or [{}])[0].get("name")}
                break
        cache[key] = {"fetched": time.time(), "album": album}
        return album

    def artist(self, name: str) -> dict:
        return self.data["artists"].get(name, {})

    def genre(self, artist: str) -> str | None:
        return self.artist(artist).get("genre")

    def track_rank(self, artist: str, title: str) -> int | None:
        """Deezer rank if it's one of the artist's top tracks, 0 if not, None if artist unknown."""
        a = self.artist(artist)
        if not a.get("found"):
            return None
        top = a.get("top", {})
        k = norm(title)
        if k in top:
            return top[k]
        # Loose match for titles like "Basket Case - 2001 Remaster" vs "Basket Case".
        for tk, r in top.items():
            if len(k) > 4 and (tk.startswith(k) or k.startswith(tk)) and min(len(k), len(tk)) > 4:
                return r
        return 0
