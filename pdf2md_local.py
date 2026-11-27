#!/usr/bin/env python3
"""pdf2md-local: turn the PDFs you drop into a folder into Markdown, on your own computer.

After installing there are two folders (move or rename them whenever you like):

    input_pdf/    drop PDFs here; converted PDFs move to input_pdf/done/
    output_md/    one folder per document: <name>/<name>.md plus its images

A background watcher converts each new PDF with MinerU on this machine's GPU.
Everyday commands:

    pdf2md-local status       what is running, what is queued, recent log
    pdf2md-local open         open the input and output folders
    pdf2md-local inbox        convert everything in the input folder now
    pdf2md-local convert X    convert PDFs by path (they stay where they are)
    pdf2md-local doctor       check folders, MinerU, GPU and the watcher

Rules it never breaks: it never overwrites an existing md, never deletes a PDF,
never puts a PDF in the output folder, and never uploads a PDF anywhere (MinerU
runs locally, never with --remote).
"""

import argparse
import codecs
import collections
import datetime
import json
import os
import platform
import html
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import zipfile
from pathlib import Path
from types import SimpleNamespace

__version__ = "1.2.0"
APP = "pdf2md-local"

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
HOME = Path.home()

LOG_NAME = "pdf2md-local_log.txt"
LOCK_NAME = ".pdf2md-local.lock"
LOCK_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
STATUS_FOLDERS = ("done", "skipped", "failed")


# ---- naming ----
# These rules decide every output folder name. Changing them would make existing
# mds look missing, so they are frozen and covered by tests/test_naming.py.
# Since 3.2 every name is lower case ("okafor_2021_variational-…"); lookups ignore case,
# so folders made earlier with a capital surname are still found.

STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "for", "and", "or", "to", "with",
    "by", "from", "at", "as", "via", "using", "is", "are", "its", "into",
    "under", "between", "through", "toward", "towards", "vs", "versus",
}

# Zotero's default file name: "Author - Year - Title"
ZOTERO_PATTERN = re.compile(r"^(?P<author>.+?) - (?P<year>\d{4}) - (?P<title>.+)$")


def first_author_surname(author_text):
    """First author's surname from "Mendes and Okafor", "Rivera et al.".

    Also handles Zotero's Chinese UI: "Ålund …等", "Ålund 等", "Mendes 和 Okafor".
    """
    text = author_text.strip()
    text = re.sub(r"\s*(?:et al\.?|(?:…|\.{3})?\s*等人?)$", "", text)
    first_author = re.split(r"\s+and\s+|\s*[和與与&]\s*|,|，|、", text)[0]
    # Leading initials are not part of the surname: "J. Hollis", "J.K. Hollis", "J.-K. Hollis", "J Hollis"
    first_author = re.sub(r"^(?:[A-Z](?:\.-?[A-Z])*\.?\s+)+(?=\S)", "", first_author.strip())
    surname = first_author.strip().replace(" ", "-")
    return surname


def short_title(title_text):
    """First 4 meaningful English words of the title, lower case, joined by hyphens."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", title_text)
    meaningful = []
    for word in words:
        lower_word = word.lower()
        if lower_word in STOPWORDS:
            continue
        meaningful.append(lower_word)
        if len(meaningful) == 4:
            break
    if len(meaningful) < 3:
        return None
    joined = "-".join(meaningful)
    return joined


def output_name_for(pdf_path):
    """Zotero names become "surname_year_short-title"; anything else keeps its name with _ for spaces. All lower case."""
    stem = Path(pdf_path).stem
    plain_name = stem.replace(" ", "_").lower()
    match = ZOTERO_PATTERN.match(stem)
    if match is None:
        return plain_name
    surname = first_author_surname(match.group("author"))
    year = match.group("year")
    title_part = short_title(match.group("title"))
    if title_part is None:
        return plain_name
    zotero_name = f"{surname}_{year}_{title_part}".lower()
    return zotero_name


def is_zotero_name(pdf_path):
    """True when the file name itself gives author, year and title (the frozen rule applies)."""
    match = ZOTERO_PATTERN.match(Path(pdf_path).stem)
    return match is not None and short_title(match.group("title")) is not None


def md_path_for(pdf_path, output_root):
    """Where the file-name rule puts the md (folders made before metadata naming, and Zotero names)."""
    name = output_name_for(pdf_path)
    return Path(output_root) / name / f"{name}.md"


def existing_folder_md(output_root, name):
    """The md of folder `name`, matching the folder name without regard to case (Linux is case-sensitive)."""
    output_root = Path(output_root)
    exact = output_root / name / f"{name}.md"
    if exact.exists():
        return exact
    wanted = name.casefold()
    try:
        for child in output_root.iterdir():
            if child.name.casefold() == wanted:
                for md in child.glob("*.md"):
                    if md.stem.casefold() == wanted:
                        return md
    except OSError:
        pass
    return None


# ---- metadata naming ----
# A file name that says nothing ("2101.01234v1.pdf", "hollis.pdf") is replaced by a
# name read from the paper itself, after conversion:
#   1. a DOI in the PDF's metadata or on its first page -> Crossref
#   2. an arXiv id in the file name or first page        -> arXiv
#   3. the md's title                                    -> Crossref title search
#   4. offline: title, first author and year from the md's first page
# Online answers are accepted only when their title matches the md's own title, so
# a DOI from the reference list cannot rename the paper. Only the DOI, arXiv id or
# title is sent to those services; the PDF is never uploaded.
# The index file in the output folder maps each PDF's SHA-256 to its folder, so the
# same PDF is found again whatever its file name.

INDEX_NAME = "pdf2md-local_index.json"
HTTP_TIMEOUT = 15
USER_AGENT = f"pdf2md-local/{__version__} (local file naming; python-urllib)"
DOI_PATTERN = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>()\[\]{},;]+)", re.I)
# Only the PDF's own metadata: XMP keys, or a /doi key of the document info dictionary ("/doi (10....)").
# A bare "/doi" also matched link annotations such as "https://doi.org/10...." in the reference list, so a
# paper that cited an already converted paper was skipped as that paper.
PDF_DOI_PATTERN = re.compile(
    rb"(?:(?:prism:doi|pdfx:doi|crossmark:doi|dc:identifier)[^0-9]{0,40}|/doi\s*\(\s*)(10\.\d{4,9}/[^\s<>()\"'\[\]]+)", re.I)
# Any DOI in the raw bytes, link annotations included. These may be the paper's own DOI or cited ones, so they
# are used only together with a matching title (see find_existing_by_doi).
PDF_LINKED_DOI_PATTERN = re.compile(rb"(?:/doi|doi\.org/)[^0-9]{0,40}(10\.\d{4,9}/[^\s<>()\"'\[\]]+)", re.I)
# A title belongs to the metadata block it is in: an XMP packet, or a dictionary such as the document info.
# Embedded figures carry XMP packets of their own ("Adobe Illustrator Artwork", "R Graphics Output"), so the
# first title in the file is not necessarily the paper's.
PDF_XMP_PACKET = re.compile(rb"<rdf:RDF\b.*?</rdf:RDF>", re.I | re.S)
PDF_XMP_TITLE = re.compile(rb"<dc:title\b[^>]*>(.*?)</dc:title>", re.I | re.S)
PDF_XMP_LI = re.compile(rb"<rdf:li\b[^>]*>(.*?)</rdf:li>", re.I | re.S)
PDF_INFO_TITLE_KEY = re.compile(rb"/Title\s*([(<])")
# Titles that PDF software writes by itself; never a paper title
JUNK_TITLE = re.compile(
    r"^(microsoft (word|powerpoint|excel)\b|adobe illustrator|r graphics output|untitled|uuid:|xmp\.did:)"
    r"|\.(docx?|pptx?|tex|dvi|indd|qxd|ps|eps|ai|pdf)\b", re.I)
ARXIV_HEAD_PATTERN = re.compile(r"arxiv[:\s]*(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
ARXIV_NAME_PATTERN = re.compile(r"^(?:arxiv[_.-]?)?(\d{4}\.\d{4,5})(?:v\d+)?$", re.I)
REFERENCES_HEADING = re.compile(r"^#+\s*(references|bibliography|參考文獻|参考文献)\b", re.I | re.M)
YEAR_HINT = re.compile(r"(?:©|\(c\)|copyright|received|published|accepted|online)[^\n]{0,40}?(?<!\d)((?:19|20)\d{2})(?!\d)", re.I)
AFFILIATION_WORDS = re.compile(r"\s(?:The\s)?(?:University|Universit\w*|Institute|College|School|Department|Dept\.|"
                               r"Center|Centre|Laboratory|Faculty|Hospital|Academy)\b")
VOLUME_YEAR = re.compile(r"\d+\s*\(\d+\)\s*[,:]\s*((?:19|20)\d{2})(?!\d)")
UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def file_sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_doi(doi):
    return doi.rstrip(".").lower() if doi else None


def strip_markup(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\$[^$]*\$", " ", text)
    text = re.sub(r"[*_`#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def md_body(md_text):
    """The md without its YAML front matter."""
    if md_text.startswith("---\n"):
        end = md_text.find("\n---", 4)
        if end != -1:
            return md_text[end + 4:].lstrip("\n")
    return md_text


def md_head(md_text, limit=6000):
    """The first page or so, never the reference list (its DOIs belong to other papers)."""
    body = md_body(md_text)
    match = REFERENCES_HEADING.search(body)
    if match:
        body = body[:match.start()]
    return body[:limit]


def md_title(head):
    for line in head.splitlines():
        if line.startswith("# "):
            title = strip_markup(line[2:])
            if len(title.split()) >= 3:
                return title
    return None


def md_first_author(head, title):
    """Surname of the first name on the line after the title ("J. Hollis $^{a,b}$ , K. Park" -> "Hollis")."""
    lines = head.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# ") and strip_markup(line[2:]) == title:
            following = [text for text in lines[index + 1:index + 4] if text.strip()]
            break
    else:
        return None
    if not following or following[0].startswith("#"):
        return None
    author_line = strip_markup(following[0])
    author_line = re.sub(r"[\d∗*†‡§¶]+", " ", author_line)
    if len(author_line) > 300:
        return None
    first = re.split(r",|;|·|\s+and\s+|&", author_line)[0]
    first = AFFILIATION_WORDS.split(first)[0]  # "Maria Rivera University of Washington" -> "Maria Rivera"
    words = [word for word in re.findall(r"[^\W\d_][\w'’.-]*", first) if not word.endswith(".")]
    if not words or len(first.split()) > 6:
        return None
    surname = words[-1]
    return surname if len(surname) >= 2 else None


def normalize_title(title):
    return " ".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", title or "").lower()))


def titles_match(title_a, title_b, threshold=0.85):
    import difflib
    a, b = normalize_title(title_a), normalize_title(title_b)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 20 and longer.startswith(shorter):
        return True  # one of them has a subtitle
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def http_get(url, accept="application/json"):
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read()


def crossref_record(item):
    titles = item.get("title") or []
    authors = item.get("author") or []
    year = None
    # The print year first: it is what Zotero and citations use (issued is often the online-first year)
    for key in ("published-print", "issued", "published-online", "created"):
        parts = (item.get(key) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    surname = None
    for author in authors:
        if author.get("family"):
            surname = author["family"]
            break
    return {"title": titles[0] if titles else None, "surname": surname,
            "year": str(year) if year else None, "doi": clean_doi(item.get("DOI"))}


def crossref_by_doi(doi):
    import urllib.parse
    try:
        data = json.loads(http_get("https://api.crossref.org/works/" + urllib.parse.quote(doi)))
        return crossref_record(data["message"])
    except Exception:
        return None


def crossref_by_title(title):
    import urllib.parse
    query = urllib.parse.urlencode({"query.bibliographic": title, "rows": 3})
    try:
        data = json.loads(http_get("https://api.crossref.org/works?" + query))
        return [crossref_record(item) for item in data["message"]["items"]]
    except Exception:
        return []


def arxiv_by_id(arxiv_id):
    import xml.etree.ElementTree as ElementTree
    atom = "{http://www.w3.org/2005/Atom}"
    try:
        raw = http_get(f"https://export.arxiv.org/api/query?id_list={arxiv_id}", accept="application/atom+xml")
        entry = ElementTree.fromstring(raw).find(atom + "entry")
        title = re.sub(r"\s+", " ", entry.findtext(atom + "title") or "").strip()
        author = entry.find(atom + "author")
        name = (author.findtext(atom + "name") or "").strip() if author is not None else ""
        published = entry.findtext(atom + "published") or ""
        doi = entry.findtext("{http://arxiv.org/schemas/atom}doi")
        if not title or not name:
            return None
        return {"title": title, "surname": name.split()[-1], "year": published[:4] or None,
                "doi": clean_doi(doi), "arxiv": arxiv_id}
    except Exception:
        return None


def pdf_chunks(pdf_path):
    """The first 2 MiB and, for larger files, the last 1 MiB (where the document info often is)."""
    try:
        size = Path(pdf_path).stat().st_size
        with open(pdf_path, "rb") as handle:
            chunks = [handle.read(2 << 20)]
            if size > 3 << 20:
                handle.seek(-(1 << 20), os.SEEK_END)
                chunks.append(handle.read())
    except OSError:
        return []
    return chunks


def pdf_embedded_dois(pdf_path):
    """DOIs in the PDF's own metadata (XMP or document info), read from the raw bytes."""
    return list(dict.fromkeys(doi for doi, _ in pdf_metadata_records(pdf_path)))


def arxiv_id_for(pdf_path, head):
    match = ARXIV_NAME_PATTERN.match(Path(pdf_path).stem) or ARXIV_HEAD_PATTERN.search(head)
    return match.group(1) if match else None


def offline_year(pdf_path, head, arxiv_id):
    # A citation line such as "測驗學刊, 71(1), 2024" is the publication year itself
    match = VOLUME_YEAR.search(head) or YEAR_HINT.search(head)
    if match:
        return match.group(1)
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", Path(pdf_path).stem)
    if match:
        return match.group(1)
    if arxiv_id:
        return "20" + arxiv_id[:2]
    return None


def detect_metadata(pdf_path, md_text, online=True):
    """Title, first author's surname, year and DOI of a converted PDF; missing parts are None."""
    head = md_head(md_text)
    title = md_title(head)
    arxiv_id = arxiv_id_for(pdf_path, head)
    if online:
        embedded = pdf_embedded_dois(pdf_path)
        on_page = [clean_doi(doi) for doi in DOI_PATTERN.findall(head)]
        for doi in list(dict.fromkeys(embedded + on_page))[:5]:
            record = crossref_by_doi(doi)
            # A DOI from the PDF's own metadata is trusted when the md has no title to compare with
            trusted = title is None and doi in embedded
            if record and record["title"] and (trusted or titles_match(record["title"], title)):
                return {**record, "source": "crossref-doi"}
        if arxiv_id:
            record = arxiv_by_id(arxiv_id)
            if record and (title is None or titles_match(record["title"], title)):
                return {**record, "source": "arxiv"}
        if title:
            for record in crossref_by_title(title):
                if record["title"] and titles_match(record["title"], title, threshold=0.92):
                    return {**record, "source": "crossref-title"}
    surname = md_first_author(head, title) if title else None
    return {"title": title, "surname": surname, "year": offline_year(pdf_path, head, arxiv_id),
            "doi": None, "arxiv": arxiv_id, "source": "md" if title else None}


def name_from_metadata(meta):
    """"Surname_Year_short-title", the same shape as Zotero names; None if a part is missing."""
    if not meta or not (meta.get("surname") and meta.get("year") and meta.get("title")):
        return None
    title_part = short_title(meta["title"])
    if title_part is None:
        return None
    surname = UNSAFE_NAME_CHARS.sub("", unicodedata.normalize("NFC", meta["surname"]).strip()).replace(" ", "-")
    if not surname:
        return None
    return f"{surname}_{meta['year']}_{title_part}".lower()


def choose_name(pdf_path, md_text, config):
    """Folder name and metadata. Zotero file names keep the frozen rule; others use the paper's metadata."""
    online = config.get("metadata_lookup", True)
    try:
        meta = detect_metadata(pdf_path, md_text, online=online)
    except Exception:
        meta = {}
    if is_zotero_name(pdf_path):
        return output_name_for(pdf_path), meta, "file name (Zotero)"
    name = name_from_metadata(meta)
    if name:
        return name, meta, meta.get("source") or "metadata"
    return output_name_for(pdf_path), meta, "file name"


# ---- index ----

def index_path(output_root):
    return Path(output_root) / INDEX_NAME


_INDEX_OVERRIDE = {}  # output folder -> index to use instead of the file (recheck's what-if questions)


def load_index(output_root):
    override = _INDEX_OVERRIDE.get(str(Path(output_root)))
    if override is not None:
        return override
    try:
        data = json.loads(index_path(output_root).read_text(encoding="utf-8"))
        if isinstance(data.get("entries"), list):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": 1, "entries": []}


def save_index(output_root, index):
    """Atomic replace, so a reader never sees half a file."""
    path = index_path(output_root)
    temp = path.with_name(path.name + ".tmp")
    text = json.dumps(index, ensure_ascii=False, indent=1) + "\n"
    temp.write_text(text, encoding="utf-8")
    # A sync client (Dropbox) briefly locks a file it is uploading; wait and retry
    for attempt in range(8):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    path.write_text(text, encoding="utf-8")
    try:
        temp.unlink()
    except OSError:
        pass


def merge_index_entry(index, entry):
    for existing in index["entries"]:
        if existing.get("folder") == entry.get("folder") and existing.get("sha256") == entry.get("sha256"):
            existing.update({key: value for key, value in entry.items() if value})
            break
    else:
        index["entries"].append(entry)


def add_index_entry(output_root, entry):
    index = load_index(output_root)
    merge_index_entry(index, entry)
    save_index(output_root, index)


def folder_md(output_root, folder):
    return Path(output_root) / folder / f"{folder}.md"


def folder_taken(output_root, name):
    """Is there a folder of this name, in any letter case? (Two names differing only in case would clash on Windows and macOS.)"""
    wanted = name.casefold()
    try:
        return any(child.name.casefold() == wanted for child in Path(output_root).iterdir())
    except OSError:
        return False


def find_md(pdf_path, output_root, sha=None):
    """The existing md for this PDF: by content hash in the index, then by the file-name rule."""
    output_root = Path(output_root)
    pdf_path = Path(pdf_path)
    if sha is None and pdf_path.is_file():
        sha = file_sha256(pdf_path)
    if sha:
        for entry in load_index(output_root)["entries"]:
            if entry.get("sha256") == sha and entry.get("folder"):
                candidate = existing_folder_md(output_root, entry["folder"])
                if candidate:
                    return candidate
    return existing_folder_md(output_root, output_name_for(pdf_path))


def find_md_by_doi(output_root, dois, titles=None, require_title=False):
    """The md of an indexed paper with one of these DOIs.

    titles: this PDF's title, or several candidates. When candidates and the indexed title are both known,
    one candidate must match, so a DOI picked up by mistake cannot make two papers one.
    require_title: only accept an entry whose title matches (for DOIs that may be cited ones)."""
    wanted = {doi for doi in dois if doi}
    if not wanted:
        return None
    candidates = [titles] if isinstance(titles, str) else [title for title in (titles or []) if title]
    found = []
    for entry in load_index(output_root)["entries"]:
        if entry.get("doi") in wanted and entry.get("folder"):
            if require_title and not (candidates and entry.get("title")):
                continue
            if candidates and entry.get("title") and not any(titles_match(entry["title"], c) for c in candidates):
                continue
            candidate = existing_folder_md(output_root, entry["folder"])
            if candidate and candidate not in found:
                found.append(candidate)
    # Several documents can share a DOI (a paper and a summary of it, kept apart): the longest one
    # is the paper
    return max(found, key=lambda md: md_body_length(md) or 0) if found else None


def pdf_linked_dois(pdf_path):
    """DOIs anywhere in the raw bytes, including links in the reference list; never trusted without a title."""
    found = []
    for chunk in pdf_chunks(pdf_path):
        found.extend(clean_doi(m.group(1).decode("latin-1")) for m in PDF_LINKED_DOI_PATTERN.finditer(chunk))
    return list(dict.fromkeys(found))


def find_existing_by_doi(output_root, pdf_path):
    """Before conversion: each metadata DOI, checked against the title of the same metadata block when it has
    one; then DOIs from links, accepted only when one of the PDF's titles matches the indexed title."""
    if any(supplement_kind(title) for title in pdf_embedded_titles(pdf_path)[:1]):
        return None
    for doi, title in pdf_metadata_records(pdf_path):
        existing = find_md_by_doi(output_root, [doi], title)
        if existing is not None:
            return existing
    titles = pdf_embedded_titles(pdf_path)
    if titles:
        return find_md_by_doi(output_root, pdf_linked_dois(pdf_path), titles, require_title=True)
    return None


PDF_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}


def pdf_literal_string(data, start):
    """The bytes of the PDF literal string whose "(" is at data[start]: escapes, octal codes and balanced
    parentheses; None when it is not terminated."""
    out, depth, i = bytearray(), 0, start + 1
    while i < len(data):
        c = data[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= len(data):
                break
            c = data[i]
            if c in PDF_ESCAPES:
                out += PDF_ESCAPES[c]
            elif 0x30 <= c <= 0x37:
                octal = re.match(rb"[0-7]{1,3}", data[i:i + 3]).group(0)
                out.append(int(octal, 8) & 0xFF)
                i += len(octal) - 1
            elif c == 0x0D:  # line continuation
                if data[i + 1:i + 2] == b"\n":
                    i += 1
            elif c != 0x0A:
                out.append(c)
        elif c == 0x28:
            depth += 1
            out.append(c)
        elif c == 0x29:
            if depth == 0:
                return bytes(out)
            depth -= 1
            out.append(c)
        else:
            out.append(c)
        i += 1
    return None


def pdf_text(raw):
    """Text of a PDF string: UTF-16 with a byte-order mark, else UTF-8, else PDFDocEncoding (close to Latin-1)."""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="ignore")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="ignore")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def clean_title(text):
    """A usable paper title, or None for empty, too short or software-made titles."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text) if "&" in text else text
    text = " ".join(text.split())
    if len(text.split()) < 3 or JUNK_TITLE.search(text):
        return None
    return text


def xmp_title(packet):
    """dc:title of one XMP packet."""
    match = PDF_XMP_TITLE.search(packet)
    if not match:
        return None
    for item in PDF_XMP_LI.findall(match.group(1)) or [match.group(1)]:
        title = clean_title(item.decode("utf-8", errors="ignore"))
        if title:
            return title
    return None


def info_title(block):
    """/Title of a PDF dictionary, as a literal or hex string."""
    for match in PDF_INFO_TITLE_KEY.finditer(block):
        if match.group(1) == b"(":
            raw = pdf_literal_string(block, match.start(1))
        else:
            end = block.find(b">", match.start(1))
            digits = re.sub(rb"\s", b"", block[match.start(1) + 1:end]) if end > 0 else b""
            try:
                raw = bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii"))
            except ValueError:
                raw = None
        title = clean_title(pdf_text(raw)) if raw else None
        if title:
            return title
    return None


def enclosing_dict(data, pos):
    """The dictionary << ... >> around data[pos] (a document info holds no nested dictionaries)."""
    start, end = data.rfind(b"<<", 0, pos), data.find(b">>", pos)
    return data[start:end + 2] if start >= 0 and end >= 0 else b""


def pdf_metadata_records(pdf_path):
    """(doi, title) for each DOI in the PDF's own metadata. The title comes from the same XMP packet or
    dictionary, or is None when that block has no usable one."""
    records = []
    for chunk in pdf_chunks(pdf_path):
        packets = [(m.start(), m.end()) for m in PDF_XMP_PACKET.finditer(chunk)]
        for match in PDF_DOI_PATTERN.finditer(chunk):
            doi = clean_doi(match.group(1).decode("latin-1"))
            packet = next((chunk[s:e] for s, e in packets if s <= match.start() < e), None)
            title = xmp_title(packet) if packet is not None else info_title(enclosing_dict(chunk, match.start()))
            records.append((doi, title))
    return list(dict.fromkeys(records))


def pdf_embedded_titles(pdf_path):
    """Every usable title in the PDF's metadata: XMP packets and /Title entries (the document info, but also
    figures and bookmarks). Only used with a DOI from the same PDF and a matching indexed title."""
    titles = []
    for chunk in pdf_chunks(pdf_path):
        titles.extend(xmp_title(m.group(0)) for m in PDF_XMP_PACKET.finditer(chunk))
        titles.extend(info_title(chunk[m.start():m.start() + 4096]) for m in PDF_INFO_TITLE_KEY.finditer(chunk))
    return list(dict.fromkeys(title for title in titles if title))


def existing_md_title(md_file):
    try:
        return md_title(md_head(md_file.read_text(encoding="utf-8")))
    except OSError:
        return None


# ---- platform folders ----

def config_dir():
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
    elif IS_MAC:
        base = HOME / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
    return base / APP


def cache_dir():
    """Machine-local folder for the progress file and the watcher's own lock (never synced)."""
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    elif IS_MAC:
        base = HOME / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache")
    folder = base / APP
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def no_window_flag():
    """Keep background runs on Windows from flashing a console window for each tool call."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


# ---- tools ----

TOOL_INFO = {
    "mineru": {"exe": "mineru-kit", "package": "mineru", "env": "mineru", "conda_envs": ["pdf2md-mineru"]},
}

CONDA_BASES = ["miniconda3", "anaconda3", "miniforge3", "mambaforge", "micromamba", ".conda"]
APP_HOME = HOME / ".pdf2md-local"          # the installer puts the app and MinerU here
INSTALL_ENVS = APP_HOME / "envs"


def exe_in_env(env_dir, exe_name):
    if IS_WINDOWS:
        return Path(env_dir) / "Scripts" / f"{exe_name}.exe"
    return Path(env_dir) / "bin" / exe_name


def python_in_env(env_dir):
    if IS_WINDOWS:
        return Path(env_dir) / "python.exe"
    return Path(env_dir) / "bin" / "python"


def env_dir_of_exe(exe_path):
    """The environment folder an executable lives in (…/Scripts/x.exe or …/bin/x)."""
    return Path(exe_path).parent.parent if IS_WINDOWS else Path(exe_path).parent.parent


def detect_tool(tool):
    """Find a tool's executable and its environment's python. Returns dict or None."""
    info = TOOL_INFO[tool]
    candidates = []
    # The installer's own environment first, then conda environments of that name
    conda_envs = [HOME / base / "envs" / name for name in info.get("conda_envs", []) for base in CONDA_BASES]
    for env_dir in [INSTALL_ENVS / info["env"]] + conda_envs:
        candidates.append(exe_in_env(env_dir, info["exe"]))
    on_path = shutil.which(info["exe"])
    if on_path:
        candidates.append(Path(on_path))
    for exe_path in candidates:
        if exe_path.is_file():
            env_dir = env_dir_of_exe(exe_path)
            python_path = python_in_env(env_dir)
            if not python_path.is_file():
                python_path = Path(sys.executable)
            return {"cmd": str(exe_path), "python": str(python_path)}
    return None


def tool_paths(config, tool):
    """(exe, python) for a tool from the config, falling back to detection."""
    entry = config.get("tools", {}).get(tool) or {}
    if entry.get("cmd") and Path(entry["cmd"]).is_file():
        python_text = entry.get("python") or str(python_in_env(env_dir_of_exe(entry["cmd"])))
        return Path(entry["cmd"]), Path(python_text)
    found = detect_tool(tool)
    if found is None:
        raise UserError(
            f"MinerU was not found on this machine. Run the installer (install.sh / install.ps1), "
            f"or set tools.{tool}.cmd in {config_path()}."
        )
    return Path(found["cmd"]), Path(found["python"])


def tool_env_vars():
    """Environment for MinerU: UTF-8 (non-UTF-8 Windows locales such as cp950 break its model files), and no
    usage statistics sent anywhere (MinerU's Doclib telemetry, Hugging Face Hub telemetry)."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["MINERU_TELEMETRY_DISABLED"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    return env


def run_python(python_path, code, timeout=120):
    result = subprocess.run(
        [str(python_path), "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=tool_env_vars(), creationflags=no_window_flag(), timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:])
    return result.stdout.strip()


def tool_version(config, tool):
    _, python_path = tool_paths(config, tool)
    package = TOOL_INFO[tool]["package"]
    return run_python(python_path, f"from importlib.metadata import version; print(version('{package}'))")


# ---- hardware: the minimum requirements ----
# MinerU 4.x runs its slow step (the VLM) through llama.cpp with Vulkan (Windows, Linux) or
# Metal/MLX (Apple Silicon), not through CUDA. Measured with a 6-page paper:
#   NVIDIA RTX 3060 (discrete)       54 s     about 9 s/page
#   Intel UHD Graphics 770 (iGPU)    851 s    about 142 s/page, same output
#   CPU only, 8 of 24 threads        ~10 h    (1 of 86 blocks in 7 min; stopped)
# So pdf2md-local needs a GPU: it does not install on CPU-only machines.

MIN_DISK_GB = 15            # MinerU, its Python packages and models, with room to spare
MIN_DISK_GB_INSTALLED = 5   # MinerU already installed: its models (downloaded on first use) and the md files
MIN_RAM_GB = {"discrete": 8, "apple": 8, "integrated": 16}   # integrated GPUs share system memory
MIN_VRAM_GB = 6             # MinerU itself treats smaller discrete GPUs as unreliable
MIN_MACOS = (14, 0)         # MinerU's MLX engine needs macOS 14+

# Integrated GPUs that meet the minimum (Iris Xe class or better). "slow" is only used for an
# unrecognised GPU let through with --accept-gpu.
INTEGRATED_GPUS = [
    (re.compile(r"radeon\s*(?:\d{3,4}m)\b", re.I), "ok", "AMD Radeon 680M/780M/880M/890M class"),
    (re.compile(r"\barc\b", re.I), "ok", "Intel Arc"),
    (re.compile(r"iris\s*xe", re.I), "ok", "Intel Iris Xe"),
]
# Below the minimum although they work: measured 851 s for 6 pages on a UHD 770 (30 pages ≈ 70 min)
TOO_SLOW_GPUS = re.compile(r"uhd\s*graphics\s*7\d\d", re.I)
# Definitely not enough: old Intel HD / UHD 6xx, old AMD APUs, and software renderers (= CPU only)
TOO_WEAK_GPUS = re.compile(r"(?:\bhd\s*graphics\b|uhd\s*graphics\s*6\d\d|radeon\s*(?:vega|r\d)\b|\bgma\b|"
                           r"microsoft basic|llvmpipe|softpipe|swiftshader|software)", re.I)
DISCRETE_GPUS = re.compile(r"(?:nvidia|geforce|quadro|tesla|\brtx\b|\bgtx\b|radeon\s*(?:rx|pro)\b|instinct|arc\s*a\d{3})", re.I)


def classify_gpu(name, vram_gb=None, machine=None):
    """("discrete"|"apple"|"integrated"|None, verdict "ok"|"slow"|"no", reason) for one GPU name."""
    # "Intel(R) Iris(R) Xe", "Arc(TM) A770": trademark marks would break the patterns
    text = re.sub(r"\((?:r|tm)\)|[®™]", "", name or "", flags=re.I).strip()
    if machine == "apple" or text.lower().startswith("apple"):
        return "apple", "ok", "Apple Silicon GPU (Metal/MLX)"
    if TOO_WEAK_GPUS.search(text):
        return None, "no", "too old or too weak for MinerU's VLM, or a software (CPU) renderer"
    if DISCRETE_GPUS.search(text):
        if vram_gb is not None and vram_gb < MIN_VRAM_GB:
            return "discrete", "no", f"only {vram_gb:.0f} GB of video memory (needs {MIN_VRAM_GB} GB)"
        note = "" if vram_gb is not None else f" (video memory unknown; needs {MIN_VRAM_GB} GB)"
        return "discrete", "ok", "discrete GPU" + note
    if TOO_SLOW_GPUS.search(text):
        return "integrated", "no", "below the minimum: Intel UHD 7xx measured about 142 s per page (30 pages ≈ 70 min)"
    for pattern, verdict, label in INTEGRATED_GPUS:
        if pattern.search(text):
            return "integrated", verdict, label
    if not text:
        return None, "no", "no name reported"
    # e.g. "AMD Radeon(TM) Graphics" (a strong 780M or a weak 2-core APU) or an unnamed PCI id
    return "integrated", "unknown", "not recognised; if it is one of the supported GPUs, use --accept-gpu"


def system_ram_gb():
    try:
        if IS_WINDOWS:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / 2**30
        if IS_MAC:
            return int(run_tool_text(["sysctl", "-n", "hw.memsize"]).strip()) / 2**30
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 2**20
    except Exception:
        pass
    return None


def run_tool_text(command):
    """stdout of a system tool, or "" if it is missing or fails."""
    try:
        return run_quiet(command).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def list_gpus():
    """[(name, vram_gb or None)] as the operating system reports them; no extra packages needed."""
    gpus = []
    nvidia = shutil.which("nvidia-smi")
    nvidia_memory = {}
    if nvidia:
        for line in run_tool_text([nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]).splitlines():
            name, _, memory = line.rpartition(",")
            if name.strip():
                nvidia_memory[name.strip()] = float(memory) / 1024 if memory.strip().isdigit() else None
    if IS_WINDOWS:
        text = run_tool_text(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"])
        names = [line.strip() for line in text.splitlines() if line.strip()]
    elif IS_MAC:
        names = ["Apple GPU"] if platform.machine() == "arm64" else []
        if not names:
            profile = run_tool_text(["system_profiler", "SPDisplaysDataType"])
            names = [line.split(":", 1)[1].strip() for line in profile.splitlines() if "Chipset Model" in line]
    else:
        names = []
        if shutil.which("lspci"):
            for line in run_tool_text(["lspci"]).splitlines():
                if re.search(r"VGA|3D controller|Display controller", line):
                    names.append(line.split(":", 2)[-1].strip())
    for name in names:
        vram = next((memory for nvidia_name, memory in nvidia_memory.items()
                     if nvidia_name.lower() in name.lower() or name.lower() in nvidia_name.lower()), None)
        if vram is None and not IS_WINDOWS and not IS_MAC:
            vram = linux_vram_gb(name)
        gpus.append((name, vram))
    for nvidia_name, memory in nvidia_memory.items():
        if not any(nvidia_name.lower() in name.lower() or name.lower() in nvidia_name.lower() for name in names):
            gpus.append((nvidia_name, memory))
    return gpus


def linux_vram_gb(name):
    """AMD discrete cards report their memory in sysfs; others return None."""
    if "amd" not in name.lower() and "radeon" not in name.lower():
        return None
    for path in Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"):
        try:
            return int(path.read_text().strip()) / 2**30
        except (OSError, ValueError):
            continue
    return None


def hardware_report(gpus=None, ram_gb=None, disk_free_gb=None, macos_version=None, machine=None, accept_gpu=False,
                    mineru_installed=None):
    """Does this machine meet the minimum requirements? Returns a dict: ok, tier, verdict, best GPU, problems, notes.

    Arguments are only for tests; normally everything is measured here.
    """
    if machine is None:
        machine = "apple" if IS_MAC and platform.machine() == "arm64" else ("intel-mac" if IS_MAC else None)
    if gpus is None:
        gpus = list_gpus()
    if ram_gb is None:
        ram_gb = system_ram_gb()
    if disk_free_gb is None:
        disk_free_gb = shutil.disk_usage(str(HOME)).free / 2**30
        if mineru_installed is None:
            # Installing MinerU itself uses most of the 15 GB, so checking again afterwards needs far less
            mineru_installed = detect_tool("mineru") is not None
    if macos_version is None and IS_MAC:
        macos_version = tuple(int(part) for part in (platform.mac_ver()[0] or "0").split(".")[:2] if part.isdigit())

    rated = []
    for name, vram in gpus:
        tier, verdict, reason = classify_gpu(name, vram, "apple" if machine == "apple" else None)
        if verdict == "unknown" and accept_gpu:
            verdict, reason = "slow", "accepted with --accept-gpu; speed unknown"
        rated.append({"name": name, "vram_gb": vram, "tier": tier, "verdict": verdict, "reason": reason})
    order = {("discrete", "ok"): 0, ("apple", "ok"): 1, ("integrated", "ok"): 2, ("integrated", "slow"): 3}
    usable = sorted((gpu for gpu in rated if gpu["verdict"] in ("ok", "slow")), key=lambda gpu: order[(gpu["tier"], gpu["verdict"])])
    problems, notes = [], []
    best = usable[0] if usable else None
    if machine == "intel-mac":
        problems.append("Intel Macs are not supported (MinerU needs Apple Silicon on macOS).")
    if best is None:
        found = ", ".join(f"{gpu['name']} ({gpu['reason']})" for gpu in rated) or "no GPU found"
        problems.append(f"No supported GPU: {found}. pdf2md-local needs a GPU; CPU-only machines are not supported.")
    else:
        need = MIN_RAM_GB[best["tier"]]
        if ram_gb is not None and ram_gb + 0.5 < need:
            problems.append(f"{ram_gb:.0f} GB of memory; with this GPU pdf2md-local needs {need} GB.")
        if best["tier"] == "apple" and macos_version and tuple(macos_version) < MIN_MACOS:
            problems.append(f"macOS {'.'.join(map(str, macos_version))}; MinerU on Apple Silicon needs macOS 14 or newer.")
        if best["verdict"] == "slow":
            notes.append(f"{best['name']}: works but slowly ({best['reason']}). Fine for a few papers, e.g. overnight.")
    if mineru_installed:
        if disk_free_gb is not None and disk_free_gb < MIN_DISK_GB_INSTALLED:
            problems.append(f"{disk_free_gb:.0f} GB free disk space; MinerU's models and the md files need "
                            f"{MIN_DISK_GB_INSTALLED} GB.")
    elif disk_free_gb is not None and disk_free_gb < MIN_DISK_GB:
        problems.append(f"{disk_free_gb:.0f} GB free disk space; installing MinerU needs {MIN_DISK_GB} GB.")
    return {"ok": not problems, "best": best, "gpus": rated, "ram_gb": ram_gb, "disk_free_gb": disk_free_gb,
            "problems": problems, "notes": notes}


def format_hardware_report(report):
    lines = []
    for gpu in report["gpus"] or [{"name": "(no GPU found)", "verdict": "no", "reason": "", "vram_gb": None}]:
        mark = {"ok": "OK  ", "slow": "SLOW", "no": "NO  ", "unknown": "??  "}[gpu["verdict"]]
        memory = f", {gpu['vram_gb']:.0f} GB" if gpu.get("vram_gb") else ""
        lines.append(f"  [{mark}] {gpu['name']}{memory}: {gpu['reason']}")
    if report["ram_gb"] is not None:
        lines.append(f"  memory: {report['ram_gb']:.0f} GB")
    if report["disk_free_gb"] is not None:
        lines.append(f"  free disk: {report['disk_free_gb']:.0f} GB")
    for note in report["notes"]:
        lines.append(f"  note: {note}")
    for problem in report["problems"]:
        lines.append(f"  problem: {problem}")
    lines.append("  verdict: " + ("meets the minimum requirements" if report["ok"] else "does not meet the minimum requirements"))
    return "\n".join(lines)


LLAMA_DEVICE_CODE = (
    "import pathlib, subprocess, sys, mineru_llama_cpp\n"
    "folder = pathlib.Path(mineru_llama_cpp.__file__).parent / 'bin'\n"
    "exe = next((p for p in folder.glob('llama-server*') if p.suffix in ('', '.exe')), None)\n"
    "if exe is None: sys.exit('no llama-server')\n"
    "print(subprocess.run([str(exe), '--list-devices'], capture_output=True, text=True, timeout=60).stdout)\n"
)


def tool_device(config, tool):
    """("gpu"|"cpu", description): which accelerators MinerU's VLM engine (llama.cpp) can use here."""
    _, python_path = tool_paths(config, tool)
    try:
        text = run_python(python_path, LLAMA_DEVICE_CODE)
    except Exception:
        # Not MinerU's llama.cpp build: fall back to what the operating system reports
        report = hardware_report()
        return ("gpu", report["best"]["name"]) if report["best"] else ("cpu", "no supported GPU")
    # "  Vulkan0: Intel(R) UHD Graphics 770 (7989 MiB, 7333 MiB free)"
    devices = [re.sub(r"\s*\(\d+\s*MiB.*$", "", match).strip()
               for match in re.findall(r"^\s*(?:Vulkan|Metal|CUDA|MTL)\d*:\s*(.+)$", text, re.M)]
    usable = [name for name in devices if classify_gpu(name)[1] in ("ok", "slow", "unknown") or "apple" in name.lower()]
    if not usable:
        return "cpu", "llama.cpp sees no usable GPU" + (f" (only: {', '.join(devices)})" if devices else "")
    return "gpu", ", ".join(usable)


def require_gpu(config, tool, allow_cpu):
    """Refuse to convert without a usable GPU: on CPU MinerU's VLM takes hours per paper."""
    kind, description = tool_device(config, tool)
    if kind == "cpu" and not allow_cpu:
        raise UserError(
            f"MinerU sees no usable GPU ({description}). pdf2md-local does not convert on the CPU; "
            "see the minimum requirements in the README."
        )
    return kind, description


# ---- config ----

class UserError(Exception):
    """A problem the user can fix; printed without a traceback."""


def config_path():
    return Path(os.environ.get("PDF2MD_LOCAL_CONFIG") or config_dir() / "config.json")


def default_config():
    return {
        "inbox": None,           # the input folder (input_pdf)
        "output": None,          # the output folder (output_md)
        "stable_seconds": 60,
        "metadata_lookup": True,
        "tools": {},
    }


def load_config():
    config = default_config()
    path = config_path()
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise UserError(f"Config file is not valid JSON: {path} ({error})")
        config.update(stored)
        config["_source"] = str(path)
    else:
        config["_source"] = None
    if os.environ.get("PDF2MD_LOCAL_INPUT"):
        config["inbox"] = os.environ["PDF2MD_LOCAL_INPUT"]
        config["_fixed_folders"] = True
    if os.environ.get("PDF2MD_LOCAL_OUTPUT"):
        config["output"] = os.environ["PDF2MD_LOCAL_OUTPUT"]
        config["_fixed_folders"] = True
    return config


def save_config(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {key: value for key, value in config.items() if not key.startswith("_")}
    path.write_text(json.dumps(stored, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def resolve_folders(args, config):
    """Input and output folders: command-line flags beat env vars beat the config file.

    Folders from the config file are followed when the user moves them (see locate_folder).
    """
    flag_inbox, flag_output = getattr(args, "inbox", None), getattr(args, "output", None)
    inbox = flag_inbox or config.get("inbox")
    output = flag_output or config.get("output")
    if not inbox or not output:
        raise UserError("No input/output folders configured. Run `pdf2md-local setup` first.")
    if not flag_inbox:
        inbox = locate_folder(config, "inbox")
    if not flag_output:
        output = locate_folder(config, "output")
    return Path(inbox), Path(output)


# ---- folders that may move ----
# Each folder holds a small hidden marker with a random id. The config remembers the id, so
# when the user moves or renames a folder, pdf2md-local searches the home folder for that id
# and follows it. A folder is never recreated at an old path; if the marker cannot be found,
# the old path is kept and commands report it missing.

MARKER_NAME = ".pdf2md-local_folder.json"
FOLDER_KINDS = {"inbox": "input", "output": "output"}
FOLDER_LABELS = {"inbox": "input", "output": "output"}
SEARCH_SKIP = {"library", "appdata", "application data", "node_modules", "site-packages", "__pycache__",
               "envs", "miniconda3", "anaconda3", "miniforge3", "$recycle.bin", "windows", "program files",
               "program files (x86)", "programdata", "snap", "proc", "sys", "dev", "venv", ".venv"}
SEARCH_MAX_DEPTH = 5
SEARCH_MAX_DIRS = 40000
SEARCH_SECONDS = 20


def read_marker(folder):
    try:
        data = json.loads((Path(folder) / MARKER_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("id") else None
    except (OSError, ValueError):
        return None


def ensure_marker(folder, kind):
    """The folder's id, writing a marker if it has none."""
    existing = read_marker(folder)
    if existing:
        return existing["id"]
    import uuid
    marker = {"pdf2md-local": "folder marker; keep this file so pdf2md-local can find this folder after it moves",
              "kind": kind, "id": uuid.uuid4().hex, "created": datetime.date.today().isoformat()}
    (Path(folder) / MARKER_NAME).write_text(json.dumps(marker, indent=1) + "\n", encoding="utf-8")
    return marker["id"]


def search_roots():
    """Where moved folders are looked for: the home folder (Desktop, Documents, Dropbox, ... are inside it)."""
    return [HOME]


def find_marked_folder(folder_id, roots=None, max_depth=SEARCH_MAX_DEPTH):
    """Breadth-first search for the folder whose marker has this id; None if not found in time."""
    deadline = time.time() + SEARCH_SECONDS
    seen = 0
    queue = collections.deque((Path(root), 0) for root in (roots or search_roots()))
    visited = set()
    while queue and seen < SEARCH_MAX_DIRS and time.time() < deadline:
        folder, depth = queue.popleft()
        key = str(folder).lower()
        if key in visited:
            continue
        visited.add(key)
        seen += 1
        marker = read_marker(folder)
        if marker and marker.get("id") == folder_id:
            return folder
        if depth >= max_depth:
            continue
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith(".") or name.lower() in SEARCH_SKIP:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            queue.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def locate_folder(config, key):
    """The current path of config[key] ("inbox" or "output"), following it if it was moved."""
    path = Path(config[key])
    id_key = f"{key}_id"
    marker = read_marker(path) if path.is_dir() else None
    if config.get("_fixed_folders"):
        return path
    folder_id = config.get(id_key)
    if marker and folder_id is None:
        config[id_key] = marker["id"]          # adopt the id the folder already has
        if config.get("_source") and config.get("_source") == str(config_path()):
            save_config(config)
        return path
    if path.is_dir() and (folder_id is None or (marker and marker["id"] == folder_id)):
        return path
    if folder_id is None:
        return path                              # missing and never marked: nothing to follow
    # Missing, or a different folder now sits at the old path: look for the marked one
    found = find_marked_folder(folder_id)
    if found is None or found == path:
        return path
    print(f"pdf2md-local: the {FOLDER_LABELS[key]} folder moved: {path} -> {found}", file=sys.stderr)
    config[key] = str(found)
    if config.get("_source") == str(config_path()):
        save_config(config)
    return found


def mark_folders(config):
    """Give both folders a marker and remember their ids (no-op when they are fixed or missing)."""
    if config.get("_fixed_folders"):
        return
    changed = False
    for key, kind in FOLDER_KINDS.items():
        if config.get(key) and Path(config[key]).is_dir():
            folder_id = ensure_marker(Path(config[key]), kind)
            if config.get(f"{key}_id") != folder_id:
                config[f"{key}_id"] = folder_id
                changed = True
    if changed and config.get("_source") == str(config_path()):
        save_config(config)


# ---- progress display and progress file ----

# Which file of the batch is running, for the "[1/3]" prefix and the progress file
PROGRESS_STATE = {"prefix": "", "pdf": ""}

# tqdm lines, e.g. "VLM Predict:  45%|████▌     | 51/113 [00:12<00:15, ...]"
TQDM_PATTERN = re.compile(r"([A-Za-z][A-Za-z -]*?):\s+(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")
# MinerU splits long documents into 64-page windows: "processing window 1/3: pages 1-64/155"
WINDOW_PATTERN = re.compile(r"processing window (\d+)/(\d+): pages (\d+)-(\d+)/(\d+)")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

STAGE_LABELS = {
    "Loading weights": "Loading model",
    "Layout Predict": "Layout",
    "MFD Predict": "Finding formulas",
    "MFR Predict": "Formulas",
    "OCR-det": "Text detection",
    "OCR-det Predict": "Text detection",
    "OCR-rec Predict": "Text recognition",
    "Table Predict": "Tables",
}


def progress_file_path():
    return cache_dir() / "progress.json"


def write_progress_file(text):
    data = {"pid": os.getpid(), "pdf": PROGRESS_STATE["pdf"], "text": text, "updated": time.time()}
    path = progress_file_path()
    temp_path = path.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, path)
    except OSError:
        pass  # progress is informational only


def read_progress_file():
    try:
        return json.loads(progress_file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def remove_progress_file():
    data = read_progress_file()
    if data is not None and data.get("pid") == os.getpid():
        try:
            progress_file_path().unlink()
        except OSError:
            pass


def console_progress_enabled():
    """Show the live progress line only in a real console (not under pythonw, a service or a pipe)."""
    stdout = sys.stdout
    return stdout is not None and hasattr(stdout, "isatty") and stdout.isatty()


def display_width(text):
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def format_elapsed(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


class ProgressLine:
    """Parses a tool's output and keeps one line of progress text."""

    BAR_WIDTH = 20

    def __init__(self):
        self.started = time.time()
        self.last_update = self.started
        self.stage_name = None
        self.stage_label = "Starting"
        self.previous_name = None
        self.current = 0
        self.total = 0
        self.window = None
        self.last_width = 0
        self.lock = threading.Lock()

    def feed(self, raw_line):
        line = ANSI_PATTERN.sub("", raw_line)
        with self.lock:
            window_match = WINDOW_PATTERN.search(line)
            if window_match:
                self.window = tuple(int(value) for value in window_match.groups())
            tqdm_match = TQDM_PATTERN.search(line)
            if tqdm_match is None:
                return
            name = tqdm_match.group(1).strip()
            current = int(tqdm_match.group(3))
            total = int(tqdm_match.group(4))
            # A new stage: a different name, or the same name counting again from the start
            # (MinerU's VLM runs twice: block content, then inline formulas)
            restarted = name == self.stage_name and (total != self.total or current < self.current)
            if name != self.stage_name or restarted:
                self.previous_name = self.stage_name
                self.stage_name = name
                self.stage_label = self.label_for(name)
            self.current = current
            self.total = total
            self.last_update = time.time()

    def label_for(self, name):
        if name == "VLM Predict":
            if self.previous_name == "VLM Predict":
                return "Inline formulas"
            return "Content"
        return STAGE_LABELS.get(name, name)

    def text(self):
        with self.lock:
            parts = []
            if PROGRESS_STATE["prefix"]:
                parts.append(PROGRESS_STATE["prefix"])
            if self.window is not None:
                index, count, first, last, pages = self.window
                if count > 1:
                    parts.append(f"part {index}/{count} (pages {first}-{last}/{pages})")
                else:
                    parts.append(f"{pages} pages")
            finished_a_while_ago = (self.total > 0 and self.current >= self.total
                                    and time.time() - self.last_update > 3)
            if self.total == 0:
                parts.append(f"{self.stage_label}…")
            elif finished_a_while_ago:
                # Gap between two progress bars, when the tool prints nothing
                parts.append("Preparing model…" if self.stage_name == "Loading weights" else "Working…")
            else:
                filled = round(self.BAR_WIDTH * self.current / self.total)
                bar = "█" * filled + "░" * (self.BAR_WIDTH - filled)
                parts.append(f"{self.stage_label} {bar} {self.current}/{self.total}")
            parts.append(format_elapsed(time.time() - self.started))
            return "  " + " | ".join(parts)

    def render(self):
        write_line(self.text(), self)

    def clear(self):
        clear_line(self)


def write_line(line, holder):
    """Rewrite the current console line; pad with spaces to cover a longer previous line."""
    width = display_width(line)
    padding = " " * max(0, holder.last_width - width)
    sys.stdout.write("\r" + line + padding)
    sys.stdout.flush()
    holder.last_width = width


def clear_line(holder):
    sys.stdout.write("\r" + " " * holder.last_width + "\r")
    sys.stdout.flush()
    holder.last_width = 0


def run_command(command):
    """Run a conversion tool, parsing its output for progress as it goes."""
    show_in_console = console_progress_enabled()
    process = subprocess.Popen(
        [str(part) for part in command],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=tool_env_vars(), creationflags=no_window_flag(),
    )
    progress = ProgressLine()
    output_tail = collections.deque(maxlen=200)
    stop_ticking = threading.Event()

    # Refresh twice a second even without new output, so the elapsed time keeps moving
    def tick():
        while not stop_ticking.wait(0.5):
            write_progress_file(progress.text())
            if show_in_console:
                progress.render()

    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    pending = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = process.stdout.read1(4096)
        if not chunk:
            break
        pending += decoder.decode(chunk)
        # tqdm rewrites one line with \r, so treat \r and \n both as line ends
        pieces = re.split(r"[\r\n]", pending)
        pending = pieces.pop()
        for piece in pieces:
            if piece.strip():
                output_tail.append(piece)
                progress.feed(piece)
    return_code = process.wait()
    stop_ticking.set()
    ticker.join()
    remove_progress_file()
    if show_in_console:
        progress.clear()
    if return_code != 0:
        tail_text = "\n".join(output_tail)[-2000:]
        raise RuntimeError(f"Command failed (exit code {return_code}):\n{tail_text}")


def convert_with_mineru(config, pdf_path, work_dir):
    """MinerU writes a zip; unpack it and return the md path. Local parsing only, never --remote."""
    exe, _ = tool_paths(config, "mineru")
    run_command([exe, "parse", pdf_path, "-o", work_dir, "-f", "zip"])
    zip_files = list(work_dir.glob("*.zip"))
    if not zip_files:
        raise RuntimeError("MinerU produced no zip")
    unzip_dir = work_dir / "unzipped"
    with zipfile.ZipFile(zip_files[0]) as archive:
        archive.extractall(unzip_dir)
    return unzip_dir / "markdown.md"


# ---- output ----

def yaml_quote(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def front_matter(pdf_name, tool, version, extra=None):
    today = datetime.date.today().isoformat()
    lines = [
        "---",
        f"source_pdf: {yaml_quote(pdf_name)}",
        f"tool: {tool}",
        f"tool_version: {version}",
        f"converted: {today}",
    ]
    for key, value in (extra or {}).items():
        if value:
            lines.append(f"{key}: {yaml_quote(value)}")
    lines += ["---", ""]
    return "\n".join(lines)


def save_output(md_path, target_dir, name, header):
    """Put the md and only the images it references into the target folder, side by side."""
    md_text = md_path.read_text(encoding="utf-8")
    image_refs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", md_text)
    target_dir.mkdir(parents=True, exist_ok=False)
    for ref in set(image_refs):
        source_image = md_path.parent / ref
        if not source_image.is_file():
            continue
        image_name = Path(ref).name
        shutil.copy2(source_image, target_dir / image_name)
        md_text = md_text.replace(f"]({ref})", f"]({image_name})")
    final_md = target_dir / f"{name}.md"
    final_md.write_text(header + md_text, encoding="utf-8")
    return final_md


# ---- process liveness ----

def pid_alive(pid, started=None):
    """Is this process still running? A PID reused by a newer process counts as gone."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        if not psutil.pid_exists(pid):
            return False
        if started is None:
            return True
        try:
            created = datetime.datetime.fromtimestamp(psutil.Process(pid).create_time())
        except psutil.Error:
            return False
        return created <= started + datetime.timedelta(seconds=5)
    if IS_WINDOWS:
        return windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def windows_pid_alive(pid):
    import ctypes
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


# ---- batch lock: one conversion at a time per inbox ----
# <input>/.pdf2md-local.lock holds three lines: PID, mode (watch/manual/convert), start time.

def lock_path_for(inbox):
    return Path(inbox) / LOCK_NAME


def read_lock(lock_path):
    try:
        lines = Path(lock_path).read_text(encoding="utf-8").splitlines()
        pid = int(lines[0].strip())
        mode = lines[1].strip()
        started = datetime.datetime.strptime(lines[2].strip(), LOCK_TIME_FORMAT)
    except (OSError, ValueError, IndexError):
        return None
    return {"pid": pid, "mode": mode, "started": started}


def lock_owner_alive(info):
    return pid_alive(info["pid"], info["started"])


def write_new_lock(lock_path, mode):
    """Create the lock atomically; raises FileExistsError if someone else holds it."""
    handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    now_text = datetime.datetime.now().strftime(LOCK_TIME_FORMAT)
    os.write(handle, f"{os.getpid()}\n{mode}\n{now_text}\n".encode("utf-8"))
    os.close(handle)


UNKNOWN_HOLDER = {"pid": "?", "mode": "?", "started": None}


def acquire_lock(inbox, mode, log_file):
    """None when the lock is ours; otherwise the live holder's info."""
    lock_path = lock_path_for(inbox)
    for _ in range(3):
        try:
            write_new_lock(lock_path, mode)
            return None
        except FileExistsError:
            pass
        info = read_lock(lock_path)
        if info is None:
            # Another process may be writing it right now: younger than 10 s counts as busy
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < 10:
                return dict(UNKNOWN_HOLDER)
            log(log_file, f"Removed unreadable stale lock: {lock_path}")
        elif lock_owner_alive(info):
            return info
        else:
            started_text = info["started"].strftime(LOCK_TIME_FORMAT)
            log(log_file, f"Removed stale lock: PID {info['pid']} (mode {info['mode']}, started {started_text}) is gone")
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
    return dict(UNKNOWN_HOLDER)


def release_lock(inbox):
    """Delete the lock only if it is ours."""
    lock_path = lock_path_for(inbox)
    info = read_lock(lock_path)
    if info is not None and info["pid"] == os.getpid():
        lock_path.unlink()


HOLDER_NAMES = {"watch": "The watcher", "manual": "Another run",
                "scheduled": "A scheduled run", "read": "Another conversion"}


def follow_other_process(holder, inbox):
    """Show the progress of the process holding the lock until it lets go."""
    if holder["started"] is None:
        time.sleep(2)  # lock being written; the caller tries again
        return
    who = HOLDER_NAMES.get(holder["mode"], f"Process ({holder['mode']})")
    print(f"{who} (PID {holder['pid']}) is converting. Showing its progress; "
          "the rest of the input folder follows when it is done.")
    print("(Closing this window does not affect it.)")
    lock_path = lock_path_for(inbox)
    line = SimpleNamespace(last_width=0)
    waiting_started = time.time()
    shown_pdf = None
    while True:
        current = read_lock(lock_path)
        if current is None or current["pid"] != holder["pid"] or not lock_owner_alive(holder):
            break
        data = read_progress_file()
        fresh = data is not None and data.get("pid") == holder["pid"] and time.time() - data["updated"] < 10
        if fresh:
            if data["pdf"] != shown_pdf:
                clear_line(line)
                print(f"  Converting: {data['pdf']}")
                shown_pdf = data["pdf"]
            text = data["text"]
        else:
            text = f"  Working… | waited {format_elapsed(time.time() - waiting_started)}"
        write_line(text, line)
        time.sleep(1)
    clear_line(line)
    print(f"{who} has finished.")


def wait_for_lock(inbox, mode, log_file, quiet=False):
    """Block until we hold the inbox lock (used by `read` and `convert`)."""
    announced = False
    while True:
        holder = acquire_lock(inbox, mode, log_file)
        if holder is None:
            return
        if console_progress_enabled():
            follow_other_process(holder, inbox)
            continue
        if not announced and not quiet:
            print(f"Waiting: PID {holder['pid']} ({holder['mode']}) is converting.", file=sys.stderr)
            announced = True
        time.sleep(10)


# ---- batch ----

def log(log_file, message):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    if sys.stdout is not None:
        print(line)
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def move_without_overwrite(pdf_path, folder):
    """Move a PDF into a folder; a name clash gets _2, _3, … Never overwrites."""
    folder.mkdir(exist_ok=True)
    target = folder / pdf_path.name
    counter = 2
    while target.exists():
        target = folder / f"{pdf_path.stem}_{counter}{pdf_path.suffix}"
        counter += 1
    shutil.move(str(pdf_path), str(target))
    return target


def stable_pdfs(pdf_files, stable_seconds):
    """Only files untouched for stable_seconds (a syncing file may still be partial)."""
    now = time.time()
    return [path for path in pdf_files if now - path.stat().st_mtime >= stable_seconds]


def remember_alias(output_root, pdf_path, sha, existing_md):
    """Another copy of an already converted paper: point its hash at the existing folder."""
    add_index_entry(output_root, {"folder": existing_md.parent.name, "source_pdf": pdf_path.name,
                                  "sha256": sha, "added": datetime.date.today().isoformat()})


# An appendix, supplement or erratum shares its paper's title and DOI but is a document of its own. Its
# folder gets the paper's name plus this suffix, and its index entry has no DOI, so DOI lookups find the paper.
SUPPLEMENT_TITLE = re.compile(
    r"^\W*(?:(?:online|web)\s+)?(?:(?P<appendix>appendix|appendices)|(?P<supplement>supplement(?:ary|al)?|supporting"
    r"\s+information)|(?P<erratum>erratum|corrigendum|correction\s+to))\b", re.I)


def supplement_kind(title):
    """"appendix", "supplement" or "erratum" for such a title, else None."""
    match = SUPPLEMENT_TITLE.match(title or "")
    return next((kind for kind, text in match.groupdict().items() if text), None) if match else None


# Same DOI or title but one md this many times longer than the other: not the same document (a 3-page
# summary or review of a paper, a supplement, an erratum), so both are kept
LENGTH_RATIO = 3.0


def md_body_length(md_file):
    """Characters of an md without its front matter, or None when it cannot be read."""
    try:
        return len(md_body(Path(md_file).read_text(encoding="utf-8")))
    except OSError:
        return None


def lengths_differ(length_a, length_b):
    if not length_a or not length_b:
        return False
    return max(length_a, length_b) > LENGTH_RATIO * min(length_a, length_b)


def free_folder_name(output_root, name, title, length=None):
    """(name, existing_md): reuse nothing, never overwrite. Same title in a taken folder = same paper, unless
    the two mds differ a lot in length."""
    candidate, counter = name, 2
    while folder_taken(output_root, candidate):
        existing = existing_folder_md(output_root, candidate)
        if existing and title and titles_match(existing_md_title(existing), title, threshold=0.92):
            if not lengths_differ(length, md_body_length(existing)):
                return candidate, existing
        candidate = f"{name}_{counter}"
        counter += 1
    return candidate, None


def process_one(config, pdf_path, tool, version, output_root, done_dir, log_file, move_source=True):
    output_root = Path(output_root)
    sha = file_sha256(pdf_path)
    existing = find_md(pdf_path, output_root, sha)
    if existing is None:
        existing = find_existing_by_doi(output_root, pdf_path)
    if existing is not None:
        log(log_file, f"Skipped (md exists): {pdf_path.name} -> {existing}")
        if existing != md_path_for(pdf_path, output_root):
            remember_alias(output_root, pdf_path, sha, existing)
        return "skipped"
    legacy_dir = output_root / output_name_for(pdf_path)
    if is_zotero_name(pdf_path) and folder_taken(output_root, legacy_dir.name):
        log(log_file, f"Skipped (output folder exists): {pdf_path.name} -> {legacy_dir}")
        return "skipped"

    log(log_file, f"Converting ({tool}): {pdf_path.name}")
    PROGRESS_STATE["pdf"] = pdf_path.name
    started = time.time()
    # MinerU's intermediate files stay in a temp folder; only the result goes to the output folder
    with tempfile.TemporaryDirectory(prefix="pdf2md-local_") as temp_text:
        work_dir = Path(temp_text)
        md_path = convert_with_mineru(config, pdf_path, work_dir)
        name, meta, how = choose_name(pdf_path, md_path.read_text(encoding="utf-8"), config)
        md_text = md_path.read_text(encoding="utf-8")
        length = len(md_body(md_text))
        own_title = md_title(md_head(md_text))
        kind = supplement_kind(own_title)
        if kind:
            # Never a duplicate of its paper: kept as "<paper name>_<kind>", without the paper's DOI in the index
            log(log_file, f"{kind.capitalize()} of {meta.get('title') or 'a paper'}: kept as a document of its own")
            name, _ = free_folder_name(output_root, f"{name}_{kind}", None)
            meta = {**meta, "title": " ".join(own_title.split()), "doi": None}
            duplicate = None
        else:
            duplicate = find_md_by_doi(output_root, [meta.get("doi")], meta.get("title"))
        if duplicate is not None and lengths_differ(length, md_body_length(duplicate)):
            log(log_file, f"Kept separately: {pdf_path.name} has the DOI of {duplicate.parent.name}, but its md is "
                          f"{length} characters against {md_body_length(duplicate)} (a summary, supplement or "
                          f"the full paper?); check which is which")
            duplicate = None
        if duplicate is None and not kind:
            name, duplicate = free_folder_name(output_root, name, meta.get("title"), length)
        if duplicate is not None:
            remember_alias(output_root, pdf_path, sha, duplicate)
            log(log_file, f"Skipped (same paper already converted): {pdf_path.name} -> {duplicate}")
            return "skipped"
        extra = {"title": meta.get("title"), "doi": meta.get("doi"), "arxiv": meta.get("arxiv"),
                 "named_from": how, "source_sha256": sha}
        final_md = save_output(md_path, output_root / name, name, front_matter(pdf_path.name, tool, version, extra))
    add_index_entry(output_root, {"folder": name, "source_pdf": pdf_path.name, "sha256": sha,
                                  "doi": meta.get("doi"), "title": meta.get("title"),
                                  "added": datetime.date.today().isoformat()})

    # PDFs from the input folder move to done/ (moved, never deleted); PDFs given by path stay put
    if move_source:
        move_without_overwrite(pdf_path, done_dir)
    log(log_file, f"Done ({time.time() - started:.0f} s, named from {how}): {final_md}")
    return "done"


def run_batch(config, opts, pdf_files, move_source):
    """Convert a list of PDFs. The caller must hold the inbox lock."""
    inbox, output_root = opts.inbox, opts.output
    log_file = inbox / LOG_NAME
    if not output_root.is_dir():
        # Never recreate it: it may have been moved, and a new empty folder at the old path would
        # silently split the outputs in two
        raise UserError(f"Output folder not found: {output_root} (moved out of your home folder? "
                        "run `pdf2md-local setup` to point to it)")
    tool = opts.tool
    version = tool_version(config, tool)
    kind, description = require_gpu(config, tool, opts.allow_cpu)
    log(log_file, f"=== Batch start: {len(pdf_files)} file(s), {tool} {version} on {kind} ({description}), output {output_root}")

    counts = {"done": 0, "skipped": 0, "failed": 0}
    results = []
    for index, pdf_path in enumerate(pdf_files, start=1):
        PROGRESS_STATE["prefix"] = f"[{index}/{len(pdf_files)}]" if len(pdf_files) > 1 else ""
        try:
            status = process_one(config, pdf_path, tool, version, output_root, inbox / "done", log_file, move_source)
        except Exception:
            log(log_file, f"Failed: {pdf_path.name}\n{traceback.format_exc()}")
            status = "failed"
        counts[status] += 1
        results.append((pdf_path, status))
        # Unattended runs move skipped/failed files aside so they are not retried forever
        if opts.unattended and move_source and status != "done":
            moved_to = move_without_overwrite(pdf_path, inbox / status)
            log(log_file, f"Moved to {status}/: {moved_to.name}")
    log(log_file, f"=== Batch end: {counts['done']} done, {counts['skipped']} skipped, {counts['failed']} failed")
    return results


def inbox_pdfs(inbox):
    return sorted(path for path in Path(inbox).glob("*.pdf") if path.is_file())


def run_inbox(config, opts, busy_already_logged=None):
    """Convert the input folder under the lock. Returns True if skipped because another process is busy."""
    inbox = opts.inbox
    log_file = inbox / LOG_NAME
    # Never create the input folder: if it was renamed, recreating the old name would send PDFs astray
    if not inbox.is_dir():
        print(f"Input folder not found: {inbox}", file=sys.stderr)
        return False

    def collect():
        files = inbox_pdfs(inbox)
        if opts.unattended:
            files = stable_pdfs(files, config.get("stable_seconds", 60))
        return files

    pdf_files = collect()
    if not pdf_files:
        if not opts.unattended:
            print(f"No PDFs in the input folder: {inbox}")
        return False
    holder = acquire_lock(inbox, opts.mode, log_file)
    # A person at a console: follow the busy process instead of skipping, then do what is left
    while holder is not None and not opts.unattended and console_progress_enabled():
        follow_other_process(holder, inbox)
        pdf_files = collect()
        if not pdf_files:
            print("Everything in the input folder has been handled.")
            return False
        holder = acquire_lock(inbox, opts.mode, log_file)
    if holder is not None:
        if busy_already_logged is None or holder["pid"] not in busy_already_logged:
            log(log_file, f"Another conversion (PID {holder['pid']}, mode {holder['mode']}) is running; skipped this time")
            if busy_already_logged is not None:
                busy_already_logged.add(holder["pid"])
        return True
    try:
        run_batch(config, opts, pdf_files, move_source=True)
    finally:
        release_lock(inbox)
    return False


# ---- watcher ----

SAFETY_RESCAN_SECONDS = 30 * 60
POLL_SECONDS = 15


def watch_lock_path():
    return cache_dir() / "watch.lock"


def watcher_info_path():
    return cache_dir() / "watcher.json"


def hold_watch_lock():
    """Returns an open, locked handle; None if another watcher already runs. The OS frees it on exit."""
    handle = open(watch_lock_path(), "a+")
    try:
        if IS_WINDOWS:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def read_watcher_info():
    try:
        info = json.loads(watcher_info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    started = datetime.datetime.strptime(info["started"], LOCK_TIME_FORMAT)
    if not pid_alive(info["pid"], started):
        return None
    return info


def watch(config, opts):
    """Watch the input folder until it disappears (moved or renamed), then return; the caller looks for it."""
    inbox = opts.inbox
    if not inbox.is_dir():
        return
    mark_folders(config)
    log_file = inbox / LOG_NAME
    wake_up = threading.Event()
    observer = None
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class PdfHandler(FileSystemEventHandler):
            def on_any_event(self, event):
                path_text = getattr(event, "dest_path", "") or event.src_path
                if str(path_text).lower().endswith(".pdf"):
                    wake_up.set()

        observer = Observer()
        observer.schedule(PdfHandler(), str(inbox), recursive=False)
        observer.start()
        mechanism = "file events"
    except ImportError:
        mechanism = f"polling every {POLL_SECONDS} s"

    version = tool_version(config, opts.tool)
    info = {"pid": os.getpid(), "started": datetime.datetime.now().strftime(LOCK_TIME_FORMAT),
            "inbox": str(inbox), "tool": f"{opts.tool} {version}"}
    watcher_info_path().write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    log(log_file, f"Watcher started: PID {os.getpid()}, {opts.tool} {version}, {mechanism}, input {inbox}")

    busy_already_logged = set()
    stable_seconds = config.get("stable_seconds", 60)
    wake_up.set()  # first scan picks up PDFs that arrived while we were off
    last_scan = 0.0
    while True:
        if not inbox.is_dir():
            # Inbox renamed or moved: stop watching this path; the caller follows the folder
            if observer is not None:
                observer.stop()
            return
        if not opts.output.is_dir():
            opts.output = Path(locate_folder(config, "output"))
        due = time.time() - last_scan >= (SAFETY_RESCAN_SECONDS if observer else POLL_SECONDS)
        if wake_up.is_set() or due:
            wake_up.clear()
            last_scan = time.time()
            try:
                run_inbox(config, opts, busy_already_logged)
            except UserError as error:
                log(log_file, f"Watcher cannot convert: {error}")
            # PDFs still waiting (not yet stable, or another process busy): look again soon
            if inbox_pdfs(inbox):
                time.sleep(stable_seconds / 2)
                wake_up.set()
                continue
        # Short waits so a moved inbox is noticed within seconds (the check is a single stat call)
        wake_up.wait(timeout=POLL_SECONDS)


# ---- services (start the watcher at logon) ----

SERVICE_NAME_WINDOWS = "pdf2md-local watcher"
SERVICE_LABEL_MAC = "com.pdf2md-local.watcher"
SERVICE_UNIT_LINUX = "pdf2md-local-watcher.service"


def watcher_command():
    """Interpreter and script for the service. On Windows, pythonw.exe so no console window opens."""
    python_path = Path(sys.executable)
    if IS_WINDOWS:
        pythonw = python_path.with_name("pythonw.exe")
        if pythonw.is_file():
            python_path = pythonw
    script = Path(__file__).resolve()
    return [str(python_path), "-X", "utf8", str(script), "watch"]


def windows_task_xml(command):
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    program = command[0]
    arguments = " ".join(f'"{part}"' if " " in part else part for part in command[1:])

    def escape(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    start = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>pdf2md-local: watch input_pdf and convert new PDFs</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
    <TimeTrigger>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(program)}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
</Task>
"""


def mac_plist(command):
    log_path = cache_dir() / "watcher.out.txt"
    args_xml = "\n".join(f"    <string>{part}</string>" for part in command)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{SERVICE_LABEL_MAC}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
"""


def linux_unit(command):
    exec_line = " ".join(f'"{part}"' if " " in part else part for part in command)
    return f"""[Unit]
Description=pdf2md-local: watch input_pdf and convert new PDFs

[Service]
ExecStart={exec_line}
Restart=always
RestartSec=60

[Install]
WantedBy=default.target
"""


def mac_plist_path():
    return HOME / "Library" / "LaunchAgents" / f"{SERVICE_LABEL_MAC}.plist"


def linux_unit_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
    return base / "systemd" / "user" / SERVICE_UNIT_LINUX


def run_quiet(command):
    # Windows system tools (schtasks, taskkill) print in the console's OEM code page, e.g. cp950
    encoding = "oem" if IS_WINDOWS else "utf-8"
    return subprocess.run(command, capture_output=True, text=True, encoding=encoding, errors="replace",
                          creationflags=no_window_flag())


def service_install():
    command = watcher_command()
    if IS_WINDOWS:
        xml_path = cache_dir() / "watcher_task.xml"
        xml_path.write_text(windows_task_xml(command), encoding="utf-16")
        result = run_quiet(["schtasks", "/create", "/tn", SERVICE_NAME_WINDOWS, "/xml", str(xml_path), "/f"])
        if result.returncode != 0:
            raise UserError(f"schtasks failed: {result.stdout}{result.stderr}")
        run_quiet(["schtasks", "/run", "/tn", SERVICE_NAME_WINDOWS])
        return f"Task Scheduler task '{SERVICE_NAME_WINDOWS}' (at logon, retried every 5 min)"
    if IS_MAC:
        plist = mac_plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        uid = str(os.getuid())
        run_quiet(["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL_MAC}"])
        plist.write_text(mac_plist(command), encoding="utf-8")
        result = run_quiet(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
        if result.returncode != 0:
            result = run_quiet(["launchctl", "load", "-w", str(plist)])
            if result.returncode != 0:
                raise UserError(f"launchctl failed: {result.stdout}{result.stderr}")
        return f"launchd agent {plist}"
    unit = linux_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(linux_unit(command), encoding="utf-8")
    for step in (["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", "--now", SERVICE_UNIT_LINUX]):
        result = run_quiet(step)
        if result.returncode != 0:
            raise UserError(f"{' '.join(step)} failed: {result.stdout}{result.stderr}")
    return (f"systemd user unit {unit}. To keep it running while you are logged out: "
            f"loginctl enable-linger {os.environ.get('USER', '$USER')}")


def service_uninstall():
    if IS_WINDOWS:
        run_quiet(["schtasks", "/end", "/tn", SERVICE_NAME_WINDOWS])
        result = run_quiet(["schtasks", "/delete", "/tn", SERVICE_NAME_WINDOWS, "/f"])
        if result.returncode != 0:
            raise UserError(f"schtasks failed: {result.stdout}{result.stderr}")
    elif IS_MAC:
        run_quiet(["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_LABEL_MAC}"])
        if mac_plist_path().exists():
            mac_plist_path().unlink()
    else:
        run_quiet(["systemctl", "--user", "disable", "--now", SERVICE_UNIT_LINUX])
        if linux_unit_path().exists():
            linux_unit_path().unlink()
        run_quiet(["systemctl", "--user", "daemon-reload"])
    stop_watcher()


def stop_watcher():
    """Stop a running watcher process (the service manager may start it again)."""
    info = read_watcher_info()
    if info is None:
        return False
    try:
        if IS_WINDOWS:
            run_quiet(["taskkill", "/PID", str(info["pid"]), "/T", "/F"])
        else:
            os.kill(info["pid"], 15)
    except OSError:
        return False
    return True


def service_state():
    """Short text: whether the watcher service is installed and running."""
    if IS_WINDOWS:
        result = run_quiet(["schtasks", "/query", "/tn", SERVICE_NAME_WINDOWS, "/fo", "csv", "/nh"])
        if result.returncode != 0:
            return "not installed"
        fields = [field.strip('"') for field in result.stdout.strip().splitlines()[-1].split('","')]
        return f"installed ({fields[-1].strip(chr(34))})" if fields else "installed"
    if IS_MAC:
        if not mac_plist_path().exists():
            return "not installed"
        result = run_quiet(["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL_MAC}"])
        return "installed (loaded)" if result.returncode == 0 else "installed (not loaded)"
    if not linux_unit_path().exists():
        return "not installed"
    result = run_quiet(["systemctl", "--user", "is-active", SERVICE_UNIT_LINUX])
    return f"installed ({result.stdout.strip() or 'unknown'})"


# ---- open the folders ----

def open_folder(path):
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606 (a folder, opened in Explorer)
    elif IS_MAC:
        subprocess.run(["open", str(path)])
    else:
        subprocess.run(["xdg-open", str(path)])


# ---- commands ----

def make_opts(args, config, mode, unattended=False):
    inbox, output = resolve_folders(args, config)
    return SimpleNamespace(inbox=inbox, output=output, mode=mode, unattended=unattended,
                           tool="mineru", allow_cpu=False)


def cmd_check(args, config):
    _, output = resolve_folders(args, config)
    for pdf in args.pdf:
        found = find_md(pdf, output)
        print(f"EXISTS\t{found}" if found else f"MISSING\t{md_path_for(pdf, output)}")
    return 0


def convert_paths(config, opts, pdf_paths):
    """Convert PDFs given by path; the originals stay where they are."""
    missing = [path for path in pdf_paths if not path.is_file()]
    if missing:
        raise UserError("File not found: " + ", ".join(str(path) for path in missing))
    if not opts.inbox.is_dir():
        raise UserError(f"Input folder not found: {opts.inbox} (its log and lock live there)")
    log_file = opts.inbox / LOG_NAME
    wait_for_lock(opts.inbox, opts.mode, log_file)
    try:
        return run_batch(config, opts, pdf_paths, move_source=False)
    finally:
        release_lock(opts.inbox)


def cmd_convert(args, config):
    opts = make_opts(args, config, "convert")
    results = convert_paths(config, opts, [Path(path) for path in args.pdf])
    for pdf_path, status in results:
        found = find_md(pdf_path, opts.output)
        print(f"{status}\t{found or pdf_path}")
    return 0 if all(status != "failed" for _, status in results) else 3


def cmd_run(args, config):
    opts = make_opts(args, config, "manual")
    run_inbox(config, opts)
    return 0


def cmd_watch(args, config, sleep=time.sleep, max_rounds=None):
    """Keep watching the input folder, following it when it moves. Never recreates a missing folder."""
    lock_handle = hold_watch_lock()
    if lock_handle is None:
        return 0  # another watcher is already running
    fixed = bool(getattr(args, "inbox", None)) or config.get("_fixed_folders")
    reported_missing = None
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        opts = make_opts(args, config, "watch", unattended=True)
        if opts.inbox.is_dir():
            reported_missing = None
            watch(config, opts)
            if fixed:
                return 0
            continue  # the input folder disappeared: look for it again
        if fixed:
            print(f"Input folder not found: {opts.inbox}", file=sys.stderr)
            return 0
        if reported_missing != str(opts.inbox):
            reported_missing = str(opts.inbox)
            print(f"Input folder not found: {opts.inbox}; looking for it again every minute", file=sys.stderr)
        sleep(60)
    return 0


def tail_lines(path, count):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return list(collections.deque(handle, maxlen=count))
    except OSError:
        return []


def cmd_status(args, config):
    inbox, output = resolve_folders(args, config)
    print(f"{APP} {__version__} | config: {config.get('_source') or 'none'}")
    print(f"input:  {inbox}" + ("" if inbox.is_dir() else "  (not found)"))
    print(f"output: {output}" + ("" if output.is_dir() else "  (not found)"))
    print(f"service: {service_state()}")
    watcher = read_watcher_info()
    if watcher:
        print(f"watcher: running, PID {watcher['pid']} since {watcher['started']} ({watcher['tool']})")
    else:
        print("watcher: not running (start it with `pdf2md-local service install`)")
    holder = read_lock(lock_path_for(inbox)) if inbox.is_dir() else None
    if holder:
        print(f"converting now: PID {holder['pid']} ({holder['mode']}) since {holder['started']:%H:%M:%S}")
        data = read_progress_file()
        if data and data.get("pid") == holder["pid"] and time.time() - data["updated"] < 10:
            print(f"  {data['pdf']}\n{data['text']}")
    else:
        print("converting now: nothing")
    if inbox.is_dir():
        queued = inbox_pdfs(inbox)
        counts = ", ".join(f"{name} {len(list((inbox / name).glob('*.pdf')))}"
                           for name in STATUS_FOLDERS if (inbox / name).is_dir())
        print(f"waiting: {len(queued)}" + (f" ({', '.join(path.name for path in queued[:5])})" if queued else ""))
        if counts:
            print(f"history: {counts}")
        lines = tail_lines(inbox / LOG_NAME, args.lines)
        if lines:
            print(f"--- last {len(lines)} log lines ---")
            sys.stdout.write("".join(lines))
    return 0


def cmd_open(args, config):
    inbox, output = resolve_folders(args, config)
    for label, folder in (("input", inbox), ("output", output)):
        if not folder.is_dir():
            raise UserError(f"The {label} folder was not found: {folder}")
        print(f"{label}: {folder}")
        if not args.print_only:
            open_folder(folder)
    return 0


def cmd_hardware(args, config):
    report = hardware_report(accept_gpu=args.accept_gpu)
    print(f"{APP} {__version__} hardware check ({platform.system()} {platform.machine()})")
    print(format_hardware_report(report))
    ok = report["ok"]
    if ok and detect_tool("mineru"):
        kind, description = tool_device({}, "mineru")
        print(f"  MinerU can use: {description}")
        ok = kind != "cpu"
        if not ok:
            print("  problem: MinerU's engine sees no usable GPU (on Ubuntu: sudo apt install mesa-vulkan-drivers libvulkan1)")
    return 1 if (args.require and not ok) else 0


def cmd_doctor(args, config):
    print(f"{APP} {__version__} | Python {sys.version.split()[0]} | {sys.platform}")
    print(f"program: {Path(__file__).resolve()}")
    print(f"config: {config_path()} ({'found' if config_path().is_file() else 'missing: run `pdf2md-local setup`'})")
    for key, label in (("inbox", "input"), ("output", "output")):
        value = config.get(key)
        state = "" if not value else ("" if Path(value).is_dir() else "  (not found)")
        print(f"  {label}: {value}{state}")
    for name in ("psutil", "watchdog"):
        try:
            __import__(name)
            print(f"{name}: installed")
        except ImportError:
            print(f"{name}: not installed (works without it, with simpler fallbacks)")
    try:
        exe, _ = tool_paths(config, "mineru")
        try:
            version = tool_version(config, "mineru")
            kind, description = tool_device(config, "mineru")
            print(f"mineru: {version} at {exe} | device {kind} ({description})")
        except Exception as error:
            print(f"mineru: found at {exe} but could not run it: {error}")
    except UserError:
        print("mineru: not found (run the installer)")
    print("hardware (minimum requirements):\n" + format_hardware_report(hardware_report()))
    print(f"service: {service_state()}")
    return 0


ASSUME_DEFAULTS = {"on": False}


def ask(question, default):
    if ASSUME_DEFAULTS["on"] or not sys.stdin or not sys.stdin.isatty():
        print(f"{question}: {default}")
        return default
    try:
        answer = input(f"{question} [{default}]: ").strip()
    except EOFError:
        print()
        return default
    return answer or default


def nested(folder_a, folder_b):
    """True if the two folders are the same or one lies inside the other."""
    a, b = Path(folder_a).resolve(), Path(folder_b).resolve()
    return a == b or a in b.parents or b in a.parents


def cmd_setup(args, config):
    ASSUME_DEFAULTS["on"] = args.yes
    print(f"{APP} setup. Press Enter to accept the value in brackets.\n")
    # Converting needs a GPU: refuse before writing anything on machines below the minimum
    report = hardware_report(accept_gpu=args.accept_gpu)
    print("Hardware check:\n" + format_hardware_report(report))
    if not report["ok"]:
        raise UserError("This computer does not meet the minimum requirements (see above and the README). "
                        "Nothing was set up.")
    found = detect_tool("mineru")
    if not found:
        raise UserError("MinerU was not found. Run the installer (install.sh / install.ps1) first.")
    print(f"MinerU: {found['cmd']}")
    # The operating system may list a GPU that MinerU's engine cannot use (missing Vulkan driver, say)
    kind, description = tool_device({"tools": {"mineru": found}}, "mineru")
    print(f"MinerU can use: {description}")
    if kind == "cpu":
        hint = " On Ubuntu, install the Vulkan driver: sudo apt install mesa-vulkan-drivers libvulkan1" if sys.platform.startswith("linux") else ""
        raise UserError(f"MinerU sees no usable GPU ({description}).{hint}")

    if args.inbox and args.output:
        inbox, output = Path(args.inbox), Path(args.output)
    elif config.get("inbox") and config.get("output"):
        # Follow folders the user has moved, so setup never recreates one at its old path
        inbox, output = Path(locate_folder(config, "inbox")), Path(locate_folder(config, "output"))
    else:
        base = Path(ask("\nWhere to create the two folders", str(HOME / APP)))
        inbox, output = base / "input_pdf", base / "output_md"
    inbox = Path(ask("Input folder (drop PDFs here)", str(inbox))).expanduser()
    output = Path(ask("Output folder (the md files appear here)", str(output))).expanduser()
    if nested(inbox, output):
        raise UserError("The input and output folders must be two separate folders, neither inside the other.")

    new_config = default_config()
    new_config.update({key: value for key, value in config.items() if not key.startswith("_")})
    new_config.update({"inbox": str(inbox), "output": str(output), "tools": {"mineru": found}})
    for key, folder in (("inbox", inbox), ("output", output)):
        folder.mkdir(parents=True, exist_ok=True)
        new_config[f"{key}_id"] = ensure_marker(folder, FOLDER_KINDS[key])
    path = save_config(new_config)
    print(f"\nSaved {path}")
    print(f"Input folder:  {inbox}\nOutput folder: {output}")
    print("You can move or rename both folders later; pdf2md-local finds them again "
          "(keep the hidden .pdf2md-local_folder.json file inside each).")
    print("Next: `pdf2md-local service install` starts the watcher now and at every logon.")
    return 0


def cmd_service(args, config):
    if args.action == "status":
        print(service_state())
        return 0
    if args.action == "uninstall":
        service_uninstall()
        print("Watcher service removed. Your folders and files are untouched.")
        return 0
    if not config.get("inbox"):
        raise UserError("Run `pdf2md-local setup` first.")
    if args.action == "restart":
        stop_watcher()
        print("Stopped the watcher; starting it again.")
    print(f"Installed: {service_install()}")
    return 0


def front_matter_value(md_text, key):
    if not md_text.startswith("---\n"):
        return None
    head = md_text[:md_text.find("\n---", 4)]
    match = re.search(rf"^{key}:\s*(.+)$", head, re.M)
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


SOURCE_SHA_LINE = re.compile(r'^source_sha256:\s*"?([0-9a-f]{64})"?\s*$', re.M)


def md_source_sha(md_file):
    """The source_sha256 in an md's front matter, or None."""
    try:
        head = Path(md_file).read_text(encoding="utf-8")[:4000]
    except (OSError, TypeError):
        return None
    match = SOURCE_SHA_LINE.search(head)
    return match.group(1) if match else None


def pdf_text_and_pages(pdf_path, pages=2):
    """(text of the first pages, page count) when pypdfium2 is there (it comes with MinerU), else (None, None)."""
    try:
        import pypdfium2
        document = pypdfium2.PdfDocument(str(pdf_path))
        try:
            text = " ".join(document[i].get_textpage().get_text_range() for i in range(min(pages, len(document))))
            return text, len(document)
        finally:
            document.close()
    except Exception:
        return None, None


def title_in_text(title, text):
    """Do the first six words of this title appear, in order, in the text?"""
    words = normalize_title(title or "").split()[:6]
    return len(words) >= 3 and " ".join(words) in normalize_title(text or "")


# A PDF this many pages long next to an md under this many characters per page: the folder may hold a shorter
# document (slides, a summary) under the paper's name
CHARS_PER_PAGE = 1500


def recheck_skipped(inbox, output):
    """Decide again, with today's rules, why each PDF in skipped/ was skipped.

    Returns (pdf, verdict, folder, entries, note) for each PDF:
    - "same file": the very PDF an md was made from;
    - "copy": still recognised as a copy (today's rules, or the folder's title on the PDF's first page);
    - "check": a copy by title, but the PDF is much longer than the md: the folder may hold a shorter document;
    - "wrong": only an index entry written by older rules ties it to that folder (entries: those to remove)."""
    index = load_index(output)
    results = []
    for pdf in sorted((Path(inbox) / "skipped").glob("*.pdf")):
        sha = file_sha256(pdf)
        mine = [e for e in index["entries"] if e.get("sha256") == sha and e.get("folder")]
        original = next((e for e in mine
                         if md_source_sha(existing_folder_md(output, e["folder"])) == sha), None)
        if original:
            results.append((pdf, "same file", original["folder"], [], ""))
            continue
        _INDEX_OVERRIDE[str(Path(output))] = {**index, "entries": [e for e in index["entries"] if e not in mine]}
        try:
            found = find_md(pdf, output, sha) or find_existing_by_doi(output, pdf)
        finally:
            _INDEX_OVERRIDE.pop(str(Path(output)), None)
        md = found or (existing_folder_md(output, mine[0]["folder"]) if mine else None)
        if md is None:
            results.append((pdf, "wrong", mine[0]["folder"] if mine else None, mine, "no md matches it today"))
            continue
        text, pages = pdf_text_and_pages(pdf)
        evidence = text if text else pdf.stem
        start = " ".join((text or "").split())[:80]
        if supplement_kind(start) or supplement_kind(pdf.stem.split(" - ")[-1]):
            results.append((pdf, "wrong", md.parent.name, mine, "an appendix or supplement, not the paper"))
            continue
        if found is None and not title_in_text(existing_md_title(md), evidence):
            results.append((pdf, "wrong", md.parent.name, mine, "the folder's title is not on its first page"))
            continue
        length = md_body_length(md)
        if pages and length and pages * CHARS_PER_PAGE > LENGTH_RATIO * length:
            results.append((pdf, "check", md.parent.name, [],
                            f"{pages} pages against an md of {length} characters: is that folder a shorter document?"))
            continue
        results.append((pdf, "copy", md.parent.name, [], ""))
    return results


def cmd_recheck(args, config):
    """List PDFs in skipped/ that older rules skipped by mistake; with --apply, send them back to be converted."""
    inbox, output = resolve_folders(args, config)
    if not (inbox / "skipped").is_dir():
        print(f"Nothing to recheck: no skipped/ folder in {inbox}")
        return 0
    results = recheck_skipped(inbox, output)
    labels = {"same file": "same file", "copy": "copy     ", "check": "CHECK    ", "wrong": "MISTAKE  "}
    for pdf, verdict, folder, _, note in results:
        target = f"(was -> {folder})" if verdict == "wrong" and folder else (f"-> {folder}" if folder else "")
        print(f"{labels[verdict]}  {pdf.name}  {target}".rstrip() + (f"\n             {note}" if note else ""))
    wrong = [(pdf, entries) for pdf, verdict, _, entries, _ in results if verdict == "wrong"]
    checks = sum(1 for result in results if result[1] == "check")
    print(f"\n{len(results)} PDF(s) in skipped/: {len(wrong)} skipped by mistake, {checks} to check by hand.")
    if checks:
        print("CHECK: open that folder's md. If it is not the paper, rename the folder (and its md), then move the "
              "PDF back to the input folder.")
    if not wrong:
        return 0
    if not args.apply:
        print(f"Run `{APP} recheck --apply` to remove their index entries (after a backup) and move them back to "
              f"the input folder, where they are converted again.")
        return 0
    log_file = inbox / LOG_NAME
    holder = acquire_lock(inbox, "recheck", log_file)
    if holder is not None:
        raise UserError("A conversion is running right now; run recheck --apply again when it has finished.")
    try:
        backup = inbox / f"{INDEX_NAME}.backup-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        if index_path(output).is_file():
            shutil.copy2(index_path(output), backup)
        index = load_index(output)
        drop = [entry for _, entries in wrong for entry in entries]
        index["entries"] = [e for e in index["entries"] if e not in drop]
        save_index(output, index)
        for pdf, entries in wrong:
            moved = move_without_overwrite(pdf, inbox)
            log(log_file, f"Recheck: {pdf.name} was skipped by mistake"
                          + (f" (as {entries[0]['folder']})" if entries else "") + "; sent back to be converted")
            print(f"sent back: {moved.name}")
        print(f"Index backup: {backup.name}. The watcher converts them within a few minutes "
              f"(`{APP} status` shows progress).")
    finally:
        release_lock(inbox)
    return 0


def cmd_index(args, config):
    """rebuild: record every existing output folder in the index (mds are only read, never changed)."""
    inbox, output = resolve_folders(args, config)
    online = config.get("metadata_lookup", True) and not args.offline
    folders = sorted(path for path in output.iterdir() if folder_md(output, path.name).exists())
    renames = []
    index = load_index(output)
    for folder in folders:
        md_file = folder_md(output, folder.name)
        md_text = md_file.read_text(encoding="utf-8")
        source_name = front_matter_value(md_text, "source_pdf") or f"{folder.name}.pdf"
        pdf = next((candidate for candidate in (inbox / "done" / source_name, inbox / "skipped" / source_name,
                                                inbox / source_name) if candidate.is_file()), None)
        meta = detect_metadata(pdf or Path(source_name), md_text, online=online)
        entry = {"folder": folder.name, "source_pdf": source_name,
                 "sha256": file_sha256(pdf) if pdf else front_matter_value(md_text, "source_sha256"),
                 "doi": front_matter_value(md_text, "doi") or meta.get("doi"),
                 "title": meta.get("title")}
        merge_index_entry(index, {key: value for key, value in entry.items() if value})
        suggested = None if is_zotero_name(source_name) else name_from_metadata(meta)
        flag = f"  (metadata name: {suggested})" if suggested and suggested != folder.name else ""
        if flag:
            renames.append((folder.name, suggested))
        print(f"{folder.name}: {'hash' if entry['sha256'] else 'no PDF found'}, doi {entry['doi'] or '-'}{flag}")
    save_index(output, index)
    print(f"Index: {index_path(output)} ({len(folders)} folders)")
    if renames:
        print(f"{len(renames)} folder(s) would get a different name today; nothing was renamed.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog=APP, description="Convert the PDFs you drop into input_pdf to Markdown in output_md, on this computer's GPU.")
    parser.add_argument("--version", action="version", version=f"{APP} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def folder_flags(command_parser):
        command_parser.add_argument("--input", dest="inbox", metavar="DIR", help="use this input folder")
        command_parser.add_argument("--output", metavar="DIR", help="use this output folder")

    p = sub.add_parser("setup", help="first run: check the hardware, create input_pdf and output_md")
    p.add_argument("--yes", action="store_true", help="accept every suggested value without asking")
    p.add_argument("--accept-gpu", action="store_true",
                   help="treat a GPU pdf2md-local does not recognise as usable (never accepts CPU-only machines)")
    folder_flags(p)
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("hardware", help="does this computer meet the minimum requirements? (GPU, memory, disk)")
    p.add_argument("--require", action="store_true", help="exit with code 1 if it does not (for installers)")
    p.add_argument("--accept-gpu", action="store_true", help="treat an unrecognised GPU as usable")
    p.set_defaults(func=cmd_hardware)

    p = sub.add_parser("status", help="watcher, current conversion, waiting PDFs and recent log")
    p.add_argument("--lines", type=int, default=10)
    folder_flags(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("open", help="open the input and output folders")
    p.add_argument("--print-only", action="store_true", help="only print where they are")
    folder_flags(p)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("run", help="convert every PDF in the input folder now, with progress")
    folder_flags(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("convert", help="convert PDFs by path; the originals stay where they are")
    p.add_argument("pdf", nargs="+")
    folder_flags(p)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("check", help="EXISTS/MISSING and the md path for each PDF")
    p.add_argument("pdf", nargs="+")
    folder_flags(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("service", help="the background watcher: install, uninstall, restart, status")
    p.add_argument("action", choices=["install", "uninstall", "restart", "status"])
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("watch", help="run the watcher in the foreground (the service runs this)")
    folder_flags(p)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("doctor", help="check config, folders, MinerU, GPU and the watcher")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("recheck", help="find PDFs in skipped/ that older versions skipped by mistake")
    p.add_argument("--apply", action="store_true",
                   help="remove their index entries (after a backup) and send them back to be converted")
    folder_flags(p)
    p.set_defaults(func=cmd_recheck)

    p = sub.add_parser("index", help=f"rebuild: record existing output folders in {INDEX_NAME}")
    p.add_argument("action", choices=["rebuild"])
    p.add_argument("--offline", action="store_true", help="no Crossref/arXiv lookups")
    folder_flags(p)
    p.set_defaults(func=cmd_index)
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        config = load_config()
        return args.func(args, config) or 0
    except UserError as error:
        print(f"{APP}: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
