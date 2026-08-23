# MODI Planet 3.0 — Local Product Build

This package layers the first MODI Planet 3.0 product experience on top of the
existing edu-agent generation engine.

## Included

- Home with explicit Learn and Create entry points
- Learn curriculum with 27 published lessons across elementary, middle, and high school
- Three difficulty bands, each with 3 Web, 3 hardware, and 3 Web + hardware lessons
- Per-lesson plans with objectives, standards, materials, timed flow, teacher notes, and assessment
- Full-screen lesson player with pacing timer, slide navigation, quiz feedback, activity prompts, and AI workspace
- Create selection for Web (`react`), MODI (`blockly`), and Web + MODI (`hybrid`)
- Guided Create sessions fixed to the existing `design → implement → verify` flow
- Streaming workspace for design notes, generated code, Blockly output, and progress
- Existing `/chat`, RAG, build validation, sessions, quota, and observability preserved

The curriculum data is stored in `curriculum/{elementary,middle,high}.json`.
Every lesson's slide minutes are validated against the configured 40, 45, or
50 minute class period at load time. The interface uses the original MODI
Planet logo, official UI color tokens, and locally bundled Pretendard font.

## Run on Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
$env:PYTHONUTF8 = "1"
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for AI LAB or
`http://127.0.0.1:8000/lms` for the curriculum.

For actual AI generation, configure either a signed-in local Claude CLI or the
Anthropic API settings described in `.env.example`. Home, Learn, Create setup,
curriculum APIs, and session creation can be tested without an LLM call.

## Verify

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -p no:cacheprovider
node --check web/app.js
node --check web/lms.js
node --test hybrid/sdk.test.mjs
```

## Data safety

The clean deliverable omits historical `projects/`, conversation-derived RAG
data, deployment environment files, caches, and local virtual environments.
Build a reviewed RAG corpus from `reference/` before enabling local RAG. The
attached `Microsoft.Services.Store.winmd` is unrelated to this FastAPI project
and is not used.
