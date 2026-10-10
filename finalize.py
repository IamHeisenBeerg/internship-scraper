"""Last step of each run: fill 'Open' links and put the best-scored jobs on top."""
from common import Sheet, load_config


def main():
    cfg = load_config()
    sheet = Sheet(cfg)
    sheet.fill_links()
    if cfg.get("sheet", {}).get("sort_by_score", True):
        sheet.sort_by_score()
        print("[done] sheet sorted by score")


if __name__ == "__main__":
    main()
