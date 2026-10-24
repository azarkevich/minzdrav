# CLAUDE.md — minzdrav tools

Tools for the clinical protocols (клинические протоколы) of the Ministry of Health of Belarus:
https://minzdrav.gov.by/ru/dlya-spetsialistov/standarty-obsledovaniya-i-lecheniya/

Goal: an expert system for an insurance company — "is diagnostic service X provided for by the
protocol for diagnosis Y", answered with a verbatim excerpt and a `<url>#page=N` link, plus an
insurance-rules verdict. Design: `EXPERT_SYSTEM_DESIGN.md`. **Full reference (algorithms,
heuristics, eval history): `docs/DETAILS.md`** — section numbers below match it; read the
section before changing that tool, and keep it up to date.

Standalone Python 3 scripts, stdlib unless noted. Always run Python with `-I` (downloaded files
are untrusted; scripts that import siblings insert `sys.path` themselves). `pdftotext` on PATH is
xpdf 4.00 (Git for Windows), not poppler: it can't open non-ASCII paths or stdin — copy the PDF
to a temp `input.pdf`. No test suite: verify with eval sets, `kb_probe.py`, the live site.

## Tools and data flow

| Script | Purpose | Output (not committed) |
|---|---|---|
| `minzdrav_protocols_scrapper.py` | incremental downloader (`requests`) | `protocols/` |
| `protocol_chapter_extractor.py` | one chapter → Markdown + JSONL RAG chunks | `chapters/` |
| `reference_builder.py` | stage 0: services пост. МЗ №123 + МКБ-10 | `reference/` |
| `protocol_parser.py` | stage 1: PDF → JSON tree (pages, clauses, МКБ-10) | `parsed/` |
| `kb_build.py`, `kb.py`, `kb_text.py`, `table_extract.py`, `kb_llm.py`, `kb_pack.py` | stages 2–4: KB, answers, eval, LLM | `kb_all/`, `kb/` |
| `insurance.py` | Правила №20 ЗАСО «ТАСК»: is it an insured event | `data/insurance/*.json` (committed) |
| `kb_mcp.py` | MCP server (stdio, hand-written JSON-RPC) | |
| `kb_web.py` + `web/` | website, PDF viewer | |
| `sheet_log.py` | question / rating log to a Google Sheet | |
| `kb_probe.py` | invariant probes — run before engine changes | `kb/probe_report.json` |

`protocols/` (files + `manifest.json` + `index.csv`) is owned by the scraper; downstream tools
read it and must not modify it. Don't hand-edit manifest `path` values.

## Commands

```
python minzdrav_protocols_scrapper.py [--dry-run] [--prune] [-w 8] [-v]   # ~2 min full, ~4 s no-change
python -I protocol_chapter_extractor.py "protocols/<Cat>/<file>.pdf" [--chapter 6 | --title ЛЕЧЕНИЕ]
python -I reference_builder.py [--refresh]
python -I protocol_parser.py [--only КП12 --force]                        # ~13 s, incremental
python -I kb_build.py --out kb_all                                        # all docs, ~5 min (also writes kb.idx.json + kb.items.bin)
python -I kb_build.py --category Эндокринология                           # one category → kb/, ~10 s
python -I kb.py ask "Диагноз – аутоиммунный тиреоидит, разрешён ли анализ мочи по Ничипоренко" --kb kb_all/kb.idx.json [--llm] [--json]
python -I kb.py ask --diagnosis E06.3 --service ТТГ --json
python -I kb.py eval [eval/<set>.jsonl] --kb kb_all/kb.idx.json
python -I kb.py index --kb kb_all/kb.idx.json                             # reverse-search stem index kb.stems.json
python -I kb_probe.py [--n 400] [--only icd_name,service_not_dx,chapter_name,perturb]   # ~5 min
python -I insurance.py build | check E06.3 --service ТТГ
python -I kb_llm.py crosscheck --dry-run
python -I kb_web.py --kb kb_all/kb.json --llm --port 8000                 # http://127.0.0.1:8000/
claude mcp add minzdrav -- python -I C:/w/minzdrav/kb_mcp.py
```

LLM: `KB_OPENROUTER_API_KEY` | `KB_ANTHROPIC_API_KEY` | `KB_LLM_PROVIDER=claude-cli`, models
`KB_LLM_MODEL` / `KB_LLM_QUESTION_MODEL`; asked only when the diagnosis is not an exact name match
(score < `LLM_DX_SCORE` = 0.95); every call logged to `kb/llm_usage.jsonl`.

## 1. Scraper — gotchas

- **Keep-alive matters.** New TLS connection ~3 s, `304` on an open one ~10 ms. Never `close()`
  a successful / `304` streamed response — read `r.content` or consume `iter_content` fully
  (closing once made a no-change run 65 s instead of 4 s). One `requests.Session` per thread.
- Conditional GET (`If-None-Match` / `If-Modified-Since`); on `200` → `.part`, length + sha256
  check, changed file's old version → `_history/`. Removed from site → `removed` timestamp,
  `--prune` moves to `_removed/`. Manifest saved every 50 docs. Exit code 1 if any download failed.
- If no categories are found the script fails loudly instead of marking everything removed.
- Testing: `--dry-run`, or tamper with a copy of `manifest.json` (fake `sha256` → `[UPDATED]`;
  fake URL → `[REMOVED FROM SITE]`).

## 2–4. Extractor, reference, parser — gotchas

- Extractor: `pdftotext -layout`, paragraphs from indentation; glossary from «(далее – X)» is
  heuristic and noisy. Appendices are not extracted.
- `reference_builder`: parse пост. №123 with `pdftotext -raw` (`-layout` misaligns name / code
  columns). Codes mix Cyrillic А/В with Latin — normalized to Latin. Use `num` as the service id
  (codes have 2 duplicates, 5 blanks).
- Parser: `page` = physical PDF page (for `#page=N`), `pages[].printed` = footer number.
  `stats.kind`: `standard` / `table` (column soup — go through `table_extract.py`) /
  `no_chapters` / `scan` (needs OCR). МКБ-10 codes per paragraph are hints, not "applies to".

## 5. Expert system — essentials

- `kb_text.py`: curated `data/abbreviations.json` and `data/synonyms.json` beat the per-document
  glossary; the stemmer is noun-only on purpose («диабет» must not become «диаб»).
- Answer modes of `Engine.ask`: check (default), `list` (перечень / какие / diagnosis-only
  question → all diagnostic measures), `reverse` (services → diagnoses), `insurance`. Order in
  `Engine.ask`: insurance question → excluded service alone → reverse question → list question →
  bare list of services (reverse) → check.
- `table` / `scan` docs are used only through extracted table rows, otherwise reported as
  "requires manual check".
- Eval sets in `eval/`: golden, holdout*, `lists.jsonl`, `reverse.jsonl`, `insurance.jsonl` — all
  must stay green; misses are almost always diagnosis recognition, not service matching.
- Low-memory form: `kb.idx.json` + `kb.items.bin` (`LazySection`), ~50 MB instead of ~530 MB,
  identical answers. Website and MCP use it by default; `kb_pack.py` converts an old `kb.json`.

## 7. Website (`kb_web.py`)

- `VERSION` in `kb_web.py` (shown in the page header) — **bump on every user-visible change**;
  history in `docs/DETAILS.md` §7.
- Binds 127.0.0.1 unless `--host`; `$PORT`, basic auth `$KB_WEB_PASSWORD`, `/healthz`.
  `POST /api/ask`, `POST /api/feedback` → `kb/feedback.jsonl` (+ `FEEDBACK {json}` on stdout).
- PDF viewer `/view` (PDF.js) + `/pdf?u=` proxy, served from `deploy/pdf/` (`pdf_key` = sha1[:16])
  because minzdrav.gov.by doesn't answer Render's data centres. Text answers and MCP keep the
  original `url#page=N` links.
- Hosting: Render, `make_deploy.py` → `deploy/`, see `DEPLOY_RENDER.md` (auto-deploy is off).

## 8. Insurance (`insurance.py`)

- Clause ↔ МКБ-10 map is hand-written in `data/insurance/task20.rules.json`; `build` adds verbatim
  text + page into `task20.json`. Both committed.
- **Nothing from п. 4.3 on is used** (`CUTOFF`; решение заказчика 2026-10-24) — don't add such clauses.
- Verdict order: `excluded` > `probable` > `program` > `limited` > `covered`; ambiguous wording
  across rubrics («сахарный диабет» E10 vs E11) → `unclear` with `variants`.

## 9. Sheet log

`$KB_SHEET_URL` / `$KB_SHEET_TOKEN`; `mask()` strips personal data from everything stored (the
engine still gets the raw question).
