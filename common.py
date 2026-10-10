"""Shared helpers: config, header-driven Google Sheet access, Gemini, Telegram."""
import hashlib
import html
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

import gspread
import requests
import yaml
from gspread.utils import rowcol_to_a1

# key -> pretty header label shown in the sheet
LABELS = {
    "score": "Score", "title": "Title", "company": "Company", "location": "Location",
    "fit_summary": "Why it fits", "watch_outs": "Watch-outs", "link": "Open",
    "interested": "Interested", "applied": "Applied", "interview": "Interview",
    "offer": "Offer received", "rejected": "Rejected", "notes": "Notes",
    "docs": "CV + letter", "date_posted": "Posted", "site": "Source",
    "job_key": "Job key", "job_url": "Job URL", "docs_url": "Docs URL",
    "description": "Description", "description_snippet": "Snippet",
    "scraped_at": "Scraped at", "notified": "Notified", "status": "Status",
}
LEGACY_KEYS = {"status", "description_snippet"}
DEFAULT_COLUMNS = [k for k in LABELS if k not in LEGACY_KEYS]


# ---------- text helpers ----------
def fold(s) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def norm_label(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", fold(s)).strip("_")


KEYMAP = {norm_label(v): k for k, v in LABELS.items()}


def to_key(label) -> str:
    n = norm_label(label)
    return KEYMAP.get(n, n)


def clean(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return "" if s.lower() in ("nan", "nat", "none") else s


def make_key(company, title, location) -> str:
    """Stored in the 'Job key' column. Unchanged from earlier versions."""
    city = norm(location).split(",")[0]
    raw = "|".join([norm(company), norm(title), city])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def loose_key(company, title, location) -> str:
    """Tolerant signature to catch the same job on different boards."""
    t = fold(title)
    t = re.sub(r"\(?\b[hfmx]\s*/\s*[hfmx](\s*/\s*[hfmx])?\b\)?", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    c = re.sub(r"[^a-z0-9]+", " ", fold(company)).strip()
    city = fold(location).split(",")[0]
    city = re.sub(r"\(.*?\)", "", city)
    city = re.sub(r"\bcedex\b", "", city)
    city = re.sub(r"[^a-z]+", " ", city).strip()
    return f"{c}|{t}|{city}"


def word_regex(words):
    parts = [r"(?<!\w)" + re.escape(w.strip()) + r"(?!\w)" for w in (words or []) if w.strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def slug(s, n=40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-")[:n] or "x"


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def load_config(path="config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_text(path, default="") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def parse_ts(s):
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_true(v) -> bool:
    return str(v).strip().upper() == "TRUE"


# ---------- Google Sheet (header-driven: columns can be reordered freely) ----------
class Sheet:
    def __init__(self, cfg):
        creds = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
        self.sh = gspread.service_account_from_dict(creds).open_by_key(os.environ["SHEET_ID"])
        name = cfg.get("worksheet", "Jobs")
        try:
            self.ws = self.sh.worksheet(name)
        except gspread.WorksheetNotFound:
            self.ws = self.sh.add_worksheet(title=name, rows=1000, cols=len(DEFAULT_COLUMNS))
        if not self.ws.row_values(1):
            self.ws.update(range_name="A1", values=[[LABELS[k] for k in DEFAULT_COLUMNS]])
        self.reload()

    def reload(self):
        vals = self.ws.get_all_values()
        self.header = [to_key(h) if h.strip() else "" for h in (vals[0] if vals else [])]
        self.cols = {k: i for i, k in enumerate(self.header) if k}
        self.rows = vals[1:]

    def ensure(self, keys):
        missing = [k for k in keys if k not in self.cols]
        if not missing:
            return
        first = len(self.header) + 1
        need = first + len(missing) - 1
        if need > self.ws.col_count:
            self.ws.add_cols(need - self.ws.col_count)
        self.ws.update(range_name=rowcol_to_a1(1, first), values=[[LABELS.get(k, k) for k in missing]])
        self.reload()

    def get(self, row, key) -> str:
        i = self.cols.get(key)
        return row[i] if i is not None and i < len(row) else ""

    def append(self, dicts):
        rows = [[d.get(k, "") if k else "" for k in self.header] for d in dicts]
        self.ws.append_rows(rows, value_input_option="RAW")
        self.reload()

    def update_cells(self, updates):
        """updates: list of (row_index_0_based, key, value). Written as RAW values."""
        data = [{"range": rowcol_to_a1(r + 2, self.cols[k] + 1), "values": [[v]]} for r, k, v in updates]
        for part in chunks(data, 300):
            self.ws.batch_update(part)

    def _write_formulas(self, a1, values):
        try:
            self.ws.update(range_name=a1, values=values, value_input_option="USER_ENTERED")
        except TypeError:
            self.ws.update(range_name=a1, values=values, raw=False)

    def write_formula(self, row_idx, key, formula):
        self._write_formulas(rowcol_to_a1(row_idx + 2, self.cols[key] + 1), [[formula]])

    def fill_links(self):
        """Fill the 'Open' column with =HYPERLINK(...) for rows that have a job URL."""
        if "link" not in self.cols or "job_url" not in self.cols:
            return
        todo = [i for i, r in enumerate(self.rows)
                if self.get(r, "job_url").startswith("http") and not self.get(r, "link")]
        if not todo:
            return
        letter = re.sub(r"\d", "", rowcol_to_a1(1, self.cols["link"] + 1))
        run = [todo[0]]
        for i in todo[1:] + [None]:
            if i is not None and i == run[-1] + 1:
                run.append(i)
                continue
            vals = [[f'=HYPERLINK("{self.get(self.rows[j], "job_url").replace(chr(34), "%22")}","Open ↗")']
                    for j in run]
            self._write_formulas(f"{letter}{run[0] + 2}:{letter}{run[-1] + 2}", vals)
            run = [i] if i is not None else []
        self.reload()

    def sort_by_score(self):
        if "score" not in self.cols:
            return
        req = {"sortRange": {
            "range": {"sheetId": self.ws.id, "startRowIndex": 1, "startColumnIndex": 0,
                      "endColumnIndex": len(self.header)},
            "sortSpecs": [{"dimensionIndex": self.cols["score"], "sortOrder": "DESCENDING"}]}}
        self.sh.batch_update({"requests": [req]})


# ---------- Gemini (free tier, plain REST) ----------
class QuotaExhausted(Exception):
    pass


def gemini(prompt, model, system=None, max_tokens=2048, json_mode=False, temperature=0.2):
    key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gen = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        gen["responseMimeType"] = "application/json"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    thinking_off = True
    for attempt in range(4):
        if thinking_off:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        else:
            gen.pop("thinkingConfig", None)
        r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                          json=body, timeout=180)
        if r.status_code == 200:
            data = r.json()
            try:
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts)
            except (KeyError, IndexError):
                raise RuntimeError(f"Unexpected Gemini response: {str(data)[:300]}")
        if r.status_code == 400 and thinking_off:
            thinking_off = False  # some models reject thinkingConfig: retry without it
            continue
        if r.status_code == 429:
            if "perday" in r.text.lower().replace(" ", "").replace("_", ""):
                raise QuotaExhausted(r.text[:200])
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Gemini: too many retries")


def parse_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M).strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("["), t.rfind("]")
        data = json.loads(t[a:b + 1])
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [data])
    return data


# ---------- Telegram (free Bot API) ----------
def telegram(text) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=30)
    if not r.ok:
        print(f"[warn] Telegram error {r.status_code}: {r.text[:200]}")
    return r.ok


def esc(s) -> str:
    return html.escape(str(s or ""))
