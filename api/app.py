#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Samarth Insta Info — full free-tier build.
Public data only. No session IDs. No paid APIs.
"""

import os
import re
import ssl
import json
import time
import socket
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import urllib3
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

try:
    from langdetect import detect as detect_lang, DetectorFactory
    DetectorFactory.seed = 0
    HAS_LANGDETECT = True
except Exception:
    HAS_LANGDETECT = False

try:
    from textblob import TextBlob
    HAS_TEXTBLOB = True
except Exception:
    HAS_TEXTBLOB = False

try:
    import whois as whois_lib
    HAS_WHOIS = True
except Exception:
    HAS_WHOIS = False

import storage

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))

app = Flask(__name__, static_folder=ROOT_DIR, static_url_path="")
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


def clear_cache():
    with LOCK:
        n = len(CACHE)
        CACHE.clear()
        return n


def normalize_username(u: str) -> str:
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


def fetch_profile(username: str) -> dict:
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
    if r.status_code != 200:
        raise ValueError(f"Instagram returned {r.status_code}")

    raw = r.json()
    user = (raw.get("data") or {}).get("user")
    if not user:
        raise ValueError("Profile not found or private")

    edge_followed = user.get("edge_followed_by") or {}
    edge_follow = user.get("edge_follow") or {}
    edge_media = user.get("edge_owner_to_timeline_media") or {}

    return {
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "biography": user.get("biography"),
        "is_private": user.get("is_private"),
        "is_verified": user.get("is_verified"),
        "is_business": user.get("is_business_account"),
        "is_professional": user.get("is_professional_account"),
        "profile_pic": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "external_url": user.get("external_url"),
        "category": user.get("category_name"),
        "followers": edge_followed.get("count"),
        "following": edge_follow.get("count"),
        "posts": edge_media.get("count"),
        "user_id": user.get("id"),
        "business_email": user.get("business_email"),
        "business_phone": user.get("business_phone_number"),
        "business_address": user.get("business_address_json"),
        "bio_links": [l.get("url") for l in (user.get("bio_links") or []) if l.get("url")],
        "pronouns": user.get("pronouns"),
        "has_channel": user.get("has_channel"),
    }


def fetch_profile_fallback(username: str) -> dict:
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
        "is_business": None, "is_professional": None,
        "profile_pic": pic, "external_url": None, "category": None,
        "followers": followers, "following": following, "posts": posts,
        "user_id": None, "business_email": None, "business_phone": None,
        "business_address": None, "bio_links": [], "pronouns": None,
        "has_channel": None,
    }


def fetch_about_account(user_id: str) -> dict:
    if not user_id:
        return {}
    return {
        "created_year": None,
        "created_month": None,
        "account_country": None,
        "former_username_count": None,
    }


def analyze_bio(bio: str) -> dict:
    if not bio:
        return {
            "language": None, "sentiment": None, "sentiment_score": None,
            "hashtags": [], "mentions": [], "emojis": [], "length": 0,
        }

    hashtags = re.findall(r'#(\w+)', bio)
    mentions = re.findall(r'@(\w+)', bio)
    emojis = re.findall(
        r'[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]',
        bio
    )

    lang = None
    if HAS_LANGDETECT and len(bio) > 3:
        try:
            lang = detect_lang(bio)
        except Exception:
            pass

    sentiment = None
    score = None
    if HAS_TEXTBLOB and len(bio) > 3:
        try:
            blob = TextBlob(bio)
            score = round(blob.sentiment.polarity, 3)
            if score > 0.1:
                sentiment = "positive"
            elif score < -0.1:
                sentiment = "negative"
            else:
                sentiment = "neutral"
        except Exception:
            pass

    return {
        "language": lang,
        "sentiment": sentiment,
        "sentiment_score": score,
        "hashtags": list(dict.fromkeys(hashtags))[:20],
        "mentions": list(dict.fromkeys(mentions))[:20],
        "emojis": list(dict.fromkeys(emojis))[:20],
        "length": len(bio),
    }


def compute_trust_score(p: dict, history: list) -> dict:
    score = 50
    signals = []

    followers = p.get("followers") or 0
    following = p.get("following") or 0
    posts = p.get("posts") or 0

    if p.get("is_verified"):
        score += 15
        signals.append({"text": "Verified account", "delta": +15, "type": "good"})

    if posts > 30:
        score += 10
        signals.append({"text": "Active posting history", "delta": +10, "type": "good"})

    if followers > 1000 and following > 0:
        ratio = followers / following
        if ratio > 5:
            score += 10
            signals.append({"text": f"Healthy follower ratio ({ratio:.1f})", "delta": +10, "type": "good"})
        elif ratio < 0.5:
            score -= 15
            signals.append({"text": f"Poor follower ratio ({ratio:.1f})", "delta": -15, "type": "bad"})

    if p.get("external_url"):
        score += 5
        signals.append({"text": "Has external link", "delta": +5, "type": "good"})

    if p.get("business_email") or p.get("business_phone"):
        score += 10
        signals.append({"text": "Business contact listed", "delta": +10, "type": "good"})

    if followers > 1000 and posts < 5:
        score -= 20
        signals.append({"text": "High followers but few posts", "delta": -20, "type": "bad"})

    if followers > 0 and posts == 0:
        score -= 10
        signals.append({"text": "Zero posts", "delta": -10, "type": "bad"})

    if p.get("is_private"):
        signals.append({"text": "Private account", "delta": 0, "type": "info"})

    if history and len(history) >= 2:
        first = history[0].get("followers") or 0
        last = history[-1].get("followers") or 0
        if first > 0:
            growth = ((last - first) / first) * 100
            if growth > 20:
                signals.append({"text": f"Growing fast (+{growth:.0f}%)", "delta": +5, "type": "good"})
                score += 5
            elif growth < -5:
                signals.append({"text": f"Declining ({growth:.0f}%)", "delta": -5, "type": "bad"})
                score -= 5

    score = max(0, min(100, score))

    if score >= 70:
        level = "green"
    elif score >= 40:
        level = "amber"
    else:
        level = "red"

    return {"score": score, "level": level, "signals": signals}


def estimate_engagement(p: dict) -> dict:
    followers = p.get("followers") or 0
    if followers == 0:
        return {"engagement_rate": None, "note": "No follower data"}
    return {
        "engagement_rate": None,
        "note": "Requires post-level data (not publicly exposed without login)",
        "followers_for_estimate": followers,
    }


def check_link(url: str) -> dict:
    result = {"url": url, "http_status": None, "ssl_valid": None,
              "domain_age_days": None, "domain": None}

    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        result["domain"] = domain

        r = requests.head(url, headers=HEADERS, timeout=8,
                          allow_redirects=True, verify=False)
        result["http_status"] = r.status_code

        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((domain, 443), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=domain):
                    result["ssl_valid"] = True
        except Exception:
            result["ssl_valid"] = False

        if HAS_WHOIS:
            try:
                w = whois_lib.whois(domain)
                created = w.creation_date
                if isinstance(created, list):
                    created = created[0]
                if created:
                    age = (datetime.now() - created).days
                    result["domain_age_days"] = age
            except Exception:
                pass

    except Exception as e:
        result["error"] = str(e)

    return result


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
            "details": errors
        }), 502

    about = fetch_about_account(profile.get("user_id"))

    try:
        storage.save_snapshot(profile, now)
        storage.save_profile_meta(username, {
            "user_id": profile.get("user_id"),
            "created_year": about.get("created_year"),
            "created_month": about.get("created_month"),
            "account_country": about.get("account_country"),
            "former_username_count": about.get("former_username_count"),
        }, now)
    except Exception:
        pass

    history = storage.get_history(username, 90)
    bio_analysis = analyze_bio(profile.get("biography"))
    trust = compute_trust_score(profile, history)
    engagement = estimate_engagement(profile)

    link_checks = []
    for url in ([profile.get("external_url")] + (profile.get("bio_links") or [])):
        if url and url not in [l["url"] for l in link_checks]:
            lc = check_link(url)
            link_checks.append(lc)
            try:
                storage.save_link_check(
                    username, url,
                    lc.get("http_status") or 0,
                    bool(lc.get("ssl_valid")),
                    lc.get("domain_age_days") or 0,
                    now
                )
            except Exception:
                pass

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
            "about": about,
            "bio_analysis": bio_analysis,
            "trust": trust,
            "engagement": engagement,
            "links": link_checks,
            "history": history,
            "growth": growth,
            "sources": {
                "profile": "Instagram public web_profile_info",
                "about": "Instagram public info endpoint",
                "bio_analysis": "langdetect + TextBlob (local)",
                "whois": "python-whois (public WHOIS servers)",
                "history": "local SQLite (built over time)",
            },
            "checked_at": now,
        },
    }

    cache_set(username, result)
    return jsonify(result)


@app.route("/history/<username>", methods=["GET"])
def history(username):
    username = normalize_username(username)
    if not username:
        return jsonify({"success": False, "error": "Invalid username"}), 400
    h = storage.get_history(username, 365)
    return jsonify({"success": True, "username": username, "history": h})


@app.route("/cache/clear", methods=["GET"])
def clear():
    n = clear_cache()
    return jsonify({"success": True, "cleared": n})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "samarth-insta-info",
        "whois": HAS_WHOIS,
        "langdetect": HAS_LANGDETECT,
        "textblob": HAS_TEXTBLOB,
    })


@app.route("/", methods=["GET"])
def index():
    return send_from_directory(ROOT_DIR, "index.html")


@app.errorhandler(404)
def not_found(_):
    return jsonify({"success": False, "error": "Not found"}), 404


@app.errorhandler(500)
def server_error(_):
    return jsonify({"success": False, "error": "Server error"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"→ Service: Samarth Insta Info")
    print(f"→ WHOIS: {'on' if HAS_WHOIS else 'off'}")
    print(f"→ langdetect: {'on' if HAS_LANGDETECT else 'off'}")
    print(f"→ textblob: {'on' if HAS_TEXTBLOB else 'off'}")
    app.run(host="0.0.0.0", port=port, debug=False)
