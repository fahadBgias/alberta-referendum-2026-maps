#!/usr/bin/env python3
"""Fetch the latest unofficial results of Alberta's 2026 referendum, division by division.

Source: the JSON feed behind Elections Alberta's results page, https://results.elections.ab.ca/8400
(the same data that page loads in your browser; it refreshes itself every 60 seconds).

Writes three files in data/:
  results.json   what the web page loads
  results.csv    one row per division and question, for anyone who wants the data
  history.csv    province-wide totals each time the count moves, so the night can be replayed

Checks before writing:
  - all 87 divisions are present for every question
  - the divisions add up to the feed's own province-wide totals (reported as a warning if not)
  - any division where a question has more votes than the ballots counted there on Question 10
    is flagged, shown as published, and listed on the page

If nothing changed since the last run, no file is touched (so the workflow makes no commit).

Standard library only.
Exit codes: 0 = written or nothing new, 1 = the feed could not be read or failed validation
(existing files are left as they were).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE_URL = "https://results.elections.ab.ca"
EVENT_ID = "8400"
USER_AGENT = os.environ.get(
    "RESULTS_USER_AGENT",
    "alberta-referendum-maps/1.0 (open-source results map; checks every 15 minutes)",
)
ROOT = Path(__file__).resolve().parent.parent
N_DIVISIONS = 87
FLAG_TOLERANCE = 1.02  # allow 2% for counting teams reporting a few minutes apart

DIVISIONS = [
    "Calgary-Acadia", "Calgary-Beddington", "Calgary-Bow", "Calgary-Buffalo", "Calgary-Cross",
    "Calgary-Currie", "Calgary-East", "Calgary-Edgemont", "Calgary-Elbow", "Calgary-Falconridge",
    "Calgary-Fish Creek", "Calgary-Foothills", "Calgary-Glenmore", "Calgary-Hays", "Calgary-Klein",
    "Calgary-Lougheed", "Calgary-Bhullar-McCall", "Calgary-Mountain View", "Calgary-North",
    "Calgary-North East", "Calgary-North West", "Calgary-Peigan", "Calgary-Shaw", "Calgary-South East",
    "Calgary-Varsity", "Calgary-West", "Edmonton-Beverly-Clareview", "Edmonton-Castle Downs",
    "Edmonton-City Centre", "Edmonton-Decore", "Edmonton-Ellerslie", "Edmonton-Glenora",
    "Edmonton-Gold Bar", "Edmonton-Highlands-Norwood", "Edmonton-Manning", "Edmonton-McClung",
    "Edmonton-Meadows", "Edmonton-Mill Woods", "Edmonton-North West", "Edmonton-Riverview",
    "Edmonton-Rutherford", "Edmonton-South", "Edmonton-South West", "Edmonton-Strathcona",
    "Edmonton-West Henday", "Edmonton-Whitemud", "Airdrie-Cochrane", "Airdrie-East",
    "Athabasca-Barrhead-Westlock", "Banff-Kananaskis", "Bonnyville-Cold Lake-St. Paul",
    "Brooks-Medicine Hat", "Camrose", "Cardston-Siksika", "Central Peace-Notley",
    "Chestermere-Strathmore", "Cypress-Medicine Hat", "Drayton Valley-Devon", "Drumheller-Stettler",
    "Fort McMurray-Lac La Biche", "Fort McMurray-Wood Buffalo", "Fort Saskatchewan-Vegreville",
    "Grande Prairie", "Grande Prairie-Wapiti", "Highwood", "Innisfail-Sylvan Lake",
    "Lac Ste. Anne-Parkland", "Lacombe-Ponoka", "Leduc-Beaumont", "Lesser Slave Lake",
    "Lethbridge-East", "Lethbridge-West", "Livingstone-Macleod", "Maskwacis-Wetaskiwin",
    "Morinville-St. Albert", "Olds-Didsbury-Three Hills", "Peace River", "Red Deer-North",
    "Red Deer-South", "Rimbey-Rocky Mountain House-Sundre", "Sherwood Park",
    "Spruce Grove-Stony Plain", "St. Albert", "Strathcona-Sherwood Park", "Taber-Warner",
    "Vermilion-Lloydminster-Wainwright", "West Yellowhead",
]

# Question number on the ballot -> labels and wording. "a" is the feed's yesVotes, "b" its noVotes.
QUESTIONS = {
    10: ("Separation", "Remain", "Referendum",
         "Should Alberta remain a province of Canada, or should the Government of Alberta commence the legal process required under the Canadian Constitution to hold a binding provincial referendum on whether or not Alberta should separate from Canada?"),
    1: ("Immigration control", "Yes", "No",
        "Do you support the Government of Alberta taking increased control over immigration for the purposes of decreasing immigration to more sustainable levels, prioritizing economic migration and giving Albertans first priority on new employment opportunities?"),
    2: ("Program eligibility", "Yes", "No",
        "Do you support the Government of Alberta introducing a law mandating that only Canadian citizens, permanent residents and individuals with an Alberta-approved immigration status will be eligible for provincially-funded programs, such as health care, education and other social services?"),
    3: ("12-month wait for supports", "Yes", "No",
        "Assuming that all Canadian citizens and permanent residents continue to qualify for social support programs as they do now, do you support the Government of Alberta introducing a law requiring all individuals with a non-permanent legal immigration status to reside in Alberta for at least 12 months before qualifying for any provincially-funded social support programs?"),
    4: ("Health and education fee", "Yes", "No",
        "Assuming that all Canadian citizens and permanent residents continue to qualify for public health care and education as they do now, do you support the Government of Alberta charging a reasonable fee or premium to individuals with a non-permanent immigration status living in Alberta for their and their family's use of the health care and education systems?"),
    5: ("Proof of citizenship to vote", "Yes", "No",
        "Do you support the Government of Alberta introducing a law requiring individuals to provide proof of citizenship, such as a passport, birth certificate or citizenship card, to vote in an Alberta provincial election?"),
    6: ("Provinces pick judges", "Yes", "No",
        "Do you support the Government of Alberta working with the governments of other willing provinces to amend the Canadian Constitution to have provincial governments, and not the federal government, select the justices appointed to provincial King's Bench and Appeal courts?"),
    7: ("Abolish the Senate", "Yes", "No",
        "Do you support the Government of Alberta working with the governments of other willing provinces to amend the Canadian Constitution to abolish the unelected federal Senate?"),
    8: ("Opt out of federal programs", "Yes", "No",
        "Do you support the Government of Alberta working with the governments of other willing provinces to amend the Canadian Constitution to allow provinces to opt out of federal programs that intrude on provincial jurisdiction such as health care, education and social services, without a province losing any of the associated federal funding for use in its social programs?"),
    9: ("Provincial laws first", "Yes", "No",
        "Do you support the Government of Alberta working with the governments of other willing provinces to amend the Canadian Constitution to better protect provincial rights from federal interference by giving a province's laws dealing with provincial or shared areas of constitutional jurisdiction priority over federal laws when the province's laws and federal laws conflict?"),
}
ORDER = [10, 1, 2, 3, 4, 5, 6, 7, 8, 9]  # the page lists Question 10 first
# The feed's internal IDs. Question 10 is stored as 11. Re-read from the results page if these stop working.
DEFAULT_IDS = {**{n: n for n in range(1, 10)}, 10: 11}


class FeedError(RuntimeError):
    pass


def http_get(url: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 * 2 ** attempt)
    raise FeedError(f"Could not reach {url}: {last}")


def read_page_config(base_url: str) -> tuple[str | None, dict[int, int]]:
    """Read the data version and question IDs that the results page embeds in its HTML."""
    html = http_get(f"{base_url}/{EVENT_ID}").decode("utf-8", "replace")
    m = re.search(r"version:\s*'([^']+)'", html)
    version = m.group(1) if m else None
    ids: dict[int, int] = {}
    start = html.find("questions:")
    if start != -1:
        try:
            items, _ = json.JSONDecoder().raw_decode(html[html.index("[", start):])
            for q in items:
                ids[int(q["questionNumber"])] = int(q["questionID"])
        except (ValueError, KeyError, TypeError):
            pass
    return version, ids


def fetch_question(base_url: str, qid: int, version: str | None) -> dict | None:
    query = (f"version={version}&" if version else "") + f"questions={qid}&wards="
    try:
        data = json.loads(http_get(f"{base_url}/data/{EVENT_ID}?{query}"))
    except json.JSONDecodeError as exc:
        raise FeedError(f"Question ID {qid}: response was not JSON ({exc})") from exc
    # A stale or missing version comes back as {"forceFullRefresh": true} with no results.
    if not data.get("success") or data.get("forceFullRefresh") or not data.get("referendumResults"):
        return None
    return data


def fetch_all(base_url: str) -> tuple[dict[int, dict], dict[int, int]]:
    version, ids, config_read = None, dict(DEFAULT_IDS), False
    responses = {}
    for number in ORDER:
        data = fetch_question(base_url, ids[number], version)
        if data is None and not config_read:
            version, page_ids = read_page_config(base_url)
            ids.update(page_ids)
            config_read = True
            print(f"Read page config: version={version}, question IDs={page_ids or 'unchanged'}")
            data = fetch_question(base_url, ids[number], version)
        if data is None:
            raise FeedError(f"Question {number} (ID {ids[number]}): the feed returned no results")
        responses[number] = data
        time.sleep(0.5)  # be gentle: ten small requests, spaced out
    return responses, ids


def clean_timestamp(raw: str) -> str:
    """'2026-10-20T19:15:36.4810290+00:00' -> '2026-10-20T19:15:36Z' (safe for every browser)."""
    dt = datetime.fromisoformat(re.sub(r"\.\d+", "", raw))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(responses: dict[int, dict], ids: dict[int, int]) -> dict:
    warnings, questions = [], {}
    for number in ORDER:
        rr = responses[number]["referendumResults"]
        stats = next((s for s in rr["votes"]["questionStats"] if s["questionID"] == ids[number]), None)
        if stats is None:
            raise FeedError(f"Question {number}: ID {ids[number]} missing from the response")
        votes = {int(w["ward"]): [int(w["yesVotes"] or 0), int(w["noVotes"] or 0)] for w in stats["wardStats"]}
        missing = [e for e in range(1, N_DIVISIONS + 1) if e not in votes]
        if missing:
            raise FeedError(f"Question {number}: divisions missing from the feed: {missing}")
        total = [int(stats["yesVotes"] or 0), int(stats["noVotes"] or 0)]
        summed = [sum(v[0] for v in votes.values()), sum(v[1] for v in votes.values())]
        if summed != total:
            warnings.append(f"Question {number}: divisions add up to {summed}, feed total is {total}")
        short, a, b, text = QUESTIONS[number]
        questions[str(number)] = {"short": short, "text": text, "a": a, "b": b, "total": total,
                                  "votes": {str(e): votes[e] for e in range(1, N_DIVISIONS + 1)}}

    latest = max(responses.values(), key=lambda d: d["lastUpdated"])
    rr = latest["referendumResults"]
    polls = {str(int(w["ward"])): [int(w["pollsReporting"]), int(w["totalPolls"])] for w in rr["wards"]}
    summary = rr["widgets"]["summary"]

    flags = []
    for number in range(1, 10):
        for e in range(1, N_DIVISIONS + 1):
            t = sum(questions[str(number)]["votes"][str(e)])
            ballots = sum(questions["10"]["votes"][str(e)])
            if ballots and t > ballots * FLAG_TOLERANCE:
                flags.append({"q": number, "ed": e, "total": t, "ballots": ballots})

    return {
        "updated": clean_timestamp(latest["lastUpdated"]),
        "pollsReporting": int(summary["pollsReporting"]),
        "totalPolls": int(summary["totalPolls"]),
        "names": DIVISIONS,
        "order": ORDER,
        "questions": questions,
        "polls": polls,
        "flags": flags,
        "warnings": warnings,
        "source": f"{DEFAULT_BASE_URL}/{EVENT_ID}",
    }


def same_count(old: dict, new: dict) -> bool:
    keys = ("pollsReporting", "questions", "polls")
    return all(old.get(k) == new.get(k) for k in keys)


def write_outputs(data_dir: Path, out: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "results.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))

    with open(data_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["division_number", "division_name", "question", "question_short", "option_a", "option_b",
                    "votes_a", "votes_b", "share_a_pct", "leader", "voting_areas_reported",
                    "voting_areas_total", "data_flag", "updated_utc"])
        flagged = {(f["q"], f["ed"]) for f in out["flags"]}
        for number in sorted(QUESTIONS):
            q = out["questions"][str(number)]
            for e in range(1, N_DIVISIONS + 1):
                va, vb = q["votes"][str(e)]
                t = va + vb
                leader = "" if not t else (q["a"] if va > vb else q["b"] if vb > va else "Tie")
                pr = out["polls"].get(str(e), ["", ""])
                w.writerow([e, DIVISIONS[e - 1], number, q["short"], q["a"], q["b"], va, vb,
                            f"{100 * va / t:.2f}" if t else "", leader, pr[0], pr[1],
                            "votes exceed Q10 ballots" if (number, e) in flagged else "", out["updated"]])

    history = data_dir / "history.csv"
    new_file = not history.exists()
    with open(history, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["updated_utc", "question", "votes_a", "votes_b", "voting_areas_reported", "voting_areas_total"])
        for number in sorted(QUESTIONS):
            ta, tb = out["questions"][str(number)]["total"]
            w.writerow([out["updated"], number, ta, tb, out["pollsReporting"], out["totalPolls"]])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base-url", default=os.environ.get("RESULTS_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("RESULTS_DATA_DIR", ROOT / "data")))
    args = p.parse_args(argv)
    base_url = args.base_url.rstrip("/")

    try:
        responses, ids = fetch_all(base_url)
        out = build(responses, ids)
    except FeedError as exc:
        print(f"::error::{exc}")
        return 1

    for w in out["warnings"]:
        print(f"::warning::{w}")

    current = args.data_dir / "results.json"
    if current.exists():
        try:
            if same_count(json.loads(current.read_text()), out):
                print(f"No change since the last run (feed updated {out['updated']}).")
                return 0
        except json.JSONDecodeError:
            pass

    write_outputs(args.data_dir, out)
    q10 = out["questions"]["10"]["total"]
    print(f"Updated: {out['pollsReporting']:,} of {out['totalPolls']:,} voting areas; "
          f"Q10 Remain {q10[0]:,} vs Referendum {q10[1]:,}; {len(out['flags'])} data flags.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
