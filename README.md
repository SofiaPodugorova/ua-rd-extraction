# UA R&D Reports Extraction Pipeline — 2017

Extracts 4 129 bilingual Ukrainian R&D project report PDFs (UKRISTEI /
Max Planck Keeper archive, 2017) into one structured CSV (1 518 rows ×
101 columns, English snake_case, UTF-8 with BOM).

## What's in this repo

| File | What it is |
|---|---|
| [`extract_rd_data.py`](extract_rd_data.py) | The extraction script (one file, ~660 lines, annotated). |
| [`requirements.txt`](requirements.txt) | Pinned Python dependencies. |
| [`data/output_2017.csv`](data/output_2017.csv) | **The result.** 1 518 rows × 101 columns, ~12 MB. |
| [`data/extraction_log.txt`](data/extraction_log.txt) | Per-file extraction log. |
| [`samples/sample_output.csv`](samples/sample_output.csv) | 5 hand-picked rows from the result. |
| [`samples/preview.html`](samples/preview.html) | Same 5 rows as a self-contained HTML page. |

Raw PDFs (~gigabytes) are **not** in the repo — only the extracted output.

## Quick look — code and result

You don't have to install anything to inspect what was produced.

**See the result as a table:**

1. Click **[`samples/sample_output.csv`](https://github.com/SofiaPodugorova/ua-rd-extraction/blob/main/samples/sample_output.csv)**
   on GitHub — small file (5 rows, ~42 KB), GitHub renders it as a sortable
   table right in the browser.
2. For the full result, open
   **[`data/output_2017.csv`](https://github.com/SofiaPodugorova/ua-rd-extraction/blob/main/data/output_2017.csv)**
   on GitHub and click **"Download raw file"** (top-right) — the file is
   12 MB so GitHub can't preview it inline. Then double-click the
   downloaded file: it opens in Excel / Numbers / LibreOffice Calc /
   Google Sheets with the Ukrainian text intact (UTF-8 BOM is set for
   exactly this).
3. Or grab the whole repo at once:

   ```bash
   git clone https://github.com/SofiaPodugorova/ua-rd-extraction.git
   ```

   …and double-click `data/output_2017.csv` locally.

**See the code:** open
[`extract_rd_data.py`](https://github.com/SofiaPodugorova/ua-rd-extraction/blob/main/extract_rd_data.py)
on GitHub, or in any text editor / IDE after cloning. The function order
mirrors the pipeline: PDF → text → sections → fields → language → CSV.

Column meanings and language-detection rules are documented in
[Output Files](#output-files) below; quirks of the source data are in
[Known Issues](#known-issues).

## Reproducing the pipeline from scratch

Only needed if you want to **re-run** extraction (e.g. on another year's
batch). Requires **Python 3.10+** and the raw PDF archive — the dataset
is not in the repo; put the UKRISTEI archive folder so that the path
`data/raw_2017/2017-01/`, …, `data/raw_2017/2017-12/` exists, or pass the
location via `--data-dir`.

### macOS

```bash
brew install python                            # skip if `python3 --version` ≥ 3.10
git clone https://github.com/SofiaPodugorova/ua-rd-extraction.git
cd ua-rd-extraction
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python extract_rd_data.py --sample 5           # smoke test on 5 files
python extract_rd_data.py                      # full run, ~1.5–3 min
```

### Windows (PowerShell)

```powershell
winget install --id Python.Python.3.12 -e      # skip if `python --version` ≥ 3.10
git clone https://github.com/SofiaPodugorova/ua-rd-extraction.git
cd ua-rd-extraction
python -m venv .venv
.\.venv\Scripts\Activate.ps1                   # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
pip install -r requirements.txt
python extract_rd_data.py --sample 5
python extract_rd_data.py
```

### Linux

Same as macOS, replacing `brew install python` with the distro's package
(`sudo apt install python3 python3-venv python3-pip` on Debian/Ubuntu).

### CLI options

| Flag | Default | Purpose |
|---|---|---|
| `--data-dir` | `data/raw_2017` | Root folder with month subdirectories of PDFs. Use this if the dataset lives outside the project, e.g. `--data-dir "D:\datasets\ukristei_2017"`. |
| `--output`   | `data/output_2017.csv` | Where to write the resulting CSV. |
| `--log`      | `data/extraction_log.txt` | Where to write the per-file log. |
| `--sample N` | `0` (full run) | Process only N evenly-spaced files — handy for a smoke test. |

### Input folder layout

```
data/raw_2017/
├── 2017-01/
│   └── 2017-01-03/
│       ├── page_1/0217U000001_Head_… .pdf, …
│       └── page_2/                ← same reports, deduplicated automatically
└── 2017-02/ … 2017-12/
```

Each report appears in 2–13 `page_N/` sub-directories (pagination
artefact). PDFs differ only in metadata — the extracted text is
identical — so the script keeps one copy per `registration_number`
(alphabetically first path). Only `*.pdf` files are read.

## Output Files

### `data/output_2017.csv`

UTF-8 with BOM. One row per unique report. Free-text fields preserve internal
newlines as `\n`. Columns by section (the three system columns required by
the spec — `source_file`, `year`, `language` — sit at the front of each row):

| Section | Columns |
|---|---|
| System | `source_file`, `year` (hardcoded `2017`), `language` (row-level summary) |
| I — General info | `registration_number`, `registration_date`, `special_marks` |
| II — Work stage | `stage_number`, `stage_title`, `stage_start`, `stage_end`, `report_type` |
| III — Performer | `performer_name`, `performer_edrpou`, `performer_location`, `performer_ministry`, `performer_phone` |
| IV — Co-performers | `co_performers` (JSON list of dicts, see Issue #3) |
| V — Customer | `customer_name`, `customer_edrpou`, `customer_location`, `customer_ministry`, `customer_phone` |
| VI — Funding | `legal_basis`, `funding_direction`, `funding_sources`, `budget_code`, `funding_amount_kgrn`, `funding_amount_usd`, `funding_amount_eur` |
| **VII — Topic / abstract / PI** | `title_uk`, `title_en`, `abstract_uk`, `abstract_en`, `udc_index`, `thematic_codes`, `pi_name`, `pi_degree`, `pi_title`, `pi_orcid`, `additional_info` |
| VIII — Sci-tech output (NTP) | `ntp_title_uk`, `ntp_title_en`, `ntp_planned`, `ntp_failure_reasons`, `ntp_results`, `ntp_application_field`, `ntp_card_number`, `ntp_description`, `ntp_socioeconomic`, `ntp_environment`, `ntp_implementation`, `ntp_consumers`, `ntp_markets`, `ntp_invest_amount_kgrn`, `ntp_investor_rights`, `ntp_business_plan`, `ntp_techno_economic_basis`, `ntp_sales_amount_kgrn`, `ntp_payback_years` |
| IX — Bibliography | `bibliography` |
| X — Final info | `org_head`, `executors` |

Naming conventions: `performer_*` / `customer_*` disambiguate the identical
labels in Sections III and V; `pi_*` is the PI sub-block of Section VII;
`ntp_*` is Section VIII; `_uk` / `_en` mark the Ukrainian / English variants
of titles and abstracts; `_kgrn` / `_usd` / `_eur` mark funding amounts per
currency.

In addition, every textual content field gets a companion `lang_<field>`
column with the detected language. Values: `uk` (≥85 % Cyrillic), `en`
(≤15 % Cyrillic), `mixed` (0.15–0.85 ratio with ≥40 letters), `unknown`
(<3 letters), `empty`. The row-level `language` column uses the same
taxonomy minus `empty` and is derived from pooled text of `title_uk`,
`abstract_uk`, `stage_title`, `performer_name`, `customer_name`,
`ntp_description`.

### `data/extraction_log.txt`

Per-file log (DEBUG+ to file, INFO+ to stdout, truncated each run):

```
2026-05-24 00:08:54 [INFO] Starting extraction from data/raw_2017
2026-05-24 00:08:54 [INFO] Found 1518 unique reports
2026-05-24 00:08:54 [DEBUG] EMPTY 0217U000001_… .pdf — performer_location, pi_orcid, …
2026-05-24 00:08:55 [DEBUG] DONE 0217U000001_… .pdf
2026-05-24 00:08:55 [WARNING] WARN 0217U007335_… .pdf — empty critical fields: title_uk
2026-05-24 00:08:55 [ERROR] FAIL 0217U002999_… .pdf — cannot open PDF: …
2026-05-24 00:09:54 [INFO] Summary -- files: 1518  OK: 1518  WARN: 0  FAIL: 0  -> data/output_2017.csv (1518 rows, 101 cols)
```

- `EMPTY` — every blank data field in the file (flags un-extractable fields
  without spamming stdout).
- `DONE` — record built successfully.
- `WARN` — at least one critical field empty (`registration_number`,
  `title_uk`, `title_en`, `abstract_uk`, `abstract_en`, `pi_name`,
  `performer_name`).
- `FAIL` — PDF could not be opened or parsing threw. Processing continues
  with the next file.

### `samples/`

`sample_output.csv` — 5 representative rows (months Jan / May / Jul / Oct /
Dec, indices 0/379/758/1137/1517). `preview.html` — wide-table HTML view.
See `samples/README.md` for selection rationale and a regeneration command.

## Known Issues

### 1. Font encoding — solved via HTML extraction
Plain `get_text("text")` returns garbled Cyrillic because the embedded Lora
fonts use a non-standard encoding. `get_text("html")` + `html.unescape()`
recovers correct Unicode without OCR.

### 2. Duplicates across `page_N/` directories
Each report is mirrored in 2–13 paginated sub-directories. PDFs are
byte-different (PDF metadata only); extracted text is identical. Script
deduplicates by `registration_number` → **1 518 unique reports** out of
4 129 raw PDFs.

### 3. Section IV — structured JSON when present
~5 % of reports (77/1518) list at least one co-performer; the rest leave
`co_performers` empty. When populated, the cell is a JSON-encoded list of
dicts with keys `name`, `edrpou`, `location`, `ownership`, `ministry`,
`ror`, `size`, `phone`, `contribution` (empty sub-fields omitted). If no
known label matches (unexpected layout), the raw text is kept verbatim
rather than silently dropped.

```json
[{"name": "Одеська національна академія харчових технологій",
  "edrpou": "02071062", "ror": "Не застосовується"}]
```

### 4. Section IX (bibliography) — often empty
Present in ~42 % of reports.

### 5. Universally-empty template fields in 2017
Several Section VII / VIII labels exist in every form template but were
never filled by institutes in the 2017 batch — `pi_orcid`,
`additional_info`, `ntp_planned`, `ntp_failure_reasons`, `ntp_card_number`,
`ntp_socioeconomic`, `ntp_environment`, `ntp_investor_rights`. Kept in the
schema for consistent row shape across reports and forward compatibility
with later years. The five commercial-track NTP fields (invest amount,
business plan, techno-economic basis, sales amount, payback years) are
populated only in 14–18 % of rows.

### 6. Language detection ladder
Decision order for `detect_field_language`:

1. Empty / whitespace → `empty`.
2. <3 alphabet characters → `unknown`.
3. Cyrillic ratio ≥0.85 → `uk`; ≤0.15 → `en`.
4. Mixed scripts, ≥40 letters → `mixed`.
5. Borderline short text → `langdetect`; accept `uk`/`en` verbatim, collapse
   anything else (`ru`, `mk`, `bg`, …) to `mixed`. langdetect routinely
   misfires to sibling Slavic languages on short Cyrillic strings.

`DetectorFactory.seed = 0` at import time keeps the langdetect sampler
deterministic so the CSV is byte-stable run-to-run.

### 7. Section heading detection
The Roman-numeral header regex requires a capital Cyrillic letter after the
numeral (e.g. `III. Відомості про виконавця`), which rejects false matches
against author initials in the bibliography. All 1 518 documents yield all
10 sections.

### 8. Field boundaries — explicit whitelist
Multi-line values terminate at the next `Label:` or sub-header line, defined
as **exact whitelists** in `extract_rd_data.py`: `_LABEL_NAMES_WITH_COLON`
(~50 entries, including all three funding-amount currency variants) and
`_SUB_HEADER_NAMES` (`Джерела фінансування`, `Керівники роботи`,
`Практична реалізація НТП`, `Характер співробітництва з інвестором`).
A heuristic like `[А-ЯІЄЇ][^\n:\d]{1,60}:` is unsafe — Ukrainian abstracts
routinely open with phrases that look like labels (`Об'єкт дослідження:`,
`Мета роботи:`, `Звіт про НДР:`) and would silently truncate hundreds of
values. Any label introduced in future-year batches must be added here.

### 9. Source-data artefacts (preserved as-is)
- `performer_location` is empty in 99.8 % of reports (institutes typically
  only fill the customer's address in Section V).
- `stage_title` of 5 reports starts with a literal `91` prefix (footnote
  marker not separated from text in the source). One report has
  `stage_number` = `91` instead of the typical 1–5.
- `performer_phone` of 23 reports contains Ukrainian abbreviations
  (`т.`, `факс`) mixed with the digits.
- `customer_edrpou` non-canonical in 62 / 1 516 non-empty rows: 24 with
  trailing period, 16 foreign placeholders (`BY000000`, `US000000`, …),
  22 with non-8-digit length. `performer_edrpou` non-canonical in 4 reports.
- 59 reports share the identical `udc_index` `006.03; 006.06, 006.03`
  (duplicated source data).
- 1 report has Ukrainian text in `title_en`; 2 in `abstract_en`. The
  `lang_*` columns flag these as `uk` so downstream tooling can spot them.
- Russian-language fragments occasionally appear inside Ukrainian-labelled
  fields (e.g. `0217U004886` quotes a Soviet-era standard). The detector
  reports them as `uk` — it distinguishes Ukrainian vs. English, not vs.
  Russian (see Issue #6).
- 9 reports funded in foreign currency use a non-canonical funding-amount
  label; their amounts are captured in `funding_amount_usd` /
  `funding_amount_eur`, kept separate from `funding_amount_kgrn`.
- `funding_sources` of 165 reports preserves an internal `\n` from a line
  wrap in the source form — kept verbatim per the cell-newline contract.
