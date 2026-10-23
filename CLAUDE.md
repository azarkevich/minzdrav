# CLAUDE.md — minzdrav tools

A set of tools for working with clinical diagnostic and treatment protocols
(клинические протоколы) published by the Ministry of Health of Belarus:
https://minzdrav.gov.by/ru/dlya-spetsialistov/standarty-obsledovaniya-i-lecheniya/

Each tool is a standalone Python 3 script in this directory. Git repository; script output (`protocols/`) is not committed.

## Tools

| Script | Purpose |
|---|---|
| `minzdrav_protocols_scrapper.py` | Incremental downloader of the protocol files from the site into `./protocols` |
| `protocol_chapter_extractor.py` | Extracts one chapter of a protocol PDF into Markdown + JSONL chunks for semantic search / RAG (`./chapters`) |
| `reference_builder.py` | Expert system stage 0: service catalogue (пост. МЗ №123) + МКБ-10 dictionaries (`./reference`) |
| `protocol_parser.py` | Expert system stage 1: every protocol PDF → JSON tree with pages, clauses, МКБ-10 codes (`./parsed`) |
| `kb_build.py`, `kb.py`, `kb_text.py`, `table_extract.py`, `kb_llm.py` | Expert system: knowledge base (`./kb_all`, `./kb`), question answering CLI + eval, LLM cross-check |
| `kb_mcp.py` | MCP server (stdio, stdlib) exposing the expert system to Claude Desktop / Claude Code |
| `kb_probe.py` | Invariant probes over thousands of dictionary inputs (no labels, no LLM) — run before engine changes |
| `insurance.py` | Insurance rules (Правила №20 ЗАСО «ТАСК»): is the diagnosis an insured event — a separate block in every answer |

Downstream tools should treat `protocols/` (files + `manifest.json` + `index.csv`) as
their input and must not modify it — it is owned by the scraper.

The goal of the project is an expert system for an insurance company: "is diagnostic
service X provided for by the protocol for diagnosis Y" — answered with a verbatim
excerpt and a `<url>#page=N` link. Design: `EXPERT_SYSTEM_DESIGN.md`.

---

## 1. `minzdrav_protocols_scrapper.py` — protocol downloader

Python 3 + `requests` (no other deps).

### Run

```
pip install requests
python minzdrav_protocols_scrapper.py                  # sync into ./protocols
python minzdrav_protocols_scrapper.py -o D:/kp -w 8    # other folder, 8 parallel downloads
python minzdrav_protocols_scrapper.py --dry-run        # report changes, download nothing
python minzdrav_protocols_scrapper.py --prune          # move files gone from the site to _removed/
python minzdrav_protocols_scrapper.py -v               # debug logging (retries etc.)
```

Exit code is 1 if any document failed to download. A full first sync is ~400 files /
~130 MB and takes ~2 min; a no-change re-run takes ~4 s.

### How it works

1. **Scrape catalogue.** The index page links to ~24 category pages
   (`/ru/dlya-spetsialistov/standarty-obsledovaniya-i-lecheniya/<slug>.php`, `index.php`
   excluded). Each category page lists protocols as `<section class="document">` cards:
   `<h4>` = protocol title (sometimes missing), two `<a>` (view + download) to the same
   file under `/upload/...` (mostly `/upload/dadvfiles/CProtokol/`, some `/upload/lcfiles/`).
   Parsed with stdlib `html.parser` (`_LinkParser`); category name comes from
   `<p class="content-title">`. Links outside cards that point to `/upload/*.pdf|doc|…`
   are picked up too.
2. **Dedupe by URL.** The same file can appear in several categories; it's downloaded once
   and stored in the folder of the first category, all categories are kept in the manifest.
   hrefs contain raw Cyrillic, spaces and sometimes trailing spaces — `normalize_url`
   strips and percent-encodes them.
3. **Conditional GET.** Files are static on nginx with `ETag` + `Last-Modified`; the script
   sends `If-None-Match` / `If-Modified-Since` from the previous run and gets a bodiless
   `304` when nothing changed. If the local file is missing, it's re-downloaded.
4. **Content check.** On `200` the body goes to `<file>.part`, length is checked against
   `Content-Length`, and `sha256` is compared with the stored one: same content → only
   metadata is updated; changed → old version moved to `_history/<category>/<name>.<timestamp>.<ext>`.
5. **Removed documents** (in manifest, not on site) get a `removed` timestamp;
   with `--prune` the file is moved to `_removed/`.

### Output layout (`--out`, default `./protocols`)

```
protocols/
  <Category name>/<original file name>   # current versions
  _history/…                             # previous versions of updated files
  _removed/…                             # files no longer on the site (--prune)
  manifest.json                          # sync state, keyed by URL
  index.csv                              # catalogue: category;title;file;last_modified;size;status;url
                                         # (UTF-8 with BOM, ';' separator — opens in Excel)
```

`manifest.json` entry: `path` (relative to out dir), `title`, `categories`, `etag`,
`last_modified`, `sha256`, `size`, `first_seen`, `downloaded`, `checked`, optional
`error` / `removed`. It is saved every 50 documents so an interrupted run resumes cheaply.
Don't hand-edit `path` values: they define where files are expected.

### Gotchas

- **Keep-alive matters.** A new TLS connection to the site costs ~3 s, a `304` on an open
  one ~10 ms. Never `close()` a successful/`304` streamed response — read `r.content`
  (empty) or fully consume `iter_content` so the connection returns to the pool.
  Closing them once made a no-change run take 65 s instead of 4 s.
- One `requests.Session` per worker thread (`threading.local`).
- Retries: `http_get` retries 5xx/429/network errors when opening a request; `sync_one`
  additionally retries the whole download, since read timeouts happen mid-body.
- File names are sanitized for Windows (`safe_name`: illegal chars → `_`, length capped);
  category folder names are Russian.
- If the site layout changes and no categories are found, the script fails loudly
  instead of marking everything as removed. Zero documents in a category is not checked.

### Testing changes

No test suite. Verify against the live site with `--dry-run`, or copy `protocols/` to a
scratch folder and tamper with its `manifest.json` (drop `etag`/`last_modified` and set a
fake `sha256` → expect `[UPDATED]` + `_history`; add a fake URL entry → expect
`[REMOVED FROM SITE]`). Run Python with `-I` when reading downloaded files.

---

## 2. `protocol_chapter_extractor.py` — chapter → RAG chunks

Python 3 stdlib + `pdftotext` on PATH (the Git-for-Windows `/mingw64/bin/pdftotext`
is xpdf 4.00, not poppler). Text PDFs only — no OCR.

### Run

```
python -I protocol_chapter_extractor.py "protocols/<Category>/<file>.pdf"               # chapter whose title starts with ДИАГНОСТИКА
python -I protocol_chapter_extractor.py "<file>.pdf" --chapter 6                        # by number
python -I protocol_chapter_extractor.py "<file>.pdf" --title ЛЕЧЕНИЕ --max-chars 2000
```

If the chapter isn't found, it exits listing the available `ГЛАВА N` titles.
`--title` picks the first chapter whose title starts with the given text
(e.g. «ДИАГНОСТИКА АГ», not «ДИАГНОСТИКА И ЛЕЧЕНИЕ … КРИЗОМ»).

### Output (`--out-dir`, default `./chapters`, not committed)

```
chapters/<pdf name>/
  chapterNN.md      # clean chapter text, one paragraph per line
  chapterNN.jsonl   # chunks: one per top-level clause (11., 12., …)
  glossary.json     # abbreviations of the whole document: {"СМАД": "суточном мониторировании АД"}
```

Chunk fields: `id`, `source`, `document`, `chapter`, `chapter_title`, `clause`, `page`,
`text`, `embedding_text` (context header «Документ. Глава N. Пункт X.» + text +
expanded abbreviations — this is what to embed), `abbreviations`, `appendix_refs`.
A clause longer than `--max-chars` is split by its sub-clauses (11.1, 11.2…), each
prefixed with the clause's intro sentence so it stays self-contained.

### How it works / gotchas

- `pdftotext -layout`; pages split on `\f` (gives `page`). Page footer (page number) and
  the «Национальный правовой Интернет-портал…» header are dropped by regex.
- Paragraphs are rebuilt from indentation: body paragraphs start with ~7 spaces,
  wrapped lines with 0. Headings are centered (indent > 15). A line ending in `-` is
  glued without a space (only real compound hyphens occur in these documents).
- xpdf on Windows can't open non-ASCII paths and can't read stdin, so the PDF is
  copied to a temp `input.pdf` first.
- Glossary is heuristic: for each «(далее – X)» it matches X's letters against initials
  of the preceding words, falling back to "back up to the nearest word starting with
  X's first letter". Expansions keep the case form from the text; a few are noisy
  (ИОАГ, ИААГ); multi-word abbreviations (ХС ЛПВП) are not captured.
- Appendices (приложения) referenced from the chapter are not extracted.

---

## 3. `reference_builder.py` — reference dictionaries (stage 0)

```
python -I reference_builder.py               # downloads sources into reference/raw/ once, builds JSON
python -I reference_builder.py --refresh     # re-download
```

Output (`reference/`, not committed):

- `services.json` — 7055 rows of «Перечень медицинских услуг, медицинских вмешательств»
  (пост. МЗ 05.12.2016 №123; the PDF on minzdrav.gov.by is the consolidated edition incl.
  №90/2017 and №179/2024). Row: `code` (`A09.28.060`), `num` (row number `1.1828`, unique —
  use it as the id), `name`, `class` A/B, `type`/`type_name`, `area`/`area_name`
  (anatomical area for A, specialty for B), `parent` (4-part code → 3-part), `diagnostic`, `excluded`.
  Class B03 holds lab complexes: «Анализ мочи общий» B03.014.006, ОАК B03.014.002, БАК B03.014.004.
- `service_codes.json` — meaning of code parts, parsed from the list's footnotes.
- `icd10.json` — МКБ-10, Russian NSI dictionary 1.2.643.5.1.13.13.11.1005 v2.27
  (via github.com/ak4nv/mkb10): `code`, `name`, `parent`, `actual`.

Gotchas: parse the decree with `pdftotext -raw` — `-layout` misaligns name and code
columns on some pages (it put Нечипоренко at .053 instead of .060). Source defects kept
as is: 5 rows without a code, 2 duplicate codes, ~4 out-of-order codes. Codes in the PDF
mix Cyrillic А/В with Latin — normalized to Latin.

## 4. `protocol_parser.py` — protocol structure (stage 1)

```
python -I protocol_parser.py                 # all protocols, incremental (sha256 + PARSER_VERSION)
python -I protocol_parser.py --only КП12 --force
```

Needs `reference/icd10.json`. ~13 s for all 400 files. Output (`parsed/`, not committed):
`docs/<sha256[:16]>.json`, `index.json`, `coverage.csv` (UTF-8 BOM, `;`).

Tree: `protocols[] → chapters[] (no, title, page, icd10) → clauses[] (id "73", page, page_end,
icd10) → paragraphs[] (page, page_end, text, sub "73.1", heading, icd10, dx markers)`.
`page` is the physical PDF page (for `url#page=N`); `pages[].printed` is the footer number.
Reuses `pdf_to_pages`, `build_glossary`, regexes from `protocol_chapter_extractor.py`
(`sys.path` insert because `python -I` drops the script dir).

`stats.kind`: `standard` (319, «ГЛАВА N» + numbered clauses), `table` (55, >30% of pages are
multi-column tables — old 2005–2012 protocols and big consolidated ones; their paragraph
text is column soup, re-extract tables by column positions or from page images),
`no_chapters` (17), `scan` (9: image pages, only the cover order has text — need OCR).
МКБ-10 detection: Latin or look-alike Cyrillic letter, validated against the dictionary,
ranges expanded; drops "CD B20" and V–Y codes outside МКБ context (ATC codes, doses).
Codes per paragraph are hints, not "applies to" — differential diagnosis lists codes too.
`dx` markers (mandatory/additional/diagnostics/monitoring) are regex hints for stage 2;
wording varies a lot («Для постановки или подтверждения диагноза осуществляются», tables).

## 5. Expert system (stages 2–4): `kb_build.py`, `kb.py`, `kb_text.py`, `table_extract.py`, `kb_llm.py`

```
python -I kb_build.py --out kb_all                     # all 400 docs → kb_all/kb.json (~5 min)
python -I kb_build.py --category Эндокринология        # one category → kb/kb.json (~10 s)
python -I kb.py ask "Диагноз – аутоиммунный тиреоидит, разрешён ли анализ мочи по Ничипоренко" --kb kb_all/kb.json
python -I kb.py ask --diagnosis E06.3 --service ТТГ --json
python -I kb.py eval [eval/<set>.jsonl] [--kb kb_all/kb.json]
python -I table_extract.py "protocols/Урология/<file>.pdf"   # inspect table rows of one PDF
python -I kb_llm.py crosscheck --dry-run               # LLM cross-check: KB_OPENROUTER_API_KEY | KB_ANTHROPIC_API_KEY | KB_LLM_PROVIDER=claude-cli; models KB_LLM_MODEL / KB_LLM_QUESTION_MODEL
python -I kb.py ask "…" --llm                          # LLM fallback for unrecognized diagnoses
```

- `kb_text.py` — normalization: curated `data/abbreviations.json` (priority over the noisy
  per-document glossary), word synonyms `data/synonyms.json`, a crude noun-only Russian stemmer
  (no verb endings: «диабет» must not become «диаб»), fleeting vowel «белок/белка», typo-tolerant
  stem comparison (`close`: one edit for 6+ letters; `prefix_match`: same root ≥5 letters or a
  common prefix ≥6 covering 75% — «сифилис/сифилитическ»), trigram similarity for diagnosis names.
  GENERIC = method words and question verbs that a service match must not require.
- `kb_build.py` — per chapter: МКБ-10 codes (classification lines incl. «Е 21.0», «M.81.0»;
  paragraphs mentioning МКБ / «классификации болезней» / «шифр», «кроме X» excluded; else title
  match; else protocol scope = codes of the preamble + chapter 1 + chapters before the first
  one with lists); names = chapter title, definition «X (синонимы) – …», «Синонимы термина – …»;
  protocol title diseases («…пациентов с артериальной гипертензией») → `protocol_names`.
  Lists: header regexes (mandatory / additional / differential / monitoring / listed = status not
  stated), also found after a clause number glued to a chapter title on flat pages; sub-headers
  «Лабораторные исследования:» (inherit the clause's status); condition sub-headers «при …:»;
  a list ending with "." is suspended, not closed — the next numbered sub-clause resumes it.
  Table pages (≥2) go through `table_extract.py`: one section per table row, `icd10_source="table"`.
- `table_extract.py` — column cuts at blank channels between the «1 2 3 4 5» numbers row,
  column roles from header words (обязательн / дополнительн / наименование), rows separated by
  blank lines, cell lines → items. Works on the urology / STI / skin / nephrology tables
  (320 rows from 12 docs); narrow-column tables (child endocrinology 2019, ЛОР, 2005 orders)
  are not extracted; nephrology rows without blank separators get merged.
- `kb.py` — question split by regex; diagnosis → МКБ-10 over МКБ names + chapter/table/protocol
  names (score < 0.6 → asks to clarify; a close candidate covered by protocols beats an uncovered
  one). Sections tiered: 0 = names the code or a broader one; 1 = bound only by the scope of a
  protocol about another disease (protocol title doesn't name the diagnosis); 2 = only narrower
  codes (complications). Match = all non-generic service stems occur in a list member/segment.
  Verdict per population; `table`/`scan` docs are used only through their table rows, otherwise
  reported as "requires manual check".
- List mode (`Engine.list_services`, answer `mode: "list"`): questions with «перечень / список /
  какой / какие / что входит» (`parse_list_question`, tried before the check split) — «сахарный диабет
  перечень лабораторных анализов», «ангиомиолипома, какой вид КТ используется». Category lab /
  instrumental / consult from the words of the question; what remains after filler words is a service
  filter (same stem matching as check mode). Item kind (`item_kinds`): list sub-header
  «Лабораторные исследования:», else member words (`KIND_WORDS`), else the matched пост. №123 code
  (A09/A26/A08/A12.05… lab, A02–A07/A11 instrumental, B01 consult). Sections: tier-1 chapters dropped
  when anything closer exists; chapters/protocols naming the diagnosis first, the rest flagged
  «смежный раздел»; a «ДИАГНОСТИКА…» chapter about the diagnosis with nothing listed → `text_only`
  note (criteria stated in text, e.g. «ДИАГНОСТИКА СД»). A question that is only a diagnosis («сахарный диабет 2 типа», «E06.3»; no method words — «анализ крови» is not one)
  is a list of all diagnostic measures (`diagnosis_only: true`; also when the LLM finds no service). A question without a check verb is split on
  the first comma: «Сахарный диабет 2 типа, определение гликированного гемоглобина». Eval: `eval/lists.jsonl`
  (`mode: list`, `contains` = item substrings that must be listed). Known gap: chapters covering many
  diseases (child neurology ch. 2) list every disease's items for any of them.
- Eval (2026-10-23), `kb.py eval`: golden 34/34 (written while tuning); endocrinology held-out
  17/20 blind → 19/20; cross-specialty `holdout_all` 11/20 blind → 20/20; `holdout_all2` 13/14
  blind → 14/14. Every blind miss was diagnosis recognition, not service matching. Remaining
  miss: «болезнь Аддисона» (eponym absent from protocols and МКБ names). With `--llm` (OpenRouter,
  Haiku 5.5): all 88 eval questions correct; the LLM is asked only when the diagnosis is not an exact
  name match (score < LLM_DX_SCORE = 0.95) — 6 calls of 88, ~$0.00016 per call; among its codes the
  one covered by protocols wins. Every call is logged to kb/llm_usage.jsonl.
- Coverage on all 400 docs: 23.5k list items, 96% reachable by an МКБ-10 code; 50 parsed docs
  still have no lists found (other wordings / appendices «согласно приложению N» not extracted).

## 6. `kb_mcp.py` — MCP server

`claude mcp add minzdrav -- python -I C:/w/minzdrav/kb_mcp.py` (or the same command in Claude
Desktop's config). Tools: `check_service(diagnosis, service, population?)` (= `kb.py ask`, rendered
text with excerpts and links), `find_diagnosis(text)`, `list_diagnostics(diagnosis, category?, service?, population?)`
(= list mode; category lab / instrumental / consult).
Hand-written newline-delimited JSON-RPC over stdio (no `mcp` package); KB from `$KB_PATH`, else
`kb_all/kb.json`, else `kb/kb.json`. Smoke test: pipe `initialize`, `tools/list`, `tools/call`
JSON lines into it.

## 7. `kb_web.py` + `web/index.html` — local website

`python -I kb_web.py --kb kb_all/kb.json --llm --port 8000` → http://127.0.0.1:8000/. Stdlib
`http.server`, one POST endpoint `/api/ask` (`{question, population?}` → `Engine.ask` JSON plus
`text` = `kb.render`). The page shows diagnosis/service recognition, a coloured verdict per
population, each excerpt with a `url#page=N` link, and the plain-text form for copying; query
history is in the browser's localStorage. The page header shows `VERSION` from `kb_web.py`
(substituted for `__VERSION__` in `web/index.html`; 1.1 = list mode, 1.2 = Enter sends / Shift+Enter new line, spinner, 1.3 = insurance block, 1.4 = insurance / protocols tabs, 1.5 = rating panel between the question and the answer, 1.6 = «уточните диагноз», Google Sheet log, 1.7 = diagnosis-only question → all diagnostic measures, compact rating panel, 2.0 = own PDF viewer, 2.1 = ⚙ settings: open PDFs in the viewer or on the original site, 2.2 = PDFs shipped in deploy/pdf) — bump it on user-visible changes. Binds to 127.0.0.1 only (no auth). `--llm` needs
`KB_OPENROUTER_API_KEY` in the environment of the server process.

Feedback: under each answer the page shows a 3-star rating, tags (диагноз/услуга распознаны неверно, вердикт неверный, цитата не по теме, не нашёл) and a comment. `POST /api/feedback` appends one line to `kb/feedback.jsonl` with the rating and a summary of the answer (diagnosis, service, verdict, evidence links) — low-rated lines are candidates for `eval/` cases.

### Low-memory form (`kb_pack.py`)

`kb_build.py` also writes `kb.idx.json` (section metadata, ~3 MB) + `kb.items.bin` (each section's
`items` as a zlib blob, ~19 MB) next to `kb.json`; `python -I kb_pack.py kb_all/kb.json` converts an
existing build (5 s). `kb.py ... --kb kb_all/kb.idx.json` reads a section's blob only when the
section is checked (`LazySection`, LRU of 64): process ~50 MB instead of ~530 MB (peak 720 MB while
loading kb.json), same answers (eval sets and sample outputs identical). The website and the MCP
server use the idx form by default. To deploy, copy only `kb.idx.json` + `kb.items.bin` + `reference/`.

### Hosting (Render)

`render.yaml` + `requirements.txt` + `deploy/` (`make_deploy.py` copies the packed KB, the two reference JSONs and the PDFs there) — see `DEPLOY_RENDER.md`. `kb_web.py`: `--host`, `$PORT`, optional basic auth from `$KB_WEB_PASSWORD`, unauthenticated `/healthz`, ratings also printed as `FEEDBACK {json}` to stdout (the free plan's disk is throwaway). No git remote is configured yet.

## 8. `insurance.py` — insurance rules (Правила №20 ДСМР ЗАСО «ТАСК»)

```
python -I insurance.py build                      # rules PDF (reference/raw/task20_rules.pdf, downloaded once) → data/insurance/task20.json
python -I insurance.py check E06.3 --service ТТГ  # one diagnosis code
python -I kb.py eval eval/insurance.jsonl --kb kb_all/kb.idx.json
```

The clause ↔ МКБ-10 mapping is hand-written in `data/insurance/task20.rules.json`; `build` adds each
clause's verbatim text and PDF page (pdftotext, numbered clauses «4.1.17», the repeated «4.1.5» becomes
«4.1.5#2»). Both JSONs are committed — the website needs only `task20.json`, no rebuild on deploy.
Clause kinds: `disease` (п. 4.1.x; `named` codes = examples named in the clause → «НЕ СТРАХОВОЙ»,
`class` = only the group it names, e.g. L00–L99 for «кожные заболевания» → «ВЕРОЯТНО НЕ СТРАХОВОЙ»,
a parent rubric of an excluded subcode — E06 vs E06.3 — also «вероятно»; `except_codes` = exceptions of
the exclusion, e.g. маточные кровотечения in 4.1.34; `dx_words` for clauses without codes), `partial`
(operations not paid: 4.1.39–4.1.44), `program` (teeth 4.1.55), `circumstance` (injuries 4.1.6–4.1.10),
`person` (п. 1.4, contracts not concluded), `service` (4.1.36/45/46…, Программа «А» limits 4.3.x matched by
stems of the service or of list items). Notes: п. 4.2 (paid until the final diagnosis), п. 3.1, п. 4.1.1
when the diagnosis says «хронический», п. 4.1.50 when the protocol does not provide for the service.
`Engine.ask` / `list_services` put the result under `insurance`; `kb.render` appends it, the page shows
it in two tabs (`Engine.insurance_check`: when the wording fits several МКБ-10 rubrics with a named
exclusion on one side and insured ones on the other — «сахарный диабет» E10 vs E11, «артрит», «гепатит» — the
level is `unclear` with `variants`; O/P/Q rubrics only when the question is about them): «Правила страхования: <verdict>» and «Клинические протоколы»; a non-insured
(excluded / probable) case opens on the rules tab, an insured one on the protocols tab. Appendices 11–13 (program contents) are not in
the PDF — the answer says to check the contract's program.

## 9. `sheet_log.py` + `web/sheet_logger.gs` — question / rating log in a Google Sheet

`kb_web.py` sends a row per question («Запросы») and per rating («Оценки») to an Apps Script web app when
`$KB_SHEET_URL` / `$KB_SHEET_TOKEN` are set (setup: `DEPLOY_RENDER.md`); one background thread, 3 retries,
`SHEET_FAIL` on stdout otherwise. `mask()` replaces personal data (ФИО with initials / patronymic / after
«пациент», phones, e-mail, dates, passport / personal numbers, «полис № …», long numbers) in everything stored —
the engine still gets the raw question. Personal data in the question also break the diagnosis / service split
(«…, HbA1c, Ковалёва А.В» → service «HbA1c, Ковалёва»), so operators should type only the diagnosis and the service.

## 10. `kb_probe.py` — invariant probes

`python -I kb_probe.py [--n 400] [--seed 1] [--only icd_name,service_not_dx,chapter_name,perturb]` (~5 min at n=300),
failures → `kb/probe_report.json`. Probes: a service name of пост. №123 is not taken for a diagnosis; an МКБ-10 rubric
name resolves to that rubric (names with a code inside or «…в других рубриках» skipped); a protocol chapter title
resolves to a code of the chapter; eval questions keep diagnosis / verdicts with «?», «Пациент, …», a swapped-letter
typo, lower / upper case. 2026-10-23: first run 95 / 94 / 92 / 86 % → after fixes 99–100 / 100 / 92 / 96 %
(fixes: transposition typos in `kb_text.close`, exact-name bonus in `resolve_diagnosis`, `SERVICE_STEM_RE` for
procedure words, «Пациент, …» stripped, a misspelt check verb «разреешна ли» dropped from the service). Left:
chapter_name failures are wrong codes in the KB (stomatology chapters bound to J05/D01); perturb failures are typos
that change a service stem («молочынх») or a list word («лаборатонрых», «преечень»).

### PDF viewer (`web/viewer.html`, 2.0)

Every «стр. N» link of the page goes to `/view?u=<pdf url>&p=<page>&q=<quote>`: PDF.js 3.11.174 (cdnjs) renders
the document lazily, scrolls to the page and highlights the quote — split on «…» into the clause intro and the
member, each matched in the page text with whitespace / hyphens / line breaks ignored, falling back to its longest
prefix (≥ 20 letters); the next page is searched too. A scan or column-soup table page shows «цитату найти не
удалось». The browser can't fetch the ministry's PDFs itself (CORS), so `/pdf?u=` serves them: first from `deploy/pdf/`
(`<kb dir>/pdf`, then `deploy/pdf`; file name `kb_web.pdf_key(url)` = sha1[:16]; `make_deploy.py` copies all 400 KB
PDFs + the insurance rules, ~127 MB), else downloads (hosts minzdrav.gov.by / task.by only, ≤ 80 MB, 20 s × 2) into
`kb/pdfcache/`. minzdrav.gov.by does not answer Render's data centres (a request hung 188 s → 502, task.by 2.6 s),
hence the shipped copies. If the PDF doesn't load in 25 s or fails, the viewer redirects to the original `url#page=N`. Check 2026-10-23: 23 of 24 evidence links from eval answers highlighted (the miss was a site timeout).
The text answer and the MCP server keep the original `url#page=N` links. ⚙ Настройки on the page (localStorage
`pdfmode`: `viewer` / `orig`) switches the page's links back to the original site — a fallback if the proxy is slow on Render.
