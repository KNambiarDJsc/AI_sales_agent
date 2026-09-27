"""Lead ingestion pipeline (Section 14): upload -> parse -> validate -> normalize
phone -> E.164 -> deduplicate -> flag consent -> ready for campaign/queue.

Column names are NOT assumed. Callers must supply a `column_mapping` (or accept
`guess_column_mapping`'s best-effort guess and let a human confirm it before import —
never silently trust a guess for a real campaign). Cross-file suppression checking
(against database/models.Suppression) happens at the campaign-service layer, since
that needs a DB session and tenant scope; this module only handles what's knowable
from the file alone.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import pandas as pd
import phonenumbers
from phonenumbers import NumberParseException

# Best-effort header aliases for guess_column_mapping — a guess, not a guarantee.
# Real campaigns must have a human confirm the mapping before import.
_HEADER_ALIASES: dict[str, list[str]] = {
    "phone": ["phone", "phone number", "mobile", "contact number", "phone_number", "mobile number"],
    "contact_name": ["name", "contact name", "contact", "customer name"],
    "business_name": ["business name", "company", "company name", "business", "organisation", "organization"],
    "consent_flag": ["consent", "opt in", "opt-in", "opted_in"],
}


@dataclass
class ParsedLeadRow:
    phone_e164: str
    raw_phone: str
    dedupe_key: str
    contact_name: str | None
    business_name: str | None
    consent_flag: bool | None
    extra: dict[str, Any]
    source_row: dict[str, Any]


@dataclass
class RowError:
    row_index: int
    reason: str


@dataclass
class ImportReport:
    total_rows: int = 0
    valid_rows: int = 0
    duplicate_within_file: int = 0
    errors: list[RowError] = field(default_factory=list)


def guess_column_mapping(columns: list[str]) -> dict[str, str]:
    normalized = {c.strip().lower(): c for c in columns}
    mapping: dict[str, str] = {}
    for field_name, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field_name] = normalized[alias]
                break
    return mapping


def _read_table(file_obj: BinaryIO, filename: str) -> pd.DataFrame:
    if filename.lower().endswith(".xlsx"):
        return pd.read_excel(file_obj, dtype=str)
    return pd.read_csv(file_obj, dtype=str)


def _normalize_phone(raw: str, default_region: str) -> str | None:
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _parse_consent(value: Any) -> bool | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().lower()
    if text in ("yes", "y", "true", "1", "opted_in", "opt-in", "opted-in"):
        return True
    if text in ("no", "n", "false", "0", "opted_out", "opt-out", "opted-out"):
        return False
    return None


def import_leads(
    file_bytes: bytes,
    filename: str,
    column_mapping: dict[str, str],
    default_region: str = "IN",
) -> tuple[list[ParsedLeadRow], ImportReport]:
    if "phone" not in column_mapping:
        raise ValueError("column_mapping must include a 'phone' column mapping")

    df = _read_table(io.BytesIO(file_bytes), filename)
    report = ImportReport(total_rows=len(df))
    rows: list[ParsedLeadRow] = []
    seen_dedupe_keys: set[str] = set()

    phone_col = column_mapping["phone"]
    contact_col = column_mapping.get("contact_name")
    business_col = column_mapping.get("business_name")
    consent_col = column_mapping.get("consent_flag")
    mapped_cols = {c for c in (phone_col, contact_col, business_col, consent_col) if c}

    for idx, row in df.iterrows():
        raw_phone = str(row.get(phone_col, "") or "").strip()
        if not raw_phone:
            report.errors.append(RowError(row_index=int(idx), reason="Missing phone number"))
            continue

        phone_e164 = _normalize_phone(raw_phone, default_region)
        if phone_e164 is None:
            report.errors.append(RowError(row_index=int(idx), reason=f"Could not parse phone number: {raw_phone!r}"))
            continue

        dedupe_key = phone_e164
        if dedupe_key in seen_dedupe_keys:
            report.duplicate_within_file += 1
            continue
        seen_dedupe_keys.add(dedupe_key)

        extra = {col: row[col] for col in df.columns if col not in mapped_cols and pd.notna(row[col])}
        source_row = {col: (None if pd.isna(row[col]) else row[col]) for col in df.columns}

        rows.append(
            ParsedLeadRow(
                phone_e164=phone_e164,
                raw_phone=raw_phone,
                dedupe_key=dedupe_key,
                contact_name=str(row[contact_col]).strip() if contact_col and pd.notna(row.get(contact_col)) else None,
                business_name=str(row[business_col]).strip() if business_col and pd.notna(row.get(business_col)) else None,
                consent_flag=_parse_consent(row.get(consent_col)) if consent_col else None,
                extra=extra,
                source_row=source_row,
            )
        )

    report.valid_rows = len(rows)
    return rows, report
