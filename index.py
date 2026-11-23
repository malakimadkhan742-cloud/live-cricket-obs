import json
import os
import re
import threading
import time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory
import requests

CREX_URL = os.environ.get(
    "CREX_URL",
    "https://crex.com/cricket-live-score/ban-vs-sa-2nd-test-bangladesh-tour-of-south-africa-2026-match-updates-10MA",
)
JSON_FILE = "match_data.json"
POLL_INTERVAL_SECONDS = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

app = Flask(__name__, static_folder=".")
data_lock = threading.Lock()


def load_existing_data():
    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def find_player_images(soup, player_names):
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
                found[name] = urljoin(CREX_URL, src)
                break
    return found


def parse_crex_page(html_text, data):
    soup = BeautifulSoup(html_text, "html.parser")
    page_text = " ".join(soup.stripped_strings)

    score_match = re.search(
        r"\b(SA|BAN)\s+(\d+[-/]\d+)\s*\(?(\d+\.\d+|\d+)\)?", page_text
    )
    if score_match:
        team_code, raw_score, overs = score_match.groups()
        data["score"] = raw_score.replace("-", "/")
        data["overs"] = overs
        if team_code == "SA":
            data["batting_team"] = "SOUTH AFRICA"
            data["bowling_team"] = "BANGLADESH"
            data["batting_flag"] = "https://flagcdn.com/w160/za.png"
            data["bowling_flag"] = "https://flagcdn.com/w160/bd.png"
        else:
            data["batting_team"] = "BANGLADESH"
            data["bowling_team"] = "SOUTH AFRICA"
            data["batting_flag"] = "https://flagcdn.com/w160/bd.png"
            data["bowling_flag"] = "https://flagcdn.com/w160/za.png"

    crr_match = re.search(r"CRR:\s*([\d.]+)", page_text)
    if crr_match:
        data["crr"] = crr_match.group(1)

    pship_match = re.search(r"P'ship\s*:\s*(\d+\s*\(\d+\))", page_text)
    if pship_match:
        data["partnership"] = pship_match.group(1)

    status_match = re.search(
        r"((?:SA|BAN)\s+(?:trail by|lead by|need|opt to)[^|.]+)",
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
        r"([A-Z]\s+[A-Za-z]+)\s+(\d+)\s*\((\d+)\)\s+(\d+)\s+(\d+)\s+([\d.]+)",
        page_text,
    )
    bowler_match = re.search(
        r"([A-Z]\s+[A-Za-z]+)\s+(\d+-\d+)\s+(\d+(?:\.\d+)?)\s+([\d.]+)",
        page_text,
    )

    names = []
    if len(batter_matches) >= 2:
        names.extend([batter_matches[0][0], batter_matches[1][0]])
    if bowler_match:
        names.append(bowler_match.group(1))
    player_imgs = find_player_images(soup, names)

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
            "on_strike": True,
        }
        data["non_striker"] = {
            "name": b2[0].upper(),
            "runs": b2[1],
            "balls": f"({b2[2]})",
            "fours": b2[3],
            "sixes": b2[4],
            "sr": b2[5],
            "image": player_imgs.get(b2[0], data.get("non_striker", {}).get("image", "")),
            "on_strike": False,
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

    return data


def background_scraper():
    last_snapshot = ""
    while True:
        try:
            response = requests.get(CREX_URL, headers=HEADERS, timeout=10)
            if response.status_code == 200:
                with data_lock:
                    current_data = load_existing_data()
                    updated_data = parse_crex_page(response.text, current_data)
                    serialized = json.dumps(updated_data, indent=2)
                    if serialized != last_snapshot:
                        with open(JSON_FILE, "w", encoding="utf-8") as f:
                            f.write(serialized)
                        last_snapshot = serialized
        except Exception as exc:
            print(f"Scraper error: {exc}")
        time.sleep(POLL_INTERVAL_SECONDS)


# Start background scraper thread automatically when server boots
threading.Thread(target=background_scraper, daemon=True).start()


@app.route("/")
def serve_index():
    return send_from_directory(".", "index.html")


@app.route("/style.css")
def serve_css():
    return send_from_directory(".", "style.css")


@app.route("/match_data.json")
def serve_json():
    with data_lock:
        return jsonify(load_existing_data())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)