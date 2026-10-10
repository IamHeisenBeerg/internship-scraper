"""Score unscored jobs against your CV + preferences with Gemini (free tier)."""
import os
import time

from common import (QuotaExhausted, Sheet, chunks, gemini, load_config, parse_json, read_text)

SYSTEM = """You are a rigorous career advisor. You score internship offers for ONE candidate, using only the candidate's CV and stated preferences.

Scale (integer 0-100):
- 90-100: excellent fit: role, skills, location and timing all match
- 75-89: strong fit: minor gaps
- 60-74: possible: noticeable gaps or doubts
- 0-59: weak: wrong field or level, missing core requirements, or not really an internship

Rules:
- Base every judgement on the CV and the job text. Never assume skills the CV does not show.
- Penalize clear conflicts with the stated preferences (location, dates, duration, language, deal-breakers). Boost slightly for target companies/sectors if the preferences list some.
- If a job has no description, judge from title and company only, put "limited info" in watch_outs and never exceed 85.
- Be strict: most jobs should land between 40 and 80.
- fit_summary: max 25 words, say concretely WHY it fits (skills / role match).
- watch_outs: max 20 words, gaps or risks; empty string if none.
- Write fit_summary and watch_outs in {language}.
Return ONLY a JSON array with one object per job: {"id": <same id>, "score": <int>, "fit_summary": "...", "watch_outs": "..."}"""


def main():
    cfg = load_config()
    ai = cfg.get("ai", {})
    if not ai.get("enabled", True):
        print("[skip] ai.enabled is false")
        return
    if not os.environ.get("GEMINI_API_KEY"):
        print("[skip] GEMINI_API_KEY not set")
        return
    cv, profile = read_text("cv.md"), read_text("profile.md")
    if len(cv.strip()) < 200:
        print("[skip] cv.md is empty or still the template. Paste your CV into cv.md first.")
        return

    model = ai.get("scoring_model", "gemini-2.5-flash-lite")
    bs = int(ai.get("batch_size", 5))
    max_n = int(ai.get("max_scored_per_run", 150))
    gap = 60.0 / float(ai.get("scoring_rpm", 12))
    lang = ai.get("output_language", "English")
    system = SYSTEM.replace("{language}", lang)

    sheet = Sheet(cfg)
    sheet.ensure(["score", "fit_summary", "watch_outs"])
    todo = [(i, r) for i, r in enumerate(sheet.rows) if sheet.get(r, "title") and sheet.get(r, "score") == ""]
    todo.sort(key=lambda t: sheet.get(t[1], "scraped_at"), reverse=True)  # newest first
    todo = todo[:max_n]
    print(f"[info] {len(todo)} jobs to score with {model}")

    last, scored = 0.0, 0
    for batch in chunks(todo, bs):
        jobs = [{
            "id": i, "title": sheet.get(r, "title"), "company": sheet.get(r, "company"),
            "location": sheet.get(r, "location"),
            "description": (sheet.get(r, "description") or sheet.get(r, "description_snippet"))[:1500],
        } for i, r in batch]
        prompt = (f"CANDIDATE CV:\n{cv}\n\nCANDIDATE PREFERENCES:\n{profile}\n\n"
                  f"JOBS (JSON):\n{jobs}")
        wait = gap - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        try:
            text = gemini(prompt, model, system=system, max_tokens=2048, json_mode=True)
            data = parse_json(text)
        except QuotaExhausted as e:
            print(f"[warn] daily free quota reached, stopping: {e}")
            break
        except Exception as e:
            print(f"[warn] batch failed, will retry next run: {e}")
            continue
        ids = {i for i, _ in batch}
        updates = []
        for item in data:
            try:
                i = int(item["id"])
                score = max(0, min(100, int(round(float(item["score"])))))
            except (KeyError, TypeError, ValueError):
                continue
            if i not in ids:
                continue
            updates += [(i, "score", score),
                        (i, "fit_summary", str(item.get("fit_summary", ""))[:300]),
                        (i, "watch_outs", str(item.get("watch_outs", ""))[:300])]
            scored += 1
        if updates:
            sheet.update_cells(updates)  # saved after every batch
    print(f"[done] scored {scored} jobs")


if __name__ == "__main__":
    main()
