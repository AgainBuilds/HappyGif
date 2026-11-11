from pathlib import Path
from urllib.parse import urlparse

import re

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl

from extractors import ExtractionError, extract, is_media_host, make_client

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="GIFReaper", version="0.6.0")


PLATFORMS = {
    "giphy.com": "Giphy",
    "reddit.com": "Reddit",
    "redd.it": "Reddit",
    "tenor.com": "Tenor",
    "x.com": "X",
    "twitter.com": "X",
    "pinterest.com": "Pinterest",
    "pin.it": "Pinterest",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "vm.tiktok.com": "TikTok",
}

EXTRACTABLE = {"Giphy", "Reddit", "Tenor", "X"}


class DetectRequest(BaseModel):
    url: HttpUrl


def detect_platform(url: str):
    hostname = (urlparse(str(url)).hostname or "").lower().rstrip(".")
    for domain, platform in PLATFORMS.items():
        if hostname == domain or hostname.endswith("." + domain):
            return platform
    return None


@app.get("/", include_in_schema=False)
def homepage():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/style.css", include_in_schema=False)
def css():
    return FileResponse(BASE_DIR / "style.css", media_type="text/css")


@app.get("/app.js", include_in_schema=False)
def js():
    return FileResponse(BASE_DIR / "app.js", media_type="application/javascript")


@app.post("/api/detect")
def detect_link(data: DetectRequest):
    platform = detect_platform(str(data.url))
    if platform is None:
        return {
            "supported": False,
            "platform": None,
            "message": "This platform is not supported yet.",
        }
    if platform in EXTRACTABLE:
        message = f"{platform} link recognized. Ready to extract."
    else:
        message = (
            f"{platform} link recognized, but downloading from {platform} "
            "isn't available yet. Giphy, Reddit, Tenor, and X work for now."
        )
    return {"supported": True, "platform": platform, "message": message}


@app.post("/api/extract")
def extract_link(data: DetectRequest):
    url = str(data.url)
    platform = detect_platform(url)
    if platform is None:
        raise HTTPException(400, "This platform is not supported yet.")
    with make_client() as client:
        try:
            return extract(url, platform, client)
        except ExtractionError as error:
            raise HTTPException(error.status, error.message)


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


@app.get("/api/file")
async def download_file(url: str, name: str = "animation"):
    if not is_media_host(url):
        raise HTTPException(400, "That file host is not allowed.")
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "animation"
    client = httpx.AsyncClient(timeout=30, follow_redirects=False)
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(502, "Couldn't fetch the file.")

    length = upstream.headers.get("content-length", "")
    if upstream.status_code != 200 or (length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES):
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, "The file isn't available or is too large.")

    async def body():
        total = 0
        try:
            async for chunk in upstream.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    break
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if length.isdigit():
        headers["Content-Length"] = length
    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "gifreaper", "version": "0.6.0"}
