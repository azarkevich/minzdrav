@echo off
rem Starts the expert system website and a public Cloudflare quick tunnel.
rem Needs: python, cloudflared (winget install Cloudflare.cloudflared), KB_OPENROUTER_API_KEY user variable.
chcp 65001 >nul
cd /d "%~dp0"

if "%KB_OPENROUTER_API_KEY%"=="" (
  echo WARNING: KB_OPENROUTER_API_KEY is not set - the server starts without LLM.
  set LLMFLAG=
) else (
  set LLMFLAG=--llm
)

rem Website in its own window (close it to stop the server)
start "minzdrav-web" python -I kb_web.py --kb kb_all/kb.idx.json %LLMFLAG% --port 8000

echo Waiting for the knowledge base to load...
:wait
timeout /t 2 >nul
curl -s -o nul http://127.0.0.1:8000/ || goto wait
echo Local:  http://127.0.0.1:8000/
echo Public link appears below as https://....trycloudflare.com (Ctrl+C stops the tunnel)
cloudflared tunnel --url http://127.0.0.1:8000
