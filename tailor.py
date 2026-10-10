"""For high-scoring jobs: write a tailored CV + cover letter (Gemini, free tier) and save them in applications/."""
import os
import re
import time
from datetime import datetime, timedelta, timezone

from common import (QuotaExhausted, Sheet, gemini, is_true, load_config, parse_ts, read_text, slug)

SYSTEM = """You are an expert career coach who tailors internship applications.

ABSOLUTE RULES
- Use ONLY facts that appear in the candidate's CV. Never invent employers, dates, degrees, grades, skills, tools, numbers or achievements.
- You may reorder, shorten, rephrase and emphasize CV content, and mirror the job's keywords ONLY where the CV genuinely supports them.
- If the job needs something the CV does not show, do not claim it. List it under GAPS in the notes.
- Keep any placeholders from the CV (e.g. [Name], [Phone]) exactly as they are.
- Write in the language of the job posting (French or English). If unclear, use the CV's language.

OUTPUT FORMAT (exactly these three markers, each on its own line):
===CV===
(the tailored CV in Markdown, max one page: summary of 2-3 lines, then education, experience, projects, skills; most relevant items first)
===COVER_LETTER===
(a cover letter of 200-280 words: specific to this company and role, concrete evidence from the CV, no cliches, no flattery, professional tone, ends with a short call to action)
===NOTES===
(max 6 short bullets: what you emphasized, GAPS between CV and job, and anything the candidate must double-check before sending)"""


def split_sections(text):
    parts = re.split(r"^===(CV|COVER_LETTER|NOTES)===\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def main():
    cfg = load_config()
    t = cfg.get("tailoring", {})
    if not t.get("enabled", True):
        print("[skip] tailoring.enabled is false")
        return
    if not os.environ.get("GEMINI_API_KEY"):
        print("[skip] GEMINI_API_KEY not set")
        return
    cv, profile = read_text("cv.md"), read_text("profile.md")
    if len(cv.strip()) < 200:
        print("[skip] cv.md is empty or still the template.")
        return

    threshold = int(t.get("min_score", 80))
    max_n = int(t.get("max_per_run", 6))
    max_age = timedelta(days=int(t.get("max_age_days", 7)))
    model = t.get("model", "gemini-2.5-flash")
    gap = 60.0 / float(t.get("rpm", 8))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")

    sheet = Sheet(cfg)
    sheet.ensure(["docs", "docs_url", "rejected"])
    now = datetime.now(timezone.utc)
    cands = []
    for i, r in enumerate(sheet.rows):
        sc = sheet.get(r, "score")
        if not sc.isdigit() or int(sc) < threshold or sheet.get(r, "docs_url"):
            continue
        if is_true(sheet.get(r, "rejected")):
            continue
        ts = parse_ts(sheet.get(r, "scraped_at"))
        if ts and now - ts > max_age:
            continue
        cands.append((int(sc), i, r))
    cands.sort(key=lambda c: c[0], reverse=True)
    cands = cands[:max_n]
    print(f"[info] {len(cands)} jobs to tailor (score >= {threshold}) with {model}")

    last, done = 0.0, 0
    for sc, i, r in cands:
        title, company = sheet.get(r, "title"), sheet.get(r, "company")
        desc = (sheet.get(r, "description") or sheet.get(r, "description_snippet"))[:4000]
        url = sheet.get(r, "job_url")
        prompt = (f"CANDIDATE CV:\n{cv}\n\nCANDIDATE PREFERENCES:\n{profile}\n\n"
                  f"JOB:\nTitle: {title}\nCompany: {company}\nLocation: {sheet.get(r, 'location')}\n"
                  f"URL: {url}\nDescription:\n{desc or '(no description available: stay generic and say so in NOTES)'}")
        wait = gap - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        try:
            text = gemini(prompt, model, system=SYSTEM, max_tokens=6000, temperature=0.4)
        except QuotaExhausted as e:
            print(f"[warn] daily free quota reached, stopping: {e}")
            break
        except Exception as e:
            print(f"[warn] tailoring failed for '{title}' ({company}): {e}")
            continue
        sec = split_sections(text)
        if "CV" not in sec or "COVER_LETTER" not in sec:
            print(f"[warn] unexpected format for '{title}' ({company}), will retry next run")
            continue

        day = (sheet.get(r, "scraped_at") or now.strftime("%Y-%m-%d"))[:10]
        folder = f"applications/{day}_{slug(company, 25)}_{slug(title, 35)}_{sheet.get(r, 'job_key')[:6]}"
        os.makedirs(folder, exist_ok=True)
        with open(f"{folder}/cv.md", "w", encoding="utf-8") as f:
            f.write(sec["CV"] + "\n")
        with open(f"{folder}/cover_letter.md", "w", encoding="utf-8") as f:
            f.write(sec["COVER_LETTER"] + "\n")
        with open(f"{folder}/job.md", "w", encoding="utf-8") as f:
            f.write(f"# {title} - {company}\n\n- Score: {sc}/100\n- Why it fits: {sheet.get(r, 'fit_summary')}\n"
                    f"- Watch-outs: {sheet.get(r, 'watch_outs')}\n- Offer: {url}\n\n"
                    f"## AI notes (read before sending!)\n\n{sec.get('NOTES', '')}\n")
        link = f"{server}/{repo}/tree/main/{folder}" if repo else folder
        sheet.update_cells([(i, "docs_url", link)])
        sheet.write_formula(i, "docs", f'=HYPERLINK("{link}","Open ↗")')
        done += 1
        print(f"[info] tailored: {title} ({company}) -> {folder}")
    print(f"[done] tailored {done} applications")


if __name__ == "__main__":
    main()
