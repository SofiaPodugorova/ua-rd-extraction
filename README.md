# UA R&D Reports Extraction Pipeline — 2017–2025

This repository contains the rebuilt extraction of Ukrainian R&D reports
from the NRAT PDF archive. The nine yearly CSV files contain **47,401 rows**
in total. Every CSV has the same **101-column schema**, uses UTF-8 with BOM,
and keeps one row per unique source PDF filename.

The raw PDFs are intentionally not committed. The rebuilt inputs came from
the `nrat_pdfs` folder in the project Google Drive; only tables, extraction
logs, code, and small samples are stored in Git.

## Repository contents

| Path | Purpose |
|---|---|
| `extract_rd_data.py` | PDF-to-CSV extraction pipeline |
| `requirements.txt` | Python dependencies |
| `data/output_<year>.csv` | Rebuilt yearly tables for 2017–2025 |
| `data/extraction_log.txt` | 2017 per-file extraction log |
| `data/extraction_log_<year>.txt` | Per-file logs for 2018–2025 |
| `samples/sample_output.csv` | Five representative rebuilt 2017 rows |
| `samples/preview.html` | Readable HTML view of the same five rows |
| `samples/build_preview.py` | Deterministic sample/preview generator |

## Rebuilt outputs

| Year | Rows | OK | WARN | FAIL | Source notes |
|---:|---:|---:|---:|---:|---|
| 2017 | 6,100 | 6,100 | 0 | 0 | 6,100 unique PDF names |
| 2018 | 6,220 | 6,220 | 0 | 0 | 6,220 unique PDF names |
| 2019 | 6,652 | 6,644 | 8 | 0 | 7 missing PI names; 1 source has empty abstracts |
| 2020 | 1,961 | 1,946 | 15 | 0 | Drive folder contains January and February only |
| 2021 | 6,912 | 6,900 | 12 | Warnings are source-level missing PI names |
| 2022 | 4,820 | 4,784 | 36 | 5,591 Drive objects; 771 extra exact-name copies |
| 2023 | 5,347 | 5,343 | 4 | July is empty; one truncated PDF was recovered from the live NRAT source |
| 2024 | 4,782 | 4,773 | 9 | Two pairs share an internal registration number |
| 2025 | 4,607 | 4,607 | 0 | One pair shares an internal registration number |
| **Total** | **47,401** | **47,317** | **84** | **0** | |

All `WARN` rows were checked against their source PDFs. They represent
fields that are genuinely absent from the forms rather than parser
failures. No yearly run has a `FAIL`.

The current CSV sizes range from about 21 MB to 74 MB. GitHub may not render
the larger files in its table preview; download the raw file and open it in
Excel, LibreOffice Calc, Numbers, pandas, or another CSV reader.

## Input identity and deduplication

The script searches the input root recursively for `*.pdf` and deduplicates
only by the **complete source filename**. This matches the rebuilt Drive
archive:

- repeated folders containing the same complete filename are one source;
- different complete filenames remain distinct even when their registration
  prefix or internal registration number overlaps;
- sorting is deterministic, so repeated runs produce stable row order.

This distinction matters in the source data. In 2022, duplicate day folders
add 771 repeated objects with the same complete names. Conversely, 2024 has
two filename pairs and 2025 has one pair whose PDFs report the same internal
registration number. Those rows remain in the tables because the source
filenames are different. Downstream users who need one row per internal
registration can deduplicate on `registration_number`.

## Output schema

The 101 columns are ordered identically in every yearly table:

| Group | Content |
|---|---|
| System | `source_file`, `year`, row-level `language` |
| Section I | Registration number, date, special marks |
| Section II | Work stage, dates, report type |
| Section III | Performer identity and contact fields |
| Section IV | Co-performers as a JSON list when present |
| Section V | Customer identity and contact fields |
| Section VI | Legal basis, funding direction, sources, amounts |
| Section VII | Ukrainian/English titles and abstracts, UDC, codes, PI data |
| Section VIII | Scientific and technical output fields |
| Section IX | Bibliography |
| Section X | Organization head and executors |
| Language metadata | 39 `lang_<field>` columns |

Language values are `uk`, `en`, `mixed`, `unknown`, or `empty` for
field-level metadata. The row-level `language` column uses the same taxonomy
except `empty`. `DetectorFactory.seed = 0` keeps language detection
deterministic.

Free-text cells preserve embedded line breaks. `co_performers`, when
present, is JSON text containing the available organization fields.
Empty form fields remain empty rather than being guessed or imputed.

## Reproducing a yearly table

Requires Python 3.10+ and a local copy of that year's PDFs.

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

python extract_rd_data.py \
  --data-dir data/raw_2018 \
  --output data/output_2018.csv \
  --log data/extraction_log_2018.txt \
  --year 2018
```

The input may be flat or nested into month/day folders; the script searches
recursively. For a quick smoke test, add `--sample 5`. The default arguments
target the 2017 paths:

```bash
python extract_rd_data.py \
  --data-dir data/raw_2017 \
  --output data/output_2017.csv \
  --log data/extraction_log.txt \
  --year 2017
```

Raw directories under `data/raw_*` are ignored by Git.

## Extraction behavior

The embedded PDF fonts do not reliably decode through plain text
extraction. The pipeline uses PyMuPDF HTML extraction, unescapes the result,
finds the ordered Roman-numeral form sections, and parses their labelled
fields.

Each log contains:

- `DONE` for a successfully built record;
- `EMPTY` with all blank fields for auditing;
- `WARN` when a critical field is absent;
- `FAIL` when a PDF cannot be opened or parsing raises an exception;
- a final summary with file, row, column, and status counts.

Critical fields are `registration_number`, both titles, both abstracts,
`pi_name`, and `performer_name`.

One Drive file in the 2023 source,
`0223U002335_8c2291621873bda3bf520316f743a621.pdf`, was only 7,030 bytes
and lacked the PDF cross-reference/trailer. It was re-fetched from the live
NRAT document endpoint using the same document identifier and parsed under
its original source filename. The rebuilt row is present in
`data/output_2023.csv`.

## Samples

To inspect the schema without loading a full yearly file, open
`samples/sample_output.csv` or `samples/preview.html`.

Regenerate both from the rebuilt 2017 table with:

```bash
python samples/build_preview.py
```

See `samples/README.md` for the selected source rows and dates.
