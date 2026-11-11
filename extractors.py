"""Platform extractors. One plain function per platform.

Each extractor takes a URL and an httpx.Client and returns a dict:
    {"supported": True, "platform", "title", "name", "formats": [{"format", "url", "size"}]}
or raises ExtractionError(status, message) with a message safe to show the user.
"""
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

# Hosts we are willing to fetch media from (also enforced by the download route).
MEDIA_DOMAINS = (
    "giphy.com",
    "i.redd.it",
    "v.redd.it",
    "preview.redd.it",
    "tenor.com",
    "c.tenor.com",
    "video.twimg.com",
    "pbs.twimg.com",
)

# Hosts allowed as intermediate or final destinations when following short-link redirects.
APPROVED_REDIRECT_DOMAINS = (
    "giphy.com",
    "reddit.com",
    "redd.it",
    "i.redd.it",
    "v.redd.it",
    "preview.redd.it",
    "tenor.com",
    "c.tenor.com",
    "x.com",
    "twitter.com",
    "fxtwitter.com",
    "api.fxtwitter.com",
    "twimg.com",
)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GIFReaper/0.4)", "Accept": "*/*"}
REDDIT_HEADERS = {"User-Agent": "web:gifreaper:0.4 (animation downloader)", "Accept": "application/json"}

MAX_REDIRECTS = 5


class ExtractionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def make_client() -> httpx.Client:
    # Do not follow redirects automatically — callers use resolve_redirects.
    return httpx.Client(timeout=15, follow_redirects=False, headers=HEADERS)


def hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _host_matches(host: str, domains: tuple) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_media_host(url: str) -> bool:
    """True only for https URLs on explicitly allowed media hosts."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and bool(host) and _host_matches(host, MEDIA_DOMAINS)


def is_approved_redirect_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in ("http", "https") and bool(host) and _host_matches(host, APPROVED_REDIRECT_DOMAINS)


def is_public_hostname(host: str) -> bool:
    """Reject hostnames that resolve only to private, loopback, link-local, or reserved IPs (SSRF)."""
    if not host or host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def resolve_redirects(client: httpx.Client, url: str, headers: dict | None = None) -> str:
    """Follow redirects only to approved platform hosts; block private/reserved targets."""
    current = url
    for _ in range(MAX_REDIRECTS):
        if not is_approved_redirect_host(current):
            raise ExtractionError(400, "Redirect landed on a host that is not allowed.")
        host = hostname(current)
        if not is_public_hostname(host):
            raise ExtractionError(400, "Redirect target is not a public address.")
        try:
            response = client.get(current, headers=headers or HEADERS)
        except httpx.HTTPError:
            raise ExtractionError(502, "Couldn't follow this link. Try the full post URL.")
        if response.status_code not in (301, 302, 303, 307, 308):
            return str(response.url) if response.url else current
        location = response.headers.get("location")
        if not location:
            return current
        next_url = str(httpx.URL(current).join(location))
        if not is_approved_redirect_host(next_url):
            raise ExtractionError(400, "Redirect landed on a host that is not allowed.")
        current = next_url
    raise ExtractionError(400, "Too many redirects while resolving this link.")


def probe(client: httpx.Client, url: str):
    """Read only the response headers of a file. Returns (status, size) or (None, None)."""
    if not is_media_host(url):
        return None, None
    try:
        with client.stream("GET", url) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get("location")
                if location:
                    next_url = str(httpx.URL(url).join(location))
                    if is_media_host(next_url) and is_public_hostname(hostname(next_url)):
                        with client.stream("GET", next_url) as r2:
                            length = r2.headers.get("content-length", "")
                            return r2.status_code, int(length) if length.isdigit() else None
                return r.status_code, None
            length = r.headers.get("content-length", "")
            return r.status_code, int(length) if length.isdigit() else None
    except httpx.HTTPError:
        return None, None


# ---------------------------------------------------------------- Giphy

def extract_giphy(url: str, client: httpx.Client) -> dict:
    host = hostname(url)
    segs = [s for s in urlparse(url).path.split("/") if s]
    is_site = host in ("giphy.com", "www.giphy.com")
    gif_id, title = None, None

    if is_site and len(segs) >= 2 and segs[0] in ("gifs", "gif", "stickers", "embed"):
        slug = segs[1]
        gif_id = slug.rsplit("-", 1)[-1]
        if "-" in slug:
            title = slug.rsplit("-", 1)[0].replace("-", " ").title()
    elif not is_site and segs:
        if segs[0] == "media" and len(segs) >= 2:
            gif_id = segs[-2] if len(segs) >= 3 else segs[1]
        else:
            gif_id = segs[0].split(".")[0]

    if not gif_id or not re.fullmatch(r"[A-Za-z0-9]+", gif_id):
        raise ExtractionError(400, "Couldn't find a Giphy animation in this link. Paste a link to a single GIF.")

    formats, gif_status = [], None
    for ext in ("gif", "mp4"):
        media_url = f"https://media.giphy.com/media/{gif_id}/giphy.{ext}"
        status, size = probe(client, media_url)
        if ext == "gif":
            gif_status = status
        if status == 200:
            formats.append({"format": ext, "url": media_url, "size": size})

    if not formats:
        if gif_status in (404, 403):
            raise ExtractionError(404, "This Giphy animation wasn't found. It may have been removed or be private.")
        raise ExtractionError(502, "Couldn't reach Giphy. Please try again.")

    return {"supported": True, "platform": "Giphy", "title": title, "name": f"giphy-{gif_id}", "formats": formats}


# --------------------------------------------------------------- Reddit

def _direct_reddit_media(url: str, client: httpx.Client):
    """Handle direct i.redd.it / v.redd.it / preview.redd.it animation links."""
    host = hostname(url)
    path = urlparse(url).path
    if host not in ("i.redd.it", "v.redd.it", "preview.redd.it"):
        return None

    clean_path = path.split("?")[0]
    ext = clean_path.rsplit(".", 1)[-1].lower() if "." in clean_path else ""

    if host == "i.redd.it":
        if ext not in ("gif", "mp4"):
            raise ExtractionError(422, "This Reddit image is not an animation.")
    elif host == "v.redd.it":
        if ext not in ("mp4", "gif"):
            return None
    elif host == "preview.redd.it":
        if ext not in ("gif", "mp4"):
            raise ExtractionError(422, "This Reddit preview is not an animation.")

    status, size = probe(client, url)
    if status != 200:
        raise ExtractionError(404, "This Reddit file wasn't found.")
    name = clean_path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "animation"
    return {
        "supported": True,
        "platform": "Reddit",
        "title": None,
        "name": f"reddit-{name}",
        "formats": [{"format": ext, "url": url, "size": size}],
    }


def extract_reddit(url: str, client: httpx.Client) -> dict:
    host = hostname(url)
    path = urlparse(url).path

    direct = _direct_reddit_media(url, client)
    if direct is not None:
        return direct

    if host in ("redd.it", "v.redd.it") or "/s/" in path:
        try:
            resolved = resolve_redirects(client, url, headers=REDDIT_HEADERS)
        except ExtractionError:
            raise
        except httpx.HTTPError:
            raise ExtractionError(502, "Couldn't open this Reddit short link. Try the full post link.")
        path = urlparse(resolved).path
        media_again = _direct_reddit_media(resolved, client)
        if media_again is not None:
            return media_again

    if "/comments/" not in path:
        raise ExtractionError(400, "This doesn't look like a link to a Reddit post.")

    json_url = f"https://www.reddit.com{path.rstrip('/')}.json"
    if not is_approved_redirect_host(json_url) or not is_public_hostname("www.reddit.com"):
        raise ExtractionError(400, "Refusing to fetch from an unapproved host.")

    try:
        r = client.get(json_url, params={"raw_json": 1}, headers=REDDIT_HEADERS)
    except httpx.HTTPError:
        raise ExtractionError(502, "Couldn't reach Reddit. Please try again.")

    if r.status_code == 404:
        raise ExtractionError(404, "This Reddit post wasn't found. It may have been deleted.")
    if r.status_code == 403:
        raise ExtractionError(
            403,
            "Reddit is blocking this server (common on free cloud hosts like Render). "
            "Try a Giphy or Tenor link here, or run GIFReaper on your phone/PC for Reddit.",
        )
    if r.status_code == 429:
        raise ExtractionError(429, "Reddit is rate limiting us. Wait a minute and try again.")
    if r.status_code != 200:
        raise ExtractionError(502, "Reddit returned an unexpected response. Please try again.")

    try:
        post = r.json()[0]["data"]["children"][0]["data"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ExtractionError(502, "Couldn't read this Reddit post.")

    if post.get("crosspost_parent_list"):
        post = post["crosspost_parent_list"][0]
    if post.get("is_gallery"):
        raise ExtractionError(422, "This is a Reddit gallery. Paste a link to a single animation post.")

    found = {}

    def add(fmt, media_url):
        if media_url and is_media_host(media_url):
            found.setdefault(fmt, media_url)

    dest = post.get("url_overridden_by_dest") or post.get("url") or ""
    dest_host = hostname(dest)
    if dest_host == "i.redd.it" and dest.split("?")[0].lower().endswith(".gif"):
        add("gif", dest)
    elif dest_host in ("v.redd.it", "i.redd.it") and dest.split("?")[0].lower().endswith((".mp4", ".gif")):
        ext = dest.split("?")[0].rsplit(".", 1)[-1].lower()
        add(ext, dest)

    video = (post.get("media") or {}).get("reddit_video") or {}
    if video.get("is_gif"):
        add("mp4", video.get("fallback_url"))

    preview = post.get("preview") or {}
    images = preview.get("images") or []
    variants = (images[0].get("variants") or {}) if images else {}
    add("gif", ((variants.get("gif") or {}).get("source") or {}).get("url"))
    add("mp4", ((variants.get("mp4") or {}).get("source") or {}).get("url"))
    add("mp4", (preview.get("reddit_video_preview") or {}).get("fallback_url"))

    if not found:
        raise ExtractionError(
            422,
            "No animation found in this post. It may be a still image, text, or a regular video.",
        )

    formats = []
    for fmt in ("gif", "mp4"):
        if fmt in found:
            _, size = probe(client, found[fmt])
            formats.append({"format": fmt, "url": found[fmt], "size": size})

    return {
        "supported": True,
        "platform": "Reddit",
        "title": post.get("title"),
        "name": f"reddit-{post.get('id', 'animation')}",
        "formats": formats,
    }


# ---------------------------------------------------------------- Tenor

def extract_tenor(url: str, client: httpx.Client) -> dict:
    """Extract GIF/MP4 from a Tenor view page or direct media URL via og: tags."""
    host = hostname(url)
    path = urlparse(url).path

    # Direct media link
    if _host_matches(host, ("tenor.com", "c.tenor.com")) and path:
        lower = path.lower()
        if lower.endswith((".gif", ".mp4", ".webm")):
            ext = lower.rsplit(".", 1)[-1]
            if ext == "webm":
                ext = "mp4"  # treat as video download; label conservatively
            status, size = probe(client, url)
            if status != 200:
                raise ExtractionError(404, "This Tenor file wasn't found.")
            name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "tenor"
            return {
                "supported": True,
                "platform": "Tenor",
                "title": None,
                "name": f"tenor-{name}",
                "formats": [{"format": "gif" if ext == "gif" else "mp4", "url": url, "size": size}],
            }

    if "tenor.com" not in host:
        raise ExtractionError(400, "Couldn't find a Tenor animation in this link.")

    # Need a view page (or similar) with open-graph media tags
    if not path or path == "/":
        raise ExtractionError(400, "Paste a link to a specific Tenor animation.")

    try:
        r = client.get(url, headers=HEADERS)
    except httpx.HTTPError:
        raise ExtractionError(502, "Couldn't reach Tenor. Please try again.")

    if r.status_code == 404:
        raise ExtractionError(404, "This Tenor animation wasn't found.")
    if r.status_code != 200:
        raise ExtractionError(502, "Tenor returned an unexpected response. Please try again.")

    html = r.text or ""
    og_video = re.search(
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
        html,
        re.I,
    )
    og_image = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        html,
        re.I,
    )
    og_title = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        html,
        re.I,
    )

    found = {}
    if og_video:
        v = og_video.group(1)
        if is_media_host(v):
            found["mp4"] = v
    if og_image:
        img = og_image.group(1)
        # Prefer real gif over static poster jpg/png
        if is_media_host(img) and img.lower().split("?")[0].endswith(".gif"):
            found["gif"] = img

    if not found:
        raise ExtractionError(
            422,
            "No animation found on this Tenor page. Try a direct GIF link or another post.",
        )

    title = og_title.group(1).strip() if og_title else None
    # Stable name from path slug
    slug = [s for s in path.split("/") if s]
    name_part = slug[-1] if slug else "animation"
    name_part = re.sub(r"[^A-Za-z0-9._-]", "-", name_part)[:60]

    formats = []
    for fmt in ("gif", "mp4"):
        if fmt in found:
            _, size = probe(client, found[fmt])
            formats.append({"format": fmt, "url": found[fmt], "size": size})

    return {
        "supported": True,
        "platform": "Tenor",
        "title": title,
        "name": f"tenor-{name_part}",
        "formats": formats,
    }



# ---------------------------------------------------------------- X (Twitter)

def extract_x(url: str, client: httpx.Client) -> dict:
    """Extract animated media from a public X/Twitter status link via FxTwitter JSON."""
    path = urlparse(url).path
    # /user/status/ID or /i/status/ID or /status/ID
    m = re.search(r"/status/(\d+)", path)
    if not m:
        raise ExtractionError(400, "Paste a link to a single X post (…/status/123…).")
    status_id = m.group(1)

    api_url = f"https://api.fxtwitter.com/status/{status_id}"
    try:
        r = client.get(api_url, headers=HEADERS)
    except httpx.HTTPError:
        raise ExtractionError(502, "Couldn't reach X media lookup. Please try again.")

    if r.status_code == 404:
        raise ExtractionError(404, "This X post wasn't found. It may have been deleted or is private.")
    if r.status_code == 429:
        raise ExtractionError(429, "X lookup is rate limiting us. Wait a minute and try again.")
    if r.status_code != 200:
        raise ExtractionError(502, "Couldn't read this X post. Please try again.")

    try:
        payload = r.json()
        tweet = payload.get("tweet") or payload
    except (ValueError, TypeError):
        raise ExtractionError(502, "Couldn't parse this X post.")

    if not isinstance(tweet, dict):
        raise ExtractionError(502, "Couldn't parse this X post.")

    media = tweet.get("media") or {}
    items = []
    if isinstance(media, dict):
        items = list(media.get("all") or [])
        # Prefer explicit videos list as backup
        for v in media.get("videos") or []:
            if v not in items:
                items.append(v)
    elif isinstance(media, list):
        items = media

    # Keep animated only (video / gif). Still photos are skipped.
    mp4_url = None
    best_bitrate = -1
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = (item.get("type") or item.get("format") or "").lower()
        if kind not in ("video", "gif", "animated_gif", "video/mp4"):
            # Some payloads only set format
            if "video" not in kind and "gif" not in kind:
                continue
        # Pick best mp4 from formats list
        formats = item.get("formats") or []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if (fmt.get("container") or "").lower() != "mp4":
                continue
            u = fmt.get("url") or ""
            if not u or not is_media_host(u):
                continue
            br = int(fmt.get("bitrate") or 0)
            if br >= best_bitrate:
                best_bitrate = br
                mp4_url = u
        # Fallback: top-level url
        if mp4_url is None:
            u = item.get("url") or ""
            if u and is_media_host(u) and ".mp4" in u.lower():
                mp4_url = u

    if not mp4_url:
        raise ExtractionError(
            422,
            "No animation found in this X post. It may be text or still images only.",
        )

    _, size = probe(client, mp4_url)
    title = (tweet.get("text") or "").strip() or None
    if title and len(title) > 120:
        title = title[:117] + "…"
    author = ((tweet.get("author") or {}) or {}).get("screen_name") or ((tweet.get("author") or {}) or {}).get("name")
    name = f"x-{status_id}"

    return {
        "supported": True,
        "platform": "X",
        "title": title,
        "name": name,
        "formats": [{"format": "mp4", "url": mp4_url, "size": size}],
    }


# ----------------------------------------------------------- dispatcher

EXTRACTORS = {"Giphy": extract_giphy, "Reddit": extract_reddit, "Tenor": extract_tenor, "X": extract_x}


def extract(url: str, platform: str, client: httpx.Client) -> dict:
    fn = EXTRACTORS.get(platform)
    if fn is None:
        raise ExtractionError(
            501,
            f"{platform} links are recognized, but downloading from {platform} isn't available yet. "
            "Giphy, Reddit, Tenor, and X work for now.",
        )
    try:
        return fn(url, client)
    except ExtractionError:
        raise
    except httpx.HTTPError:
        raise ExtractionError(502, f"Couldn't reach {platform}. Please try again.")
