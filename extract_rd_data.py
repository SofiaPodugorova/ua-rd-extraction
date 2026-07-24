"""Extract Ukrainian R&D project report PDFs (UKRISTEI archive) into a single
CSV per year batch for downstream NLP analysis. Defaults target the 2017
batch; later batches are processed via --data-dir/--output/--log/--year.
Each PDF is a bilingual form with ten Roman-numeral sections; PyMuPDF's HTML
mode is used because the embedded Lora font breaks plain-text extraction."""

import argparse
import html
import json
import logging
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
import pandas as pd
from langdetect import detect, DetectorFactory, LangDetectException

# Seed langdetect so consecutive runs produce identical CSV bytes.
DetectorFactory.seed = 0


# --- Configuration ---

RAW_DATA_DIR = Path("data/raw_2017")
OUTPUT_CSV   = Path("data/output_2017.csv")
LOG_FILE     = Path("data/extraction_log.txt")
YEAR         = "2017"

# Empty value in any of these fields triggers a WARN log entry.
CRITICAL_FIELDS = [
    "registration_number", "title_uk", "title_en",
    "abstract_uk", "abstract_en",
    "pi_name", "performer_name",
]


# --- Logging ---

def setup_logging(log_path: Path) -> logging.Logger:
    """Build a logger that writes DEBUG+ to file and INFO+ to stdout."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("rd_extractor")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# --- PDF text extraction ---

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE  = re.compile(r"[ \t]{2,}")


def page_to_text(page: fitz.Page) -> str:
    """Convert one PDF page to clean Unicode via HTML extraction."""
    # HTML mode encodes non-standard-font characters as numeric entities, which
    # html.unescape() correctly converts back to Unicode (plain `text` mode
    # garbles Cyrillic because of the embedded Lora font).
    raw_html = page.get_text("html")
    text = html.unescape(_TAG_RE.sub(" ", raw_html))
    text = text.replace("\xa0", " ")
    return _WS_RE.sub(" ", text).strip()


def pdf_to_text(pdf_path: Path) -> str:
    """Concatenate all pages of a PDF into one newline-separated string."""
    doc = fitz.open(str(pdf_path))
    try:
        pages = [page_to_text(page) for page in doc]
    finally:
        doc.close()
    return "\n".join(pages)


# --- Section splitting ---

# Lookahead on a capital Cyrillic letter rejects false matches against author
# initials in the bibliography (e.g. "I. Petrov" would otherwise match).
_SECTION_HEADER_RE = re.compile(
    r"(?m)^\s*(X|IX|VIII|VII|VI|V|IV|III|II|I)\.\s+(?=[А-ЯІЄЇ])"
)

_ROMAN_INDEX = {n: i for i, n in enumerate(
    ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"])}


def _drop_false_headers(matches: list) -> list:
    """Keep only the longest strictly-increasing run of section numerals.

    Real headers always appear in document order I → X. A bibliography line can
    still slip past the capital-Cyrillic lookahead when an author initial is a
    Cyrillic homoglyph (e.g. "V. Оrobej …" with Cyrillic О), and such a match
    overwrites a real section. False matches break the I → X monotonicity, so
    the longest strictly-increasing subsequence keeps exactly the real headers.
    Ties prefer the later match (the closing "X. Заключні відомості" is always
    the last header in the document).
    """
    n = len(matches)
    if n <= 1:
        return matches
    idx = [_ROMAN_INDEX[m.group(1)] for m in matches]
    best = [1] * n   # length of the longest increasing run ending at i
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if idx[j] < idx[i] and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
                prev[i] = j
    end = max(range(n), key=lambda i: (best[i], i))
    chain = []
    while end != -1:
        chain.append(matches[end])
        end = prev[end]
    return chain[::-1]


def split_sections(text: str) -> dict[str, str]:
    """Split a document into 10 sections by Roman-numeral headers."""
    sections: dict[str, str] = {n: "" for n in ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]}

    matches = _drop_false_headers(list(_SECTION_HEADER_RE.finditer(text)))
    for i, m in enumerate(matches):
        numeral = m.group(1)
        start   = m.end()
        end     = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        # Drop the section title line (first line, never contains a colon).
        first_nl = section_text.find("\n")
        if first_nl == -1:
            if ":" not in section_text:
                section_text = ""
        elif ":" not in section_text[:first_nl]:
            section_text = section_text[first_nl + 1:].lstrip()
        sections[numeral] = section_text

    return sections


# --- Field extraction helpers ---

# Whitelist of "Label:" markers that bound a field value. A heuristic
# (`[А-ЯІЄЇ][^\n:\d]{1,60}:`) would silently truncate abstracts on phrases
# like "Об'єкт дослідження:" or "Мета роботи:" — must be explicit.
_LABEL_NAMES_WITH_COLON = [
    # Section I
    "Державний обліковий номер", "Особливі позначки", "Дата реєстрації",
    # Section II
    "Номер етапу", "Назва етапу", "Початок етапу", "Закінчення етапу",
    "Вид звітного документа",
    # Section III + V (shared sub-labels)
    "Повне найменування юридичної особи (або ПІБ фізичної особи)",
    "Повне найменування юридичної особи", "Код за ЄДРПОУ",
    "Місцезнаходження", "Форма власності", "Сфера управління",
    "Ідентифікатор ROR", "Розмір організації", "Телефон",
    # Section VI (canonical + foreign-currency variants)
    "Підстава для проведення ДіР", "Напрям фінансування",
    "Код програмної класифікації видатків і кредитування (КПКВК)",
    "Фактичний обсяг фінансування (тис. грн.)",
    "Фактичний обсяг фінансування (дол.)",
    "Фактичний обсяг фінансування (євро.)",
    # Section VII
    "Назва роботи українською", "Назва роботи англійською",
    "Реферат українською", "Реферат англійською",
    "Індекс УДК", "Коди тематичних рубрик",
    "Власне Прізвище Ім'я По-батькові",
    "Науковий ступінь", "Наукове звання",
    "Ідентифікатор ORCID ID", "Ідентифікатор ORCHID ID",
    "Додаткова інформація",
    # Section VIII
    "Назва НТП українською", "Назва НТП англійською",
    "НТП, яку передбачалося створити",
    "Причини, через які НТП не було створено",
    "Отримані результати", "Галузь застосування",
    "Реєстраційний номер картки технології", "Опис НТП",
    "Соціально-економічна спрямованість НТП", "Вплив НТП на довкілля",
    "Впровадження НТП", "Споживачі продукції", "Перспективні ринки",
    "Потрібний обсяг інвестицій, тис. грн.",
    "Права, що надаються інвестору після завершення роботи",
    "Наявність бізнес-плану", "Техніко-економічне обгрунтування",
    "Потенціальний обсяг продажу, тис. грн.",
    "Очікуваний термін окупності (років)",
]

# Standalone heading lines (no trailing colon) that also bound a field.
# Without them the multiline parser silently slurps the next block's content
# into the previous field.
_SUB_HEADER_NAMES = [
    "Джерела фінансування",
    "Керівники роботи",
    "Практична реалізація НТП",
    "Характер співробітництва з інвестором",
]


def _escape_label(name: str) -> str:
    """Escape a label for regex, allowing arbitrary whitespace between words."""
    return r"[ \t]+".join(re.escape(w) for w in name.split())


# Combined boundary: a "Label:" OR a standalone sub-header line. Longest
# variants first to avoid prefix matches.
_NEXT_LABEL = (
    r"(?:"
    + r"(?:"
    + r"|".join(_escape_label(n) for n in sorted(_LABEL_NAMES_WITH_COLON, key=len, reverse=True))
    + r")[ \t]*:"
    + r"|"
    + r"(?:"
    + r"|".join(_escape_label(n) for n in sorted(_SUB_HEADER_NAMES, key=len, reverse=True))
    + r")[ \t]*(?=\n)"
    + r")"
)


_FIELD_PATTERN_CACHE: dict[tuple[str, bool], "re.Pattern[str]"] = {}


def _compile_field_pattern(label: str, multiline: bool) -> "re.Pattern[str]":
    """Build (and cache) a regex extracting the value after a specific label."""
    key = (label, multiline)
    cached = _FIELD_PATTERN_CACHE.get(key)
    if cached is not None:
        return cached
    escaped = _escape_label(label)
    if multiline:
        # `(?![ \t]*<label>)` must come BEFORE consuming whitespace — otherwise
        # the engine backtracks `[ \t]*` to zero chars and the negative
        # lookahead matches against the leading space, swallowing the whole
        # next block.
        src = (
            escaped
            + r"[ \t]*:?[ \t]*"
            + rf"(?:\n(?![ \t]*{_NEXT_LABEL})[ \t]*)?"
            + r"(.*?)"
            + rf"(?=\n[ \t]*{_NEXT_LABEL}|\Z)"
        )
        compiled = re.compile(src, re.DOTALL)
    else:
        compiled = re.compile(escaped + r"[ \t]*:?[ \t]*([^\n]*)")
    _FIELD_PATTERN_CACHE[key] = compiled
    return compiled


def _field(text: str, label: str, multiline: bool = False) -> str:
    """Return the value of `label: …` from `text`; "" if not found."""
    m = _compile_field_pattern(label, multiline).search(text)
    if not m:
        return ""
    value = m.group(1).strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value


def _field_any(text: str, labels: list[str], multiline: bool = False) -> str:
    """Return the first populated value among spelling/layout label variants."""
    for label in labels:
        value = _field(text, label, multiline)
        if value:
            return value
    return ""


# --- Per-section extractors ---

def parse_section_i(text: str) -> dict:
    """Section I — general info: registration number, date, special marks."""
    return {
        "registration_number": _field(text, "Державний обліковий номер"),
        "special_marks":       _field(text, "Особливі позначки"),
        "registration_date":   _field(text, "Дата реєстрації"),
    }


def parse_section_ii(text: str) -> dict:
    """Section II — work stage: number, title, start/end, document type."""
    return {
        "stage_number": _field(text, "Номер етапу"),
        "stage_title":  _field(text, "Назва етапу", multiline=True),
        "stage_start":  _field(text, "Початок етапу"),
        "stage_end":    _field(text, "Закінчення етапу"),
        "report_type":  _field(text, "Вид звітного документа"),
    }


def parse_section_iii(text: str) -> dict:
    """Section III — performer details: name, EDRPOU, address, ministry, phone."""
    return {
        "performer_name":     _field_any(
            text,
            [
                "Повне найменування юридичної особи (або ПІБ фізичної особи)",
                "Повне найменування юридичної особи",
            ],
            multiline=True,
        ),
        "performer_edrpou":   _field(text, "Код за ЄДРПОУ"),
        "performer_location": _field(text, "Місцезнаходження", multiline=True),
        "performer_ministry": _field(text, "Сфера управління", multiline=True),
        "performer_phone":    _field(text, "Телефон"),
    }


# Labels recognised inside a single co-performer block. Order matches
# Section III; first hit per line wins. Used to parse the raw block into a
# JSON-serialisable list of dicts.
_CO_PERFORMER_LABELS = [
    ("name",         "Повне найменування юридичної особи (або ПІБ фізичної особи)"),
    ("edrpou",       "Код за ЄДРПОУ"),
    ("location",     "Місцезнаходження"),
    ("ownership",    "Форма власності"),
    ("ministry",     "Сфера управління"),
    ("ror",          "Ідентифікатор ROR"),
    ("size",         "Розмір організації"),
    ("phone",        "Телефон"),
    ("contribution", "Внесок співвиконавця у звітний етап"),
]


def _parse_co_performers_block(body: str) -> str:
    """Parse the raw co-performers block into a JSON-encoded list of dicts.

    Empty cell → "". Returns the original text if no label could be matched
    (so we never silently drop data on an unexpected layout).
    """
    body = body.strip()
    if not body:
        return ""

    # Split into per-co-performer chunks on the canonical opener label.
    opener = "Повне найменування юридичної особи (або ПІБ фізичної особи)"
    if opener in body:
        chunks = re.split(rf"(?=^\s*{re.escape(opener)}\b)", body, flags=re.MULTILINE)
        chunks = [c.strip() for c in chunks if c.strip()]
    else:
        chunks = [body]

    records: list[dict] = []
    for chunk in chunks:
        record: dict[str, str] = {}
        for key, label in _CO_PERFORMER_LABELS:
            v = _field(chunk, label, multiline=False)
            if v:
                record[key] = v
        if record:
            records.append(record)

    if not records:
        return body  # keep raw text rather than silently lose it
    return json.dumps(records, ensure_ascii=False)


def parse_section_iv(text: str) -> dict:
    """Section IV — co-performers: parsed to JSON list of dicts when populated."""
    body = text.strip()
    if ":" not in body:
        return {"co_performers": ""}
    return {"co_performers": _parse_co_performers_block(body)}


def parse_section_v(text: str) -> dict:
    """Section V — customer details (same structure as the performer)."""
    return {
        "customer_name":     _field_any(
            text,
            [
                "Повне найменування юридичної особи (або ПІБ фізичної особи)",
                "Повне найменування юридичної особи",
            ],
            multiline=True,
        ),
        "customer_edrpou":   _field(text, "Код за ЄДРПОУ"),
        "customer_location": _field(text, "Місцезнаходження", multiline=True),
        "customer_ministry": _field(text, "Сфера управління", multiline=True),
        "customer_phone":    _field(text, "Телефон"),
    }


def parse_section_vi(text: str) -> dict:
    """Section VI — funding: legal basis, direction, sources, budget code, amounts (UAH/USD/EUR)."""
    return {
        "legal_basis":         _field(text, "Підстава для проведення ДіР", multiline=True),
        "funding_direction":   _field(text, "Напрям фінансування", multiline=True),
        "funding_sources":     _field(text, "Джерела фінансування", multiline=True),
        "budget_code":         _field(text, "Код програмної класифікації видатків і кредитування (КПКВК)"),
        "funding_amount_kgrn": _field(text, "Фактичний обсяг фінансування (тис. грн.)"),
        "funding_amount_usd":  _field(text, "Фактичний обсяг фінансування (дол.)"),
        "funding_amount_eur":  _field(text, "Фактичний обсяг фінансування (євро.)"),
    }


def parse_section_vii(text: str) -> dict:
    """Section VII — topic, abstract, PI: titles/abstracts UK+EN, UDC, PI details."""
    return {
        "title_uk":        _field(text, "Назва роботи українською", multiline=True),
        "title_en":        _field(text, "Назва роботи англійською", multiline=True),
        "abstract_uk":     _field(text, "Реферат українською", multiline=True),
        "abstract_en":     _field(text, "Реферат англійською", multiline=True),
        "udc_index":       _field(text, "Індекс УДК"),
        "thematic_codes":  _field(text, "Коди тематичних рубрик"),
        "pi_name":         _field(text, "Власне Прізвище Ім'я По-батькові", multiline=True),
        "pi_degree":       _field(text, "Науковий ступінь"),
        "pi_title":        _field(text, "Наукове звання"),
        "pi_orcid":        _field_any(
            text,
            ["Ідентифікатор ORCID ID", "Ідентифікатор ORCHID ID"],
        ),
        "additional_info": _field(text, "Додаткова інформація", multiline=True),
    }


def parse_section_viii(text: str) -> dict:
    """Section VIII — scientific & technical output (NTP): description, results, investment block."""
    return {
        "ntp_title_uk":              _field(text, "Назва НТП українською", multiline=True),
        "ntp_title_en":              _field(text, "Назва НТП англійською", multiline=True),
        "ntp_planned":               _field(text, "НТП, яку передбачалося створити", multiline=True),
        "ntp_failure_reasons":       _field(text, "Причини, через які НТП не було створено", multiline=True),
        "ntp_results":               _field(text, "Отримані результати", multiline=True),
        "ntp_application_field":     _field(text, "Галузь застосування", multiline=True),
        "ntp_card_number":           _field(text, "Реєстраційний номер картки технології"),
        "ntp_description":           _field(text, "Опис НТП", multiline=True),
        "ntp_socioeconomic":         _field(text, "Соціально-економічна спрямованість НТП", multiline=True),
        "ntp_environment":           _field(text, "Вплив НТП на довкілля", multiline=True),
        "ntp_implementation":        _field(text, "Впровадження НТП"),
        "ntp_consumers":             _field(text, "Споживачі продукції", multiline=True),
        "ntp_markets":               _field(text, "Перспективні ринки", multiline=True),
        "ntp_invest_amount_kgrn":    _field(text, "Потрібний обсяг інвестицій, тис. грн."),
        "ntp_investor_rights":       _field(text, "Права, що надаються інвестору після завершення роботи", multiline=True),
        "ntp_business_plan":         _field(text, "Наявність бізнес-плану", multiline=True),
        "ntp_techno_economic_basis": _field(text, "Техніко-економічне обгрунтування", multiline=True),
        "ntp_sales_amount_kgrn":     _field(text, "Потенціальний обсяг продажу, тис. грн."),
        "ntp_payback_years":         _field(text, "Очікуваний термін окупності (років)"),
    }


def parse_section_ix(text: str) -> dict:
    """Section IX — bibliography: stored as one block."""
    return {"bibliography": text.strip()}


def parse_section_x(text: str) -> dict:
    """Section X — final info: organisation head and list of executors.

    The sub-headers ("Керівник юридичної особи", "Перелік осіб-виконавців",
    "Відповідальний за підготовку…") lack colons, so we slice by anchors
    instead of calling `_field`. The UkrINTEI bureaucratic footer is dropped.
    """
    m_head = re.search(r"Керівник[ \t]+юридичної[ \t]+особи[ \t]*\n", text)
    m_exec = re.search(r"Перелік[ \t]+осіб-виконавців[ \t]*\n", text)
    m_foot = re.search(r"Відповідальний[ \t]+за[ \t]+підготовку", text)

    org_head = ""
    if m_head:
        end_head = (
            m_exec.start() if m_exec
            else m_foot.start() if m_foot
            else len(text)
        )
        block = text[m_head.end():end_head]
        for ln in block.splitlines():
            ln = ln.strip()
            if ln:
                org_head = ln
                break

    executors = ""
    if m_exec:
        end_exec = m_foot.start() if m_foot else len(text)
        block = text[m_exec.end():end_exec]
        lines = [ln.strip() for ln in block.splitlines()]
        lines = [ln for ln in lines if len(ln) >= 3]
        executors = "\n".join(lines)

    return {"org_head": org_head, "executors": executors}


# --- Language detection ---

# Text fields that get per-field language detection. Numeric / identifier
# fields (EDRPOU, phones, dates, codes, ORCID, amounts) are excluded —
# language detection on them is meaningless.
TEXT_FIELDS = [
    "stage_title", "special_marks", "report_type",
    "performer_name", "performer_location", "performer_ministry",
    "co_performers",
    "customer_name", "customer_location", "customer_ministry",
    "legal_basis", "funding_direction", "funding_sources",
    "title_uk", "title_en", "abstract_uk", "abstract_en",
    "pi_name", "pi_degree", "pi_title", "additional_info",
    "ntp_title_uk", "ntp_title_en",
    "ntp_planned", "ntp_failure_reasons",
    "ntp_results", "ntp_application_field",
    "ntp_description", "ntp_socioeconomic", "ntp_environment",
    "ntp_implementation", "ntp_consumers", "ntp_markets",
    "ntp_investor_rights", "ntp_business_plan", "ntp_techno_economic_basis",
    "bibliography",
    "org_head", "executors",
]

# Pooled text of these fields determines the row-level `language` column.
DOMINANT_LANG_FIELDS = [
    "title_uk", "abstract_uk", "stage_title",
    "performer_name", "customer_name", "ntp_description",
]


def detect_field_language(text: str) -> str:
    """Detect the language of a field: uk / en / mixed / empty / unknown."""
    if not text or not text.strip():
        return "empty"

    cyr = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    lat = sum(1 for ch in text if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    total = cyr + lat
    if total < 3:
        return "unknown"

    ratio = cyr / total
    if ratio >= 0.85:
        return "uk"
    if ratio <= 0.15:
        return "en"
    if total >= 40:
        return "mixed"

    # Borderline short text — ask langdetect; accept only uk/en (otherwise
    # langdetect frequently misfires to sibling Slavic languages on short
    # Cyrillic strings, which we collapse to `mixed`).
    try:
        lang = detect(text)
    except LangDetectException:
        return "unknown"
    if lang == "uk":
        return "uk"
    if lang == "en":
        return "en"
    return "mixed"


def detect_row_language(record: dict) -> str:
    """Detect the language of a whole record from its principal text fields."""
    sample = " ".join(record.get(f, "") or "" for f in DOMINANT_LANG_FIELDS).strip()
    if not sample:
        return "unknown"
    return detect_field_language(sample)


# --- Output column ordering ---

SYSTEM_COLUMNS = ["source_file", "year", "language"]

DATA_COLUMNS = [
    # Section I
    "registration_number", "registration_date", "special_marks",
    # Section II
    "stage_number", "stage_title", "stage_start", "stage_end", "report_type",
    # Section III
    "performer_name", "performer_edrpou", "performer_location", "performer_ministry", "performer_phone",
    # Section IV
    "co_performers",
    # Section V
    "customer_name", "customer_edrpou", "customer_location", "customer_ministry", "customer_phone",
    # Section VI
    "legal_basis", "funding_direction", "funding_sources", "budget_code",
    "funding_amount_kgrn", "funding_amount_usd", "funding_amount_eur",
    # Section VII
    "title_uk", "title_en", "abstract_uk", "abstract_en",
    "udc_index", "thematic_codes",
    "pi_name", "pi_degree", "pi_title", "pi_orcid", "additional_info",
    # Section VIII
    "ntp_title_uk", "ntp_title_en",
    "ntp_planned", "ntp_failure_reasons",
    "ntp_results", "ntp_application_field",
    "ntp_card_number",
    "ntp_description", "ntp_socioeconomic", "ntp_environment",
    "ntp_implementation", "ntp_consumers", "ntp_markets",
    "ntp_invest_amount_kgrn", "ntp_investor_rights",
    "ntp_business_plan", "ntp_techno_economic_basis",
    "ntp_sales_amount_kgrn", "ntp_payback_years",
    # Section IX
    "bibliography",
    # Section X
    "org_head", "executors",
]

CSV_COLUMNS = SYSTEM_COLUMNS + DATA_COLUMNS + [f"lang_{f}" for f in TEXT_FIELDS]


# --- File discovery ---

def collect_files(root: Path) -> list[Path]:
    """Return every source PDF in deterministic path order.

    The corrected NRAT dataset may contain multiple, textually distinct PDF
    versions with the same registration number. The hash suffix in each
    filename identifies the source file, so registration number alone must not
    be used for deduplication.
    """
    return sorted(root.rglob("*.pdf"))


# --- Single-file extractor ---

def extract_record(pdf_path: Path, logger: logging.Logger, year: str) -> tuple[dict, bool] | None:
    """Process one PDF into a record. Returns (record, has_warn) or None on error.

    `has_warn=True` iff any CRITICAL_FIELDS came back empty.
    """
    try:
        full_text = pdf_to_text(pdf_path)
    except Exception as exc:
        logger.error("FAIL %s — cannot open PDF: %s", pdf_path.name, exc)
        return None

    sections = split_sections(full_text)

    record: dict = {}
    try:
        record.update(parse_section_i(sections["I"]))
        record.update(parse_section_ii(sections["II"]))
        record.update(parse_section_iii(sections["III"]))
        record.update(parse_section_iv(sections["IV"]))
        record.update(parse_section_v(sections["V"]))
        record.update(parse_section_vi(sections["VI"]))
        record.update(parse_section_vii(sections["VII"]))
        record.update(parse_section_viii(sections["VIII"]))
        record.update(parse_section_ix(sections["IX"]))
        record.update(parse_section_x(sections["X"]))
    except Exception as exc:
        logger.error("FAIL %s — parsing error: %s", pdf_path.name, exc)
        return None

    empty_critical = [f for f in CRITICAL_FIELDS if not record.get(f)]
    if empty_critical:
        logger.warning("WARN %s — empty critical fields: %s",
                       pdf_path.name, ", ".join(empty_critical))
    empty_all = [f for f in DATA_COLUMNS if not (record.get(f) or "").strip()]
    if empty_all:
        logger.debug("EMPTY %s — %s", pdf_path.name, ", ".join(empty_all))

    for field in TEXT_FIELDS:
        record[f"lang_{field}"] = detect_field_language(record.get(field, "") or "")
    record["language"]    = detect_row_language(record)
    record["source_file"] = pdf_path.name
    record["year"]        = year

    logger.debug("DONE %s", pdf_path.name)
    return record, bool(empty_critical)


# --- Main ---

def main():
    """CLI entry point: process PDFs in --data-dir and write CSV to --output."""
    parser = argparse.ArgumentParser(description="Extract Ukrainian R&D PDFs to CSV")
    parser.add_argument("--sample", type=int, default=0,
                        help="Process only N evenly-spaced files (0 = full dataset)")
    parser.add_argument("--data-dir", type=Path, default=RAW_DATA_DIR,
                        help="Root directory containing month subdirectories")
    parser.add_argument("--output", type=Path, default=OUTPUT_CSV, help="Output CSV path")
    parser.add_argument("--log", type=Path, default=LOG_FILE, help="Log file path")
    parser.add_argument("--year", type=str, default=YEAR,
                        help="Value for the `year` column (e.g. 2018, 2020)")
    args = parser.parse_args()

    logger = setup_logging(args.log)
    logger.info("Starting extraction from %s", args.data_dir)

    files = collect_files(args.data_dir)
    logger.info("Found %d source PDFs", len(files))

    if args.sample:
        step  = max(1, len(files) // args.sample)
        files = files[::step][: args.sample]
        logger.info("Sample mode: processing %d files", len(files))

    records, n_ok, n_warn, n_fail = [], 0, 0, 0
    for i, pdf_path in enumerate(files, 1):
        if i % 100 == 0 or i == len(files):
            logger.info("Progress: %d / %d", i, len(files))
        result = extract_record(pdf_path, logger, args.year)
        if result is None:
            n_fail += 1
            continue
        record, has_warn = result
        if has_warn:
            n_warn += 1
        else:
            n_ok += 1
        records.append(record)

    df = pd.DataFrame(records)
    for col in CSV_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[CSV_COLUMNS]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False, encoding="utf-8-sig")

    logger.info("Summary -- files: %d  OK: %d  WARN: %d  FAIL: %d  -> %s (%d rows, %d cols)",
                len(files), n_ok, n_warn, n_fail, args.output, len(df), len(df.columns))


if __name__ == "__main__":
    main()
