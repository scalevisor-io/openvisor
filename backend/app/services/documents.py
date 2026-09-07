"""§chat documents: turning an uploaded document into text the model can read.

Images reach the model as pixels and therefore need a model that reads pixels
(services/vision). Documents take the other road on purpose: the text is
extracted HERE, once, at upload, and stored next to the bytes - so a PDF, a
Word file, a Markdown or HTML page reaches ANY model as plain text, vision or
not, and the worker never re-parses a document per answer.

Sniffing trusts the bytes, never the client's content-type header, like
services/images: `%PDF-` is a PDF, a zip holding word/document.xml is a .docx,
and anything else must decode as text without a NUL byte in it - then the
filename and a peek at the head decide between HTML (tags stripped), Markdown,
CSV, JSON and plain text. A scanned PDF (no text layer) and an encrypted one are
refused with the reason: nothing the model could read would be stored, and a
silent empty attachment is worse than a 422.

Everything the parsers do is bounded: pages, characters, the size of the XML a
.docx may unpack to - a document is customer-supplied data and a parser is an
attack surface.
"""
from __future__ import annotations

import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import PurePosixPath
from xml.etree import ElementTree

# §chat documents switch: one admin-level kill switch for the whole feature
# (upload, the model reading attached text, sandbox staging), stored like the
# routines switch as an AppSetting flag - absent means ON. Two readers, async for
# the API and sync for the workers, so the composer, the upload route and the
# answer paths can never disagree about whether documents are on.
DISABLED_KEY = "chat_documents_disabled"
DISABLED_REASON = ("Document attachments are switched off on this instance - an admin can "
                   "enable them under Settings.")


async def enabled_async(db) -> bool:
    from app.services import app_settings
    return not await app_settings.get_flag(db, DISABLED_KEY)


def enabled_sync(db) -> bool:
    from app.models import AppSetting
    row = db.get(AppSetting, DISABLED_KEY)
    return not (row is not None and bool(row.value))


PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HTML = "text/html"
MARKDOWN = "text/markdown"
CSV = "text/csv"
JSON = "application/json"
PLAIN = "text/plain"

# What the composer's file picker offers and what the chips call each kind.
DOCUMENT_TYPES: dict[str, str] = {
    PDF: "PDF", DOCX: "Word", HTML: "HTML", MARKDOWN: "Markdown",
    CSV: "CSV", JSON: "JSON", PLAIN: "Text",
}
EXTENSIONS: dict[str, str] = {
    PDF: "pdf", DOCX: "docx", HTML: "html", MARKDOWN: "md",
    CSV: "csv", JSON: "json", PLAIN: "txt",
}

# Extracted text kept per document. ~30k tokens: enough for a long spec, small
# enough that four of them still fit a mainstream context window next to the
# conversation. Past it the text is cut and the row says so (`truncated`).
MAX_TEXT_CHARS = 120_000
MAX_PDF_PAGES = 200
# A .docx is a zip: cap what word/document.xml may unpack to before parsing it.
MAX_DOCX_XML_BYTES = 40 * 1024 * 1024

_TEXT_EXT = {
    ".html": HTML, ".htm": HTML, ".xhtml": HTML,
    ".md": MARKDOWN, ".markdown": MARKDOWN, ".mdx": MARKDOWN,
    ".csv": CSV, ".tsv": CSV,
    ".json": JSON, ".jsonl": JSON,
}


class DocumentError(ValueError):
    """The bytes are a supported kind but nothing readable came out of them
    (scanned/encrypted PDF, corrupt archive, undecodable text). The message is
    written for the person who uploaded the file."""


def sniff_document(data: bytes, filename: str | None) -> str | None:
    """Content type from the magic bytes (and, for text, the filename) - None
    when the bytes are not a document this platform reads."""
    if data.startswith(b"%PDF-"):
        return PDF
    if data[:4] == b"PK\x03\x04":
        return DOCX if _zip_has(data, "word/document.xml") else None
    if not data or b"\x00" in data[:65536]:
        return None
    ext = PurePosixPath((filename or "").replace("\\", "/")).suffix.lower()
    head = data[:4096].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") \
            or (ext in (".html", ".htm", ".xhtml")):
        return HTML
    return _TEXT_EXT.get(ext, PLAIN)


def extract_text(data: bytes, content_type: str) -> tuple[str, int | None]:
    """`(text, pages)` - the document as the model will read it, capped at
    MAX_TEXT_CHARS (the caller compares lengths to know it was cut). Raises
    DocumentError when nothing readable came out."""
    if content_type == PDF:
        text, pages = _pdf_text(data)
    elif content_type == DOCX:
        text, pages = _docx_text(data), None
    elif content_type == HTML:
        text, pages = _html_text(_decode(data)), None
    else:
        text, pages = _decode(data), None
    text = _tidy(text)
    if not text.strip():
        raise DocumentError(
            "No readable text in this document"
            + (" - a scanned PDF has no text layer; send the pages as images instead."
               if content_type == PDF else "."))
    return text[:MAX_TEXT_CHARS], pages


# ------------------------------------------------------------------- parsers

def _pdf_text(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # An owner-password-only PDF opens with the empty user password.
            if not reader.decrypt(""):
                raise DocumentError("This PDF is password-protected - remove the "
                                    "password and upload it again.")
        pages = len(reader.pages)
        parts: list[str] = []
        total = 0
        for i, page in enumerate(reader.pages):
            if i >= MAX_PDF_PAGES or total > MAX_TEXT_CHARS:
                break
            chunk = (page.extract_text() or "").strip()
            if chunk:
                parts.append(f"[page {i + 1}]\n{chunk}")
                total += len(chunk)
        return "\n\n".join(parts), pages
    except DocumentError:
        raise
    except (PdfReadError, ValueError, TypeError, KeyError, IndexError,
            RecursionError, AttributeError) as exc:
        raise DocumentError(f"This PDF could not be read ({type(exc).__name__}).") from exc


def _zip_has(data: bytes, name: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return name in zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise DocumentError("This Word document is too large to read.")
            xml = zf.read(info)
        root = ElementTree.fromstring(xml)
    except DocumentError:
        raise
    except (zipfile.BadZipFile, KeyError, OSError, ElementTree.ParseError) as exc:
        raise DocumentError(f"This Word document could not be read ({type(exc).__name__}).") from exc
    paragraphs: list[str] = []
    for p in root.iter(f"{_W}p"):
        runs: list[str] = []
        for el in p.iter():
            if el.tag == f"{_W}t":
                runs.append(el.text or "")
            elif el.tag == f"{_W}tab":
                runs.append("\t")
            elif el.tag in (f"{_W}br", f"{_W}cr"):
                runs.append("\n")
        paragraphs.append("".join(runs))
    return "\n".join(paragraphs)


class _HtmlText(HTMLParser):
    """Visible text only: script/style/template dropped, block elements
    separated by line breaks, table cells by tabs, entities decoded."""
    _SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object"}
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "tr", "table", "section", "article", "header", "footer", "nav", "aside",
              "blockquote", "pre", "hr", "dt", "dd", "figure", "figcaption", "title",
              "main", "form", "fieldset", "legend", "address", "details", "summary"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append("\t")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html_text(html: str) -> str:
    parser = _HtmlText()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - html.parser is lenient, but stay bounded
        raise DocumentError("This HTML could not be read.") from exc
    return "".join(parser.out)


def _decode(data: bytes) -> str:
    """UTF-8 first (BOM tolerated), UTF-16 on its BOM, else cp1252 - the
    encoding a Windows notepad export still carries."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


_BLANK_RUN = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_TRAIL = re.compile(r"[ \t]+\n")


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAIL.sub("\n", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip("\n")
