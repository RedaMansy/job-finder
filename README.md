# Sponsored job finder

Finds open software-and-adjacent roles at UK licensed visa sponsors, straight from their
own job boards, and writes `../Shortlist.md`.

## Monthly (register refresh, ~1.5 h, runs unattended)

1. Download the latest "Worker and Temporary Worker" register CSV from
   https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers
2. Download `BasicCompanyDataAsOneFile-*.zip` from
   https://download.companieshouse.gov.uk/en_output.html into `data/companies-house/`
3. Run:

```
python build_sponsors.py <register.csv> data/companies-house/BasicCompanyDataAsOneFile-<date>.zip
python find_boards.py
python find_boards.py --providers workable --workers 1 --tiers always,core   # optional, slow
```

`find_boards.py` only probes companies it hasn't probed before, so re-runs are cheap.

## Daily (~1 min)

```
python daily.py            # or --days 30 to hide older postings
```

Then open `Shortlist.md` in Obsidian and `/apply <link>` on anything worth it.

## Tuning

- `always_include.txt`: sponsors to keep even though their SIC code isn't tech.
- `boards_manual.txt`: job boards the name-guessing misses (e.g. Netcompany's
  SmartRecruiters board). Add one whenever you spot a sponsor's careers page on
  Greenhouse/Lever/Ashby/Workable/SmartRecruiters.
- `daily.py`: the role, seniority and location regexes plus `MIN_SALARY` live at the top.

## Caveats

- A sponsor licence doesn't mean every role is sponsored. Roles whose description says
  "no sponsorship" are flagged ❌, and ones that offer it are flagged ✅.
- `MIN_SALARY` is the lower going rate for SOC 2134 (£40,000, gov.uk table updated
  22 July 2025). Other codes differ: 2139 (DevOps, IT consultants) is £38,700 and
  2133 (data engineers, architects) is £42,400.
- Big employers on Workday or their own sites (Amazon, Google, banks) aren't covered
  and still need checking by hand.
