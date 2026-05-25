# Sample output

`sample_output.csv` contains 5 rows hand-picked from the full
`data/output_2017.csv` so reviewers can eyeball the extraction quality without
loading the 12 MB main file in Excel.

## Selection

Rows are taken at evenly-spaced positions across the deduplicated, sorted
file list (`extract_rd_data.collect_unique_files`). The sort key is the full
PDF path (`data/raw_2017/2017-MM/2017-MM-DD/page_N/<name>.pdf`), so the order
is quasi-chronological by archive download date — within a single day files
are alphabetical by `registration_number`, which is why the actual
`registration_date` of the picked rows does not line up exactly with the
quartile months.

| CSV index | `registration_date` | Source month |
|---:|---|---|
| 0    | 03-01-2017 | January  |
| 379  | 04-05-2017 | May      |
| 758  | 14-07-2017 | July     |
| 1137 | 13-10-2017 | October  |
| 1517 | 29-12-2017 | December |

Together they span all four quarters of 2017 and a mix of disciplines
(nano-physics, cultural studies, biology, ecology, geophysics).

## Regenerating

After re-running the full pipeline:

```bash
python -c "import pandas as pd; df = pd.read_csv('data/output_2017.csv', encoding='utf-8-sig', dtype=str).fillna(''); df.iloc[[0,379,758,1137,1517]].to_csv('samples/sample_output.csv', index=False, encoding='utf-8-sig')"
```

Same encoding (UTF-8 with BOM) and column layout as the main CSV.
