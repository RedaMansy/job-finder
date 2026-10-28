"""Pull open roles from every known sponsor job board and write Shortlist.md.

Keeps UK / UK-remote software-and-adjacent roles below senior level, flags sponsorship
wording and salaries under the new-entrant going rate, and drops anything already
tracked in the vault.

Usage:
    python daily.py                 # writes ../Shortlist.md
    python daily.py --days 30       # only roles posted in the last 30 days

Reads data/boards.jsonl (from find_boards.py) and boards_manual.txt.
"""
import argparse
import csv
import html
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from build_sponsors import norm
from find_boards import get

HERE = Path(__file__).parent
VAULT = HERE.parent
BOARDS = HERE / "data" / "boards.jsonl"
MANUAL = HERE / "boards_manual.txt"
SEEN = HERE / "data" / "seen.json"
OUT_MD = VAULT / "Shortlist.md"
OUT_CSV = HERE / "data" / "shortlist.csv"

# Lower ("new entrant") going rate for SOC 2134, gov.uk table updated 22 July 2025.
# 2139 (DevOps, IT consultants) is £38,700 and 2133 (data engineers, architects) £42,400.
MIN_SALARY = 40_000

ROLE = re.compile(
    r"software|developer|engineer|engineering|\bsre\b|devops|\bdata\b|machine learning|"
    r"\bml\b|\bai\b|back[- ]?end|front[- ]?end|full[- ]?stack|platform|cloud|infrastructure|"
    r"solutions? (architect|consultant)|forward[- ]deployed|technical consultant|"
    r"\bqa\b|test automation|\bsdet\b", re.I)
NOT_ROLE = re.compile(
    r"\bsenior\b|\bsr\.?\b|\bstaff\b|principal|\blead\b|\bhead\b|director|manager|\bvp\b|"
    r"chief|distinguished|(?<!solutions )architect|\bintern(ship)?\b|placement|apprentice|"
    r"mechanical|electrical|civil|hardware|\brf\b|manufacturing|field service|chemical|"
    r"structural|process engineer|sales|account executive|recruit|\biii\b|\biv\b|"
    r"\bl[4-9]\b|level [4-9]", re.I)
JUNIOR = re.compile(
    r"graduate|\bgrad\b|junior|\bjr\.?\b|entry|early career|new grad|associate|\bi\b|"
    r"\bl[12]\b|level [12]|trainee", re.I)

UK = re.compile(
    r"united kingdom|\buk\b|\bu\.k\.|england|scotland|wales|northern ireland|great britain|"
    r"\bgb\b|london|manchester|edinburgh|glasgow|bristol|cambridge|oxford|leeds|birmingham|"
    r"belfast|cardiff|reading|newcastle|sheffield|nottingham|liverpool|brighton|bath|"
    r"guildford|milton keynes|southampton|\byork\b|swansea|aberdeen|dundee|exeter|"
    r"bournemouth|coventry|leicester|norwich|cheltenham|basingstoke|slough", re.I)
REMOTE_EU = re.compile(r"remote.*(emea|europe)|(emea|europe).*remote", re.I)

NO_SPONSOR = re.compile(
    r"(not|unable to|cannot|can't|won't|will not|do not|don't|are not able to)\s+"
    r"(be able to\s+)?(offer|provide|sponsor|support)[^.]{0,40}(sponsor|visa)|"
    r"without (the need for )?(any )?(current or future )?(visa )?sponsorship|"
    r"no (visa )?sponsorship|sponsorship (is )?not (available|offered|provided)", re.I)
YES_SPONSOR = re.compile(
    r"(visa )?sponsorship (is |may be |can be )?(available|offered|provided|possible)|"
    r"we (can|will|do|are able to) sponsor|able to (offer|provide) (visa )?sponsorship", re.I)
MONEY = re.compile(r"£\s?(\d{2,3}(?:,\d{3})|\d{2,3}(?:\.\d)?\s?[kK])\b")


def text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def money(s):
    vals = []
    for m in MONEY.findall(s or ""):
        m = m.replace(",", "").replace(" ", "")
        v = float(m[:-1]) * 1000 if m[-1] in "kK" else float(m)
        if 15_000 <= v <= 400_000:
            vals.append(int(v))
    return vals


def ts(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, timezone.utc).date()
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def fetch(board):
    """Return a list of normalised job dicts for one board."""
    p, slug = board["provider"], board["slug"]
    jobs = []
    try:
        if p == "greenhouse":
            _, b = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
            for j in json.loads(b or b"{}").get("jobs", []):
                jobs.append({"id": str(j["id"]), "title": j["title"],
                             "location": (j.get("location") or {}).get("name", ""),
                             "url": j["absolute_url"],
                             "posted": ts(j.get("first_published") or j.get("updated_at")),
                             "desc": text(j.get("content")), "salary": ""})
        elif p == "lever":
            for host in ("api.lever.co", "api.eu.lever.co"):
                _, b = get(f"https://{host}/v0/postings/{slug}?mode=json")
                data = json.loads(b or b"[]")
                if data:
                    break
            for j in data:
                cat = j.get("categories") or {}
                locs = cat.get("allLocations") or [cat.get("location", "")]
                sr = j.get("salaryRange") or {}
                sal = f"{sr.get('currency', '')} {sr.get('min')}-{sr.get('max')}" if sr else ""
                jobs.append({"id": j["id"], "title": j["text"],
                             "location": " / ".join(filter(None, locs)) +
                                         (" (remote)" if j.get("workplaceType") == "remote" else ""),
                             "url": j["hostedUrl"], "posted": ts(j.get("createdAt")),
                             "desc": (j.get("descriptionPlain") or "") + " " +
                                     (j.get("additionalPlain") or ""),
                             "salary": sal,
                             "salary_vals": [sr["max"]] if sr.get("currency") == "GBP"
                                            and sr.get("max") else []})
        elif p == "ashby":
            _, b = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                       "?includeCompensation=true")
            for j in json.loads(b or b"{}").get("jobs", []):
                country = (((j.get("address") or {}).get("postalAddress") or {})
                           .get("addressCountry") or "")
                locs = [j.get("location", "")] + [s.get("location", "") for s in
                                                   j.get("secondaryLocations") or []]
                comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
                jobs.append({"id": j["id"], "title": j["title"],
                             "location": " / ".join(filter(None, locs + [country])) +
                                         (" (remote)" if j.get("isRemote") else ""),
                             "url": j["jobUrl"], "posted": ts(j.get("publishedAt")),
                             "desc": j.get("descriptionPlain") or "", "salary": comp})
        elif p == "workable":
            _, b = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
            for j in json.loads(b or b"{}").get("jobs", []):
                jobs.append({"id": j["shortcode"], "title": j["title"],
                             "location": ", ".join(filter(None, [j.get("city"), j.get("country")]))
                                         + (" (remote)" if j.get("telecommuting") else ""),
                             "url": j["url"], "posted": ts(j.get("published_on")),
                             "desc": "", "salary": ""})
        elif p == "smartrecruiters":
            _, b = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                       "?country=gb&limit=100")
            for j in json.loads(b or b"{}").get("content", []):
                loc = j.get("location") or {}
                jobs.append({"id": j["id"], "title": j["name"],
                             "location": ", ".join(filter(None, [loc.get("city"),
                                                                 loc.get("country")])),
                             "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                             "posted": ts(j.get("releasedDate")), "desc": "", "salary": ""})
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    for j in jobs:
        j.update(company=board["name"], provider=p, slug=slug, check=board.get("check", ""))
    return jobs


def load_boards():
    boards = {}
    if BOARDS.exists():
        rows = [json.loads(l) for l in BOARDS.read_text(encoding="utf-8").splitlines() if l]
        claims = defaultdict(set)
        for r in rows:
            claims[(r["provider"], r["slug"])].add(r["key"])
        for r in rows:
            k = (r["provider"], r["slug"])
            names = claims[k]
            if k in boards:
                continue
            r["check"] = ""
            if len(names) > 1:
                r["name"] = r.get("board_name") or r["name"]
                r["check"] = "board matches several sponsors: " + "; ".join(sorted(names))
            elif not r["confident"]:
                r["check"] = "board match unverified"
            boards[k] = r
    if MANUAL.exists():
        for line in MANUAL.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                name, provider, slug = (x.strip() for x in line.split("|"))
                boards[(provider, slug)] = {"name": name, "provider": provider,
                                            "slug": slug, "check": ""}
    return list(boards.values())


def company_word(name):
    """'Faculty Science Limited' and 'Faculty' should count as the same company."""
    return (norm(name).split() or [""])[0]


def tracked():
    """(urls, job ids, normalised company+role) already in the vault."""
    urls, ids, pairs = set(), set(), set()
    for note in VAULT.glob("*.md"):
        head = note.read_text(encoding="utf-8").split("---")[1:2]
        if not head:
            continue
        fm = dict(re.findall(r"^(\w+):\s*\"?(.*?)\"?\s*$", head[0], re.M))
        if fm.get("type") != "job-application":
            continue
        if fm.get("jobUrl"):
            urls.add(fm["jobUrl"].split("?")[0].rstrip("/"))
            ids.add(fm["jobUrl"].split("?")[0].rstrip("/").split("/")[-1])
        pairs.add((company_word(fm.get("company", "")), fm.get("role", "").lower().strip()))
    return urls, ids, pairs


def classify(j):
    """Return None to drop, else add level/flags to the job."""
    title = j["title"]
    if not ROLE.search(title) or NOT_ROLE.search(title):
        return None
    loc = j["location"]
    if UK.search(loc):
        j["where"] = loc
    elif REMOTE_EU.search(loc):
        j["where"] = loc + " ⚠ check UK-based"
    else:
        return None
    j["level"] = "target" if JUNIOR.search(title) else "reach"

    flags = []
    if NO_SPONSOR.search(j["desc"]):
        flags.append("❌ says no sponsorship")
    elif YES_SPONSOR.search(j["desc"]):
        flags.append("✅ mentions sponsorship")
    vals = j.get("salary_vals") or money(j["salary"]) or money(j["desc"])
    if vals:
        top = max(vals)
        if not j["salary"]:
            j["salary"] = f"£{min(vals):,}–£{top:,}" if len(vals) > 1 else f"£{top:,}"
        if top < MIN_SALARY:
            flags.append(f"⚠ max salary £{top:,} < £{MIN_SALARY:,}")
    if j["check"]:
        flags.append("🔍 " + j["check"])
    j["flags"] = "; ".join(flags)
    return j


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="drop roles posted longer ago")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    boards = load_boards()
    with ThreadPoolExecutor(args.workers) as pool:
        all_jobs = [j for js in pool.map(fetch, boards) for j in js]

    urls, ids, pairs = tracked()
    cutoff = date.today() - timedelta(days=args.days) if args.days else None
    seen = json.loads(SEEN.read_text(encoding="utf-8")) if SEEN.exists() else {}
    today = date.today().isoformat()

    keep, dupes = [], set()
    for j in all_jobs:
        u = j["url"].split("?")[0].rstrip("/")
        if u in dupes or u in urls or j["id"] in ids or \
                (company_word(j["company"]), j["title"].lower().strip()) in pairs:
            continue
        if cutoff and j["posted"] and j["posted"] < cutoff:
            continue
        if not classify(j):
            continue
        dupes.add(u)
        j["first_seen"] = seen.setdefault(u, today)
        keep.append(j)

    # Forget jobs that have closed so seen.json doesn't grow forever.
    seen = {u: d for u, d in seen.items() if u in dupes or u in urls}
    SEEN.write_text(json.dumps(seen, indent=0), encoding="utf-8")

    def sort_key(j):
        return (j["first_seen"] != today, "❌" in j["flags"], j["level"] != "target",
                -(j["posted"] or date.min).toordinal(), j["company"].lower())
    keep.sort(key=sort_key)

    cols = ["first_seen", "company", "title", "level", "where", "salary", "posted",
            "flags", "url", "provider"]
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(keep)

    write_md(keep, today, len(boards), len(all_jobs))
    new = sum(j["first_seen"] == today for j in keep)
    print(f"{len(boards)} boards, {len(all_jobs)} jobs fetched, {len(keep)} match "
          f"({new} new today) -> {OUT_MD}")


def cell(s):
    return str(s or "").replace("|", "/").replace("\n", " ")


def write_md(jobs, today, n_boards, n_jobs):
    new = [j for j in jobs if j["first_seen"] == today]
    old = [j for j in jobs if j["first_seen"] != today]
    lines = [
        "# Sponsored role shortlist", "",
        f"Generated {today} from {n_boards} sponsor job boards ({n_jobs} open roles scanned). "
        f"Licensed sponsors only, UK or UK-remote, below senior level, not already tracked. "
        f"**target** = graduate/junior/associate, **reach** = mid-level. "
        f"Run `/apply <link>` on anything worth it.", "",
    ]

    def table(rows):
        out = ["| Company | Role | Level | Location | Salary | Posted | Flags |",
               "|---|---|---|---|---|---|---|"]
        for j in rows:
            out.append(f"| {cell(j['company'])} | [{cell(j['title'])}]({j['url']}) | "
                       f"{j['level']} | {cell(j['where'])} | {cell(j['salary'])} | "
                       f"{j['posted'] or ''} | {cell(j['flags'])} |")
        return out

    lines += [f"## New today ({len(new)})", ""] + (table(new) if new else ["Nothing new."])
    lines += ["", f"## Still open ({len(old)})", ""] + (table(old) if old else ["—"])
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
