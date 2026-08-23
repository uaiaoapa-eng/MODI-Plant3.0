(() => {
  "use strict";

  const main = document.getElementById("lmsMain");
  const planDialog = document.getElementById("planDialog");
  const planKicker = document.getElementById("planKicker");
  const planTitle = document.getElementById("planTitle");
  const planBody = document.getElementById("planBody");
  const lessonPlayer = document.getElementById("lessonPlayer");
  const learningStudio = document.getElementById("learningStudio");
  const studioToggle = document.getElementById("studioToggle");
  const studioBackdrop = document.getElementById("studioBackdrop");
  const mobileTeacherToggle = document.getElementById("mobileTeacherToggle");
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

  const WORLD_PROFILES = {
    elementary: { id: "elementary", name: "MOMO PLANET", zone: "별빛 행성학교" },
    middle: { id: "middle", name: "NOVA CITY", zone: "루나 메이커 페스티벌" },
    high: { id: "high", name: "ORBIT-9", zone: "심우주 시스템 미션" }
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
    previewSource: "preset",
    previewDemoActive: false,
    studioOpen: false,
    teacherNoteOpen: false,
    quizAnswers: {},
    checklistAnswers: {},
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
  const DEPTH_POINTER_QUERY = window.matchMedia("(hover: hover) and (pointer: fine)");
  const REDUCED_MOTION_QUERY = window.matchMedia("(prefers-reduced-motion: reduce)");
  let previewDepthFrame = 0;
  let pendingDepthUpdate = null;

  const PREVIEW_PRESETS = {
    "elementary-01": { product: "햇살의 작은 우주", eyebrow: "나의 소개 카드", primaryLabel: "반가워! 내 별명은", primaryValue: "햇살", status: "개인정보 0개", message: "그림 그리기 · 우주 관찰 · 고양이 돌보기", metrics: [["취미", "3가지"], ["안전 점검", "완료"], ["수정", "1회"]], meter: 100, action: "카드 인사 보기", activeStatus: "친구 공개 준비 완료", activePrimary: "안녕!" },
    "elementary-02": { product: "우리 반, 같이 자라는 숲", eyebrow: "우리 반 소개 페이지", primaryLabel: "계획한 순서", primaryValue: "3단계", status: "내용 확인 완료", message: "반 이름 → 급훈 → 우리 반 자랑 3가지", metrics: [["소개 항목", "5개"], ["빠진 내용", "0개"], ["요청 기록", "3개"]], meter: 92, action: "페이지 흐름 보기", activeStatus: "발표 화면 준비 완료", activePrimary: "우리 반 최고!" },
    "elementary-03": { product: "오늘의 행운 뽑기", eyebrow: "긍정 운세 머신", primaryLabel: "오늘의 한마디", primaryValue: "좋은 일이 가까워요", status: "배려 문구만 사용", message: "버튼을 누를 때마다 다섯 가지 응원 중 하나가 나타나요.", metrics: [["응원 문구", "5개"], ["시험", "10회"], ["유해 표현", "0개"]], meter: 88, action: "운세 다시 뽑기", activeStatus: "새 운세를 뽑았어요", activePrimary: "용기 100점!" },
    "elementary-04": { product: "MODI 교통 신호등", eyebrow: "입력 → 프로그램 → 출력", primaryLabel: "현재 신호", primaryValue: "초록", status: "샘플 시뮬레이션", message: "보행자 버튼을 누르면 빨강·노랑·초록 순서로 LED가 바뀝니다.", metrics: [["입력", "버튼"], ["처리", "순서"], ["출력", "LED"]], meter: 74, action: "신호 바꾸기", activeStatus: "버튼 입력 감지", activePrimary: "노랑", input: "버튼 ON", logic: "색상 +1", output: "LED 초록" },
    "elementary-05": { product: "보물 지킴이", eyebrow: "거리 센서 경보기", primaryLabel: "안전 기준", primaryValue: "20 cm", status: "SAFE · 샘플", message: "손이 기준보다 가까워지면 빨간 LED와 경보음이 함께 켜집니다.", metrics: [["현재 거리", "42 cm"], ["조건", "20 cm 미만"], ["경보", "대기"]], meter: 68, action: "가까이 다가가기", activeStatus: "ALERT · 경보 작동", activePrimary: "14 cm", input: "거리 42cm", logic: "20cm 비교", output: "초록 LED" },
    "elementary-06": { product: "바람 3단 선풍기", eyebrow: "다이얼 속도 제어", primaryLabel: "다이얼 위치", primaryValue: "62%", status: "중풍 · 샘플", message: "다이얼을 오른쪽으로 돌릴수록 종이 날개가 더 빠르게 회전합니다.", metrics: [["바람", "중풍"], ["모터", "128 rpm"], ["안전", "확인"]], meter: 62, action: "강풍으로 돌리기", activeStatus: "강풍 · 출력 변경", activePrimary: "92%", input: "다이얼 62%", logic: "3단계 매핑", output: "모터 128rpm" },
    "elementary-07": { product: "STAR SCOUT 01", eyebrow: "별빛 행성 탐사차", primaryLabel: "크레이터 거리", primaryValue: "18 cm", status: "AUTO SAFE · 샘플", message: "탐사차가 빛나는 정원 길을 달리다가 크레이터 앞에서 스스로 멈춥니다.", metrics: [["탐사 상태", "안전 정지"], ["추진력", "0%"], ["임무 시험", "3/3"]], meter: 46, action: "탐사 장면 재생", activeStatus: "탐사 → 자동 정지", activePrimary: "SAFE", input: "거리 센서", logic: "18cm 안전 정지", output: "모터 OFF" },
    "elementary-08": { product: "MOMO BASE CONTROL", eyebrow: "기지 ↔ 탐사차", primaryLabel: "탐사차 연결", primaryValue: "연결됨", status: "양방향 샘플", message: "기지의 명령은 탐사차로, 센서의 거리 값은 기지 계기판으로 이동합니다.", metrics: [["거리", "64 cm"], ["추진력", "42%"], ["명령", "탐사 시작"]], meter: 72, action: "귀환 명령 보내기", activeStatus: "귀환 명령 전달 완료", activePrimary: "RETURN", input: "기지 버튼", logic: "명령 전송", output: "탐사차 제어" },
    "elementary-09": { product: "별빛 탐험 쇼케이스", eyebrow: "최종 탐사 브리핑", primaryLabel: "안전 미션 기록", primaryValue: "3/3", status: "발표 준비 완료", message: "2분 브리핑과 1분 탐사 시연으로 기능·구조·수정 과정을 보여 줍니다.", metrics: [["안전 점검", "100%"], ["브리핑", "2분"], ["탐사 시연", "1분"]], meter: 96, action: "미션 데모 시작", activeStatus: "LIVE MISSION · 샘플", activePrimary: "SUCCESS", input: "기지 리모컨", logic: "안전 조건", output: "탐사 시연" },
    "middle-01": { product: "우리 반 D-day", eyebrow: "요구사항 기반 알림 앱", primaryLabel: "과학 수행평가", primaryValue: "D-5", status: "수용 기준 통과", message: "마감일과 오늘 날짜를 계산해 남은 날짜를 카드로 표시합니다.", metrics: [["등록 일정", "4개"], ["임박 일정", "1개"], ["검증", "5/5"]], meter: 66, action: "임박 일정 확인", activeStatus: "D-3 강조 규칙 확인", activePrimary: "D-3" },
    "middle-02": { product: "Sprint 기록판", eyebrow: "변수와 상태", primaryLabel: "최고 기록", primaryValue: "5.00초", status: "기록 저장 완료", message: "5.20 · 5.00 · 5.40초를 저장하고 가장 짧은 기록을 비교합니다.", metrics: [["현재 기록", "3개"], ["평균", "5.20초"], ["상태", "STOP"]], meter: 84, action: "새 기록 측정", activeStatus: "RUNNING · 상태 변경", activePrimary: "00:03.24" },
    "middle-03": { product: "밸런스 투표 LAB", eyebrow: "디버깅 전후 비교", primaryLabel: "빠른 두 번 클릭", primaryValue: "1표", status: "버그 수정 완료", message: "첫 클릭 뒤 버튼을 잠가 중복 투표가 집계되지 않게 고쳤습니다.", metrics: [["수정 전", "2표"], ["수정 후", "1표"], ["회귀 시험", "통과"]], meter: 100, action: "중복 클릭 시험", activeStatus: "두 번째 클릭 차단", activePrimary: "PASS" },
    "middle-04": { product: "MOOD LIGHT", eyebrow: "밝기 기반 자동 조명", primaryLabel: "현재 밝기", primaryValue: "20 lx", status: "LED ON · 샘플", message: "세 장소의 측정값으로 임계값을 정해 어두운 곳에서만 켜집니다.", metrics: [["임계값", "35 lx"], ["LED", "파란색"], ["표본", "9개"]], meter: 35, action: "밝은 곳으로 이동", activeStatus: "LED OFF · 조건 변경", activePrimary: "48 lx", input: "밝기 20lx", logic: "35lx 비교", output: "LED ON" },
    "middle-05": { product: "SAFE DOOR", eyebrow: "거리·시간 제어", primaryLabel: "문 상태", primaryValue: "OPEN", status: "샘플 시뮬레이션", message: "22cm 이상이 3초 동안 유지된 뒤에만 문을 닫습니다.", metrics: [["현재 거리", "14 cm"], ["대기 시간", "3.0초"], ["모터", "열림"]], meter: 58, action: "통과 장면 재생", activeStatus: "3초 확인 후 닫힘", activePrimary: "CLOSED", input: "거리 14cm", logic: "거리+시간", output: "모터 OPEN" },
    "middle-06": { product: "MODI BEAT", eyebrow: "전자 드럼 키트", primaryLabel: "볼륨 다이얼", primaryValue: "25%", status: "4패드 준비", message: "버튼마다 다른 소리와 LED 색을 연결해 연속 입력까지 시험합니다.", metrics: [["Button 1", "북 · 파랑"], ["Button 2", "심벌 · 노랑"], ["BPM", "112"]], meter: 52, action: "샘플 비트 연주", activeStatus: "입력 2개 정상 처리", activePrimary: "BEAT!", input: "버튼 1·2", logic: "소리 매핑", output: "스피커+LED" },
    "middle-07": { product: "NOVA SENSOR STAGE", eyebrow: "관객 반응형 무대", primaryLabel: "공연 상태", primaryValue: "READY", status: "센서 리허설 · 샘플", message: "관객 거리에 따라 조명과 비트가 바뀌고 공연 상태 데이터가 생성됩니다.", metrics: [["audienceCm", "18"], ["lightLevel", "72"], ["sceneCount", "3"]], meter: 68, action: "리허설 장면 재생", activeStatus: "LIVE 장면 전환 완료", activePrimary: "LIVE", input: "거리·밝기 센서", logic: "상태 전이", output: "조명+비트" },
    "middle-08": { product: "NOVA LIVE CONSOLE", eyebrow: "실시간 공연 콘솔", primaryLabel: "관객 거리", primaryValue: "42 cm", status: "LIVE SYNC · 샘플", message: "무대 센서 데이터와 콘솔의 안전 정지 명령이 양방향으로 흐릅니다.", metrics: [["조명", "78%"], ["BPM", "112"], ["갱신", "0.2초 전"]], meter: 78, action: "장면 전환 시험", activeStatus: "FINALE 명령 확인", activePrimary: "FINALE", input: "무대 텔레메트리", logic: "라이브 콘솔", output: "장면 제어" },
    "middle-09": { product: "NOVA FESTIVAL SHOW", eyebrow: "인터랙티브 쇼케이스", primaryLabel: "검증 시나리오", primaryValue: "12/12", status: "공연 준비 완료", message: "센서 반응·LIVE 콘솔·테스트 기록이 하나의 기술 공연을 뒷받침합니다.", metrics: [["무대 구조", "완료"], ["라이브 공연", "준비"], ["질의응답", "1분"]], meter: 94, action: "쇼케이스 시작", activeStatus: "LIVE SHOW · 샘플", activePrimary: "ON AIR", input: "공연 큐", logic: "근거 연결", output: "라이브 무대" },
    "high-01": { product: "SPACE BOOKING", eyebrow: "예약 충돌 정책", primaryLabel: "16:30 요청 결과", primaryValue: "예약 거부", status: "정책 검증 완료", message: "기존 16:00~17:00 예약과 겹쳐 목록은 바꾸지 않고 수정 안내를 표시합니다.", metrics: [["기존 예약", "4개"], ["충돌", "1건"], ["데이터 변경", "0건"]], meter: 100, action: "경계 예약 시험", activeStatus: "17:00 시작은 예약 가능", activePrimary: "예약 승인" },
    "high-02": { product: "MEAL SIGNAL", eyebrow: "급식 리뷰 데이터", primaryLabel: "카레라이스 평균", primaryValue: "4.0", status: "모델 일치", message: "리뷰 2개를 평균 내고 별점순 화면과 원본 저장 순서를 분리합니다.", metrics: [["리뷰", "2개"], ["최고 별점", "5.0"], ["유효성", "통과"]], meter: 80, action: "정렬 방식 바꾸기", activeStatus: "별점순 보기", activePrimary: "5점 먼저" },
    "high-03": { product: "FOCUS 25", eyebrow: "사용성 개선 리포트", primaryLabel: "시작 버튼 탐색", primaryValue: "5초", status: "개선안 검증", message: "작은 아이콘을 큰 ‘집중 시작’ 버튼으로 바꾸고 같은 과제로 다시 측정했습니다.", metrics: [["수정 전", "12초"], ["수정 후", "5초"], ["오조작", "0회"]], meter: 72, action: "수정 전후 비교", activeStatus: "완료 시간 58% 단축", activePrimary: "-7초" },
    "high-04": { product: "ENTRY COUNTER", eyebrow: "센서 데이터 전처리", primaryLabel: "정제된 통과", primaryValue: "1명", status: "샘플 시뮬레이션", message: "110→18→17→20→112cm 연속 값은 한 번의 통과로만 집계됩니다.", metrics: [["정확도", "98.6%"], ["중복 제거", "3건"], ["누적", "184명"]], meter: 86, action: "통과 데이터 재생", activeStatus: "쿨다운 적용 완료", activePrimary: "+1", input: "거리 시계열", logic: "진입·이탈", output: "카운트 +1" },
    "high-05": { product: "THERMO CONTROL", eyebrow: "히스테리시스 제어", primaryLabel: "현재 온도", primaryValue: "29.1°C", status: "FAN ON · 샘플", message: "켜짐·꺼짐 경계를 나눠 임계값 주변에서 팬이 떨리는 현상을 줄입니다.", metrics: [["ON 경계", "29.0°C"], ["OFF 경계", "27.0°C"], ["전환", "2회"]], meter: 71, action: "온도 흐름 재생", activeStatus: "27.0°C에서 FAN OFF", activePrimary: "OFF", input: "온도 29.1°C", logic: "이전 상태 유지", output: "팬 ON" },
    "high-06": { product: "REFLEX TEST", eyebrow: "경계·예외 테스트", primaryLabel: "최고 반응속도", primaryValue: "0.211초", status: "8/8 TEST PASS", message: "신호 전에 누르면 반칙 안내를 띄우고 기존 기록은 바꾸지 않습니다.", metrics: [["이번 기록", "0.238초"], ["조기 입력", "차단"], ["회귀", "통과"]], meter: 93, action: "조기 입력 시험", activeStatus: "반칙 감지 · 기록 유지", activePrimary: "INVALID", input: "버튼 입력", logic: "상태 검증", output: "기록 보존" },
    "high-07": { product: "ORBIT-9 DOCKING ARCH", eyebrow: "3계층 도킹 아키텍처", primaryLabel: "도킹 상태", primaryValue: "APPROACH", status: "궤도 텔레메트리 · 샘플", message: "우주선→네트워크→관제실로 데이터가 흐르고 명령은 반대 방향으로 돌아옵니다.", metrics: [["도킹 거리", "28 cm"], ["계층", "3/3"], ["중단 횟수", "1회"]], meter: 52, action: "도킹 상태 전이 재생", activeStatus: "CRUISE→APPROACH→HOLD", activePrimary: "HOLD", input: "distanceCm", logic: "도킹 상태 머신", output: "추력 제어" },
    "high-08": { product: "ORBIT-9 MISSION OPS", eyebrow: "심우주 텔레메트리 관제", primaryLabel: "왕복 반응 시간", primaryValue: "74 ms", status: "LIVE · E2E 샘플", message: "우주선 상태·이벤트 로그·E-Stop 명령을 한 화면에서 종단 간 검증합니다.", metrics: [["패킷", "1,284"], ["누락", "0"], ["계층 진단", "정상"]], meter: 78, action: "E-Stop 왕복 시험", activeStatus: "명령 확인 · 도킹 중단", activePrimary: "E-STOP ACTIVE", input: "텔레메트리", logic: "E2E 검증", output: "E-STOP" },
    "high-09": { product: "ORBIT-9 INCIDENT LAB", eyebrow: "장애 대응 데모데이", primaryLabel: "요구사항 추적", primaryValue: "12/12", status: "MISSION READY", message: "실패 화면·수신 로그·원인 가설·복구 검증을 하나의 미션 사례로 연결합니다.", metrics: [["테스트 커버리지", "94%"], ["증거", "18개"], ["브리핑", "5분"]], meter: 94, action: "비상 대응 사례 보기", activeStatus: "원인 계층 식별 완료", activePrimary: "RECOVERED", input: "장애 증거", logic: "계층 진단", output: "복구 결과" }
  };

  const LESSON_SCENE_PROFILES = {
    "elementary-01": { kind: "profile", code: "SAFE-ID 01", title: "개인정보 방패", subtitle: "별명과 취미만 안전하게 공개", tokens: ["별명 햇살", "취미 3", "개인정보 0"], route: 0 },
    "elementary-02": { kind: "ordered-page", code: "CLASS PAGE 02", title: "우리 반 이야기 숲", subtitle: "내용을 읽기 좋은 순서로 배치", tokens: ["반 이름", "급훈", "자랑 3"], route: 1 },
    "elementary-03": { kind: "random-wheel", code: "LUCK LAB 03", title: "응원 캡슐 머신", subtitle: "무작위 결과를 여러 번 관찰", tokens: ["문구 5", "실험 10", "유해 0"], route: 2 },
    "elementary-04": { kind: "signal-chain", code: "SIGNAL 04", title: "보행자 신호 회로", subtitle: "입력에서 출력까지 신호 여행", tokens: ["BUTTON", "NETWORK", "LED"], route: 3 },
    "elementary-05": { kind: "distance-alarm", code: "VAULT 05", title: "보물 경계 구역", subtitle: "거리 기준으로 경보를 판단", tokens: ["42 cm", "20 cm", "ALERT"], route: 4 },
    "elementary-06": { kind: "dial-fan", code: "WIND 06", title: "바람 조절 링", subtitle: "다이얼 값을 세 단계 바람으로 변환", tokens: ["62%", "중풍", "128 rpm"], route: 5 },
    "elementary-07": { kind: "rover-zone", code: "SCOUT 07", title: "크레이터 탐사차", subtitle: "센서가 보는 안전 정지 구역", tokens: ["SENSE", "STOP", "SAFE"], route: 0 },
    "elementary-08": { kind: "remote-link", code: "BASE 08", title: "기지 원격 관제", subtitle: "명령과 거리 값이 양방향 이동", tokens: ["COMMAND", "64 cm", "RETURN"], route: 1 },
    "elementary-09": { kind: "showcase", code: "LIVE 09", title: "별빛 탐험 브리핑", subtitle: "기능·구조·수정을 무대에서 설명", tokens: ["2분", "1분", "3/3"], route: 2 },
    "middle-01": { kind: "requirements-trace", code: "REQ 01", title: "D-day 요구사항 추적", subtitle: "사용자 문제부터 수용 기준까지", tokens: ["사용자", "요구사항", "D-5"], route: 3 },
    "middle-02": { kind: "stopwatch-state", code: "SPRINT 02", title: "스톱워치 상태 머신", subtitle: "이벤트에 따라 RUN·STOP 전환", tokens: ["READY", "RUN", "LAP"], route: 4 },
    "middle-03": { kind: "debug-trace", code: "DEBUG 03", title: "중복 투표 추적기", subtitle: "재현·수정·회귀 시험을 한 흐름으로", tokens: ["2표", "FIX", "PASS"], route: 5 },
    "middle-04": { kind: "lux-threshold", code: "MOOD 04", title: "빛 임계값 지도", subtitle: "측정값과 LED 조건을 비교", tokens: ["20 lx", "35 lx", "LED ON"], route: 0 },
    "middle-05": { kind: "auto-door", code: "DOOR 05", title: "안전 자동문 상태", subtitle: "거리와 시간으로 OPEN·WAIT·CLOSE", tokens: ["OPEN", "3 sec", "CLOSED"], route: 1 },
    "middle-06": { kind: "rhythm-wave", code: "BEAT 06", title: "전자 드럼 시퀀서", subtitle: "버튼·소리·빛을 박자에 맞춰 연결", tokens: ["PAD", "BEAT", "LIGHT"], route: 2 },
    "middle-07": { kind: "sensor-stage", code: "NOVA 07", title: "NOVA 센서 무대", subtitle: "관객 움직임으로 장면을 전환", tokens: ["SENSOR", "SCENE", "LIVE"], route: 3 },
    "middle-08": { kind: "telemetry-console", code: "NOVA OPS 08", title: "무대 관제 콘솔", subtitle: "텔레메트리와 E-Stop을 양방향 검증", tokens: ["DATA", "STALE", "E-STOP"], route: 4 },
    "middle-09": { kind: "festival", code: "NOVA LIVE 09", title: "NOVA 메이커 페스티벌", subtitle: "근거가 보이는 팀별 라이브 쇼", tokens: ["BOOTH", "DEMO", "FEEDBACK"], route: 5 },
    "high-01": { kind: "interval-collision", code: "BOOKING 01", title: "예약 충돌 관제", subtitle: "시간 구간 겹침과 정책을 판정", tokens: ["09:00", "OVERLAP", "REJECT"], route: 0 },
    "high-02": { kind: "er-aggregate", code: "MEAL 02", title: "리뷰 데이터 파이프라인", subtitle: "메뉴·리뷰 관계와 평균 집계", tokens: ["MENU", "1:N", "AVG 4.0"], route: 1 },
    "high-03": { kind: "journey-priority", code: "FOCUS 03", title: "사용자 여정 레이더", subtitle: "관찰한 불편을 개선 우선순위로", tokens: ["12초", "PAIN", "5초"], route: 2 },
    "high-04": { kind: "timeseries-entry", code: "ENTRY 04", title: "통과 이벤트 추출", subtitle: "거리 시계열에서 한 번의 통과만 집계", tokens: ["110", "18·17·20", "+1"], route: 3 },
    "high-05": { kind: "hysteresis-band", code: "THERMO 05", title: "히스테리시스 제어 밴드", subtitle: "켜짐·꺼짐 경계를 나눠 상태 안정화", tokens: ["29°C ON", "HOLD", "27°C OFF"], route: 4 },
    "high-06": { kind: "reaction-state", code: "REFLEX 06", title: "반응속도 상태·파형", subtitle: "조기 입력과 정상 입력을 분리 검증", tokens: ["WAIT", "READY", "0.211s"], route: 5 },
    "high-07": { kind: "orbit-architecture", code: "ORBIT-9 07", title: "3계층 도킹 아키텍처", subtitle: "우주선·통신·관제 계층을 분리", tokens: ["SHIP", "NETWORK", "OPS"], route: 0 },
    "high-08": { kind: "mission-ops", code: "ORBIT-9 08", title: "심우주 미션 관제", subtitle: "명령·텔레메트리·로그를 종단 간 추적", tokens: ["74 ms", "1,284 pkt", "E-STOP"], route: 1 },
    "high-09": { kind: "traceability-incident", code: "ORBIT-9 09", title: "장애 대응 증거망", subtitle: "요구사항부터 복구 증거까지 연결", tokens: ["REQ", "INCIDENT", "RECOVERED"], route: 2 }
  };

  const SLIDE_VISUAL_META = {
    title: { label: "MISSION BRIEF", layout: "cinema" },
    goals: { label: "GOAL MAP", layout: "mission-map" },
    hook: { label: "WHY SCENE", layout: "contrast-stage" },
    vocabulary: { label: "CONCEPT ATLAS", layout: "concept-atlas" },
    concept: { label: "HOW IT WORKS", layout: "mechanism-board" },
    example: { label: "WORKED EXAMPLE", layout: "story-filmstrip" },
    check: { label: "CONCEPT CHECK", layout: "quiz-orbit" },
    setup: { label: "BUILD BENCH", layout: "topdown-bench" },
    plan: { label: "PROJECT ROUTE", layout: "route-map" },
    build: { label: "MAKER STEP", layout: "maker-blueprint" },
    checkpoint: { label: "EVIDENCE CHECK", layout: "evidence-pinboard" },
    troubleshoot: { label: "DIAGNOSTIC TREE", layout: "diagnostic-tree" },
    differentiate: { label: "LEVEL ROUTES", layout: "branch-lanes" },
    rubric: { label: "EVIDENCE MATRIX", layout: "rubric-heatmap" },
    exit: { label: "MISSION COMPLETE", layout: "completion-orbit" }
  };

  const MODULE_VISUAL_MATCHES = [
    { words: ["다이얼"], file: "modi-dial.png", label: "Dial" },
    { words: ["스피커", "소리", "경보음"], file: "modi-speaker.png", label: "Speaker" },
    { words: ["LED", "조명", "신호등", "빛"], file: "modi-led.png", label: "LED" },
    { words: ["Display", "디스플레이", "화면"], file: "modi-display.png", label: "Display" },
    { words: ["Network", "네트워크", "통신", "전송"], file: "modi-network.png", label: "Network" },
    { words: ["배터리", "전원"], file: "modi-battery.png", label: "Battery" }
  ];

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
      '<div class="rubric-wrap"><table class="rubric-table"><caption class="sr-only">평가 기준별 기초·도달·심화 수준</caption><thead><tr><th scope="col">평가 기준</th><th scope="col">기초</th><th scope="col">도달</th><th scope="col">심화</th></tr></thead><tbody>',
      values.map((row) => [
        '<tr><th scope="row">', escapeHtml(row.criterion), '</th><td data-label="기초">', escapeHtml(row.basic), '</td><td data-label="도달">',
        escapeHtml(row.proficient), '</td><td data-label="심화">', escapeHtml(row.advanced), "</td></tr>"
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

  function shortenVisualText(value, limit) {
    const text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
    const max = Number(limit) || 54;
    return text.length > max ? text.slice(0, Math.max(1, max - 1)).trim() + "…" : text;
  }

  function visualValue(value) {
    if (typeof value === "string" || typeof value === "number") {
      return String(value);
    }
    if (!value || typeof value !== "object") {
      return "";
    }
    return String(value.term || value.criterion || value.symptom || value.fix || value.title || value.label || "");
  }

  function resolveSlideModule(slide, lesson) {
    const slideSource = [slide.title, slide.question, asList(slide.body).join(" "), asList(slide.instructions).join(" ")].join(" ").toLowerCase();
    const directMatch = MODULE_VISUAL_MATCHES.find((candidate) => candidate.words.some((word) => slideSource.includes(word.toLowerCase())));
    if (directMatch) {
      return directMatch;
    }
    const materialSource = asList(lesson.materials).join(" ").toLowerCase();
    return MODULE_VISUAL_MATCHES.find((candidate) => candidate.words.some((word) => materialSource.includes(word.toLowerCase()))) || null;
  }

  function resolveLessonVisualAsset(levelId, lesson, slide) {
    const module = resolveSlideModule(slide, lesson);
    if (lesson.projectType === "web") {
      return {
        file: "/static/assets/brand/" + getLevelMeta(levelId).thumbnail,
        alt: getLevelMeta(levelId).difficulty + " Web 프로젝트 미리보기",
        kind: "web"
      };
    }
    if (module && ["build", "concept", "example"].includes(slide.type)) {
      return {
        file: "/static/assets/lesson-visuals/" + module.file,
        alt: "MODI " + module.label + " 모듈",
        kind: "module",
        moduleLabel: module.label
      };
    }
    if (lesson.projectType === "hw") {
      let file = "modi-kit-flatlay.jpg";
      if ((levelId === "middle" && lesson.no === 4) || (levelId === "high" && lesson.no === 5)) {
        file = "modi-smart-farm.jpg";
      } else if ((levelId === "middle" && lesson.no === 5) || (levelId === "high" && lesson.no === 4)) {
        file = "modi-car-robot.jpg";
      } else if (slide.type === "title" || slide.type === "hook") {
        file = "modi-ecosystem.jpg";
      }
      return { file: "/static/assets/lesson-visuals/" + file, alt: "MODI 프로젝트 제품 구성", kind: "photo" };
    }
    if (levelId === "elementary") {
      return { file: "/static/assets/lesson-visuals/modi-car-robot.jpg", alt: "MODI 탐사차 프로젝트 장면", kind: "photo" };
    }
    if (levelId === "middle") {
      return { file: "/static/assets/lesson-visuals/modi-control-workspace.jpg", alt: "웹 화면과 MODI 장치를 함께 제어하는 장면", kind: "photo" };
    }
    return { file: "/static/assets/lesson-visuals/web-modi-hybrid.png", alt: "웹 관제 화면과 장치 사이의 양방향 데이터 흐름", kind: "cutout" };
  }

  function getLessonSceneProfile(levelId, lesson) {
    return LESSON_SCENE_PROFILES[lessonKey(levelId, lesson.no)] || {
      kind: "showcase",
      code: "MODI LAB",
      title: lesson.title,
      subtitle: lesson.summary,
      tokens: ["INPUT", "LOGIC", "OUTPUT"],
      route: 0
    };
  }

  function getVisualTextList(values, limit, fallback) {
    const list = asList(values).map((value) => shortenVisualText(visualValue(value), 58)).filter(Boolean);
    if (list.length) {
      return list.slice(0, Number(limit) || 3);
    }
    return asList(fallback).slice(0, Number(limit) || 3);
  }

  function renderSceneTokenRail(profile) {
    return '<div class="scene-token-rail">' + asList(profile.tokens).slice(0, 3).map((token, index) => (
      '<span><i>' + String(index + 1).padStart(2, "0") + '</i><b>' + escapeHtml(token) + '</b></span>'
    )).join("") + "</div>";
  }

  function renderLessonScene(ctx, variant) {
    const profile = ctx.profile;
    const tokens = asList(profile.tokens);
    const t0 = escapeHtml(tokens[0] || "INPUT");
    const t1 = escapeHtml(tokens[1] || "LOGIC");
    const t2 = escapeHtml(tokens[2] || "OUTPUT");
    let scene = "";

    if (profile.kind === "profile") {
      scene = '<div class="scene-profile-card"><span class="scene-avatar">해</span><b>햇살의 카드</b><small>그림 · 우주 · 고양이</small></div><div class="scene-privacy-shield"><i>0</i><b>개인정보</b></div>';
    } else if (profile.kind === "ordered-page") {
      scene = '<div class="scene-page-stack"><span><i>01</i>반 이름</span><span><i>02</i>급훈</span><span><i>03</i>우리 반 자랑</span></div><div class="scene-scroll-line"></div>';
    } else if (profile.kind === "random-wheel") {
      scene = '<div class="scene-fortune-wheel"><i></i><i></i><i></i><i></i><b>행운</b></div><div class="scene-trial-dots">' + Array.from({ length: 10 }, (_value, index) => '<i class="' + (index % 3 === 0 ? "hot" : "") + '"></i>').join("") + '</div>';
    } else if (profile.kind === "signal-chain") {
      scene = '<div class="scene-circuit"><span class="input">BUTTON</span><i></i><span class="logic">NETWORK</span><i></i><span class="output"><b></b><b></b><b></b>LED</span></div>';
    } else if (profile.kind === "distance-alarm") {
      scene = '<div class="scene-sensor"><i></i><i></i><i></i><b>ToF</b></div><div class="scene-distance-ruler"><span>0</span><b>20cm</b><span>42cm</span></div><div class="scene-vault">보물</div>';
    } else if (profile.kind === "dial-fan") {
      scene = '<div class="scene-dial"><i></i><b>62%</b></div><div class="scene-fan"><i></i><i></i><i></i><span></span></div><div class="scene-wind-lines"><i></i><i></i><i></i></div>';
    } else if (profile.kind === "rover-zone") {
      scene = '<div class="scene-crater"></div><div class="scene-rover"><b>M</b><i></i><i></i></div><div class="scene-sensor-cone"><i></i></div><span class="scene-stop-mark">SAFE STOP</span>';
    } else if (profile.kind === "remote-link") {
      scene = '<div class="scene-base-console"><b>BASE</b><span>↑</span><span>←</span><span>→</span></div><div class="scene-data-lanes"><i>COMMAND →</i><i>← 64cm</i></div><div class="scene-mini-rover"><b>M</b><i></i><i></i></div>';
    } else if (profile.kind === "showcase") {
      scene = '<div class="scene-stage"><div class="scene-stage-light left"></div><div class="scene-stage-light right"></div><b>STAR SCOUT</b><span>LIVE MISSION</span></div><div class="scene-run-sheet"><i>2:00</i><i>1:00</i><i>3/3</i></div>';
    } else if (profile.kind === "requirements-trace") {
      scene = '<div class="scene-trace"><span class="user">사용자</span><i></i><span class="requirement">요구사항</span><i></i><span class="screen">D-5</span></div><div class="scene-acceptance"><b>✓</b> 수용 기준 5/5</div>';
    } else if (profile.kind === "stopwatch-state") {
      scene = '<div class="scene-stopwatch"><b>00:05.20</b><span>LAP 03</span></div><div class="scene-state-loop"><i>READY</i><b>→</b><i>RUN</i><b>→</b><i>STOP</i></div>';
    } else if (profile.kind === "debug-trace") {
      scene = '<div class="scene-debug-before"><small>BEFORE</small><b>CLICK×2</b><span>2표</span></div><div class="scene-patch">LOCK</div><div class="scene-debug-after"><small>AFTER</small><b>CLICK×2</b><span>1표 · PASS</span></div>';
    } else if (profile.kind === "lux-threshold") {
      scene = '<div class="scene-lux-plot"><i style="--x:12%;--y:74%"></i><i style="--x:32%;--y:58%"></i><i style="--x:58%;--y:32%"></i><i style="--x:80%;--y:18%"></i><b>35 lx 기준</b></div><div class="scene-lamp"><i></i><span>LED ON</span></div>';
    } else if (profile.kind === "auto-door") {
      scene = '<div class="scene-door"><i></i><i></i><span>14cm</span></div><div class="scene-door-states"><b>OPEN</b><i>3s</i><b>WAIT</b><i>→</i><b>CLOSED</b></div>';
    } else if (profile.kind === "rhythm-wave") {
      scene = '<div class="scene-pads">' + ["KICK", "SNARE", "HAT", "FX"].map((label, index) => '<i class="p' + index + '">' + label + '</i>').join("") + '</div><div class="scene-wave">' + Array.from({ length: 12 }, (_value, index) => '<i style="--h:' + (18 + (index * 17) % 72) + '%"></i>').join("") + '</div>';
    } else if (profile.kind === "sensor-stage") {
      scene = '<div class="scene-rig"><i></i><i></i><i></i><span class="beam one"></span><span class="beam two"></span><b>NOVA LIVE</b></div><div class="scene-audience"><i></i><i></i><i></i></div>';
    } else if (profile.kind === "telemetry-console") {
      scene = '<div class="scene-console-screen"><b>LIVE</b><div>' + Array.from({ length: 8 }, (_value, index) => '<i style="--h:' + (24 + (index * 23) % 68) + '%"></i>').join("") + '</div><span>74 ms</span></div><span class="scene-estop">E-STOP</span>';
    } else if (profile.kind === "festival") {
      scene = '<div class="scene-festival-booths"><span>A<small>센서 무대</small></span><span>B<small>관제 콘솔</small></span><span>C<small>증거 전시</small></span></div><div class="scene-crowd">' + Array.from({ length: 7 }, () => "<i></i>").join("") + '</div>';
    } else if (profile.kind === "interval-collision") {
      scene = '<div class="scene-calendar"><span>09</span><span>10</span><span>11</span><i class="booking one">A</i><i class="booking two">B</i><b>OVERLAP</b></div>';
    } else if (profile.kind === "er-aggregate") {
      scene = '<div class="scene-erd"><span><b>MENU</b><i>menuId</i></span><em>1 : N</em><span><b>REVIEW</b><i>rating</i></span></div><div class="scene-aggregate">Σ ÷ n <b>4.0</b></div>';
    } else if (profile.kind === "journey-priority") {
      scene = '<div class="scene-journey"><span>발견</span><i class="pain">12s</i><span>시작</span><i>→</i><span>완료</span><b>5s</b></div><div class="scene-priority"><i>효과 ↑</i><b>NOW</b></div>';
    } else if (profile.kind === "timeseries-entry") {
      scene = '<div class="scene-timeseries"><span>110</span><i style="--x:8%;--y:18%"></i><i style="--x:28%;--y:72%"></i><i style="--x:45%;--y:76%"></i><i style="--x:62%;--y:70%"></i><i style="--x:84%;--y:16%"></i><b>ENTRY +1</b></div>';
    } else if (profile.kind === "hysteresis-band") {
      scene = '<div class="scene-hysteresis"><span class="on">29°C · FAN ON</span><div><i></i><b>상태 유지 구간</b></div><span class="off">27°C · FAN OFF</span></div>';
    } else if (profile.kind === "reaction-state") {
      scene = '<div class="scene-reaction-lights"><i></i><i></i><i class="ready"></i></div><div class="scene-reaction-wave"><span>WAIT</span><i></i><span>READY</span><b>0.211s</b></div>';
    } else if (profile.kind === "orbit-architecture") {
      scene = '<div class="scene-orbit"><span class="ship">SHIP</span><i class="orbit-ring one"></i><span class="network">NETWORK</span><i class="orbit-ring two"></i><span class="ops">OPS</span></div>';
    } else if (profile.kind === "mission-ops") {
      scene = '<div class="scene-spacecraft"><i></i><b>ORBIT-9</b></div><div class="scene-uplink"><span>COMMAND ↑</span><span>↓ TELEMETRY</span></div><div class="scene-mission-panels"><i>74ms</i><i>1,284pkt</i><i>E2E OK</i></div>';
    } else {
      scene = '<div class="scene-incident-chain"><span>REQ</span><i></i><span>FAIL</span><i></i><span>LOG</span><i></i><span>FIX</span><i></i><span>PASS</span></div><div class="scene-recovered">RECOVERED</div>';
    }

    return [
      '<div class="lesson-scene scene--', escapeHtml(profile.kind), ' scene-variant--', escapeHtml(variant || "standard"), '" role="img" aria-label="',
      escapeHtml(profile.title + " — " + profile.subtitle), '"><div class="lesson-scene-art">', scene, '</div>',
      variant === "compact" ? "" : renderSceneTokenRail(profile),
      '<span class="sr-only">', t0, " → ", t1, " → ", t2, "</span></div>"
    ].join("");
  }

  function createSlideVisualContext(levelId, lesson, slide, slideIndex) {
    const profile = getLessonSceneProfile(levelId, lesson);
    return {
      levelId,
      lesson,
      slide,
      slideIndex,
      profile,
      preset: PREVIEW_PRESETS[lessonKey(levelId, lesson.no)] || {},
      asset: resolveLessonVisualAsset(levelId, lesson, slide),
      meta: SLIDE_VISUAL_META[slide.type] || { label: "VISUAL NOTE", layout: "mechanism-board" }
    };
  }

  function renderVisualShell(ctx, body, options) {
    const settings = options || {};
    const layout = settings.layout || ctx.meta.layout;
    const buildKind = settings.buildKind || "";
    const layoutFingerprint = [layout, ctx.profile.kind, buildKind].filter(Boolean).join(":");
    return [
      '<figure class="lesson-slide-visual visual-layout--', escapeHtml(layout), " mode-", escapeHtml(ctx.lesson.projectType), " level-", escapeHtml(ctx.levelId),
      '" data-layout="', escapeHtml(layoutFingerprint), '" data-scene="', escapeHtml(ctx.profile.kind), '" data-scene-kind="', escapeHtml(ctx.profile.kind),
      '" data-lesson-key="', escapeHtml(lessonKey(ctx.levelId, ctx.lesson.no)), '" data-slide-type="', escapeHtml(ctx.slide.type), '" data-visual-level="', escapeHtml(ctx.levelId),
      '" data-visual-lesson="', String(ctx.lesson.no), '" data-visual-slide="', String(Number(ctx.slideIndex) + 1), '"',
      buildKind ? ' data-build-kind="' + escapeHtml(buildKind) + '"' : "", ">",
      '<div class="lesson-visual-topline"><span>', escapeHtml(ctx.meta.label), '</span><b>', escapeHtml(ctx.profile.code), '</b><small>', escapeHtml(ctx.profile.title), "</small></div>",
      '<div class="lesson-visual-stage" data-visual-body>', body, "</div>",
      '<figcaption><span>', escapeHtml(ctx.profile.code), '</span><b>', escapeHtml(shortenVisualText(ctx.slide.title, 72)),
      '</b><small>', escapeHtml(MODES[ctx.lesson.projectType].long), " · ", escapeHtml(WORLD_PROFILES[ctx.levelId].name), "</small></figcaption></figure>"
    ].join("");
  }

  function renderAtmosphereAsset(ctx, className) {
    return '<img class="' + escapeHtml(className || "visual-atmosphere-image") + '" src="' + escapeHtml(ctx.asset.file) + '" alt="' + escapeHtml(ctx.asset.alt) + '">';
  }

  function renderTitleVisual(ctx) {
    const artifacts = getVisualTextList(ctx.lesson.studentArtifacts, 2, ["완성 작품", "제작 근거"]);
    return [
      '<section class="visual-cinema"><div class="visual-cinema-backdrop">', renderAtmosphereAsset(ctx), '<div class="visual-cinema-shade"></div></div>',
      '<div class="visual-cinema-copy"><small>', escapeHtml(ctx.profile.code), '</small><strong>', escapeHtml(ctx.profile.title), '</strong><p>', escapeHtml(ctx.profile.subtitle), "</p></div>",
      '<div class="visual-cinema-scene">', renderLessonScene(ctx, "hero"), "</div>",
      '<div class="visual-result-plaque"><span>FINAL ARTIFACT</span>', artifacts.map((item, index) => '<b><i>' + String(index + 1).padStart(2, "0") + "</i>" + escapeHtml(item) + "</b>").join(""), "</div></section>"
    ].join("");
  }

  function renderGoalsVisual(ctx) {
    const objectives = getVisualTextList(ctx.slide.objectives, 3, getVisualTextList(ctx.slide.body, 3, ["문제 이해", "작품 제작", "근거 설명"]));
    const success = getVisualTextList(ctx.slide.successCriteria || ctx.lesson.successCriteria, 1, ["완성 결과를 근거로 설명"]);
    return [
      '<section class="visual-goal-map visual-mission-map goal-route--', String(ctx.profile.route || 0), '"><div class="goal-map-watermark">', renderLessonScene(ctx, "compact"), "</div>",
      '<div class="goal-map-origin"><small>START</small><b>', escapeHtml(ctx.profile.code), "</b></div><ol>",
      objectives.map((item, index) => '<li style="--goal-index:' + index + '"><span>' + String(index + 1).padStart(2, "0") + "</span><b>" + escapeHtml(item) + "</b></li>").join(""),
      '</ol><div class="goal-success-ring"><span>SUCCESS</span><b>', escapeHtml(success[0]), "</b><i></i></div></section>"
    ].join("");
  }

  function renderHookVisual(ctx) {
    const body = getVisualTextList(ctx.slide.body, 2, [ctx.profile.subtitle, "무엇이 달라져야 할까요?"]);
    const before = ctx.profile.tokens[0] || "현재";
    const after = ctx.profile.tokens[2] || "개선";
    return [
      '<section class="visual-contrast-stage"><article class="contrast-panel contrast-before"><small>BEFORE · 문제 장면</small><b>', escapeHtml(before),
      '</b><p>', escapeHtml(body[0]), '</p><div class="contrast-scene">', renderLessonScene(ctx, "compact"), "</div></article>",
      '<div class="contrast-lens"><span>WHY?</span><i></i></div><article class="contrast-panel contrast-after"><small>AFTER · 바라는 장면</small><b>',
      escapeHtml(after), '</b><p>', escapeHtml(body[1] || ctx.profile.subtitle), '</p><div class="contrast-proof"><i>01</i><i>02</i><i>03</i></div></article></section>'
    ].join("");
  }

  function renderVocabularyVisual(ctx) {
    const terms = asList(ctx.slide.terms).slice(0, 3);
    const fallback = asList(ctx.profile.tokens).map((token) => ({ term: token, definition: ctx.profile.subtitle }));
    const items = terms.length ? terms : fallback;
    return [
      '<section class="visual-concept-atlas"><div class="atlas-index"><small>FIELD GUIDE</small><b>', escapeHtml(ctx.profile.title), '</b>',
      renderLessonScene(ctx, "compact"), '</div><div class="atlas-entries">',
      items.map((term, index) => '<article class="atlas-entry atlas-entry--' + (index + 1) + '"><span>' + String(index + 1).padStart(2, "0") + '</span><div class="atlas-glyph"><i></i><i></i><i></i></div><b>' + escapeHtml(term.term || visualValue(term)) + '</b><p>' + escapeHtml(shortenVisualText(term.definition || asList(ctx.slide.body)[index] || ctx.profile.subtitle, 76)) + "</p></article>").join(""),
      "</div></section>"
    ].join("");
  }

  function renderConceptVisual(ctx) {
    const notes = getVisualTextList(ctx.slide.body, 4, asList(ctx.profile.tokens));
    return [
      '<section class="visual-mechanism-board"><div class="mechanism-grid"></div><div class="mechanism-scene">', renderLessonScene(ctx, "mechanism"), "</div>",
      '<div class="mechanism-notes">', notes.map((note, index) => '<span class="note-' + (index + 1) + '"><i>' + String(index + 1).padStart(2, "0") + "</i><b>" + escapeHtml(note) + "</b></span>").join(""),
      '</div><div class="mechanism-legend"><small>MODEL</small><b>', escapeHtml(ctx.profile.subtitle), "</b></div></section>"
    ].join("");
  }

  function renderExampleVisual(ctx) {
    const input = getVisualTextList(ctx.slide.input, 2, [ctx.profile.tokens[0] || "입력"]);
    const process = getVisualTextList(ctx.slide.process, 2, [ctx.profile.tokens[1] || "규칙"]);
    const output = getVisualTextList(ctx.slide.output, 2, [ctx.profile.tokens[2] || "결과"]);
    const frames = [
      { label: "01 · INPUT", values: input },
      { label: "02 · PROCESS", values: process },
      { label: "03 · OUTPUT", values: output }
    ];
    return [
      '<section class="visual-story-filmstrip"><div class="film-perforation top"></div><div class="story-frames">',
      frames.map((frame, index) => '<article class="story-frame frame-' + (index + 1) + '"><small>' + frame.label + '</small>' + (index === 1 ? renderLessonScene(ctx, "compact") : '<div class="story-number">' + escapeHtml(ctx.profile.tokens[index] || String(index + 1)) + '</div>') + '<p>' + frame.values.map((value) => '<b>' + escapeHtml(value) + '</b>').join("") + "</p></article>").join(""),
      '</div><div class="film-compare"><span>SCENARIO</span><b>', escapeHtml(shortenVisualText(ctx.slide.scenario || ctx.slide.compare || ctx.profile.subtitle, 90)), '</b></div><div class="film-perforation bottom"></div></section>'
    ].join("");
  }

  function renderCheckVisual(ctx) {
    const choices = asList(ctx.slide.choices).slice(0, 4);
    const safeChoices = choices.length ? choices : ["입력을 먼저 확인한다", "규칙을 다시 읽는다", "결과를 근거로 비교한다"];
    return [
      '<section class="visual-quiz-orbit"><div class="quiz-radar"><i class="ring-one"></i><i class="ring-two"></i><i class="sweep"></i><div class="quiz-core"><small>CHECK</small><b>',
      escapeHtml(shortenVisualText(ctx.slide.question || ctx.slide.title, 92)), '</b></div>',
      safeChoices.map((choice, index) => '<div class="quiz-choice choice-' + (index + 1) + '"><span>' + String.fromCharCode(65 + index) + "</span><b>" + escapeHtml(shortenVisualText(choice, 48)) + "</b></div>").join(""),
      '</div><p class="quiz-no-spoiler"><i>?</i> 먼저 근거를 찾고 실제 선택은 아래 활동에서 확인하세요.</p></section>'
    ].join("");
  }

  function renderSetupVisual(ctx) {
    const checklist = getVisualTextList(ctx.slide.checklist, 4, ["재료 확인", "연결 확인", "안전 확인"]);
    return [
      '<section class="visual-topdown-bench"><div class="bench-surface"><div class="bench-photo">', renderAtmosphereAsset(ctx, "bench-product-image"),
      ctx.asset.kind === "module" ? '<span class="bench-module-label">MODI · ' + escapeHtml(ctx.asset.moduleLabel) + "</span>" : "", '</div><div class="bench-scene-card">', renderLessonScene(ctx, "compact"),
      '</div><div class="bench-tape tape-one"></div><div class="bench-tape tape-two"></div></div><div class="bench-checklist"><small>BEFORE POWER ON</small>',
      checklist.map((item, index) => '<label><span>' + (index + 1) + "</span><b>" + escapeHtml(item) + '</b><i aria-hidden="true">✓</i></label>').join(""), "</div></section>"
    ].join("");
  }

  function renderPlanVisual(ctx) {
    const steps = getVisualTextList(ctx.slide.steps, 5, ["설계", "제작", "시험", "개선", "공유"]);
    const artifact = getVisualTextList(ctx.slide.studentArtifacts || ctx.lesson.studentArtifacts, 1, ["완성 작품"]);
    return [
      '<section class="visual-route-map route-variant--', String(ctx.profile.route || 0), '"><div class="route-map-compass"><i></i><b>N</b></div><ol>',
      steps.map((step, index) => '<li style="--route-step:' + index + '"><span>' + String(index + 1).padStart(2, "0") + "</span><b>" + escapeHtml(step) + "</b><i></i></li>").join(""),
      '</ol><div class="route-destination">FINISH<b>', escapeHtml(artifact[0]), '</b></div><div class="route-scene">', renderLessonScene(ctx, "compact"), "</div></section>"
    ].join("");
  }

  function classifyBuildScene(slide) {
    const titleSource = String(slide.title || "").toLowerCase();
    const source = [slide.title, asList(slide.instructions).join(" "), slide.checkpoint].join(" ").toLowerCase();
    if (["범위", "사용자와 문제", "역할 분담", "팀 역할"].some((word) => titleSource.includes(word))) {
      return "brief";
    }
    const titleRules = [
      { kind: "iteration", words: ["수정", "개선", "디버그", "회귀", "보완"] },
      { kind: "assembly", words: ["연결", "조립", "배선", "회로", "장착", "모듈 구성"] },
      { kind: "logic", words: ["구현", "상태 변화", "제어", "코드", "알고리즘", "로그 만들기", "계산"] },
      { kind: "blueprint", words: ["설계", "구조", "스키마", "화면", "대시보드", "데이터 모델", "요구사항"] },
      { kind: "instrument", words: ["측정", "수집", "관찰", "기록", "센서 보정", "텔레메트리 분석"] },
      { kind: "testbench", words: ["시험", "검증", "테스트", "체크리스트", "e2e"] },
      { kind: "storyboard", words: ["발표", "시연", "리허설", "데모", "포트폴리오", "공유"] },
      { kind: "brief", words: ["범위", "목표", "문제", "사용자", "역할", "안전", "고정"] }
    ];
    const titleMatch = titleRules.find((rule) => rule.words.some((word) => titleSource.includes(word)));
    if (titleMatch) {
      return titleMatch.kind;
    }
    const rules = [
      { kind: "iteration", words: ["수정", "개선", "오류", "디버그", "회귀", "보완", "리팩터"] },
      { kind: "storyboard", words: ["발표", "시연", "리허설", "데모", "설명", "포트폴리오", "공유"] },
      { kind: "assembly", words: ["연결", "조립", "배선", "모듈", "회로", "장착", "전원"] },
      { kind: "instrument", words: ["측정", "관찰", "수집", "기록", "센서", "보정", "텔레메트리", "로그"] },
      { kind: "testbench", words: ["시험", "검증", "테스트", "확인", "경계", "종단", "e2e"] },
      { kind: "logic", words: ["구현", "상태", "제어", "코드", "블록", "알고리즘", "정렬", "계산", "처리"] },
      { kind: "blueprint", words: ["설계", "구조", "스키마", "데이터", "화면", "요구사항", "정책", "모델"] },
      { kind: "brief", words: ["안전", "역할", "문제", "사용자", "범위", "목표", "고정"] }
    ];
    const match = rules.find((rule) => rule.words.some((word) => source.includes(word)));
    if (match) {
      return match.kind;
    }
    return ["brief", "blueprint", "assembly", "logic", "instrument", "testbench", "iteration", "storyboard"][(Math.max(1, Number(slide.stepNumber) || 1) - 1) % 8];
  }

  function renderBuildBrief(ctx, instructions) {
    return '<div class="build-brief-canvas"><div class="build-brief-target"><small>WHO · NEED · RESULT</small><b>' + escapeHtml(instructions[0]) + '</b></div><div class="build-sticky-cloud">' + instructions.slice(1).map((item, index) => '<span class="sticky-' + (index + 1) + '">' + escapeHtml(item) + "</span>").join("") + '</div><div class="build-brief-scene">' + renderLessonScene(ctx, "compact") + "</div></div>";
  }

  function renderBuildBlueprint(ctx, instructions) {
    return '<div class="build-blueprint-sheet"><div class="blueprint-grid"></div><div class="blueprint-scene">' + renderLessonScene(ctx, "mechanism") + '</div><div class="blueprint-specs">' + instructions.map((item, index) => '<span><i>' + String(index + 1).padStart(2, "0") + "</i><b>" + escapeHtml(item) + "</b></span>").join("") + "</div></div>";
  }

  function renderBuildAssembly(ctx, instructions) {
    return '<div class="build-assembly-bench"><div class="assembly-product">' + renderAtmosphereAsset(ctx, "assembly-product-image") + '<span class="assembly-ring ring-a"></span><span class="assembly-ring ring-b"></span></div><ol>' + instructions.map((item, index) => '<li><span>' + (index + 1) + "</span><b>" + escapeHtml(item) + "</b></li>").join("") + "</ol></div>";
  }

  function renderBuildLogic(ctx, instructions) {
    const rails = instructions.slice(0, 3);
    return '<div class="build-logic-console"><div class="logic-state-rail">' + asList(ctx.profile.tokens).map((token, index) => '<span><i>' + (index ? "→" : "●") + "</i><b>" + escapeHtml(token) + "</b></span>").join("") + '</div><div class="logic-blocks">' + rails.map((item, index) => '<code><span>' + ["IF", "THEN", "VERIFY"][index] + "</span>" + escapeHtml(item) + "</code>").join("") + '</div><div class="logic-monitor">' + renderLessonScene(ctx, "compact") + "</div></div>";
  }

  function renderBuildInstrument(ctx, instructions) {
    return '<div class="build-instrument-panel"><div class="instrument-chart">' + Array.from({ length: 14 }, (_value, index) => '<i style="--bar:' + (16 + (index * 29) % 78) + '%"></i>').join("") + '<span>LIVE SAMPLE</span></div><div class="instrument-gauge"><i></i><b>' + escapeHtml(ctx.profile.tokens[1] || "MEASURE") + '</b></div><ul>' + instructions.map((item) => "<li>" + escapeHtml(item) + "</li>").join("") + "</ul></div>";
  }

  function renderBuildTestbench(ctx, instructions) {
    return '<div class="build-testbench"><div class="testbench-head"><span>CASE</span><span>EXPECTED</span><span>EVIDENCE</span></div>' + instructions.slice(0, 4).map((item, index) => '<div class="testbench-row"><b>T-' + String(index + 1).padStart(2, "0") + "</b><span>" + escapeHtml(item) + '</span><i class="' + (index % 3 === 2 ? "watch" : "pass") + '">' + (index % 3 === 2 ? "WATCH" : "PASS") + "</i></div>").join("") + '<div class="testbench-scene">' + renderLessonScene(ctx, "compact") + "</div></div>";
  }

  function renderBuildIteration(ctx, instructions) {
    return '<div class="build-iteration"><article><small>BEFORE</small><b>' + escapeHtml(instructions[0]) + '</b><div class="iteration-bug">!</div></article><div class="iteration-diff"><i>−</i><span>원인 한 가지씩 분리</span><i>+</i></div><article><small>AFTER</small><b>' + escapeHtml(instructions[1] || instructions[0]) + '</b><div class="iteration-pass">PASS</div></article></div>';
  }

  function renderBuildStoryboard(ctx, instructions) {
    return '<div class="build-storyboard"><div class="storyboard-stage">' + renderLessonScene(ctx, "compact") + '</div><ol>' + instructions.slice(0, 4).map((item, index) => '<li><span>' + String(index + 1).padStart(2, "0") + "</span><b>" + escapeHtml(item) + "</b><i>" + (index + 1) * 30 + "s</i></li>").join("") + "</ol></div>";
  }

  function renderBuildVisual(ctx) {
    const buildKind = classifyBuildScene(ctx.slide);
    const instructions = getVisualTextList(ctx.slide.instructions, 4, getVisualTextList(ctx.slide.body, 4, ["핵심 단계 실행", "결과 확인", "증거 남기기"]));
    const renderers = {
      brief: renderBuildBrief,
      blueprint: renderBuildBlueprint,
      assembly: renderBuildAssembly,
      logic: renderBuildLogic,
      instrument: renderBuildInstrument,
      testbench: renderBuildTestbench,
      iteration: renderBuildIteration,
      storyboard: renderBuildStoryboard
    };
    const body = [
      '<section class="visual-maker-blueprint build-scene--', buildKind, '"><header><span>STEP ', String(ctx.slide.stepNumber || 1), " / ", String(ctx.slide.stepTotal || "?"), "</span><b>",
      escapeHtml(ctx.slide.title), "</b></header>", renderers[buildKind](ctx, instructions),
      '<footer><span>CHECKPOINT</span><b>', escapeHtml(shortenVisualText(ctx.slide.checkpoint || "실행 결과와 근거를 확인하세요.", 92)), "</b></footer></section>"
    ].join("");
    return renderVisualShell(ctx, body, { buildKind });
  }

  function renderCheckpointVisual(ctx) {
    const criteria = getVisualTextList(ctx.slide.criteria, 4, ["동작 증거", "설계 일치", "수정 기록"]);
    const artifacts = getVisualTextList(ctx.slide.studentArtifacts || ctx.lesson.studentArtifacts, 2, ["완성 화면", "제작 기록"]);
    return [
      '<section class="visual-evidence-pinboard"><div class="pinboard-title"><small>EVIDENCE WALL</small><b>', escapeHtml(ctx.profile.title), '</b></div><div class="evidence-polaroids">',
      artifacts.map((item, index) => '<article class="polaroid-' + (index + 1) + '"><div>' + renderLessonScene(ctx, "compact") + '</div><b>' + escapeHtml(item) + "</b><i></i></article>").join(""),
      '</div><ul>', criteria.map((item, index) => '<li><span>' + (index + 1) + "</span><b>" + escapeHtml(item) + '<i class="evidence-stamp">PROOF</i></b></li>').join(""), "</ul></section>"
    ].join("");
  }

  function renderTroubleshootVisual(ctx) {
    const issues = asList(ctx.slide.issues).slice(0, 3);
    const safeIssues = issues.length ? issues : [{ symptom: "예상과 다른 결과", cause: "입력·상태·연결 중 하나", fix: "한 항목씩 고치고 다시 시험" }];
    return [
      '<section class="visual-diagnostic-tree"><div class="diagnostic-root"><span>?</span><b>', escapeHtml(ctx.profile.title), '</b><small>증상을 먼저 재현</small></div><div class="diagnostic-trunk"></div><div class="diagnostic-branches">',
      safeIssues.map((issue, index) => '<article><div class="symptom"><small>SYMPTOM ' + String(index + 1).padStart(2, "0") + "</small><b>" + escapeHtml(shortenVisualText(issue.symptom || "문제 재현", 58)) + '</b></div><i></i><div class="cause"><small>CAUSE</small><b>' + escapeHtml(shortenVisualText(issue.cause || "원인 분리", 58)) + '</b></div><i></i><div class="fix"><small>FIX</small><b>' + escapeHtml(shortenVisualText(issue.fix || "수정 후 재시험", 58)) + "</b></div></article>").join(""),
      "</div></section>"
    ].join("");
  }

  function renderDifferentiateVisual(ctx) {
    const support = getVisualTextList(ctx.slide.support, 2, ["핵심 기능부터 완성", "제공된 예시로 다시 시도"]);
    const challenge = getVisualTextList(ctx.slide.challenge, 2, ["새 조건으로 확장", "다른 상황까지 시험"]);
    const core = getVisualTextList(ctx.lesson.successCriteria, 1, [ctx.profile.subtitle]);
    return [
      '<section class="visual-branch-lanes"><div class="branch-core">CORE<b>', escapeHtml(core[0]), '</b><i></i></div><div class="branch-split"><i></i><i></i></div>',
      '<article class="branch-support"><span>SUPPORT ROUTE</span>', support.map((item, index) => '<b><i>' + (index + 1) + "</i>" + escapeHtml(item) + "</b>").join(""), '</article>',
      '<article class="branch-challenge"><span>CHALLENGE ROUTE</span>', challenge.map((item, index) => '<b><i>' + (index + 1) + "</i>" + escapeHtml(item) + "</b>").join(""), '</article>',
      '<div class="branch-finish">같은 목표 · 다른 경로</div></section>'
    ].join("");
  }

  function renderRubricVisual(ctx) {
    const rows = asList(ctx.slide.rows).slice(0, 4);
    const safeRows = rows.length ? rows : [{ criterion: "기능", basic: "도움 받아 동작", proficient: "스스로 동작", advanced: "새 조건까지 확장" }];
    return [
      '<section class="visual-rubric-heatmap"><div class="heatmap-head"><b>CRITERION</b><span>기초</span><span>도달</span><span>심화</span></div>',
      safeRows.map((row, index) => '<div class="heatmap-row"><b>' + escapeHtml(row.criterion) + '</b><span data-level="1">' + escapeHtml(shortenVisualText(row.basic, 38)) + '</span><span data-level="2">' + escapeHtml(shortenVisualText(row.proficient, 38)) + '</span><span data-level="3">' + escapeHtml(shortenVisualText(row.advanced, 38)) + "</span><i>" + String(index + 1).padStart(2, "0") + "</i></div>").join(""),
      '<div class="heatmap-evidence">평가는 작품이 아니라 <b>작품 + 과정 + 근거</b>를 함께 봅니다.</div></section>'
    ].join("");
  }

  function renderExitVisual(ctx) {
    const takeaways = getVisualTextList(ctx.slide.takeaways, 3, asList(ctx.profile.tokens));
    return [
      '<section class="visual-completion-orbit"><div class="completion-core"><i></i><span>MISSION</span><b>COMPLETE</b><small>', escapeHtml(ctx.profile.code), '</small></div><div class="completion-ring ring-a"></div><div class="completion-ring ring-b"></div>',
      takeaways.map((item, index) => '<article class="takeaway-' + (index + 1) + '"><span>' + String(index + 1).padStart(2, "0") + "</span><b>" + escapeHtml(item) + "</b></article>").join(""),
      '<div class="next-gate"><small>NEXT MISSION</small><b>', escapeHtml(shortenVisualText(ctx.slide.question || "오늘의 근거를 다음 프로젝트에 연결하세요.", 82)), "</b><i>→</i></div></section>"
    ].join("");
  }

  const SLIDE_VISUAL_RENDERERS = {
    title: renderTitleVisual,
    goals: renderGoalsVisual,
    hook: renderHookVisual,
    vocabulary: renderVocabularyVisual,
    concept: renderConceptVisual,
    example: renderExampleVisual,
    check: renderCheckVisual,
    setup: renderSetupVisual,
    plan: renderPlanVisual,
    checkpoint: renderCheckpointVisual,
    troubleshoot: renderTroubleshootVisual,
    differentiate: renderDifferentiateVisual,
    rubric: renderRubricVisual,
    exit: renderExitVisual
  };

  function renderLessonSlideVisual(levelId, lesson, slide, slideIndex) {
    const ctx = createSlideVisualContext(levelId, lesson, slide, slideIndex);
    if (slide.type === "build") {
      return renderBuildVisual(ctx);
    }
    const renderer = SLIDE_VISUAL_RENDERERS[slide.type] || renderConceptVisual;
    return renderVisualShell(ctx, renderer(ctx));
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
    if (state.activeLesson) {
      dismissLessonPlayer();
    }
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
      '<div class="mode-tabs" role="group" aria-label="수업 유형 필터">',
      tabs.map((tab) => [
        '<button type="button" data-mode-filter="', tab.id, '" class="', state.modeFilter === tab.id ? "active" : "",
        '" aria-pressed="', state.modeFilter === tab.id ? "true" : "false", '">', tab.label, "</button>"
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
      String(lesson.no).padStart(2, "0"), completed ? '<span class="completion-mark" aria-label="완료">✓</span>' : "", '</span><span class="mode-chip ', escapeHtml(lesson.projectType), '">',
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

    planBody.scrollTop = 0;
    planDialog.showModal();
  }

  function closePlan() {
    if (planDialog.open) {
      planDialog.close();
    }
  }

  function usesMobileStudio() {
    return window.matchMedia("(max-width: 980px)").matches;
  }

  function syncStudioAccessibility() {
    const mobile = usesMobileStudio();
    const open = mobile && state.studioOpen;
    const backgroundSections = document.querySelectorAll(".player-header, .slide-rail, .slide-stage");
    learningStudio.classList.toggle("mobile-open", open);
    studioBackdrop.classList.toggle("open", open);
    studioBackdrop.disabled = !open;
    studioBackdrop.setAttribute("aria-hidden", open ? "false" : "true");
    studioToggle.setAttribute("aria-expanded", open ? "true" : "false");
    studioToggle.textContent = open ? "활동실 닫기" : "활동실 열기";
    learningStudio.setAttribute("aria-hidden", mobile && !open ? "true" : "false");
    if (mobile && !open) {
      learningStudio.setAttribute("inert", "");
    } else {
      learningStudio.removeAttribute("inert");
    }
    backgroundSections.forEach((section) => {
      if (open) {
        section.setAttribute("inert", "");
      } else {
        section.removeAttribute("inert");
      }
    });
  }

  function openStudio() {
    if (!state.activeLesson || !usesMobileStudio()) {
      return;
    }
    state.studioOpen = true;
    syncStudioAccessibility();
    document.getElementById("closeStudioButton").focus();
  }

  function closeStudio(options) {
    const settings = options || {};
    const wasOpen = state.studioOpen;
    state.studioOpen = false;
    syncStudioAccessibility();
    if (wasOpen && settings.restoreFocus !== false && usesMobileStudio()) {
      studioToggle.focus();
    }
  }

  function setTeacherNoteOpen(next) {
    state.teacherNoteOpen = Boolean(next);
    [document.getElementById("teacherToggle"), mobileTeacherToggle].forEach((button) => {
      button.classList.toggle("active", state.teacherNoteOpen);
      button.setAttribute("aria-pressed", state.teacherNoteOpen ? "true" : "false");
    });
    document.getElementById("teacherNote").classList.toggle("open", state.teacherNoteOpen);
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
    state.previewSource = "preset";
    state.previewDemoActive = false;
    state.studioOpen = false;
    state.teacherNoteOpen = false;
    state.quizAnswers = {};
    state.checklistAnswers = {};
    state.lessonStartedAt = Date.now();
    state.chatMessages = [{ type: "assistant", text: "수업 활동의 예시 문장을 눌러 시작하거나, 만들고 싶은 내용을 직접 설명해 보세요." }];
    state.files = {};
    state.blocklyXml = "";
    state.modiModules = [];
    stopChat();
    document.body.classList.add("player-open");
    lessonPlayer.classList.add("open");
    lessonPlayer.setAttribute("aria-hidden", "false");
    if (!lessonPlayer.open) {
      lessonPlayer.showModal();
    }
    document.getElementById("playerMeta").textContent = getLevelMeta(catalog.level).difficulty + " · " + catalog.label + " " + catalog.subject + " · " + catalog.classMinutes + "분";
    document.getElementById("playerTitle").textContent = lesson.no + "차시 · " + lesson.title;
    setTeacherNoteOpen(false);
    syncStudioAccessibility();
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

  function dismissLessonPlayer() {
    stopChat();
    window.clearInterval(state.timerId);
    state.timerId = null;
    lessonPlayer.classList.remove("open");
    lessonPlayer.setAttribute("aria-hidden", "true");
    closeStudio({ restoreFocus: false });
    if (lessonPlayer.open) {
      lessonPlayer.close();
    }
    document.body.classList.remove("player-open");
    state.activeLesson = null;
  }

  function exitLesson(completed) {
    if (!state.activeLesson) {
      return;
    }
    const catalog = getActiveCatalog();
    const lessonNumber = state.activeLesson.no;
    if (completed) {
      state.progress[lessonKey(catalog.level, state.activeLesson.no)] = {
        completedAt: new Date().toISOString(),
        title: state.activeLesson.title
      };
      saveProgress();
    }
    dismissLessonPlayer();
    renderCourse();
    window.requestAnimationFrame(() => {
      const startButton = document.querySelector('[data-start-lesson="' + String(lessonNumber) + '"]');
      if (startButton) {
        startButton.focus({ preventScroll: true });
      }
    });
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
    timer.innerHTML = "<span>" + minutes + ":" + seconds + '</span><span class="class-duration"> · ' + String(catalog.classMinutes) + "분</span>";
    timer.classList.toggle("warning", elapsed >= Math.max(0, catalog.classMinutes - 5) * 60);
  }

  function renderSlideRail() {
    const lesson = state.activeLesson;
    document.getElementById("slideRail").innerHTML = [
      '<p class="slide-rail-summary">', escapeHtml(MODES[lesson.projectType].label), " · ", String(totalMinutes(lesson)), "분 · ", String(lesson.slides.length), "단계</p>",
      asList(lesson.slides).map((slide, index) => [
        '<button type="button" class="slide-step ', index === state.slideIndex ? "active" : "", " ", index < state.slideIndex ? "completed" : "",
        '" data-slide-index="', String(index), '"', index === state.slideIndex ? ' aria-current="step"' : "", '><span class="step-index">', index < state.slideIndex ? "✓" : String(index + 1),
        "</span><span><strong>", escapeHtml(slide.title), "</strong><small>", escapeHtml(slide.phase), " · ", String(slide.minutes || 0), "분</small></span></button>"
      ].join("")).join("")
    ].join("");
  }

  function renderQuizContent(slide) {
    const takeaways = asList(slide.takeaways);
    const answerKey = String(state.slideIndex);
    const answered = Object.prototype.hasOwnProperty.call(state.quizAnswers, answerKey);
    const selected = answered ? Number(state.quizAnswers[answerKey]) : -1;
    return [
      "<h2>", escapeHtml(slide.title), "</h2>",
      '<p class="quiz-question">Q. ', escapeHtml(slide.question), "</p>",
      '<div class="quiz-choices">', asList(slide.choices).map((choice, index) => [
        '<button type="button" class="quiz-choice', answered && index === Number(slide.answer) ? " correct" : "", answered && index === selected && index !== Number(slide.answer) ? " wrong" : "",
        '" data-quiz-choice="', String(index), '" aria-pressed="', index === selected ? "true" : "false", '"', answered ? " disabled" : "", '><b>', String(index + 1), "</b><span>", escapeHtml(choice), "</span></button>"
      ].join("")).join(""), '</div><div class="quiz-feedback" id="quizFeedback" aria-live="polite">',
      answered
        ? escapeHtml((selected === Number(slide.answer) ? "정답입니다. " : "정답을 함께 표시했습니다. ") + String(slide.explanation || "핵심 개념을 다시 확인해 보세요."))
        : slide.type === "exit" ? "답을 선택하면 이 차시를 완료할 수 있어요." : "",
      "</div>",
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
    const slideHeading = slideCard.querySelector("h2");
    if (slideHeading) {
      slideHeading.insertAdjacentHTML("afterend", renderLessonSlideVisual(getActiveCatalog().level, lesson, slide, state.slideIndex));
    }
    slideCard.scrollTop = 0;

    const note = document.getElementById("teacherNote");
    note.innerHTML = '<strong>교사 노트</strong>' + escapeHtml(slide.teacherNote || "이 단계에는 별도 교사 노트가 없습니다.");
    note.classList.toggle("open", state.teacherNoteOpen);

    const phases = ["도입", "전개", "정리"];
    document.getElementById("phaseTrack").innerHTML = phases.map((phase) => (
      '<span class="' + (phase === slide.phase ? "active" : "") + '">' + phase + "</span>"
    )).join("");

    document.getElementById("previousSlideButton").disabled = state.slideIndex === 0;
    const finalSlide = state.slideIndex === lesson.slides.length - 1;
    const exitAnswered = slide.type !== "exit" || Object.prototype.hasOwnProperty.call(state.quizAnswers, String(state.slideIndex));
    const nextButton = document.getElementById("nextSlideButton");
    nextButton.textContent = finalSlide ? "수업 완료" : "다음";
    nextButton.disabled = finalSlide && !exitAnswered;
    document.getElementById("slideCounter").textContent = String(state.slideIndex + 1) + " / " + String(lesson.slides.length);
    const progress = document.getElementById("slideProgress");
    progress.setAttribute("aria-valuemax", String(lesson.slides.length));
    progress.setAttribute("aria-valuenow", String(state.slideIndex + 1));
    progress.querySelector("span").style.width = String(((state.slideIndex + 1) / lesson.slides.length) * 100) + "%";
    document.getElementById("lessonLive").textContent = String(state.slideIndex + 1) + "단계, " + slide.title + ", " + slide.phase;
    renderSlideRail();
    renderStudio();
  }

  function focusCurrentSlide() {
    const heading = document.querySelector("#slideCard h2");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
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
      const currentSlide = state.activeLesson.slides[state.slideIndex];
      if (currentSlide.type === "exit" && !Object.prototype.hasOwnProperty.call(state.quizAnswers, String(state.slideIndex))) {
        showToast("마무리 문항에 답한 뒤 차시를 완료해 주세요.");
        const firstChoice = document.querySelector("[data-quiz-choice]");
        if (firstChoice) {
          firstChoice.focus();
        }
        return;
      }
      exitLesson(true);
      return;
    }
    state.slideIndex = next;
    renderSlide();
    focusCurrentSlide();
  }

  function chooseQuiz(index) {
    const slide = state.activeLesson.slides[state.slideIndex];
    if (!isQuizSlide(slide)) {
      return;
    }
    state.quizAnswers[String(state.slideIndex)] = Number(index);
    document.querySelectorAll("[data-quiz-choice]").forEach((button) => {
      const choice = Number(button.dataset.quizChoice);
      button.classList.toggle("correct", choice === Number(slide.answer));
      button.classList.toggle("wrong", choice === Number(index) && choice !== Number(slide.answer));
      button.setAttribute("aria-pressed", choice === Number(index) ? "true" : "false");
      button.disabled = true;
    });
    const correct = Number(index) === Number(slide.answer);
    document.getElementById("quizFeedback").textContent = (correct ? "정답입니다. " : "정답을 함께 표시했습니다. ") + String(slide.explanation || "핵심 개념을 다시 확인해 보세요.");
    document.getElementById("nextSlideButton").disabled = false;
  }

  function renderStudio() {
    const slide = state.activeLesson.slides[state.slideIndex];
    learningStudio.classList.toggle("preview-mode", state.studioTab === "preview");
    document.querySelectorAll("[data-studio-tab]").forEach((button) => {
      const active = button.dataset.studioTab === state.studioTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.setAttribute("tabindex", active ? "0" : "-1");
    });
    const studio = document.getElementById("studioBody");
    studio.setAttribute("aria-live", state.studioTab === "activity" ? "polite" : "off");
    const activeTabButton = document.querySelector('[data-studio-tab="' + state.studioTab + '"]');
    if (activeTabButton) {
      studio.setAttribute("aria-labelledby", activeTabButton.id);
    }
    if (state.studioTab === "preview") {
      studio.innerHTML = renderPreview();
      syncTutorComposerState();
      return;
    }
    if (state.studioTab === "modi") {
      studio.innerHTML = renderModiPanel();
      syncTutorComposerState();
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
        '<label><input type="checkbox" data-checkpoint="', String(index), '"', asList(state.checklistAnswers[String(state.slideIndex)]).includes(index) ? " checked" : "", '><span>', escapeHtml(line), "</span></label>"
      ].join("")).join("") + "</div>" : "",
      prompts.length ? '<div class="prompt-list">' + prompts.map((prompt) => (
        '<button class="prompt-button" type="button" data-prompt="' + escapeHtml(prompt) + '">' + escapeHtml(prompt) + "</button>"
      )).join("") + "</div>" : "",
      !body.length && !prompts.length ? '<div class="studio-empty"><div><span class="empty-icon">✓</span><strong>수업 화면에 집중하세요</strong><span>제작 단계에서 체크리스트와 AI 예시가 나타납니다.</span></div></div>' : "",
      slide.checkpoint ? '<div class="studio-checkpoint"><b>통과 조건</b>' + escapeHtml(slide.checkpoint) + "</div>" : "",
      renderChatThread(),
      "</div>"
    ].join("");
    syncTutorComposerState();
  }

  function syncTutorComposerState() {
    const input = document.getElementById("tutorInput");
    const button = document.querySelector("#tutorForm button[type='submit']");
    if (!input || !button) {
      return;
    }
    input.disabled = state.streaming;
    button.disabled = state.streaming;
    button.setAttribute("aria-busy", state.streaming ? "true" : "false");
  }

  function renderChatThread() {
    return '<div class="chat-thread" id="chatThread">' + state.chatMessages.map((message) => (
      '<div class="chat-message ' + escapeHtml(message.type) + '">' + escapeHtml(message.text) + "</div>"
    )).join("") + "</div>";
  }

  function getPreviewPreset(lesson, catalog) {
    const key = lessonKey(catalog.level, lesson.no);
    return PREVIEW_PRESETS[key] || {
      product: lesson.title,
      eyebrow: "차시 완성 예시",
      primaryLabel: "완성 상태",
      primaryValue: "READY",
      status: "샘플 데이터",
      message: lesson.summary,
      metrics: [["차시", lesson.no + "차시"], ["유형", lesson.projectType], ["준비", "완료"]],
      meter: 84,
      action: "샘플 실행",
      activeStatus: "예시 실행 완료",
      activePrimary: "DONE"
    };
  }

  function getPreviewExample(lesson) {
    return asList(lesson.slides).find((slide) => slide.type === "example") || {};
  }

  function renderPreviewSourceSwitch(activeSource, hasGenerated) {
    if (!hasGenerated) {
      return "";
    }
    return [
      '<div class="preview-source-switch" role="tablist" aria-label="미리보기 결과 선택">',
      '<button type="button" role="tab" id="presetPreviewTab" aria-controls="previewResultPanel" aria-selected="', activeSource === "preset" ? "true" : "false", '" tabindex="', activeSource === "preset" ? "0" : "-1", '" data-preview-source="preset">완성 예시</button>',
      '<button type="button" role="tab" id="generatedPreviewTab" aria-controls="previewResultPanel" aria-selected="', activeSource === "mine" ? "true" : "false", '" tabindex="', activeSource === "mine" ? "0" : "-1", '" data-preview-source="mine"><span aria-hidden="true">●</span> 내 결과</button>',
      "</div>"
    ].join("");
  }

  function renderPreviewMetrics(preset) {
    return '<div class="seed-metric-grid">' + asList(preset.metrics).map((metric) => [
      "<div><span>", escapeHtml(metric[0]), "</span><strong>", escapeHtml(metric[1]), "</strong></div>"
    ].join("")).join("") + "</div>";
  }

  function renderWorldPortal(world, lesson, preset) {
    return [
      '<div class="seed-world-portal" aria-hidden="true">',
      '<div class="seed-world-depth"><i></i><i></i><i></i></div>',
      '<div class="seed-world-vignette"></div><div class="seed-world-orbit"><i></i><i></i><i></i></div>',
      '<div class="seed-world-meta"><span>', escapeHtml(world.name), '</span><strong>MISSION ', String(lesson.no).padStart(2, "0"), '</strong><small>', escapeHtml(world.zone), ' · ', escapeHtml(preset.eyebrow), '</small></div>',
      '<div class="seed-world-beacon"><i></i><i></i><i></i><i></i></div>',
      '</div>'
    ].join("");
  }

  function renderWebPreset(preset, active, world, lesson) {
    const primary = active ? preset.activePrimary : preset.primaryValue;
    const status = active ? preset.activeStatus : preset.status;
    return [
      '<div class="seed-web-app world-', escapeHtml(world.id), active ? " is-running" : "", '">',
      renderWorldPortal(world, lesson, preset),
      '<div class="seed-web-spark" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>',
      '<div class="seed-scene-camera seed-scene-camera-web">',
      '<div class="seed-app-nav"><span class="seed-app-logo" aria-hidden="true">M</span><b>', escapeHtml(preset.product), '</b><span class="seed-status">', escapeHtml(status), "</span></div>",
      '<div class="seed-app-hero"><div class="seed-hero-labels"><span class="seed-eyebrow">', escapeHtml(preset.eyebrow), '</span><b>AI READY</b></div><p>', escapeHtml(preset.primaryLabel), '</p><strong class="seed-primary-value" role="status" aria-live="polite">', escapeHtml(primary), "</strong><small>", escapeHtml(preset.message), "</small>",
      '<div class="seed-visualizer" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>',
      '<div class="seed-progress" aria-label="완성도 ', String(preset.meter), '%"><i style="width:', String(preset.meter), '%"></i></div></div>',
      renderPreviewMetrics(preset),
      '<button class="seed-run-button" type="button" data-preview-action="demo"><span aria-hidden="true">', active ? "↻" : "▶", "</span>", escapeHtml(active ? "처음 상태로 되돌리기" : preset.action), "</button>",
      "</div></div>"
    ].join("");
  }

  function renderHardwarePreset(preset, active, world, lesson) {
    const primary = active ? preset.activePrimary : preset.primaryValue;
    const status = active ? preset.activeStatus : preset.status;
    return [
      '<div class="seed-hardware world-', escapeHtml(world.id), active ? " is-running" : "", '">',
      renderWorldPortal(world, lesson, preset),
      '<div class="seed-hw-banner"><span><i></i> DIGITAL TWIN</span><b>MODI LAB</b><div aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i></div></div>',
      '<div class="seed-scene-camera seed-scene-camera-hardware">',
      '<div class="seed-device-flow" aria-label="입력 처리 출력 흐름">',
      '<div><span>INPUT</span><strong>', escapeHtml(preset.input || "센서 입력"), '</strong></div><i aria-hidden="true">→</i>',
      '<div><span>LOGIC</span><strong>', escapeHtml(preset.logic || "조건 처리"), '</strong></div><i aria-hidden="true">→</i>',
      '<div><span>OUTPUT</span><strong>', escapeHtml(preset.output || "장치 출력"), "</strong></div></div>",
      '<div class="seed-device-stage"><div class="seed-device-visual" aria-hidden="true"><span class="seed-sensor-wave"></span><div class="seed-modi-core"><i></i><b>M</b><i></i></div><span class="seed-device-output"></span></div>',
      '<div class="seed-device-readout"><span class="seed-eyebrow">', escapeHtml(preset.eyebrow), '</span><p>', escapeHtml(preset.primaryLabel), '</p><strong class="seed-primary-value" role="status" aria-live="polite">', escapeHtml(primary), '</strong><span class="seed-status">', escapeHtml(status), "</span><small>", escapeHtml(preset.message), "</small></div></div>",
      renderPreviewMetrics(preset),
      '<div class="seed-signal-track" aria-label="샘플 신호 흐름"><span>00:01</span><div aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div><b>SYNC</b></div>',
      '<button class="seed-run-button" type="button" data-preview-action="demo"><span aria-hidden="true">', active ? "↻" : "▶", "</span>", escapeHtml(active ? "시뮬레이션 초기화" : preset.action), "</button>",
      "</div></div>"
    ].join("");
  }

  function renderConnectedPreset(preset, active, world, lesson) {
    const primary = active ? preset.activePrimary : preset.primaryValue;
    const status = active ? preset.activeStatus : preset.status;
    const missionMark = world.id === "high" ? "◈" : world.id === "middle" ? "✦" : "M";
    return [
      '<div class="seed-ops world-', escapeHtml(world.id), active ? " is-running" : "", '">',
      renderWorldPortal(world, lesson, preset),
      '<div class="seed-ops-glow" aria-hidden="true"><i></i><i></i><i></i></div>',
      '<div class="seed-scene-camera seed-scene-camera-ops">',
      '<div class="seed-ops-head"><div><span class="seed-live-dot" aria-hidden="true"></span><b>MODI CONTROL</b><small>SIMULATION</small></div><button type="button" data-preview-action="demo">', escapeHtml(active ? "RESET" : preset.action), "</button></div>",
      '<div class="seed-ops-grid"><div class="seed-rover-panel"><div class="seed-rover-scene" aria-hidden="true"><span class="seed-road-line"></span><div class="seed-rover"><i></i><b>', missionMark, '</b><i></i></div><span class="seed-obstacle"></span></div>',
      '<div class="seed-flow-caption"><span>', escapeHtml(preset.input || "장치 데이터"), '</span><i aria-hidden="true">↔</i><span>', escapeHtml(preset.output || "관제 명령"), "</span></div></div>",
      '<div class="seed-telemetry"><span class="seed-eyebrow">', escapeHtml(preset.eyebrow), '</span><p>', escapeHtml(preset.primaryLabel), '</p><strong class="seed-primary-value" role="status" aria-live="polite">', escapeHtml(primary), '</strong><span class="seed-status">', escapeHtml(status), "</span>", renderPreviewMetrics(preset), "</div></div>",
      '<div class="seed-event-log"><span><i></i> TELEMETRY</span><b>', escapeHtml(preset.logic || "양방향 데이터 흐름"), "</b><small>", escapeHtml(preset.message), '</small><div class="seed-console-chart" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div></div>',
      "</div></div>"
    ].join("");
  }

  function renderPresetScene(lesson, preset, catalog) {
    const world = WORLD_PROFILES[catalog.level] || WORLD_PROFILES.elementary;
    if (lesson.projectType === "hw") {
      return renderHardwarePreset(preset, state.previewDemoActive, world, lesson);
    }
    if (lesson.projectType === "webhw") {
      return renderConnectedPreset(preset, state.previewDemoActive, world, lesson);
    }
    return renderWebPreset(preset, state.previewDemoActive, world, lesson);
  }

  function renderPresetPreview(lesson, catalog, hasGenerated) {
    const preset = getPreviewPreset(lesson, catalog);
    const example = getPreviewExample(lesson);
    const evidence = asList(example.output).slice(0, 3);
    const artifacts = asList(lesson.studentArtifacts).slice(-2);
    const modeLabel = lesson.projectType === "hw" ? "H/W 디지털 트윈" : lesson.projectType === "webhw" ? "Web + H/W 관제" : "Web 앱";
    return [
      '<section class="preview-showcase" data-preview-key="', escapeHtml(lessonKey(catalog.level, lesson.no)), '" data-preview-mode="', escapeHtml(lesson.projectType), '" data-world="', escapeHtml(catalog.level), '">',
      renderPreviewSourceSwitch("preset", hasGenerated),
      '<div class="preview-heading"><div class="preview-badge-row"><span class="preview-demo-badge">완성 예시</span><span class="preview-mode-badge ', escapeHtml(lesson.projectType), '">', escapeHtml(modeLabel), '</span><span class="preview-preloaded-label">수업 시작 전</span></div><h3 id="presetPreviewTitle">', escapeHtml(preset.product), "</h3><p>", escapeHtml(example.scenario || lesson.summary), "</p></div>",
      '<div class="preview-window preview-window-preset"><div class="preview-bar"><span class="preview-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="preview-address">preview.modiplanet.com · ', String(lesson.no).padStart(2, "0"), '</span><span class="preview-sample-label">샘플 데이터</span></div>',
      '<div class="preview-result-stage" id="previewResultPanel" role="tabpanel"', hasGenerated ? ' aria-labelledby="presetPreviewTab"' : ' aria-labelledby="presetPreviewTitle"', ">", renderPresetScene(lesson, preset, catalog), "</div></div>",
      '<div class="preview-proof"><div class="preview-proof-head"><strong>결과에서 확인할 것</strong><span>완성 기준 ', String(evidence.length), '개</span></div><ul>', evidence.map((item) => '<li><span aria-hidden="true">✓</span><p>' + escapeHtml(item) + "</p></li>").join(""), "</ul></div>",
      '<div class="preview-artifacts"><div><span>STUDENT OUTPUT</span><strong>이 차시에서 남기는 결과물</strong></div><ul>', artifacts.map((item, index) => '<li><i aria-hidden="true">0' + String(index + 1) + '</i><span>' + escapeHtml(item) + "</span></li>").join(""), "</ul></div>",
      '<div class="preview-note"><span aria-hidden="true">✦</span><p><strong>미리 준비된 수업 예시입니다.</strong> 실제 장치 연결이나 학생 결과가 아니며, AI로 제작하면 <b>내 결과</b> 화면으로 자동 전환됩니다.</p></div>',
      '<button class="preview-start-button" type="button" data-preview-action="start"><span>이 완성 예시로 시작하기</span><i aria-hidden="true">→</i></button>',
      "</section>"
    ].join("");
  }

  function renderPreviewFrame(source, title) {
    return '<div class="preview-window preview-window-generated"><div class="preview-bar"><span class="preview-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="preview-address">내 작품 · 실시간 미리보기</span><span class="preview-live-badge"><i></i> LIVE</span></div><iframe title="' + escapeHtml(title) + '" sandbox="allow-scripts" srcdoc="' + escapeHtml(source) + '"></iframe></div>';
  }

  function renderPreview() {
    const names = Object.keys(state.files);
    const lesson = state.activeLesson;
    const catalog = getActiveCatalog();
    if (!names.length || state.previewSource !== "mine") {
      return renderPresetPreview(lesson, catalog, names.length > 0);
    }
    const htmlName = names.find((name) => name.toLowerCase().endsWith(".html"));
    const sourceSwitch = renderPreviewSourceSwitch("mine", true);
    if (htmlName) {
      return '<section class="preview-showcase preview-showcase-generated">' + sourceSwitch + '<div class="preview-heading"><div class="preview-badge-row"><span class="preview-demo-badge live">내 결과</span><span class="preview-preloaded-label">AI 생성 완료</span></div><h3>직접 만든 작품</h3><p>완성 예시와 비교하며 바꾸고 싶은 부분을 이어서 요청해 보세요.</p></div><div id="previewResultPanel" role="tabpanel" aria-labelledby="generatedPreviewTab">' + renderPreviewFrame(state.files[htmlName], lesson.title + " — 학생 작품 미리보기") + '</div><div class="preview-note live"><span aria-hidden="true">✓</span><p><strong>내 결과가 준비되었습니다.</strong> 완성 예시와 오가며 내용·동작·읽기 쉬움을 비교할 수 있습니다.</p></div></section>';
    }
    const firstName = names[0];
    return '<section class="preview-showcase preview-showcase-generated">' + sourceSwitch + '<div class="preview-heading"><div class="preview-badge-row"><span class="preview-demo-badge live">내 결과</span><span class="preview-preloaded-label">코드 생성 완료</span></div><h3>생성된 파일</h3><p>웹 화면이 아닌 코드·블록 결과를 확인합니다.</p></div><div id="previewResultPanel" role="tabpanel" aria-labelledby="generatedPreviewTab"><pre class="code-output">' + escapeHtml(firstName + "\n\n" + state.files[firstName]) + "</pre></div></section>";
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
    if (state.streaming || input.disabled) {
      showToast("AI 응답이 끝난 뒤 다음 요청을 입력해 주세요.");
      return;
    }
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
    state.streaming = true;
    updateChatMessage("user", message, false);
    updateChatMessage("status", "AI가 수업 활동을 확인하고 있어요…", false);
    state.abortController = new AbortController();
    syncTutorComposerState();

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
      syncTutorComposerState();
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
      state.previewSource = "mine";
    } else if (event.type === "blockly_ready") {
      state.blocklyXml = String(event.blockly_xml || "");
      state.modiModules = asList(event.modi_modules);
    } else if (event.type === "done") {
      state.chatMessages = state.chatMessages.filter((item) => item.type !== "status");
      if (event.generated_code) {
        state.files = event.generated_code;
        state.previewSource = "mine";
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
      window.requestAnimationFrame(() => {
        const activeFilter = document.querySelector('[data-mode-filter="' + state.modeFilter + '"]');
        if (activeFilter) {
          activeFilter.focus({ preventScroll: true });
          activeFilter.scrollIntoView({ block: "nearest", inline: "center" });
        }
      });
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
      focusCurrentSlide();
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

    const previewSourceButton = event.target.closest("[data-preview-source]");
    if (previewSourceButton && state.activeLesson) {
      state.previewSource = previewSourceButton.dataset.previewSource;
      state.previewDemoActive = false;
      renderStudio();
      window.requestAnimationFrame(() => {
        const activeButton = document.querySelector('[data-preview-source="' + state.previewSource + '"]');
        if (activeButton) {
          activeButton.focus({ preventScroll: true });
        }
      });
      return;
    }

    const previewActionButton = event.target.closest("[data-preview-action]");
    if (previewActionButton && state.activeLesson) {
      if (previewActionButton.dataset.previewAction === "demo") {
        state.previewDemoActive = !state.previewDemoActive;
        renderStudio();
        window.requestAnimationFrame(() => {
          const demoButton = document.querySelector('[data-preview-action="demo"]');
          if (demoButton) {
            demoButton.focus({ preventScroll: true });
          }
        });
      } else if (previewActionButton.dataset.previewAction === "start") {
        const example = getPreviewExample(state.activeLesson);
        const suggestedPrompt = asList(state.activeLesson.slides).flatMap((slide) => asList(slide.prompts))[0]
          || example.scenario
          || state.activeLesson.summary;
        setStudioTab("activity");
        usePrompt(String(suggestedPrompt || ""));
      }
      return;
    }

    const promptButton = event.target.closest("[data-prompt]");
    if (promptButton) {
      usePrompt(promptButton.dataset.prompt);
    }
  });

  document.addEventListener("change", (event) => {
    const checkpoint = event.target.closest("[data-checkpoint]");
    if (!checkpoint || !state.activeLesson) {
      return;
    }
    const key = String(state.slideIndex);
    const current = new Set(asList(state.checklistAnswers[key]));
    const index = Number(checkpoint.dataset.checkpoint);
    if (checkpoint.checked) {
      current.add(index);
    } else {
      current.delete(index);
    }
    state.checklistAnswers[key] = Array.from(current);
  });

  document.getElementById("closePlanButton").addEventListener("click", closePlan);
  document.getElementById("closePlanFooterButton").addEventListener("click", closePlan);
  document.getElementById("startFromPlanButton").addEventListener("click", () => startLesson(state.planLesson));
  document.getElementById("exitLessonButton").addEventListener("click", () => exitLesson(false));
  document.getElementById("previousSlideButton").addEventListener("click", () => moveSlide(-1));
  document.getElementById("nextSlideButton").addEventListener("click", () => moveSlide(1));
  document.getElementById("teacherToggle").addEventListener("click", () => setTeacherNoteOpen(!state.teacherNoteOpen));
  mobileTeacherToggle.addEventListener("click", () => setTeacherNoteOpen(!state.teacherNoteOpen));
  studioToggle.addEventListener("click", () => state.studioOpen ? closeStudio() : openStudio());
  document.getElementById("closeStudioButton").addEventListener("click", () => closeStudio());
  studioBackdrop.addEventListener("click", () => closeStudio());

  document.getElementById("tutorForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.getElementById("tutorInput");
    const message = input.value.trim();
    if (!message) {
      return;
    }
    if (state.streaming) {
      showToast("AI 응답이 끝난 뒤 다음 요청을 보내 주세요.");
      return;
    }
    input.value = "";
    sendTutorMessage(message);
  });

  document.getElementById("tutorInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      document.getElementById("tutorForm").requestSubmit();
    }
  });

  document.querySelector(".studio-tabs").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      return;
    }
    const tabs = Array.from(document.querySelectorAll("[data-studio-tab]"));
    const current = tabs.indexOf(document.activeElement);
    if (current < 0) {
      return;
    }
    event.preventDefault();
    const next = event.key === "Home" ? 0
      : event.key === "End" ? tabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    setStudioTab(tabs[next].dataset.studioTab);
    tabs[next].focus();
  });

  learningStudio.addEventListener("pointermove", (event) => {
    const stage = event.target.closest(".preview-result-stage");
    if (!stage
      || !DEPTH_POINTER_QUERY.matches
      || REDUCED_MOTION_QUERY.matches) {
      return;
    }
    pendingDepthUpdate = { stage, clientX: event.clientX, clientY: event.clientY };
    if (previewDepthFrame) {
      return;
    }
    previewDepthFrame = window.requestAnimationFrame(() => {
      const update = pendingDepthUpdate;
      pendingDepthUpdate = null;
      previewDepthFrame = 0;
      if (!update || !update.stage.isConnected) {
        return;
      }
      const rect = update.stage.getBoundingClientRect();
      if (!rect.width || !rect.height) {
        return;
      }
      const x = Math.max(0, Math.min(1, (update.clientX - rect.left) / rect.width));
      const y = Math.max(0, Math.min(1, (update.clientY - rect.top) / rect.height));
      update.stage.style.setProperty("--world-pan-x", ((0.5 - x) * 14).toFixed(2) + "px");
      update.stage.style.setProperty("--world-pan-y", ((0.5 - y) * 9).toFixed(2) + "px");
      update.stage.style.setProperty("--world-near-x", ((x - 0.5) * 18).toFixed(2) + "px");
      update.stage.style.setProperty("--world-near-y", ((y - 0.5) * 12).toFixed(2) + "px");
      update.stage.style.setProperty("--object-rotate-x", ((0.5 - y) * 7).toFixed(2) + "deg");
      update.stage.style.setProperty("--object-rotate-y", ((x - 0.5) * 10).toFixed(2) + "deg");
    });
  });

  learningStudio.addEventListener("pointerout", (event) => {
    const stage = event.target.closest(".preview-result-stage");
    if (!stage || stage.contains(event.relatedTarget)) {
      return;
    }
    if (pendingDepthUpdate && pendingDepthUpdate.stage === stage) {
      pendingDepthUpdate = null;
    }
    ["--world-pan-x", "--world-pan-y", "--world-near-x", "--world-near-y", "--object-rotate-x", "--object-rotate-y"].forEach((property) => stage.style.removeProperty(property));
  });

  learningStudio.addEventListener("keydown", (event) => {
    const previewSourceTab = event.target.closest("[data-preview-source]");
    if (previewSourceTab && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const sources = ["preset", "mine"];
      const current = sources.indexOf(previewSourceTab.dataset.previewSource);
      const next = event.key === "Home" ? 0
        : event.key === "End" ? sources.length - 1
          : (current + (event.key === "ArrowRight" ? 1 : -1) + sources.length) % sources.length;
      state.previewSource = sources[next];
      state.previewDemoActive = false;
      renderStudio();
      window.requestAnimationFrame(() => {
        const nextTab = document.querySelector('[data-preview-source="' + state.previewSource + '"]');
        if (nextTab) {
          nextTab.focus({ preventScroll: true });
        }
      });
      return;
    }
    if (event.key !== "Tab" || !state.studioOpen || !usesMobileStudio()) {
      return;
    }
    const focusable = Array.from(learningStudio.querySelectorAll("button:not([disabled]), textarea:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])"))
      .filter((element) => element.getClientRects().length > 0 && element.getAttribute("aria-hidden") !== "true");
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
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
    }
  });

  lessonPlayer.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (state.studioOpen && usesMobileStudio()) {
      closeStudio();
      return;
    }
    exitLesson(false);
  });

  planDialog.addEventListener("click", (event) => {
    if (event.target === planDialog) {
      closePlan();
    }
  });

  window.addEventListener("resize", () => {
    if (!usesMobileStudio()) {
      state.studioOpen = false;
    }
    syncStudioAccessibility();
  });

  window.addEventListener("hashchange", route);
  boot();
})();
