"""HappyGif public-link media extraction.

Uses yt-dlp's maintained platform extractors for public posts. It does not
use account cookies, bypass private-post restrictions, or download playlists.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx
import yt_dlp
from yt_dlp.utils import DownloadError


PLATFORM_DOMAINS = {
    "Giphy": ("giphy.com",),
    "Reddit": ("reddit.com", "redd.it"),
    "Tenor": ("tenor.com",),
    "X": ("x.com", "twitter.com"),
    "Instagram": ("instagram.com",),
    "TikTok": ("tiktok.com",),
    "Pinterest": ("pinterest.com", "pin.it"),
}

# Media is proxied through /api/file. Keep this list narrow: never proxy an
# arbitrary URL supplied by a browser client.
MEDIA_DOMAINS = (
    "media.giphy.com", "giphy.com",
    "i.redd.it", "v.redd.it", "preview.redd.it",
    "external-preview.redd.it", "redditmedia.com",
    "tenor.com", "c.tenor.com", "tenor.co",
    "video.twimg.com", "pbs.twimg.com", "twimg.com",
    "cdninstagram.com", "fbcdn.net",
    "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com",
    "byteoversea.com", "ibytedtos.com", "muscdn.com",
    "tiktokv.com", "webapp.tiktok.com",
    "v16-webapp.tiktok.com", "v19-webapp.tiktok.com",
    "pinimg.com",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; HappyGif/2.0; "
        "+https://happygif.onrender.com)"
    ),
    "Accept": "*/*",
}


class ExtractionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=20,
        follow_redirects=False,
        headers=HEADERS
    )


def hostname(url: str) -> str:
    return (
        urlparse(url).hostname or ""
    ).lower().rstrip(".")


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(
        host == domain or host.endswith("." + domain)
        for domain in domains
    )


def platform_for_url(url: str) -> str | None:
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None

    host = parsed.hostname.lower().rstrip(".")

    for platform, domains in PLATFORM_DOMAINS.items():
        if _host_matches(host, domains):
            return platform

    return None


def is_public_hostname(host: str) -> bool:
    """Reject private, loopback, link-local and reserved destinations."""

    if not host or host == "localhost":
        return False

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, None)
            ]
        except (socket.gaierror, ValueError, IndexError):
            return False

    return bool(addresses) and all(
        not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
        for ip in addresses
    )


def is_media_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")

    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
        and bool(host)
        and _host_matches(host, MEDIA_DOMAINS)
    )


class _QuietLogger:
    def debug(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass


def _clean_name(value: str) -> str:
    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        value or "animation"
    )

    return value.strip("-._")[:70] or "animation"


def _candidate_formats(info: dict) -> list[dict]:
    candidates: dict[str, dict] = {}
    raw_formats = info.get("formats") or []

    # Some extractors expose only the selected format at the top level.
    if info.get("url"):
        raw_formats = list(raw_formats) + [{
            "url": info.get("url"),
            "ext": info.get("ext"),
            "format_id": info.get("format_id"),
            "height": info.get("height") or 0,
            "filesize": (
                info.get("filesize")
                or info.get("filesize_approx")
            ),
            "vcodec": info.get("vcodec"),
        }]

    for item in raw_formats:
        if not isinstance(item, dict):
            continue

        ext = (item.get("ext") or "").lower()

        if ext not in ("gif", "mp4"):
            continue

        if ext == "mp4" and item.get("vcodec") == "none":
            continue

        media_url = item.get("url") or ""

        if not is_media_host(media_url):
            continue

        current = candidates.get(ext)

        score = (
            int(item.get("height") or 0),
            float(item.get("tbr") or 0)
        )

        old_score = (
            current.get("_score", (-1, -1))
            if current
            else (-1, -1)
        )

        if current is None or score >= old_score:
            candidates[ext] = {
                "format": ext,
                "url": media_url,
                "size": (
                    item.get("filesize")
                    or item.get("filesize_approx")
                ),
                "_score": score,
            }

    # GIF and MP4 are the only formats the current frontend handles.
    result = []

    for ext in ("gif", "mp4"):
        item = candidates.get(ext)

        if item:
            item.pop("_score", None)
            result.append(item)

    return result


def _extract_with_ytdlp(
    url: str,
    platform: str,
    client: httpx.Client | None = None
) -> dict:
    """Resolve one public post into downloadable GIF/MP4 formats."""

    actual_platform = platform_for_url(url)

    if not actual_platform or actual_platform != platform:
        raise ExtractionError(
            400,
            "Paste a valid public link from a supported platform."
        )

    parsed = urlparse(url)

    if (
        parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 443)
    ):
        raise ExtractionError(400, "That link is not valid.")

    if not is_public_hostname(parsed.hostname or ""):
        raise ExtractionError(
            400,
            "That link does not point to a public website."
        )

    options = {
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 1,
        "ignoreerrors": False,
        "nocheckcertificate": False,
        "cachedir": False,
        "http_headers": HEADERS,
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

    except DownloadError as exc:
        detail = str(exc).lower()

        if any(
            word in detail
            for word in (
                "private",
                "login",
                "sign in",
                "authentication"
            )
        ):
            raise ExtractionError(
                403,
                "This post is private or requires a login. "
                "HappyGif only extracts publicly accessible posts."
            )

        if (
            "unsupported url" in detail
            or "not a valid url" in detail
        ):
            raise ExtractionError(
                422,
                f"{platform} could not resolve this link. "
                "Check that it points to a specific public post."
            )

        raise ExtractionError(
            422,
            f"{platform} could not provide downloadable media "
            "for this post. It may be unavailable or access "
            "may be restricted."
        )

    except Exception:
        raise ExtractionError(
            502,
            f"HappyGif couldn't process this {platform} link "
            "right now. Please try again later."
        )

    if (
        not isinstance(info, dict)
        or info.get("_type") in ("playlist", "multi_video")
    ):
        raise ExtractionError(
            422,
            "Paste a link to one post or animation, not a collection."
        )

    formats = _candidate_formats(info)

    if not formats:
        raise ExtractionError(
            422,
            f"No downloadable GIF or MP4 was exposed by this "
            f"{platform} post. It may contain only still images, "
            "or the platform may restrict access."
        )

    title = (
        info.get("title")
        or info.get("description")
        or ""
    ).strip()

    if len(title) > 160:
        title = title[:157] + "..."

    identifier = _clean_name(
        str(
            info.get("id")
            or info.get("display_id")
            or "animation"
        )
    )

    return {
        "supported": True,
        "platform": platform,
        "title": title or None,
        "name": _clean_name(
            f"{platform.lower()}-{identifier}"
        ),
        "formats": formats,
    }


# Keep the original HappyGif-specific Giphy and Tenor resolvers.
# These two services are not consistently covered by yt-dlp's
# dedicated extractors.


def _probe(client: httpx.Client, media_url: str):
    if not is_media_host(media_url):
        return None, None

    try:
        with client.stream("GET", media_url) as response:
            length = response.headers.get("content-length", "")

            return (
                response.status_code,
                int(length) if length.isdigit() else None
            )

    except httpx.HTTPError:
        return None, None


def _extract_giphy(url: str, client: httpx.Client) -> dict:
    parts = [
        part
        for part in urlparse(url).path.split("/")
        if part
    ]

    host = hostname(url)
    gif_id = None
    title = None

    if (
        host.endswith("giphy.com")
        and len(parts) >= 2
        and parts[0] in (
            "gifs",
            "gif",
            "stickers",
            "embed"
        )
    ):
        slug = parts[1]
        gif_id = slug.rsplit("-", 1)[-1]

        if "-" in slug:
            title = (
                slug.rsplit("-", 1)[0]
                .replace("-", " ")
                .title()
            )

    elif parts:
        if parts[0] == "media" and len(parts) >= 2:
            gif_id = (
                parts[-2]
                if len(parts) >= 3
                else parts[1]
            )
        else:
            gif_id = parts[0].split(".")[0]

    if (
        not gif_id
        or not re.fullmatch(r"[A-Za-z0-9]+", gif_id)
    ):
        raise ExtractionError(
            400,
            "Paste a link to one specific Giphy animation."
        )

    formats = []

    for ext in ("gif", "mp4"):
        media_url = (
            f"https://media.giphy.com/media/"
            f"{gif_id}/giphy.{ext}"
        )

        status, size = _probe(client, media_url)

        if status == 200:
            formats.append({
                "format": ext,
                "url": media_url,
                "size": size
            })

    if not formats:
        raise ExtractionError(
            404,
            "This Giphy animation could not be accessed."
        )

    return {
        "supported": True,
        "platform": "Giphy",
        "title": title,
        "name": f"giphy-{gif_id}",
        "formats": formats
    }


def _extract_tenor(url: str, client: httpx.Client) -> dict:
    host = hostname(url)
    path = urlparse(url).path

    if (
        not _host_matches(host, ("tenor.com",))
        or path in ("", "/")
    ):
        raise ExtractionError(
            400,
            "Paste a link to one specific Tenor animation."
        )

    direct_ext = (
        path.lower().rsplit(".", 1)[-1]
        if "." in path
        else ""
    )

    if direct_ext in ("gif", "mp4"):
        status, size = _probe(client, url)

        if status != 200:
            raise ExtractionError(
                404,
                "This Tenor media file wasn't found."
            )

        return {
            "supported": True,
            "platform": "Tenor",
            "title": None,
            "name": "tenor-animation",
            "formats": [{
                "format": direct_ext,
                "url": url,
                "size": size
            }]
        }

    try:
        response = client.get(url, headers=HEADERS)

    except httpx.HTTPError:
        raise ExtractionError(
            502,
            "Couldn't reach Tenor. Please try again."
        )

    if response.status_code == 404:
        raise ExtractionError(
            404,
            "This Tenor animation wasn't found."
        )

    if response.status_code != 200:
        raise ExtractionError(
            502,
            "Tenor did not return the animation page."
        )

    html = response.text or ""

    def meta_value(prop):
        pattern = (
            rf'<meta[^>]+(?:property|name)='
            rf'["\']{re.escape(prop)}["\'][^>]+content='
            rf'["\']([^"\']+)["\']'
        )

        match = re.search(pattern, html, re.I)

        if not match:
            pattern = (
                rf'<meta[^>]+content=["\']([^"\']+)["\']'
                rf'[^>]+(?:property|name)='
                rf'["\']{re.escape(prop)}["\']'
            )

            match = re.search(pattern, html, re.I)

        return match.group(1) if match else None

    found = {}

    video_url = (
        meta_value("og:video")
        or meta_value("og:video:url")
    )

    image_url = meta_value("og:image")

    if video_url and is_media_host(video_url):
        found["mp4"] = video_url

    if (
        image_url
        and is_media_host(image_url)
        and image_url.lower().split("?")[0].endswith(".gif")
    ):
        found["gif"] = image_url

    formats = []

    for ext in ("gif", "mp4"):
        if ext in found:
            _, size = _probe(client, found[ext])

            formats.append({
                "format": ext,
                "url": found[ext],
                "size": size
            })

    if not formats:
        raise ExtractionError(
            422,
            "No downloadable animation was exposed by this Tenor page."
        )

    slug = re.sub(
        r"[^A-Za-z0-9._-]",
        "-",
        path.strip("/").split("/")[-1]
    )[:60]

    return {
        "supported": True,
        "platform": "Tenor",
        "title": meta_value("og:title"),
        "name": f"tenor-{slug or 'animation'}",
        "formats": formats
    }


def _extract_reddit_fallback(
    url: str,
    client: httpx.Client
) -> dict:
    path = urlparse(url).path

    if "/comments/" not in path:
        raise ExtractionError(
            400,
            "Paste a link to one Reddit post."
        )

    json_url = (
        f"https://www.reddit.com"
        f"{path.rstrip('/')}.json"
    )

    try:
        response = client.get(
            json_url,
            params={"raw_json": 1},
            headers={
                "User-Agent": (
                    "web:happygif:2.0 "
                    "(public media extractor)"
                )
            }
        )

    except httpx.HTTPError:
        raise ExtractionError(
            502,
            "Couldn't reach Reddit. Please try again."
        )

    if response.status_code != 200:
        raise ExtractionError(
            502,
            "Reddit did not make this post available to HappyGif."
        )

    try:
        post = (
            response.json()[0]["data"]
            ["children"][0]["data"]
        )

    except (
        ValueError,
        KeyError,
        IndexError,
        TypeError
    ):
        raise ExtractionError(
            502,
            "Couldn't read this Reddit post."
        )

    found = {}

    def add(ext, media_url):
        if media_url and is_media_host(media_url):
            found.setdefault(ext, media_url)

    dest = (
        post.get("url_overridden_by_dest")
        or post.get("url")
        or ""
    )

    if dest.lower().split("?")[0].endswith(".gif"):
        add("gif", dest)

    video = (
        (post.get("media") or {})
        .get("reddit_video") or {}
    )

    if video.get("is_gif") or video.get("fallback_url"):
        add("mp4", video.get("fallback_url"))

    preview = post.get("preview") or {}
    images = preview.get("images") or []

    variants = (
        images[0].get("variants") or {}
        if images
        else {}
    )

    add(
        "gif",
        (
            (variants.get("gif") or {})
            .get("source") or {}
        ).get("url")
    )

    add(
        "mp4",
        (
            (variants.get("mp4") or {})
            .get("source") or {}
        ).get("url")
    )

    if not found:
        raise ExtractionError(
            422,
            "No downloadable GIF or MP4 was found in this Reddit post."
        )

    formats = []

    for ext in ("gif", "mp4"):
        if ext in found:
            _, size = _probe(client, found[ext])

            formats.append({
                "format": ext,
                "url": found[ext],
                "size": size
            })

    return {
        "supported": True,
        "platform": "Reddit",
        "title": post.get("title"),
        "name": f"reddit-{post.get('id', 'animation')}",
        "formats": formats
    }


def _extract_x_fallback(url: str, client: httpx.Client) -> dict:
    match = re.search(
        r"/status/(\d+)",
        urlparse(url).path
    )

    if not match:
        raise ExtractionError(
            400,
            "Paste a link to one X post."
        )

    status_id = match.group(1)

    try:
        response = client.get(
            f"https://api.fxtwitter.com/status/{status_id}",
            headers=HEADERS
        )

    except httpx.HTTPError:
        raise ExtractionError(
            502,
            "Couldn't reach X media lookup."
        )

    if response.status_code != 200:
        raise ExtractionError(
            502,
            "X did not make this post available to HappyGif."
        )

    try:
        payload = response.json()
        tweet = payload.get("tweet") or payload

    except (ValueError, TypeError):
        raise ExtractionError(
            502,
            "Couldn't read this X post."
        )

    media = tweet.get("media") or {}

    items = (
        list(media.get("all") or [])
        if isinstance(media, dict)
        else media if isinstance(media, list)
        else []
    )

    best_url = None
    best_rate = -1

    for item in items:
        if not isinstance(item, dict):
            continue

        for fmt in item.get("formats") or []:
            if (
                not isinstance(fmt, dict)
                or (fmt.get("container") or "").lower() != "mp4"
            ):
                continue

            candidate = fmt.get("url") or ""

            if (
                is_media_host(candidate)
                and int(fmt.get("bitrate") or 0) >= best_rate
            ):
                best_rate = int(fmt.get("bitrate") or 0)
                best_url = candidate

        candidate = item.get("url") or ""

        if (
            not best_url
            and is_media_host(candidate)
            and ".mp4" in candidate.lower()
        ):
            best_url = candidate

    if not best_url:
        raise ExtractionError(
            422,
            "No downloadable animation was found in this X post."
        )

    _, size = _probe(client, best_url)
    title = (tweet.get("text") or "").strip() or None

    return {
        "supported": True,
        "platform": "X",
        "title": title,
        "name": f"x-{status_id}",
        "formats": [{
            "format": "mp4",
            "url": best_url,
            "size": size
        }]
    }


def extract(
    url: str,
    platform: str,
    client: httpx.Client | None = None
) -> dict:
    """Use legacy HappyGif resolvers for Giphy/Tenor and yt-dlp for others."""

    if client is None:
        with make_client() as owned_client:
            return extract(url, platform, owned_client)

    if platform == "Giphy":
        return _extract_giphy(url, client)

    if platform == "Tenor":
        try:
            return _extract_tenor(url, client)
        except ExtractionError:
            # Some Tenor pages expose video metadata more reliably
            # through yt-dlp's generic extractor.
            return _extract_with_ytdlp(url, platform, client)

    if platform == "Reddit":
        try:
            return _extract_with_ytdlp(url, platform, client)
        except ExtractionError:
            return _extract_reddit_fallback(url, client)

    if platform == "X":
        try:
            return _extract_with_ytdlp(url, platform, client)
        except ExtractionError:
            return _extract_x_fallback(url, client)

    if platform in ("Instagram", "TikTok", "Pinterest"):
        return _extract_with_ytdlp(url, platform, client)

    raise ExtractionError(
        501,
        "This platform is not supported yet."
    )
