"""Pull open roles from every known sponsor job board and write Shortlist.md.

Keeps UK / UK-remote software-and-adjacent roles below senior level, flags sponsorship
wording and salaries under the new-entrant going rate, and drops anything already
tracked in the vault.

Usage:
    python daily.py                 # writes <vault>/Shortlist.md
    python daily.py --vault D:/notes/job-apps
    python daily.py --days 30       # only roles posted in the last 30 days

Reads data/boards.jsonl (from find_boards.py) and boards_manual.txt.
"""
import argparse
import csv
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from build_sponsors import norm
from find_boards import UA, get

HERE = Path(__file__).parent
# The Obsidian job-apps vault: read to skip tracked roles, and Shortlist.md goes there.
VAULT = Path(os.environ.get("JOB_APPS_VAULT", HERE.parent / "job-apps"))
BOARDS = HERE / "data" / "boards.jsonl"
MANUAL = HERE / "boards_manual.txt"
SEEN = HERE / "data" / "seen.json"
OUT_MD = VAULT / "Shortlist.md"
OUT_CSV = HERE / "data" / "shortlist.csv"
OUT_JS = HERE / "web" / "jobs.js"

# Lower ("new entrant") going rate for SOC 2134, gov.uk table updated 22 July 2025.
# 2139 (DevOps, IT consultants) is £38,700 and 2133 (data engineers, architects) £42,400.
MIN_SALARY = 40_000
# Postings that ask for at least this many years of experience get flagged and sorted down.
MAX_YEARS = 5
# Cap on postings pulled per Workday board (after the UK filter), 20 per request.
WORKDAY_MAX = 1000

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
    r"\bl[4-9]\b|level [4-9]|scientist|researcher|analyst|service desk|"
    r"\bvice president\b|\bavp\b|\bsvp\b|\bmd\b|managing director|"
    r"\b(german|french|spanish|italian|dutch|japanese|korean|mandarin)\b|"
    r"electronic|embedded|firmware|\bfpga\b|\basic\b|silicon|high voltage|highways|"
    r"\b3d\b|fellow|expression of interest|people partner|talent|weekend|contractor|"
    r"people operations|banker|go-to-market|\bgtm\b|marketing|legal|counsel", re.I)
# Titles that look like the roles applied to so far get a ★ and sort first.
STRONG = re.compile(
    r"software|back[- ]?end|full[- ]?stack|python|\bai\b|applied ai|\bllm\b|"
    r"forward[- ]deployed|data engineer|developer|product engineer|devops|platform engineer|"
    r"solutions? (engineer|architect|consultant)", re.I)
# Big 4 / consultancy grades that sit around 2-5 years' experience despite the wording
# (PwC Senior Associate, Deloitte and EY Senior Consultant, KPMG Assistant Manager).
# They're ignored by the seniority filter and noted in Flags.
GRADE = re.compile(r"senior associate|senior consultant|assistant manager", re.I)
JUNIOR = re.compile(
    r"graduate|\bgrad\b|junior|\bjr\.?\b|entry|early career|new grad|associate|\bi\b|"
    r"\bl[12]\b|level [12]|trainee", re.I)

UK = re.compile(
    r"united kingdom|\buk\b|\bu\.k\.|england|scotland|wales|northern ireland|great britain|"
    r"\bgb\b|london|manchester|edinburgh|glasgow|bristol|cambridge|oxford|leeds|birmingham|"
    r"belfast|cardiff|reading|newcastle|sheffield|nottingham|liverpool|brighton|bath|"
    r"guildford|milton keynes|southampton|\byork\b|swansea|aberdeen|dundee|exeter|"
    r"bournemouth|coventry|leicester|norwich|cheltenham|basingstoke|slough|watford|"
    r"bradford|east midlands|castle donington|derby|plymouth|ipswich|luton|st albans|"
    r"chelmsford|gloucester|bracknell|stirling|inverness|telford|hull\b", re.I)
REMOTE_EU = re.compile(r"remote.*(emea|europe)|(emea|europe).*remote", re.I)
# Places that would otherwise trip UK (New York -> "york", Cambridge MA, Georgia "GB"...).
NOT_UK = re.compile(
    r"new york|new england|new south wales|cambridge,? (ma|massachusetts)|"
    r"birmingham,? (al|alabama)|\bal-birmingham|reading,? (pa|pennsylvania)|bath,? (me|maine)|"
    r"(london|cambridge|richmond),? (on|ontario|ky|kentucky|va|virginia)", re.I)
# Banks often leave the corporate title out of the job title and open the description with it.
SENIOR_DESC = re.compile(r"\b(SVP|AVP|VP)\b|vice president", re.I)
YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?years?\b(?:'|’)?[^.;]{0,60}?"
    r"experience", re.I)

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
    """HTML -> plain text, keeping paragraph breaks and bullets for the web view."""
    s = html.unescape(s or "")
    s = re.sub(r"(?i)<li[^>]*>", "\n• ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|ul|ol|h[1-6]|tr|table)>", "\n\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^\S\n]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


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
        elif p == "workday":
            jobs = fetch_workday(slug)
        elif p == "avature":
            jobs = fetch_avature(slug)
        elif p == "successfactors":
            jobs = fetch_successfactors(slug)
        elif p == "kpmg-uk":
            jobs = fetch_kpmg_uk(slug)
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


def post_json(url, body, retries=3, timeout=20):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(),
                                         {**UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return {}
            time.sleep(5 * (attempt + 1))
        except (TimeoutError, OSError, ValueError):
            return {}
    return {}


def workday_posted(s):
    """'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> date."""
    s = (s or "").lower()
    if "today" in s:
        return date.today()
    if "yesterday" in s:
        return date.today() - timedelta(days=1)
    m = re.search(r"(\d+)", s)
    return date.today() - timedelta(days=int(m.group(1))) if m else None


def fetch_workday(slug):
    """slug is '<tenant>.wd<N>/<site>', e.g. 'accenture.wd103/AccentureCareers'.

    Workday boards are huge and global, so filter to the UK with the country facet when
    the tenant has one, keep only titles that pass the role filter, and fetch the full
    posting (description, all locations) for just those.
    """
    host, site = slug.split("/")
    tenant = host.split(".")[0]
    base = f"https://{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"

    first = post_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0,
                                      "searchText": ""})
    facets = {}

    def find_uk(fs):
        for f in fs:
            for v in f.get("values") or []:
                if "values" in v:
                    find_uk([v])
                elif v.get("descriptor") == "United Kingdom":
                    facets[f["facetParameter"]] = [v["id"]]
    find_uk(first.get("facets") or [])

    # No country facet (e.g. PwC UK's site, shared with India): pick the UK offices
    # from the locations facet instead, so the cap isn't spent on other countries.
    def uk_offices(fs):
        for f in fs:
            vals = f.get("values") or []
            if f.get("facetParameter") == "locations":
                ids = [v["id"] for v in vals if "id" in v and is_uk(v.get("descriptor", ""))]
                if ids:
                    facets["locations"] = ids
            uk_offices([v for v in vals if "values" in v])
    if not facets:
        uk_offices(first.get("facets") or [])

    postings, offset, total = [], 0, None
    while total is None or offset < min(total, WORKDAY_MAX):
        page = post_json(f"{api}/jobs", {"appliedFacets": facets, "limit": 20,
                                         "offset": offset, "searchText": ""})
        batch = page.get("jobPostings") or []
        if not batch:
            break
        if total is None:
            total = page.get("total", 0)
        postings += batch
        offset += 20

    cands = [p for p in postings if title_ok(p.get("title", ""))]

    def detail(p):
        info = (json.loads(get(api + p["externalPath"])[1] or b"{}")
                .get("jobPostingInfo") or {})
        locs = [info.get("location") or p.get("locationsText", "")]
        locs += info.get("additionalLocations") or []
        country = (info.get("country") or {}).get("descriptor", "")
        return {"id": info.get("jobReqId") or p["externalPath"].rsplit("_", 1)[-1],
                "title": p["title"],
                "location": " / ".join(filter(None, locs + [country])),
                "url": base + "/" + site + p["externalPath"],
                "posted": ts(info.get("startDate")) or workday_posted(p.get("postedOn")),
                "desc": text(info.get("jobDescription")), "salary": ""}

    with ThreadPoolExecutor(4) as pool:
        return list(pool.map(detail, cands))


# Career sites with no public API (the Big 4): scrape the listing pages, keep titles that
# pass the role filter, then fetch the description from each remaining job page.
SCRAPE_MAX_PAGES = 80


def page(url):
    return get(url, timeout=30)[1].decode("utf-8", "replace")


def between(s, start, end, cap=40_000):
    i = s.find(start)
    if i < 0:
        return ""
    j = s.find(end, i)
    return s[i:j if j > 0 else i + cap][:cap]


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def with_details(jobs, desc_of):
    """desc_of(page_html, job) fills in desc (and posted, where the page has it)."""
    jobs = [j for j in jobs if title_ok(j["title"])]

    def one(j):
        desc_of(page(j["url"]), j)
        return j
    with ThreadPoolExecutor(4) as pool:
        return list(pool.map(one, jobs))


def fetch_avature(slug):
    """slug is '<host>/<portal>', e.g. 'apply.deloitte.co.uk/UKCareers'."""
    base, jobs, seen = f"https://{slug}", [], set()
    for n in range(SCRAPE_MAX_PAGES):
        s = page(f"{base}/SearchJobs/?jobRecordsPerPage=20&jobOffset={n * 20}")
        arts = re.findall(r'<article class="article article--result">(.*?)</article>', s, re.S)
        if not arts:
            break
        for a in arts:
            m = re.search(r'href="([^"]*/JobDetail/[^"]*?/(\d+))"[^>]*>(.*?)</a>', a, re.S)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            sub = [strip_tags(x) for x in re.findall(r"<span>(.*?)</span>", a, re.S)]
            posted = next((x[7:].strip() for x in sub if x.startswith("Posted:")), "")
            try:
                posted = datetime.strptime(posted, "%d %b %Y").date()
            except ValueError:
                posted = None
            jobs.append({"id": m.group(2), "title": strip_tags(m.group(3)),
                         "location": (sub[0] if sub else "") + " / United Kingdom",
                         "url": html.unescape(m.group(1)), "posted": posted, "salary": ""})

    def desc(s, j):
        parts = s.split("js_collapsible__content")
        j["desc"] = text(parts[-1].split("Share this job")[0]) if len(parts) > 2 else ""
    return with_details(jobs, desc)


def fetch_successfactors(slug):
    """slug is '<host>/<site>[?<search query>]', e.g.
    'careers.ey.com/ey?optionsFacetsDD_country=GB'."""
    path, _, query = slug.partition("?")
    host = path.split("/")[0]
    jobs, seen = [], set()
    for n in range(SCRAPE_MAX_PAGES):
        s = page(f"https://{path}/search/?q=&{query}&startrow={n * 25}")
        rows = re.findall(r'<tr class="data-row">(.*?)</tr>', s, re.S)
        if not rows:
            break
        for r in rows:
            m = re.search(r'<a href="([^"]*/job/[^"]*?/(\d+)/?)" class="jobTitle-link">'
                          r'(.*?)</a>', r, re.S)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            loc = re.search(r'class="jobLocation">(.*?)</span>', r, re.S)
            jobs.append({"id": m.group(2), "title": strip_tags(m.group(3)),
                         "location": strip_tags(loc.group(1)) if loc else "",
                         "url": f"https://{host}" + html.unescape(m.group(1)),
                         "posted": None, "salary": ""})

    def desc(s, j):
        j["desc"] = text(between(s, 'class="jobdescription"', 'class="jobmarkets"'))
        m = re.search(r'itemprop="datePosted" content="([^"]+)"', s)
        if m:
            try:
                j["posted"] = datetime.strptime(m.group(1), "%a %b %d %H:%M:%S %Z %Y").date()
            except ValueError:
                pass
    return with_details(jobs, desc)


def fetch_kpmg_uk(slug):
    """KPMG UK's own site (slug 'www.kpmgcareers.co.uk'). No posting dates: the date in
    its job pages is just the time of the request."""
    jobs, seen = [], set()
    for n in range(1, SCRAPE_MAX_PAGES + 1):
        s = page(f"https://{slug}/search/vacancies/?page={n}")
        cards = s.split('class="row vacancy-result')[1:]
        if not cards:
            break
        for v in cards:
            m = re.search(r'data-vacancy-id="(\d+)"', v)
            t = re.search(r"<h3>(.*?)</h3>", v, re.S)
            u = re.search(r'href="(/Vacancies/[^"]+)"', v)
            if not (m and t and u) or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            loc = re.search(r'vacancy-location">.*?<b>(.*?)</b>', v, re.S)
            jobs.append({"id": m.group(1), "title": strip_tags(t.group(1)),
                         "location": (strip_tags(loc.group(1)) if loc else "") +
                                     " / United Kingdom",
                         "url": f"https://{slug}{u.group(1)}", "posted": None, "salary": ""})

    def desc(s, j):
        j["desc"] = text(between(s, 'row job-description', 'idse-panel'))
    return with_details(jobs, desc)


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
                name, provider, slug, *note = (x.strip() for x in line.split("|"))
                boards[(provider, slug)] = {"name": name, "provider": provider,
                                            "slug": slug, "check": "".join(note)}
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


def title_ok(title):
    return bool(ROLE.search(title)) and not NOT_ROLE.search(GRADE.sub(" ", title))


def is_uk(loc):
    """True if any of the listed locations is in the UK."""
    return any(UK.search(NOT_UK.sub(" ", part))
               for part in re.split(r"\s*(?:/|;|\|)\s*", loc))


def classify(j):
    """Return None to drop, else add level/flags to the job."""
    title = j["title"]
    if not title_ok(title):
        return None
    loc = j["location"]
    if is_uk(loc):
        j["where"] = loc if len(loc) <= 80 else loc[:77] + "..."
    elif REMOTE_EU.search(loc):
        j["where"] = loc + " ⚠ check UK-based"
    else:
        return None
    j["level"] = "junior" if JUNIOR.search(GRADE.sub(" ", title)) else "mid"

    flags = []
    grade = GRADE.search(title)
    if grade:
        flags.append(f"grade: {grade.group(0).lower()} (~2-5 yrs)")
    rank = SENIOR_DESC.search(j["desc"][:120])
    if rank:
        flags.append(f"⚠ {rank.group(0)} per description")
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
    years = [int(y) for y in YEARS.findall(j["desc"]) if int(y) <= 15]
    if years and max(years) >= MAX_YEARS:
        flags.append(f"⚠ asks {max(years)}+ yrs")
    if j["check"]:
        flags.append("🔍 " + j["check"])
    j["flags"] = "; ".join(flags)
    return j


def main():
    global VAULT, OUT_MD
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="drop roles posted longer ago")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--vault", type=Path, default=VAULT,
                    help="job-apps vault (default $JOB_APPS_VAULT or ../job-apps)")
    ap.add_argument("--show-no-sponsor", action="store_true",
                    help="keep roles whose description says no sponsorship")
    args = ap.parse_args()
    VAULT, OUT_MD = args.vault, args.vault / "Shortlist.md"
    if not VAULT.is_dir():
        ap.error(f"vault not found: {VAULT}")

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
        if "❌" in j["flags"] and not args.show_no_sponsor:
            continue
        j["fit"] = "★" if STRONG.search(j["title"]) else ""
        dupes.add(u)
        j["first_seen"] = seen.setdefault(u, today)
        keep.append(j)

    # Forget jobs that have closed so seen.json doesn't grow forever.
    seen = {u: d for u, d in seen.items() if u in dupes or u in urls}
    SEEN.write_text(json.dumps(seen, indent=0), encoding="utf-8")

    def sort_key(j):
        return (j["first_seen"] != today, "❌" in j["flags"], not j["fit"],
                "yrs" in j["flags"] or "per description" in j["flags"],
                j["level"] != "junior",
                -(j["posted"] or date.min).toordinal(), j["company"].lower())
    keep.sort(key=sort_key)

    cols = ["first_seen", "fit", "company", "title", "level", "where", "salary", "posted",
            "flags", "url", "provider"]
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(keep)

    write_js(keep, cols, today, len(boards), len(all_jobs))
    write_md(keep, today, len(boards), len(all_jobs))
    new = sum(j["first_seen"] == today for j in keep)
    print(f"{len(boards)} boards, {len(all_jobs)} jobs fetched, {len(keep)} match "
          f"({new} new today) -> {OUT_MD}")


def write_js(jobs, cols, today, n_boards, n_jobs):
    """Data for web/index.html. A script, not JSON, so the page works from file://."""
    rows = [{**{c: str(j.get(c) or "") for c in cols},
             "desc": re.sub(r"\n\s*\n\s*", "\n\n", j["desc"]).strip()} for j in jobs]
    data = {"generated": today, "boards": n_boards, "scanned": n_jobs, "jobs": rows}
    OUT_JS.write_text("window.SHORTLIST = " + json.dumps(data, ensure_ascii=False) + ";\n",
                      encoding="utf-8")


def cell(s):
    return str(s or "").replace("|", "/").replace("\n", " ")


def write_md(jobs, today, n_boards, n_jobs):
    new = [j for j in jobs if j["first_seen"] == today]
    old = [j for j in jobs if j["first_seen"] != today]
    lines = [
        "# Sponsored role shortlist", "",
        f"Generated {today} from {n_boards} sponsor job boards ({n_jobs} open roles scanned). "
        f"Licensed sponsors only, UK or UK-remote, below senior level, not already tracked. "
        f"Roles that say no sponsorship are hidden. ★ = title like the roles applied to so far "
        f"(sorted first). **junior** = graduate/junior/associate/level I, **mid** = no level "
        f"in the title. Run `/apply <link>` on anything worth it.", "",
    ]

    def table(rows):
        out = ["| ★ | Company | Role | Level | Location | Salary | Posted | Flags |",
               "|---|---|---|---|---|---|---|---|"]
        for j in rows:
            out.append(f"| {j['fit']} | {cell(j['company'])} | [{cell(j['title'])}]({j['url']}) | "
                       f"{j['level']} | {cell(j['where'])} | {cell(j['salary'])} | "
                       f"{j['posted'] or ''} | {cell(j['flags'])} |")
        return out

    lines += [f"## New today ({len(new)})", ""] + (table(new) if new else ["Nothing new."])
    lines += ["", f"## Still open ({len(old)})", ""] + (table(old) if old else ["—"])
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
