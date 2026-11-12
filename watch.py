"""
Watches the 2026 U.S. Senate election in Alaska and sends a push
notification (via ntfy) every time the first-choice vote totals change.

Each alert shows the leader's margin, vote total and share, how far the
leader is from 50%, and whether Peltola leads both Dan Sullivans combined.
The two Sullivans are told apart by vote count (the incumbent has far more).

Results sources, tried in order (the one that worked last time is tried first):
  1. The New York Times' results data file (AP numbers)
  2. Wikipedia's race page (backup; only updates when editors add numbers)
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

RACE = "AK Senate"
PAGE = "2026_United_States_Senate_election_in_Alaska"
# The results table we want is the one that contains both of these names.
MUST_INCLUDE = ["Peltola", "Sullivan"]
FOCUS = "Peltola"              # compared against...
COMBINE = "Sullivan"           # ...every candidate with this surname, added together
OTHER_SULLIVAN = "Dan J. Sullivan"  # label for the Sullivan with fewer votes

API = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
    {"action": "parse", "page": PAGE, "prop": "wikitext|revid", "format": "json",
     "formatversion": "2"}
)
USER_AGENT = "senate-results-watcher/1.0 (personal results-notification script)"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
FAILS_BEFORE_ALERT = 3


# ---------------------------------------------------------------- fetching ---

JSON_SOURCES = {
    "NYT": "https://static01.nyt.com/elections-assets/pages/data/2026-11-03/results-alaska.json",
}
SURNAMES = ["Peltola", "Sullivan", "Heikes"]
LAST_NAME_KEYS = ["last_name", "lastName", "lastNameOrSurname", "surname"]
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


_ID_MAP = {}  # candidate ID code -> surname, rebuilt for each file


def _build_id_map(data):
    """Find candidate info entries (with names) and remember their ID codes,
    so vote entries that only carry an ID can be matched to a name."""
    found = {}
    stack = [(None, data)]
    while stack:
        key, node = stack.pop()
        if isinstance(node, dict):
            strings = {k: v for k, v in node.items() if isinstance(v, (str, int))
                       and not isinstance(v, bool)}
            text = " ".join(str(v) for v in strings.values()).lower()
            hits = [s for s in SURNAMES if s.lower() in text]
            # A last-name field settles it even if a bio mentions other candidates.
            for k in LAST_NAME_KEYS:
                ln = str(node.get(k, "")).strip().lower()
                exact = [s for s in SURNAMES if s.lower() == ln]
                if exact:
                    hits = exact
                    break
            if len(hits) == 1:
                ids = [str(v) for v in strings.values()
                       if re.fullmatch(r"[\w\-]{2,40}", str(v)) and re.search(r"[\d\-_]", str(v))]
                if key is not None:
                    ids.append(str(key))
                for i in ids:
                    found.setdefault(i, set()).add(hits[0])
            stack.extend(node.items())
        elif isinstance(node, list):
            stack.extend((None, v) for v in node)
    _ID_MAP.clear()
    _ID_MAP.update({i: s.pop() for i, s in found.items() if len(s) == 1})


def _name_of(d):
    for k in LAST_NAME_KEYS:
        ln = str(d.get(k, "")).strip().lower()
        for s in SURNAMES:
            if s.lower() == ln:
                return s
    text = " ".join(str(v) for v in d.values() if isinstance(v, (str, int)))
    for s in SURNAMES:
        if s.lower() in text.lower():
            return s
    for v in d.values():
        if isinstance(v, (str, int)) and str(v) in _ID_MAP:
            return _ID_MAP[str(v)]
    for k in NAME_KEYS:
        if isinstance(d.get(k), str) and d[k].strip():
            return d[k].strip()
    return None


def parse_json_results(data):
    """Find the statewide list of candidates in any results JSON.

    Looks for every list of candidate-like objects that includes both
    the MUST_INCLUDE candidates with vote counts, and keeps the one with the most
    votes (the statewide total rather than a single county).
    """
    _build_id_map(data)
    best = None
    stack = [data]
    while stack:
        node = stack.pop()
        items = None
        if isinstance(node, dict):
            stack.extend(node.values())
            # Candidates stored by ID, e.g. {"colom-s": {"votes": ...}, ...}
            if len(node) >= 2 and all(isinstance(v, dict) for v in node.values()):
                items = [dict(v, _key=k) for k, v in node.items()]
        elif isinstance(node, list):
            stack.extend(node)
            items = [i for i in node if isinstance(i, dict)]
        if items:
            rows = []
            for item in items:
                v = _votes_of(item)
                if v is not None:
                    # Keep unnamed entries (e.g. write-ins) so the 50% math is right.
                    rows.append((_name_of(item) or "Write-ins/other", v))
            names = {n for n, _ in rows}
            if all(s in names for s in MUST_INCLUDE):
                total = sum(v for _, v in rows)
                # Most votes = statewide first-choice count (not a county, not a later RCV round)
                if total > 0 and (best is None or total > best[0]):
                    best = (total, rows)
    if not best:
        raise ValueError("No statewide vote counts for both candidates in this file.")
    return normalize(best[1])


def normalize(rows):
    """Give every candidate a short, unique label.

    Both Dan Sullivans usually show up as plain "Sullivan"; the one with more
    votes is the incumbent and keeps the plain label.
    """
    labeled = []
    for name, votes in rows:
        label = next((s for s in SURNAMES if s.lower() in name.lower()), name)
        labeled.append((label, votes))
    sullivans = sorted([r for r in labeled if r[0] == COMBINE], key=lambda r: -r[1])
    others = [r for r in labeled if r[0] != COMBINE]
    renamed = []
    for i, (_, votes) in enumerate(sullivans):
        renamed.append((COMBINE if i == 0 else OTHER_SULLIVAN if i == 1 else f"{COMBINE} #{i + 1}", votes))
    merged = {}
    for label, votes in renamed + others:
        merged[label] = merged.get(label, 0) + votes
    out = sorted(((n, v) for n, v in merged.items() if v > 0), key=lambda r: -r[1])
    return {"candidates": out}


def get_results(prefer=None):
    """Try each source; return (results, source_name). Raises if all fail."""
    order = list(JSON_SOURCES) + ["Wikipedia"]
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    errors = []
    for name in order:
        raw = None
        try:
            if name == "Wikipedia":
                raw, _ = fetch_wikitext()
                return parse_results(raw), name
            raw = _get(JSON_SOURCES[name])
            return parse_json_results(json.loads(raw)), name
        except Exception as e:
            errors.append(f"{name}: {str(e)[:120]}")
            _debug_sample(name, raw)
    raise RuntimeError(" | ".join(errors))


_DEBUG_SHOWN = set()


def _debug_sample(name, raw):
    """Once per run, print a short piece of a source's raw data so it can be fixed."""
    if not raw or name in _DEBUG_SHOWN:
        return
    _DEBUG_SHOWN.add(name)
    print(f"----- {name} sample (for troubleshooting) -----")
    if name == "Wikipedia":
        i = raw.find(FOCUS, raw.lower().find("general election"))
        i = raw.rfind("{{Election box begin", 0, i) if i > 0 else -1
        print(raw[max(i, 0):max(i, 0) + 1500])
    else:
        try:
            top = json.loads(raw)
            if isinstance(top, dict):
                print("top-level keys:", list(top)[:20])
        except Exception:
            pass
        start = raw.find('"office":"U.S. Senate"')
        if start < 0:
            start = 0
        shown = 0
        for key in ('"reporting_units"', '"candidates"', '"candidate_metadata"', '"votes"'):
            i = raw.find(key, start)
            if i >= 0 and shown < 2:
                print(f"[{key}]", raw[i:i + 1200])
                shown += 1
        if not shown:
            print("start of Senate race:", raw[start:start + 2500])
    print("----- end sample -----")


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
    # Keep numbers written as {{formatnum:123}}, {{nts|123}}, {{Nowrap|123}} etc.
    body = re.sub(r"\{\{\s*formatnum\s*:\s*([\d,.\s]+)\}\}", r"\1", body, flags=re.I)
    body = re.sub(r"\{\{\s*(?:nts|ntsh|nowrap|small)\s*\|\s*([\d,.\s]+)\}\}", r"\1", body, flags=re.I)
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
    found_empty = False
    best = None
    for box in boxes:
        title = box[:box.find("}}")].lower()  # the box's opening line(s), incl. its title
        if "primary" in title:   # skip the August primary table
            continue
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
                found_empty = True
                continue
            total = sum(v for _, v in counted)
            # Most votes = first-choice round (later ranked-choice rounds have fewer)
            if best is None or total > best[0]:
                best = (total, counted)
    if best:
        return normalize(best[1])
    if found_empty:
        raise ValueError("Found the results table, but it has no vote counts yet.")
    raise ValueError("Couldn't find a general-election results table containing "
                     + " and ".join(MUST_INCLUDE) + ".")


# ------------------------------------------------------------ notification ---

def stats(candidates):
    """Numbers the alert is built from."""
    cands = sorted(candidates, key=lambda c: -c[1])
    total = sum(v for _, v in cands)
    leader, runner = cands[0], cands[1]
    share = leader[1] / total
    focus = dict(cands).get(FOCUS, 0)
    combined = sum(v for n, v in cands if n == COMBINE or n == OTHER_SULLIVAN
                   or n.startswith(COMBINE + " #"))
    return {
        "cands": cands, "total": total, "leader": leader[0], "runner": runner[0],
        "margin": leader[1] - runner[1], "share": share,
        "to50": share - 0.5,                         # + above 50%, - below
        "vs_rest": leader[1] - (total - leader[1]),  # leader minus everyone else combined
        "vs_combined": focus - combined,             # Peltola minus both Sullivans
    }


def build_message(current, previous):
    s = stats(current["candidates"])
    p = stats(previous["candidates"]) if previous else None
    leader = s["leader"]

    # 1. Lead
    lead = f"{leader} leads {s['runner']} by {s['margin']:,}"
    if p:
        lead += f" (was {p['leader']} +{p['margin']:,})"

    # 2. Distance from 50%
    if s["to50"] > 0:
        fifty = (f"{leader} is ABOVE 50% by {s['to50'] * 100:.2f} pts "
                 f"(majority of {s['vs_rest']:,} over all others)")
    else:
        fifty = (f"{leader} is {abs(s['to50']) * 100:.2f} pts short of 50% "
                 f"(all others combined lead by {-s['vs_rest']:,})")
    if p:
        fifty += f" [was {p['to50'] * 100:+.2f}]"

    # 3. Peltola vs. both Sullivans combined
    vc = s["vs_combined"]
    if vc > 0:
        both = f"{FOCUS} LEADS both Sullivans combined by {vc:,}"
    elif vc < 0:
        both = f"{FOCUS} TRAILS both Sullivans combined by {-vc:,}"
    else:
        both = f"{FOCUS} is TIED with both Sullivans combined"
    if p:
        both += f" (was {p['vs_combined']:+,})"

    lines = [lead, fifty, both, ""]
    lines += [f"{n}: {v:,} ({v / s['total']:.2%})" for n, v in s["cands"]]

    if p:
        prev = dict(previous["candidates"])
        changes = [f"{n} {v - prev[n]:+,}" for n, v in s["cands"] if n in prev and v != prev[n]]
        if changes:
            lines.append("New votes: " + ", ".join(changes))

    title = (f"{RACE}: {leader} +{s['margin']:,} · {s['share']:.2%}"
             if s["margin"] else f"{RACE}: TIED")
    return title, "\n".join(lines)


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
            notify(f"{RACE} watcher: can't read results",
                   f"{FAILS_BEFORE_ALERT} checks in a row failed. Last error: {e}",
                   priority="default", tags="warning")
            state["fail_alerted"] = True
            save_state(state)
        return state

    if state.get("fail_alerted"):
        notify(f"{RACE} watcher: working again", "Results are readable again.",
               priority="low", tags="white_check_mark")
    state["fail_count"] = 0
    state["fail_alerted"] = False

    previous = state.get("last")
    first_ever = previous is None
    if first_ever or current["candidates"] != [tuple(c) for c in previous["candidates"]]:
        title, body = build_message(current, None if first_ever else previous)
        if first_ever:
            title = f"{RACE} watcher is on"
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
    if state.get("race") != RACE:
        state = {"race": RACE}   # ignore numbers saved for a different race
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
