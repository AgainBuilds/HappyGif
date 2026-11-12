
"""HappyGif platform extractors.

Each extractor takes a URL and an httpx.Client and returns:
    {
        "supported": True,
        "platform": "...",
        "title": "...",
        "name": "...",
        "formats": [
            {"format": "gif", "url": "...", "size": 12345}
        ]
    }

Errors are raised as ExtractionError with a status code
and a message safe to show to the user.
"""

import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx


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


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HappyGif/1.0)",
    "Accept": "*/*",
}

REDDIT_HEADERS = {
    "User-Agent": "web:happygif:1.0 (animation downloader)",
    "Accept": "application/json",
}

MAX_REDIRECTS = 5


class ExtractionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def make_client() -> httpx.Client:
    # Redirects are handled manually by resolve_redirects.
    return httpx.Client(
        timeout=15,
        follow_redirects=False,
        headers=HEADERS
    )


def hostname(url: str) -> str:
    return (
        urlparse(url).hostname or ""
    ).lower().rstrip(".")


def _host_matches(host: str, domains: tuple) -> bool:
    return any(
        host == domain or host.endswith("." + domain)
        for domain in domains
    )


def is_media_host(url: str) -> bool:
    """Only allow HTTPS URLs on explicitly approved media hosts."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")

    return (
        parsed.scheme == "https"
        and bool(host)
        and _host_matches(host, MEDIA_DOMAINS)
    )


def is_approved_redirect_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")

    return (
        parsed.scheme in ("http", "https")
        and bool(host)
        and _host_matches(host, APPROVED_REDIRECT_DOMAINS)
    )


def is_public_hostname(host: str) -> bool:
    """Reject private, loopback, link-local and reserved IP addresses."""

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


def resolve_redirects(
    client: httpx.Client,
    url: str,
    headers: dict | None = None
) -> str:
    """Follow approved platform redirects only."""

    current = url

    for _ in range(MAX_REDIRECTS):
        if not is_approved_redirect_host(current):
            raise ExtractionError(
                400,
                "Redirect landed on a host that is not allowed."
            )

        host = hostname(current)

        if not is_public_hostname(host):
            raise ExtractionError(
                400,
                "Redirect target is not a public address."
            )

        try:
            response = client.get(
                current,
                headers=headers or HEADERS
            )
        except httpx.HTTPError:
            raise ExtractionError(
                502,
                "Couldn't follow this link. Try the full post URL."
            )

        if response.status_code not in (
            301, 302, 303, 307, 308
        ):
            return str(response.url) if response.url else current

        location = response.headers.get("location")

        if not location:
            return current

        next_url = str(
            httpx.URL(current).join(location)
        )

        if not is_approved_redirect_host(next_url):
            raise ExtractionError(
                400,
                "Redirect landed on a host that is not allowed."
            )

        current = next_url

    raise ExtractionError(
        400,
        "Too many redirects while resolving this link."
    )


def probe(client: httpx.Client, url: str):
    """Check a media file and return its status and size."""

    if not is_media_host(url):
        return None, None

    try:
        with client.stream("GET", url) as response:

            if response.status_code in (
                301, 302, 303, 307, 308
            ):
                location = response.headers.get("location")

                if location:
                    next_url = str(
                        httpx.URL(url).join(location)
                    )

                    if (
                        is_media_host(next_url)
                        and is_public_hostname(hostname(next_url))
                    ):
                        with client.stream(
                            "GET",
                            next_url
                        ) as response2:

                            length = response2.headers.get(
                                "content-length",
                                ""
                            )

                            return (
                                response2.status_code,
                                int(length) if length.isdigit() else None
                            )

                return response.status_code, None

            length = response.headers.get(
                "content-length",
                ""
            )

            return (
                response.status_code,
                int(length) if length.isdigit() else None
            )

    except httpx.HTTPError:
        return None, None


# ------------------------------------------------------------
# GIPHY
# ------------------------------------------------------------

def extract_giphy(
    url: str,
    client: httpx.Client
) -> dict:

    host = hostname(url)

    segs = [
        segment
        for segment in urlparse(url).path.split("/")
        if segment
    ]

    is_site = host in (
        "giphy.com",
        "www.giphy.com"
    )

    gif_id = None
    title = None

    if (
        is_site
        and len(segs) >= 2
        and segs[0] in (
            "gifs",
            "gif",
            "stickers",
            "embed"
        )
    ):
        slug = segs[1]
        gif_id = slug.rsplit("-", 1)[-1]

        if "-" in slug:
            title = (
                slug.rsplit("-", 1)[0]
                .replace("-", " ")
                .title()
            )

    elif not is_site and segs:

        if segs[0] == "media" and len(segs) >= 2:
            gif_id = (
                segs[-2]
                if len(segs) >= 3
                else segs[1]
            )
        else:
            gif_id = segs[0].split(".")[0]

    if (
        not gif_id
        or not re.fullmatch(r"[A-Za-z0-9]+", gif_id)
    ):
        raise ExtractionError(
            400,
            "Couldn't find a Giphy animation in this link. "
            "Paste a link to a single GIF."
        )

    formats = []
    gif_status = None

    for ext in ("gif", "mp4"):

        media_url = (
            f"https://media.giphy.com/media/"
            f"{gif_id}/giphy.{ext}"
        )

        status, size = probe(client, media_url)

        if ext == "gif":
            gif_status = status

        if status == 200:
            formats.append({
                "format": ext,
                "url": media_url,
                "size": size
            })

    if not formats:

        if gif_status in (404, 403):
            raise ExtractionError(
                404,
                "This Giphy animation wasn't found. "
                "It may have been removed or be private."
            )

        raise ExtractionError(
            502,
            "Couldn't reach Giphy. Please try again."
        )

    return {
        "supported": True,
        "platform": "Giphy",
        "title": title,
        "name": f"giphy-{gif_id}",
        "formats": formats
    }


# ------------------------------------------------------------
# REDDIT
# ------------------------------------------------------------

def _direct_reddit_media(
    url: str,
    client: httpx.Client
):
    """Handle direct Reddit animation URLs."""

    host = hostname(url)
    path = urlparse(url).path

    if host not in (
        "i.redd.it",
        "v.redd.it",
        "preview.redd.it"
    ):
        return None

    clean_path = path.split("?")[0]

    ext = (
        clean_path.rsplit(".", 1)[-1].lower()
        if "." in clean_path
        else ""
    )

    if host == "i.redd.it":

        if ext not in ("gif", "mp4"):
            raise ExtractionError(
                422,
                "This Reddit image is not an animation."
            )

    elif host == "v.redd.it":

        if ext not in ("mp4", "gif"):
            return None

    elif host == "preview.redd.it":

        if ext not in ("gif", "mp4"):
            raise ExtractionError(
                422,
                "This Reddit preview is not an animation."
            )

    status, size = probe(client, url)

    if status != 200:
        raise ExtractionError(
            404,
            "This Reddit file wasn't found."
        )

    name = (
        clean_path.rsplit("/", 1)[-1]
        .rsplit(".", 1)[0]
        or "animation"
    )

    return {
        "supported": True,
        "platform": "Reddit",
        "title": None,
        "name": f"reddit-{name}",
        "formats": [{
            "format": ext,
            "url": url,
            "size": size
        }]
    }


def extract_reddit(
    url: str,
    client: httpx.Client
) -> dict:

    host = hostname(url)
    path = urlparse(url).path

    direct = _direct_reddit_media(url, client)

    if direct is not None:
        return direct

    if host in ("redd.it", "v.redd.it") or "/s/" in path:

        try:
            resolved = resolve_redirects(
                client,
                url,
                headers=REDDIT_HEADERS
            )

        except ExtractionError:
            raise

        except httpx.HTTPError:
            raise ExtractionError(
                502,
                "Couldn't open this Reddit short link. "
                "Try the full post link."
            )

        path = urlparse(resolved).path

        media_again = _direct_reddit_media(
            resolved,
            client
        )

        if media_again is not None:
            return media_again

    if "/comments/" not in path:
        raise ExtractionError(
            400,
            "This doesn't look like a link to a Reddit post."
        )

    json_url = (
        f"https://www.reddit.com"
        f"{path.rstrip('/')}.json"
    )

    if (
        not is_approved_redirect_host(json_url)
        or not is_public_hostname("www.reddit.com")
    ):
        raise ExtractionError(
            400,
            "Refusing to fetch from an unapproved host."
        )

    try:
        response = client.get(
            json_url,
            params={"raw_json": 1},
            headers=REDDIT_HEADERS
        )

    except httpx.HTTPError:
        raise ExtractionError(
            502,
            "Couldn't reach Reddit. Please try again."
        )

    if response.status_code == 404:
        raise ExtractionError(
            404,
            "This Reddit post wasn't found. "
            "It may have been deleted."
        )

    if response.status_code == 403:
        raise ExtractionError(
            403,
            "Reddit is blocking this server. "
            "This can happen on cloud hosts such as Render. "
            "Try another supported source."
        )

    if response.status_code == 429:
        raise ExtractionError(
            429,
            "Reddit is rate limiting us. "
            "Wait a minute and try again."
        )

    if response.status_code != 200:
        raise ExtractionError(
            502,
            "Reddit returned an unexpected response. "
            "Please try again."
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

    if post.get("crosspost_parent_list"):
        post = post["crosspost_parent_list"][0]

    if post.get("is_gallery"):
        raise ExtractionError(
            422,
            "This is a Reddit gallery. "
            "Paste a link to a single animation post."
        )

    found = {}

    def add(fmt, media_url):
        if media_url and is_media_host(media_url):
            found.setdefault(fmt, media_url)

    dest = (
        post.get("url_overridden_by_dest")
        or post.get("url")
        or ""
    )

    dest_host = hostname(dest)

    if (
        dest_host == "i.redd.it"
        and dest.split("?")[0].lower().endswith(".gif")
    ):
        add("gif", dest)

    elif (
        dest_host in ("v.redd.it", "i.redd.it")
        and dest.split("?")[0].lower().endswith(
            (".mp4", ".gif")
        )
    ):
        ext = (
            dest.split("?")[0]
            .rsplit(".", 1)[-1]
            .lower()
        )

        add(ext, dest)

    video = (
        post.get("media") or {}
    ).get("reddit_video") or {}

    if video.get("is_gif"):
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

    add(
        "mp4",
        (
            preview.get("reddit_video_preview") or {}
        ).get("fallback_url")
    )

    if not found:
        raise ExtractionError(
            422,
            "No animation found in this post. "
            "It may be a still image, text, or a regular video."
        )

    formats = []

    for fmt in ("gif", "mp4"):

        if fmt in found:
            _, size = probe(client, found[fmt])

            formats.append({
                "format": fmt,
                "url": found[fmt],
                "size": size
            })

    return {
        "supported": True,
        "platform": "Reddit",
        "title": post.get("title"),
        "name": f"reddit-{post.get('id', 'animation')}",
        "formats": formats
    }


# ------------------------------------------------------------
# TENOR
# ------------------------------------------------------------

def extract_tenor(
    url: str,
    client: httpx.Client
) -> dict:
    """Extract GIF/MP4 from a Tenor page or direct media URL."""

    host = hostname(url)
    path = urlparse(url).path

    # Direct media link
    if (
        _host_matches(host, ("tenor.com", "c.tenor.com"))
        and path
    ):

        lower = path.lower()

        if lower.endswith((".gif", ".mp4", ".webm")):

            ext = lower.rsplit(".", 1)[-1]

            if ext == "webm":
                ext = "mp4"

            status, size = probe(client, url)

            if status != 200:
                raise ExtractionError(
                    404,
                    "This Tenor file wasn't found."
                )

            name = (
                path.rsplit("/", 1)[-1]
                .rsplit(".", 1)[0]
                or "tenor"
            )

            return {
                "supported": True,
                "platform": "Tenor",
                "title": None,
                "name": f"tenor-{name}",
                "formats": [{
                    "format": "gif" if ext == "gif" else "mp4",
                    "url": url,
                    "size": size
                }]
            }

    if "tenor.com" not in host:
        raise ExtractionError(
            400,
            "Couldn't find a Tenor animation in this link."
        )

    if not path or path == "/":
        raise ExtractionError(
            400,
            "Paste a link to a specific Tenor animation."
        )

    try:
        response = client.get(
            url,
            headers=HEADERS
        )

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
            "Tenor returned an unexpected response. "
            "Please try again."
        )

    html = response.text or ""

    og_video = re.search(
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
        html,
        re.I
    )

    og_image = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        html,
        re.I
    )

    og_title = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        html,
        re.I
    )

    found = {}

    if og_video:
        video_url = og_video.group(1)

        if is_media_host(video_url):
            found["mp4"] = video_url

    if og_image:
        image_url = og_image.group(1)

        if (
            is_media_host(image_url)
            and image_url.lower().split("?")[0].endswith(".gif")
        ):
            found["gif"] = image_url

    if not found:
        raise ExtractionError(
            422,
            "No animation found on this Tenor page. "
            "Try a direct GIF link or another post."
        )

    title = (
        og_title.group(1).strip()
        if og_title
        else None
    )

    slug = [
        segment
        for segment in path.split("/")
        if segment
    ]

    name_part = slug[-1] if slug else "animation"

    name_part = re.sub(
        r"[^A-Za-z0-9._-]",
        "-",
        name_part
    )[:60]

    formats = []

    for fmt in ("gif", "mp4"):

        if fmt in found:
            _, size = probe(client, found[fmt])

            formats.append({
                "format": fmt,
                "url": found[fmt],
                "size": size
            })

    return {
        "supported": True,
        "platform": "Tenor",
        "title": title,
        "name": f"tenor-{name_part}",
        "formats": formats
    }


# ------------------------------------------------------------
# X / TWITTER
# ------------------------------------------------------------

def extract_x(
    url: str,
    client: httpx.Client
) -> dict:
    """Extract animated media from a public X post."""

    path = urlparse(url).path

    match = re.search(
        r"/status/(\d+)",
        path
    )

    if not match:
        raise ExtractionError(
            400,
            "Paste a link to a single X post."
        )

    status_id = match.group(1)

    api_url = (
        f"https://api.fxtwitter.com/status/{status_id}"
    )

    try:
        response = client.get(
            api_url,
            headers=HEADERS
        )

    except httpx.HTTPError:
        raise ExtractionError(
            502,
            "Couldn't reach X media lookup. Please try again."
        )

    if response.status_code == 404:
        raise ExtractionError(
            404,
            "This X post wasn't found. It may have been deleted or is private."
        )

    if response.status_code == 429:
        raise ExtractionError(
            429,
            "X lookup is rate limiting us. Wait a minute and try again."
        )

    if response.status_code != 200:
        raise ExtractionError(
            502,
            "Couldn't read this X post. Please try again."
        )

    try:
        payload = response.json()
        tweet = payload.get("tweet") or payload

    except (ValueError, TypeError):
        raise ExtractionError(
            502,
            "Couldn't parse this X post."
        )

    if not isinstance(tweet, dict):
        raise ExtractionError(
            502,
            "Couldn't parse this X post."
        )

    media = tweet.get("media") or {}

    items = []

    if isinstance(media, dict):

        items = list(media.get("all") or [])

        for video in media.get("videos") or []:
            if video not in items:
                items.append(video)

    elif isinstance(media, list):
        items = media

    mp4_url = None
    best_bitrate = -1

    for item in items:

        if not isinstance(item, dict):
            continue

        kind = (
            item.get("type")
            or item.get("format")
            or ""
        ).lower()

        if kind not in (
            "video",
            "gif",
            "animated_gif",
            "video/mp4"
        ):

            if (
                "video" not in kind
                and "gif" not in kind
            ):
                continue

        formats = item.get("formats") or []

        for fmt in formats:

            if not isinstance(fmt, dict):
                continue

            if (
                fmt.get("container") or ""
            ).lower() != "mp4":
                continue

            media_url = fmt.get("url") or ""

            if (
                not media_url
                or not is_media_host(media_url)
            ):
                continue

            bitrate = int(
                fmt.get("bitrate") or 0
            )

            if bitrate >= best_bitrate:
                best_bitrate = bitrate
                mp4_url = media_url

        if mp4_url is None:

            media_url = item.get("url") or ""

            if (
                media_url
                and is_media_host(media_url)
                and ".mp4" in media_url.lower()
            ):
                mp4_url = media_url

    if not mp4_url:
        raise ExtractionError(
            422,
            "No animation found in this X post. "
            "It may be text or still images only."
        )

    _, size = probe(client, mp4_url)

    title = (
        tweet.get("text") or ""
    ).strip() or None

    if title and len(title) > 120:
        title = title[:117] + "…"

    name = f"x-{status_id}"

    return {
        "supported": True,
        "platform": "X",
        "title": title,
        "name": name,
        "formats": [{
            "format": "mp4",
            "url": mp4_url,
            "size": size
        }]
    }


# ------------------------------------------------------------
# EXTRACTOR DISPATCHER
# ------------------------------------------------------------

EXTRACTORS = {
    "Giphy": extract_giphy,
    "Reddit": extract_reddit,
    "Tenor": extract_tenor,
    "X": extract_x
}


def extract(
    url: str,
    platform: str,
    client: httpx.Client
) -> dict:

    extractor = EXTRACTORS.get(platform)

    if extractor is None:
        raise ExtractionError(
            501,
            f"{platform} links are recognized, but downloading "
            f"from {platform} isn't available yet. "
            "Giphy, Reddit, Tenor, and X work for now."
        )

    try:
        return extractor(url, client)

    except ExtractionError:
        raise

    except httpx.HTTPError:
        raise ExtractionError(
            502,
            f"Couldn't reach {platform}. Please try again."
        )
