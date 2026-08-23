(() => {
  "use strict";

  if ("scrollRestoration" in window.history) {
    window.history.scrollRestoration = "manual";
  }

  const app = document.getElementById("app");
  const globalLive = document.getElementById("globalLive");
  const RECENT_KEY = "modi-planet-v3-recent-projects";
  const MAX_RECENT = 6;

  const ICONS = {
    arrow: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
    back: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>',
    check: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m6 12 4 4 8-9"/></svg>',
    send: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m4 4 17 8-17 8 3-8z"/><path d="M7 12h14"/></svg>',
    learn: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m3 7 9-4 9 4-9 4z"/><path d="M6 10v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5"/><path d="M21 8v6"/></svg>',
    create: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><path d="m5.6 5.6 2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/><circle cx="12" cy="12" r="3"/></svg>',
    web: '<svg aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 8h18M7 6h.01M10 6h.01"/><path d="m9 13-2 2 2 2M15 13l2 2-2 2"/></svg>',
    modi: '<svg aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><path d="M17.5 14v7M14 17.5h7"/></svg>',
    hybrid: '<svg aria-hidden="true" viewBox="0 0 24 24"><rect x="2.5" y="4" width="11" height="9" rx="2"/><path d="M6 17h4M8 13v4"/><rect x="15.5" y="11" width="6" height="6" rx="1.5"/><path d="M13.5 8h2M18.5 8v3"/></svg>',
    elementary: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M5 5.5A2.5 2.5 0 0 1 7.5 3H12v17H7.5A2.5 2.5 0 0 0 5 22z"/><path d="M19 5.5A2.5 2.5 0 0 0 16.5 3H12v17h4.5A2.5 2.5 0 0 1 19 22z"/></svg>',
    middle: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m3 7 9-4 9 4-9 4z"/><path d="M6 10v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5"/></svg>',
    high: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 20h16M6 20V8l6-5 6 5v12"/><path d="M9 12h6M9 16h6"/></svg>'
  };

  const PROJECT_TYPES = [
    {
      id: "react",
      label: "Web",
      code: "react",
      description: "화면에서 바로 실행되는 인터랙티브 웹 작품",
      icon: ICONS.web
    },
    {
      id: "blockly",
      label: "MODI",
      code: "blockly",
      description: "블록을 연결해 센서와 모듈을 움직이는 작품",
      icon: ICONS.modi
    },
    {
      id: "hybrid",
      label: "Web + MODI",
      code: "hybrid",
      description: "웹 화면과 MODI 하드웨어가 함께 반응하는 작품",
      icon: ICONS.hybrid
    }
  ];

  const EXAMPLE_CATEGORIES = ["전체", "학습", "게임", "인터랙티브", "예술", "서비스", "로봇"];

  // Official AI MODI Planet showcase catalog, verified from ai.modiplanet.com.
  const EXAMPLE_PROJECTS = [
    { ref: "rhythm-game", title: "리듬 게임", description: "버튼·조이스틱·다이얼로 노트를 맞추는 DDR 스타일 MODI 리듬 액션 게임", category: "게임", type: "hybrid" },
    { ref: "modi-math-dashboard", title: "MODI 수학 대시보드", description: "MODI 센서(조이스틱·거리·IMU)로 삼각함수·미분·기울기를 배우는 수학 대시보드", category: "학습", type: "hybrid" },
    { ref: "cell-biology", title: "세포생물학 탐험", description: "체세포분열·감수분열, DNA 이중나선과 복제, 염기쌍을 직접 조작하며 배우는 생명과학", category: "학습", type: "react" },
    { ref: "earth-science", title: "지구과학 교실", description: "지구본, 대기권, 자기장·태양풍을 움직이며 배우는 지구과학 시뮬레이터", category: "학습", type: "react" },
    { ref: "color-school", title: "Color School", description: "색의 3요소·색상환·배색·빛과 물감의 혼합·코드 변환을 배우는 색채 학습 사이트", category: "학습", type: "react" },
    { ref: "constellation", title: "별자리 만들기", description: "마우스로 밤하늘의 별을 연결하며 나만의 별자리를 만드는 인터랙티브 파티클 애니메이션", category: "인터랙티브", type: "react" },
    { ref: "webcam-magic-filter", title: "웹캠 매직 필터", description: "웹캠 영상에 실시간 필터를 입히고 사진·영상으로 저장하는 웹앱", category: "인터랙티브", type: "react" },
    { ref: "2048", title: "2048", description: "같은 숫자 타일을 합쳐 2048을 만드는 퍼즐 게임 (방향키·스와이프)", category: "게임", type: "react" },
    { ref: "minesweeper", title: "지뢰찾기", description: "숫자 힌트로 지뢰를 피해 모든 칸을 여는 클래식 퍼즐 게임 (난이도 3종)", category: "게임", type: "react" },
    { ref: "obstacle-avoiding-joystick-car", title: "장애물 회피 조이스틱 자동차", description: "조이스틱으로 조종하고 장애물을 만나면 자동으로 피하는 MODI 자동차", category: "로봇", type: "blockly" },
    { ref: "bubble-bobble", title: "보글보글", description: "MODI 조이스틱·버튼으로 조작하는 보글보글 스타일 플랫폼 슈팅 게임", category: "게임", type: "hybrid" },
    { ref: "space-shooter", title: "우주 슈팅게임", description: "MODI IMU를 기울여 우주선을 사방으로 조종하고 버튼으로 운석을 쏘는 슈팅 게임", category: "게임", type: "hybrid" },
    { ref: "modi-science-lab", title: "모디 과학 탐구 실험실", description: "MODI 센서로 빛·소리·온도·거리·기울기를 배우는 과학 탐구 실험실", category: "학습", type: "hybrid" },
    { ref: "smart-dice", title: "스마트 주사위", description: "모듈을 흔들었다 멈추면 1~6 숫자가 디스플레이에 표시되는 MODI 주사위", category: "로봇", type: "blockly" },
    { ref: "instagram-clone", title: "Instagram 클론", description: "스토리·피드·릴스·프로필 탭으로 사진을 공유하는 인스타그램 스타일 소셜 앱", category: "서비스", type: "react" },
    { ref: "daangn-market", title: "당근마켓 스타일 동네거래 앱", description: "동네 기반 중고거래 피드·상품 상세·채팅·동네생활·프로필을 갖춘 모바일 마켓 앱", category: "서비스", type: "react" },
    { ref: "modi-synth", title: "MODI 신디사이저", description: "MODI 스피커·거리센서·다이얼로 건반을 연주하는 웹 신디사이저", category: "예술", type: "hybrid" },
    { ref: "modi-paint", title: "모디 그림판", description: "MODI 조이스틱·다이얼·버튼으로 캔버스에 그림을 그리는 인터랙티브 드로잉 앱", category: "예술", type: "hybrid" },
    { ref: "beat-maker", title: "비트 메이커", description: "드럼을 찍어 나만의 비트를 만드는 리듬 메이커", category: "예술", type: "react" }
  ];

  const GRADE_BANDS = [
    {
      id: "elementary",
      label: "초등",
      subtitle: "컴퓨팅의 기초를 놀이처럼",
      icon: ICONS.elementary
    },
    {
      id: "middle",
      label: "중등",
      subtitle: "문제를 발견하고 해결하며",
      icon: ICONS.middle
    },
    {
      id: "high",
      label: "고등",
      subtitle: "기술을 연결해 깊이 있게",
      icon: ICONS.high
    }
  ];

  const state = {
    view: "home",
    learnGrade: null,
    createType: null,
    idea: "",
    exampleCategory: "전체",
    exampleQuery: "",
    createError: "",
    creating: false,
    session: null,
    messages: [],
    streaming: false,
    streamStatus: "",
    streamError: "",
    activeAssistantIndex: -1,
    abortController: null,
    mobileWorkspacePanel: "conversation",
    artifactTab: "design",
    resultView: "code",
    activeCodeFile: "",
    artifacts: emptyArtifacts(),
    recent: loadRecent()
  };

  function emptyArtifacts() {
    return {
      designDoc: null,
      generatedCode: null,
      blocklyXml: "",
      blocklyFlowchart: "",
      blocklyDetail: null,
      blocklyCodeLangs: null,
      modiModules: null,
      taskPlan: null,
      agentSteps: null,
      toolLog: [],
      appType: ""
    };
  }

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

  function announce(message) {
    globalLive.textContent = "";
    window.setTimeout(() => {
      globalLive.textContent = message;
    }, 20);
  }

  function loadRecent() {
    try {
      const stored = JSON.parse(window.localStorage.getItem(RECENT_KEY) || "[]");
      if (!Array.isArray(stored)) {
        return [];
      }
      return stored.filter((item) => (
        item &&
        typeof item.sessionId === "string" &&
        PROJECT_TYPES.some((type) => type.id === item.codingType)
      )).slice(0, MAX_RECENT);
    } catch (_error) {
      return [];
    }
  }

  function saveRecent() {
    try {
      window.localStorage.setItem(RECENT_KEY, JSON.stringify(state.recent.slice(0, MAX_RECENT)));
    } catch (_error) {
      // Storage can be unavailable in private or embedded browser contexts.
    }
  }

  function upsertRecent(project) {
    state.recent = [
      project,
      ...state.recent.filter((item) => item.sessionId !== project.sessionId)
    ].slice(0, MAX_RECENT);
    saveRecent();
    renderRailHistory();
  }

  function updateRecentPhase(phase) {
    if (!state.session) {
      return;
    }
    const current = state.recent.find((item) => item.sessionId === state.session.id);
    if (!current) {
      return;
    }
    current.phase = phase;
    current.updatedAt = new Date().toISOString();
    saveRecent();
    renderRailHistory();
  }

  function getProjectType(id) {
    return PROJECT_TYPES.find((type) => type.id === id) || PROJECT_TYPES[0];
  }

  function getGradeBand(id) {
    return GRADE_BANDS.find((grade) => grade.id === id) || GRADE_BANDS[0];
  }

  function formatRelativeDate(isoDate) {
    if (!isoDate) {
      return "최근";
    }
    const date = new Date(isoDate);
    if (Number.isNaN(date.getTime())) {
      return "최근";
    }
    const elapsed = Date.now() - date.getTime();
    const minutes = Math.max(0, Math.floor(elapsed / 60000));
    if (minutes < 1) {
      return "방금 전";
    }
    if (minutes < 60) {
      return String(minutes) + "분 전";
    }
    const hours = Math.floor(minutes / 60);
    if (hours < 24) {
      return String(hours) + "시간 전";
    }
    const days = Math.floor(hours / 24);
    if (days < 7) {
      return String(days) + "일 전";
    }
    return new Intl.DateTimeFormat("ko-KR", {
      month: "short",
      day: "numeric"
    }).format(date);
  }

  function routeTitle(view) {
    if (view === "learn") {
      return "교육과정";
    }
    if (view === "create" || view === "workspace") {
      return "자유롭게 만들기";
    }
    return "MODI Planet 3.0";
  }

  function navigate(view, options) {
    const settings = options || {};
    if (view === "learn") {
      window.location.assign("/lms");
      return;
    }
    if (state.streaming && view !== "workspace" && state.abortController) {
      state.abortController.abort();
    }

    if (view === "home") {
      state.learnGrade = null;
      state.createType = null;
      state.idea = "";
      state.exampleCategory = "전체";
      state.exampleQuery = "";
      state.createError = "";
    } else if (view === "learn" && !settings.preserve) {
      state.learnGrade = null;
    } else if (view === "create" && !settings.preserve) {
      state.createType = null;
      state.idea = "";
      state.exampleCategory = "전체";
      state.exampleQuery = "";
      state.createError = "";
      state.session = null;
      state.messages = [];
      state.artifacts = emptyArtifacts();
    }

    state.view = view;
    if (!settings.historySilent) {
      const hash = view === "home" ? "#home" : "#" + view;
      window.history.pushState({ view: view }, "", hash);
    }
    render();
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    announce(routeTitle(view) + " 화면");
    window.requestAnimationFrame(() => app.focus({ preventScroll: true }));
  }

  function render() {
    if (state.view === "learn") {
      app.innerHTML = renderLearn();
    } else if (state.view === "create") {
      app.innerHTML = renderCreateSetup();
    } else if (state.view === "workspace") {
      app.innerHTML = renderWorkspace();
      window.requestAnimationFrame(scrollChatToBottom);
    } else {
      state.view = "home";
      app.innerHTML = renderHome();
    }

    document.title = routeTitle(state.view) + (state.view === "home" ? "" : " · MODI Planet 3.0");
    updateNav();
    renderRailHistory();
  }

  function updateNav() {
    const activeView = state.view === "workspace" ? "create" : state.view;
    document.querySelectorAll("[data-nav-item]").forEach((button) => {
      const active = button.getAttribute("data-nav-item") === activeView;
      button.classList.toggle("active", active);
      if (active) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    });
  }

  function renderRailHistory() {
    const list = document.getElementById("railHistory");
    const count = document.getElementById("historyCount");
    if (!list || !count) {
      return;
    }
    count.textContent = String(state.recent.length);
    count.setAttribute("aria-label", "프로젝트 " + String(state.recent.length) + "개");

    if (!state.recent.length) {
      list.innerHTML = '<p class="rail-history-empty">첫 작품을 만들면 여기에 나타나요.</p>';
      return;
    }

    list.innerHTML = state.recent.map((project) => {
      const type = getProjectType(project.codingType);
      return [
        '<button class="rail-project-button" type="button" data-resume-session="',
        escapeHtml(project.sessionId),
        '" title="',
        escapeHtml(project.title),
        ' 이어서 만들기">',
        '<span class="rail-project-icon" aria-hidden="true">',
        escapeHtml(type.label.replace(" + ", "+").slice(0, 3)),
        '</span><span class="rail-project-copy"><strong>',
        escapeHtml(project.title),
        '</strong><span>',
        escapeHtml(type.label),
        " · ",
        escapeHtml(formatRelativeDate(project.updatedAt)),
        "</span></span></button>"
      ].join("");
    }).join("");
  }

  function renderHome() {
    return [
      '<section class="page-view home-view" aria-labelledby="homeTitle">',
      '<div class="page-frame home-frame">',
      '<header class="home-hero">',
      '<p class="eyebrow">AI LAB · 바이브 코딩</p>',
      '<h1 id="homeTitle">무엇을<br><span class="accent-word">만들어볼까요?</span></h1>',
      '<p>27차시 교육과정을 따라 배우거나, 만들고 싶은 것을 설명하고 AI와 Web·MODI 작품을 완성해보세요.</p>',
      "</header>",
      '<div class="mode-grid" aria-label="시작 방식 선택">',
      '<button class="mode-card learn-card" type="button" data-route="learn">',
      '<span class="mode-icon">', ICONS.learn, "</span>",
      "<h2>교육과정으로 배우기</h2>",
      "<p>학교 수업에 맞춘 단계별 활동으로 개념을 익히고 프로젝트를 완성해요.</p>",
      '<span class="mode-card-footer"><span>과정 살펴보기</span><span>', ICONS.arrow, "</span></span>",
      "</button>",
      '<button class="mode-card create-card" type="button" data-route="create">',
      '<span class="mode-icon">', ICONS.create, "</span>",
      "<h2>자유롭게 만들기</h2>",
      "<p>떠오른 아이디어를 말해보세요. AI와 설계하고, 만들고, 확인해요.</p>",
      '<span class="mode-card-footer"><span>새 작품 시작하기</span><span>', ICONS.arrow, "</span></span>",
      "</button>",
      "</div>",
      '<section class="home-recent" aria-labelledby="homeRecentTitle">',
      '<div class="section-heading-row"><div><h2 id="homeRecentTitle">최근 프로젝트</h2></div>',
      "<p>이어서 만들 수 있어요</p></div>",
      renderHomeRecent(),
      "</section>",
      "</div>",
      "</section>"
    ].join("");
  }

  function renderHomeRecent() {
    if (!state.recent.length) {
      return [
        '<div class="recent-empty">',
        '<span class="recent-empty-icon" aria-hidden="true">＋</span>',
        "<div><strong>아직 만든 프로젝트가 없어요</strong>",
        "<span>자유롭게 만들기에서 첫 아이디어를 시작해보세요.</span></div>",
        "</div>"
      ].join("");
    }

    return [
      '<div class="recent-grid">',
      state.recent.slice(0, 3).map((project) => {
        const type = getProjectType(project.codingType);
        return [
          '<button class="recent-project-card" type="button" data-resume-session="',
          escapeHtml(project.sessionId),
          '"><span class="recent-card-icon" aria-hidden="true">',
          escapeHtml(type.label.replace(" + ", "+").slice(0, 3)),
          '</span><span class="recent-card-copy"><strong>',
          escapeHtml(project.title),
          '</strong><span>',
          escapeHtml(type.label),
          " · ",
          escapeHtml(formatRelativeDate(project.updatedAt)),
          "</span></span>",
          ICONS.arrow,
          "</button>"
        ].join("");
      }).join(""),
      "</div>"
    ].join("");
  }

  function renderLearn() {
    return [
      '<section class="page-view learn-view" aria-labelledby="learnTitle">',
      '<div class="page-frame">',
      '<header class="page-heading">',
      '<p class="step-kicker">Curriculum</p>',
      '<h1 id="learnTitle">27차시 교육과정을<br>여는 중입니다.</h1>',
      '<p><a href="/lms">교육과정 화면으로 바로 이동</a></p>',
      "</header>",
      "</div>",
      "</section>"
    ].join("");
  }

  function renderCreateSetup() {
    const selected = state.createType ? getProjectType(state.createType) : null;
    return [
      '<section class="page-view create-view" aria-labelledby="createTitle">',
      '<div class="page-frame create-setup-frame">',
      '<div class="page-toolbar"><button class="back-button" type="button" data-route="home">',
      ICONS.back, "<span>홈으로</span></button>",
      '<span class="page-badge">AI와 함께 만들기</span></div>',
      '<header class="create-heading">',
      '<p class="step-kicker">Create</p>',
      '<h1 id="createTitle">무엇으로<br>만들어볼까요?</h1>',
      "<p>작품이 실행될 방식을 고르고 아이디어를 들려주세요. 설계부터 구현과 확인까지 함께 진행해요.</p>",
      "</header>",
      '<span class="selection-label" id="typeSelectionLabel">작품 유형</span>',
      '<div class="project-type-grid" role="group" aria-labelledby="typeSelectionLabel">',
      PROJECT_TYPES.map((type) => {
        const isSelected = state.createType === type.id;
        return [
          '<button class="type-card', isSelected ? " selected" : "", '" type="button" data-coding-type="',
          escapeHtml(type.id),
          '" aria-pressed="', isSelected ? "true" : "false", '">',
          '<span class="type-code">', escapeHtml(type.code), "</span>",
          '<span class="type-icon">', type.icon, "</span>",
          "<h2>", escapeHtml(type.label), "</h2>",
          "<p>", escapeHtml(type.description), "</p>",
          '<span class="type-check" aria-hidden="true">', ICONS.check, "</span>",
          "</button>"
        ].join("");
      }).join(""),
      "</div>",
      '<form class="idea-panel" id="ideaForm">',
      '<label for="ideaInput">만들고 싶은 것을 알려주세요',
      '<span>', selected ? escapeHtml(selected.label) + " 작품으로 함께 설계해요." : "먼저 위에서 작품 유형을 선택해주세요.", "</span></label>",
      '<div class="idea-input-wrap">',
      '<textarea id="ideaInput" name="idea" maxlength="1200" required ',
      selected && !state.creating ? "" : "disabled ",
      'placeholder="예: 클릭할 때마다 별이 생기는 나만의 밤하늘을 만들고 싶어요.">',
      escapeHtml(state.idea),
      "</textarea>",
      '<div class="idea-actions"><span class="idea-hint">Ctrl + Enter로 시작</span>',
      '<button class="primary-button" type="submit" ',
      selected && state.idea.trim() && !state.creating ? "" : "disabled ",
      '><span>', state.creating ? "작업실 여는 중…" : "함께 만들기", "</span>",
      state.creating ? "" : ICONS.arrow,
      "</button></div></div>",
      state.createError ? '<div class="error-banner" role="alert">' + escapeHtml(state.createError) + "</div>" : "",
      "</form>",
      renderExampleExplorer(),
      "</div>",
      "</section>"
    ].join("");
  }

  function getVisibleExamples() {
    const query = state.exampleQuery.trim().toLocaleLowerCase("ko-KR");
    return EXAMPLE_PROJECTS.filter((example) => {
      const categoryMatch = state.exampleCategory === "전체" || example.category === state.exampleCategory;
      const queryMatch = !query || (example.title + " " + example.description).toLocaleLowerCase("ko-KR").includes(query);
      return categoryMatch && queryMatch;
    });
  }

  function renderExampleExplorer() {
    return [
      '<section class="example-explorer" aria-labelledby="exampleExplorerTitle">',
      '<header class="example-heading"><div><p class="step-kicker">Official showcase</p>',
      '<h2 id="exampleExplorerTitle">이런 건 어때요?</h2>',
      '<p>공식 AI MODI Planet의 예시 19개를 골라 바로 시작할 수 있어요.</p></div>',
      '<label class="example-search"><span class="sr-only">예시 검색</span>',
      '<input id="exampleSearch" type="search" value="', escapeHtml(state.exampleQuery), '" placeholder="제목·설명 검색" autocomplete="off"></label></header>',
      '<div class="example-categories" role="group" aria-label="예시 카테고리">',
      EXAMPLE_CATEGORIES.map((category) => [
        '<button type="button" data-example-category="', escapeHtml(category), '" class="',
        state.exampleCategory === category ? "active" : "", '" aria-pressed="',
        state.exampleCategory === category ? "true" : "false", '">', escapeHtml(category), "</button>"
      ].join("")).join(""),
      "</div>",
      '<p class="example-result-count" id="exampleResultCount" aria-live="polite">', String(getVisibleExamples().length), "개의 예시</p>",
      '<div class="example-grid" id="exampleGrid">', renderExampleCards(), "</div>",
      "</section>"
    ].join("");
  }

  function renderExampleCards() {
    const examples = getVisibleExamples();
    if (!examples.length) {
      return '<div class="example-empty"><strong>검색 결과가 없어요</strong><span>다른 제목이나 카테고리를 선택해보세요.</span></div>';
    }
    return examples.map((example) => {
      const type = getProjectType(example.type);
      return [
        '<button class="example-card type-', escapeHtml(example.type), '" type="button" data-example-ref="', escapeHtml(example.ref), '">',
        '<span class="example-card-meta"><span>', escapeHtml(example.category), '</span><span>', escapeHtml(type.label), "</span></span>",
        '<strong>', escapeHtml(example.title), "</strong>",
        '<span class="example-description">', escapeHtml(example.description), "</span>",
        '<span class="example-start"><span>이 예시로 시작</span>', ICONS.arrow, "</span>",
        "</button>"
      ].join("");
    }).join("");
  }

  function updateExampleResults() {
    const grid = document.getElementById("exampleGrid");
    const count = document.getElementById("exampleResultCount");
    if (!grid || !count) {
      return;
    }
    const visible = getVisibleExamples();
    grid.innerHTML = renderExampleCards();
    count.textContent = visible.length + "개의 예시";
    document.querySelectorAll("[data-example-category]").forEach((button) => {
      const active = button.dataset.exampleCategory === state.exampleCategory;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function renderWorkspace() {
    const type = getProjectType(state.session ? state.session.codingType : state.createType);
    const title = state.session ? state.session.title : "새 프로젝트";
    return [
      '<section class="page-view workspace-view" aria-labelledby="workspaceTitle">',
      '<header class="workspace-topbar">',
      '<button class="back-button" type="button" data-route="home" aria-label="홈으로">',
      ICONS.back,
      "</button>",
      '<div class="workspace-title"><strong id="workspaceTitle">',
      escapeHtml(title),
      "</strong><span>",
      escapeHtml(type.label),
      " 프로젝트</span></div>",
      renderPhaseRail(),
      '<div class="workspace-status', state.streaming ? " streaming" : (state.streamError ? " error" : ""), '" id="workspaceStatus" aria-live="polite">',
      '<span class="status-pulse" aria-hidden="true"></span><span>',
      escapeHtml(state.streaming ? (state.streamStatus || "AI가 작업 중이에요") : (state.streamError ? "확인이 필요해요" : "준비됨")),
      "</span></div>",
      "</header>",
      '<div class="workspace-grid">',
      '<nav class="mobile-workspace-switch" aria-label="모바일 작업실 보기">',
      '<button type="button" data-workspace-panel="conversation" aria-controls="conversationPanel" aria-pressed="', state.mobileWorkspacePanel === "conversation" ? "true" : "false", '" class="', state.mobileWorkspacePanel === "conversation" ? "active" : "", '">대화</button>',
      '<button type="button" data-workspace-panel="artifact" aria-controls="artifactPanel" aria-pressed="', state.mobileWorkspacePanel === "artifact" ? "true" : "false", '" class="', state.mobileWorkspacePanel === "artifact" ? "active" : "", '">결과</button>',
      "</nav>",
      '<section class="conversation-panel', state.mobileWorkspacePanel === "conversation" ? "" : " mobile-panel-hidden", '" id="conversationPanel" tabindex="-1" aria-labelledby="conversationTitle">',
      '<header class="panel-header"><h2 id="conversationTitle">AI와 함께 만들기</h2>',
      '<span class="panel-chip">설계 · 구현 · 검증</span></header>',
      '<div class="chat-scroll" id="chatScroll" role="log" aria-live="polite" aria-relevant="additions text">',
      renderMessagesMarkup(),
      renderStreamFeedback(),
      "</div>",
      renderComposer(),
      "</section>",
      '<section class="artifact-panel', state.mobileWorkspacePanel === "artifact" ? "" : " mobile-panel-hidden", '" id="artifactPanel" tabindex="-1" aria-labelledby="artifactPanelTitle">',
      '<h2 id="artifactPanelTitle" class="sr-only">프로젝트 작업 결과</h2>',
      renderArtifactTabs(),
      '<div class="artifact-body" id="artifactBody">',
      renderArtifactBodyMarkup(),
      "</div>",
      "</section>",
      "</div>",
      "</section>"
    ].join("");
  }

  function normalizePhase(phase) {
    const value = String(phase || "design").toLowerCase();
    if (value === "implement" || value === "구현") {
      return "implement";
    }
    if (value === "verify" || value === "검증") {
      return "verify";
    }
    return "design";
  }

  function renderPhaseRail() {
    const current = normalizePhase(state.session ? state.session.phase : "design");
    const phases = [
      ["design", "설계"],
      ["implement", "구현"],
      ["verify", "검증"]
    ];
    const currentIndex = phases.findIndex((phase) => phase[0] === current);
    return [
      '<div class="phase-rail" id="phaseRail" aria-label="프로젝트 진행 단계">',
      phases.map((phase, index) => [
        '<div class="phase-step ',
        index < currentIndex ? "completed" : (index === currentIndex ? "active" : ""),
        '"', index === currentIndex ? ' aria-current="step"' : "", ">",
        '<span class="phase-dot" aria-hidden="true">',
        index < currentIndex ? "✓" : String(index + 1),
        "</span><span>", phase[1], "</span></div>"
      ].join("")).join(""),
      "</div>"
    ].join("");
  }

  function renderMessagesMarkup() {
    if (!state.messages.length) {
      return [
        '<div class="chat-message assistant">',
        '<span class="message-avatar" aria-hidden="true">AI</span>',
        '<div class="message-body"><span class="message-name">MODI AI</span>',
        '<div class="message-content">아이디어를 함께 정리해볼게요. 만들고 싶은 모습을 편하게 이야기해주세요.</div>',
        "</div></div>"
      ].join("");
    }

    return state.messages.map((message, index) => [
      '<div class="chat-message ', message.role === "user" ? "user" : "assistant", '" data-message-index="', String(index), '">',
      '<span class="message-avatar" aria-hidden="true">', message.role === "user" ? "나" : "AI", "</span>",
      '<div class="message-body"><span class="message-name">',
      message.role === "user" ? "나" : "MODI AI",
      '</span><div class="message-content',
      message.streaming ? " pending" : "",
      '" data-message-content="', String(index), '">',
      escapeHtml(message.text || (message.streaming ? "생각을 정리하고 있어요…" : "")),
      "</div></div></div>"
    ].join("")).join("");
  }

  function renderStreamFeedback() {
    return [
      state.streaming && state.streamStatus
        ? '<div class="stream-status-line" id="streamStatusLine">' + escapeHtml(state.streamStatus) + "</div>"
        : '<div id="streamStatusLine"></div>',
      state.streamError
        ? '<div class="error-banner" id="streamErrorBanner" role="alert">' + escapeHtml(state.streamError) + "</div>"
        : '<div id="streamErrorBanner"></div>'
    ].join("");
  }

  function renderComposer() {
    return [
      '<form class="composer" id="chatForm">',
      '<label class="sr-only" for="chatInput">AI에게 메시지 보내기</label>',
      '<div class="composer-box"><textarea id="chatInput" name="message" maxlength="2000" ',
      state.streaming ? "disabled " : "",
      'placeholder="수정할 점이나 다음 아이디어를 말해주세요."></textarea>',
      '<div class="composer-meta"><span>Enter 전송 · Shift + Enter 줄바꿈</span>',
      '<button class="send-button" id="sendButton" type="submit" disabled aria-label="메시지 보내기">',
      ICONS.send,
      "</button></div></div></form>"
    ].join("");
  }

  function renderArtifactTabs() {
    const tabs = [
      ["design", "설계 요약"],
      ["result", "만든 결과"],
      ["progress", "진행 과정"]
    ];
    return [
      '<div class="artifact-tabs" role="tablist" aria-label="작업 결과 보기">',
      tabs.map((tab) => [
        '<button class="artifact-tab', state.artifactTab === tab[0] ? " active" : "",
        '" type="button" role="tab" data-artifact-tab="', tab[0],
        '" aria-selected="', state.artifactTab === tab[0] ? "true" : "false",
        '" tabindex="', state.artifactTab === tab[0] ? "0" : "-1", '" aria-controls="artifactBody">', tab[1], "</button>"
      ].join("")).join(""),
      "</div>"
    ].join("");
  }

  function renderArtifactBodyMarkup() {
    if (state.artifactTab === "result") {
      return renderResultArtifact();
    }
    if (state.artifactTab === "progress") {
      return renderProgressArtifact();
    }
    return renderDesignArtifact();
  }

  function renderArtifactEmpty(title, description) {
    return [
      '<div class="empty-artifact">',
      '<div class="empty-artifact-visual" aria-hidden="true"><span></span><span></span><span></span></div>',
      "<strong>", escapeHtml(title), "</strong>",
      "<p>", escapeHtml(description), "</p>",
      "</div>"
    ].join("");
  }

  function itemLabel(item, fallback) {
    if (typeof item === "string") {
      return item;
    }
    if (!item || typeof item !== "object") {
      return fallback;
    }
    return item.name || item.title || item.description || fallback;
  }

  function renderSimpleList(items, fallback) {
    const list = asList(items);
    if (!list.length) {
      return '<li>' + escapeHtml(fallback) + "</li>";
    }
    return list.map((item) => "<li>" + escapeHtml(itemLabel(item, fallback)) + "</li>").join("");
  }

  function renderFeatureList(features) {
    const list = asList(features);
    if (!list.length) {
      return "<li>대화하며 핵심 기능을 정리하고 있어요.</li>";
    }
    return list.map((feature) => {
      if (typeof feature === "string") {
        return "<li><strong>" + escapeHtml(feature) + "</strong></li>";
      }
      return [
        "<li><strong>", escapeHtml(feature.name || "기능"), "</strong>",
        feature.description ? "<span>" + escapeHtml(feature.description) + "</span>" : "",
        "</li>"
      ].join("");
    }).join("");
  }

  function renderDesignArtifact() {
    const doc = state.artifacts.designDoc;
    if (!doc) {
      return renderArtifactEmpty(
        "아이디어를 설계하고 있어요",
        "AI와 나눈 대화에서 프로젝트의 목표, 사용자, 기능과 화면 구성이 정리되면 이곳에 나타납니다."
      );
    }

    const projectName = doc.project_name || doc.projectName || (state.session && state.session.title) || "나의 프로젝트";
    const description = doc.description || "함께 이야기하며 프로젝트 설명을 구체화하고 있어요.";
    const users = doc.users || doc.target_users || [];
    const features = doc.features || [];
    const pages = doc.pages || [];
    const flows = doc.user_flows || doc.userFlows || [];

    return [
      '<div class="artifact-section">',
      '<header class="artifact-section-header"><h2>설계 요약</h2>',
      "<p>대화에서 합의한 내용을 한눈에 확인할 수 있어요.</p></header>",
      '<article class="design-hero-card"><span class="mini-label">Project</span>',
      "<h3>", escapeHtml(projectName), "</h3><p>", escapeHtml(description), "</p></article>",
      '<div class="summary-grid">',
      '<section class="summary-card"><span class="mini-label">Audience</span><h3>누가 사용하나요?</h3>',
      '<ul class="summary-list">', renderSimpleList(users, "사용자를 정리하고 있어요."), "</ul></section>",
      '<section class="summary-card"><span class="mini-label">Features</span><h3>핵심 기능</h3>',
      '<ul class="feature-list">', renderFeatureList(features), "</ul></section>",
      '<section class="summary-card"><span class="mini-label">Screens</span><h3>화면과 구성</h3>',
      '<ul class="summary-list">', renderSimpleList(pages, "화면 구성을 정리하고 있어요."), "</ul></section>",
      '<section class="summary-card"><span class="mini-label">Flow</span><h3>사용 흐름</h3>',
      '<ul class="summary-list">', renderSimpleList(flows, "사용 흐름을 정리하고 있어요."), "</ul></section>",
      "</div></div>"
    ].join("");
  }

  function normalizedCodeEntries() {
    const generated = state.artifacts.generatedCode;
    if (!generated) {
      return [];
    }
    if (Array.isArray(generated)) {
      return generated.map((file, index) => {
        if (typeof file === "string") {
          return ["file-" + String(index + 1) + ".txt", file];
        }
        return [
          file.path || file.name || "file-" + String(index + 1) + ".txt",
          file.content || file.code || JSON.stringify(file, null, 2)
        ];
      });
    }
    if (typeof generated === "object") {
      return Object.entries(generated).map((entry) => {
        const value = entry[1];
        if (typeof value === "string") {
          return [entry[0], value];
        }
        return [entry[0], value && (value.content || value.code) ? (value.content || value.code) : JSON.stringify(value, null, 2)];
      });
    }
    return [["result.txt", String(generated)]];
  }

  function renderResultArtifact() {
    const codeEntries = normalizedCodeEntries();
    const hasCode = codeEntries.length > 0;
    const hasBlockly = Boolean(state.artifacts.blocklyXml);
    if (!hasCode && !hasBlockly) {
      return renderArtifactEmpty(
        "아직 만들어진 결과가 없어요",
        "설계가 충분히 구체화되면 AI가 코드를 만들고 검증한 결과를 이곳에 보여줍니다."
      );
    }

    let view = state.resultView;
    if (view === "code" && !hasCode) {
      view = "blockly";
    }
    if (view === "blockly" && !hasBlockly) {
      view = "code";
    }
    state.resultView = view;

    return [
      '<div class="artifact-section">',
      '<div class="result-toolbar"><div class="result-switches">',
      hasCode ? '<button class="result-switch' + (view === "code" ? " active" : "") + '" type="button" data-result-view="code">코드</button>' : "",
      hasBlockly ? '<button class="result-switch' + (view === "blockly" ? " active" : "") + '" type="button" data-result-view="blockly">MODI 블록</button>' : "",
      '</div><span class="artifact-meta">',
      escapeHtml(state.artifacts.appType || getProjectType(state.session.codingType).label),
      "</span></div>",
      view === "blockly" ? renderBlocklyResult() : renderCodeResult(codeEntries),
      "</div>"
    ].join("");
  }

  function renderCodeResult(entries) {
    if (!entries.length) {
      return "";
    }
    const activeExists = entries.some((entry) => entry[0] === state.activeCodeFile);
    const activeFile = activeExists ? state.activeCodeFile : entries[0][0];
    state.activeCodeFile = activeFile;
    const activeEntry = entries.find((entry) => entry[0] === activeFile) || entries[0];
    return [
      '<div class="code-shell">',
      '<div class="code-file-tabs" role="tablist" aria-label="생성된 파일">',
      entries.map((entry) => [
        '<button class="code-file-tab', entry[0] === activeFile ? " active" : "",
        '" type="button" role="tab" data-code-file="', escapeHtml(entry[0]),
        '" aria-selected="', entry[0] === activeFile ? "true" : "false", '">',
        escapeHtml(entry[0]),
        "</button>"
      ].join("")).join(""),
      '</div><pre class="code-view" tabindex="0"><code>',
      escapeHtml(activeEntry[1]),
      "</code></pre></div>"
    ].join("");
  }

  function extractBlocklyNames(xml) {
    if (!xml) {
      return [];
    }
    const names = [];
    const expression = /<block[^>]+type=["']([^"']+)["']/g;
    let match;
    while ((match = expression.exec(xml)) && names.length < 5) {
      const label = match[1].replace(/^modi_/, "").replace(/_/g, " ");
      if (!names.includes(label)) {
        names.push(label);
      }
    }
    return names;
  }

  function stringifyDetail(value, fallback) {
    if (value == null || value === "") {
      return fallback;
    }
    if (typeof value === "string") {
      return value;
    }
    try {
      return JSON.stringify(value, null, 2);
    } catch (_error) {
      return fallback;
    }
  }

  function renderBlocklyResult() {
    const names = extractBlocklyNames(state.artifacts.blocklyXml);
    const blockNames = names.length ? names : ["시작하기", "센서 값 읽기", "조건 확인", "모듈 움직이기"];
    return [
      '<section class="blockly-card" aria-labelledby="blocklyResultTitle">',
      '<div class="blockly-visual"><span class="mini-label">MODI BLOCKS</span>',
      '<h2 id="blocklyResultTitle" class="sr-only">생성된 MODI 블록</h2>',
      '<div class="blockly-placeholder" aria-label="블록 구성 미리보기">',
      blockNames.map((name) => '<span class="visual-block">' + escapeHtml(name) + "</span>").join(""),
      "</div></div>",
      '<div class="blockly-info"><div><span class="mini-label">Flow</span><p>',
      escapeHtml(stringifyDetail(state.artifacts.blocklyFlowchart, "블록 실행 흐름이 생성되었어요.")),
      '</p></div><div><span class="mini-label">MODI Modules</span><p>',
      escapeHtml(stringifyDetail(state.artifacts.modiModules, "필요한 모듈 구성을 확인하고 있어요.")),
      "</p></div></div></section>",
      '<details class="xml-details"><summary>Blockly XML 원본 보기</summary><pre>',
      escapeHtml(state.artifacts.blocklyXml),
      "</pre></details>"
    ].join("");
  }

  function normalizeTasks() {
    const taskPlan = state.artifacts.taskPlan;
    if (taskPlan && Array.isArray(taskPlan.tasks)) {
      return taskPlan.tasks.map((task, index) => ({
        name: task.name || "작업 " + String(index + 1),
        description: task.description || asList(task.files).join(", "),
        status: task.status || "pending"
      }));
    }
    const agentSteps = state.artifacts.agentSteps;
    if (agentSteps && Array.isArray(agentSteps.steps)) {
      return agentSteps.steps.map((step, index) => ({
        name: step.step || step.name || "단계 " + String(index + 1),
        description: step.description || step.action || "",
        status: step.status || "done"
      }));
    }
    return state.artifacts.toolLog.map((step) => ({
      name: step.label,
      description: step.description || "",
      status: step.status
    }));
  }

  function taskStatusClass(status) {
    const value = String(status || "").toLowerCase();
    if (value === "done" || value === "completed" || value === "success") {
      return "done";
    }
    if (value === "active" || value === "running" || value === "in_progress") {
      return "active";
    }
    return "";
  }

  function taskStatusLabel(status) {
    const cssClass = taskStatusClass(status);
    if (cssClass === "done") {
      return "완료";
    }
    if (cssClass === "active") {
      return "진행 중";
    }
    return "예정";
  }

  function renderProgressArtifact() {
    const tasks = normalizeTasks();
    if (!tasks.length) {
      return renderArtifactEmpty(
        "진행 과정이 여기에 기록돼요",
        "AI가 설계를 정리하고 코드를 만들고 검증하는 과정이 시작되면 단계별 상태를 확인할 수 있어요."
      );
    }
    return [
      '<div class="artifact-section">',
      '<header class="artifact-section-header"><h2>진행 과정</h2>',
      "<p>AI가 지금 어떤 일을 하고 있는지 단계별로 보여드려요.</p></header>",
      '<section class="progress-card"><ul class="task-list">',
      tasks.map((task, index) => {
        const cssClass = taskStatusClass(task.status);
        return [
          "<li><span class=\"task-state ", cssClass, '" aria-hidden="true">',
          cssClass === "done" ? "✓" : String(index + 1),
          '</span><span class="task-copy"><strong>', escapeHtml(task.name), "</strong>",
          task.description ? "<span>" + escapeHtml(task.description) + "</span>" : "",
          '</span><span class="task-badge">', taskStatusLabel(task.status), "</span></li>"
        ].join("");
      }).join(""),
      "</ul></section>",
      state.streamStatus
        ? '<div class="steps-timeline"><div class="timeline-row ' + (state.streaming ? "running" : "") + '"><span class="timeline-dot"></span>' + escapeHtml(state.streamStatus) + "</div></div>"
        : "",
      "</div>"
    ].join("");
  }

  function renderMessages() {
    const scroll = document.getElementById("chatScroll");
    if (!scroll) {
      return;
    }
    scroll.innerHTML = renderMessagesMarkup() + renderStreamFeedback();
    scrollChatToBottom();
  }

  function renderArtifactBody() {
    const body = document.getElementById("artifactBody");
    if (body) {
      body.innerHTML = renderArtifactBodyMarkup();
    }
    document.querySelectorAll("[data-artifact-tab]").forEach((button) => {
      const active = button.getAttribute("data-artifact-tab") === state.artifactTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.setAttribute("tabindex", active ? "0" : "-1");
    });
  }

  function setMobileWorkspacePanel(panel) {
    state.mobileWorkspacePanel = panel === "artifact" ? "artifact" : "conversation";
    document.querySelectorAll("[data-workspace-panel]").forEach((button) => {
      const active = button.dataset.workspacePanel === state.mobileWorkspacePanel;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    const conversation = document.getElementById("conversationPanel");
    const artifact = document.getElementById("artifactPanel");
    if (conversation && artifact) {
      conversation.classList.toggle("mobile-panel-hidden", state.mobileWorkspacePanel !== "conversation");
      artifact.classList.toggle("mobile-panel-hidden", state.mobileWorkspacePanel !== "artifact");
      (state.mobileWorkspacePanel === "conversation" ? conversation : artifact).focus({ preventScroll: true });
    }
  }

  function updateWorkspaceChrome() {
    const phaseRail = document.getElementById("phaseRail");
    if (phaseRail) {
      const holder = document.createElement("div");
      holder.innerHTML = renderPhaseRail();
      phaseRail.replaceWith(holder.firstElementChild);
    }
    const status = document.getElementById("workspaceStatus");
    if (status) {
      status.className = "workspace-status" + (state.streaming ? " streaming" : (state.streamError ? " error" : ""));
      const label = status.querySelector("span:last-child");
      if (label) {
        label.textContent = state.streaming
          ? (state.streamStatus || "AI가 작업 중이에요")
          : (state.streamError ? "확인이 필요해요" : "준비됨");
      }
    }
  }

  function updateComposerState() {
    const input = document.getElementById("chatInput");
    const button = document.getElementById("sendButton");
    if (!input || !button) {
      return;
    }
    input.disabled = state.streaming;
    button.disabled = state.streaming || !input.value.trim();
  }

  function scrollChatToBottom() {
    const scroll = document.getElementById("chatScroll");
    if (scroll) {
      scroll.scrollTop = scroll.scrollHeight;
    }
  }

  function resumeProject(sessionId) {
    const project = state.recent.find((item) => item.sessionId === sessionId);
    if (!project) {
      return;
    }
    state.session = {
      id: project.sessionId,
      codingType: project.codingType,
      title: project.title,
      phase: project.phase || "design",
      chatEndpoint: safeChatEndpoint(project.chatEndpoint, project.sessionId)
    };
    state.createType = project.codingType;
    state.messages = [{
      role: "assistant",
      text: "프로젝트를 다시 열었어요. 이어서 만들고 싶은 내용을 이야기해주세요.",
      streaming: false
    }];
    state.artifacts = emptyArtifacts();
    state.artifactTab = "design";
    state.mobileWorkspacePanel = "conversation";
    state.streamStatus = "";
    state.streamError = "";
    state.view = "workspace";
    window.history.pushState({ view: "workspace" }, "", "#workspace");
    render();
    announce(project.title + " 프로젝트를 열었습니다.");
  }

  function createChatEndpoint(sessionId) {
    return productPath("api/v3/create/sessions/" + encodeURIComponent(sessionId) + "/chat");
  }

  function productPath(path) {
    const relativePath = String(path || "").replace(/^\/+/, "");
    return new URL(relativePath, document.baseURI).pathname;
  }

  function safeChatEndpoint(endpoint, sessionId) {
    if (typeof endpoint === "string" && !endpoint.startsWith("//") && !endpoint.includes("://")) {
      return productPath(endpoint);
    }
    return createChatEndpoint(sessionId);
  }

  function conciseTitle(idea) {
    const normalized = String(idea || "").replace(/\s+/g, " ").trim();
    if (!normalized) {
      return "새 프로젝트";
    }
    return normalized.length > 34 ? normalized.slice(0, 34) + "…" : normalized;
  }

  async function startProject() {
    if (!state.createType || !state.idea.trim() || state.creating) {
      return;
    }
    state.creating = true;
    state.createError = "";
    render();

    try {
      const response = await window.fetch(productPath("api/v3/create/sessions"), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          coding_type: state.createType
        })
      });
      if (!response.ok) {
        throw new Error(await responseErrorMessage(response, "작업실을 열지 못했어요."));
      }
      const payload = await response.json();
      const sessionId = String(payload.session_id || payload.id || "");
      if (!sessionId) {
        throw new Error("세션 정보를 받지 못했어요. 잠시 후 다시 시도해주세요.");
      }

      const title = conciseTitle(state.idea);
      state.session = {
        id: sessionId,
        codingType: payload.coding_type || state.createType,
        title: title,
        phase: normalizePhase(payload.phase || "design"),
        chatEndpoint: safeChatEndpoint(payload.chat_endpoint, sessionId)
      };
      state.messages = [];
      state.artifacts = emptyArtifacts();
      state.artifactTab = "design";
      state.mobileWorkspacePanel = "conversation";
      state.resultView = state.createType === "blockly" ? "blockly" : "code";
      state.activeCodeFile = "";
      state.streamStatus = "";
      state.streamError = "";
      state.view = "workspace";
      state.creating = false;

      upsertRecent({
        sessionId: sessionId,
        codingType: state.session.codingType,
        title: title,
        phase: state.session.phase,
        chatEndpoint: state.session.chatEndpoint,
        updatedAt: new Date().toISOString()
      });
      window.history.pushState({ view: "workspace" }, "", "#workspace");
      render();
      announce("프로젝트 작업실을 열었습니다.");
      await sendChat(state.idea);
    } catch (error) {
      state.creating = false;
      state.createError = error instanceof Error ? error.message : "작업실을 열지 못했어요.";
      render();
      announce(state.createError);
    }
  }

  async function responseErrorMessage(response, fallback) {
    try {
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        const payload = await response.json();
        return payload.message || payload.detail || fallback;
      }
      const text = await response.text();
      return text.trim().slice(0, 240) || fallback;
    } catch (_error) {
      return fallback;
    }
  }

  function assistantMessage() {
    const message = state.messages[state.activeAssistantIndex];
    return message && message.role === "assistant" ? message : null;
  }

  function updateActiveAssistantText() {
    const message = assistantMessage();
    const element = document.querySelector('[data-message-content="' + String(state.activeAssistantIndex) + '"]');
    if (!message || !element) {
      renderMessages();
      return;
    }
    element.textContent = message.text || (message.streaming ? "생각을 정리하고 있어요…" : "");
    element.classList.toggle("pending", Boolean(message.streaming));
    scrollChatToBottom();
  }

  async function sendChat(messageText) {
    const message = String(messageText || "").trim();
    if (!message || state.streaming || !state.session) {
      return;
    }

    state.messages.push({ role: "user", text: message, streaming: false });
    state.messages.push({ role: "assistant", text: "", streaming: true });
    state.activeAssistantIndex = state.messages.length - 1;
    state.streaming = true;
    state.streamStatus = "아이디어를 살펴보고 있어요…";
    state.streamError = "";
    state.abortController = new AbortController();
    renderMessages();
    updateWorkspaceChrome();
    updateComposerState();

    let receivedDone = false;
    try {
      const response = await window.fetch(state.session.chatEndpoint, {
        method: "POST",
        credentials: "same-origin",
        signal: state.abortController.signal,
        headers: {
          "Accept": "text/event-stream",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          message: message,
          coding_type: state.session.codingType
        })
      });
      if (!response.ok) {
        throw new Error(await responseErrorMessage(response, "AI와 연결하지 못했어요."));
      }
      if (!response.body) {
        throw new Error("스트리밍 응답을 열지 못했어요.");
      }

      receivedDone = await consumeEventStream(response);
      if (!receivedDone) {
        const active = assistantMessage();
        if (active) {
          active.streaming = false;
        }
      }
    } catch (error) {
      if (error && error.name === "AbortError") {
        return;
      }
      state.streamError = error instanceof Error ? error.message : "응답을 받지 못했어요.";
      const active = assistantMessage();
      if (active) {
        active.streaming = false;
        if (!active.text) {
          active.text = "잠시 연결이 매끄럽지 않았어요. 같은 내용을 한 번 더 보내주세요.";
        }
      }
      announce(state.streamError);
    } finally {
      state.streaming = false;
      state.streamStatus = receivedDone ? "이번 단계가 완료되었어요." : "";
      state.abortController = null;
      const active = assistantMessage();
      if (active) {
        active.streaming = false;
      }
      renderMessages();
      updateWorkspaceChrome();
      updateComposerState();
      renderArtifactBody();
      updateRecentPhase(state.session.phase);
      const input = document.getElementById("chatInput");
      if (input && !state.streamError) {
        input.focus();
      }
    }
  }

  async function consumeEventStream(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let receivedDone = false;

    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
      const records = buffer.split(/\r?\n\r?\n/);
      buffer = records.pop() || "";
      records.forEach((record) => {
        const event = parseEventRecord(record);
        if (event) {
          if (event.type === "done") {
            receivedDone = true;
          }
          handleStreamEvent(event);
        }
      });
      if (chunk.done) {
        break;
      }
    }

    if (buffer.trim()) {
      const event = parseEventRecord(buffer);
      if (event) {
        if (event.type === "done") {
          receivedDone = true;
        }
        handleStreamEvent(event);
      }
    }
    return receivedDone;
  }

  function parseEventRecord(record) {
    const data = record.split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) {
      return null;
    }
    if (data === "[DONE]") {
      return { type: "done" };
    }
    try {
      return JSON.parse(data);
    } catch (_error) {
      return { type: "token", text: data };
    }
  }

  function addToolLog(label, description, status, key) {
    const toolKey = key || label;
    const current = state.artifacts.toolLog.find((item) => item.key === toolKey);
    if (current) {
      current.label = label || current.label;
      current.description = description || current.description;
      current.status = status || current.status;
      return;
    }
    state.artifacts.toolLog.push({
      key: toolKey,
      label: label || "작업",
      description: description || "",
      status: status || "running"
    });
    state.artifacts.toolLog = state.artifacts.toolLog.slice(-12);
  }

  function applyArtifactPayload(event) {
    if (event.design_doc) {
      state.artifacts.designDoc = event.design_doc;
    }
    if (event.generated_code) {
      state.artifacts.generatedCode = event.generated_code;
      if (!state.activeCodeFile) {
        const entries = normalizedCodeEntries();
        state.activeCodeFile = entries.length ? entries[0][0] : "";
      }
    }
    if (event.blockly_xml) {
      state.artifacts.blocklyXml = event.blockly_xml;
    }
    if (event.blockly_flowchart) {
      state.artifacts.blocklyFlowchart = event.blockly_flowchart;
    }
    if (event.blockly_detail) {
      state.artifacts.blocklyDetail = event.blockly_detail;
    }
    if (event.blockly_code_langs) {
      state.artifacts.blocklyCodeLangs = event.blockly_code_langs;
    }
    if (event.modi_modules) {
      state.artifacts.modiModules = event.modi_modules;
    }
    if (event.task_plan) {
      state.artifacts.taskPlan = event.task_plan;
    }
    if (event.agent_steps) {
      state.artifacts.agentSteps = event.agent_steps;
    }
    if (event.app_type) {
      state.artifacts.appType = event.app_type;
    }
    if (event.phase && state.session) {
      state.session.phase = normalizePhase(event.phase);
    }
  }

  function handleStreamEvent(event) {
    if (!event || typeof event !== "object") {
      return;
    }

    applyArtifactPayload(event);
    const active = assistantMessage();
    switch (event.type) {
      case "token":
        if (active) {
          if (!active.text && active.streaming) {
            active.text = "";
          }
          active.text += String(event.text || event.token || "");
          active.streaming = true;
          updateActiveAssistantText();
        }
        break;
      case "status":
        state.streamStatus = String(event.message || event.text || "작업하고 있어요…");
        renderMessages();
        updateWorkspaceChrome();
        if (state.artifactTab === "progress") {
          renderArtifactBody();
        }
        break;
      case "phase":
        if (state.session) {
          state.session.phase = normalizePhase(event.phase || event.value);
          updateWorkspaceChrome();
        }
        break;
      case "tool_call":
        addToolLog(
          event.description || event.name || "도구 실행",
          event.name || "",
          "running",
          event.name || event.description
        );
        if (state.artifactTab === "progress") {
          renderArtifactBody();
        }
        break;
      case "tool_result":
        addToolLog(
          event.description || event.name || "도구 실행",
          typeof event.result === "string" ? event.result.slice(0, 120) : "",
          "done",
          event.name || event.description
        );
        if (state.artifactTab === "progress") {
          renderArtifactBody();
        }
        break;
      case "code_validated":
        state.streamStatus = "코드 검증을 마쳤어요.";
        state.artifactTab = "result";
        state.resultView = "code";
        renderArtifactBody();
        updateWorkspaceChrome();
        break;
      case "blockly_ready":
        state.streamStatus = "MODI 블록을 준비했어요.";
        state.artifactTab = "result";
        state.resultView = "blockly";
        renderArtifactBody();
        updateWorkspaceChrome();
        break;
      case "error":
        state.streamError = String(event.message || "작업 중 문제가 생겼어요.");
        if (active) {
          active.streaming = false;
        }
        renderMessages();
        updateWorkspaceChrome();
        announce(state.streamError);
        break;
      case "done":
        if (active) {
          active.streaming = false;
          if (!active.text && event.message) {
            active.text = String(event.message);
          }
          if (!active.text) {
            active.text = "좋아요. 이번 단계를 마쳤어요. 결과를 확인하고 다음 요청을 알려주세요.";
          }
        }
        state.streamStatus = "이번 단계가 완료되었어요.";
        addToolLog("이번 단계 완료", "결과가 작업실에 반영되었어요.", "done", "turn-done-" + String(Date.now()));
        renderMessages();
        renderArtifactBody();
        updateWorkspaceChrome();
        announce("AI 작업이 완료되었습니다.");
        break;
      default:
        if (event.message && !event.type && active) {
          active.text += String(event.message);
          updateActiveAssistantText();
        }
        renderArtifactBody();
        updateWorkspaceChrome();
    }
  }

  document.addEventListener("click", (event) => {
    const workspacePanelButton = event.target.closest("[data-workspace-panel]");
    if (workspacePanelButton) {
      setMobileWorkspacePanel(workspacePanelButton.dataset.workspacePanel);
      return;
    }

    const exampleCategoryButton = event.target.closest("[data-example-category]");
    if (exampleCategoryButton) {
      state.exampleCategory = exampleCategoryButton.dataset.exampleCategory;
      updateExampleResults();
      return;
    }

    const exampleButton = event.target.closest("[data-example-ref]");
    if (exampleButton) {
      const example = EXAMPLE_PROJECTS.find((item) => item.ref === exampleButton.dataset.exampleRef);
      if (!example) {
        return;
      }
      state.createType = example.type;
      state.idea = example.title + " 프로젝트를 만들어줘. " + example.description + ".";
      state.createError = "";
      render();
      window.requestAnimationFrame(() => {
        const input = document.getElementById("ideaInput");
        if (input) {
          input.scrollIntoView({ behavior: "smooth", block: "center" });
          input.focus({ preventScroll: true });
          input.setSelectionRange(input.value.length, input.value.length);
        }
      });
      announce(example.title + " 예시를 선택했습니다.");
      return;
    }

    const routeButton = event.target.closest("[data-route]");
    if (routeButton) {
      navigate(routeButton.getAttribute("data-route"));
      return;
    }

    const gradeButton = event.target.closest("[data-grade]");
    if (gradeButton) {
      state.learnGrade = gradeButton.getAttribute("data-grade");
      render();
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      announce(getGradeBand(state.learnGrade).label + " 교육과정 9차시");
      app.focus({ preventScroll: true });
      return;
    }

    const typeButton = event.target.closest("[data-coding-type]");
    if (typeButton) {
      state.createType = typeButton.getAttribute("data-coding-type");
      state.createError = "";
      render();
      const input = document.getElementById("ideaInput");
      if (input) {
        input.focus();
      }
      announce(getProjectType(state.createType).label + " 작품을 선택했습니다.");
      return;
    }

    const actionButton = event.target.closest("[data-action]");
    if (actionButton && actionButton.getAttribute("data-action") === "all-grades") {
      state.learnGrade = null;
      render();
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      app.focus({ preventScroll: true });
      return;
    }

    const recentButton = event.target.closest("[data-resume-session]");
    if (recentButton) {
      resumeProject(recentButton.getAttribute("data-resume-session"));
      return;
    }

    const artifactTab = event.target.closest("[data-artifact-tab]");
    if (artifactTab) {
      state.artifactTab = artifactTab.getAttribute("data-artifact-tab");
      renderArtifactBody();
      return;
    }

    const resultView = event.target.closest("[data-result-view]");
    if (resultView) {
      state.resultView = resultView.getAttribute("data-result-view");
      renderArtifactBody();
      return;
    }

    const codeFile = event.target.closest("[data-code-file]");
    if (codeFile) {
      state.activeCodeFile = codeFile.getAttribute("data-code-file");
      renderArtifactBody();
    }
  });

  document.addEventListener("input", (event) => {
    if (event.target.id === "exampleSearch") {
      state.exampleQuery = event.target.value;
      updateExampleResults();
      return;
    }
    if (event.target.id === "ideaInput") {
      state.idea = event.target.value;
      const button = document.querySelector("#ideaForm .primary-button");
      if (button) {
        button.disabled = !state.createType || !state.idea.trim() || state.creating;
      }
      return;
    }
    if (event.target.id === "chatInput") {
      const button = document.getElementById("sendButton");
      if (button) {
        button.disabled = state.streaming || !event.target.value.trim();
      }
      event.target.style.height = "auto";
      event.target.style.height = Math.min(event.target.scrollHeight, 170) + "px";
    }
  });

  document.addEventListener("keydown", (event) => {
    const artifactTab = event.target.closest && event.target.closest("[data-artifact-tab]");
    if (artifactTab && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      const tabs = Array.from(document.querySelectorAll("[data-artifact-tab]"));
      const current = tabs.indexOf(artifactTab);
      const next = event.key === "Home" ? 0
        : event.key === "End" ? tabs.length - 1
          : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      event.preventDefault();
      state.artifactTab = tabs[next].dataset.artifactTab;
      renderArtifactBody();
      tabs[next].focus();
      return;
    }
    if (event.target.id === "ideaInput" && event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      startProject();
      return;
    }
    if (event.target.id === "chatInput" && event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      const form = document.getElementById("chatForm");
      if (form && event.target.value.trim() && !state.streaming) {
        form.requestSubmit();
      }
    }
  });

  document.addEventListener("submit", (event) => {
    if (event.target.id === "ideaForm") {
      event.preventDefault();
      startProject();
      return;
    }
    if (event.target.id === "chatForm") {
      event.preventDefault();
      const input = document.getElementById("chatInput");
      if (!input || !input.value.trim() || state.streaming) {
        return;
      }
      const message = input.value;
      input.value = "";
      input.style.height = "auto";
      updateComposerState();
      sendChat(message);
    }
  });

  window.addEventListener("popstate", () => {
    const hash = window.location.hash.replace(/^#/, "");
    const route = ["home", "learn", "create"].includes(hash) ? hash : "home";
    navigate(route, { historySilent: true });
  });

  const initialHash = window.location.hash.replace(/^#/, "");
  if (initialHash === "learn") {
    window.location.replace("/lms");
    return;
  }
  state.view = ["home", "learn", "create"].includes(initialHash) ? initialHash : "home";
  render();
  window.requestAnimationFrame(() => window.scrollTo(0, 0));
})();
