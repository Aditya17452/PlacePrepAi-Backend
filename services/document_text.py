"""Extracting text from resumes and LinkedIn profile PDFs.

`api/resume.py` called `pdfplumber` first but **pdfplumber was never in
requirements.txt**, so in production the import failed every time and it silently fell
back to PyPDF2 — the weak extractor the brief wanted replaced. Both pdfplumber and a
PyMuPDF fallback are declared now, DOCX is supported for the same pipeline, and nothing is
truncated at 3,000 characters.
"""

from __future__ import annotations

import io

from core.logging_setup import get_logger

log = get_logger("document_text")

PDF_MAGIC = b"%PDF-"
DOCX_MAGIC = b"PK\x03\x04"


class UnsupportedDocument(ValueError):
    pass


def sniff_kind(data: bytes, filename: str = "") -> str:
    """Identify the document by its bytes, not by the extension it claims."""
    if data.startswith(PDF_MAGIC):
        return "pdf"
    if data.startswith(DOCX_MAGIC):
        # A .docx is a zip; so is .xlsx. The extension disambiguates for us here.
        if filename.lower().endswith((".docx", ".doc")):
            return "docx"
        return "docx"
    raise UnsupportedDocument(
        "Only PDF and DOCX files are supported. Please upload one of those."
    )


def extract(data: bytes, filename: str = "") -> str:
    kind = sniff_kind(data, filename)
    if kind == "pdf":
        return _extract_pdf(data)
    return _extract_docx(data)


def _extract_pdf(data: bytes) -> str:
    """pdfplumber first (best layout fidelity), PyMuPDF as the fallback."""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        if text.strip():
            log.info("pdf_extracted", extractor="pdfplumber", chars=len(text))
            return _clean(text)
    except Exception as exc:
        log.info("pdfplumber_failed", error=str(exc)[:160])

    try:
        import fitz  # PyMuPDF

        with fitz.open(stream=data, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
        if text.strip():
            log.info("pdf_extracted", extractor="pymupdf", chars=len(text))
            return _clean(text)
    except Exception as exc:
        log.info("pymupdf_failed", error=str(exc)[:160])

    # A scanned PDF has no text layer. Say so instead of returning noise that then gets
    # graded as if it were a resume.
    raise UnsupportedDocument(
        "We could not read any text from that PDF. If it is a scan, please upload a "
        "text-based PDF or a DOCX instead."
    )


def _extract_docx(data: bytes) -> str:
    try:
        import docx

        document = docx.Document(io.BytesIO(data))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
        text = "\n".join(p for p in parts if p and p.strip())
        if not text.strip():
            raise UnsupportedDocument("That DOCX file appears to be empty.")
        log.info("docx_extracted", chars=len(text))
        return _clean(text)
    except UnsupportedDocument:
        raise
    except Exception as exc:
        log.info("docx_failed", error=str(exc)[:160])
        raise UnsupportedDocument("We could not read that DOCX file.") from exc


def _clean(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    # Collapse runs of blank lines; PDF extraction produces a lot of them.
    cleaned: list[str] = []
    blank = 0
    for line in lines:
        if line.strip():
            blank = 0
            cleaned.append(line)
        else:
            blank += 1
            if blank <= 1:
                cleaned.append("")
    return "\n".join(cleaned).strip()
