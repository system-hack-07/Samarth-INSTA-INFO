#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Samarth Insta Info — public Instagram profile lookup."""

import os
import re
import sys
import time
import threading
from datetime import datetime, timezone

import requests
import urllib3
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------
# Paths — always resolve from THIS file, not the cwd
# ---------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))

sys.path.insert(0, BASE_DIR)
import storage  # noqa: E402

# ---------------------------------------------------------------
# Flask
# ---------------------------------------------------------------
app = Flask(__name__)
CORS(app)

CACHE = {}
CACHE_TTL = 300
LOCK = threading.Lock()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": "936619743392459",
    "X-ASBD-ID": "198387",
    "X-IG-WWW-Claim": "0",
    "X-Requested-With": "XMLHttpRequest",
}


def cache_get(key):
    with LOCK:
        item = CACHE.get(key)
        if not item:
            return None
        if time.time() - item["ts"] > CACHE_TTL:
            CACHE.pop(key, None)
            return None
        return item["value"]


def cache_set(key, value):
    with LOCK:
        CACHE[key] = {"ts": time.time(), "value": value}


def normalize_username(u):
    u = (u or "").strip()
    if not u:
        return ""
    u = re.sub(r'^https?://(www\.)?instagram\.com/', '', u)
    u = u.split('?')[0].split('/')[0].replace('@', '')
    return u.lower()


def parse_count(s):
    if s is None:
        return None
    if isinstance(s, int):
        return s
    s = str(s).strip().replace(',', '')
    m = re.match(r'^([\d.]+)\s*([KMB]?)$', s, re.I)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(n * mult.get(m.group(2).upper(), 1))


def fetch_profile(username):
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
    if r.status_code != 200:
        raise ValueError(f"Instagram returned {r.status_code}")

    raw = r.json()
    user = (raw.get("data") or {}).get("user")
    if not user:
        raise ValueError("Profile not found or private")

    return {
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "biography": user.get("biography"),
        "is_private": user.get("is_private"),
        "is_verified": user.get("is_verified"),
        "is_business": user.get("is_business_account"),
        "profile_pic": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "external_url": user.get("external_url"),
        "category": user.get("category_name"),
        "followers": (user.get("edge_followed_by") or {}).get("count"),
        "following": (user.get("edge_follow") or {}).get("count"),
        "posts": (user.get("edge_owner_to_timeline_media") or {}).get("count"),
        "user_id": user.get("id"),
        "business_email": user.get("business_email"),
        "business_phone": user.get("business_phone_number"),
        "bio_links": [l.get("url") for l in (user.get("bio_links") or []) if l.get("url")],
        "pronouns": user.get("pronouns"),
        "has_channel": user.get("has_channel"),
    }


def fetch_profile_fallback(username):
    url = f"https://www.instagram.com/{username}/"
    r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
    if r.status_code != 200:
        raise ValueError(f"Instagram returned {r.status_code}")

    html = r.text
    m = re.search(r'<meta property="og:description" content="([^"]+)"', html)
    desc = m.group(1) if m else ""

    followers = following = posts = None
    fm = re.search(r'([\d.,KMB]+)\s+Followers', desc, re.I)
    if fm: followers = parse_count(fm.group(1))
    gm = re.search(r'([\d.,KMB]+)\s+Following', desc, re.I)
    if gm: following = parse_count(gm.group(1))
    pm = re.search(r'([\d.,KMB]+)\s+Posts', desc, re.I)
    if pm: posts = parse_count(pm.group(1))

    om = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    pic = om.group(1) if om else None

    tm = re.search(r'<title>([^<]+)</title>', html)
    title = tm.group(1) if tm else ""
    name = title.split('(')[0].strip().replace(' • Instagram photos and videos', '') if title else username

    if not followers and not following and not posts:
        raise ValueError("Could not parse profile")

    return {
        "username": username, "full_name": name, "biography": desc,
        "is_private": None, "is_verified": None,
        "is_business": None, "profile_pic": pic,
        "external_url": None, "category": None,
        "followers": followers, "following": following, "posts": posts,
        "user_id": None, "business_email": None, "business_phone": None,
        "bio_links": [], "pronouns": None, "has_channel": None,
    }


def analyze_bio(bio):
    if not bio:
        return {"language": None, "hashtags": [], "mentions": [], "length": 0}
    hashtags = re.findall(r'#(\w+)', bio)
    mentions = re.findall(r'@(\w+)', bio)
    return {
        "language": None,
        "hashtags": list(dict.fromkeys(hashtags))[:20],
        "mentions": list(dict.fromkeys(mentions))[:20],
        "length": len(bio),
    }


def compute_trust_score(p, history):
    score = 50
    signals = []

    followers = p.get("followers") or 0
    following = p.get("following") or 0
    posts = p.get("posts") or 0

    if p.get("is_verified"):
        score += 15
        signals.append({"text": "Verified account", "delta": 15, "type": "good"})

    if posts > 30:
        score += 10
        signals.append({"text": "Active posting history", "delta": 10, "type": "good"})

    if followers > 1000 and following > 0:
        ratio = followers / following
        if ratio > 5:
            score += 10
            signals.append({"text": f"Healthy follower ratio ({ratio:.1f})", "delta": 10, "type": "good"})
        elif ratio < 0.5:
            score -= 15
            signals.append({"text": f"Poor follower ratio ({ratio:.1f})", "delta": -15, "type": "bad"})

    if p.get("external_url"):
        score += 5
        signals.append({"text": "Has external link", "delta": 5, "type": "good"})

    if followers > 1000 and posts < 5:
        score -= 20
        signals.append({"text": "High followers but few posts", "delta": -20, "type": "bad"})

    if followers > 0 and posts == 0:
        score -= 10
        signals.append({"text": "Zero posts", "delta": -10, "type": "bad"})

    score = max(0, min(100, score))
    level = "green" if score >= 70 else "amber" if score >= 40 else "red"
    return {"score": score, "level": level, "signals": signals}


# ---------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------

@app.route("/api/insta/<username>", methods=["GET"])
def insta_info(username):
    username = normalize_username(username)
    if not username or not re.match(r'^[a-zA-Z0-9._]{1,30}$', username):
        return jsonify({"success": False, "error": "Invalid username"}), 400

    cached = cache_get(username)
    if cached:
        return jsonify(cached)

    now = datetime.now(timezone.utc).isoformat()

    errors = []
    profile = None
    try:
        profile = fetch_profile(username)
    except Exception as e:
        errors.append(f"api: {e}")

    if profile is None:
        try:
            profile = fetch_profile_fallback(username)
        except Exception as e:
            errors.append(f"fallback: {e}")

    if profile is None:
        return jsonify({
            "success": False,
            "error": "Could not fetch profile",
            "details": errors,
        }), 502

    try:
        storage.save_snapshot(profile, now)
        storage.save_profile_meta(username, {"user_id": profile.get("user_id")}, now)
    except Exception:
        pass

    history = storage.get_history(username, 90)
    bio_analysis = analyze_bio(profile.get("biography"))
    trust = compute_trust_score(profile, history)

    growth = None
    if len(history) >= 2:
        first = history[0].get("followers") or 0
        last = history[-1].get("followers") or 0
        if first > 0:
            growth = {
                "delta": last - first,
                "percent": round(((last - first) / first) * 100, 2),
                "samples": len(history),
            }

    result = {
        "success": True,
        "data": {
            "profile": profile,
            "bio_analysis": bio_analysis,
            "trust": trust,
            "history": history,
            "growth": growth,
            "checked_at": now,
        },
    }

    cache_set(username, result)
    return jsonify(result)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "samarth-insta-info"})


@app.route("/")
def index():
    return send_from_directory(ROOT_DIR, "index.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({
            "success": False,
            "error": "API endpoint not found",
            "path": request.path,
        }), 404
    return send_from_directory(ROOT_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Samarth Insta Info")
    print(f"  Frontend:  http://localhost:{port}/")
    print(f"  API test:  http://localhost:{port}/api/insta/nasa\n")
    app.run(host="0.0.0.0", port=port, debug=False)
