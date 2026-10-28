"""Filter the Home Office sponsor register down to tech / tech-adjacent companies.

Joins the register (name only) to Companies House bulk data (which has SIC codes)
on a normalised company name, then keeps companies with tech SIC codes plus anything
listed in always_include.txt.

Usage:
    python -I build_sponsors.py <register.csv> <BasicCompanyDataAsOneFile-*.zip>

Writes data/sponsors_tech.csv.
"""
import csv
import io
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "data" / "sponsors_tech.csv"
ALWAYS = HERE / "always_include.txt"

ROUTES = {"Skilled Worker", "Scale-up"}
# Holding an intra-company transfer licence is a decent proxy for "multinational".
GBM_ROUTE = "Global Business Mobility: Senior or Specialist Worker"
TECHY_NAME = re.compile(
    r"\b(software|tech|technology|technologies|digital|data|labs?|ai|systems|cloud|cyber|"
    r"analytics|computing|robotics|fintech|it solutions|devops|platforms?)\b"
)

# SIC 2007 codes. "core" = software/IT; "adjacent" = worth a look.
CORE_SIC = {"58210", "58290", "62011", "62012", "62020", "62030", "62090", "63110", "63120"}
TIERS = ["always", "core", "multinational", "adjacent", "name"]
ADJACENT_SIC = {"61900", "63990", "72190", "71122", "74909", "64999", "66190"}

SUFFIXES = re.compile(
    r"\b(limited|ltd|plc|llp|lp|inc|incorporated|llc|corp|corporation|company|co|uk|u k|"
    r"gb|group|holdings|the|t a|trading as)\b"
)


def norm(name: str) -> str:
    n = name.lower().replace("&", " and ")
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = SUFFIXES.sub(" ", n)
    return " ".join(n.split())


def load_register(path):
    sponsors, gbm = {}, set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = norm(row["Organisation Name"].strip())
            if not key or "A" not in row["Type & Rating"]:
                continue
            route = row["Route"].strip()
            if route == GBM_ROUTE:
                gbm.add(key)
            if route in ROUTES:
                sponsors.setdefault(key, {"name": row["Organisation Name"].strip(),
                                          "town": row["Town/City"].strip()})
    return sponsors, gbm


def scan_companies_house(zip_path, keys):
    """Return {norm_name: (company_number, [sic codes], status)} for names in keys."""
    found = {}
    with zipfile.ZipFile(zip_path) as z:
        member = z.namelist()[0]
        with z.open(member) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""))
            header = [h.strip() for h in next(reader)]
            i_name = header.index("CompanyName")
            i_num = header.index("CompanyNumber")
            i_status = header.index("CompanyStatus")
            i_sic = [header.index(f"SICCode.SicText_{k}") for k in range(1, 5)]
            for row in reader:
                if len(row) < len(header):
                    continue
                key = norm(row[i_name])
                if key not in keys:
                    continue
                sics = [row[i].split(" ")[0] for i in i_sic if row[i].strip()]
                active = row[i_status].strip() == "Active"
                prev = found.get(key)
                # Prefer an active company over a dissolved one with the same name.
                if prev is None or (active and prev[2] != "Active"):
                    found[key] = (row[i_num], sics, row[i_status].strip())
    return found


def main(register_path, ch_zip):
    sponsors, gbm = load_register(register_path)
    always = set()
    if ALWAYS.exists():
        always = {norm(l) for l in ALWAYS.read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.startswith("#")}

    ch = scan_companies_house(ch_zip, sponsors.keys())

    rows = []
    for key, s in sponsors.items():
        num, sics, status = ch.get(key, ("", [], ""))
        if any(c in CORE_SIC for c in sics):
            tier = "core"
        elif any(c in ADJACENT_SIC for c in sics):
            tier = "adjacent"
        elif key in always:
            tier = "always"
        elif key in gbm:
            tier = "multinational"
        elif not num and TECHY_NAME.search(key):
            tier = "name"
        else:
            continue
        rows.append({"name": s["name"], "key": key, "town": s["town"], "tier": tier,
                     "company_number": num, "sic": " ".join(sics)})

    rows.sort(key=lambda r: (TIERS.index(r["tier"]), r["key"]))
    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    tiers = {t: sum(r["tier"] == t for r in rows) for t in TIERS}
    print(f"register sponsors (A-rated, Skilled Worker/Scale-up): {len(sponsors)}")
    print(f"matched to Companies House: {len(ch)} ({len(ch) / len(sponsors):.0%})")
    print(f"kept: {len(rows)} {tiers} -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
