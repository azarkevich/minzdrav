"""Copy what the website needs at runtime into deploy/ (committed, ~155 MB) for hosts that build from git.

    python -I make_deploy.py       # after kb_build.py / reference_builder.py / insurance.py build

deploy/pdf/ holds every protocol PDF of the KB and the insurance rules, named by kb_web.pdf_key(url):
the PDF viewer reads them there because minzdrav.gov.by does not answer requests from the hosting's
data centres.
"""
import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = [("kb_all/kb.idx.json", "deploy/kb.idx.json"), ("kb_all/kb.items.bin", "deploy/kb.items.bin"),
         ("kb_all/kb.stems.json", "deploy/kb.stems.json"),  # python -I kb.py index --kb kb_all/kb.idx.json
         ("reference/services.json", "deploy/reference/services.json"), ("reference/icd10.json", "deploy/reference/icd10.json")]

for src, dst in FILES:
    (HERE / dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / src, HERE / dst)
    print(f"{dst}  {(HERE / dst).stat().st_size / 2**20:.1f} MB")


def pdf_key(url: str) -> str:  # the same as kb_web.pdf_key
    return hashlib.sha1(url.encode()).hexdigest()[:16]


pdfs = {}
for d in json.loads((HERE / "kb_all/kb.idx.json").read_text(encoding="utf-8"))["documents"].values():
    src = HERE / "protocols" / d["path"]
    if d["url"].lower().endswith(".pdf") and src.exists():
        pdfs[pdf_key(d["url"])] = src
rules = json.loads((HERE / "data/insurance/task20.json").read_text(encoding="utf-8"))
if (HERE / "reference/raw/task20_rules.pdf").exists():
    pdfs[pdf_key(rules["url"])] = HERE / "reference/raw/task20_rules.pdf"
out = HERE / "deploy" / "pdf"
out.mkdir(parents=True, exist_ok=True)
for f in out.glob("*.pdf"):
    if f.stem not in pdfs:
        f.unlink()  # a protocol gone from the KB
for key, src in pdfs.items():
    dst = out / f"{key}.pdf"
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        shutil.copyfile(src, dst)
print(f"deploy/pdf  {len(pdfs)} files, {sum(f.stat().st_size for f in out.glob('*.pdf')) / 2**20:.1f} MB")
