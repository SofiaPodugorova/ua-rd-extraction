"""Tests for extract_rd_data.

Unit tests exercise the parser on small synthetic strings; integration tests
load the generated `data/output_2017.csv` and assert invariants that would be
violated by historical bugs (label-leak, section-title leak, УкрІНТЕІ footer
leak, `(КПКВК)` suffix capture, etc.).

Run with:
    pytest tests/ -v
"""

import json
from pathlib import Path
import sys

import pandas as pd
import pytest

# Make the project root importable when pytest is run from the repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extract_rd_data import (  # noqa: E402
    _field,
    detect_field_language,
    detect_row_language,
    parse_section_i,
    parse_section_iv,
    parse_section_v,
    parse_section_vii,
    parse_section_viii,
    parse_section_x,
    split_sections,
)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_single_line_empty_value_does_not_capture_next_label():
    text = "Науковий ступінь: \n Наукове звання: \n"
    assert _field(text, "Науковий ступінь") == ""


def test_single_line_value_present():
    text = "Науковий ступінь: д. т. н. \n Наукове звання: професор\n"
    assert _field(text, "Науковий ступінь") == "д. т. н."


def test_multiline_empty_value_does_not_leak():
    text = "Місцезнаходження: \n Форма власності: \n Сфера управління: Foo\n"
    assert _field(text, "Місцезнаходження", multiline=True) == ""


def test_multiline_value_on_same_line():
    text = "Сфера управління: Кабінет Міністрів України \n Ідентифікатор ROR: Х\n"
    assert _field(text, "Сфера управління", multiline=True) == "Кабінет Міністрів України"


def test_multiline_value_on_next_line_with_wrap():
    text = (
        "Назва роботи українською: \n"
        " Розробка системи для дослідження \n"
        " параметрів колоїдних розчинів. \n"
        " Назва роботи англійською: \n"
        " Development of the system.\n"
    )
    val = _field(text, "Назва роботи українською", multiline=True)
    assert "Розробка системи для дослідження" in val
    assert "колоїдних розчинів" in val
    assert "англійською" not in val


def test_budget_code_extracts_digits_only():
    text = "Код програмної класифікації видатків і кредитування (КПКВК): 2201040\n"
    assert _field(
        text,
        "Код програмної класифікації видатків і кредитування (КПКВК)",
    ) == "2201040"


def test_content_line_with_colon_is_not_treated_as_label():
    # Realistic case: a Ukrainian title that embeds a colon mid-sentence.
    text = (
        "Назва роботи українською: \n"
        " Аналіз даних та моделювання середовища у хмарній інфраструктурі: "
        "інформаційні технології прогнозу \n"
        " Реферат українською: \n"
        " Розроблено...\n"
    )
    val = _field(text, "Назва роботи українською", multiline=True)
    assert "Аналіз даних" in val
    assert "Реферат" not in val


def test_ukrainian_subheading_colons_inside_value():
    # Abstracts routinely open with phrases like "Об'єкт дослідження:" and
    # "Мета роботи:" that look like labels but are content. Whitelist-based
    # boundary detection must NOT split the value on them.
    text = (
        "Реферат українською: \n"
        " Об'єкт дослідження: процеси у напівпровідниках. \n"
        " Мета роботи: розробити нові методи. \n"
        " Реферат англійською: \n Foo\n"
    )
    val = _field(text, "Реферат українською", multiline=True)
    assert "Об'єкт дослідження" in val
    assert "Мета роботи" in val
    assert "Foo" not in val


def test_split_sections_drops_title_line():
    text = (
        "IX. Бібліографічний опис \n"
        " 1. Foo bar. \n"
        " 2. Baz qux. \n"
        "X. Заключні відомості \n"
        " Керівник юридичної особи \n"
    )
    sections = split_sections(text)
    assert not sections["IX"].startswith("Бібліографічний опис")
    assert sections["IX"].startswith("1. Foo bar")


def test_split_sections_empty_title_only_block_is_empty():
    text = "IX. Бібліографічний опис\nX. Заключні відомості\n"
    sections = split_sections(text)
    assert sections["IX"] == ""


def test_detect_field_language_pure_ukrainian():
    assert detect_field_language("Розробка програмно-апаратної системи") == "uk"


def test_detect_field_language_pure_english():
    assert detect_field_language("Development of the software system") == "en"


def test_detect_field_language_mixed():
    assert detect_field_language(
        "Гібридна система with значущою English component і Cyrillic words"
    ) == "mixed"


def test_detect_field_language_empty():
    assert detect_field_language("") == "empty"
    assert detect_field_language("   ") == "empty"


def test_detect_field_language_too_short():
    # 1 letter — undetectable
    assert detect_field_language("A") == "unknown"


def test_detect_row_language_ukrainian_record():
    record = {
        "title_uk": "Розробка нової методики аналізу",
        "abstract_uk": "Виконано аналіз нової методики для дослідження.",
        "stage_title": "Перший етап дослідження",
        "performer_name": "Київський національний університет",
        "customer_name": "Міністерство освіти",
        "ntp_description": "Опис розробленої методики",
    }
    assert detect_row_language(record) == "uk"


def test_detect_row_language_empty_record_is_unknown():
    assert detect_row_language({}) == "unknown"


# ---------------------------------------------------------------------------
# Per-section parser unit tests
# ---------------------------------------------------------------------------

def test_parse_section_i_basic():
    text = (
        "Державний обліковий номер: 0217U001234 \n"
        " Особливі позначки: відкрита \n"
        " Дата реєстрації: 15-03-2017 \n"
    )
    out = parse_section_i(text)
    assert out["registration_number"] == "0217U001234"
    assert out["special_marks"]       == "відкрита"
    assert out["registration_date"]   == "15-03-2017"


def test_parse_section_vii_bilingual():
    text = (
        "Назва роботи українською: \n"
        " Розробка методики \n"
        " Назва роботи англійською: \n"
        " Development of the method \n"
        " Реферат українською: \n"
        " Об'єкт дослідження: процеси теплообміну. \n"
        " Реферат англійською: \n"
        " The work investigates heat exchange. \n"
        " Індекс УДК: 536.2 \n"
        " Коди тематичних рубрик: 29.31.23 \n"
        " Власне Прізвище Ім'я По-батькові: Іванов Іван Іванович \n"
        " Науковий ступінь: д. т. н. \n"
        " Наукове звання: професор \n"
        " Ідентифікатор ORCHID ID: \n"
        " Додаткова інформація: примітка автора\n"
    )
    out = parse_section_vii(text)
    assert out["title_uk"] == "Розробка методики"
    assert out["title_en"] == "Development of the method"
    assert "Об'єкт дослідження" in out["abstract_uk"]   # subheading colon preserved
    assert out["abstract_en"].startswith("The work investigates")
    assert out["udc_index"] == "536.2"
    assert out["thematic_codes"] == "29.31.23"
    assert out["pi_name"] == "Іванов Іван Іванович"
    assert out["pi_degree"] == "д. т. н."
    assert out["pi_title"]  == "професор"
    assert out["pi_orcid"]  == ""  # empty in source
    assert out["additional_info"] == "примітка автора"


def test_parse_section_v_extracts_customer_phone():
    text = (
        "Повне найменування юридичної особи: Замовник \n"
        " Код за ЄДРПОУ: 12345678 \n"
        " Місцезнаходження: \n"
        " Сфера управління: Міністерство \n"
        " Телефон: 380442223344\n"
    )
    out = parse_section_v(text)
    assert out["customer_name"] == "Замовник"
    assert out["customer_phone"] == "380442223344"


def test_parse_section_viii_full_investment_block():
    text = (
        "Назва НТП українською: методика \n"
        " Назва НТП англійською: methodology \n"
        " НТП, яку передбачалося створити: \n"
        " Причини, через які НТП не було створено: \n"
        " Отримані результати: результат \n"
        " Галузь застосування: освіта \n"
        " Реєстраційний номер картки технології: \n"
        " Опис НТП: опис \n"
        " Соціально-економічна спрямованість НТП: \n"
        " Вплив НТП на довкілля: \n"
        " Впровадження НТП: Впроваджено \n"
        " Практична реалізація НТП \n"
        " Початок етапу: 01.2017 \n"
        " Споживачі продукції: студенти \n"
        " Перспективні ринки: Україна \n"
        " Характер співробітництва з інвестором \n"
        " Потрібний обсяг інвестицій, тис. грн.: 500 \n"
        " Права, що надаються інвестору після завершення роботи: \n"
        " Наявність бізнес-плану: Так \n"
        " Техніко-економічне обгрунтування: Ні \n"
        " Потенціальний обсяг продажу, тис. грн.: 800 \n"
        " Очікуваний термін окупності (років): 3\n"
    )
    out = parse_section_viii(text)
    assert out["ntp_title_uk"] == "методика"
    assert out["ntp_results"] == "результат"
    assert out["ntp_implementation"] == "Впроваджено"
    assert out["ntp_markets"] == "Україна"
    assert out["ntp_invest_amount_kgrn"] == "500"
    assert out["ntp_business_plan"] == "Так"
    assert out["ntp_techno_economic_basis"] == "Ні"
    assert out["ntp_sales_amount_kgrn"] == "800"
    assert out["ntp_payback_years"] == "3"
    # Empty fields are captured as empty strings, not missing keys
    assert out["ntp_planned"] == ""
    assert out["ntp_socioeconomic"] == ""


def test_parse_section_x_extracts_head_and_executors_without_footer():
    text = (
        "Заключні відомості \n"
        " Керівник юридичної особи \n"
        " Петренко Петро Петрович \n"
        " Перелік осіб-виконавців \n"
        " Іваненко Іван Іванович \n"
        " Сидоренко Сидір Сидорович \n"
        " Відповідальний за підготовку \n"
        " облікових документів \n"
        " Телефон \n"
        " Реєстратор \n"
        " Керівник відділу УкрІНТЕІ\n"
    )
    out = parse_section_x(text)
    assert out["org_head"] == "Петренко Петро Петрович"
    assert "Іваненко" in out["executors"]
    assert "Сидоренко" in out["executors"]
    assert "Відповідальний" not in out["executors"]
    assert "УкрІНТЕІ"      not in out["executors"]
    assert "\n" not in out["org_head"]  # always one line


def test_parse_section_x_without_executors_keeps_org_head():
    text = (
        "Заключні відомості \n"
        " Керівник юридичної особи \n"
        " Шевченко Тарас Григорович \n"
        " Відповідальний за підготовку \n облікових документів \n"
    )
    out = parse_section_x(text)
    assert out["org_head"] == "Шевченко Тарас Григорович"
    assert out["executors"] == ""


def test_parse_section_iv_empty_when_title_only():
    # In the common case Section IV's body, after split_sections strips the
    # title, contains only whitespace — no real co-performer data.
    assert parse_section_iv("") == {"co_performers": ""}
    assert parse_section_iv("   ") == {"co_performers": ""}


def test_parse_section_iv_returns_json_list_of_dicts():
    body = (
        "Повне найменування юридичної особи: Foo Bar Institute \n"
        " Код за ЄДРПОУ: 12345678 \n"
        " Внесок співвиконавця у звітний етап: 30%"
    )
    out = parse_section_iv(body)
    parsed = json.loads(out["co_performers"])
    assert isinstance(parsed, list) and len(parsed) == 1
    rec = parsed[0]
    assert rec["name"] == "Foo Bar Institute"
    assert rec["edrpou"] == "12345678"
    assert rec["contribution"] == "30%"


def test_parse_section_iv_multiple_co_performers():
    body = (
        "Повне найменування юридичної особи: First Inst \n"
        " Код за ЄДРПОУ: 11111111 \n"
        "Повне найменування юридичної особи: Second Inst \n"
        " Код за ЄДРПОУ: 22222222 \n"
    )
    out = parse_section_iv(body)
    parsed = json.loads(out["co_performers"])
    assert [r["name"] for r in parsed] == ["First Inst", "Second Inst"]


# ---------------------------------------------------------------------------
# Integration tests on the generated CSV
# ---------------------------------------------------------------------------

CSV_PATH = ROOT / "data" / "output_2017.csv"


def _load_csv_raw():
    if not CSV_PATH.exists():
        pytest.skip(f"CSV not generated yet: {CSV_PATH}")
    return pd.read_csv(CSV_PATH, encoding="utf-8-sig", dtype=str).fillna("")


@pytest.fixture(scope="module")
def df_raw():
    """CSV as written. Single all-English schema (no rename layer)."""
    return _load_csv_raw()


@pytest.fixture(scope="module")
def df(df_raw):
    """Alias of `df_raw`. Kept for assertion readability across tests."""
    return df_raw


def test_csv_has_expected_column_names(df_raw):
    """All columns are English snake_case (internal IDs)."""
    cols = set(df_raw.columns)
    expected = [
        # System
        "source_file", "year", "language",
        # Section I
        "registration_number", "registration_date",
        # Section III / V — performer-vs-customer prefix disambiguation
        "performer_phone", "customer_phone",
        "performer_edrpou", "customer_edrpou",
        # Section VI — funding amount per currency
        "funding_amount_kgrn", "funding_amount_usd", "funding_amount_eur",
        # Section VII — NLP core
        "title_uk", "abstract_uk", "pi_name",
        # Per-field language detection prefix
        "lang_title_uk", "lang_abstract_uk",
    ]
    missing = [c for c in expected if c not in cols]
    assert not missing, f"Expected columns missing from CSV: {missing}"


def test_csv_row_count(df):
    assert len(df) == 1518


def test_registration_numbers_are_unique_and_well_formed(df):
    assert df["registration_number"].nunique() == 1518
    assert df["registration_number"].str.match(r"^\d{4}U\d{6}$").all()


def test_no_pi_label_leak(df):
    # pi_orcid, pi_degree, pi_title must not contain neighbouring field labels
    for col, bad_pattern in [
        ("pi_orcid",  r"Наукове звання:|Додаткова інформація:|Ідентифікатор ORCHID"),
        ("pi_degree", r"Наукове звання:|Ідентифікатор ORCHID"),
        ("pi_title",  r"Ідентифікатор ORCHID|Додаткова інформація:"),
    ]:
        leaks = df[col].str.contains(bad_pattern, regex=True).sum()
        assert leaks == 0, f"{col}: {leaks} label leaks ({bad_pattern})"


def test_no_form_власності_in_locations(df):
    for col in ("performer_location", "customer_location"):
        leaks = df[col].str.contains("Форма власності:", regex=False).sum()
        assert leaks == 0, f"{col}: {leaks} 'Форма власності:' leaks"


def test_no_sphere_management_label_leak_in_ministry_columns(df):
    for col in ("performer_ministry", "customer_ministry"):
        leaks = df[col].str.contains("Сфера управління:", regex=False).sum()
        assert leaks == 0, f"{col}: {leaks} 'Сфера управління:' leaks"


def test_budget_code_is_digits_only(df):
    nonempty = df["budget_code"].str.strip() != ""
    bad = df.loc[nonempty, "budget_code"]
    assert bad.str.match(r"^\d+$").all(), (
        f"budget_code contains non-digit values: {bad[~bad.str.match(r'^\\d+$')].head().tolist()}"
    )


def test_ntp_results_does_not_swallow_next_field(df):
    leaks = df["ntp_results"].str.contains("Галузь застосування:", regex=False).sum()
    assert leaks == 0


def test_executors_has_no_ukrintei_footer(df):
    # The bureaucratic footer text from UkrINTEI should be stripped, not stored
    for marker in ("Відповідальний за підготовку", "Реєстратор"):
        leaks = df["executors"].str.contains(marker, regex=False).sum()
        assert leaks == 0, f"executors: {leaks} '{marker}' leaks"


def test_org_head_is_single_line(df):
    multi = df["org_head"].str.contains("\n", regex=False).sum()
    assert multi == 0, f"org_head: {multi} multi-line entries"


def test_co_performers_does_not_contain_section_title(df):
    bad = df["co_performers"].str.startswith("Відомості про співвиконавців").sum()
    assert bad == 0


def test_bibliography_does_not_start_with_section_title(df):
    bad = df["bibliography"].str.startswith("Бібліографічний опис").sum()
    assert bad == 0


def test_field_level_language_columns_present(df):
    lang_cols = [c for c in df.columns if c.startswith("lang_")]
    assert len(lang_cols) >= 25, f"expected ≥25 lang_* columns, found {len(lang_cols)}"


def test_customer_phone_populated_in_most_rows(df):
    # Section V's `Телефон:` field is real data — must populate in ≥95 %
    # of rows. A drop below that indicates the customer-phone label is no
    # longer being caught by the parser.
    filled = (df["customer_phone"].str.strip() != "").sum()
    assert filled >= 1450, f"customer_phone only populated in {filled} rows"


def test_field_level_language_values_are_in_known_set(df):
    # Per-field language columns must only contain uk / en / mixed (the
    # `unknown` / `empty` markers are internal codes for missing or
    # undetectable text). Exotic langdetect codes like `ru` / `mk` / `bg`
    # were almost always false positives on short Cyrillic names and are
    # normalised to `mixed` upstream.
    allowed = {"uk", "en", "mixed", "unknown", "empty"}
    for col in (c for c in df.columns if c.startswith("lang_")):
        unknown = set(df[col].unique()) - allowed
        assert not unknown, f"{col}: unexpected lang values {unknown}"


def test_title_uk_is_predominantly_ukrainian(df):
    nonempty = df["lang_title_uk"] != "empty"
    uk_rate = (df.loc[nonempty, "lang_title_uk"] == "uk").mean()
    assert uk_rate > 0.95, f"title_uk only {uk_rate:.1%} Ukrainian"


def test_title_en_is_predominantly_english(df):
    nonempty = df["lang_title_en"] != "empty"
    en_rate = (df.loc[nonempty, "lang_title_en"] == "en").mean()
    assert en_rate > 0.95, f"title_en only {en_rate:.1%} English"


def test_language_column_present_and_per_row(df):
    # Row-level language taxonomy mirrors the per-field detector minus
    # `empty` — an empty record reports `unknown`, not `empty`, since we
    # cannot tell missing-data from missing-language.
    assert "language" in df.columns
    allowed = {"uk", "en", "mixed", "unknown"}
    extras = set(df["language"].unique()) - allowed
    assert not extras, f"unexpected language values: {extras}"


def test_no_ukrainian_parser_misses_in_abstract(df):
    # Zero rows should have an empty `abstract_uk` while their `abstract_en`
    # carries real content. Any non-zero count is a parser miss.
    empty_uk = df["abstract_uk"].str.strip() == ""
    real_en  = df["abstract_en"].str.len() >= 50
    suspect  = (empty_uk & real_en).sum()
    assert suspect == 0, f"{suspect} probable abstract_uk parser misses"


def test_no_ukrainian_parser_misses_in_title(df):
    empty_uk = df["title_uk"].str.strip() == ""
    real_en  = df["title_en"].str.len() >= 20
    suspect  = (empty_uk & real_en).sum()
    assert suspect == 0, f"{suspect} probable title_uk parser misses"


def test_samples_csv_schema_matches_main(df_raw):
    """Both files must carry the same Ukrainian column names in the same
    order — `df_raw` is the un-renamed view of the main CSV, so this
    catches drift in either name OR ordering."""
    samples = pd.read_csv(
        ROOT / "samples" / "sample_output.csv",
        encoding="utf-8-sig",
        dtype=str,
    ).fillna("")
    assert list(samples.columns) == list(df_raw.columns), (
        "samples/sample_output.csv schema diverged from data/output_2017.csv — "
        "regenerate after column changes"
    )


def test_funding_direction_clean_of_subheader_leak(df):
    """`funding_direction` must not swallow the `Джерела фінансування`
    sub-header or its value. Stays clean only if `_SUB_HEADER_NAMES` boundary
    detection is in place."""
    leaks = df["funding_direction"].str.contains(
        "Джерела фінансування", regex=False
    ).sum()
    assert leaks == 0, (
        f"{leaks} rows contaminated by sub-header 'Джерела фінансування'"
    )


def test_ntp_markets_clean_of_cooperation_subheader(df):
    """`ntp_markets` must not swallow the `Характер співробітництва з інвестором`
    sub-header — relies on the same sub-header boundary detection."""
    leaks = df["ntp_markets"].str.contains(
        "Характер співробітництва", regex=False
    ).sum()
    assert leaks == 0, (
        f"{leaks} rows contaminated by sub-header 'Характер співробітництва з інвестором'"
    )


def test_no_known_subheader_leaks_at_value_end(df):
    """Catch-all: no data column should end with a known sub-header (the
    classic contamination pattern is `<value> \\n <sub-header>`)."""
    from extract_rd_data import _SUB_HEADER_NAMES, DATA_COLUMNS
    leaks = {}
    for col in DATA_COLUMNS:
        if col == "co_performers":
            continue   # legit structured block, may contain sub-headers
        for sh in _SUB_HEADER_NAMES:
            n = int(df[col].str.endswith(sh).sum())
            if n:
                leaks[(col, sh)] = n
    assert not leaks, f"sub-header leaks detected: {leaks}"


def test_funding_sources_clean_of_foreign_currency_label(df):
    """Reports funded in USD / EUR carry foreign-currency variants of the
    funding-amount label. Verify those variants don't leak — and the amount
    itself doesn't leak — into the end of `funding_sources`."""
    leaks = df["funding_sources"].str.contains(
        r"Фактичний обсяг фінансування \((?:дол|євро)\.\)", regex=True
    ).sum()
    assert leaks == 0, (
        f"{leaks} rows contaminated by foreign-currency funding label"
    )


def test_foreign_currency_funding_amounts_captured(df):
    """USD/EUR funding amounts must populate at least a few rows — the 2017
    batch had ~9 such reports. Both columns combined > 0 catches a regression
    where the parser silently drops them."""
    usd_filled = (df["funding_amount_usd"].str.strip() != "").sum()
    eur_filled = (df["funding_amount_eur"].str.strip() != "").sum()
    assert usd_filled + eur_filled >= 1, (
        f"Foreign-currency funding amounts not captured: usd={usd_filled} eur={eur_filled}"
    )


def test_co_performers_when_populated_is_valid_json(df):
    """Non-empty co_performers cells must be JSON-decodable into a list of dicts."""
    nonempty = df["co_performers"][df["co_performers"].str.strip() != ""]
    assert len(nonempty) > 0, "expected at least a few rows with co_performers"
    for value in nonempty:
        parsed = json.loads(value)
        assert isinstance(parsed, list) and parsed, (
            f"co_performers must be a non-empty list, got: {value[:80]}"
        )
        for record in parsed:
            assert isinstance(record, dict)
            assert "name" in record


def test_langdetect_is_seeded_for_determinism():
    """The pipeline relies on langdetect being seeded so runs are reproducible.
    If the seed accidentally goes unset, borderline rows like row 869's
    `customer_name` (`Компанія \"Huawei Tech. Investment Co., Ltd\"`) flip
    between `mixed`/`en`/`de` on consecutive runs and the CSV stops being
    byte-stable."""
    from langdetect import detect, DetectorFactory  # noqa: F401 — import is the check
    sample = 'Компанія "Huawei Tech. Investment Co., Ltd"'
    runs = {detect(sample) for _ in range(10)}
    assert len(runs) == 1, f"langdetect produced multiple labels {runs}, seed not set"
