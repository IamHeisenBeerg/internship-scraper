"""Scrape internships with JobSpy and append NEW, RELEVANT ones to a Google Sheet."""
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import gspread
import yaml
from jobspy import scrape_jobs

HEADERS = [
    "job_key", "title", "company", "location", "site", "job_url",
    "date_posted", "scraped_at", "status", "description_snippet",
]


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def make_key(company, title, location) -> str:
    """Stable ID: company + title + city. Also dedupes the same job across sites."""
    city = norm(location).split(",")[0]
    raw = "|".join([norm(company), norm(title), city])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def clean(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return "" if s.lower() in ("nan", "nat", "none") else s


def word_regex(words):
    """Whole-word matcher: 'intern' will NOT match 'international', 'lead' not 'leadership'."""
    parts = [r"(?<!\w)" + re.escape(w.strip()) + r"(?!\w)" for w in (words or []) if w.strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def load_config(path="config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def scrape(cfg):
    rows = []
    for term in cfg["search_terms"]:
        for loc in cfg["locations"]:
            kwargs = dict(
                site_name=cfg.get("sites", ["indeed"]),
                search_term=term,
                location=loc,
                results_wanted=cfg.get("results_wanted", 30),
                hours_old=cfg.get("hours_old", 72),
                job_type="internship",
                country_indeed=cfg.get("country_indeed", "france"),
                is_remote=cfg.get("remote_only", False),
                linkedin_fetch_description=False,
                verbose=0,
            )
            if "google" in kwargs["site_name"]:
                kwargs["google_search_term"] = f"{term} jobs near {loc} since yesterday"
            try:
                df = scrape_jobs(**kwargs)
                print(f"[info] {len(df):>3} results | {loc} | {term[:60]}")
                rows.extend(df.to_dict("records"))
            except Exception as e:  # one failing query must not kill the run
                print(f"[warn] failed | {loc} | {term[:60]} | {e}", file=sys.stderr)
            time.sleep(cfg.get("delay_seconds", 2))
    return rows


def main():
    cfg = load_config()
    include_re = word_regex(cfg.get("include_title_keywords"))
    exclude_re = word_regex(cfg.get("exclude_title_keywords"))
    topic = [k.lower() for k in cfg.get("require_any_keywords", [])]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    ws = open_worksheet(cfg)
    seen = set(ws.col_values(1)[1:])  # existing job_keys, skip header
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
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)  # also dedupes within this batch
        new_rows.append([
            key, title, company, clean(r.get("location")), clean(r.get("site")),
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
