import json
import os
import re
import threading
import time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory
import requests

DB_FILE = "matches.json"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
CACHE_TTL_SECONDS = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

app = Flask(__name__)
db_lock = threading.Lock()
_fetch_timestamps = {}


def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_db(db_data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db_data, f, indent=2)


def default_match_payload(crex_url, title=""):
    return {
        "title": title or "Live Cricket Match",
        "crex_url": crex_url,
        "batting_team": "TEAM A",
        "bowling_team": "TEAM B",
        "batting_flag": "https://flagcdn.com/w160/za.png",
        "bowling_flag": "https://flagcdn.com/w160/bd.png",
        "score": "0/0",
        "overs": "0.0",
        "last_ball": "0",
        "crr": "0.00",
        "partnership": "0 (0)",
        "match_status": "MATCH IN PROGRESS",
        "over_prev_num": "PREV:",
        "over_prev_balls": ["0", "0", "0", "0", "0", "0"],
        "over_prev_total": "= 0",
        "over_curr_num": "CURR:",
        "over_curr_balls": ["•", "•", "•", "•", "•", "•"],
        "over_curr_total": "= 0",
        "striker": {
            "name": "BATTER 1",
            "runs": "0",
            "balls": "(0)",
            "fours": "0",
            "sixes": "0",
            "sr": "0.00",
            "image": "",
        },
        "non_striker": {
            "name": "BATTER 2",
            "runs": "0",
            "balls": "(0)",
            "fours": "0",
            "sixes": "0",
            "sr": "0.00",
            "image": "",
        },
        "bowler": {
            "name": "BOWLER",
            "figures": "0-0",
            "overs": "(0.0)",
            "econ": "0.00",
            "image": "",
        },
        "last_wicket": "LIVE BROADCAST",
    }


TEAM_MAP = {
    "IND": ("INDIA", "https://flagcdn.com/w160/in.png"),
    "NZ": ("NEW ZEALAND", "https://flagcdn.com/w160/nz.png"),
    "SA": ("SOUTH AFRICA", "https://flagcdn.com/w160/za.png"),
    "BAN": ("BANGLADESH", "https://flagcdn.com/w160/bd.png"),
    "PAK": ("PAKISTAN", "https://flagcdn.com/w160/pk.png"),
    "AUS": ("AUSTRALIA", "https://flagcdn.com/w160/au.png"),
    "ENG": ("ENGLAND", "https://flagcdn.com/w160/gb-eng.png"),
    "SL": ("SRI LANKA", "https://flagcdn.com/w160/lk.png"),
    "AFG": ("AFGHANISTAN", "https://flagcdn.com/w160/af.png"),
    "WI": ("WEST INDIES", "https://flagcdn.com/w160/jm.png"),
    "ZIM": ("ZIMBABWE", "https://flagcdn.com/w160/zw.png"),
    "IRE": ("IRELAND", "https://flagcdn.com/w160/ie.png"),
    "NED": ("NETHERLANDS", "https://flagcdn.com/w160/nl.png"),
    "OMA": ("OMAN", "https://flagcdn.com/w160/om.png"),
    "UAE": ("UAE", "https://flagcdn.com/w160/ae.png"),
}


def find_player_images(soup, base_url, player_names):
    found = {}
    all_imgs = soup.find_all("img")
    for name in player_names:
        last_name = name.split()[-1].lower()
        for img in all_imgs:
            src = img.get("src") or img.get("data-src") or ""
            alt = (img.get("alt") or "").lower()
            if not src or "flag" in src.lower() or "logo" in src.lower():
                continue
            if last_name in alt or last_name in src.lower():
                found[name] = urljoin(base_url, src)
                break
    return found


def parse_crex_page(html_text, data):
    soup = BeautifulSoup(html_text, "html.parser")
    page_text = " ".join(soup.stripped_strings)
    base_url = data.get("crex_url", "https://crex.com")

    # Detect teams from title or scoreline
    teams_in_title = re.findall(r"\b([A-Z]{2,4})\s+vs\s+([A-Z]{2,4})\b", page_text)
    all_codes = list(teams_in_title[0]) if teams_in_title else []

    score_match = re.search(
        r"\b([A-Z]{2,4})\s+(\d+[-/]\d+)\s*\(?(\d+\.\d+|\d+)\)?", page_text
    )
    if score_match:
        bat_code, raw_score, overs = score_match.groups()
        data["score"] = raw_score.replace("-", "/")
        data["overs"] = overs
        if bat_code in TEAM_MAP:
            data["batting_team"], data["batting_flag"] = TEAM_MAP[bat_code]
        else:
            data["batting_team"] = bat_code

        bowl_code = next((c for c in all_codes if c != bat_code), "")
        if bowl_code in TEAM_MAP:
            data["bowling_team"], data["bowling_flag"] = TEAM_MAP[bowl_code]
        elif bowl_code:
            data["bowling_team"] = bowl_code

    crr_match = re.search(r"CRR:\s*([\d.]+)", page_text)
    if crr_match:
        data["crr"] = crr_match.group(1)

    pship_match = re.search(r"P'ship\s*:\s*(\d+\s*\(\d+\))", page_text)
    if pship_match:
        data["partnership"] = pship_match.group(1)

    status_match = re.search(
        r"\b([A-Z]{2,4}\s+(?:trail by \d+ runs?|lead by \d+ runs?|need \d+ runs?(?: to win)?|opt to (?:bat|bowl)|won by [^.|]+))",
        page_text,
        re.IGNORECASE,
    )
    if status_match:
        data["match_status"] = status_match.group(1).strip().upper()

    over_blocks = re.findall(
        r"Over\s+(\d+)\s+((?:[0-6Wwdnb+•]+\s+){1,8})=\s*(\d+)", page_text
    )
    if len(over_blocks) >= 2:
        prev_ov, prev_balls, prev_tot = over_blocks[-2]
        curr_ov, curr_balls, curr_tot = over_blocks[-1]
        p_list = prev_balls.strip().split()
        c_list = curr_balls.strip().split()
        while len(c_list) < 6:
            c_list.append("•")

        data["over_prev_num"] = f"OVER {prev_ov}:"
        data["over_prev_balls"] = p_list[:6]
        data["over_prev_total"] = f"= {prev_tot}"

        data["over_curr_num"] = f"OVER {curr_ov}:"
        data["over_curr_balls"] = c_list[:6]
        data["over_curr_total"] = f"= {curr_tot}"

        bowled = [b for b in c_list if b != "•"]
        if bowled:
            data["last_ball"] = bowled[-1]
        elif p_list:
            data["last_ball"] = p_list[-1]

    batter_matches = re.findall(
        r"\b([A-Z]\s+[A-Za-z]+)\s+(\d+)\s*\((\d+)\)\s+(\d+)\s+(\d+)\s+([\d.]+)",
        page_text,
    )
    bowler_match = re.search(
        r"\b([A-Z]\s+[A-Za-z]+)\s+(\d+-\d+)\s+(\d+(?:\.\d+)?)\s+([\d.]+)",
        page_text,
    )

    names = []
    if len(batter_matches) >= 2:
        names.extend([batter_matches[0][0], batter_matches[1][0]])
    if bowler_match:
        names.append(bowler_match.group(1))
    player_imgs = find_player_images(soup, base_url, names)

    if len(batter_matches) >= 2:
        b1, b2 = batter_matches[0], batter_matches[1]
        data["striker"] = {
            "name": b1[0].upper(),
            "runs": b1[1],
            "balls": f"({b1[2]})",
            "fours": b1[3],
            "sixes": b1[4],
            "sr": b1[5],
            "image": player_imgs.get(b1[0], data.get("striker", {}).get("image", "")),
        }
        data["non_striker"] = {
            "name": b2[0].upper(),
            "runs": b2[1],
            "balls": f"({b2[2]})",
            "fours": b2[3],
            "sixes": b2[4],
            "sr": b2[5],
            "image": player_imgs.get(b2[0], data.get("non_striker", {}).get("image", "")),
        }

    if bowler_match:
        bw_name = bowler_match.group(1)
        data["bowler"] = {
            "name": bw_name.upper(),
            "figures": bowler_match.group(2),
            "overs": f"({bowler_match.group(3)})",
            "econ": bowler_match.group(4),
            "image": player_imgs.get(bw_name, data.get("bowler", {}).get("image", "")),
        }

    lw_match = re.search(
        r"Last Wkt:\s*([A-Za-z.\s]+\d+\s*\(\d+\))", page_text, re.IGNORECASE
    )
    if lw_match:
        data["last_wicket"] = f"LAST WKT: {lw_match.group(1).strip().upper()}"

    return data


def refresh_match(match_id, force=False):
    with db_lock:
        db = load_db()
        if match_id not in db:
            return None
        match_data = db[match_id]

    now = time.time()
    last_time = _fetch_timestamps.get(match_id, 0.0)
    if force or (now - last_time >= CACHE_TTL_SECONDS):
        crex_url = match_data.get("crex_url", "")
        if crex_url:
            try:
                res = requests.get(crex_url, headers=HEADERS, timeout=8)
                if res.status_code == 200:
                    with db_lock:
                        db = load_db()
                        if match_id in db:
                            db[match_id] = parse_crex_page(res.text, db[match_id])
                            save_db(db)
                            match_data = db[match_id]
                            _fetch_timestamps[match_id] = now
            except Exception as exc:
                print(f"[{match_id}] Fetch error: {exc}")

    return match_data


# --- PUBLIC & OBS ROUTES ---

@app.route("/")
def serve_admin():
    return send_from_directory(".", "admin.html")


@app.route("/obs/<match_id>")
def serve_overlay(match_id):
    return send_from_directory(".", "overlay.html")


@app.route("/api/match/<match_id>")
def get_match_api(match_id):
    data = refresh_match(match_id)
    if not data:
        return jsonify({"error": "Match not found"}), 404
    return jsonify(data)


# --- ADMIN MANAGEMENT API ---

@app.route("/api/matches", methods=["GET"])
def list_matches():
    with db_lock:
        db = load_db()
    summary = [
        {
            "id": mid,
            "title": info.get("title", mid),
            "crex_url": info.get("crex_url", ""),
            "score": info.get("score", "0/0"),
            "overs": info.get("overs", "0.0"),
            "status": info.get("match_status", ""),
        }
        for mid, info in db.items()
    ]
    return jsonify(summary)


@app.route("/api/matches", methods=["POST"])
def create_or_update_match():
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    raw_slug = body.get("slug", "").strip().lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", raw_slug).strip("-")
    crex_url = body.get("crex_url", "").strip()
    title = body.get("title", "").strip()

    if not slug or not crex_url:
        return jsonify({"error": "Match ID and CREX URL are required"}), 400

    with db_lock:
        db = load_db()
        existing = db.get(slug, default_match_payload(crex_url, title))
        existing["crex_url"] = crex_url
        if title:
            existing["title"] = title
        db[slug] = existing
        save_db(db)

    # Immediately scrape live score so the URL is ready right away
    refresh_match(slug, force=True)
    return jsonify({"ok": True, "id": slug})


@app.route("/api/matches/<match_id>", methods=["DELETE"])
def delete_match(match_id):
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    with db_lock:
        db = load_db()
        if match_id in db:
            del db[match_id]
            save_db(db)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
