"""Pack a knowledge base for low-memory serving.

    python -I kb_pack.py kb_all/kb.json        # → kb_all/kb.idx.json + kb_all/kb.items.bin

kb.json keeps every list item of every section in memory (~560 MB of process for the full corpus).
The packed form keeps only section metadata in the index (~1.5 MB) and stores each section's
`items` as an independent zlib blob in kb.items.bin; kb.py reads a blob when a section is checked.
"""
import json
import sys
import zlib
from pathlib import Path


def pack(kb: dict, out_dir: Path, name: str = "kb"):
    blob = out_dir / f"{name}.items.bin"
    sections = []
    with blob.open("wb") as f:
        for s in kb["sections"]:
            data = zlib.compress(json.dumps(s["items"], ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
            sections.append({**{k: v for k, v in s.items() if k != "items"}, "_blob": [f.tell(), len(data)]})
            f.write(data)
    idx = {**kb, "sections": sections, "packed": blob.name}
    (out_dir / f"{name}.idx.json").write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return blob.stat().st_size


def main():
    src = Path(sys.argv[1])
    size = pack(json.loads(src.read_text(encoding="utf-8")), src.parent)
    print(f"{src.parent / 'kb.idx.json'}, {src.parent / 'kb.items.bin'} ({size / 2**20:.1f} MB)")


if __name__ == "__main__":
    main()
