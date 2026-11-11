# GIFReaper (flat layout)

All files sit in one folder — no nested directories.

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000

## Deploy on Render

1. Push this folder to GitHub
2. New Web Service → connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

## Files

```text
main.py          app server
extractors.py    Giphy / Reddit / Tenor logic
index.html       website
style.css        styles
app.js           frontend
requirements.txt dependencies
render.yaml      optional Render blueprint
README.md        this file
```
