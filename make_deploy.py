"""Copy what the website needs at runtime into deploy/ (committed, ~29 MB) for hosts that build from git.

    python -I make_deploy.py       # after kb_build.py / reference_builder.py
"""
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = [("kb_all/kb.idx.json", "deploy/kb.idx.json"), ("kb_all/kb.items.bin", "deploy/kb.items.bin"),
         ("reference/services.json", "deploy/reference/services.json"), ("reference/icd10.json", "deploy/reference/icd10.json")]

for src, dst in FILES:
    (HERE / dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / src, HERE / dst)
    print(f"{dst}  {(HERE / dst).stat().st_size / 2**20:.1f} MB")
