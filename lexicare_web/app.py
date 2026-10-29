import os

import requests
from flask import Flask, Response, jsonify, render_template, request

from lexicare_core import (
    corrections, save_corrections, syllabify, tokenize, write_report,
)

app = Flask(__name__)

PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
image_cache = {}


def get_image(word):
    if word in image_cache:
        return image_cache[word]
    if not PIXABAY_API_KEY:
        return None
    try:
        r = requests.get(
            "https://pixabay.com/api/",
            params={"key": PIXABAY_API_KEY, "q": word,
                    "image_type": "illustration", "safesearch": "true",
                    "per_page": 3},
            timeout=8,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
        url = hits[0].get("webformatURL") if hits else None
        if not url:
            return None
        pic = requests.get(url, timeout=8)
        pic.raise_for_status()
        if len(image_cache) > 200:
            image_cache.clear()
        image_cache[word] = (pic.content,
                             pic.headers.get("Content-Type", "image/jpeg"))
        return image_cache[word]
    except (requests.RequestException, ValueError):
        return None


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/api/syllabify")
def api_syllabify():
    text = str((request.get_json(silent=True) or {}).get("text", ""))[:600]
    words = []
    for word in tokenize(text)[:60]:
        parts, confident, source = syllabify(word)
        words.append({"word": word, "parts": parts,
                      "confident": confident, "source": source})
    return jsonify(words=words)


@app.get("/api/image")
def api_image():
    result = get_image(request.args.get("q", "").strip().lower()[:40])
    if not result:
        return "", 404
    data, mimetype = result
    return Response(data, mimetype=mimetype)


@app.post("/api/correct")
def api_correct():
    d = request.get_json(silent=True) or {}
    word = str(d.get("word", ""))
    parts = [str(p).strip() for p in d.get("parts", [])]
    if (not word or not parts or any(not p for p in parts)
            or "".join(parts).lower() != word.lower()):
        return jsonify(ok=False, error="The parts must rebuild the word "
                       "exactly, with no empty syllables."), 400
    corrections[word.lower()] = parts
    if not save_corrections():
        corrections.pop(word.lower(), None)
        return jsonify(ok=False, error="The server could not save this."), 500
    return jsonify(ok=True)


@app.delete("/api/correct")
def api_remove():
    word = str((request.get_json(silent=True) or {}).get("word", ""))
    previous = corrections.pop(word.lower(), None)
    if previous is None:
        return jsonify(ok=False, error="No saved correction for this word."), 404
    if not save_corrections():
        corrections[word.lower()] = previous
        return jsonify(ok=False, error="The server could not save this."), 500
    parts, confident, source = syllabify(word)
    return jsonify(ok=True, parts=parts, confident=confident, source=source)


@app.post("/api/report")
def api_report():
    d = request.get_json(silent=True) or {}
    word = str(d.get("word", ""))[:60]
    suggested = [str(p).strip() for p in d.get("suggested", []) if str(p).strip()]
    if "".join(suggested).lower() != word.lower():
        suggested = []
    ok = write_report(word, [str(p) for p in d.get("shown", [])],
                      suggested, str(d.get("reason", ""))[:300])
    return jsonify(ok=ok)


if __name__ == "__main__":
    app.run(debug=True)
