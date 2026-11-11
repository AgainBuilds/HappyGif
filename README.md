# GIFReaper (flat layout)

Paste a public animation link → preview → download.

**Works now:** Giphy, Reddit, Tenor, **X (Twitter)**  
**Recognized only:** Pinterest, Instagram, TikTok  

X posts are resolved via the public FxTwitter JSON API (no X API key).  
X GIFs are MP4s on X's CDN — downloads are MP4.  
Reddit may still fail on cloud hosts (Render) that Reddit blocks.

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000

## Deploy on Render

1. Push this folder to GitHub  
2. New → Web Service  
3. Build: `pip install -r requirements.txt`  
4. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

## Files

```text
main.py  extractors.py  index.html  style.css  app.js
requirements.txt  render.yaml  README.md
```
