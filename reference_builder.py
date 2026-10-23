"""Build the reference dictionaries the expert system normalizes against:

  * services.json  — the list of medical services («Перечень медицинских услуг,
                     медицинских вмешательств», постановление МЗ РБ 05.12.2016 №123,
                     consolidated edition incl. amendments №90/2017 and №179/2024),
                     the vocabulary the clinical protocols are written in;
  * service_codes.json — meaning of the code parts (type A09 = «исследования
                     биологических жидкостей…», area 28 = «почки и мочевыделительная
                     система», specialty for class B);
  * icd10.json     — МКБ-10 (Russian NSI dictionary 1.2.643.5.1.13.13.11.1005).

Sources are downloaded into <out>/raw/ on first run (re-used afterwards, --refresh to
re-download). Requires `pdftotext` on PATH and `requests`.

Usage:
    python -I reference_builder.py [--out reference] [--refresh]
"""
import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import requests

DECREE_URL = "https://minzdrav.gov.by/upload/lcfiles/" + quote("постановление_МЗ_2016_123.pdf")
ICD_URL = ("https://raw.githubusercontent.com/ak4nv/mkb10/master/resources/"
           "1.2.643.5.1.13.13.11.1005_2.27.csv")

# Row of the list: "1.1828. Подсчет количества форменных элементов методом Нечипоренко A09 28 053".
# The name may wrap onto following lines and the code may sit on a line of its own.
ROW_RE = re.compile(r"^([12])\.(\d+)\.\s+(.*)$")
CODE_RE = re.compile(r"(?:^|\s)([AАBВ])(\d{2})((?:\s+\d{2,3}){1,3})\s*$")
FOOTNOTES_RE = re.compile(r"^1 В графе 3")
# Footnote dictionaries: "А09 – исследования биологических жидкостей…;" / "28 – почки…;"
FOOTNOTE_ITEM_RE = re.compile(r"^([AАBВ]\d{2}|\d{2,3})\s+[–-]\s+(.*?)[;.]?$")
CYR2LAT = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")

# Code types that are diagnostic services. A10 «диагностические исследования, выполняемые
# в процессе оказания медицинской помощи», B01 = specialist consultation (protocols list
# «прием (осмотр, консультация) врача-…» among diagnostics), B03 = diagnostic complexes.
DIAGNOSTIC_TYPES = {"A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09", "A10",
                    "A11", "A12", "A26", "B01", "B03"}


def download(url: str, dest: Path, refresh: bool):
    if dest.exists() and not refresh:
        return
    print(f"downloading {url}")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)


def pdf_raw_text(pdf: Path) -> str:
    # -raw keeps content-stream order: name and code of a row stay together.
    # -layout misaligns them on some pages (codes of one row printed next to another).
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input.pdf"
        shutil.copyfile(pdf, src)  # xpdf on Windows can't open non-ASCII paths
        return subprocess.run(["pdftotext", "-raw", "-enc", "UTF-8", str(src), "-"],
                              capture_output=True, check=True).stdout.decode("utf-8")


def parse_footnotes(lines: list[str]) -> dict:
    """Code part meanings from the footnotes at the end of the list."""
    types, areas_a, specialties_b = {}, {}, {}
    target, cur = None, None
    for line in lines:
        line = line.strip()
        if line.startswith("1 В графе 3"):
            target = types
        elif line.startswith("для класса «А»"):
            target = areas_a
        elif line.startswith("для класса «В»"):
            target = specialties_b
        elif re.match(r"^[234] В графе", line):
            target = None if not line.startswith("2 В графе 4") else target
        elif target is not None:
            m = FOOTNOTE_ITEM_RE.match(line)
            if m:
                cur = m.group(1).translate(CYR2LAT)
                target[cur] = m.group(2)
            elif cur in target:  # wrapped description
                target[cur] = (target[cur] + " " + line).rstrip(";.")
    return {"type": types, "area_A": areas_a, "specialty_B": specialties_b}


def parse_services(text: str) -> tuple[list[dict], dict]:
    lines = [l.replace("\f", "") for l in text.splitlines()]
    end = next(i for i, l in enumerate(lines) if FOOTNOTES_RE.match(l))
    codes = parse_footnotes(lines[end:])

    rows, cur = [], None
    for line in lines[:end]:
        line = line.strip()
        if not line or line.startswith("(Национальный правовой"):
            continue
        m = ROW_RE.match(line)
        if m:
            cur = {"num": f"{m.group(1)}.{m.group(2)}", "name_parts": [m.group(3)], "code": None}
            rows.append(cur)
        elif cur is not None and cur["code"] is None:
            cur["name_parts"].append(line)
        else:
            continue
        if cur["code"] is None:
            tail = cur["name_parts"][-1]
            cm = CODE_RE.search(tail)
            if cm:
                cur["code"] = ".".join([(cm.group(1) + cm.group(2)).translate(CYR2LAT)]
                                       + cm.group(3).split())
                cur["name_parts"][-1] = tail[:cm.start()].strip()

    services = []
    for r in rows:
        name = " ".join(p for p in r["name_parts"] if p)
        name = re.sub(r"-\s+(?=[а-яё])", "-", re.sub(r"\s{2,}", " ", name)).strip()
        code = r["code"]
        excluded = name.lower().startswith("исключен")
        parts = code.split(".") if code else []
        svc_type = parts[0] if parts else None
        area = parts[1] if len(parts) > 1 else None
        services.append({
            "code": code,
            "num": r["num"],
            "name": name,
            "class": "A" if r["num"].startswith("1.") else "B",
            "type": svc_type,
            "type_name": codes["type"].get(svc_type),
            "area": area,
            "area_name": (codes["area_A"] if r["num"].startswith("1.") else codes["specialty_B"]).get(area),
            # parent = same code without the last part (A09.28.053.001 -> A09.28.053)
            "parent": ".".join(parts[:3]) if len(parts) > 3 else None,
            "diagnostic": svc_type in DIAGNOSTIC_TYPES,
            "excluded": excluded,
        })
    known = {s["code"] for s in services if s["code"]}
    for s in services:
        if s["parent"] not in known:
            s["parent"] = None
    return services, codes


def parse_icd(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    by_id = {r["ID"]: r for r in rows}
    out = []
    for r in rows:
        parent = by_id.get(r["ID_PARENT"])
        out.append({
            "code": r["MKB_CODE"].strip(),
            "name": " ".join(r["MKB_NAME"].split()),
            "parent": parent["MKB_CODE"].strip() if parent else None,
            "actual": r["ACTUAL"] == "1",
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("reference"))
    ap.add_argument("--refresh", action="store_true", help="re-download the sources")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    raw = args.out / "raw"
    decree_pdf, icd_csv = raw / "pmz_2016_123.pdf", raw / "mkb10_nsi_1005_2.27.csv"
    download(DECREE_URL, decree_pdf, args.refresh)
    download(ICD_URL, icd_csv, args.refresh)

    services, codes = parse_services(pdf_raw_text(decree_pdf))
    no_code = [s for s in services if not s["code"] and not s["excluded"]]
    dup = {}
    for s in services:
        if s["code"]:
            dup.setdefault(s["code"], []).append(s["num"])
    dup = {c: n for c, n in dup.items() if len(n) > 1}

    icd = parse_icd(icd_csv.read_bytes())

    def dump(name, obj):
        (args.out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

    dump("services.json", {"source": DECREE_URL,
                           "title": "Перечень медицинских услуг, медицинских вмешательств "
                                    "(постановление МЗ РБ 05.12.2016 № 123, ред. 26.12.2024 № 179)",
                           "services": services})
    dump("service_codes.json", codes)
    dump("icd10.json", {"source": ICD_URL, "codes": icd})

    active = [s for s in services if not s["excluded"]]
    print(f"services: {len(services)} rows, {len(active)} active, "
          f"{sum(s['diagnostic'] for s in active)} diagnostic, class B {sum(s['class'] == 'B' for s in active)}")
    print(f"  code types {len(codes['type'])}, areas {len(codes['area_A'])}, specialties {len(codes['specialty_B'])}")
    if no_code:
        print(f"  WARNING: {len(no_code)} rows without a code, e.g. "
              + "; ".join(f"{s['num']} {s['name'][:60]}" for s in no_code[:5]))
    if dup:
        print(f"  WARNING: {len(dup)} duplicate codes, e.g. "
              + "; ".join(f"{c}: {n}" for c, n in list(dup.items())[:5]))
    print(f"icd10: {len(icd)} entries, {sum(1 for c in icd if '.' in c['code'])} with subcodes")


if __name__ == "__main__":
    main()
