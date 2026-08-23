# MODI Planet 3.0 — Local P1

This package layers the first MODI Planet 3.0 product experience on top of the
existing edu-agent generation engine.

## Included

- Home with explicit Learn and Create entry points
- Learn school-band selection and nine-slot curriculum shell
- Create selection for Web (`react`), MODI (`blockly`), and Web + MODI (`hybrid`)
- Guided Create sessions fixed to the existing `design → implement → verify` flow
- Streaming workspace for design notes, generated code, Blockly output, and progress
- Existing `/chat`, RAG, build validation, sessions, quota, and observability preserved

The Learn lesson engine and the first 45-minute lesson are intentionally P2.
Lesson titles shown in P1 are placeholders or non-final layout examples.

## Run on Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
$env:PYTHONUTF8 = "1"
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`.

For actual AI generation, configure either a signed-in local Claude CLI or the
Anthropic API settings described in `.env.example`. Home, Learn, Create setup,
curriculum APIs, and session creation can be tested without an LLM call.

## Verify

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -p no:cacheprovider
node --check web/app.js
node --test hybrid/sdk.test.mjs
```

## Data safety

The clean deliverable omits historical `projects/`, conversation-derived RAG
data, deployment environment files, caches, and local virtual environments.
Build a reviewed RAG corpus from `reference/` before enabling local RAG. The
attached `Microsoft.Services.Store.winmd` is unrelated to this FastAPI project
and is not used.
