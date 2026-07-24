"""Regenerate the small CSV and HTML previews from the rebuilt 2017 table."""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "output_2017.csv"
SAMPLE_CSV = ROOT / "samples" / "sample_output.csv"
PREVIEW_HTML = ROOT / "samples" / "preview.html"

# The NRAT 2017 folder has no July registrations. These five populated
# months give a useful spread across the year.
SAMPLE_MONTHS = (1, 4, 6, 10, 12)

GROUPS = (
    ("System columns", 0, 3),
    ("Section I — General info", 3, 6),
    ("Section II — Work stage", 6, 11),
    ("Section III — Performer", 11, 16),
    ("Section IV — Co-performers", 16, 17),
    ("Section V — Customer", 17, 22),
    ("Section VI — Funding", 22, 29),
    ("Section VII — Topic, abstract and PI", 29, 40),
    ("Section VIII — Sci-tech output", 40, 59),
    ("Section IX — Bibliography", 59, 60),
    ("Section X — Final info", 60, 62),
    ("Language metadata", 62, 101),
)


def choose_rows(df: pd.DataFrame) -> list[int]:
    dates = pd.to_datetime(
        df["registration_date"], format="%d-%m-%Y", errors="coerce"
    )
    chosen: list[int] = []
    for month in SAMPLE_MONTHS:
        candidates = df.index[
            (dates.dt.year == 2017) & (dates.dt.month == month)
        ].tolist()
        if not candidates:
            raise RuntimeError(f"No 2017 registrations found for month {month}")
        candidates.sort(key=lambda i: (dates.loc[i], df.loc[i, "source_file"]))
        chosen.append(candidates[len(candidates) // 2])
    return chosen


def cell(value: str) -> str:
    return html.escape(value).replace("\n", "<br>\n")


def build_html(sample: pd.DataFrame) -> str:
    report_headers = "".join(
        f"<th>{cell(value)}</th>" for value in sample["registration_number"]
    )
    rows: list[str] = []
    for title, start, end in GROUPS:
        columns = sample.columns[start:end]
        rows.append(
            f'<tr class="section"><th colspan="6">{html.escape(title)} '
            f"({len(columns)})</th></tr>"
        )
        for column in columns:
            values = "".join(f"<td>{cell(value)}</td>" for value in sample[column])
            rows.append(
                f'<tr><th class="colname">{html.escape(column)}</th>{values}</tr>'
            )

    selected_dates = ", ".join(sample["registration_date"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NRAT 2017 extraction preview</title>
<style>
  body {{ font-family: "Segoe UI", sans-serif; margin: 0; padding: 1rem; background: #f3f5f7; }}
  h1 {{ font-size: 1.2rem; color: #1c2733; margin: 0 0 .5rem; }}
  .legend {{ font-size: 12px; color: #52606d; margin-bottom: 1rem; }}
  .scroll {{ overflow: auto; max-height: 88vh; border: 1px solid #b8c2cc; background: white; }}
  table {{ border-collapse: collapse; font: 11px Consolas, monospace; }}
  th, td {{ border: 1px solid #dde3e8; padding: 4px 8px; vertical-align: top;
            max-width: 360px; white-space: pre-wrap; word-break: break-word; }}
  th {{ background: #285f8f; color: white; position: sticky; top: 0; z-index: 2; }}
  th.colname {{ background: #e7f0f8; color: #17466d; text-align: left; min-width: 245px;
                position: sticky; left: 0; z-index: 3; }}
  tr.section th {{ background: #d96b25; color: white; text-align: left; padding: 7px; }}
  tr:nth-child(even) td {{ background: #fafbfc; }}
</style>
</head>
<body>
<h1>NRAT 2017 — 5 representative reports × 101 fields</h1>
<div class="legend">
Generated from <code>data/output_2017.csv</code>. Selected registration dates:
{html.escape(selected_dates)}. Scroll vertically and horizontally to inspect all fields.
</div>
<div class="scroll">
<table>
<tr><th class="colname">Field</th>{report_headers}</tr>
{''.join(rows)}
</table>
</div>
</body>
</html>
"""


def main() -> None:
    df = pd.read_csv(
        SOURCE, dtype=str, keep_default_na=False, encoding="utf-8-sig"
    )
    if df.shape[1] != 101:
        raise RuntimeError(f"Expected 101 columns, found {df.shape[1]}")
    chosen = choose_rows(df)
    sample = df.loc[chosen].reset_index(drop=True)
    sample.to_csv(SAMPLE_CSV, index=False, encoding="utf-8-sig")
    PREVIEW_HTML.write_text(build_html(sample), encoding="utf-8")
    print(
        f"Wrote {SAMPLE_CSV.relative_to(ROOT)} and "
        f"{PREVIEW_HTML.relative_to(ROOT)} from rows {chosen}"
    )


if __name__ == "__main__":
    main()
