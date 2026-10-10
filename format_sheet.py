"""One-time (re-runnable) sheet makeover: column order, widths, checkboxes, colors, filter."""
import re

from gspread.utils import rowcol_to_a1

from common import LABELS, LEGACY_KEYS, Sheet, load_config

ORDER = ["score", "title", "company", "location", "fit_summary", "watch_outs", "link",
         "interested", "applied", "interview", "offer", "rejected", "notes", "docs",
         "date_posted", "site",
         "job_key", "job_url", "docs_url", "description", "description_snippet",
         "scraped_at", "notified", "status"]
HIDDEN = {"job_key", "job_url", "docs_url", "description", "description_snippet",
          "scraped_at", "notified", "status"}
CHECKS = ["interested", "applied", "interview", "offer", "rejected"]
WRAP = {"fit_summary", "watch_outs", "notes"}
CENTER = {"score", "link", "docs", "date_posted", "site"} | set(CHECKS)
WIDTHS = {"score": 70, "title": 320, "company": 170, "location": 150, "fit_summary": 380,
          "watch_outs": 280, "link": 75, "interested": 90, "applied": 80, "interview": 85,
          "offer": 110, "rejected": 85, "notes": 260, "docs": 95, "date_posted": 95, "site": 85}


def rgb(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def letter(idx):
    return re.sub(r"\d", "", rowcol_to_a1(1, idx + 1))


def main():
    cfg = load_config()
    sheet = Sheet(cfg)
    sheet.ensure([k for k in ORDER if k not in LEGACY_KEYS])
    sid = sheet.ws.id

    # 1) reorder columns (only ever moves a column to the left)
    order = list(sheet.header)
    target = [k for k in ORDER if k in order] + [k for k in order if k not in ORDER]
    moves = []
    for t, key in enumerate(target):
        c = order.index(key, t)
        if c != t:
            moves.append({"moveDimension": {"source": {"sheetId": sid, "dimension": "COLUMNS",
                                                      "startIndex": c, "endIndex": c + 1},
                                            "destinationIndex": t}})
            order.insert(t, order.pop(c))
    if moves:
        sheet.sh.batch_update({"requests": moves})
    sheet.reload()

    # 2) pretty header labels
    raw_header = sheet.ws.row_values(1)
    labels = [LABELS.get(k, raw_header[i] if i < len(raw_header) else "") for i, k in enumerate(sheet.header)]
    sheet.ws.update(range_name="A1", values=[labels])
    sheet.reload()
    cols, n = sheet.cols, len(sheet.header)

    def rng(r0=None, r1=None, c0=None, c1=None):
        d = {"sheetId": sid}
        for k, v in (("startRowIndex", r0), ("endRowIndex", r1), ("startColumnIndex", c0), ("endColumnIndex", c1)):
            if v is not None:
                d[k] = v
        return d

    def fmt(r, cell, fields):
        return {"repeatCell": {"range": r, "cell": {"userEnteredFormat": cell}, "fields": f"userEnteredFormat({fields})"}}

    reqs = []
    # remove old conditional rules so re-running does not stack them
    meta = sheet.sh.fetch_sheet_metadata()
    for s in meta.get("sheets", []):
        if s["properties"]["sheetId"] == sid:
            for idx in range(len(s.get("conditionalFormats", [])) - 1, -1, -1):
                reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": idx}})

    reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                                           "fields": "gridProperties.frozenRowCount"}})
    reqs.append(fmt(rng(1, None, 0, n), {"verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP",
                                         "textFormat": {"fontSize": 10}},
                    "verticalAlignment,wrapStrategy,textFormat"))
    reqs.append(fmt(rng(0, 1, 0, n), {"backgroundColor": rgb("#1f2937"), "horizontalAlignment": "CENTER",
                                      "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP",
                                      "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": rgb("#ffffff")}},
                    "backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat"))
    reqs.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                                               "properties": {"pixelSize": 38}, "fields": "pixelSize"}})
    for k in WRAP & set(cols):
        reqs.append(fmt(rng(1, None, cols[k], cols[k] + 1), {"wrapStrategy": "WRAP"}, "wrapStrategy"))
    for k in CENTER & set(cols):
        reqs.append(fmt(rng(1, None, cols[k], cols[k] + 1), {"horizontalAlignment": "CENTER"}, "horizontalAlignment"))
    if "score" in cols:
        reqs.append(fmt(rng(1, None, cols["score"], cols["score"] + 1), {"textFormat": {"bold": True, "fontSize": 11}}, "textFormat"))
    for k, i in cols.items():
        props, fields = {"hiddenByUser": k in HIDDEN}, "hiddenByUser"
        if k in WIDTHS:
            props["pixelSize"], fields = WIDTHS[k], "hiddenByUser,pixelSize"
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                                   "properties": props, "fields": fields}})
    for k in CHECKS:  # checkboxes
        if k in cols:
            reqs.append({"setDataValidation": {"range": rng(1, None, cols[k], cols[k] + 1),
                                               "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}}})
    # conditional formatting: score gradient first (highest priority), then row colours
    pos = 0
    if "score" in cols:
        reqs.append({"addConditionalFormatRule": {"index": pos, "rule": {
            "ranges": [rng(1, None, cols["score"], cols["score"] + 1)],
            "gradientRule": {"minpoint": {"color": rgb("#f4a7a7"), "type": "NUMBER", "value": "30"},
                             "midpoint": {"color": rgb("#ffe599"), "type": "NUMBER", "value": "65"},
                             "maxpoint": {"color": rgb("#8fce7e"), "type": "NUMBER", "value": "95"}}}}})
        pos += 1
    row_rules = [("offer", {"backgroundColor": rgb("#b6e2b0")}),
                 ("rejected", {"textFormat": {"strikethrough": True, "foregroundColor": rgb("#9e9e9e")}}),
                 ("interview", {"backgroundColor": rgb("#ffe8a3")}),
                 ("applied", {"backgroundColor": rgb("#d3e5f8")})]
    for k, style in row_rules:
        if k in cols:
            reqs.append({"addConditionalFormatRule": {"index": pos, "rule": {
                "ranges": [rng(1, None, 0, n)],
                "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                              "values": [{"userEnteredValue": f"=${letter(cols[k])}2=TRUE"}]},
                                "format": style}}}})
            pos += 1
    reqs.append({"setBasicFilter": {"filter": {"range": rng(0, None, 0, n)}}})
    sheet.sh.batch_update({"requests": reqs})

    sheet.fill_links()
    if cfg.get("sheet", {}).get("sort_by_score", True):
        sheet.sort_by_score()
    print("[done] sheet formatted")


if __name__ == "__main__":
    main()
