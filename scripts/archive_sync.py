#!/usr/bin/env python3
"""
Neforus archive sync.

Runs on a schedule in GitHub Actions. Each run it:
  1. Reads the long-form uploads feed of the YouTube channel (Shorts and
     livestreams are excluded) and adds any new video to INDEX.md, taking the
     "Next upload" production number, with its title, date and thumbnail.
  2. Looks for the mirrors of recent videos on Rumble, Odysee, BitChute and
     X, and adds any links that are still missing. An upload whose title or
     link says "UNCENSORED" is listed separately as "(Uncensored)" next to the
     automatic reupload on the same platform.
  3. Rebuilds the "Latest uploads" grid and badge in README.md.
  4. Opens a GitHub issue (assigned to the repo owner, so GitHub emails them)
     when a video's X link still can't be found after a grace period, or other
     mirrors are still missing, and closes it once everything is found.

Nothing here uses an AI service. Only the Python standard library and Pillow
(for saving thumbnails as PNG) are needed.

Environment variables:
  GITHUB_TOKEN       token for opening issues (provided by GitHub Actions)
  GITHUB_REPOSITORY  owner/repo (provided by GitHub Actions)
  X_BEARER_TOKEN     optional; an X API bearer token, used first when set
  DRY_RUN=1          print what would change, write nothing, open no issues
"""

from __future__ import annotations

import datetime as dt
import difflib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

YOUTUBE_CHANNEL_ID = "UC2gVzcWyCakpUZhwcKIr74g"
ODYSEE_CHANNEL = "@Neforus:d"
BITCHUTE_CHANNEL_ID = "xz6ZHIfOqeMP"
RUMBLE_CHANNEL_URL = "https://rumble.com/user/Neforus/videos"
X_HANDLE = "Neforus"
X_HANDLES = ("Neforus", "TheNeforus", "Neforuss")  # old handles still seen in links
REPO_OWNER = "Neforuss"

# Upload dates in INDEX.md are written in this time zone.
TIMEZONE = "America/Chicago"

# Videos uploaded within this many days are rechecked for missing mirrors.
RECHECK_DAYS = 30
# Notify when X is still missing this long after the YouTube upload.
X_GRACE_HOURS = 48
# Notify when any other mirror is still missing this long after the upload.
MIRROR_GRACE_HOURS = 72
# Notify when YouTube itself can't be read for this long.
YOUTUBE_ALERT_HOURS = 6

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "INDEX.md"
README = ROOT / "README.md"
THUMBS = ROOT / "thumbnails"
STATE = ROOT / ".github" / "archive-sync-state.json"

PLACEHOLDER_TITLE = "*Next upload (in production)*"
PLATFORMS = ["Rumble", "Odysee", "BitChute", "X"]
LABELS = {"Rumble": "Rumble", "Odysee": "Odysee", "BitChute": "BitChute", "X": "Twitter/X"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
NOW = dt.datetime.now(dt.timezone.utc)


SUMMARY: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 30, follow: bool = True) -> tuple[int, bytes]:
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


def http_json(url: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, object]:
    h = {"Accept": "application/json"}
    if payload is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    status, body = http(url, json.dumps(payload).encode() if payload is not None else None, h)
    try:
        return status, json.loads(body or b"null")
    except ValueError:
        return status, None


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def norm(text: str) -> str:
    """Normalise a title for comparison."""
    t = html.unescape(urllib.parse.unquote(text or "")).lower()
    t = t.replace("’", "'").replace("'", "")
    t = re.sub(r"#\w+", " ", t)                      # hashtags
    t = re.sub(r"\buncensored\b|\bre-?upload\b|_1080\b", " ", t)
    t = re.sub(r"[^0-9a-z]+", " ", t)
    t = re.sub(r"^\s*\d{1,4}\s+", " ", t)            # leading production number
    return " ".join(t.split())


def titles_match(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 12 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.88


def is_uncensored(*texts: str) -> bool:
    return any("uncensored" in urllib.parse.unquote(t or "").lower() for t in texts)


YT_ID_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^\s\"<>]*&)?v=|shorts/|live/|embed/))([0-9A-Za-z_-]{11})")
RUMBLE_RE = re.compile(r"https?://(?:www\.)?rumble\.com/(v[0-9a-z]{4,})(?:-[^\s\"<>)\]]*?\.html|\.html)?(?=[\s\"<>)\]?#]|$)", re.I)
ODYSEE_RE = re.compile(r"https?://(?:www\.)?odysee\.com/(?:(@[^\s/\"<>)\]]+)/)?([^\s/@\"<>)\]:]+):([0-9a-z]+)", re.I)
BITCHUTE_RE = re.compile(r"https?://(?:www\.)?bitchute\.com/video/([0-9A-Za-z_-]+)/?", re.I)
X_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/(" + "|".join(X_HANDLES) + r")/status/(\d+)", re.I)


class Link:
    """A mirror link. key identifies the upload regardless of URL spelling."""

    def __init__(self, platform: str, url: str, key: str, uncensored: bool):
        self.platform, self.url, self.key, self.uncensored = platform, url, key.lower(), uncensored

    def __repr__(self) -> str:
        return f"{self.platform}{' (Uncensored)' if self.uncensored else ''} {self.url}"


def extract_links(text: str) -> list[Link]:
    out: list[Link] = []
    text = text or ""
    for m in RUMBLE_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!")
        out.append(Link("Rumble", url, m.group(1), is_uncensored(url)))
    for m in ODYSEE_RE.finditer(text):
        url = m.group(0).rstrip(".,;!")
        out.append(Link("Odysee", url, urllib.parse.unquote(m.group(2)), is_uncensored(url)))
    for m in BITCHUTE_RE.finditer(text):
        out.append(Link("BitChute", m.group(0), m.group(1), False))
    for m in X_RE.finditer(text):
        out.append(Link("X", f"https://x.com/{m.group(1)}/status/{m.group(2)}", m.group(2), False))
    return out


def yt_ids_in(text: str) -> set[str]:
    return set(YT_ID_RE.findall(text or ""))


def parse_time(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return dt.datetime.fromtimestamp(int(value), dt.timezone.utc)
        s = str(value).replace("Z", "+00:00")
        if re.match(r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d", s):  # X API v1 style
            return dt.datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        t = dt.datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except (ValueError, OSError):
        return None


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

class Upload:
    """One upload on a mirror platform."""

    def __init__(self, platform, url, key, title, published, description="", yt_ids=None, uncensored=None):
        self.platform = platform
        self.url = url
        self.key = key
        self.title = title or ""
        self.published = published
        self.description = description or ""
        self.yt_ids = set(yt_ids or ()) | yt_ids_in(self.description)
        self.uncensored = is_uncensored(self.title, url) if uncensored is None else uncensored

    def link(self) -> Link:
        return Link(self.platform, self.url, self.key, self.uncensored)


ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/"}


def youtube_feed(playlist_prefix: str | None, tries: int = 4) -> list[dict] | None:
    """Read a YouTube RSS feed. These feeds fail intermittently, so retry."""
    if playlist_prefix:
        url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_prefix}{YOUTUBE_CHANNEL_ID[2:]}"
    else:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
    status = None
    for attempt in range(tries):
        status, body = http(url)
        if status == 200:
            break
        time.sleep(2 + 3 * attempt)
    if status != 200:
        log(f"  YouTube feed {playlist_prefix or 'channel'}: HTTP {status}")
        return None
    root = ET.fromstring(body)
    items = []
    for e in root.findall("a:entry", ATOM):
        items.append({
            "id": e.findtext("yt:videoId", "", ATOM),
            "title": e.findtext("a:title", "", ATOM),
            "published": parse_time(e.findtext("a:published", "", ATOM)),
            "description": e.findtext("media:group/media:description", "", ATOM) or "",
            "exact_date": True,
        })
    return items


def is_short(video_id: str) -> bool | None:
    status, _ = http(f"https://www.youtube.com/shorts/{video_id}", follow=False)
    if status == 200:
        return True
    if status in (301, 302, 303, 307, 308):
        return False
    return None


AGO_RE = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
AGO_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
             "month": 2629800, "year": 31557600}


def _lockups(obj, out):
    if isinstance(obj, dict):
        if "lockupViewModel" in obj:
            out.append(obj["lockupViewModel"])
        elif "videoRenderer" in obj:
            out.append({"_legacy": obj["videoRenderer"]})
        for v in obj.values():
            _lockups(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _lockups(v, out)


def youtube_videos_tab(tries: int = 3) -> list[dict] | None:
    """The channel's Videos tab. YouTube lists only regular uploads there:
    no Shorts (Shorts tab) and no livestreams (Live tab)."""
    url = f"https://www.youtube.com/channel/{YOUTUBE_CHANNEL_ID}/videos?hl=en&gl=US"
    for attempt in range(tries):
        status, body = http(url, headers={"Cookie": "SOCS=CAI; CONSENT=YES+1"})
        m = re.search(rb"var ytInitialData = (\{.*?\});</script>", body or b"", re.S) if status == 200 else None
        if m:
            try:
                data = json.loads(m.group(1))
            except ValueError:
                data = None
            if data:
                raw: list = []
                _lockups(data, raw)
                out, seen = [], set()
                for item in raw:
                    if "_legacy" in item:
                        v = item["_legacy"]
                        vid = v.get("videoId")
                        title = "".join(r.get("text", "") for r in (v.get("title") or {}).get("runs", []))
                        ago = json.dumps(v.get("publishedTimeText", ""))
                    else:
                        if item.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
                            continue
                        vid = item.get("contentId")
                        md = (item.get("metadata") or {}).get("lockupMetadataViewModel") or {}
                        title = (md.get("title") or {}).get("content", "")
                        ago = json.dumps(md.get("metadata", ""))
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    m2 = AGO_RE.search(ago)
                    published = None
                    if m2:
                        published = NOW - dt.timedelta(seconds=int(m2.group(1)) * AGO_UNITS[m2.group(2).lower()])
                    out.append({"id": vid, "title": title, "published": published,
                                "description": "", "exact_date": False})
                if out:
                    return out
        log(f"  YouTube Videos tab: attempt {attempt + 1} failed (HTTP {status})")
        time.sleep(3 + 3 * attempt)
    return None


def youtube_uploads() -> tuple[list[dict] | None, str | None]:
    """Regular (long-form) uploads only, newest first. Returns (videos, error).

    The Videos tab decides what counts (it never lists Shorts or livestreams).
    The channel RSS feed supplies exact upload times and descriptions. If the
    Videos tab can't be read, the RSS feeds are used instead and a video is
    only taken when it is positively known not to be a Short or a livestream.
    """
    tab = youtube_videos_tab()
    feed = youtube_feed(None)
    feed_by_id = {v["id"]: v for v in feed or []}
    if tab is not None:
        out = []
        for v in tab:
            exact = feed_by_id.get(v["id"])
            out.append(exact if exact else v)
        return out, None
    if feed is None:
        feed = youtube_feed("UULF")
        if feed is None:
            return None, "could not read YouTube (Videos tab and RSS feeds all failed)"
    shorts = youtube_feed("UUSH", tries=2)
    lives = youtube_feed("UULV", tries=2)
    if lives is None:
        return None, "could not read the YouTube Videos tab or livestreams feed, so livestreams can't be ruled out"
    excluded = {v["id"] for v in (shorts or []) + lives}
    out = []
    for v in feed:
        if v["id"] in excluded:
            continue
        if shorts is None and is_short(v["id"]) is not False:
            continue
        out.append(v)
    return out, None


def odysee_uploads() -> tuple[list[Upload], str | None]:
    payload = {"jsonrpc": "2.0", "method": "claim_search", "params": {
        "channel": ODYSEE_CHANNEL, "claim_type": ["stream"], "order_by": ["release_time"],
        "page_size": 50, "page": 1, "no_totals": True}}
    status, data = http_json("https://api.na-backend.odysee.com/api/v1/proxy?m=claim_search", payload)
    try:
        items = data["result"]["items"]
    except (TypeError, KeyError):
        return [], f"Odysee API returned HTTP {status}"
    out = []
    for c in items:
        value = c.get("value") or {}
        canon = c.get("canonical_url") or ""
        if not canon.startswith("lbry://"):
            continue
        url = "https://odysee.com/" + canon[len("lbry://"):].replace("#", ":")
        out.append(Upload("Odysee", url, c.get("name", ""), value.get("title") or c.get("name"),
                          parse_time(value.get("release_time") or c.get("timestamp")),
                          value.get("description", "")))
    return out, None


def bitchute_uploads() -> tuple[list[Upload], str | None]:
    status, data = http_json("https://api.bitchute.com/api/beta/channel/videos",
                             {"channel_id": BITCHUTE_CHANNEL_ID, "offset": 0, "limit": 50})
    if not isinstance(data, dict) or "videos" not in data:
        return [], f"BitChute API returned HTTP {status}"
    out = []
    for v in data["videos"]:
        vid = v.get("video_id", "")
        # BitChute's automatic YouTube import reuses the YouTube video ID.
        ids = {vid} if re.fullmatch(r"[0-9A-Za-z_-]{11}", vid) else set()
        out.append(Upload("BitChute", f"https://www.bitchute.com/video/{vid}/", vid, v.get("video_name"),
                          parse_time(v.get("date_published")), v.get("description", ""), ids))
    return out, None


def rumble_uploads() -> tuple[list[Upload], str | None]:
    status, body = http(RUMBLE_CHANNEL_URL)
    if status != 200:
        return [], f"Rumble channel page returned HTTP {status}"
    page = body.decode("utf-8", "replace")
    out, seen = [], set()
    for m in re.finditer(r'href="(/(v[0-9a-z]{4,})-([^"]+?)\.html)', page):
        key = m.group(2)
        if key in seen:
            continue
        seen.add(key)
        out.append(Upload("Rumble", "https://rumble.com" + m.group(1), key,
                          m.group(3).replace("-", " "), None))
    if not out:
        return [], "Rumble channel page had no videos (layout may have changed)"
    return out, None


def x_posts() -> tuple[list[Upload], str | None]:
    """Recent posts from the X account. Expected to fail often."""
    errors = []
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if token:
        auth = {"Authorization": f"Bearer {token}"}
        status, user = http_json(f"https://api.x.com/2/users/by/username/{X_HANDLE}", headers=auth)
        uid = (user or {}).get("data", {}).get("id") if isinstance(user, dict) else None
        if uid:
            status, data = http_json(
                f"https://api.x.com/2/users/{uid}/tweets?max_results=100"
                "&tweet.fields=created_at,entities&exclude=retweets,replies", headers=auth)
            if isinstance(data, dict) and "data" in data:
                return [_x_upload(t.get("id"), t.get("text"), t.get("created_at"),
                                  [u.get("expanded_url", "") for u in (t.get("entities") or {}).get("urls", [])])
                        for t in data["data"]], None
        errors.append(f"X API returned HTTP {status}")
    status, body = http(f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{X_HANDLE}")
    if status == 200:
        m = re.search(rb'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', body, re.S)
        if m:
            try:
                entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
                posts = []
                for e in entries:
                    t = (e.get("content") or {}).get("tweet") or {}
                    if t.get("id_str"):
                        urls = [u.get("expanded_url", "") for u in (t.get("entities") or {}).get("urls", [])]
                        posts.append(_x_upload(t["id_str"], t.get("full_text") or t.get("text"),
                                               t.get("created_at"), urls))
                if posts:
                    return posts, None
                errors.append("X embed timeline was empty")
            except (KeyError, TypeError, ValueError):
                errors.append("X embed timeline could not be read")
        else:
            errors.append("X embed timeline had no data")
    else:
        errors.append(f"X embed timeline returned HTTP {status}")
    return [], "; ".join(errors)


def _x_upload(tid, text, created, urls) -> Upload:
    text = text or ""
    return Upload("X", f"https://x.com/{X_HANDLE}/status/{tid}", str(tid), text,
                  parse_time(created), text + " " + " ".join(urls), uncensored=False)


# --------------------------------------------------------------------------
# INDEX.md
# --------------------------------------------------------------------------

ROW_RE = re.compile(r"^\|\s*(\d+a?)\s*\|")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
TOKEN_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


class Row:
    def __init__(self, line: str):
        cells = [c.strip() for c in CELL_SPLIT.split(line.rstrip("\n"))[1:-1]]
        cells += [""] * (6 - len(cells))
        self.number, self.title, self.date, self.thumb, self.watch, self.mirrors = cells[:6]
        self.extra = cells[6:]
        self.changed = False

    @property
    def yt_id(self) -> str | None:
        ids = yt_ids_in(self.watch)
        return next(iter(ids)) if ids else None

    def links(self) -> list[Link]:
        return extract_links(self.mirrors)

    def tokens(self) -> list[tuple[str, str]]:
        return TOKEN_RE.findall(self.mirrors)

    def render(self) -> str:
        cells = [self.number, self.title, self.date, self.thumb, self.watch, self.mirrors] + self.extra
        return "| " + " | ".join(cells) + " |"


def platform_of(url: str) -> str | None:
    links = extract_links(url)
    if links:
        return links[0].platform
    u = url.lower()
    for name, dom in (("Rumble", "rumble.com"), ("Odysee", "odysee.com"), ("BitChute", "bitchute.com"),
                      ("X", "x.com/"), ("X", "twitter.com")):
        if dom in u:
            return name
    return None


def add_links(row: Row, new: list[Link]) -> list[Link]:
    """Add links the row doesn't have yet. Existing links are never changed."""
    have = {(l.platform, l.key) for l in row.links()}
    # Platforms that already have a link under the plain (non-"Uncensored") label.
    plain_taken = {platform_of(url) for label, url in row.tokens() if "(Uncensored)" not in label}
    added = []
    for l in new:
        if (l.platform, l.key) in have:
            continue
        # The automatic reupload takes the plain label; only one per platform.
        if not l.uncensored and l.platform in plain_taken:
            continue
        have.add((l.platform, l.key))
        if not l.uncensored:
            plain_taken.add(l.platform)
        added.append(l)
    if not added:
        return []
    tokens = row.tokens() + [(LABELS[l.platform] + (" (Uncensored)" if l.uncensored else ""), l.url) for l in added]

    def rank(tok):
        p = platform_of(tok[1])
        return (PLATFORMS.index(p) if p in PLATFORMS else len(PLATFORMS), "(Uncensored)" in tok[0])

    tokens.sort(key=rank)  # stable: keeps the existing order within a platform
    row.mirrors = " ".join(f"[{label}]({url})" for label, url in tokens)
    row.changed = True
    return added


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def find_mirrors(yt_id: str, title: str, published: dt.datetime | None, yt_description: str,
                 sources: dict[str, list[Upload]]) -> list[Link]:
    found: list[Link] = []
    # 1. Links written in the YouTube description itself.
    found += extract_links(yt_description)
    for platform, uploads in sources.items():
        for up in uploads:
            confirmed = yt_id in up.yt_ids
            if not confirmed:
                if up.yt_ids:          # it names a different YouTube video
                    continue
                if published and up.published and up.published < published - dt.timedelta(days=1):
                    continue
                if platform == "X":
                    if not (norm(title) and norm(title) in norm(up.title)) and not titles_match(up.title, title):
                        continue
                elif not titles_match(up.title, title):
                    continue
            found.append(up.link())
            # 2. Links written in a confirmed mirror's description.
            if confirmed:
                found += extract_links(up.description)
    # Earliest-listed first so the automatic reupload wins the plain label.
    uniq, seen = [], set()
    for l in found:
        if (l.platform, l.key) not in seen:
            seen.add((l.platform, l.key))
            uniq.append(l)
    return uniq


# --------------------------------------------------------------------------
# README.md
# --------------------------------------------------------------------------

GRID_START, GRID_END = "<!-- LATEST-UPLOADS:START -->", "<!-- LATEST-UPLOADS:END -->"


def render_readme(text: str, rows: list[Row]) -> str:
    latest = [r for r in rows if r.yt_id and "thumbnails/" in r.thumb][:6]
    if latest:
        text = re.sub(r"latest-%23\d+a?-", f"latest-%23{latest[0].number}-", text)
    if GRID_START not in text or GRID_END not in text:
        return text
    cells = []
    for r in latest:
        thumb = re.search(r"\((/?thumbnails/[^)]+)\)", r.thumb).group(1).lstrip("/")
        title = html.escape(r.title.replace("\\|", "|"), quote=False)
        cells.append(
            f'<td align="center" width="33%"><a href="https://youtu.be/{r.yt_id}"><img src="{thumb}" '
            f'alt="{r.number}" width="260"></a><br><sub><b>#{r.number}</b> · {r.date}</sub>'
            f"<br><sub>{title}</sub></td>")
    grid = ["<table>"]
    for i in range(0, len(cells), 3):
        grid += ["<tr>"] + cells[i:i + 3] + ["</tr>"]
    grid.append("</table>")
    block = GRID_START + "\n" + "\n".join(grid) + "\n" + GRID_END
    return re.sub(re.escape(GRID_START) + r".*?" + re.escape(GRID_END), lambda _: block, text, flags=re.S)


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------

def save_thumbnail(video_id: str, dest: Path) -> bool:
    from PIL import Image  # imported here so a dry run works without Pillow

    for name in ("maxresdefault", "sddefault", "hqdefault"):
        status, body = http(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg")
        if status == 200 and body:
            img = Image.open(io.BytesIO(body)).convert("RGB")
            if img.size[0] <= 120:  # YouTube's grey "no thumbnail" image
                continue
            if img.size != (1920, 1080) and abs(img.size[0] / img.size[1] - 16 / 9) < 0.02:
                img = img.resize((1920, 1080), Image.LANCZOS)  # match the existing thumbnails
            if not DRY_RUN:
                img.save(dest, "PNG")
            return True
    return False


# --------------------------------------------------------------------------
# Notifications (GitHub issues, emailed by GitHub to the repo owner)
# --------------------------------------------------------------------------

def github(method: str, path: str, payload: dict | None = None):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo or DRY_RUN:
        log(f"  [issue] {method} {path} {json.dumps(payload)[:300] if payload else ''}")
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "neforus-archive-sync"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        log(f"  GitHub API {method} {path}: HTTP {e.code} {e.read()[:200]!r}")
        return None


def open_issue(title: str, body: str) -> int | None:
    issue = github("POST", "/issues", {"title": title, "body": f"@{REPO_OWNER}\n\n{body}",
                                       "assignees": [REPO_OWNER]})
    return issue.get("number") if isinstance(issue, dict) else None


def close_issue(number: int, comment: str) -> None:
    github("POST", f"/issues/{number}/comments", {"body": comment})
    github("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})


def close_failure_issue() -> None:
    """Close the 'sync is failing' issue the workflow opens on a crash, once runs succeed again."""
    found = github("GET", "/issues?state=open&creator=github-actions%5Bbot%5D&per_page=50")
    for issue in found or []:
        if issue.get("title", "").startswith("[archive-sync] The archive sync is failing"):
            close_issue(issue["number"], "The archive sync is running normally again.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    tz = ZoneInfo(TIMEZONE)
    state = json.loads(STATE.read_text()) if STATE.exists() else {"videos": {}}
    state.setdefault("videos", {})
    state_before = json.dumps(state, sort_keys=True)

    lines = INDEX.read_text(encoding="utf-8").split("\n")
    row_idx = [i for i, l in enumerate(lines) if ROW_RE.match(l)]
    if not row_idx:
        log("INDEX.md has no table rows; stopping.")
        return 1
    rows = [Row(lines[i]) for i in row_idx]
    index_text = "\n".join(lines)

    # ---- YouTube ------------------------------------------------------
    log("Reading YouTube…")
    uploads, yt_error = youtube_uploads()
    feed_by_id = {v["id"]: v for v in uploads or []}

    placeholder = next((r for r in rows if r.title == PLACEHOLDER_TITLE), None)
    dated = [r.date for r in rows if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.date)]
    newest_date = max(dated) if dated else "0000-00-00"

    new_videos = []
    for v in reversed(uploads or []):   # oldest first
        if not v["id"] or v["published"] is None:
            continue
        local_date = v["published"].astimezone(tz).date().isoformat()
        if v["id"] in index_text:
            continue
        # Older than the newest row: an old upload that was left out on purpose.
        # Dates read from "3 hours ago" style text are approximate, so allow slack.
        slack = 0 if v.get("exact_date") else 2
        floor = (dt.date.fromisoformat(newest_date) - dt.timedelta(days=slack)).isoformat() \
            if newest_date != "0000-00-00" else newest_date
        if local_date < floor:
            continue
        new_videos.append(v)

    added_rows: list[Row] = []
    for v in new_videos:
        if placeholder:
            number = int(re.match(r"\d+", placeholder.number).group())
        else:
            number = max(int(re.match(r"\d+", r.number).group()) for r in rows) + 1
        thumb_name = f"{number}.png"
        has_thumb = save_thumbnail(v["id"], THUMBS / thumb_name)
        title = html.unescape(v["title"]).replace("|", "\\|").strip()
        row = Row(f"| {number} | {title} | {v['published'].astimezone(tz).date().isoformat()} | "
                  f"{'![thumb](/thumbnails/' + thumb_name + ')' if has_thumb else ''} | "
                  f"[Watch](https://youtu.be/{v['id']}) |  |")
        row.changed = True
        if placeholder:
            placeholder.number = str(number + 1)
            placeholder.changed = True
        added_rows.append(row)
        state["videos"][v["id"]] = {"number": str(number), "published": v["published"].isoformat(),
                                    "approx_date": not v.get("exact_date")}
        log(f"  New video #{number}: {v['title']}")
        SUMMARY.append(f"add #{number}")

    if not placeholder and added_rows:
        placeholder = Row(f"| {int(added_rows[-1].number) + 1} | {PLACEHOLDER_TITLE} | TBA |  |  |  |")
        placeholder.changed = True

    # Newest first, placeholder on top.
    others = [r for r in rows if r is not placeholder]
    all_rows = ([placeholder] if placeholder else []) + list(reversed(added_rows)) + others

    # Replace approximate dates once YouTube's feed gives the exact upload time.
    for r in all_rows:
        meta = state["videos"].get(r.yt_id or "")
        exact = feed_by_id.get(r.yt_id or "")
        if meta and meta.get("approx_date") and exact and exact.get("exact_date"):
            date = exact["published"].astimezone(tz).date().isoformat()
            if r.date != date:
                log(f"  #{r.number}: date corrected {r.date} -> {date}")
                r.date, r.changed = date, True
                SUMMARY.append(f"#{r.number} date")
            meta["published"], meta["approx_date"] = exact["published"].isoformat(), False

    # ---- Mirrors ------------------------------------------------------
    cutoff = (NOW.astimezone(tz) - dt.timedelta(days=RECHECK_DAYS)).date().isoformat()
    to_check = [r for r in all_rows if r.yt_id and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.date)
                and (r.date >= cutoff or r.yt_id in state["videos"])]
    errors = {}
    if to_check:
        log("Reading mirrors…")
        sources = {}
        for name, fn in (("Odysee", odysee_uploads), ("BitChute", bitchute_uploads),
                         ("Rumble", rumble_uploads), ("X", x_posts)):
            sources[name], err = fn()
            if err:
                errors[name] = err
            log(f"  {name}: {len(sources[name])} items" + (f" (failed: {err})" if err else ""))
        for r in to_check:
            info = feed_by_id.get(r.yt_id, {})
            published = info.get("published") or parse_time(state["videos"].get(r.yt_id, {}).get("published"))
            links = find_mirrors(r.yt_id, info.get("title") or r.title.replace("\\|", "|"),
                                 published, info.get("description", ""), sources)
            for l in add_links(r, links):
                log(f"  #{r.number}: added {l}")
                SUMMARY.append(f"#{r.number} {LABELS[l.platform]}{' (Uncensored)' if l.uncensored else ''}")

    # ---- Notifications ------------------------------------------------
    for vid, meta in list(state["videos"].items()):
        row = next((r for r in all_rows if r.yt_id == vid), None)
        published = parse_time(meta.get("published"))
        if row is None or published is None:
            state["videos"].pop(vid, None)
            continue
        have = {l.platform for l in row.links()}
        missing = [p for p in PLATFORMS if p not in have]
        age_h = (NOW - published).total_seconds() / 3600
        issue = meta.get("issue")
        if issue and not missing:
            close_issue(issue, "All mirrors are now linked in INDEX.md.")
            meta["issue"] = None
            meta["resolved"] = True
        elif not issue and not meta.get("resolved") and missing and (
                ("X" in missing and age_h >= X_GRACE_HOURS) or age_h >= MIRROR_GRACE_HOURS):
            reasons = "\n".join(f"- **{LABELS[p]}**: {errors.get(p, 'no matching upload found')}" for p in missing)
            body = (f"Video **#{row.number} — {row.title.replace(chr(92) + '|', '|')}** "
                    f"(https://youtu.be/{vid}) was uploaded {int(age_h)} hours ago, "
                    f"but these mirrors still aren't linked in INDEX.md:\n\n{reasons}\n\n"
                    "The sync keeps trying every run and will close this issue on its own once they're found. "
                    "To fix it by hand, add the link to this video's row in INDEX.md "
                    "(or put it in the YouTube description), and the sync will keep it.")
            meta["issue"] = open_issue(f"[archive-sync] Missing mirrors for #{row.number}: "
                                       f"{', '.join(LABELS[p] for p in missing)}", body)
        if (NOW - published).days > RECHECK_DAYS and not meta.get("issue"):
            state["videos"].pop(vid, None)

    # Tell the owner if YouTube can't be read for a while (new videos would be missed).
    if yt_error:
        log(f"YouTube: {yt_error}")
        since = parse_time(state.get("youtube_fail_since")) or NOW
        state["youtube_fail_since"] = since.isoformat()
        hours = (NOW - since).total_seconds() / 3600
        if hours >= YOUTUBE_ALERT_HOURS and not state.get("youtube_issue"):
            state["youtube_issue"] = open_issue(
                "[archive-sync] Can't read YouTube, new videos aren't being added",
                f"For about {int(hours)} hours the sync hasn't been able to read the YouTube channel:\n\n"
                f"> {yt_error}\n\nNew uploads won't be added to INDEX.md or README.md until this clears. "
                "The sync keeps trying every run and will close this issue on its own once it works again.") or True
    else:
        state.pop("youtube_fail_since", None)
        issue = state.pop("youtube_issue", None)
        if isinstance(issue, int):
            close_issue(issue, "YouTube can be read again; the sync is back to normal.")
        close_failure_issue()

    # ---- Write --------------------------------------------------------
    changed_rows = [r for r in all_rows if r.changed]
    if changed_rows or added_rows:
        first, last = row_idx[0], row_idx[-1]
        lines[first:last + 1] = [r.render() for r in all_rows]
        new_index = "\n".join(lines)
        if DRY_RUN:
            log("\n--- INDEX.md rows that would change ---")
            for r in changed_rows:
                log(r.render())
        else:
            INDEX.write_text(new_index, encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    new_readme = render_readme(readme, [r for r in all_rows if r.title != PLACEHOLDER_TITLE])
    if new_readme != readme:
        log("README.md latest uploads updated.")
        if not DRY_RUN:
            README.write_text(new_readme, encoding="utf-8")
    if json.dumps(state, sort_keys=True) != state_before and not DRY_RUN:
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    out = os.environ.get("GITHUB_OUTPUT")
    if out and SUMMARY:
        with open(out, "a") as f:
            f.write("message=Archive sync: " + ", ".join(dict.fromkeys(SUMMARY))[:200] + "\n")
    if yt_error:
        log(f"::warning::{yt_error}")
    for name, err in errors.items():
        log(f"::notice::{name}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
