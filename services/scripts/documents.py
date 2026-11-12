"""Text out of the file a client uploads as their call script: .txt / .md, Word
(.docx), PDF, or .rtf. Nothing here is sent anywhere; it only reads the bytes."""
from __future__ import annotations

import io
import re

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
SUPPORTED = (".txt", ".md", ".docx", ".pdf", ".rtf")


class ScriptFileError(ValueError):
    """The file can't be used; the message is shown to the client as-is."""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _docx(data: bytes) -> str:
    import docx  # python-docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same message
        raise ScriptFileError("This Word file couldn't be opened. Save it again as .docx and retry.") from exc
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(dict.fromkeys(cells)))  # merged cells repeat their text
    return "\n".join(parts)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ScriptFileError("This PDF is password-protected. Remove the password and upload it again.")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except ScriptFileError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ScriptFileError("This PDF couldn't be read. Try exporting it again, or paste the script as text.") from exc
    if len(text.strip()) < 20:
        raise ScriptFileError(
            "No text found in this PDF — it looks like a scan or a photo. Upload a typed PDF or Word file, "
            "or paste the script as text."
        )
    return text


def _rtf(data: bytes) -> str:
    text = _decode(data)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", "", text)
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), text)
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\{\\\*?\\(?:fonttbl|colortbl|stylesheet|info|pict)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", "", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    return text.replace("{", "").replace("}", "")


def tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower().strip()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ScriptFileError("The file is too large (max 5 MB).")
    if not data:
        raise ScriptFileError("The file is empty.")
    if name.endswith(".doc"):
        raise ScriptFileError("Old Word files (.doc) aren't supported. In Word choose Save As → .docx, then upload that.")
    if name.endswith(".docx"):
        text = _docx(data)
    elif name.endswith(".pdf"):
        text = _pdf(data)
    elif name.endswith(".rtf"):
        text = _rtf(data)
    elif name.endswith((".txt", ".md")) or "." not in name:
        text = _decode(data)
    else:
        raise ScriptFileError("Upload a .txt, .docx (Word) or .pdf file — or paste the script as text.")
    text = tidy(text)
    if len(text) < 20:
        raise ScriptFileError("The file has almost no text in it. Check that it is the right file.")
    return text
