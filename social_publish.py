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
# Paused 19 Oct 2026: the Meta app is still in development mode (going live needs business verification), so its posts
# are visible only to the app's admins. Set True once the app is live (or posting goes through another route).
FACEBOOK_ENABLED = False
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


def _as_list(images) -> list:
    if not images:
        return []
    items = images if isinstance(images, (list, tuple)) else [images]
    return [Path(i) for i in items if i and Path(i).exists()]


def post_x(text: str, image) -> tuple[bool, str]:
    """`image`: one path or a list (21 Oct 2026: the same map images Telegram gets, up to X's 4 per post)."""
    k = [os.environ.get(n, "").strip() for n in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
    if not all(k):
        return False, "X credentials not set"
    try:
        media_ids, failed = [], []
        for img in _as_list(image)[:4]:
            with open(img, "rb") as fh:
                r = requests.post(X_MEDIA_URL, headers={"Authorization": oauth1_header("POST", X_MEDIA_URL, *k)},
                                  data={"media_category": "tweet_image"}, files={"media": fh}, timeout=60)
            if not r.ok:
                failed.append(f"HTTP {r.status_code}")       # one bad upload does not stop the post
                continue
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
        note = f" with {len(media_ids)} image(s)" if media_ids else " (text only: no image)"
        return True, "posted" + note + (f"; {len(failed)} image upload(s) refused: {', '.join(failed)}" if failed else "")
    except Exception as e:
        return False, f"X request failed: {type(e).__name__}"


FB_APP_ID_DEFAULT = "2428462281026408"          # COMK Nowcast Automated Alert (public, not a secret)
FB_TOKEN_STORE = Path("state_social_test/fb_page_token.enc")   # the page token, ENCRYPTED with FB_APP_SECRET (public repo)


def _fernet():
    secret = os.environ.get("FB_APP_SECRET", "").strip()
    if not secret:
        return None
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("comk-fb-token|" + secret).encode()).digest()))


def _fb_get(path, **params):
    r = requests.get(f"https://graph.facebook.com/{FB_GRAPH_VERSION}/{path}", params=params, timeout=30)
    try:
        return r.ok, r.json()
    except ValueError:
        return r.ok, {"error": {"message": r.text[:200]}}


def _token_info(token: str) -> dict:
    ok, j = _fb_get("debug_token", input_token=token, access_token=token)
    return j.get("data", {}) if ok else {}


def _stored_token() -> str | None:
    f = _fernet()
    if not f or not FB_TOKEN_STORE.exists():
        return None
    try:
        return f.decrypt(FB_TOKEN_STORE.read_bytes()).decode()
    except Exception:
        return None


def fb_page_token(notes: list | None = None) -> str | None:
    """The token to post with. Preference: the stored (encrypted) never-expiring page token; else FB_PAGE_TOKEN as it is
    if it is already a page token; else, when FB_PAGE_TOKEN is a USER token of this app and FB_APP_SECRET is set, convert it
    once (short-lived user -> long-lived user -> page token, which then never expires) and store it encrypted."""
    notes = notes if notes is not None else []
    page = os.environ.get("FB_PAGE_ID", "").strip()
    given = os.environ.get("FB_PAGE_TOKEN", "").strip()
    stored = _stored_token()
    if stored and not (given and given.startswith("EAA") and _given_is_new(given)):
        notes.append("using the stored page token")
        return stored
    if not given:
        notes.append("FB_PAGE_TOKEN not set")
        return stored
    info = _token_info(given)
    typ = (info.get("type") or "").upper()
    if typ == "PAGE":
        notes.append("FB_PAGE_TOKEN is already a page token")
        _store(given, given, notes)
        return given
    if typ != "USER":
        notes.append(f"FB_PAGE_TOKEN is not a usable token (type {typ or 'unknown'}, length {len(given)})")
        return stored
    # The exchange must use the app that issued the token (its secret must be FB_APP_SECRET).
    app_id = str(info.get("app_id") or os.environ.get("FB_APP_ID", "").strip() or FB_APP_ID_DEFAULT)
    notes.append(f"user token issued by app {info.get('application') or ''} ({app_id}), "
                 f"expires {datetime.fromtimestamp(info['expires_at'], timezone.utc):%H:%M UTC}" if info.get("expires_at") else
                 f"user token issued by app {app_id}")
    secret = os.environ.get("FB_APP_SECRET", "").strip()
    if not secret:
        notes.append("FB_PAGE_TOKEN is a user token; add the FB_APP_SECRET secret so it can be converted")
        return stored
    ok, j = _fb_get("oauth/access_token", grant_type="fb_exchange_token", client_id=app_id, client_secret=secret,
                    fb_exchange_token=given)
    if not ok or "access_token" not in j:
        notes.append(f"exchange for a long-lived user token failed (is FB_APP_SECRET the secret of app {app_id}?): "
                     f"{(j.get('error') or {}).get('message', j)}"[:300])
        return stored
    ok, j = _fb_get(page, fields="access_token", access_token=j["access_token"])
    if not ok or "access_token" not in j:
        notes.append(f"getting the page token failed: {(j.get('error') or {}).get('message', j)}"[:300])
        return stored
    notes.append("converted the user token into a page token")
    _store(j["access_token"], given, notes)
    return j["access_token"]


_GIVEN_MARK = Path("state_social_test/fb_token_source.sha")


def _given_is_new(given: str) -> bool:
    """True when FB_PAGE_TOKEN changed since the stored page token was made (the user pasted a new one)."""
    h = hashlib.sha256(("comk|" + given).encode()).hexdigest()
    return not (_GIVEN_MARK.exists() and _GIVEN_MARK.read_text().strip() == h)


def _store(page_token: str, given: str, notes: list) -> None:
    f = _fernet()
    if not f:
        notes.append("not stored: FB_APP_SECRET not set")
        return
    FB_TOKEN_STORE.parent.mkdir(parents=True, exist_ok=True)
    FB_TOKEN_STORE.write_bytes(f.encrypt(page_token.encode()))
    _GIVEN_MARK.write_text(hashlib.sha256(("comk|" + given).encode()).hexdigest() + "\n")
    notes.append("page token stored (encrypted)")


def post_facebook(text: str, image: Path | None) -> tuple[bool, str]:
    page, token = os.environ.get("FB_PAGE_ID", "").strip(), fb_page_token()
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
        res["facebook"] = list(post_facebook(fb_text, (_as_list(image) or [None])[0])) if FACEBOOK_ENABLED else [False, "Facebook paused (app not live)"]
    res["any_ok"] = bool(res["x"][0] or res["facebook"][0])
    _record({k: v for k, v in res.items()})
    return res


def send_test_x() -> tuple[bool, str]:
    """Manual check (workflow input send_test_x): one harmless test post with a small image to X ONLY, regardless of
    PUBLIC_POST_ENABLED, to prove the keys, the image upload and the posting work. The outcome (never the keys) is left in
    state_social_test/public_last.json."""
    from PIL import Image, ImageDraw
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (900, 500), (235, 242, 250))
    d = ImageDraw.Draw(img)
    d.text((40, 220), "COMK experimental rainfall nowcast - link test image", fill=(30, 50, 90))
    path = DRAFT_DIR / "x_test.png"
    img.save(path)
    stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    ok, detail = post_x(f"Test post ({stamp}) while the COMK experimental rainfall nowcast account is being set up. Please ignore.", path)
    _record({"mode": "manual_test_x", "x": [ok, detail]})
    print(f"[public] X test post: {'OK' if ok else 'FAILED'} - {detail}")
    return ok, detail


def check_facebook() -> tuple[bool, str]:
    """Manual check (workflow input send_test_facebook) that leaves NOTHING visible on the Page: reads the Page name with
    the token, uploads a small test photo as UNPUBLISHED (published=false, never shown in the feed), then deletes it.
    Proves FB_PAGE_ID, FB_PAGE_TOKEN, the posting permission and the image upload. The outcome (never the token) goes
    to state_social_test/public_last.json."""
    steps = []
    page, token = os.environ.get("FB_PAGE_ID", "").strip(), fb_page_token(steps)

    def done(ok, detail):
        _record({"mode": "manual_test_facebook", "facebook": [ok, detail], "steps": steps})
        print(f"[public] Facebook check: {'OK' if ok else 'FAILED'} - {detail}")
        return ok, detail

    if not page or not token:
        return done(False, f"no usable token (FB_PAGE_ID {'set' if page else 'NOT set'}): see steps")
    g = f"https://graph.facebook.com/{FB_GRAPH_VERSION}"
    try:
        try:   # a token may inspect itself: type, expiry, scopes (never the token itself)
            dj = requests.get(f"{g}/debug_token", params={"input_token": token, "access_token": token}, timeout=30).json().get("data", {})
            def _when(ts):
                return "never" if not ts else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            steps.append(f"token type: {dj.get('type')}, profile/page id: {dj.get('profile_id') or dj.get('user_id')}, "
                         f"expires: {_when(dj.get('expires_at'))}, data access expires: {_when(dj.get('data_access_expires_at'))}, "
                         f"scopes: {', '.join(dj.get('scopes', []))}, length {len(token)}, starts {token[:4]}")
        except Exception as e:
            steps.append(f"token self-inspection failed: {type(e).__name__}")
        r = requests.get(f"{g}/{page}", params={"fields": "id,name", "access_token": token}, timeout=30)
        if not r.ok:
            return done(False, f"token cannot read the Page: HTTP {r.status_code} {r.text[:300]}")
        steps.append(f"page ok: {r.json().get('name')} ({r.json().get('id')})")
        r = requests.get(f"{g}/me", params={"fields": "id,name", "access_token": token}, timeout=30)
        me = r.json() if r.ok else {}
        steps.append(f"token belongs to: {me.get('name')} ({me.get('id')})" + ("  <- a PAGE token" if str(me.get("id")) == page else "  <- NOT the page token: use the access_token from me/accounts"))
        from PIL import Image, ImageDraw
        DRAFT_DIR.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (600, 300), (235, 242, 250))
        ImageDraw.Draw(img).text((30, 140), "COMK nowcast - Facebook link test (unpublished)", fill=(30, 50, 90))
        path = DRAFT_DIR / "fb_test.png"
        img.save(path)
        with open(path, "rb") as fh:
            r = requests.post(f"{g}/{page}/photos", data={"published": "false", "message": "COMK link test, unpublished",
                                                        "access_token": token}, files={"source": fh}, timeout=60)
        if not r.ok:
            return done(False, f"unpublished test photo refused: HTTP {r.status_code} {r.text[:300]}")
        pid = r.json().get("id")
        steps.append(f"unpublished photo uploaded: {pid}")
        r = requests.delete(f"{g}/{pid}", params={"access_token": token}, timeout=30)
        steps.append("test photo deleted" if r.ok else f"test photo NOT deleted (it is unpublished, invisible): HTTP {r.status_code} {r.text[:200]}")
        return done(True, "token, page and photo upload all work")
    except Exception as e:
        return done(False, f"request failed: {type(e).__name__}: {str(e)[:200]}")
