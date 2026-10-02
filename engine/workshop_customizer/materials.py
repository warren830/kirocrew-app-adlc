"""Customer materials: type detection, bounded text extraction and the sensitive-data scan.

The SA uploads customer materials (policies, FAQs, slide decks, spreadsheets) so Kiro can derive a
draft from them. Materials are *never* pack sources: they are stored under
``projects/<pid>/materials`` (excluded from the pack digest, compile, release and export) and only
their extracted text is sent to the model, as JSON-escaped data, under a per-material budget
(``app/backend/routes.py`` ``allocate_materials``).

Extraction is standard library only:

* ``md`` / ``txt`` — decoded as UTF-8 (with or without BOM) or GB18030;
* ``csv`` — a Markdown table (at most :data:`CSV_MAX_ROWS` rows × :data:`CSV_MAX_COLUMNS` columns,
  :data:`CSV_MAX_CELL` characters per cell);
* ``json`` — validated, then kept as a fenced block; ``yaml`` — kept as a fenced block and never
  parsed (no alias bombs);
* ``docx`` / ``pptx`` — zip + XML: body order, headings, lists and tables for Word; presentation
  order (``sldIdLst``) plus speaker notes for PowerPoint. Zip bombs (entry count, ratio, total
  bytes), damaged or unsupported zip members, and any DOCTYPE / ENTITY declaration (in every XML
  encoding, checked by the XML parser itself) are refused as :class:`MaterialError`;
* ``pdf`` — through the optional ``pypdf`` package only; without it (or for encrypted and scanned
  files) the extraction status is ``failed`` with a message telling the SA to upload DOCX or TXT.

The text is normalized (NFC, ``\\n`` newlines, control characters stripped, at most two blank lines
in a row) and capped at :data:`MAX_TEXT_CHARS` characters.
"""

from __future__ import annotations

import csv
import importlib
import io
import json
import re
import time
import unicodedata
import zipfile
import zlib
import posixpath
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from dataclasses import dataclass, field
from typing import Any, Callable

from .validator import ASCII_END, ASCII_START, SECRET_PATTERNS

#: File extension -> media type.
SUPPORTED: dict[str, str] = {
    ".md": "md", ".markdown": "md", ".txt": "txt", ".csv": "csv", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".docx": "docx", ".pptx": "pptx", ".pdf": "pdf",
}
MEDIA_TYPES = ("md", "txt", "csv", "json", "yaml", "docx", "pptx", "pdf")
TEXT_TYPES = frozenset({"md", "txt", "csv", "json", "yaml"})
GENERATION_USES = ("source", "background", "exclude")
AUTHORS = ("customer", "sa")
EXTRACTION_STATUSES = ("ok", "partial", "failed")
MAX_TEXT_CHARS = 300_000
MAX_NAME_CHARS = 120
MAX_NOTES_CHARS = 500
#: Upload limits (enforced by the app backend).
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 20
MAX_TOTAL_BYTES = 30 * 1024 * 1024
#: Zip guards for docx / pptx.
ZIP_MAX_ENTRIES = 2000
ZIP_MAX_RATIO = 200
ZIP_MAX_TOTAL_READ = 40 * 1024 * 1024
#: PDF guards.
PDF_MAX_PAGES = 300
PDF_MIN_CHARS = 200
PDF_MIN_PRINTABLE = 0.85
PDF_TIME_BUDGET = 12.0
#: CSV table caps.
CSV_MAX_ROWS = 2000
CSV_MAX_COLUMNS = 40
CSV_MAX_CELL = 300

PDF_UNAVAILABLE = ("PDF text extraction needs the optional pypdf package, which is not installed here; "
                   "export the document as DOCX or TXT and upload that instead")
PDF_NOT_EXTRACTABLE = "PDF text not extractable (CID fonts or a scanned document); upload DOCX/TXT"

#: Credential and personal-identifier shapes (the engine's release gate plus the generation routes'
#: bearer / session-token shapes). A hit refuses the upload; the content is never echoed.
_EXTRA_SENSITIVE: tuple[tuple[str, str], ...] = (
    ("bearer-token", ASCII_START + r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ("aws-session-token", r"(?i)aws_session_token\s*[:=]\s*[A-Za-z0-9/+=]{40,}"),
    ("aws-secret-access-key", r"(?i)aws_secret_access_key\s*[:=]\s*[A-Za-z0-9/+=]{20,}"),
)
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, re.compile(pattern)) for code, pattern in (*SECRET_PATTERNS, *_EXTRA_SENSITIVE)
)
# ASCII boundaries (see validator.ASCII_START): a CJK character touching the address or number is not a word character here.
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" + ASCII_END)
_PHONE_RE = re.compile(r"(?<![0-9A-Za-z_-])(?:\+?\d{1,3}[ -]?)?(?:\(\d{2,4}\)[ -]?)?\d{3,4}[ -]?\d{4}(?![0-9A-Za-z_-])|(?<!\d)1[3-9]\d{9}(?!\d)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAME_UNSAFE_RE = re.compile(r"[^\w.\- ()]+")  # \w is Unicode-aware: CJK names stay readable

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class MaterialError(ValueError):
    """An upload the app refuses; ``status`` is the HTTP status the backend answers with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Extraction:
    text: str
    status: str
    method: str
    warnings: list[str] = field(default_factory=list)
    pages: int | None = None
    truncated: bool = False

    @property
    def chars(self) -> int:
        return len(self.text)

    def as_record(self) -> dict[str, Any]:
        return {"status": self.status, "method": self.method, "chars": self.chars, "truncated": self.truncated,
                "pages": self.pages, "warnings": list(self.warnings)}


# ---------------------------------------------------------------------------
# names, types, normalization
# ---------------------------------------------------------------------------


def sanitize_name(name: Any) -> str:
    """A safe display basename (no directories, control characters or shell metacharacters)."""
    raw = unicodedata.normalize("NFC", str(name or ""))
    base = re.split(r"[\\/]", raw)[-1]
    base = _CONTROL_RE.sub("", base).strip()
    base = _NAME_UNSAFE_RE.sub("_", base)
    base = re.sub(r"_+", "_", base).lstrip(". ").strip() or "material"
    if len(base) > MAX_NAME_CHARS:
        stem, dot, ext = base.rpartition(".")
        ext = ext if dot and len(ext) <= 10 else ""
        base = (stem if dot else base)[: MAX_NAME_CHARS - len(ext) - (1 if ext else 0)].rstrip() + (f".{ext}" if ext else "")
    return base


def extension(name: str) -> str:
    return posixpath.splitext(name.lower())[1]


def media_type(name: str) -> str:
    kind = SUPPORTED.get(extension(name))
    if kind is None:
        raise MaterialError(f"unsupported material type {extension(name) or '<none>'}; upload "
                            + ", ".join(sorted(SUPPORTED)), 415)
    return kind


def decode_text(data: bytes) -> str:
    """UTF-8 (BOM optional) or GB18030; anything else is not a text material."""
    if b"\x00" in data:
        raise MaterialError("the file contains binary data; upload a text export", 415)
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MaterialError("the text is neither UTF-8 nor GB18030; save it as UTF-8 and upload again", 415)


def detect_type(name: str, data: bytes) -> str:
    """The media type of an upload, checked against its magic bytes."""
    kind = media_type(name)
    is_zip = data[:4] == b"PK\x03\x04"
    if kind in ("docx", "pptx"):
        if not is_zip:
            raise MaterialError(f"{name} is not a {kind} file (no zip container)", 415)
    elif kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise MaterialError(f"{name} is not a PDF file", 415)
    elif is_zip or data.startswith(b"%PDF-"):
        raise MaterialError(f"{name} is a binary document, not {kind} text", 415)
    return kind


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        blank = blank + 1 if not line else 0
        if blank <= 2:
            out.append(line)
    return "\n".join(out).strip("\n") + ("\n" if any(out) else "")


# ---------------------------------------------------------------------------
# zip + XML guards
# ---------------------------------------------------------------------------


#: What a damaged or exotic zip raises while it is opened or read (never a 500 for the SA).
_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError,
               OSError, ValueError)


class _Zip:
    """A docx/pptx container read under the bomb guards."""

    def __init__(self, data: bytes):
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(data))
        except _ZIP_ERRORS as exc:
            raise MaterialError("the document is not a valid zip container", 415) from exc
        infos = self.archive.infolist()
        if len(infos) > ZIP_MAX_ENTRIES:
            raise MaterialError(f"the document has more than {ZIP_MAX_ENTRIES} zip entries", 400)
        self.infos = {info.filename: info for info in infos}
        self.read_total = 0
        if "[Content_Types].xml" not in self.infos:
            raise MaterialError("the document has no [Content_Types].xml; it is not an Office file", 415)

    def has(self, member: str) -> bool:
        return member in self.infos

    def read(self, member: str) -> bytes:
        info = self.infos.get(member)
        if info is None:
            raise MaterialError(f"the document lacks {member}", 415)
        if info.file_size > ZIP_MAX_RATIO * max(info.compress_size, 1):
            raise MaterialError(f"{member} expands more than {ZIP_MAX_RATIO}x (zip bomb guard)", 400)
        self.read_total += info.file_size
        if self.read_total > ZIP_MAX_TOTAL_READ:
            raise MaterialError("the document expands beyond the 40 MiB extraction limit", 400)
        try:
            return self.archive.read(info)
        except _ZIP_ERRORS as exc:  # bad CRC, broken deflate stream, encrypted or unsupported method
            raise MaterialError(f"{member} is damaged or uses an unsupported zip feature ({type(exc).__name__}); "
                                "save the document again and upload that copy", 400) from exc

    def xml(self, member: str) -> ET.Element:
        return parse_xml(self.read(member), member)

    def names(self) -> list[str]:
        return list(self.infos)


def _refuse_dtd(raw: bytes, member: str) -> None:
    """Refuse any DOCTYPE or ENTITY declaration, whatever the part's encoding (UTF-16 included).

    The byte check is only a fast path; the authority is expat itself, whose declaration handlers
    fire before any entity is expanded.
    """
    refused = MaterialError(f"{member} declares a DOCTYPE or ENTITY; refused", 400)
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise refused

    def refuse(*_args: Any) -> None:
        raise refused

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise MaterialError(f"{member} is not well-formed XML", 400) from exc


def parse_xml(raw: bytes, member: str = "xml") -> ET.Element:
    _refuse_dtd(raw, member)
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise MaterialError(f"{member} is not well-formed XML", 400) from exc


def _relationships(zf: _Zip, rels_member: str) -> dict[str, tuple[str, str]]:
    """Relationship id -> (type, target path resolved against the part's directory)."""
    if not zf.has(rels_member):
        return {}
    base = posixpath.dirname(posixpath.dirname(rels_member))
    out: dict[str, tuple[str, str]] = {}
    for rel in zf.xml(rels_member).iter(f"{PKG_REL}Relationship"):
        target = rel.get("Target") or ""
        path = posixpath.normpath(posixpath.join(base, target)) if not target.startswith("/") else target.lstrip("/")
        out[rel.get("Id") or ""] = (rel.get("Type") or "", path)
    return out


# ---------------------------------------------------------------------------
# docx
# ---------------------------------------------------------------------------


def _docx_run_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{W}tab":
            parts.append("\t")
        elif node.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_paragraph(p: ET.Element) -> str:
    text = _docx_run_text(p).strip()
    if not text:
        return ""
    ppr = p.find(f"{W}pPr")
    style = ""
    listed = False
    if ppr is not None:
        pstyle = ppr.find(f"{W}pStyle")
        style = (pstyle.get(f"{W}val") or "") if pstyle is not None else ""
        listed = ppr.find(f"{W}numPr") is not None
    heading = re.fullmatch(r"(?i)heading\s*([1-9])", style)
    if heading:
        return "#" * int(heading.group(1)) + " " + text
    if style.lower() == "title":
        return "# " + text
    if listed or style.lower().startswith("list"):
        return "- " + text
    return text


def _cell(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().replace("|", "\\|")
    return text[:CSV_MAX_CELL]


def _markdown_table(rows: list[list[str]]) -> str:
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return ""
    width = min(max(len(row) for row in rows), CSV_MAX_COLUMNS)
    norm = [[_cell(row[i]) if i < len(row) else "" for i in range(width)] for row in rows]
    lines = ["| " + " | ".join(norm[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(row) + " |" for row in norm[1:]]
    return "\n".join(lines)


def _docx_table(tbl: ET.Element) -> str:
    rows = []
    for tr in tbl.findall(f"{W}tr"):
        rows.append([" ".join(filter(None, (_docx_paragraph(p) for p in tc.findall(f".//{W}p")))) for tc in tr.findall(f"{W}tc")])
    return _markdown_table(rows)


def extract_docx(data: bytes) -> Extraction:
    zf = _Zip(data)
    root = zf.xml("word/document.xml")
    body = root.find(f"{W}body")
    blocks: list[str] = []
    for child in list(body) if body is not None else []:
        if child.tag == f"{W}p":
            text = _docx_paragraph(child)
        elif child.tag == f"{W}tbl":
            text = _docx_table(child)
        else:
            continue
        if text:
            blocks.append(text)
    warnings: list[str] = []
    if zf.has("word/footnotes.xml"):
        notes = []
        for note in zf.xml("word/footnotes.xml").findall(f"{W}footnote"):
            if note.get(f"{W}type") in ("separator", "continuationSeparator"):
                continue
            text = " ".join(filter(None, (_docx_paragraph(p) for p in note.findall(f".//{W}p"))))
            if text:
                notes.append(f"- {text}")
        if notes:
            blocks.append("## Footnotes\n\n" + "\n".join(notes))
    if zf.has("word/comments.xml"):
        warnings.append("Word comments were not extracted")
    return Extraction(text="\n\n".join(blocks), status="ok", method="docx-xml", warnings=warnings)


# ---------------------------------------------------------------------------
# pptx
# ---------------------------------------------------------------------------


def _slide_paragraphs(root: ET.Element) -> list[str]:
    out = []
    for paragraph in root.iter(f"{A}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{A}t")).strip()
        if text:
            out.append(text)
    return out


def _slide_number(path: str) -> int:
    match = re.search(r"(\d+)\.xml$", path)
    return int(match.group(1)) if match else 0


def extract_pptx(data: bytes) -> Extraction:
    zf = _Zip(data)
    warnings: list[str] = []
    slides: list[str] = []
    if zf.has("ppt/presentation.xml"):
        rels = _relationships(zf, "ppt/_rels/presentation.xml.rels")
        order = zf.xml("ppt/presentation.xml").find(f"{P}sldIdLst")
        for sld in list(order) if order is not None else []:
            target = rels.get(sld.get(f"{R}id") or "")
            if target and zf.has(target[1]):
                slides.append(target[1])
    if not slides:
        slides = sorted((n for n in zf.names() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_slide_number)
        if slides:
            warnings.append("slide order taken from file names (no presentation order found)")
    blocks: list[str] = []
    for number, member in enumerate(slides, start=1):
        lines = _slide_paragraphs(zf.xml(member))
        rels_member = posixpath.join(posixpath.dirname(member), "_rels", posixpath.basename(member) + ".rels")
        notes: list[str] = []
        for rel_type, target in _relationships(zf, rels_member).values():
            if rel_type.endswith("/notesSlide") and zf.has(target):
                notes += [n for n in _slide_paragraphs(zf.xml(target)) if not n.isdigit()]
        block = [f"## Slide {number}"] + lines
        if notes:
            block += ["", "Notes: " + " ".join(notes)]
        blocks.append("\n".join(block))
    return Extraction(text="\n\n".join(blocks), status="ok", method="pptx-xml", warnings=warnings, pages=len(slides))


# ---------------------------------------------------------------------------
# pdf (optional pypdf)
# ---------------------------------------------------------------------------


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    good = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    return good / len(text)


def extract_pdf(data: bytes, *, time_budget: float = PDF_TIME_BUDGET, clock: Callable[[], float] = time.monotonic) -> Extraction:
    try:
        pypdf = importlib.import_module("pypdf")
    except ImportError:
        return Extraction(text="", status="failed", method="pdf-text", warnings=[PDF_UNAVAILABLE])
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            return Extraction(text="", status="failed", method="pdf-text", warnings=["the PDF is encrypted; upload an unprotected export"])
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001 - a broken PDF is a failed extraction, never a 500
        return Extraction(text="", status="failed", method="pdf-text", warnings=[f"the PDF could not be read ({type(exc).__name__})"])
    started = clock()
    parts: list[str] = []
    warnings: list[str] = []
    status = "ok"
    for index, page in enumerate(pages):
        if index >= PDF_MAX_PAGES:
            warnings.append(f"only the first {PDF_MAX_PAGES} pages were read")
            status = "partial"
            break
        if clock() - started > time_budget:
            warnings.append(f"extraction stopped after {index} pages ({time_budget:.0f} s budget)")
            status = "partial"
            break
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one broken page does not fail the document
            warnings.append(f"page {index + 1} could not be read")
            status = "partial"
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    if len(text) < PDF_MIN_CHARS or _printable_ratio(text) < PDF_MIN_PRINTABLE:
        return Extraction(text="", status="failed", method="pdf-text", warnings=[PDF_NOT_EXTRACTABLE], pages=len(pages))
    return Extraction(text=text, status=status, method="pdf-text", warnings=warnings, pages=len(pages))


# ---------------------------------------------------------------------------
# text formats
# ---------------------------------------------------------------------------


def extract_csv(text: str) -> Extraction:
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    warnings: list[str] = []
    try:
        for index, row in enumerate(csv.reader(io.StringIO(text), dialect)):
            if index >= CSV_MAX_ROWS:
                warnings.append(f"only the first {CSV_MAX_ROWS} rows were kept")
                break
            if len(row) > CSV_MAX_COLUMNS and not any("columns" in w for w in warnings):
                warnings.append(f"only the first {CSV_MAX_COLUMNS} columns were kept")
            rows.append(row)
    except csv.Error as exc:  # e.g. a field over the csv module's size limit: keep the text, like broken JSON
        return Extraction(text=text, status="partial", method="plain",
                          warnings=[f"the CSV could not be read as a table ({exc}); kept as text"])
    return Extraction(text=_markdown_table(rows), status="partial" if warnings else "ok", method="csv-table", warnings=warnings)


def extract_json(text: str) -> Extraction:
    try:
        json.loads(text)
    except ValueError:
        return Extraction(text="```json\n" + text.strip() + "\n```", status="partial", method="plain",
                          warnings=["the JSON does not parse; kept as text"])
    return Extraction(text="```json\n" + text.strip() + "\n```", status="ok", method="plain")


def extract(name: str, data: bytes, *, max_chars: int = MAX_TEXT_CHARS, time_budget: float = PDF_TIME_BUDGET) -> Extraction:
    """Extract normalized text from one upload; raises :class:`MaterialError` for unsafe input."""
    kind = detect_type(name, data)
    if kind in TEXT_TYPES:
        text = decode_text(data)
        if kind == "csv":
            result = extract_csv(text)
        elif kind == "json":
            result = extract_json(text)
        elif kind == "yaml":  # never parsed (alias bombs, arbitrary tags)
            result = Extraction(text="```yaml\n" + text.strip() + "\n```", status="ok", method="plain")
        else:
            result = Extraction(text=text, status="ok", method="plain")
    elif kind == "docx":
        result = extract_docx(data)
    elif kind == "pptx":
        result = extract_pptx(data)
    else:
        result = extract_pdf(data, time_budget=time_budget)
    result.text = normalize_text(result.text) if result.text else ""
    if len(result.text) > max_chars:
        result.text = result.text[:max_chars]
        result.truncated = True
        result.warnings.append(f"text truncated to {max_chars} characters")
        if result.status == "ok":
            result.status = "partial"
    if result.status != "failed" and not result.text.strip():
        result.status = "failed"
        result.warnings.append("no text could be extracted")
    return result


# ---------------------------------------------------------------------------
# sensitive data
# ---------------------------------------------------------------------------


def scan_sensitive(text: str) -> list[tuple[str, int]]:
    """(code, 1-based line) of every credential or national-id shape; the matched text is never returned."""
    hits: list[tuple[str, int]] = []
    for number, line in enumerate(text.splitlines() or [text], start=1):
        for code, pattern in SENSITIVE_PATTERNS:
            if pattern.search(line) and (code, number) not in hits:
                hits.append((code, number))
    return hits


def count_contacts(text: str) -> int:
    """Email addresses plus phone-number shapes (a warning: replace people with roles)."""
    return len(_EMAIL_RE.findall(text)) + len(_PHONE_RE.findall(text))
