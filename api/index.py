#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Samarth Insta Info — Vercel serverless function with retry + UA rotation."""

import os
import re
import time
import random
import threading
from datetime import datetime, timezone

import requests
import urllib3
from flask import Flask, jsonify, request

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

CACHE = {}
CACHE_TTL = 300
LOCK = threading.Lock()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
]

PROXIES = [
    # Add free/paid proxies here, e.g.:
    # "http://user:pass@host:port",
    # "socks5://host:port",
]


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": "936619743392459",
        "X-ASBD-ID": "198387",
        "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
    }


def get_proxy():
    if not PROXIES:
        return None
    p = random.choice(PROXIES)
    return {"http": p, "https": p}


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


def fetch_profile_with_retry(username, max_attempts=5):
    endpoints = [
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
        f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}&__a=1",
    ]

    last_error = None

    for attempt in range(max_attempts):
        endpoint = endpoints[attempt % len(endpoints)]
        headers = get_headers()
        proxy = get_proxy()

        try:
            r = requests.get(endpoint, headers=headers, proxies=proxy, timeout=20, verify=False)

            if r.status_code == 429:
                last_error = f"Rate limited (429) on attempt {attempt + 1}"
                time.sleep(min(1 * (2 ** attempt), 3))
                continue

            if r.status_code != 200:
                last_error = f"Instagram returned {r.status_code}"
                continue

            raw = r.json()
            user = (raw.get("data") or {}).get("user")
            if not user:
                last_error = "Profile not found or private"
                continue

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
                "pronouns": user.get("pronouns"),
                "has_channel": user.get("has_channel"),
            }

        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            time.sleep(0.5)

    raise ValueError(last_error or "All attempts failed")


def analyze_bio(bio):
    if not bio:
        return {"hashtags": [], "mentions": [], "length": 0}
    return {
        "hashtags": list(dict.fromkeys(re.findall(r'#(\w+)', bio)))[:20],
        "mentions": list(dict.fromkeys(re.findall(r'@(\w+)', bio)))[:20],
        "length": len(bio),
    }


def compute_trust_score(p):
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


@app.route("/api/insta/<username>")
def insta_info(username):
    username = normalize_username(username)
    if not username or not re.match(r'^[a-zA-Z0-9._]{1,30}$', username):
        return jsonify({"success": False, "error": "Invalid username"}), 400

    cached = cache_get(username)
    if cached:
        return jsonify(cached)

    now = datetime.now(timezone.utc).isoformat()

    try:
        profile = fetch_profile_with_retry(username)
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "hint": "Instagram is rate-limiting this server's IP. Try again in a minute.",
        }), 429

    result = {
        "success": True,
        "data": {
            "profile": profile,
            "bio_analysis": analyze_bio(profile.get("biography")),
            "trust": compute_trust_score(profile),
            "history": [],
            "growth": None,
            "checked_at": now,
        },
    }
    cache_set(username, result)
    return jsonify(result)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "service": "samarth-insta-info"})


@app.route("/")
def root():
    return jsonify({
        "service": "Samarth Insta Info",
        "status": "running",
        "endpoints": ["/api/insta/<username>", "/api/health"],
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({"success": False, "error": "Not found", "path": request.path}), 404
