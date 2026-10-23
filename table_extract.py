"""Diagnostics from table-layout protocols (stage 4): older protocols (2005–2012) and some
big consolidated ones are tables

    Наименование нозологических форм (шифр по МКБ-10) | Диагностика: обязательная |
    дополнительная (по показаниям) | Лечение | …
                1                    2            3                4

`pdftotext -layout` keeps the columns aligned, so a page is cut into columns at the
vertical blank channels between the column numbers row («1  2  3  4  5»), the column roles
come from the header words («обязательн», «дополнительн», «наименование/шифр»), and a table
row starts where the name column starts a new nosology. Each row becomes
{page, name, icd10, mandatory: [...], additional: [...]}.

Used by kb_build.py; standalone for inspection:
    python -I table_extract.py "protocols/Урология/<file>.pdf"
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir
from protocol_chapter_extractor import pdf_to_pages

NUM_ROW_RE = re.compile(r"^\s*1(?:\s{2,}\d{1,2}){2,}\s*$")
ICD_RE = re.compile(r"(?<![\wА-ЯЁа-яё])([A-ZАВЕКМНОРСТХ])\s?(\d{2})(?:\.(\d{1,2}))?")
CYR2LAT = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")
NOISE_RE = re.compile(r"^\s*(\d{1,3}|\(?Национальный правовой Интернет-портал.*)\s*$")


def number_row(lines):
    """(index, [x of "1", x of "2", …]) of the column numbers row, numbers consecutive."""
    for i, l in enumerate(lines):
        if NUM_ROW_RE.match(l):
            nums = [(m.start(), int(m.group())) for m in re.finditer(r"\d+", l)]
            if [n for _, n in nums] == list(range(1, len(nums) + 1)):
                return i, [x for x, _ in nums]
    return None, None


def boundaries(lines, xs):
    """Column cut positions: in each gap between column numbers, the x where most text
    lines have a blank (the channel between two columns)."""
    body = [l for l in lines if l.strip()]
    width = max((len(l) for l in body), default=0)

    def blank(x):
        return sum(1 for l in body if x >= len(l) or l[x] == " ") / len(body) if body else 1

    cuts = []
    for a, b in zip(xs, xs[1:]):
        lo = a + 1
        best = max(range(lo, max(lo + 1, b)), key=lambda x: (round(blank(x), 2), -abs(x - (a + b) / 2)))
        cuts.append(best)
    return [0] + cuts + [max(width, xs[-1] + 1) + 1000]


def cells(line, cuts):
    return [line[cuts[i]:cuts[i + 1]].strip() if cuts[i] < len(line) else "" for i in range(len(cuts) - 1)]


def header_roles(header_lines, cuts):
    """Column index → role, from header words above the numbers row."""
    roles = {}
    for l in header_lines:
        for word, role in (("обязательн", "mandatory"), ("дополнительн", "additional"),
                           ("наименовани", "name"), ("нозолог", "name"), ("шифр", "name")):
            for m in re.finditer(word, l, re.I):
                col = max(i for i in range(len(cuts) - 1) if cuts[i] <= m.start())
                roles.setdefault(role, col)
    return roles


def join_lines(parts):
    out = ""
    for p in parts:
        if out.endswith("-") and not out.endswith(" -"):
            # «обследова-» + «ние» is a wrap; «врача-» + «инфекциониста» keeps its hyphen
            compound = re.search(r"(?:врач\w*|медико|социально|рентгено)-$", out, re.I)
            out = out[:-1] + p if p[:1].islower() and not compound else out + p
        else:
            out = (out + " " + p).strip()
    return out


def split_items(lines):
    """Cell lines → items: a new item starts with an upper-case letter unless the previous
    line was cut by a hyphen or ended with a comma/colon."""
    items, cur = [], []
    for l in lines:
        if not l:
            continue
        if cur and l[:1].isupper() and not cur[-1].endswith(("-", ",", ":", "(")) and not re.fullmatch(r"\d+", l):
            items.append(join_lines(cur))
            cur = []
        if re.fullmatch(r"[\d,\s-]+", l):  # «кратность» numbers that leaked into the cell
            continue
        cur.append(l)
    if cur:
        items.append(join_lines(cur))
    return [i for i in items if len(i) > 2]


def icd_codes(text, icd_names):
    out = []
    for m in ICD_RE.finditer(text):
        c = m.group(1).translate(CYR2LAT) + m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
        if c in icd_names:
            out.append(c)
    return list(dict.fromkeys(out))


def extract(pdf: Path, icd_names) -> list[dict]:
    pages = pdf_to_pages(pdf)
    rows, roles, cur = [], None, None
    for pno, page in enumerate(pages, start=1):
        lines = [l.rstrip() for l in page.splitlines() if not NOISE_RE.match(l)]
        idx, xs = number_row(lines)
        if idx is None:
            continue
        body = lines[idx + 1:]
        cuts = boundaries(body, xs)
        r = header_roles(lines[max(0, idx - 12):idx], cuts)
        if "mandatory" in r:
            roles = r
        if not roles or "mandatory" not in roles:
            continue
        name_col = roles.get("name", 0)
        prev_name, gap = "", True
        for l in body:
            if not l.strip():
                gap = True
                continue
            c = cells(l, cuts)
            name = c[name_col] if name_col < len(c) else ""
            # rows are separated by blank lines; several nosologies listed one under another
            # without a gap («Камни почки (N20.0)», «Камни мочеточника (N20.1)») share one row
            starts = name and name[:1].isupper() and gap and (not prev_name or prev_name.endswith(")"))
            gap = False
            if starts or cur is None:
                cur = {"page": pno, "page_end": pno, "name_lines": [], "mandatory": [], "additional": []}
                rows.append(cur)
            cur["page_end"] = pno
            if name:
                cur["name_lines"].append(name)
            for role in ("mandatory", "additional"):
                col = roles.get(role)
                if col is not None and col < len(c) and c[col]:
                    cur[role].append(c[col])
            prev_name = name or prev_name
    out = []
    for r in rows:
        name = join_lines(r["name_lines"])
        codes = icd_codes(name, icd_names)
        if not codes or not (r["mandatory"] or r["additional"]):
            continue
        out.append({"page": r["page"], "page_end": r["page_end"], "name": name, "icd10": codes,
                    "mandatory": split_items(r["mandatory"]), "additional": split_items(r["additional"])})
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    icd = {c["code"] for c in json.loads(Path("reference/icd10.json").read_text(encoding="utf-8"))["codes"]}
    for row in extract(Path(sys.argv[1]), icd)[: int(sys.argv[2]) if len(sys.argv) > 2 else 8]:
        print(f"p.{row['page']} {row['icd10']} {row['name'][:80]}")
        print("   обяз.: " + " | ".join(row["mandatory"]))
        print("   доп.:  " + " | ".join(row["additional"]))
