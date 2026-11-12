
# HappyGif

**Download GIFs and animated media online.**

HappyGif lets users paste a public animation link, preview the available media and download it.

## Supported platforms

### Download extraction available

- Giphy
- Reddit
- Tenor
- X

### Recognized, but downloading is not available yet

- Pinterest
- Instagram
- TikTok

X animated media is delivered as MP4.

Reddit availability can vary because Reddit may restrict requests from cloud hosting providers.

## Features

- Public animation link detection
- GIF and MP4 extraction where available
- Animation preview
- Direct file downloads
- Mobile-friendly interface
- No account required

## Technology

- Python
- FastAPI
- HTTPX
- HTML
- CSS
- JavaScript

## Run locally

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the application:

```bash
uvicorn main:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

## Deploy on Render

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Health check:

```text
/health
```

## SEO files

The project includes:

- `robots.txt`
- `sitemap.xml`
- `favicon.svg`
- Canonical URL
- Open Graph metadata
- Twitter card metadata
- WebApplication structured data
- FAQ structured data

## Project files

```text
HappyGif/
├── main.py
├── extractors.py
├── index.html
├── style.css
├── app.js
├── robots.txt
├── sitemap.xml
├── favicon.svg
├── requirements.txt
├── render.yaml
└── README.md
```

## Responsible use

Only download and reuse media when you have the right or permission to do so. Respect the rules and terms of the platforms hosting the content.
