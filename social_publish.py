"""Public posting to X (Twitter) and the Facebook Page (18 Oct 2026). Used only by social_alerts.run().

SAFE BY DEFAULT: with PUBLIC_POST_ENABLED != "true" (or without the credentials) every call is a DRY RUN: the text and image
are written to state_social_test/public_drafts/ and nothing leaves the repo. Real posting needs BOTH the repository
variable/secret PUBLIC_POST_ENABLED=true AND the credentials below. Each platform is independent: one failing never blocks
the other, and neither can affect Telegram or the map.

X (pay-per-use API, user-context OAuth 1.0a):  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
Facebook Page (Graph API, never-expiring PAGE access token):  FB_PAGE_ID, FB_PAGE_TOKEN  (FB_GRAPH_VERSION optional)

NOT yet exercised against the live APIs: the OAuth signer is checked against X's published example, the request shapes
follow the vendors' documentation, and the first real post is the real test."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

X_POST_URL = "https://api.x.com/2/tweets"
X_MEDIA_URL = "https://api.x.com/2/media/upload"
FB_GRAPH_VERSION = os.environ.get("FB_GRAPH_VERSION", "v25.0")
X_LIMIT = 280
DRAFT_DIR = Path("state_social_test/public_drafts")
STATUS_FILE = Path("state_social_test/public_last.json")


def _pct(s) -> str:
    return urllib.parse.quote(str(s), safe="~")


def oauth1_header(method, url, key, secret, token, token_secret, params=None, nonce=None, timestamp=None) -> str:
    """OAuth 1.0a (HMAC-SHA1) Authorization header. `params` = query / form fields that must be signed (JSON and
    multipart bodies are not part of the signature)."""
    oauth = {"oauth_consumer_key": key, "oauth_nonce": nonce or secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(timestamp or int(time.time())),
             "oauth_token": token, "oauth_version": "1.0"}
    allp = {**(params or {}), **oauth}
    pstr = "&".join(f"{k}={v}" for k, v in sorted((_pct(k), _pct(v)) for k, v in allp.items()))
    base = "&".join([method.upper(), _pct(url), _pct(pstr)])
    sig = base64.b64encode(hmac.new(f"{_pct(secret)}&{_pct(token_secret)}".encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def fit_x_text(text: str, disclaimer: str, hashtags: str, limit: int = X_LIMIT) -> str:
    """The alert sentence(s) + a short disclaimer + hashtags inside X's 280 characters; hashtags go first when space is tight,
    then the alert text is cut at a sentence boundary. The disclaimer is never the part that is cut."""
    body = text.replace(hashtags, "").strip()
    for tags in (hashtags, ""):
        cand = f"{body}\n\n{disclaimer}" + (f"\n{tags}" if tags else "")
        if len(cand) <= limit:
            return cand
    sents = body.split(". ")
    while len(sents) > 1:
        sents.pop()
        cand = ". ".join(sents).rstrip(".") + f".\n\n{disclaimer}"
        if len(cand) <= limit:
            return cand
    room = limit - len(disclaimer) - 4
    return body[:max(room, 0)].rstrip() + f"...\n\n{disclaimer}"


def _record(info: dict) -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), **info}, indent=1))
    except Exception:
        pass


def post_x(text: str, image: Path | None) -> tuple[bool, str]:
    k = [os.environ.get(n, "").strip() for n in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
    if not all(k):
        return False, "X credentials not set"
    try:
        media_ids = []
        if image and Path(image).exists():
            with open(image, "rb") as fh:
                r = requests.post(X_MEDIA_URL, headers={"Authorization": oauth1_header("POST", X_MEDIA_URL, *k)},
                                  data={"media_category": "tweet_image"}, files={"media": fh}, timeout=60)
            if not r.ok:
                return False, f"X media upload refused: HTTP {r.status_code} {r.text[:200]}"
            j = r.json()
            mid = (j.get("data") or {}).get("id") or j.get("media_id_string")
            if mid:
                media_ids.append(str(mid))
        body = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        r = requests.post(X_POST_URL, headers={"Authorization": oauth1_header("POST", X_POST_URL, *k)}, json=body, timeout=30)
        if not r.ok:
            return False, f"X refused: HTTP {r.status_code} {r.text[:200]}"
        return True, "posted" + ("" if media_ids else " (text only: no image)")
    except Exception as e:
        return False, f"X request failed: {type(e).__name__}"


def post_facebook(text: str, image: Path | None) -> tuple[bool, str]:
    page, token = os.environ.get("FB_PAGE_ID", "").strip(), os.environ.get("FB_PAGE_TOKEN", "").strip()
    if not page or not token:
        return False, "Facebook credentials not set"
    try:
        base = f"https://graph.facebook.com/{FB_GRAPH_VERSION}/{page}"
        if image and Path(image).exists():
            with open(image, "rb") as fh:
                r = requests.post(f"{base}/photos", data={"message": text, "access_token": token}, files={"source": fh}, timeout=60)
        else:
            r = requests.post(f"{base}/feed", data={"message": text, "access_token": token}, timeout=30)
        if not r.ok:
            return False, f"Facebook refused: HTTP {r.status_code} {r.text[:200]}"
        return True, "posted"
    except Exception as e:
        return False, f"Facebook request failed: {type(e).__name__}"


def publish(x_text: str, fb_text: str, image: Path | None, stamp: str) -> dict:
    """Returns {"mode": "dry_run"|"live", "x": [ok, detail], "facebook": [ok, detail], "any_ok": bool}. In dry-run mode the
    posts are written to public_drafts/ and counted as 'ok' so the posting limits behave exactly as they would live."""
    live = os.environ.get("PUBLIC_POST_ENABLED", "").strip().lower() == "true"
    res = {"mode": "live" if live else "dry_run"}
    if not live:
        DRAFT_DIR.mkdir(parents=True, exist_ok=True)
        (DRAFT_DIR / f"{stamp}_x.txt").write_text(x_text + "\n")
        (DRAFT_DIR / f"{stamp}_facebook.txt").write_text(fb_text + "\n")
        for old in sorted(DRAFT_DIR.glob("*.txt"))[:-40]:
            old.unlink(missing_ok=True)
        res["x"] = [True, "dry run"]
        res["facebook"] = [True, "dry run"]
    else:
        res["x"] = list(post_x(x_text, image))
        res["facebook"] = list(post_facebook(fb_text, image))
    res["any_ok"] = bool(res["x"][0] or res["facebook"][0])
    _record({k: v for k, v in res.items()})
    return res
