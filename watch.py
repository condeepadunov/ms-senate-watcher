"""
Watches the 2026 U.S. Senate election in Mississippi and sends a push
notification (via ntfy) every time the general-election vote totals change.

Results sources, tried in order (the one that worked last time is tried first):
  1. CNN's live-results data file   (AP numbers)
  2. The New York Times' results data file (AP numbers)
  3. Wikipedia's race page (backup; only updates when editors add numbers)
The run log says which source each check used.

Settings come from environment variables (set in the GitHub workflow file):
  NTFY_TOPIC      your private ntfy topic name (stored as a GitHub secret)
  LOOP_MINUTES    how long one run keeps watching before it exits (default 115)
  INTERVAL        seconds between checks (default 60)
  FORCE_NOTIFY    if "1", send the current numbers once and exit (for testing)
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PAGE = "2026_United_States_Senate_election_in_Mississippi"
# The results table we want is the one that contains both of these names
# (this skips the primary-election tables on the same page).
MUST_INCLUDE = ["Hyde-Smith", "Colom"]

API = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
    {"action": "parse", "page": PAGE, "prop": "wikitext|revid", "format": "json",
     "formatversion": "2"}
)
USER_AGENT = "ms-senate-watcher/1.0 (personal results-notification script)"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
FAILS_BEFORE_ALERT = 3


# ---------------------------------------------------------------- fetching ---

JSON_SOURCES = {
    "CNN": "https://politics-elex-results.data.api.cnn.io/results/view/2026-SG-MS.json",
    "NYT": "https://static01.nyt.com/elections-assets/pages/data/2026-11-03/results-mississippi.json",
}
SURNAMES = ["Hyde-Smith", "Colom", "Pinkins"]
VOTE_KEYS = ["voteNum", "votes", "vote_count", "voteCount", "vote", "popularVote", "votesCast"]
NAME_KEYS = ["lastName", "last_name", "lastNameOrSurname", "name", "fullName", "candidateName", "nyt_id"]


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_wikitext():
    data = json.loads(_get(API))
    if "error" in data:
        raise RuntimeError(f"Wikipedia API error: {data['error'].get('info')}")
    return data["parse"]["wikitext"], data["parse"].get("revid")


def _votes_of(d):
    for k in VOTE_KEYS:
        v = d.get(k)
        if isinstance(v, dict):
            v = v.get("total", v.get("count"))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
        if isinstance(v, str) and re.fullmatch(r"[\d,]+", v.strip()):
            return int(v.replace(",", ""))
    return None


def _name_of(d):
    text = " ".join(str(v) for v in d.values() if isinstance(v, (str, int)))
    for s in SURNAMES:
        if s.lower() in text.lower():
            return s
    for k in NAME_KEYS:
        if isinstance(d.get(k), str) and d[k].strip():
            return d[k].strip()
    return None


def parse_json_results(data):
    """Find the statewide list of candidates in any results JSON.

    Looks for every list of candidate-like objects that includes both
    Hyde-Smith and Colom with vote counts, and keeps the one with the most
    votes (the statewide total rather than a single county).
    """
    best = None
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
            rows = []
            for item in node:
                if isinstance(item, dict):
                    n, v = _name_of(item), _votes_of(item)
                    if n and v is not None:
                        rows.append((n, v))
            names = {n for n, _ in rows}
            if all(s in names for s in MUST_INCLUDE):
                total = sum(v for _, v in rows)
                if total > 0 and (best is None or total > best[0]):
                    best = (total, rows)
    if not best:
        raise ValueError("No statewide vote counts for both candidates in this file.")
    return {"candidates": [r for r in best[1] if r[1] > 0]}


def get_results(prefer=None):
    """Try each source; return (results, source_name). Raises if all fail."""
    order = list(JSON_SOURCES) + ["Wikipedia"]
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    errors = []
    for name in order:
        try:
            if name == "Wikipedia":
                wikitext, _ = fetch_wikitext()
                return parse_results(wikitext), name
            return parse_json_results(json.loads(_get(JSON_SOURCES[name]))), name
        except Exception as e:
            errors.append(f"{name}: {str(e)[:120]}")
    raise RuntimeError(" | ".join(errors))


# ----------------------------------------------------------------- parsing ---

def _clean(value):
    value = re.sub(r"<ref[^>]*/>", "", value)
    value = re.sub(r"<ref[^>]*>.*?</ref>", "", value, flags=re.S)
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", value)  # [[A|B]] -> B
    value = re.sub(r"'{2,}", "", value)  # bold/italic markup
    return value.strip()


def _templates(text, prefix):
    """Yield the inside of every {{prefix...}} template, respecting nested {{ }}."""
    for m in re.finditer(r"\{\{\s*" + re.escape(prefix), text, flags=re.I):
        depth, i = 0, m.start()
        while i < len(text) - 1:
            pair = text[i:i + 2]
            if pair == "{{":
                depth += 1
                i += 2
            elif pair == "}}":
                depth -= 1
                i += 2
                if depth == 0:
                    inner = text[m.start() + 2:i - 2]
                    yield inner.split("|", 1)[1] if "|" in inner else ""
                    break
            else:
                i += 1


def _params(template_body):
    """Turn 'a = 1 | b = 2' into {'a': '1', 'b': '2'} (good enough for these boxes)."""
    # Remove refs and nested templates first so their '|' and digits don't interfere.
    body = re.sub(r"<ref[^>]*>.*?</ref>", "", template_body, flags=re.S)
    body = re.sub(r"<ref[^>]*/>", "", body)
    while re.search(r"\{\{[^{}]*\}\}", body):
        body = re.sub(r"\{\{[^{}]*\}\}", "", body)
    out = {}
    for part in body.split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = _clean(v)
    return out


def _to_int(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def parse_results(wikitext):
    """Return {"candidates": [(name, votes), ...]} for the general-election box."""
    boxes = re.findall(r"\{\{\s*Election box begin(.*?)\{\{\s*Election box end",
                       wikitext, flags=re.S | re.I)
    for box in boxes:
        candidates = []
        for body in _templates(box, "Election box candidate"):
            p = _params(body)
            name = p.get("candidate", "")
            name = re.sub(r"\s*\((?:incumbent|inc\.?)\)\s*", "", name, flags=re.I).strip()
            votes = _to_int(p.get("votes"))
            if name:
                candidates.append((name, votes))
        names = " ".join(n for n, _ in candidates)
        if all(key.lower() in names.lower() for key in MUST_INCLUDE):
            counted = [(n, v) for n, v in candidates if v]
            if not counted:
                raise ValueError("Found the results table, but it has no vote counts yet.")
            return {"candidates": counted}
    raise ValueError("Couldn't find a results table containing "
                     + " and ".join(MUST_INCLUDE) + ".")


# ------------------------------------------------------------ notification ---

def short(name):
    return name.split()[-1]


def build_message(current, previous):
    cands = sorted(current["candidates"], key=lambda c: -c[1])
    total = sum(v for _, v in cands)
    lines = [f"{short(n)}: {v:,} ({v / total:.2%})" for n, v in cands]

    leader, runner = cands[0], cands[1]
    margin = leader[1] - runner[1]
    lead_line = f"{short(leader[0])} leads by {margin:,} votes"

    if previous:
        prev = dict(previous["candidates"])
        pc = sorted(previous["candidates"], key=lambda c: -c[1])
        if len(pc) >= 2:
            prev_margin = pc[0][1] - pc[1][1]
            lead_line += f" (was {short(pc[0][0])} +{prev_margin:,})"
        changes = []
        for n, v in cands:
            if n in prev and v != prev[n]:
                changes.append(f"{short(n)} {v - prev[n]:+,}")
        if changes:
            lines.append("New votes: " + ", ".join(changes))

    title = "MS Senate: " + (f"{short(leader[0])} +{margin:,}" if margin else "TIED")
    return title, lead_line + "\n" + "\n".join(lines)


def notify(title, body, priority="high", tags="ballot_box_with_ballot"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("!!! NTFY_TOPIC SECRET IS MISSING OR EMPTY - no notification sent. Would have sent:\n",
              title, "\n", body)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=body.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags,
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print(f"Sent notification to topic ending '...{topic[-4:]}':", title)
    except Exception as e:
        print("!!! Could not send notification:", e)


# ------------------------------------------------------------------- state ---

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state, commit=True):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    if commit and os.environ.get("GITHUB_ACTIONS"):
        # Save the latest numbers to the repo so the next run knows what it already sent.
        try:
            subprocess.run(["git", "config", "user.name", "results-watcher"], check=True)
            subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
            subprocess.run(["git", "add", STATE_FILE], check=True)
            subprocess.run(["git", "commit", "-q", "-m", "Update latest results"], check=False)
            subprocess.run(["git", "pull", "-q", "--rebase"], check=False)
            subprocess.run(["git", "push", "-q"], check=False)
        except Exception as e:  # never let a save problem stop the watching
            print("Could not save state to repo:", e)


# -------------------------------------------------------------------- main ---

def check_once(state):
    """One check. Returns the (possibly updated) state."""
    try:
        current, source = get_results(prefer=state.get("source"))
    except Exception as e:
        state["fail_count"] = state.get("fail_count", 0) + 1
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] check failed ({state['fail_count']}):", e)
        if state["fail_count"] == FAILS_BEFORE_ALERT and not state.get("fail_alerted"):
            notify("MS Senate watcher: can't read results",
                   f"{FAILS_BEFORE_ALERT} checks in a row failed. Last error: {e}",
                   priority="default", tags="warning")
            state["fail_alerted"] = True
            save_state(state)
        return state

    if state.get("fail_alerted"):
        notify("MS Senate watcher: working again", "Results are readable again.",
               priority="low", tags="white_check_mark")
    state["fail_count"] = 0
    state["fail_alerted"] = False

    previous = state.get("last")
    first_ever = previous is None
    if first_ever or current["candidates"] != [tuple(c) for c in previous["candidates"]]:
        title, body = build_message(current, None if first_ever else previous)
        if first_ever:
            title = "MS Senate watcher is on"
            body = "You'll get an alert each time the totals change.\n\n" + body
        notify(title, body + f"\n(Source: {source})")
        state["last"] = current
        state["source"] = source
        state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state)
    else:
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] no change ({source})")
    return state


def main():
    sys.stdout.reconfigure(line_buffering=True)  # show log lines as they happen
    state = load_state()
    # Each run alerts once if it can't read results, even if an earlier run already did.
    state["fail_count"] = 0
    state["fail_alerted"] = False
    print("Watcher started. Checking every", os.environ.get("INTERVAL", "60"), "seconds.")

    if os.environ.get("FORCE_NOTIFY") == "1":
        current, source = get_results()
        title, body = build_message(current, None)
        notify("TEST · " + title, body + f"\n(Source: {source})")
        return

    if not os.environ.get("NTFY_TOPIC", "").strip():
        print("!!! NTFY_TOPIC SECRET IS MISSING OR EMPTY - notifications won't be sent.")

    loop_minutes = float(os.environ.get("LOOP_MINUTES", "115"))
    interval = int(os.environ.get("INTERVAL", "60"))
    stop_at = time.time() + loop_minutes * 60
    while True:
        state = check_once(state)
        if time.time() + interval > stop_at:
            break
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
