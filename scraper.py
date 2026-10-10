"""Scrape internships (Indeed + optional LinkedIn/Glassdoor/Google) and append NEW, RELEVANT ones to the sheet."""
import sys
import time
from collections import Counter

from jobspy import scrape_jobs

from common import (Sheet, clean, load_config, loose_key, make_key, now_str, word_regex)


def build_sources(cfg):
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
                print(f"[warn] {site}: stopping early (blocked or nothing found).", file=sys.stderr)
                return rows
            time.sleep(delay)
    return rows


def scrape(cfg):
    rows = []
    for src in build_sources(cfg):
        rows.extend(run_source(cfg, src))
    return rows


def main():
    cfg = load_config()
    include_re = word_regex(cfg.get("include_title_keywords"))
    exclude_re = word_regex(cfg.get("exclude_title_keywords"))
    topic = [k.lower() for k in cfg.get("require_any_keywords", [])]
    now = now_str()

    sheet = Sheet(cfg)
    sheet.ensure(["job_key", "title", "company", "location", "site", "job_url", "date_posted",
                  "scraped_at", "description", "link"])
    seen = {sheet.get(r, "job_key") for r in sheet.rows if sheet.get(r, "job_key")}
    seen_loose = {loose_key(sheet.get(r, "company"), sheet.get(r, "title"), sheet.get(r, "location"))
                  for r in sheet.rows if sheet.get(r, "title")}
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
        new_rows.append({
            "job_key": key, "title": title, "company": company,
            "location": clean(r.get("location")), "site": site,
            "job_url": clean(r.get("job_url")), "date_posted": clean(r.get("date_posted")),
            "scraped_at": now, "description": desc[:4000],
        })

    if new_rows:
        sheet.append(new_rows)
    sheet.fill_links()

    print("---- summary ----")
    for k, v in stats.items():
        print(f"{k}: {v}")
    print(f"[done] added {len(new_rows)} new jobs")


if __name__ == "__main__":
    main()
