"""Which files a person should look at twice before their contents go into a knowledge base.

A Drive source is reviewed by the folder's owner before anything is read (canopy-web's
inventory review page). Two checks point at likely-sensitive files, and both only HOLD
a file for a person to decide; neither ever admits one.

* `name_flags`, before approval, on metadata alone: the file's and its folders' names
  (raw data, consent, patient, line list, …) and an owner outside the organisation.
* `header_flags`, after a spreadsheet is approved and before it is indexed, on its
  column headers: person names, dates of birth, identifiers and contact details. A
  match holds the file for a second look.

False positives are cheap (a click) and false negatives are not, so the patterns are
broad on purpose.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

#: Words in a file or folder name that suggest records about people.
NAME_WORDS = (
    r"raw[\s_-]*data", r"consent", r"patients?", r"line[\s_-]*lists?", r"linelist", r"participants?",
    r"beneficiar(?:y|ies)", r"respondents?", r"enrol+ment", r"registers?", r"rosters?", r"contact[\s_-]*list",
    r"household[\s_-]*list", r"case[\s_-]*list", r"medical[\s_-]*records?", r"health[\s_-]*records?",
    r"\bphi\b", r"\bpii\b", r"identifiable", r"de-?identif", r"\bhiv\b", r"names?[\s_-]*list", r"\bdob\b",
)
_NAME_RX = re.compile("|".join(NAME_WORDS), re.I)

#: Column headers that name a person, their birth date, an identifier or contact details.
_HEADER_RX = re.compile("|".join((
    r"^(?:first|last|given|family|middle|full|sur|maiden)[\s_-]*name$", r"^surname$", r"^name$",
    r"(?:patient|mother|father|child|baby|infant|client|participant|beneficiary|caregiver|guardian|"
    r"respondent|person|member|head|husband|wife)(?:'s)?[\s_-]*(?:full[\s_-]*)?name",
    r"date[\s_-]*of[\s_-]*birth", r"birth[\s_-]*date", r"^dob$", r"\bd\.?o\.?b\.?\b",
    r"\b(?:national|patient|hospital|medical|health|client|participant|beneficiary|card|voter|tax|social)"
    r"[\s_-]*(?:id|no|number|#)\b", r"\b(?:mrn|nin|nid|ssn|passport|aadhaar|nhs[\s_-]*(?:no|number))\b",
    r"\b(?:phone|mobile|cell|telephone|msisdn|whatsapp)\b", r"\b(?:e-?mail)\b", r"\b(?:home[\s_-]*)?address\b",
    r"\b(?:gps|latitude|longitude|geo[\s_-]*location)\b",
)), re.I)


def name_flags(name: str, folders: list[str] | tuple[str, ...] = (), owner: str = "",
               internal_domains: tuple[str, ...] = ()) -> list[str]:
    """Why a file's metadata alone suggests a second look; empty when it does not."""
    out = []
    if _NAME_RX.search(name or ""):
        out.append(f"name: {name!r} suggests records about people")
    for f in folders:
        if _NAME_RX.search(f or ""):
            out.append(f"folder: {f!r} suggests records about people")
            break
    domain = (owner or "").rsplit("@", 1)[-1].lower() if "@" in (owner or "") else ""
    if internal_domains and domain and domain not in {d.lower() for d in internal_domains}:
        out.append(f"owner: {owner} is outside the organisation")
    return out


def header_flags(headers: list[str]) -> list[str]:
    """The column headers that look like personal data."""
    return [h for h in dict.fromkeys(str(h).strip() for h in headers if h is not None) if h and _HEADER_RX.search(h)]


def spreadsheet_headers(path: Path, kind: str, max_rows: int = 5) -> list[str]:
    """Every sheet's header candidates: the first `max_rows` non-empty rows' cells (a title
    row often sits above the real header). `kind` is xlsx, xls or csv."""
    path = Path(path)
    rows: list[list] = []
    if kind == "csv":
        text = path.read_bytes().decode("utf-8", "replace")
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)][:max_rows]
    elif kind in ("xlsx", "xls"):
        if kind == "xls":
            import xlrd
            book = xlrd.open_workbook(file_contents=path.read_bytes())
            for sh in book.sheets():
                got = [sh.row_values(r) for r in range(sh.nrows) if any(str(v).strip() for v in sh.row_values(r))]
                rows += got[:max_rows]
        else:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            for ws in wb.worksheets:
                got = 0
                for r in ws.iter_rows(values_only=True):
                    if any(v is not None and str(v).strip() for v in r):
                        rows.append(list(r))
                        got += 1
                        if got >= max_rows:
                            break
            wb.close()
    return [str(c) for r in rows for c in r if c is not None and str(c).strip()]
