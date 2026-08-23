"""Focused v3 product, published curriculum, and Create adapter tests."""
import json
import re
from pathlib import Path

import pytest

try:
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    import server
except Exception as exc:  # pragma: no cover - dependency-less test environments
    pytest.skip(f"server import 불가(의존성 미설치): {exc}", allow_module_level=True)

from agent.create import CreateOrchestratorAdapter, SUPPORTED_CODING_TYPES
from agent.session_store import InMemorySessionStore
from curriculum import get_lesson, list_grade_bands, validate_catalog


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "create_sessions", InMemorySessionStore())
    return TestClient(server.app)


@pytest.mark.parametrize("coding_type", ["react", "blockly", "hybrid"])
def test_create_adapter_forces_design_and_keeps_existing_coding_types(coding_type):
    adapter = CreateOrchestratorAdapter.start(coding_type)
    payload = adapter.legacy_chat_payload("만들자", runtime_error="preview failed")

    assert payload == {
        "session_id": adapter.session_id,
        "message": "만들자",
        "mode": "design",
        "coding_type": coding_type,
        "runtime_error": "preview failed",
    }


@pytest.mark.parametrize("coding_type", ["quick", "web", "hardware", "REACT", ""])
def test_create_adapter_rejects_new_or_legacy_aliases(coding_type):
    with pytest.raises(ValueError, match="coding_type"):
        CreateOrchestratorAdapter.start(coding_type)


def test_runtime_neutral_catalog_contains_27_published_lessons():
    expected_minutes = {"elementary": 40, "middle": 45, "high": 50}
    expected_slides = {"elementary": 17, "middle": 19, "high": 21}
    required_lesson_fields = {
        "id",
        "lesson_no",
        "status",
        "title",
        "summary",
        "projectType",
        "project_type",
        "projectLabel",
        "duration_min",
        "objectives",
        "standards",
        "materials",
        "successCriteria",
        "vocabulary",
        "rubric",
        "differentiation",
        "studentArtifacts",
        "slides",
    }

    validate_catalog()
    bands = list_grade_bands()

    assert [band["id"] for band in bands] == ["elementary", "middle", "high"]
    assert sum(len(band["lessons"]) for band in bands) == 27
    assert sum(len(lesson["slides"]) for band in bands for lesson in band["lessons"]) == 513
    for band in bands:
        assert band["deckVersion"] == 3
        assert band["deckProfile"]["slideCountPerLesson"] == expected_slides[band["id"]]
        assert band["lesson_count"] == 9
        assert band["classMinutes"] == expected_minutes[band["id"]]
        assert band["class_minutes"] == expected_minutes[band["id"]]
        assert len(band["lessons"]) == 9
        assert [lesson["lesson_no"] for lesson in band["lessons"]] == list(range(1, 10))
        for lesson in band["lessons"]:
            assert required_lesson_fields <= set(lesson)
            assert lesson["id"] == f"{band['id']}-{lesson['lesson_no']:02d}"
            assert lesson["status"] == "published"
            assert lesson["title"].strip()
            assert lesson["summary"].strip()
            assert lesson["objectives"]
            assert lesson["standards"]
            assert lesson["materials"]
            assert lesson["successCriteria"]
            assert lesson["vocabulary"]
            assert lesson["rubric"]
            assert lesson["differentiation"]["support"]
            assert lesson["differentiation"]["challenge"]
            assert lesson["studentArtifacts"]
            assert lesson["deckVersion"] == 3
            assert len(lesson["slides"]) == expected_slides[band["id"]]
            assert sum(slide["minutes"] for slide in lesson["slides"]) == band["classMinutes"]
            assert all(slide["teacherNote"].strip() for slide in lesson["slides"])
            assert {slide["type"] for slide in lesson["slides"]} >= {
                "title",
                "goals",
                "hook",
                "vocabulary",
                "concept",
                "example",
                "check",
                "setup",
                "plan",
                "build",
                "checkpoint",
                "troubleshoot",
                "differentiate",
                "exit",
            }
            quiz_slides = [slide for slide in lesson["slides"] if slide["type"] in {"check", "exit"}]
            assert len(quiz_slides) == 2
            assert all(len(slide["choices"]) == 4 and slide["explanation"] for slide in quiz_slides)
            build_slides = [slide for slide in lesson["slides"] if slide["type"] == "build"]
            assert build_slides
            assert all(slide["checkpoint"] and slide["codingType"] in SUPPORTED_CODING_TYPES for slide in build_slides)


def test_all_lesson_standards_match_the_reviewed_2022_notice_mapping():
    bands = list_grade_bands()
    codes = {
        standard["code"]
        for band in bands
        for lesson in band["lessons"]
        for standard in lesson["standards"]
    }

    assert len(codes) == 28
    assert all(
        band["standardsSource"]["title"].startswith("교육부 고시 제2022-33호")
        for band in bands
    )
    assert all(
        band["standardsSource"]["verifiedOn"] == "2026-08-23"
        for band in bands
    )
    assert bands[0]["lessons"][0]["standards"] == [
        {
            "code": "[6실05-02]",
            "text": "컴퓨터에게 명령하는 방법을 체험하고, 주어진 문제를 해결하는 프로그램을 작성한다.",
        },
        {
            "code": "[6실04-03]",
            "text": "제작한 발표 자료를 사이버 공간에 공유하고, 건전한 정보기기의 활용을 실천한다.",
        },
    ]
    assert bands[1]["lessons"][2]["standards"] == [
        {
            "code": "[9정03-07]",
            "text": "프로그램 작성에서 함수를 활용하고, 프로그램 수행 결과를 디버거로 분석하여 오류를 수정한다.",
        }
    ]
    assert bands[2]["lessons"][7]["standards"][0] == {
        "code": "[12정01-01]",
        "text": "유무선 네트워크의 특성을 이해하고, 컴퓨팅 시스템 간 공유, 협력, 소통을 위한 네트워크 환경을 구성한다.",
    }


def test_curriculum_quiz_answers_are_not_locked_to_one_choice_position():
    answers = [
        slide["answer"]
        for band in list_grade_bands()
        for lesson in band["lessons"]
        for slide in lesson["slides"]
        if slide["type"] in {"check", "exit"}
    ]

    assert len(answers) == 54
    assert len(set(answers)) >= 3
    assert max(answers.count(position) for position in range(4)) < len(answers) * 0.7


def test_catalog_lesson_lookup_returns_detached_detail_with_course_context():
    lesson = get_lesson("elementary", 1)

    assert lesson is not None
    assert lesson["id"] == "elementary-01"
    assert lesson["grade_band"] == "elementary"
    assert lesson["grade_label"] == "초등"
    assert lesson["subject"] == "실과"
    assert lesson["class_minutes"] == 40
    assert get_lesson("elementary", 0) is None
    assert get_lesson("elementary", 10) is None
    assert get_lesson("college", 1) is None

    lesson["title"] = "mutated by caller"
    assert get_lesson("elementary", 1)["title"] != "mutated by caller"


def test_home_exposes_only_learn_and_create(client):
    response = client.get("/api/v3/home")

    assert response.status_code == 200
    assert response.json()["product"] == {"name": "MODI Planet", "version": "3.0"}
    assert [mode["id"] for mode in response.json()["modes"]] == ["learn", "create"]
    assert "quick" not in response.text.lower()


def test_curriculum_routes_return_published_courses_and_lesson_detail(client):
    all_bands = client.get("/api/v3/curriculum")
    elementary = client.get("/api/v3/curriculum/elementary")
    lesson = client.get("/api/v3/curriculum/elementary/1")

    assert all_bands.status_code == 200
    assert len(all_bands.json()["grade_bands"]) == 3
    assert "placeholder" not in all_bands.text
    assert elementary.status_code == 200
    assert elementary.json()["label"] == "초등"
    assert elementary.json()["classMinutes"] == 40
    assert len(elementary.json()["lessons"]) == 9
    assert lesson.status_code == 200
    assert lesson.json()["id"] == "elementary-01"
    assert lesson.json()["status"] == "published"
    assert lesson.json()["grade_band"] == "elementary"
    assert sum(slide["minutes"] for slide in lesson.json()["slides"]) == 40
    assert client.get("/api/v3/curriculum/college").status_code == 404
    assert client.get("/api/v3/curriculum/elementary/10").status_code == 404
    assert client.get("/api/v3/curriculum/college/1").status_code == 404


def test_lms_route_serves_the_curriculum_player(client):
    response = client.get("/lms")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MODI Planet" in response.text
    assert "교육과정" in response.text
    assert 'class="history-rail"' in response.text
    assert response.text.count("data-rail-level=") == 3
    assert 'href="/#create"' in response.text
    assert 'class="site-header"' not in response.text

    logo = client.get("/static/assets/brand/logo.svg")
    player_script = client.get("/static/lms.js")
    assert logo.status_code == 200
    assert "#ff4438" in logo.text.lower()
    assert player_script.status_code == 200
    assert "data-plan-lesson" in player_script.text
    assert "data-start-lesson" in player_script.text


def test_mobile_ux_contract_keeps_lms_and_ai_lab_touch_ready(client):
    lms_html = client.get("/lms").text
    app_html = client.get("/").text
    lms_css = client.get("/static/lms.css").text
    app_css = client.get("/static/app.css").text
    lms_script = client.get("/static/lms.js").text
    app_script = client.get("/static/app.js").text

    assert "viewport-fit=cover" in lms_html
    assert "interactive-widget=resizes-content" in app_html
    assert '<dialog class="lesson-player"' in lms_html
    assert 'id="studioToggle"' in lms_html
    assert 'role="progressbar"' in lms_html
    assert "100dvh" in lms_css
    assert ".learning-studio.mobile-open" in lms_css
    assert ".mobile-player-actions" in lms_css
    assert "state.quizAnswers" in lms_script
    assert 'document.querySelectorAll(".player-header, .slide-rail, .slide-stage")' in lms_script
    assert "studioBackdrop.disabled = !open" in lms_script
    assert 'learningStudio.addEventListener("keydown"' in lms_script
    assert "event.isComposing" in lms_script
    assert 'class="ai-lab-link" href="/#create" aria-label="AI LAB"' in lms_html
    assert lms_html.count('class="rail-nav-label"') == 3
    assert 'class="player-ai-lab" href="/#create"' in lms_html
    assert 'class="player-ai-lab-mark"' in lms_html
    assert ".player-ai-lab" in lms_css
    assert "min-height: 44px" in lms_css
    assert ".rail-nav-label" in lms_css
    assert ".rail-nav span,\n  .rail-history" not in lms_css

    assert "mobile-workspace-switch" in app_script
    assert "mobile-panel-hidden" in app_script
    assert "event.isComposing" in app_script
    assert "100dvh" in app_css
    assert ".mobile-workspace-switch" in app_css
    assert ".back-button," in app_css
    assert "min-height: 44px" in app_css
    assert app_html.count('class="rail-nav-label"') == 3
    assert ".rail-nav-label" in app_css
    assert ".rail-nav span,\n  .rail-history" not in app_css


def test_lms_preview_seeds_polished_results_before_generation(client):
    script = client.get("/static/lms.js").text
    styles = client.get("/static/lms.css").text

    for level in ("elementary", "middle", "high"):
        for lesson_no in range(1, 10):
            # Every lesson has one result-preview preset and one deck-scene profile.
            assert script.count(f'"{level}-{lesson_no:02d}"') == 2

    assert "function renderPresetPreview" in script
    assert "function renderWebPreset" in script
    assert "function renderHardwarePreset" in script
    assert "function renderConnectedPreset" in script
    assert "아직 만든 작품이 없어요" not in script
    assert 'state.previewSource = "mine"' in script
    assert 'state.files = event.generated_code' in script
    assert 'data-preview-source="preset"' in script
    assert 'data-preview-action="demo"' in script
    assert 'data-preview-mode="' in script
    assert 'data-world="' in script
    assert "seed-scene-camera" in script
    assert "WORLD_PROFILES" in script
    assert "seed-world-portal" in script
    assert "seed-world-depth" in script
    assert "function dismissLessonPlayer()" in script
    assert "if (state.activeLesson) {\n      dismissLessonPlayer();\n    }" in script
    assert "샘플 시뮬레이션" in script
    assert 'learningStudio.addEventListener("pointermove"' in script
    assert '"--world-pan-x"' in script
    assert '"--object-rotate-y"' in script

    assert ".preview-showcase" in styles
    assert ".preview-demo-badge" in styles
    assert ".preview-window-generated iframe" in styles
    assert "container-type: inline-size" in styles
    assert "min-height: 44px" in styles
    assert "perspective: 1050px" in styles
    assert "transform-style: preserve-3d" in styles
    assert ".seed-modi-core::before" in styles
    assert ".seed-rover::before" in styles
    assert "/static/assets/worlds/elementary-world.png" in styles
    assert "/static/assets/worlds/middle-world.png" in styles
    assert "/static/assets/worlds/high-world.png" in styles
    assert ".world-high .seed-rover" in styles
    assert "Fixed preview chrome" in styles
    assert "transform: none !important" in styles
    assert "seed-world-content-awaken" in styles
    assert "prefers-reduced-motion: reduce" in styles

    for asset in ("elementary-world.png", "middle-world.png", "high-world.png"):
        response = client.get(f"/static/assets/worlds/{asset}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"


def test_lesson_decks_render_unique_layouts_scenes_and_modi_assets(client):
    script = client.get("/static/lms.js").text
    styles = client.get("/static/lms.css").text
    html = client.get("/lms").text

    required_types = {
        "title", "goals", "hook", "vocabulary", "concept",
        "example", "check", "setup", "plan", "build",
        "checkpoint", "troubleshoot", "differentiate", "rubric", "exit",
    }
    layout_pattern = (
        r"^\s*(title|goals|hook|vocabulary|concept|example|check|setup|plan|"
        r"build|checkpoint|troubleshoot|differentiate|rubric|exit):\s*"
        r'\{[^\n}]*\blayout:\s*"([^"]+)"'
    )
    layouts = dict(re.findall(layout_pattern, script, re.MULTILINE))
    assert set(layouts) == required_types
    assert len(set(layouts.values())) == len(required_types)

    scene_pattern = (
        r'^\s*"((?:elementary|middle|high)-\d{2})":\s*'
        r'\{[^\n}]*\bkind:\s*"([^"]+)"'
    )
    scenes = re.findall(scene_pattern, script, re.MULTILINE)
    expected_lessons = {
        f"{level}-{number:02d}"
        for level in ("elementary", "middle", "high")
        for number in range(1, 10)
    }
    assert len(scenes) == 27
    assert {key for key, _kind in scenes} == expected_lessons
    assert len({kind for _key, kind in scenes}) == 27

    assert "function renderLessonSlideVisual" in script
    assert "function renderLessonScene" in script
    assert "function classifyBuildScene" in script
    assert "function renderSlideVisualDiagram" not in script
    assert "function renderSlideVisualMedia" not in script
    assert "SLIDE_VISUAL_RENDERERS" in script
    assert "LESSON_SCENE_PROFILES" in script
    assert "SLIDE_VISUAL_META" in script
    assert "MODULE_VISUAL_MATCHES" in script
    assert 'slideHeading.insertAdjacentHTML("afterend"' in script
    assert 'data-layout="' in script
    assert 'data-scene-kind="' in script
    assert 'data-lesson-key="' in script
    assert 'data-slide-type="' in script
    assert "data-visual-body" in script
    for build_kind in (
        "brief", "blueprint", "assembly", "logic",
        "instrument", "testbench", "iteration", "storyboard",
    ):
        assert f'"{build_kind}"' in script

    check_renderer = script.split("function renderCheckVisual", 1)[1].split(
        "function renderSetupVisual", 1
    )[0]
    exit_renderer = script.split("function renderExitVisual", 1)[1].split(
        "const SLIDE_VISUAL_RENDERERS", 1
    )[0]
    assert "slide.answer" not in check_renderer
    assert "slide.explanation" not in check_renderer
    assert "slide.answer" not in exit_renderer
    assert "slide.explanation" not in exit_renderer

    assert ".lesson-slide-visual" in styles
    assert "Unique lesson scene system" in styles
    assert "container-type: inline-size" in styles
    assert "@container (max-width: 620px)" in styles
    for layout in layouts.values():
        assert f".visual-{layout}" in styles
    assert "20260824-ai-lab-nav" in html

    expected_assets = {
        "modi-kit-flatlay.jpg": "image/jpeg",
        "modi-ecosystem.jpg": "image/jpeg",
        "modi-car-robot.jpg": "image/jpeg",
        "modi-smart-farm.jpg": "image/jpeg",
        "modi-control-workspace.jpg": "image/jpeg",
        "modi-hardware-kit.png": "image/png",
        "web-modi-hybrid.png": "image/png",
        "modi-network.png": "image/png",
        "modi-display.png": "image/png",
        "modi-dial.png": "image/png",
        "modi-speaker.png": "image/png",
        "modi-led.png": "image/png",
        "modi-battery.png": "image/png",
    }
    for asset, content_type in expected_assets.items():
        response = client.get(f"/static/assets/lesson-visuals/{asset}")
        assert response.status_code == 200
        assert response.headers["content-type"] == content_type


def test_grade_bands_end_with_three_distinct_world_projects():
    bands = {band["id"]: band for band in list_grade_bands()}

    assert bands["elementary"]["lessons"][6]["title"].startswith("별빛 탐사대")
    assert bands["middle"]["lessons"][6]["title"].startswith("NOVA 페스티벌")
    assert bands["high"]["lessons"][6]["title"].startswith("ORBIT-9 미션")
    assert len({band["finalGoal"] for band in bands.values()}) == 3
    assert all("로봇카" not in json.dumps(band, ensure_ascii=False) for band in bands.values())


def test_create_page_bundles_the_official_example_catalog(client):
    script = client.get("/static/app.js")

    assert script.status_code == 200
    assert script.text.count('{ ref: "') == 19
    assert '["전체", "학습", "게임", "인터랙티브", "예술", "서비스", "로봇"]' in script.text
    assert "MODI 수학 대시보드" in script.text
    assert "장애물 회피 조이스틱 자동차" in script.text
    assert "당근마켓 스타일 동네거래 앱" in script.text
    assert "data-example-category" in script.text
    assert "data-example-ref" in script.text


@pytest.mark.parametrize("coding_type", sorted(SUPPORTED_CODING_TYPES))
def test_create_session_accepts_only_existing_coding_types(client, coding_type):
    response = client.post("/api/v3/create/sessions", json={"coding_type": coding_type})

    assert response.status_code == 201
    body = response.json()
    assert body["session_id"]
    assert body["mode"] == "design"
    assert body["coding_type"] == coding_type
    assert server.create_sessions.get(body["session_id"]).coding_type == coding_type


def test_create_session_rejects_unknown_coding_type(client):
    response = client.post("/api/v3/create/sessions", json={"coding_type": "web"})

    assert response.status_code == 422
    assert "react" in response.json()["detail"]


def test_create_chat_delegates_to_legacy_chat_with_forced_mode(client, monkeypatch):
    created = client.post(
        "/api/v3/create/sessions", json={"coding_type": "hybrid"}
    ).json()
    captured = {}

    async def fake_legacy_chat(*, req, request, user_id):
        captured.update({"request": req, "user_id": user_id, "path": request.url.path})
        return JSONResponse({"delegated": True})

    monkeypatch.setattr(server, "chat", fake_legacy_chat)
    response = client.post(
        created["chat_endpoint"],
        params={"user_id": "student-1"},
        json={
            "message": "센서와 웹을 연결하고 싶어",
            "runtime_error": "boom",
            "mode": "quick",
            "coding_type": "react",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"delegated": True}
    assert captured["request"].session_id == created["session_id"]
    assert captured["request"].mode == "design"
    assert captured["request"].coding_type == "hybrid"
    assert captured["request"].runtime_error == "boom"
    assert captured["user_id"] == "student-1"


def test_create_chat_rejects_unknown_session_without_calling_chat(client, monkeypatch):
    async def should_not_run(**kwargs):
        raise AssertionError("legacy chat must not run")

    monkeypatch.setattr(server, "chat", should_not_run)
    response = client.post(
        "/api/v3/create/sessions/missing/chat", json={"message": "hello"}
    )

    assert response.status_code == 404


def test_product_index_and_static_mount_are_wired(client):
    response = client.get("/")
    static_mount = next(route for route in server.app.routes if route.path == "/static")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MODI Planet" in response.text
    assert Path(static_mount.app.directory).resolve() == Path(server._WEB_DIR).resolve()
