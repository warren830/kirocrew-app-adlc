"""Customer materials extraction (engine/workshop_customizer/materials.py): stdlib only, bounded,
refusing unsafe containers, never parsing YAML, and PDF through the optional pypdf only."""

from __future__ import annotations

import io
import sys
import types
import zipfile

import pytest

from workshop_customizer import materials as m

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
P_NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
REL_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
NOTES_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
SLIDE_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"


def _zip(members: dict[str, str | bytes], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        for name, content in members.items():
            archive.writestr(name, content)
    return buf.getvalue()


def _w_p(text: str, style: str | None = None, numbered: bool = False) -> str:
    ppr = ""
    if style or numbered:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "") + ("<w:numPr><w:ilvl w:val=\"0\"/></w:numPr>" if numbered else "") + "</w:pPr>"
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx(body: str, *, footnotes: str | None = None) -> bytes:
    members = {"word/document.xml": f'<?xml version="1.0"?><w:document {W_NS}><w:body>{body}</w:body></w:document>'}
    if footnotes:
        members["word/footnotes.xml"] = f'<?xml version="1.0"?><w:footnotes {W_NS}>{footnotes}</w:footnotes>'
    return _zip(members)


def test_docx_extracts_headings_lists_tables_in_body_order():
    table = ("<w:tbl><w:tr><w:tc>" + _w_p("Tier") + "</w:tc><w:tc>" + _w_p("Points") + "</w:tc></w:tr>"
             "<w:tr><w:tc>" + _w_p("Gold") + "</w:tc><w:tc>" + _w_p("5000") + "</w:tc></w:tr></w:tbl>")
    body = (_w_p("Loyalty Rules", "Title") + _w_p("Earning", "Heading1") + _w_p("Points post after 48 hours.")
            + _w_p("Receipts older than 30 days are refused", numbered=True) + _w_p("Card only", "ListBullet")
            + table + _w_p("Tiers", "Heading2"))
    note = '<w:footnote w:type="separator"><w:p/></w:footnote><w:footnote w:id="1">' + _w_p("Board decision 2026-03.") + "</w:footnote>"
    ex = m.extract("rules.docx", _docx(body, footnotes=note))
    assert ex.status == "ok" and ex.method == "docx-xml"
    assert ex.text == (
        "# Loyalty Rules\n\n# Earning\n\nPoints post after 48 hours.\n\n- Receipts older than 30 days are refused\n\n"
        "- Card only\n\n| Tier | Points |\n|---|---|\n| Gold | 5000 |\n\n## Tiers\n\n## Footnotes\n\n- Board decision 2026-03.\n"
    )


def _slide(text: str) -> str:
    return f'<?xml version="1.0"?><p:sld {P_NS}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'


def test_pptx_uses_presentation_order_and_includes_notes():
    members = {
        "ppt/presentation.xml": f'<?xml version="1.0"?><p:presentation {P_NS}><p:sldIdLst>'
                                '<p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/><p:sldId id="258" r:id="rId10"/>'
                                "</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": f'<?xml version="1.0"?><Relationships {REL_NS}>'
                                           f'<Relationship Id="rId1" Type="{SLIDE_TYPE}" Target="slides/slide1.xml"/>'
                                           f'<Relationship Id="rId2" Type="{SLIDE_TYPE}" Target="slides/slide2.xml"/>'
                                           f'<Relationship Id="rId10" Type="{SLIDE_TYPE}" Target="slides/slide10.xml"/>'
                                           "</Relationships>",
        "ppt/slides/slide1.xml": _slide("Second in the deck"),
        "ppt/slides/slide2.xml": _slide("First in the deck"),
        "ppt/slides/slide10.xml": _slide("Last in the deck"),
        "ppt/slides/_rels/slide2.xml.rels": f'<?xml version="1.0"?><Relationships {REL_NS}>'
                                            f'<Relationship Id="rId1" Type="{NOTES_TYPE}" Target="../notesSlides/notesSlide1.xml"/></Relationships>',
        "ppt/notesSlides/notesSlide1.xml": _slide("Speaker says: mention the 48 hour rule").replace("p:sld ", "p:notes ").replace("</p:sld>", "</p:notes>"),
    }
    ex = m.extract("deck.pptx", _zip(members))
    assert ex.status == "ok" and ex.pages == 3
    assert ex.text == ("## Slide 1\nFirst in the deck\n\nNotes: Speaker says: mention the 48 hour rule\n\n"
                       "## Slide 2\nSecond in the deck\n\n## Slide 3\nLast in the deck\n")
    del members["ppt/presentation.xml"]
    fallback = m.extract("deck.pptx", _zip(members))
    assert fallback.text.index("Second in the deck") < fallback.text.index("Last in the deck")  # 1, 2, 10 numerically
    assert any("file names" in w for w in fallback.warnings)


def test_csv_markdown_table_caps(monkeypatch):
    ex = m.extract("points.csv", "tier;points\nGold;5000\nSilver|x;1000\n".encode())
    assert ex.text == "| tier | points |\n|---|---|\n| Gold | 5000 |\n| Silver\\|x | 1000 |\n" and ex.status == "ok"
    monkeypatch.setattr(m, "CSV_MAX_ROWS", 3)
    monkeypatch.setattr(m, "CSV_MAX_COLUMNS", 2)
    monkeypatch.setattr(m, "CSV_MAX_CELL", 5)
    rows = "\n".join(",".join(f"r{i}c{j}xxxxxxxx" for j in range(4)) for i in range(10))
    capped = m.extract("wide.csv", rows.encode())
    lines = capped.text.strip().split("\n")
    assert len(lines) == 4 and lines[0] == "| r0c0x | r0c1x |" and capped.status == "partial"
    assert len(capped.warnings) == 2


def test_json_yaml_kept_as_fenced_text():
    ex = m.extract("rules.json", b'{"window": 30}')
    assert ex.text == '```json\n{"window": 30}\n```\n' and ex.status == "ok"
    broken = m.extract("rules.json", b'{"window": ')
    assert broken.status == "partial" and broken.text.startswith("```json")
    bomb = b'a: &a ["lol","lol"]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\nd: [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n'
    yml = m.extract("bomb.yaml", bomb)  # never parsed: the alias bomb is just text
    assert yml.text == "```yaml\n" + bomb.decode().strip() + "\n```\n"


class _FakePage:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self):
        return self.text


def _fake_pypdf(pages: list[str], *, encrypted: bool = False) -> types.ModuleType:
    module = types.ModuleType("pypdf")

    class PdfReader:
        def __init__(self, stream):
            assert stream.read(5) == b"%PDF-"
            self.is_encrypted = encrypted
            self.pages = [_FakePage(t) for t in pages]

    module.PdfReader = PdfReader
    return module


PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def test_pdf_without_pypdf_fails_with_a_clear_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)  # import raises ImportError
    ex = m.extract("policy.pdf", PDF)
    assert ex.status == "failed" and ex.text == "" and ex.warnings == [m.PDF_UNAVAILABLE]
    assert "DOCX or TXT" in m.PDF_UNAVAILABLE


def test_pdf_text_through_pypdf(monkeypatch):
    page = "Refunds are processed within 14 days of the request. " * 5
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf([page, page]))
    ex = m.extract("policy.pdf", PDF)
    assert ex.status == "ok" and ex.method == "pdf-text" and ex.pages == 2 and "14 days" in ex.text


def test_pdf_encrypted_or_cid_only_fails_with_warning(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["x" * 500], encrypted=True))
    assert m.extract("locked.pdf", PDF).status == "failed"
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["\x01\x02\x03" * 100]))
    scanned = m.extract("scan.pdf", PDF)
    assert scanned.status == "failed" and scanned.warnings == [m.PDF_NOT_EXTRACTABLE]


def test_pdf_page_and_time_budget_give_partial(monkeypatch):
    page = "A readable page of policy text that is long enough to count. " * 5
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf([page] * 5))
    monkeypatch.setattr(m, "PDF_MAX_PAGES", 2)
    ex = m.extract("long.pdf", PDF)
    assert ex.status == "partial" and ex.text.count("readable page") == 10
    ticks = iter(range(100))
    budget = m.extract_pdf(PDF, time_budget=1.5, clock=lambda: next(ticks))
    assert budget.status == "partial" and any("budget" in w for w in budget.warnings)


def test_zip_bomb_ratio_entry_count_and_doctype_rejected(monkeypatch):
    bomb = _zip({"word/document.xml": "<a>" + "x" * 1_000_000 + "</a>"})
    with pytest.raises(m.MaterialError, match="zip bomb"):
        m.extract("bomb.docx", bomb)
    monkeypatch.setattr(m, "ZIP_MAX_ENTRIES", 3)
    many = _zip({f"word/x{i}.xml": "<a/>" for i in range(5)})
    with pytest.raises(m.MaterialError, match="zip entries"):
        m.extract("many.docx", many)
    monkeypatch.undo()
    xxe = _zip({"word/document.xml": '<!DOCTYPE d [<!ENTITY x "y">]><w:document ' + W_NS + "/>"})
    with pytest.raises(m.MaterialError, match="DOCTYPE"):
        m.extract("xxe.docx", xxe)
    with pytest.raises(m.MaterialError, match="Content_Types"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("word/document.xml", "<a/>")
        m.extract("plain.docx", buf.getvalue())


def _patch_member_method(raw: bytes, member: str, method: int) -> bytes:
    """The zip with ``member``'s compression method rewritten in its local and central headers."""
    data = bytearray(raw)
    name = member.encode()
    for signature, method_at, name_len_at, header in ((b"PK\x03\x04", 8, 26, 30), (b"PK\x01\x02", 10, 28, 46)):
        pos = data.find(signature)
        while pos >= 0:
            length = int.from_bytes(data[pos + name_len_at:pos + name_len_at + 2], "little")
            if data[pos + header:pos + header + length] == name:
                data[pos + method_at:pos + method_at + 2] = method.to_bytes(2, "little")
            pos = data.find(signature, pos + 4)
    return bytes(data)


def test_damaged_office_files_are_material_errors_not_crashes():
    """A broken deflate stream, a bad CRC and an unsupported compression method are the SA's 400."""
    body = _w_p("hello " * 200)
    deflated = bytearray(_docx(body))
    at = deflated.find(b"word/document.xml") + len("word/document.xml") + 10
    deflated[at] ^= 0xFF
    stored = bytearray(_zip({"word/document.xml": f'<w:document {W_NS}><w:body>{body}</w:body></w:document>'},
                            compression=zipfile.ZIP_STORED))
    at = stored.find(b"hello hello")
    stored[at] ^= 0x01  # the stored bytes no longer match their CRC-32
    unsupported = _patch_member_method(_zip({"word/document.xml": f"<w:document {W_NS}/>"}, compression=zipfile.ZIP_STORED),
                                       "word/document.xml", 99)
    for name, raw in (("deflate.docx", bytes(deflated)), ("crc.docx", bytes(stored)), ("aes.docx", unsupported)):
        with pytest.raises(m.MaterialError, match="damaged or uses an unsupported zip feature") as info:
            m.extract(name, raw)
        assert info.value.status == 400, name


def test_csv_field_over_the_csv_limit_is_kept_as_text():
    text = 'id,text\n1,"' + "x" * 140_000 + '"\n'
    ex = m.extract("big.csv", text.encode())
    assert ex.status == "partial" and ex.method == "plain" and any("kept as text" in w for w in ex.warnings)
    assert "x" * 1000 in ex.text


def test_doctype_is_refused_in_every_xml_encoding():
    """The guard is the XML parser, not a byte search: a UTF-16 part cannot smuggle a DTD past it."""
    xml = ('<?xml version="1.0" encoding="UTF-16"?>\n<!DOCTYPE d [<!ENTITY a "AAAAAAAAAA">'
           '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
           f'<w:document {W_NS}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>')
    for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
        raw = xml.encode(encoding)
        if encoding != "utf-16":
            raw = ("\ufeff" + xml).encode(encoding)  # an explicit BOM, as Word writes it
        with pytest.raises(m.MaterialError, match="DOCTYPE or ENTITY"):
            m.extract("utf16.docx", _zip({"word/document.xml": raw}))
    clean = ('<?xml version="1.0" encoding="UTF-16"?>'
             f'<w:document {W_NS}><w:body>{_w_p("Points post after 48 hours.")}</w:body></w:document>')
    assert m.extract("clean16.docx", _zip({"word/document.xml": clean.encode("utf-16")})).text == "Points post after 48 hours.\n"


def test_type_detection_checks_magic_bytes():
    with pytest.raises(m.MaterialError) as info:
        m.extract("tool.exe", b"MZ")
    assert info.value.status == 415
    with pytest.raises(m.MaterialError, match="not a docx"):
        m.extract("fake.docx", b"just text")
    with pytest.raises(m.MaterialError, match="not a PDF"):
        m.extract("fake.pdf", b"just text")
    with pytest.raises(m.MaterialError, match="binary document"):
        m.extract("renamed.txt", _docx(_w_p("x")))


def test_text_encoding_utf8sig_gb18030_and_undecodable():
    assert m.extract("a.md", "\ufeff# 积分规则\r\n\r\n\r\n\r\n\r\n每笔消费累计积分。".encode("utf-8")).text == "# 积分规则\n\n\n每笔消费累计积分。\n"
    assert m.extract("b.txt", "会员积分三十天内到账".encode("gb18030")).text == "会员积分三十天内到账\n"
    with pytest.raises(m.MaterialError, match="UTF-8"):
        m.extract("c.txt", b"\xff\xfe\xfa\x80\x81\xff")
    with pytest.raises(m.MaterialError, match="binary"):
        m.extract("d.txt", b"text\x00with nul")
    assert m.extract("e.md", b"   \n\n").status == "failed"


def test_text_is_capped_and_marked_truncated():
    ex = m.extract("big.txt", ("line\n" * 1000).encode(), max_chars=100)
    assert ex.chars == 100 and ex.truncated and ex.status == "partial"
    assert ex.as_record()["truncated"] is True and ex.as_record()["chars"] == 100


@pytest.mark.parametrize("raw, expected", [
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\sa\\Loyalty Rules (v2).docx", "Loyalty Rules (v2).docx"),
    ("..hidden.md", "hidden.md"),
    ("rm -rf $HOME;.txt", "rm -rf _HOME_.txt"),
    ("积分规则.md", "积分规则.md"),
    ("", "material"),
    ("x" * 300 + ".docx", "x" * 115 + ".docx"),
])
def test_sanitize_name(raw, expected):
    assert m.sanitize_name(raw) == expected


def test_scan_sensitive_reports_codes_and_lines_only():
    text = "Welcome\nkey AKIA" + "ABCDEFGHIJKLMNOP" + "\nid " + "110101" + "19900307" + "4514" + "\nfine\nAuthorization: Bearer " + "a" * 30
    hits = m.scan_sensitive(text)
    assert ("aws-access-key-id", 2) in hits and ("cn-national-id", 3) in hits and ("bearer-token", 5) in hits
    assert all(isinstance(code, str) and isinstance(line, int) for code, line in hits)
    assert m.scan_sensitive("Points post after 48 hours.") == []


def test_scan_sensitive_finds_identifiers_touching_cjk_text():
    """Chinese prose writes an ID with no space around it; ``\\b`` would see no boundary there (review finding)."""
    national_id, ssn, key = "110101" + "199003071234", "123-" + "45-6789", "AKIA" + "ABCDEFGHIJKLMNOP"
    assert [c for c, _ in m.scan_sensitive(f"员工身份证号{national_id}请核实")] == ["cn-national-id"]
    assert [c for c, _ in m.scan_sensitive(f"社保号{ssn}已登记")] == ["us-ssn"]
    assert [c for c, _ in m.scan_sensitive(f"密钥{key}勿外传")] == ["aws-access-key-id"]
    assert [c for c, _ in m.scan_sensitive("令牌Bearer " + "a" * 24)] == ["bearer-token"]
    assert m.scan_sensitive(f"x1{national_id}5") == [] and m.scan_sensitive(f"A{ssn}9") == []  # glued to ASCII: not an ID


def test_count_contacts():
    assert m.count_contacts("Mail ops@example.com or call 138 0013 8000; 13800138000") == 3
    assert m.count_contacts("Points post after 48 hours.") == 0
    assert m.count_contacts("联系邮箱ops@example.com或致电010-6512-3456") == 2
