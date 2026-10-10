"""Scrape internships (Indeed + optional LinkedIn/Glassdoor/Google) and append NEW, RELEVANT ones to a Google Sheet."""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone

import gspread
import yaml
from jobspy import scrape_jobs

HEADERS = [
    "job_key", "title", "company", "location", "site", "job_url",
    "date_posted", "scraped_at", "status", "description_snippet",
]


# ---------- helpers ----------
def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def make_key(company, title, location) -> str:
    """Stored in column A. Kept identical to earlier versions so old rows still match."""
    city = norm(location).split(",")[0]
    raw = "|".join([norm(company), norm(title), city])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def fold(s) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def loose_key(company, title, location) -> str:
    """Tolerant signature to catch the same job on different boards:
    ignores accents, '(H/F)' markers, '(75)' / 'cedex' in the city, punctuation."""
    t = fold(title)
    t = re.sub(r"\(?\b[hfmx]\s*/\s*[hfmx](\s*/\s*[hfmx])?\b\)?", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    c = re.sub(r"[^a-z0-9]+", " ", fold(company)).strip()
    city = fold(location).split(",")[0]
    city = re.sub(r"\(.*?\)", "", city)
    city = re.sub(r"\bcedex\b", "", city)
    city = re.sub(r"[^a-z]+", " ", city).strip()
    return f"{c}|{t}|{city}"


def clean(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return "" if s.lower() in ("nan", "nat", "none") else s


def word_regex(words):
    """Whole-word matcher: 'intern' will NOT match 'international'."""
    parts = [r"(?<!\w)" + re.escape(w.strip()) + r"(?!\w)" for w in (words or []) if w.strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def load_config(path="config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- Google Sheets ----------
def open_worksheet(cfg):
    creds = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
    gc = gspread.service_account_from_dict(creds)
    sh = gc.open_by_key(os.environ["SHEET_ID"])
    name = cfg.get("worksheet", "Jobs")
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(HEADERS))
    if not ws.row_values(1):
        ws.update(range_name="A1", values=[HEADERS])
    return ws


# ---------- scraping ----------
def build_sources(cfg):
    """Top-level search_terms/locations/sites = main source(s) (Indeed).
    `extra_sources` in config.yaml adds LinkedIn / Glassdoor / Google, each with own settings."""
    sources = []
    for site in cfg.get("sites", ["indeed"]):
        sources.append({"site": site, "search_terms": cfg["search_terms"], "locations": cfg["locations"]})
    sources.extend(cfg.get("extra_sources") or [])
    return sources


def run_source(cfg, src):
    site = src["site"]
    delay = src.get("delay_seconds", cfg.get("delay_seconds", 2))
    max_fail = src.get("max_failures", 3)
    max_empty = src.get("max_empty_in_a_row", 6)
    fails = empties = 0
    rows = []
    print(f"==== source: {site} ====")
    for term in src["search_terms"]:
        for loc in src["locations"]:
            kwargs = dict(
                site_name=[site],
                search_term=term,
                location=loc,
                results_wanted=src.get("results_wanted", cfg.get("results_wanted", 30)),
                hours_old=src.get("hours_old", cfg.get("hours_old", 72)),
                job_type="internship",
                country_indeed=cfg.get("country_indeed", "france"),
                is_remote=cfg.get("remote_only", False),
                linkedin_fetch_description=bool(src.get("fetch_description", False)),
                verbose=0,
            )
            if site == "google":
                kwargs["google_search_term"] = f"{term} jobs near {loc} since yesterday"
            try:
                df = scrape_jobs(**kwargs)
                n = len(df)
                print(f"[info] {site:10} {n:>3} results | {loc} | {term[:55]}")
                rows.extend(df.to_dict("records"))
                fails = 0
                empties = empties + 1 if n == 0 else 0
            except Exception as e:  # one failing query must not kill the run
                fails += 1
                print(f"[warn] {site} failed | {loc} | {term[:55]} | {e}", file=sys.stderr)
            if fails >= max_fail or empties >= max_empty:
                print(f"[warn] {site}: stopping early (blocked or nothing found). Check the log above.", file=sys.stderr)
                return rows
            time.sleep(delay)
    return rows


def scrape(cfg):
    rows = []
    for src in build_sources(cfg):
        rows.extend(run_source(cfg, src))
    return rows


# ---------- main ----------
def main():
    cfg = load_config()
    include_re = word_regex(cfg.get("include_title_keywords"))
    exclude_re = word_regex(cfg.get("exclude_title_keywords"))
    topic = [k.lower() for k in cfg.get("require_any_keywords", [])]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    ws = open_worksheet(cfg)
    existing = ws.get_all_values()[1:]  # skip header
    seen = {r[0] for r in existing if r and r[0]}
    seen_loose = {loose_key(r[2], r[1], r[3]) for r in existing if len(r) > 3}
    print(f"[info] {len(seen)} jobs already in sheet")

    stats = Counter()
    new_rows = []
    for r in scrape(cfg):
        stats["scraped"] += 1
        title, company = clean(r.get("title")), clean(r.get("company"))
        if not title or not company:
            stats["missing_title_or_company"] += 1
            continue
        if include_re and not include_re.search(title):
            stats["rejected: no internship word in title"] += 1
            continue
        if exclude_re and exclude_re.search(title):
            stats["rejected: excluded title word"] += 1
            continue
        desc = clean(r.get("description"))
        if topic and not any(k in f"{title} {desc[:3000]}".lower() for k in topic):
            stats["rejected: off-topic"] += 1
            continue
        key = make_key(company, title, r.get("location"))
        lk = loose_key(company, title, r.get("location"))
        if key in seen or lk in seen_loose:
            stats["duplicate"] += 1
            continue
        seen.add(key)
        seen_loose.add(lk)
        site = clean(r.get("site"))
        stats[f"ADDED from {site}"] += 1
        new_rows.append([
            key, title, company, clean(r.get("location")), site,
            clean(r.get("job_url")), clean(r.get("date_posted")), now, "",
            desc[:300].replace("\n", " "),
        ])

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")

    print("---- summary ----")
    for k, v in stats.items():
        print(f"{k}: {v}")
    print(f"[done] added {len(new_rows)} new jobs")


if __name__ == "__main__":
    main()
