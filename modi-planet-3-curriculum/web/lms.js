(() => {
  "use strict";

  const main = document.getElementById("lmsMain");
  const planDialog = document.getElementById("planDialog");
  const planKicker = document.getElementById("planKicker");
  const planTitle = document.getElementById("planTitle");
  const planBody = document.getElementById("planBody");
  const lessonPlayer = document.getElementById("lessonPlayer");
  const toastElement = document.getElementById("toast");

  const LEVELS = [
    { id: "elementary", difficulty: "초급", thumbnail: "beginner-thumbnail.png" },
    { id: "middle", difficulty: "중급", thumbnail: "intermediate-thumbnail.png" },
    { id: "high", difficulty: "고급", thumbnail: "advanced-thumbnail.png" }
  ];

  const MODES = {
    web: { label: "Web", long: "Web 만들기", range: "1~3차시" },
    hw: { label: "하드웨어", long: "H/W 만들기", range: "4~6차시" },
    webhw: { label: "Web + 하드웨어", long: "Web + H/W", range: "7~9차시" }
  };

  const PROGRESS_KEY = "modi-planet-curriculum-progress-v1";
  const USER_KEY = "modi-planet-lms-user-v1";
  const state = {
    catalogs: new Map(),
    levelId: null,
    modeFilter: "all",
    activeLesson: null,
    planLesson: null,
    slideIndex: 0,
    studioTab: "activity",
    teacherNoteOpen: false,
    lessonStartedAt: 0,
    timerId: null,
    chatMessages: [],
    files: {},
    blocklyXml: "",
    modiModules: [],
    streaming: false,
    abortController: null,
    progress: loadProgress(),
    userId: loadUserId()
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[character]);
  }

  function asList(value) {
    return Array.isArray(value) ? value : [];
  }

  function isQuizSlide(slide) {
    return ["quiz", "check", "exit"].includes(slide.type);
  }

  function renderBulletList(items, className) {
    const values = asList(items);
    if (!values.length) {
      return "";
    }
    return '<ul class="' + escapeHtml(className || "slide-body") + '">' + values.map((item) => (
      "<li>" + escapeHtml(item) + "</li>"
    )).join("") + "</ul>";
  }

  function renderRubricTable(rows) {
    const values = asList(rows);
    if (!values.length) {
      return "";
    }
    return [
      '<div class="rubric-wrap"><table class="rubric-table"><thead><tr><th>평가 기준</th><th>기초</th><th>도달</th><th>심화</th></tr></thead><tbody>',
      values.map((row) => [
        "<tr><th>", escapeHtml(row.criterion), "</th><td>", escapeHtml(row.basic), "</td><td>",
        escapeHtml(row.proficient), "</td><td>", escapeHtml(row.advanced), "</td></tr>"
      ].join("")).join(""),
      "</tbody></table></div>"
    ].join("");
  }

  function loadProgress() {
    try {
      const data = JSON.parse(window.localStorage.getItem(PROGRESS_KEY) || "{}");
      return data && typeof data === "object" ? data : {};
    } catch (_error) {
      return {};
    }
  }

  function loadUserId() {
    const stored = window.localStorage.getItem(USER_KEY);
    if (stored) {
      return stored;
    }
    const id = "u-" + Math.random().toString(36).slice(2, 12);
    window.localStorage.setItem(USER_KEY, id);
    return id;
  }

  function saveProgress() {
    try {
      window.localStorage.setItem(PROGRESS_KEY, JSON.stringify(state.progress));
    } catch (_error) {
      // Progress still works for this page view if local storage is unavailable.
    }
  }

  function lessonKey(levelId, lessonNo) {
    return levelId + "-" + String(lessonNo).padStart(2, "0");
  }

  function getLevelMeta(id) {
    return LEVELS.find((level) => level.id === id) || LEVELS[0];
  }

  function getCatalog(id) {
    return state.catalogs.get(id);
  }

  function getActiveCatalog() {
    return state.levelId ? getCatalog(state.levelId) : null;
  }

  function totalMinutes(lesson) {
    return asList(lesson.slides).reduce((sum, slide) => sum + Number(slide.minutes || 0), 0);
  }

  function showToast(message) {
    toastElement.textContent = message;
    toastElement.classList.add("show");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toastElement.classList.remove("show"), 2400);
  }

  async function fetchCatalog(levelId) {
    const response = await fetch("/api/v3/curriculum/" + encodeURIComponent(levelId), {
      headers: { Accept: "application/json" }
    });
    if (!response.ok) {
      throw new Error(levelId + " 교육과정을 불러오지 못했습니다. (" + response.status + ")");
    }
    const catalog = await response.json();
    if (!catalog || !Array.isArray(catalog.lessons) || catalog.lessons.length !== 9) {
      throw new Error(levelId + " 교육과정 데이터가 올바르지 않습니다.");
    }
    state.catalogs.set(levelId, catalog);
    return catalog;
  }

  async function boot() {
    try {
      await Promise.all(LEVELS.map((level) => fetchCatalog(level.id)));
      route();
    } catch (error) {
      main.innerHTML = [
        '<section class="error-state">',
        "<h1>교육과정을 불러오지 못했어요</h1>",
        "<p>", escapeHtml(error.message), "</p>",
        '<button class="primary-button" type="button" data-action="retry">다시 시도</button>',
        "</section>"
      ].join("");
    }
  }

  function route() {
    const raw = window.location.hash.replace(/^#/, "");
    const requested = LEVELS.some((level) => level.id === raw) ? raw : null;
    if (requested) {
      state.levelId = requested;
      state.modeFilter = "all";
      renderCourse();
    } else {
      state.levelId = null;
      renderLevelIndex();
    }
    updateProductRail();
    window.scrollTo({ top: 0, behavior: "auto" });
    main.focus({ preventScroll: true });
  }

  function updateProductRail() {
    document.querySelectorAll("[data-rail-level]").forEach((link) => {
      const active = link.dataset.railLevel === state.levelId;
      link.classList.toggle("active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function renderLevelIndex() {
    const catalogs = LEVELS.map((level) => getCatalog(level.id));
    const totalLessons = catalogs.reduce((sum, catalog) => sum + catalog.lessons.length, 0);
    const totalTime = catalogs.reduce((sum, catalog) => sum + catalog.classMinutes * catalog.lessons.length, 0);
    const standards = new Set(catalogs.flatMap((catalog) => catalog.lessons.flatMap((lesson) => (
      asList(lesson.standards).map((standard) => standard.code)
    ))));

    main.innerHTML = [
      '<div class="page-container">',
      '<section class="catalog-hero" aria-labelledby="catalogTitle">',
      '<div class="hero-copy">',
      '<p class="hero-badge">2022 개정 교육과정 연계</p>',
      '<h1 id="catalogTitle">배우고, 만들고,<br><em>MODI로 움직여요.</em></h1>',
      '<p>초급부터 고급까지 학교급에 맞춘 27차시 프로젝트 수업입니다. Web, 하드웨어, Web+하드웨어를 난이도별로 차근차근 완성합니다.</p>',
      '<div class="hero-actions"><button class="hero-action" type="button" data-level="elementary">초급부터 시작 <span>→</span></button>',
      '<a class="hero-action light" href="https://modiplanet.com/learning-space" target="_blank" rel="noreferrer">공식 Learning Space</a></div>',
      "</div>",
      '<div class="hero-visual" aria-hidden="true"><div class="hero-course-stack">',
      catalogs.map((catalog, index) => [
        '<div class="hero-course-card"><img src="/static/assets/brand/',
        escapeHtml(LEVELS[index].thumbnail), '" alt=""><strong>',
        escapeHtml(LEVELS[index].difficulty), " · ", escapeHtml(catalog.label), " ", escapeHtml(catalog.subject),
        "</strong></div>"
      ].join("")).join(""),
      "</div></div>",
      "</section>",
      '<section class="stat-strip" aria-label="전체 교육과정 요약">',
      '<div class="stat-item"><strong>3단계</strong><span>초급 · 중급 · 고급</span></div>',
      '<div class="stat-item"><strong>', String(totalLessons), '차시</strong><span>학교급별 9차시</span></div>',
      '<div class="stat-item"><strong>3가지</strong><span>Web · H/W · 융합</span></div>',
      '<div class="stat-item"><strong>', String(totalTime), '분</strong><span>전체 수업 시간</span></div>',
      "</section>",
      '<section class="level-section" aria-labelledby="levelTitle">',
      '<header class="section-heading"><div><p class="section-kicker">Curriculum</p><h2 id="levelTitle">난이도를 선택하세요</h2></div>',
      '<p>각 단계는 Web 3차시, 하드웨어 3차시, Web+하드웨어 프로젝트 3차시로 구성됩니다.</p></header>',
      '<div class="level-grid">',
      catalogs.map((catalog, index) => renderLevelCard(catalog, LEVELS[index])).join(""),
      "</div>",
      '<p class="sr-only">연계 성취기준 ', String(standards.size), "개</p>",
      "</section></div>"
    ].join("");

    document.title = "교육과정 · MODI Planet";
  }

  function renderLevelCard(catalog, levelMeta) {
    const completed = catalog.lessons.filter((lesson) => state.progress[lessonKey(catalog.level, lesson.no)]).length;
    return [
      '<button class="level-card" type="button" data-level="', escapeHtml(catalog.level), '">',
      '<div class="level-thumb"><span class="difficulty">', escapeHtml(levelMeta.difficulty), '</span><img src="/static/assets/brand/',
      escapeHtml(levelMeta.thumbnail), '" alt=""></div>',
      '<div class="level-content"><div class="level-meta"><span>', escapeHtml(catalog.grade), "</span><span>",
      escapeHtml(catalog.subject), "</span><span>", String(catalog.classMinutes), "분</span></div>",
      "<h3>", escapeHtml(catalog.label), " 교육과정 · 9차시</h3>",
      "<p>", escapeHtml(catalog.overview), "</p>",
      '<div class="level-footer"><span>', completed ? "완료 " + completed + "/9차시" : "9차시 전체 보기", "</span><b>→</b></div>",
      "</div></button>"
    ].join("");
  }

  function renderCourse() {
    const catalog = getActiveCatalog();
    const levelMeta = getLevelMeta(catalog.level);
    const groups = ["web", "hw", "webhw"].map((mode) => ({
      mode,
      lessons: catalog.lessons.filter((lesson) => lesson.projectType === mode)
    })).filter((group) => state.modeFilter === "all" || group.mode === state.modeFilter);

    main.innerHTML = [
      '<div class="catalog-layout">',
      '<section class="course-main">',
      renderCompactLevelNav(catalog.level),
      '<nav class="breadcrumb" aria-label="현재 위치"><button type="button" data-action="all-levels">교육과정</button><span>›</span><span>',
      escapeHtml(levelMeta.difficulty), " · ", escapeHtml(catalog.label), "</span></nav>",
      '<header class="course-header"><div><p class="course-kicker">', escapeHtml(levelMeta.difficulty), " Curriculum</p>",
      "<h1>", escapeHtml(catalog.label), " ", escapeHtml(catalog.subject), '<br><span>9차시 프로젝트 수업</span></h1>',
      '<p class="course-overview">', escapeHtml(catalog.overview), "</p>",
      '<div class="course-badges"><span>', escapeHtml(catalog.grade), "</span><span>차시당 ", String(catalog.classMinutes),
      "분</span><span>Web 3</span><span>하드웨어 3</span><span>융합 3</span></div></div></header>",
      '<aside class="curriculum-note"><strong>교과 연계 안내</strong><span>', escapeHtml(catalog.curriculumNote),
      catalog.standardsSource && catalog.standardsSource.url ? ' <a href="' + escapeHtml(catalog.standardsSource.url) + '" target="_blank" rel="noreferrer">교육부 고시 원문 보기</a>' : "",
      "</span></aside>",
      renderModeTabs(),
      '<div class="lesson-groups">', groups.map((group) => renderLessonGroup(group, catalog)).join(""), "</div>",
      "</section></div>"
    ].join("");

    document.title = catalog.label + " 교육과정 · MODI Planet";
  }

  function renderCompactLevelNav(activeLevelId) {
    return [
      '<nav class="compact-level-nav" aria-label="난이도 선택">',
      LEVELS.map((meta) => {
        const catalog = getCatalog(meta.id);
        const active = meta.id === activeLevelId;
        return [
          '<a class="', active ? "active" : "", '" href="/lms#', escapeHtml(meta.id), '"',
          active ? ' aria-current="page"' : "", ">", escapeHtml(meta.difficulty), " · ", escapeHtml(catalog.label), "</a>"
        ].join("");
      }).join(""),
      "</nav>"
    ].join("");
  }

  function renderModeTabs() {
    const tabs = [
      { id: "all", label: "전체 9차시" },
      { id: "web", label: "Web · 1~3차시" },
      { id: "hw", label: "하드웨어 · 4~6차시" },
      { id: "webhw", label: "Web + 하드웨어 · 7~9차시" }
    ];
    return [
      '<div class="mode-tabs" role="tablist" aria-label="수업 유형 필터">',
      tabs.map((tab) => [
        '<button type="button" role="tab" data-mode-filter="', tab.id, '" class="', state.modeFilter === tab.id ? "active" : "",
        '" aria-selected="', state.modeFilter === tab.id ? "true" : "false", '">', tab.label, "</button>"
      ].join("")).join(""),
      "</div>"
    ].join("");
  }

  function renderLessonGroup(group, catalog) {
    const mode = MODES[group.mode];
    const description = group.mode === "web"
      ? "브라우저에서 실행되는 개별 작품을 차시마다 완성합니다."
      : group.mode === "hw"
        ? "MODI 센서와 출력 모듈을 연결해 개별 장치를 완성합니다."
        : catalog.finalGoal;
    return [
      '<section class="lesson-group" aria-labelledby="group-', group.mode, '"><header class="lesson-group-header"><h2 id="group-', group.mode,
      '">', escapeHtml(mode.range), " · ", escapeHtml(mode.long), "</h2><p>", escapeHtml(description), "</p></header>",
      '<div class="lesson-grid">', group.lessons.map((lesson) => renderLessonCard(lesson, catalog)).join(""), "</div></section>"
    ].join("");
  }

  function renderLessonCard(lesson, catalog) {
    const mode = MODES[lesson.projectType] || MODES.web;
    const completed = Boolean(state.progress[lessonKey(catalog.level, lesson.no)]);
    return [
      '<article class="lesson-card"><div class="lesson-topline"><span class="lesson-number">',
      completed ? "✓" : String(lesson.no).padStart(2, "0"), '</span><span class="mode-chip ', escapeHtml(lesson.projectType), '">',
      escapeHtml(mode.label), "</span></div><h3>", escapeHtml(lesson.title), "</h3><p>", escapeHtml(lesson.summary), "</p>",
      '<div class="standard-list">', asList(lesson.standards).map((standard) => (
        '<span class="standard-chip">' + escapeHtml(standard.code) + "</span>"
      )).join(""), '<span class="deck-chip">', String(asList(lesson.slides).length), "페이지 · 교실 실행형</span></div>",
      '<div class="lesson-actions"><button class="open-plan" type="button" data-plan-lesson="', String(lesson.no),
      '">교안 보기</button><button class="start-lesson" type="button" data-start-lesson="', String(lesson.no),
      '">', completed ? "다시 수업" : "수업 시작", "</button></div></article>"
    ].join("");
  }

  function findLesson(number) {
    const catalog = getActiveCatalog();
    return catalog ? catalog.lessons.find((lesson) => Number(lesson.no) === Number(number)) : null;
  }

  function openPlan(lesson) {
    const catalog = getActiveCatalog();
    state.planLesson = lesson;
    planKicker.textContent = getLevelMeta(catalog.level).difficulty + " · " + catalog.label + " " + catalog.subject + " · " + totalMinutes(lesson) + "분";
    planTitle.textContent = lesson.no + "차시 · " + lesson.title;

    const assessments = [];
    asList(lesson.slides).forEach((slide) => {
      if (isQuizSlide(slide)) {
        assessments.push({
          title: slide.type === "exit" ? "마무리 평가" : "형성평가",
          text: slide.question + " · 정답: " + asList(slide.choices)[slide.answer] + " · " + slide.explanation
        });
      } else if (slide.type === "checkpoint") {
        asList(slide.criteria).forEach((criterion) => assessments.push({ title: "성공 기준", text: criterion }));
      } else if (slide.type === "build" && slide.checkpoint) {
        assessments.push({ title: "제작 " + slide.stepNumber + "단계", text: slide.checkpoint });
      }
    });

    planBody.innerHTML = [
      '<p class="plan-summary">', escapeHtml(lesson.summary), "</p>",
      '<div class="plan-columns">',
      '<section class="plan-section"><h3>학습 목표</h3><ul>', asList(lesson.objectives).map((objective) => "<li>" + escapeHtml(objective) + "</li>").join(""), "</ul></section>",
      '<section class="plan-section"><h3>성공 기준</h3><ul>', asList(lesson.successCriteria).map((criterion) => "<li>" + escapeHtml(criterion) + "</li>").join(""), "</ul></section>",
      '<section class="plan-section"><h3>준비물</h3><ul>', asList(lesson.materials).map((material) => "<li>" + escapeHtml(material) + "</li>").join(""), "</ul></section>",
      '<section class="plan-section"><h3>학생 산출물</h3><ul>', asList(lesson.studentArtifacts).map((artifact) => "<li>" + escapeHtml(artifact) + "</li>").join(""), "</ul></section>",
      '<section class="plan-section full"><h3>핵심 어휘와 수업 예시</h3><div class="vocabulary-list compact">', asList(lesson.vocabulary).map((term) => [
        '<article class="vocabulary-item"><strong>', escapeHtml(term.term), "</strong><span>", escapeHtml(term.meaning),
        '</span><small>예: ', escapeHtml(term.example), "</small></article>"
      ].join("")).join(""), "</div></section>",
      '<section class="plan-section full"><h3>연계 성취기준</h3><div class="standard-blocks">', asList(lesson.standards).map((standard) => [
        '<div class="standard-block"><strong>', escapeHtml(standard.code), "</strong>", escapeHtml(standard.text), "</div>"
      ].join("")).join(""), "</div></section>",
      '<section class="plan-section full"><h3>차시별 수업 흐름 · 총 ', String(totalMinutes(lesson)), "분 · ", String(asList(lesson.slides).length), '페이지</h3><div class="lesson-timeline">',
      asList(lesson.slides).map((slide) => [
        '<div class="timeline-row"><span class="phase">', escapeHtml(slide.phase), "</span><strong>", escapeHtml(slide.title),
        '</strong><span class="minutes">', String(slide.minutes || 0), "분</span></div>"
      ].join("")).join(""), "</div></section>",
      '<section class="plan-section full"><h3>수준별 운영</h3><div class="differentiate-grid compact"><article><b>도움이 필요할 때</b>',
      renderBulletList(lesson.differentiation && lesson.differentiation.support, "mini-list"),
      '</article><article><b>더 도전할 때</b>', renderBulletList(lesson.differentiation && lesson.differentiation.challenge, "mini-list"), "</article></div></section>",
      '<section class="plan-section full"><h3>평가 루브릭</h3>', renderRubricTable(lesson.rubric), "</section>",
      '<section class="plan-section full"><h3>평가와 체크포인트</h3><div class="assessment-list">',
      assessments.map((assessment) => '<div class="assessment-item"><b>' + escapeHtml(assessment.title) + "</b>" + escapeHtml(assessment.text) + "</div>").join(""),
      "</div></section></div>"
    ].join("");

    planDialog.showModal();
  }

  function closePlan() {
    if (planDialog.open) {
      planDialog.close();
    }
  }

  function startLesson(lesson) {
    const catalog = getActiveCatalog();
    if (!catalog || !lesson) {
      return;
    }
    closePlan();
    state.activeLesson = lesson;
    state.slideIndex = 0;
    state.studioTab = "activity";
    state.teacherNoteOpen = false;
    state.lessonStartedAt = Date.now();
    state.chatMessages = [{ type: "assistant", text: "수업 활동의 예시 문장을 눌러 시작하거나, 만들고 싶은 내용을 직접 설명해 보세요." }];
    state.files = {};
    state.blocklyXml = "";
    state.modiModules = [];
    stopChat();
    document.body.classList.add("player-open");
    lessonPlayer.classList.add("open");
    lessonPlayer.setAttribute("aria-hidden", "false");
    document.getElementById("playerMeta").textContent = getLevelMeta(catalog.level).difficulty + " · " + catalog.label + " " + catalog.subject + " · " + catalog.classMinutes + "분";
    document.getElementById("playerTitle").textContent = lesson.no + "차시 · " + lesson.title;
    document.getElementById("teacherToggle").classList.remove("active");
    document.getElementById("teacherToggle").setAttribute("aria-pressed", "false");
    renderSlideRail();
    renderSlide();
    window.clearInterval(state.timerId);
    state.timerId = window.setInterval(updateTimer, 1000);
    updateTimer();
    document.getElementById("exitLessonButton").focus();
  }

  function stopChat() {
    if (state.abortController) {
      state.abortController.abort();
      state.abortController = null;
    }
    state.streaming = false;
  }

  function exitLesson(completed) {
    if (!state.activeLesson) {
      return;
    }
    const catalog = getActiveCatalog();
    if (completed) {
      state.progress[lessonKey(catalog.level, state.activeLesson.no)] = {
        completedAt: new Date().toISOString(),
        title: state.activeLesson.title
      };
      saveProgress();
    }
    stopChat();
    window.clearInterval(state.timerId);
    state.timerId = null;
    lessonPlayer.classList.remove("open");
    lessonPlayer.setAttribute("aria-hidden", "true");
    document.body.classList.remove("player-open");
    state.activeLesson = null;
    renderCourse();
    showToast(completed ? "차시를 완료했어요. 진도가 저장되었습니다." : "수업 화면을 닫았습니다.");
  }

  function updateTimer() {
    const catalog = getActiveCatalog();
    if (!catalog || !state.activeLesson) {
      return;
    }
    const elapsed = Math.max(0, Math.floor((Date.now() - state.lessonStartedAt) / 1000));
    const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const seconds = String(elapsed % 60).padStart(2, "0");
    const timer = document.getElementById("classTimer");
    timer.textContent = minutes + ":" + seconds + " · " + catalog.classMinutes + "분";
    timer.classList.toggle("warning", elapsed >= Math.max(0, catalog.classMinutes - 5) * 60);
  }

  function renderSlideRail() {
    const lesson = state.activeLesson;
    document.getElementById("slideRail").innerHTML = [
      '<p class="slide-rail-summary">', escapeHtml(MODES[lesson.projectType].label), " · ", String(totalMinutes(lesson)), "분 · ", String(lesson.slides.length), "단계</p>",
      asList(lesson.slides).map((slide, index) => [
        '<button type="button" class="slide-step ', index === state.slideIndex ? "active" : "", " ", index < state.slideIndex ? "completed" : "",
        '" data-slide-index="', String(index), '"><span class="step-index">', index < state.slideIndex ? "✓" : String(index + 1),
        "</span><span><strong>", escapeHtml(slide.title), "</strong><small>", escapeHtml(slide.phase), " · ", String(slide.minutes || 0), "분</small></span></button>"
      ].join("")).join("")
    ].join("");
  }

  function renderQuizContent(slide) {
    const takeaways = asList(slide.takeaways);
    return [
      "<h2>", escapeHtml(slide.title), "</h2>",
      '<p class="quiz-question">Q. ', escapeHtml(slide.question), "</p>",
      '<div class="quiz-choices">', asList(slide.choices).map((choice, index) => [
        '<button type="button" class="quiz-choice" data-quiz-choice="', String(index), '"><b>', String(index + 1), "</b><span>", escapeHtml(choice), "</span></button>"
      ].join("")).join(""), '</div><div class="quiz-feedback" id="quizFeedback" aria-live="polite"></div>',
      takeaways.length ? '<div class="exit-takeaways"><strong>오늘 가져갈 것</strong>' + renderBulletList(takeaways, "mini-list") + "</div>" : ""
    ].join("");
  }

  function renderRichSlideContent(slide) {
    const typeLabels = {
      goals: "학습 목표",
      hook: "생각 열기",
      vocabulary: "핵심 어휘",
      concept: "개념 이해",
      example: "작동 예시",
      check: "형성평가",
      setup: "제작 준비",
      plan: "제작 계획",
      build: "따라 만들기",
      checkpoint: "작품 검증",
      troubleshoot: "오류 해결",
      differentiate: "수준별 활동",
      rubric: "평가 루브릭",
      exit: "마무리 평가",
      ai: "AI와 함께 만들기",
      activity: "학생 활동"
    };
    const badge = typeLabels[slide.type]
      ? '<div class="slide-type-badge">' + escapeHtml(typeLabels[slide.type]) + "</div>"
      : "";

    if (isQuizSlide(slide)) {
      return badge + renderQuizContent(slide);
    }
    if (slide.type === "goals") {
      return [
        badge, "<h2>", escapeHtml(slide.title), "</h2>",
        '<div class="goal-grid"><article><strong>할 수 있어요</strong>', renderBulletList(slide.objectives, "mini-list"),
        '</article><article><strong>이렇게 확인해요</strong>', renderBulletList(slide.successCriteria, "mini-list"), "</article></div>"
      ].join("");
    }
    if (slide.type === "vocabulary") {
      return [
        badge, "<h2>", escapeHtml(slide.title), "</h2>",
        '<div class="vocabulary-list">', asList(slide.terms).map((term) => [
          '<article class="vocabulary-item"><strong>', escapeHtml(term.term), "</strong><span>", escapeHtml(term.meaning),
          '</span><small>작품 예시 · ', escapeHtml(term.example), "</small></article>"
        ].join("")).join(""), "</div>"
      ].join("");
    }
    if (slide.type === "example") {
      const scenario = Array.isArray(slide.scenario) ? slide.scenario : [slide.scenario].filter(Boolean);
      const compare = slide.compare || {};
      const flow = [
        { label: "입력", values: slide.input },
        { label: "처리", values: slide.process },
        { label: "출력", values: slide.output }
      ].filter((item) => asList(item.values).length);
      return [
        badge, "<h2>", escapeHtml(slide.title), "</h2>",
        scenario.length ? '<div class="example-scenario"><b>상황</b>' + scenario.map((item) => "<span>" + escapeHtml(item) + "</span>").join("") + "</div>" : "",
        flow.length ? '<div class="example-flow">' + flow.map((item, index) => [
          '<article><b>', escapeHtml(item.label), "</b>", renderBulletList(item.values, "mini-list"), "</article>",
          index < flow.length - 1 ? '<span class="flow-arrow" aria-hidden="true">→</span>' : ""
        ].join("")).join("") + "</div>" : "",
        compare.good || compare.bad ? '<div class="compare-panel"><article class="good"><b>좋은 선택</b><span>' + escapeHtml(compare.good) +
          '</span></article><article class="bad"><b>피할 선택</b><span>' + escapeHtml(compare.bad) + "</span></article></div>" : "",
        asList(slide.body).length && slide.decisionQuestion ? '<div class="decision-criteria"><strong>판단 기준</strong>' + renderBulletList(slide.body, "mini-list") + "</div>" : "",
        slide.decisionQuestion ? '<div class="checkpoint-callout"><strong>선택 근거</strong><span>' + escapeHtml(slide.decisionQuestion) + "</span></div>" : ""
      ].join("");
    }
    if (slide.type === "setup") {
      return [badge, "<h2>", escapeHtml(slide.title), "</h2>", '<div class="setup-checklist">',
        asList(slide.checklist).map((item) => '<div><span>✓</span>' + escapeHtml(item) + "</div>").join(""), "</div>"].join("");
    }
    if (slide.type === "plan") {
      return [
        badge, "<h2>", escapeHtml(slide.title), "</h2>",
        '<div class="plan-step-list">', asList(slide.steps).map((step, index) => '<div><b>' + String(index + 1) + "</b><span>" + escapeHtml(step) + "</span></div>").join(""), "</div>",
        '<div class="artifact-callout"><strong>남길 결과물</strong>', renderBulletList(slide.studentArtifacts, "mini-list"), "</div>"
      ].join("");
    }
    if (slide.type === "build") {
      return [
        '<div class="build-step-header"><span>제작 ', String(slide.stepNumber), " / ", String(slide.stepTotal), "</span><b>",
        escapeHtml(slide.codingType === "blockly" ? "MODI 블록" : slide.codingType === "hybrid" ? "Web + MODI" : "Web"), "</b></div>",
        "<h2>", escapeHtml(slide.title), "</h2>",
        '<ol class="step-instructions">', asList(slide.instructions).map((item) => "<li>" + escapeHtml(item) + "</li>").join(""), "</ol>",
        slide.prompt ? '<div class="prompt-callout"><strong>AI에게 이렇게 요청해 보세요</strong><code>' + escapeHtml(slide.prompt) + "</code></div>" : "",
        '<div class="checkpoint-callout"><strong>통과 조건</strong><span>', escapeHtml(slide.checkpoint), "</span></div>"
      ].join("");
    }
    if (slide.type === "checkpoint") {
      return [
        badge, "<h2>", escapeHtml(slide.title), "</h2>",
        '<div class="checkpoint-grid"><article><strong>작동 확인</strong>', renderBulletList(slide.criteria, "mini-list"),
        '</article><article><strong>증거로 남기기</strong>', renderBulletList(slide.studentArtifacts, "mini-list"), "</article></div>",
        renderRubricTable(slide.rubric)
      ].join("");
    }
    if (slide.type === "troubleshoot") {
      return [
        badge, "<h2>", escapeHtml(slide.title), "</h2>",
        '<div class="issue-table"><div class="issue-head"><b>보이는 증상</b><b>가능한 원인</b><b>확인·수정</b></div>',
        asList(slide.issues).map((issue) => '<article><span>' + escapeHtml(issue.symptom) + "</span><span>" + escapeHtml(issue.cause) + "</span><strong>" + escapeHtml(issue.fix) + "</strong></article>").join(""),
        "</div>"
      ].join("");
    }
    if (slide.type === "differentiate") {
      return [
        badge, "<h2>", escapeHtml(slide.title), "</h2>",
        '<div class="differentiate-grid"><article><b>도움이 필요하면</b>', renderBulletList(slide.support, "mini-list"),
        '</article><article><b>먼저 완성했다면</b>', renderBulletList(slide.challenge, "mini-list"), "</article></div>"
      ].join("");
    }
    if (slide.type === "rubric") {
      return [
        badge, "<h2>", escapeHtml(slide.title), "</h2>", renderRubricTable(slide.rows),
        '<div class="artifact-callout"><strong>제출 증거</strong>', renderBulletList(slide.studentArtifacts, "mini-list"), "</div>"
      ].join("");
    }

    return [badge, "<h2>", escapeHtml(slide.title), "</h2>", renderBulletList(slide.body, "slide-body")].join("");
  }

  function renderSlide() {
    const lesson = state.activeLesson;
    const slide = lesson.slides[state.slideIndex];
    const slideCard = document.getElementById("slideCard");
    slideCard.className = "slide-card type-" + escapeHtml(slide.type) + (slide.type === "title" ? " title-slide" : "") + (["ai", "build"].includes(slide.type) ? " ai-slide" : "") + (["activity", "checkpoint"].includes(slide.type) ? " activity-slide" : "");

    const phaseLine = '<div class="slide-phase">' + escapeHtml(slide.phase) + '<span>' + String(slide.minutes || 0) + "분</span></div>";
    let content = phaseLine;
    if (slide.type === "title") {
      content += "<h2>" + escapeHtml(slide.title) + "</h2><p class=\"slide-subtitle\">" + escapeHtml(slide.subtitle || "") + "</p>";
    } else {
      content += renderRichSlideContent(slide);
    }
    slideCard.innerHTML = content;

    const note = document.getElementById("teacherNote");
    note.innerHTML = '<strong>교사 노트</strong>' + escapeHtml(slide.teacherNote || "이 단계에는 별도 교사 노트가 없습니다.");
    note.classList.toggle("open", state.teacherNoteOpen);

    const phases = ["도입", "전개", "정리"];
    document.getElementById("phaseTrack").innerHTML = phases.map((phase) => (
      '<span class="' + (phase === slide.phase ? "active" : "") + '">' + phase + "</span>"
    )).join("");

    document.getElementById("previousSlideButton").disabled = state.slideIndex === 0;
    document.getElementById("nextSlideButton").textContent = state.slideIndex === lesson.slides.length - 1 ? "수업 완료" : "다음";
    document.getElementById("slideCounter").textContent = String(state.slideIndex + 1) + " / " + String(lesson.slides.length);
    document.querySelector("#slideProgress span").style.width = String(((state.slideIndex + 1) / lesson.slides.length) * 100) + "%";
    renderSlideRail();
    renderStudio();
  }

  function moveSlide(direction) {
    if (!state.activeLesson) {
      return;
    }
    const next = state.slideIndex + direction;
    if (next < 0) {
      return;
    }
    if (next >= state.activeLesson.slides.length) {
      exitLesson(true);
      return;
    }
    state.slideIndex = next;
    renderSlide();
  }

  function chooseQuiz(index) {
    const slide = state.activeLesson.slides[state.slideIndex];
    if (!isQuizSlide(slide)) {
      return;
    }
    document.querySelectorAll("[data-quiz-choice]").forEach((button) => {
      const choice = Number(button.dataset.quizChoice);
      button.classList.toggle("correct", choice === Number(slide.answer));
      button.classList.toggle("wrong", choice === Number(index) && choice !== Number(slide.answer));
    });
    const correct = Number(index) === Number(slide.answer);
    document.getElementById("quizFeedback").textContent = (correct ? "정답입니다. " : "정답을 함께 표시했습니다. ") + String(slide.explanation || "핵심 개념을 다시 확인해 보세요.");
  }

  function renderStudio() {
    const slide = state.activeLesson.slides[state.slideIndex];
    document.querySelectorAll("[data-studio-tab]").forEach((button) => {
      const active = button.dataset.studioTab === state.studioTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const studio = document.getElementById("studioBody");
    if (state.studioTab === "preview") {
      studio.innerHTML = renderPreview();
      return;
    }
    if (state.studioTab === "modi") {
      studio.innerHTML = renderModiPanel();
      return;
    }

    const prompts = asList(slide.prompts);
    const body = asList(slide.instructions).length ? asList(slide.instructions) : asList(slide.body);
    const activityTitle = slide.type === "build"
      ? "제작 " + slide.stepNumber + "/" + slide.stepTotal + " · 통과 조건을 확인하세요"
      : slide.type === "ai" ? "AI 제작 활동" : "현재 수업 활동";
    studio.innerHTML = [
      '<div class="activity-panel"><h3>', escapeHtml(activityTitle), "</h3>",
      "<p>", escapeHtml(slide.title), "</p>",
      body.length ? '<div class="activity-checklist">' + body.map((line, index) => [
        '<label><input type="checkbox" data-checkpoint="', String(index), '"><span>', escapeHtml(line), "</span></label>"
      ].join("")).join("") + "</div>" : "",
      prompts.length ? '<div class="prompt-list">' + prompts.map((prompt) => (
        '<button class="prompt-button" type="button" data-prompt="' + escapeHtml(prompt) + '">' + escapeHtml(prompt) + "</button>"
      )).join("") + "</div>" : "",
      !body.length && !prompts.length ? '<div class="studio-empty"><div><span class="empty-icon">✓</span><strong>수업 화면에 집중하세요</strong><span>제작 단계에서 체크리스트와 AI 예시가 나타납니다.</span></div></div>' : "",
      slide.checkpoint ? '<div class="studio-checkpoint"><b>통과 조건</b>' + escapeHtml(slide.checkpoint) + "</div>" : "",
      renderChatThread(),
      "</div>"
    ].join("");
  }

  function renderChatThread() {
    return '<div class="chat-thread" id="chatThread">' + state.chatMessages.map((message) => (
      '<div class="chat-message ' + escapeHtml(message.type) + '">' + escapeHtml(message.text) + "</div>"
    )).join("") + "</div>";
  }

  function renderPreview() {
    const names = Object.keys(state.files);
    if (!names.length) {
      return '<div class="studio-empty"><div><span class="empty-icon">Web</span><strong>아직 만든 작품이 없어요</strong><span>활동 탭의 예시 문장으로 AI와 제작을 시작하면 결과가 여기에 나타납니다.</span></div></div>';
    }
    const htmlName = names.find((name) => name.toLowerCase().endsWith(".html"));
    if (htmlName) {
      const safeSource = escapeHtml(state.files[htmlName]);
      return '<div class="preview-window"><div class="preview-bar"><i></i><i></i><i></i></div><iframe sandbox="allow-scripts" srcdoc="' + safeSource + '"></iframe></div>';
    }
    const firstName = names[0];
    return "<pre class=\"code-output\">" + escapeHtml(firstName + "\n\n" + state.files[firstName]) + "</pre>";
  }

  function renderModiPanel() {
    const lesson = state.activeLesson;
    if (lesson.projectType === "web" && !state.blocklyXml && !state.modiModules.length) {
      return '<div class="studio-empty"><div><span class="empty-icon">Web</span><strong>Web 중심 차시입니다</strong><span>4차시부터 MODI 하드웨어 활동이 시작됩니다.</span></div></div>';
    }
    const modules = state.modiModules.length ? state.modiModules.map((module) => (
      typeof module === "string" ? module : module.type || module.name || JSON.stringify(module)
    )) : asList(lesson.materials);
    return [
      '<div class="modi-panel"><h3>MODI 하드웨어 준비</h3><p>이 차시 교안에 지정된 준비물과 생성된 블록을 확인합니다.</p>',
      '<div class="material-chips">', modules.map((module) => "<span>" + escapeHtml(module) + "</span>").join(""), "</div>",
      state.blocklyXml ? '<pre class="code-output">' + escapeHtml(state.blocklyXml) + "</pre>" : "",
      "</div>"
    ].join("");
  }

  function setStudioTab(tab) {
    state.studioTab = tab;
    renderStudio();
  }

  function usePrompt(prompt) {
    const input = document.getElementById("tutorInput");
    input.value = prompt;
    input.focus();
    input.setSelectionRange(prompt.length, prompt.length);
    showToast("예시 문장을 입력했어요. 보내기 전에 원하는 내용을 더 붙여도 됩니다.");
  }

  function updateChatMessage(type, text, append) {
    if (append && state.chatMessages.length && state.chatMessages[state.chatMessages.length - 1].type === type) {
      state.chatMessages[state.chatMessages.length - 1].text += text;
    } else {
      state.chatMessages.push({ type, text });
    }
    if (state.studioTab === "activity") {
      renderStudio();
      const thread = document.getElementById("chatThread");
      if (thread) {
        thread.scrollIntoView({ block: "end" });
      }
    }
  }

  async function sendTutorMessage(message) {
    if (!message || state.streaming || !state.activeLesson) {
      return;
    }
    const catalog = getActiveCatalog();
    const slide = state.activeLesson.slides[state.slideIndex];
    const codingType = slide.codingType || (state.activeLesson.projectType === "hw" ? "blockly" : state.activeLesson.projectType === "webhw" ? "hybrid" : "react");
    state.studioTab = "activity";
    updateChatMessage("user", message, false);
    updateChatMessage("status", "AI가 수업 활동을 확인하고 있어요…", false);
    state.streaming = true;
    state.abortController = new AbortController();

    try {
      const response = await fetch("/chat?user_id=" + encodeURIComponent(state.userId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: state.abortController.signal,
        body: JSON.stringify({
          session_id: "lms-" + catalog.level + "-" + state.activeLesson.no + "-" + state.userId.slice(2, 8),
          message,
          mode: "design",
          coding_type: codingType
        })
      });
      if (!response.ok || !response.body) {
        throw new Error("AI 서버 응답 오류 (" + response.status + ")");
      }
      state.chatMessages = state.chatMessages.filter((item) => item.type !== "status");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const result = await reader.read();
        if (result.done) {
          break;
        }
        buffer += decoder.decode(result.value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        lines.forEach((line) => {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data:")) {
            return;
          }
          try {
            handleTutorEvent(JSON.parse(trimmed.slice(5)));
          } catch (_error) {
            // Ignore malformed stream fragments and keep reading.
          }
        });
      }
    } catch (error) {
      state.chatMessages = state.chatMessages.filter((item) => item.type !== "status");
      if (error.name !== "AbortError") {
        updateChatMessage("assistant", "AI 제작 연결을 확인해 주세요. 교안과 수업 진행 기능은 그대로 사용할 수 있습니다.\n" + error.message, false);
      }
    } finally {
      state.streaming = false;
      state.abortController = null;
    }
  }

  function handleTutorEvent(event) {
    if (!event || typeof event !== "object") {
      return;
    }
    if (event.type === "token") {
      updateChatMessage("assistant", String(event.text || ""), true);
    } else if (event.type === "status") {
      state.chatMessages = state.chatMessages.filter((item) => item.type !== "status");
      updateChatMessage("status", String(event.message || "작업 중…"), false);
    } else if (event.type === "code_validated" && event.generated_code) {
      state.files = event.generated_code;
    } else if (event.type === "blockly_ready") {
      state.blocklyXml = String(event.blockly_xml || "");
      state.modiModules = asList(event.modi_modules);
    } else if (event.type === "done") {
      state.chatMessages = state.chatMessages.filter((item) => item.type !== "status");
      if (event.generated_code) {
        state.files = event.generated_code;
      }
      if (event.blockly_xml) {
        state.blocklyXml = String(event.blockly_xml);
      }
      if (event.modi_modules) {
        state.modiModules = asList(event.modi_modules);
      }
      if (!state.chatMessages.some((item) => item.type === "assistant")) {
        updateChatMessage("assistant", String(event.message || "작품을 만들었어요. 미리보기와 MODI 탭에서 확인하세요."), false);
      }
      renderStudio();
    } else if (event.type === "error") {
      state.chatMessages = state.chatMessages.filter((item) => item.type !== "status");
      updateChatMessage("assistant", String(event.user_message || event.message || "AI 제작 중 오류가 발생했습니다."), false);
    }
  }

  document.addEventListener("click", (event) => {
    const levelButton = event.target.closest("[data-level]");
    if (levelButton) {
      window.location.hash = "#" + levelButton.dataset.level;
      return;
    }

    const allLevelsButton = event.target.closest("[data-action='all-levels']");
    if (allLevelsButton) {
      window.location.hash = "#levels";
      return;
    }

    const retryButton = event.target.closest("[data-action='retry']");
    if (retryButton) {
      main.innerHTML = '<div class="app-loading" role="status"><span class="loader"></span><strong>다시 불러오고 있어요</strong></div>';
      boot();
      return;
    }

    const filterButton = event.target.closest("[data-mode-filter]");
    if (filterButton) {
      state.modeFilter = filterButton.dataset.modeFilter;
      renderCourse();
      return;
    }

    const planButton = event.target.closest("[data-plan-lesson]");
    if (planButton) {
      openPlan(findLesson(planButton.dataset.planLesson));
      return;
    }

    const startButton = event.target.closest("[data-start-lesson]");
    if (startButton) {
      startLesson(findLesson(startButton.dataset.startLesson));
      return;
    }

    const slideButton = event.target.closest("[data-slide-index]");
    if (slideButton && state.activeLesson) {
      state.slideIndex = Number(slideButton.dataset.slideIndex);
      renderSlide();
      return;
    }

    const quizButton = event.target.closest("[data-quiz-choice]");
    if (quizButton) {
      chooseQuiz(Number(quizButton.dataset.quizChoice));
      return;
    }

    const studioTabButton = event.target.closest("[data-studio-tab]");
    if (studioTabButton) {
      setStudioTab(studioTabButton.dataset.studioTab);
      return;
    }

    const promptButton = event.target.closest("[data-prompt]");
    if (promptButton) {
      usePrompt(promptButton.dataset.prompt);
    }
  });

  document.getElementById("closePlanButton").addEventListener("click", closePlan);
  document.getElementById("closePlanFooterButton").addEventListener("click", closePlan);
  document.getElementById("startFromPlanButton").addEventListener("click", () => startLesson(state.planLesson));
  document.getElementById("exitLessonButton").addEventListener("click", () => exitLesson(false));
  document.getElementById("previousSlideButton").addEventListener("click", () => moveSlide(-1));
  document.getElementById("nextSlideButton").addEventListener("click", () => moveSlide(1));
  document.getElementById("teacherToggle").addEventListener("click", () => {
    state.teacherNoteOpen = !state.teacherNoteOpen;
    document.getElementById("teacherToggle").classList.toggle("active", state.teacherNoteOpen);
    document.getElementById("teacherToggle").setAttribute("aria-pressed", state.teacherNoteOpen ? "true" : "false");
    document.getElementById("teacherNote").classList.toggle("open", state.teacherNoteOpen);
  });

  document.getElementById("tutorForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.getElementById("tutorInput");
    const message = input.value.trim();
    if (!message) {
      return;
    }
    input.value = "";
    sendTutorMessage(message);
  });

  document.getElementById("tutorInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      document.getElementById("tutorForm").requestSubmit();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (!state.activeLesson || event.target.matches("textarea, input, button")) {
      return;
    }
    if (event.key === "ArrowRight" || event.key === " ") {
      event.preventDefault();
      moveSlide(1);
    } else if (event.key === "ArrowLeft") {
      moveSlide(-1);
    } else if (event.key.toLowerCase() === "t") {
      document.getElementById("teacherToggle").click();
    } else if (event.key === "Escape") {
      exitLesson(false);
    }
  });

  planDialog.addEventListener("click", (event) => {
    if (event.target === planDialog) {
      closePlan();
    }
  });

  window.addEventListener("hashchange", route);
  boot();
})();
