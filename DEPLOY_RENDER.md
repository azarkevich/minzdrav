# Deploying the website to Render (free)

Files involved: `render.yaml` (blueprint), `requirements.txt`, `deploy/` (the runtime knowledge base,
~28 MB, made by `make_deploy.py`), `kb_web.py` (`--host`, `$PORT`, `$KB_WEB_PASSWORD`, `/healthz`).

## One time

1. **GitHub repo.** Create an empty *private* repository on github.com (no README), then in this folder:
   ```
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin master
   ```
   (GitHub asks for a login / personal access token with the `repo` scope.) `deploy/` is committed;
   `protocols/`, `parsed/`, `kb_all/`, `reference/` are not.
2. **Render account.** render.com → sign up with GitHub (no card in the free flow as far as the docs say).
3. **Blueprint.** Dashboard → New + → Blueprint → pick the repository → Render reads `render.yaml`.
   It asks for two secrets:
   - `KB_OPENROUTER_API_KEY` — the OpenRouter key (without it the site works, but a diagnosis that is
     not an exact name — «болезнь Аддисона» — is not recognized; every answer then shows an error for
     the LLM step only when it was needed);
   - `KB_WEB_PASSWORD` — the site password; the browser asks for a login (any) and this password.
4. **Apply.** The first build takes a few minutes. The address is `https://minzdrav-<id>.onrender.com`.

## Behaviour of the free plan

- The service sleeps after 15 minutes without requests; the first request after that waits up to ~1 minute.
- The disk is throwaway: `kb/feedback.jsonl` and `kb/llm_usage.jsonl` vanish on restart. Ratings are
  also printed as `FEEDBACK {json}` lines: Dashboard → the service → Logs → search `FEEDBACK`
  (the log retention is limited — copy the lines out regularly).
- 750 free instance hours per month (enough for one always-awake service).

## Updating the knowledge base

```
python -I kb_build.py --out kb_all     # or only kb_pack.py kb_all/kb.json
python -I make_deploy.py
git add deploy && git commit -m "Update knowledge base" && git push
```
Render redeploys on every push. Each rebuilt `kb.items.bin` (18 MB) stays in git history.

## Local check of the same layout

```
set KB_WEB_PASSWORD=secret
python -I kb_web.py --kb deploy/kb.idx.json --reference deploy/reference
```
