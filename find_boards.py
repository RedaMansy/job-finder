"""Find which tech sponsors have a public job board on a known ATS.

Guesses slugs from each company name and probes Greenhouse, Ashby and Lever (and
Workable, which rate-limits hard, so it gets its own slow pass). Resumable: each
(provider, company) pair is probed once, so re-running after an interruption or after
rebuilding sponsors_tech.csv only probes what's new.

Usage:
    python find_boards.py                        # greenhouse, ashby, lever
    python find_boards.py --limit 200
    python find_boards.py --providers workable --workers 1 --tiers always,core

Reads data/sponsors_tech.csv, appends to data/boards.jsonl and data/probed.txt.
"""
import argparse
import csv
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from difflib import SequenceMatcher

from build_sponsors import norm

HERE = Path(__file__).parent
SPONSORS = HERE / "data" / "sponsors_tech.csv"
BOARDS = HERE / "data" / "boards.jsonl"
PROBED = HERE / "data" / "probed.txt"

GENERIC = {"technologies", "technology", "solutions", "systems", "services", "software",
           "labs", "bank", "international", "europe", "consulting", "global", "and"}

# provider -> URL that 200s only if the board exists
PROBES = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
}

UA = {"User-Agent": "job-search-script (personal use)"}
lock = threading.Lock()


def slugs(key):
    words = key.split()
    core = [w for w in words if w not in GENERIC] or words
    out = ["".join(words), "-".join(words), "".join(core), "-".join(core)]
    if len(core) > 1 and len(core[0]) >= 5:
        out.append(core[0])  # "faculty science" -> "faculty"; low confidence
    return [s for i, s in enumerate(out) if len(s) >= 3 and s not in out[:i]]


def get(url, retries=3, timeout=10):
    """Return (status, body). Timeouts count as a miss: Lever is very slow to 404."""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, b""
        except (TimeoutError, OSError):
            return None, b""
    return None, b""


def probe(company, providers):
    key = company["key"]
    hits = []
    cands = slugs(key)
    for provider in providers:
        tmpl = PROBES[provider]
        for slug in cands:
            status, body = get(tmpl.format(slug=slug))
            if status != 200:
                continue
            board_name = ""
            try:
                data = json.loads(body)
                if provider == "greenhouse":
                    board_name = data.get("name") or ""
                    _, jb = get(tmpl.format(slug=slug) + "/jobs")
                    jobs = len(json.loads(jb or b"{}").get("jobs", []))
                elif provider == "workable":
                    board_name = data.get("name") or ""
                    jobs = len(data.get("jobs", []))
                elif provider == "ashby":
                    jobs = len(data.get("jobs", []))
                else:  # lever, limit=1
                    jobs = len(data)
            except (ValueError, AttributeError):
                continue
            if not jobs:
                continue  # dormant or squatted board
            first_word_guess = len(key.split()) > 1 and slug == key.split()[0]
            if board_name:
                bn = norm(board_name)
                confident = (SequenceMatcher(None, bn, key).ratio() >= 0.85
                             or (len(bn) >= 5 and key.startswith(bn + " ")))
            else:
                confident = len(slug.replace("-", "")) >= 5
            if first_word_guess and not (board_name and confident):
                continue  # "agency delta" != the "agency" board
            if board_name and not confident and not first_word_guess:
                continue  # board belongs to a differently named company
            hits.append({"key": key, "name": company["name"], "tier": company["tier"],
                         "provider": provider, "slug": slug, "board_name": board_name,
                         "jobs": jobs, "confident": confident,
                         "first_word_guess": first_word_guess})
            break  # one slug per provider is enough
    return key, hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--providers", default="greenhouse,ashby,lever")
    ap.add_argument("--tiers", default="", help="comma-separated; default all")
    args = ap.parse_args()
    providers = args.providers.split(",")
    tiers = set(args.tiers.split(",")) if args.tiers else None

    done = set(PROBED.read_text(encoding="utf-8").splitlines()) if PROBED.exists() else set()
    tag = "+".join(providers)
    with open(SPONSORS, encoding="utf-8", newline="") as f:
        todo = [r for r in csv.DictReader(f)
                if f"{tag}\t{r['key']}" not in done and (tiers is None or r["tier"] in tiers)]
    if args.limit:
        todo = todo[: args.limit]
    print(f"probing {len(todo)} companies on {tag}", flush=True)

    found = 0
    start = time.time()
    with ThreadPoolExecutor(args.workers) as pool, \
            open(BOARDS, "a", encoding="utf-8") as bf, open(PROBED, "a", encoding="utf-8") as pf:
        futures = [pool.submit(probe, c, providers) for c in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            key, hits = fut.result()
            with lock:
                for h in hits:
                    bf.write(json.dumps(h) + "\n")
                pf.write(f"{tag}\t{key}\n")
                found += bool(hits)
                if i % 500 == 0:
                    bf.flush(); pf.flush()
                    print(f"{i}/{len(todo)} probed, {found} with boards, "
                          f"{time.time() - start:.0f}s", flush=True)
    print(f"done: {found} of {len(todo)} companies have a board -> {BOARDS}")


if __name__ == "__main__":
    main()
