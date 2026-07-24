# Sample output

`sample_output.csv` contains five rows from the rebuilt
`data/output_2017.csv`. It has the same 101-column schema and UTF-8 BOM as
the full yearly tables.

The rows are selected deterministically from the middle of the populated
registration months January, April, June, October, and December 2017:

| Source row | Registration number | Registration date |
|---:|---|---|
| 817 | `0217U003158` | 20-01-2017 |
| 4064 | `0217U003447` | 12-04-2017 |
| 4563 | `0217U003660` | 15-06-2017 |
| 5000 | `0217U001928` | 20-10-2017 |
| 5671 | `0217U006790` | 21-12-2017 |

`preview.html` presents the same reports as a transposed, self-contained
table so that all 101 fields remain readable.

## Regenerating

From the repository root:

```bash
python samples/build_preview.py
```

The script validates the 101-column input, rewrites both sample files, and
preserves Ukrainian text without mojibake.
