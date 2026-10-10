"""Send Telegram alerts for new high-fit jobs (free Bot API)."""
import os
from datetime import datetime, timedelta, timezone

from common import Sheet, esc, load_config, now_str, parse_ts, telegram


def main():
    cfg = load_config()
    n = cfg.get("notify", {})
    if not n.get("enabled", True):
        print("[skip] notify.enabled is false")
        return
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        print("[skip] Telegram secrets not set")
        return
    threshold = int(n.get("min_score", 75))
    max_msgs = int(n.get("max_messages_per_run", 10))
    max_age = timedelta(hours=int(n.get("max_age_hours", 72)))

    sheet = Sheet(cfg)
    sheet.ensure(["notified", "docs_url"])
    now = datetime.now(timezone.utc)
    cands, silent = [], []
    for i, r in enumerate(sheet.rows):
        sc = sheet.get(r, "score")
        if not sc.isdigit() or int(sc) < threshold or sheet.get(r, "notified"):
            continue
        ts = parse_ts(sheet.get(r, "scraped_at"))
        if ts and now - ts > max_age:
            silent.append((i, "notified", "skipped-old"))  # old backlog: no spam
        else:
            cands.append((int(sc), i, r))
    if silent:
        sheet.update_cells(silent)
    cands.sort(key=lambda c: c[0], reverse=True)

    sent = 0
    for sc, i, r in cands[:max_msgs]:
        icon = "🔥" if sc >= 90 else "⭐"
        lines = [f"{icon} <b>{sc}/100</b> · {esc(sheet.get(r, 'title'))}",
                 f"🏢 {esc(sheet.get(r, 'company'))} · 📍 {esc(sheet.get(r, 'location'))}"]
        if sheet.get(r, "fit_summary"):
            lines.append(f"💬 {esc(sheet.get(r, 'fit_summary'))}")
        if sheet.get(r, "watch_outs"):
            lines.append(f"⚠️ {esc(sheet.get(r, 'watch_outs'))}")
        links = []
        if sheet.get(r, "job_url").startswith("http"):
            links.append(f'<a href="{esc(sheet.get(r, "job_url"))}">Apply</a>')
        if sheet.get(r, "docs_url").startswith("http"):
            links.append(f'<a href="{esc(sheet.get(r, "docs_url"))}">CV + cover letter</a>')
        if links:
            lines.append("🔗 " + " · ".join(links))
        if telegram("\n".join(lines)):
            sheet.update_cells([(i, "notified", now_str())])
            sent += 1
    print(f"[done] sent {sent} Telegram alerts ({len(cands) - sent} waiting for the next run, {len(silent)} old skipped)")


if __name__ == "__main__":
    main()
