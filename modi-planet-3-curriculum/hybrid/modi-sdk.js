/**
 * modi-sdk — 바이브코딩 웹앱이 MODI 하드웨어와 실시간으로 상호작용하기 위한 브라우저 SDK.
 *
 * modi_flutter 가 이미 노출하는 postMessage 프로토콜
 *   (GET_CONNECTED_MODI / GET_PROPERTY_REQUEST / GET_PROPERTY_RESPONSE / SET_PROPERTY_REQUEST)
 * 을 감싸서, 생성된 코드가 propertyNum 같은 매직넘버를 모른 채
 *   MODI.tof(1).distance         // 센서값 동기 읽기 (구독+캐시)
 *   MODI.led(1).setColor(255,0,0)// 액추에이터 쓰기
 *   useTof(1)                    // React 훅 (값 변할 때 리렌더)
 * 로 쓸 수 있게 한다.
 *
 * 전송(transport)은 추상화되어 있다:
 *   - PostMessageTransport: 실제 modi_flutter 와 통신 (production)
 *   - MockTransport       : 가상 모듈/센서 (하드웨어 없이 개발·시연)
 *
 * blockly 의 ModiStatusManager.js / PostMessageHandler.js 를 브라우저 단독 동작용으로 포팅.
 */

/* ───────────────────────── 프로토콜 상수 ───────────────────────── */

const MSG = {
  GET_CONNECTED_MODI: "GET_CONNECTED_MODI",
  ON_CHANGE_CONNECT_MODI: "ON_CHANGE_CONNECT_MODI",
  GET_PROPERTY_REQUEST: "GET_PROPERTY_REQUEST",
  GET_PROPERTY_RESPONSE: "GET_PROPERTY_RESPONSE",
  SET_PROPERTY_REQUEST: "SET_PROPERTY_REQUEST",
  MODI_FLUTTER_READY: "MODI_FLUTTER_READY",
};

// 모듈 타입 문자열 = modi_flutter 의 ModiType.name 과 동일해야 한다.
const TYPE = {
  BUTTON: "button",
  DIAL: "dial",
  TOF: "tof",
  JOYSTICK: "joystick",
  ENVIRONMENT: "environment",
  IMU: "imu",
  LED: "led",
  SPEAKER: "speaker",
  DISPLAY: "display",
  MOTOR_A: "motorA",
  MOTOR_B: "motorB",
  NETWORK: "network",
  BATTERY: "battery",
};

// 센서 스키마 (단일 출처): 센서타입 → { value객체 키: propertyNum }.
// 읽기 propertyNum·기본값·accessor·onValue 가 모두 이 표에서 파생된다. 센서 추가/수정 시 여기만 고치면 됨.
// (ModiStatusManager 의 getPropertyXxx 매핑과 동일)
const SENSOR_SCHEMA = {
  [TYPE.BUTTON]: { click: 2, doubleClick: 2, pressedState: 2, toggle: 2 },
  [TYPE.DIAL]: { turn: 2, turnSpeed: 2 },
  [TYPE.TOF]: { distance: 2 },
  [TYPE.JOYSTICK]: { coordinateX: 2, coordinateY: 2, direction: 3 },
  [TYPE.ENVIRONMENT]: {
    intensity: 2,
    temperature: 2,
    humidity: 2,
    volume: 2,
    red: 3,
    green: 3,
    blue: 3,
    white: 3,
    black: 3,
    colorClass: 3,
    brightness: 3,
  },
  [TYPE.IMU]: {
    roll: 2,
    pitch: 2,
    yaw: 2,
    accX: 3,
    accY: 3,
    accZ: 3,
    angularVX: 4,
    angularVY: 4,
    angularVZ: 4,
    vibration: 5,
  },
};

// 사용자용 accessor 필드명 → value 키. 대부분 동일하고, 다른 것만 별칭(예: dial.speed→turnSpeed).
// MODI.tof(1).distance / MODI.dial(1).speed 처럼 쓰기 위함.
const SENSOR_ACCESSORS = {
  [TYPE.TOF]: { distance: "distance" },
  [TYPE.BUTTON]: {
    clicked: "click",
    doubleClicked: "doubleClick",
    pressed: "pressedState",
    toggled: "toggle",
  },
  [TYPE.DIAL]: { turn: "turn", speed: "turnSpeed" },
  [TYPE.JOYSTICK]: {
    x: "coordinateX",
    y: "coordinateY",
    direction: "direction",
  },
  [TYPE.ENVIRONMENT]: {
    intensity: "intensity",
    temperature: "temperature",
    humidity: "humidity",
    volume: "volume",
    red: "red",
    green: "green",
    blue: "blue",
    white: "white",
    black: "black",
    colorClass: "colorClass",
    brightness: "brightness",
    brightnessPct: "intensity",
  },
  [TYPE.IMU]: {
    roll: "roll",
    pitch: "pitch",
    yaw: "yaw",
    accX: "accX",
    accY: "accY",
    accZ: "accZ",
    angularVX: "angularVX",
    angularVY: "angularVY",
    angularVZ: "angularVZ",
    vibration: "vibration",
  },
};

// 쓰기(SET) propertyNum — ModiStatusManager 의 setXxxStatus 매핑 그대로.
const WRITE_PROP = {
  LED_RGB: 16,
  SPEAKER_TUNE: 16,
  SPEAKER_MUSIC: 18,
  SPEAKER_MELODY: 19,
  MOTOR_SPEED: 17,
  MOTOR_ANGLE: 18,
  MOTOR_APPEND_ANGLE: 19,
  MOTOR_STOP: 20,
  DISPLAY_TEXT: 17,
  DISPLAY_PICTURE: 19,
  DISPLAY_RESET: 21,
  DISPLAY_VARIABLE: 22,
  DISPLAY_OFFSET: 25,
  DISPLAY_MOVE: 26,
};

// 읽기 응답 value 객체의 기본 형태(미연결 시 0값) — SENSOR_SCHEMA 에서 파생.
const DEFAULT_VALUES = Object.fromEntries(
  Object.entries(SENSOR_SCHEMA).map(([type, keys]) => [
    type,
    Object.fromEntries(Object.keys(keys).map((k) => [k, 0])),
  ]),
);

// 센서 폴링 주기(ms). modi_flutter 의 GET_PROPERTY 는 폴링(재요청) 기반이라 이 주기가 곧 샘플레이트.
// 트래픽 = (구독 property 수) × (1000/POLL_MS). 작을수록 매끈하지만 버스 부담↑.
//   500ms=2Hz(가벼움) · 100ms=10Hz(균형, 기본값) · 50ms=20Hz(모니터링 수준, 트래픽 큼)
const POLL_MS = 100;

/* ───────────────────────── 작은 이벤트 이미터 ───────────────────────── */

function createEmitter() {
  const listeners = new Set();
  return {
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    emit(payload) {
      listeners.forEach((fn) => {
        try {
          fn(payload);
        } catch (e) {
          /* noop */
        }
      });
    },
    get size() {
      return listeners.size;
    },
  };
}

/* ───────────────────────── Transport: PostMessage (production) ───────────────────────── */

class PostMessageTransport {
  constructor({ target, targetOrigin = "*" } = {}) {
    // target 미지정 시 부모창 (modi_flutter 가 이 앱을 iframe 으로 띄운 경우)
    this.target =
      target || (typeof window !== "undefined" ? window.parent : null);
    this.targetOrigin = targetOrigin;
    this._handler = null;
    this._onWindowMessage = (event) => {
      let data = event.data;
      if (typeof data === "string") {
        try {
          data = JSON.parse(data);
        } catch (e) {
          return;
        }
      }
      if (!data || typeof data !== "object" || !data.type) return;
      this._handler && this._handler(data);
    };
  }
  start(handler) {
    this._handler = handler;
    if (typeof window !== "undefined")
      window.addEventListener("message", this._onWindowMessage);
  }
  stop() {
    if (typeof window !== "undefined")
      window.removeEventListener("message", this._onWindowMessage);
    this._handler = null;
  }
  send(message) {
    if (!this.target) return;
    this.target.postMessage(JSON.stringify(message), this.targetOrigin);
  }
}

/* ───────────────────────── Transport: Mock (하드웨어 없이 개발/시연) ───────────────────────── */

class MockTransport {
  /**
   * @param {object} opts
   * @param {Array}  opts.modules  연결된 가상 모듈 [{type, index}]
   * @param {function} opts.simulate  (state) => void  매 tick 마다 가상 센서값 갱신
   * @param {number} opts.tickMs
   * @param {function} opts.onActuator  (payload) => void  SET 요청 가로채기(시연 렌더용)
   */
  constructor({ modules, simulate, tickMs = 80, onActuator } = {}) {
    this.modules = modules || [
      { type: TYPE.TOF, index: 1, id: 11 },
      { type: TYPE.BUTTON, index: 1, id: 12 },
      { type: TYPE.DIAL, index: 1, id: 13 },
      { type: TYPE.LED, index: 1, id: 14 },
      { type: TYPE.MOTOR_A, index: 1, id: 15 },
    ];
    this.onActuator = onActuator || null;
    this.tickMs = tickMs;
    this._handler = null;
    this._subs = new Map(); // key "type:index:prop" -> {type,index,prop}
    this._t = 0;
    this._values = {}; // type -> index -> prop -> value 객체
    this._timer = null;
    // 기본 시뮬레이터: 거리센서는 사인파, 다이얼은 천천히 회전.
    this.simulate =
      simulate ||
      ((s) => {
        const t = s._t;
        s._set(TYPE.TOF, 1, SENSOR_SCHEMA[TYPE.TOF].distance, {
          distance: Math.round(15 + 14 * Math.sin(t / 12)),
        });
        s._set(TYPE.DIAL, 1, SENSOR_SCHEMA[TYPE.DIAL].turn, {
          turn: (t * 2) % 100,
          turnSpeed: 2,
        });
      });
  }
  _set(type, index, prop, value) {
    this._values[type] ||= {};
    this._values[type][index] ||= {};
    this._values[type][index][prop] = value;
  }
  start(handler) {
    this._handler = handler;
    this._timer = setInterval(() => {
      this._t += 1;
      this.simulate(this);
      // 구독된 property 들에 대해 응답 푸시
      for (const { type, index, prop } of this._subs.values()) {
        const value = this._values?.[type]?.[index]?.[prop];
        if (value !== undefined) {
          this._handler({
            type: MSG.GET_PROPERTY_RESPONSE,
            data: { moduleType: type, index, propertyNum: prop, value },
          });
        }
      }
    }, this.tickMs);
  }
  stop() {
    clearInterval(this._timer);
    this._timer = null;
    this._handler = null;
  }
  // 외부에서 버튼 클릭 등 이벤트 주입 (시연 UI 에서 사용)
  inject(type, index, prop, value) {
    this._set(type, index, prop, value);
  }
  send(message) {
    const { type, data } = message;
    if (type === MSG.GET_CONNECTED_MODI) {
      const connectModules = this.modules.map((m, i) => ({
        id: m.id ?? 10 + i,
        type: m.type,
        fieldIndex: m.index,
        index: m.index,
        uuid: `mock-${m.type}-${m.index}`,
      }));
      setTimeout(
        () =>
          this._handler({
            type: MSG.ON_CHANGE_CONNECT_MODI,
            data: { targetModule: null, connectModules },
          }),
        0,
      );
    } else if (type === MSG.GET_PROPERTY_REQUEST) {
      const key = `${data.moduleType}:${data.index}:${data.propertyNum}`;
      this._subs.set(key, {
        type: data.moduleType,
        index: data.index,
        prop: data.propertyNum,
      });
    } else if (type === MSG.SET_PROPERTY_REQUEST) {
      this.onActuator && this.onActuator(data);
    }
  }
}

/* ───────────────────────── Bridge: 구독·캐시·요청 관리 ───────────────────────── */

class ModiBridge {
  constructor() {
    this.transport = null;
    this.connected = false;
    this.modules = [];
    this._cache = {}; // type -> index -> prop -> value 객체
    this._active = new Map(); // "type:index:prop" -> { timer, pollMs }
    this._change = createEmitter(); // 센서값/모듈 변경 알림 (React useSyncExternalStore 용, 프레임당 1회 코얼레싱)
    this._modulesEmitter = createEmitter();
    this._raw = createEmitter(); // 들어오는 모든 센서 응답 (코얼레싱 X — 차트 등 "전부 그리기"용)
    this._changeScheduled = false; // 리렌더 코얼레싱 플래그
  }

  /** 들어오는 모든 GET_PROPERTY_RESPONSE 를 그대로 받는다(코얼레싱 X). 차트 피드용. */
  onValueRaw(fn) {
    return this._raw.subscribe(fn);
  }

  /** 센서 변경 알림을 프레임당 1회로 묶는다 (하드웨어 푸시 속도와 리렌더 속도 분리 → 렉 방지). */
  _scheduleChange() {
    if (this._changeScheduled) return;
    this._changeScheduled = true;
    const fire = () => {
      this._changeScheduled = false;
      this._change.emit({ kind: "sensor" });
    };
    if (typeof requestAnimationFrame !== "undefined")
      requestAnimationFrame(fire);
    else setTimeout(fire, 16);
  }

  configure(transport) {
    if (this.transport) this.transport.stop();
    this.transport = transport;
    this.transport.start((msg) => this._onMessage(msg));
    return this;
  }

  useMock(opts) {
    return this.configure(new MockTransport(opts));
  }
  usePostMessage(opts) {
    return this.configure(new PostMessageTransport(opts));
  }

  _ensureTransport() {
    if (!this.transport) {
      // 기본값: iframe 부모(modi_flutter)와 postMessage. 부모가 없으면 mock.
      const hasParent =
        typeof window !== "undefined" &&
        window.parent &&
        window.parent !== window;
      this.configure(
        hasParent ? new PostMessageTransport() : new MockTransport(),
      );
    }
  }

  /** 모듈 목록을 요청하고 첫 응답을 기다린다. */
  connect({ timeout = 4000 } = {}) {
    this._ensureTransport();
    this.transport.send({ type: MSG.GET_CONNECTED_MODI });
    return new Promise((resolve) => {
      if (this.connected) return resolve(this.modules);
      const off = this._modulesEmitter.subscribe(() => {
        off();
        resolve(this.modules);
      });
      setTimeout(() => {
        off();
        resolve(this.modules);
      }, timeout);
    });
  }

  _onMessage(msg) {
    if (msg.type === MSG.ON_CHANGE_CONNECT_MODI) {
      this.modules = (msg.data && msg.data.connectModules) || [];
      this.connected = true;
      this._modulesEmitter.emit(this.modules);
      this._change.emit({ kind: "modules" });
    } else if (msg.type === MSG.GET_PROPERTY_RESPONSE) {
      const d = msg.data || {};
      const t = d.moduleType,
        i = d.index;
      if (t == null || i == null) return;
      this._cache[t] ||= {};
      this._cache[t][i] ||= {};
      const prev = this._cache[t][i][d.propertyNum];
      this._cache[t][i][d.propertyNum] = d.value; // 캐시는 항상 즉시 최신 (동기 읽기는 지연 없음)
      // 들어온 모든 샘플을 raw로 흘린다 (차트 등 "전부 그리기"용 — 코얼레싱/dedup 없음)
      this._raw.emit({
        moduleType: t,
        index: i,
        propertyNum: d.propertyNum,
        value: d.value,
      });
      // 값이 실제로 바뀐 경우에만 리렌더 예약 → 정지 센서엔 리렌더 0, 변동도 프레임당 1회로 묶음
      if (
        prev === undefined ||
        JSON.stringify(prev) !== JSON.stringify(d.value)
      ) {
        this._scheduleChange();
      }
    }
  }

  /** (type,index,prop) 구독 보장: 1회 요청 + POLL_MS 주기 재요청(폴링). */
  _subscribe(type, index, prop, pollMs = POLL_MS) {
    this._ensureTransport();
    const key = `${type}:${index}:${prop}`;
    const cur = this._active.get(key);
    if (cur && cur.pollMs <= pollMs) return; // 이미 같거나 더 빠른 주기로 폴링 중
    if (cur && cur.timer) clearInterval(cur.timer); // 더 빠른 주기 요청 → 교체
    // fast: true → modi_flutter가 2초 재요청 스로틀을 스킵. pollMs마다 재요청해 폴링.
    const req = () =>
      this.transport.send({
        type: MSG.GET_PROPERTY_REQUEST,
        data: {
          moduleType: type,
          index: Number(index),
          propertyNum: prop,
          fast: true,
        },
      });
    req();
    const timer =
      typeof setInterval !== "undefined" ? setInterval(req, pollMs) : null;
    this._active.set(key, { timer, pollMs });
  }

  /** 캐시된 센서 value 객체 반환 (없으면 기본 0값). */
  _read(type, index, prop) {
    this._subscribe(type, index, prop);
    const v = this._cache?.[type]?.[index]?.[prop];
    return v !== undefined ? v : { ...(DEFAULT_VALUES[type] || {}) };
  }

  _write(type, index, propertyNum, value) {
    this._ensureTransport();
    this.transport.send({
      type: MSG.SET_PROPERTY_REQUEST,
      data: { moduleType: type, index: Number(index), propertyNum, value },
    });
  }

  onChange(fn) {
    return this._change.subscribe(fn);
  }
  onModules(fn) {
    return this._modulesEmitter.subscribe(fn);
  }
  hasModule(type, index) {
    return this.modules.some(
      (m) => m.type === type && (m.fieldIndex === index || m.index === index),
    );
  }
  reset() {
    for (const a of this._active.values())
      a && a.timer && clearInterval(a.timer);
    this._active.clear();
    this._cache = {};
  }
}

/* ───────────────────────── 센서/액추에이터 접근 객체 ───────────────────────── */

// 센서 accessor 생성기 — SENSOR_ACCESSORS(필드명→키) + SENSOR_SCHEMA(키→prop)에서 균일 생성.
// MODI.tof(1).distance 처럼 동기 getter. 미연결 시 0. (멀티 propertyNum 센서도 키별 prop 자동.)
function makeAccessor(bridge, type) {
  const fields = SENSOR_ACCESSORS[type] || {};
  const schema = SENSOR_SCHEMA[type] || {};
  return (index) => {
    const obj = {};
    for (const [name, key] of Object.entries(fields)) {
      const prop = schema[key];
      Object.defineProperty(obj, name, {
        enumerable: true,
        get() {
          return bridge._read(type, index, prop)?.[key] ?? 0;
        },
      });
    }
    obj.raw = (prop) => bridge._read(type, index, prop);
    return obj;
  };
}

function buildPublicApi(bridge) {
  const api = {
    _bridge: bridge,
    MSG,
    TYPE,
    SENSOR_SCHEMA,
    WRITE_PROP,
    configure: (t) => bridge.configure(t),
    useMock: (o) => bridge.useMock(o),
    usePostMessage: (o) => bridge.usePostMessage(o),
    connect: (o) => bridge.connect(o),
    onChange: (fn) => bridge.onChange(fn),
    onModules: (fn) => bridge.onModules(fn),
    get modules() {
      return bridge.modules;
    },
    get connected() {
      return bridge.connected;
    },
    hasModule: (t, i) => bridge.hasModule(t, i),
    reset: () => bridge.reset(),
    Transport: { PostMessageTransport, MockTransport },
  };

  /* 센서 — 전부 SENSOR_SCHEMA/SENSOR_ACCESSORS 에서 균일 생성 */
  api.tof = makeAccessor(bridge, TYPE.TOF);
  api.button = makeAccessor(bridge, TYPE.BUTTON);
  api.dial = makeAccessor(bridge, TYPE.DIAL);
  api.joystick = makeAccessor(bridge, TYPE.JOYSTICK);
  api.env = makeAccessor(bridge, TYPE.ENVIRONMENT);
  api.imu = makeAccessor(bridge, TYPE.IMU);

  /* 액추에이터 */
  api.led = (index) => ({
    setColor: (r, g, b) =>
      bridge._write(TYPE.LED, index, WRITE_PROP.LED_RGB, { r, g, b }),
    off: () =>
      bridge._write(TYPE.LED, index, WRITE_PROP.LED_RGB, { r: 0, g: 0, b: 0 }),
  });
  const motor = (type) => (index) => ({
    setSpeed: (speed) =>
      bridge._write(type, index, WRITE_PROP.MOTOR_SPEED, { speed }),
    turnTo: (angle, speed = 70, mode = 0) =>
      bridge._write(type, index, WRITE_PROP.MOTOR_ANGLE, {
        angle,
        speed,
        mode,
      }),
    addAngle: (angle) =>
      bridge._write(type, index, WRITE_PROP.MOTOR_APPEND_ANGLE, {
        angle,
        speed: 70,
      }),
    stop: () => bridge._write(type, index, WRITE_PROP.MOTOR_STOP, {}),
  });
  api.motor = motor(TYPE.MOTOR_A);
  api.motorA = motor(TYPE.MOTOR_A);
  api.motorB = motor(TYPE.MOTOR_B);
  api.speaker = (index) => ({
    playTone: (frequency, volume = 50) =>
      bridge._write(TYPE.SPEAKER, index, WRITE_PROP.SPEAKER_TUNE, {
        frequency,
        volume,
      }),
    stop: () =>
      bridge._write(TYPE.SPEAKER, index, WRITE_PROP.SPEAKER_TUNE, {
        frequency: 0,
        volume: 0,
      }),
  });
  api.display = (index) => ({
    text: (stringData) =>
      bridge._write(TYPE.DISPLAY, index, WRITE_PROP.DISPLAY_TEXT, {
        stringData: String(stringData),
      }),
    clear: () =>
      bridge._write(TYPE.DISPLAY, index, WRITE_PROP.DISPLAY_RESET, {
        option: 1,
      }),
  });

  // 센서값 스트림 구독 — 들어오는 "모든" 값마다 cb(number, rawValueObj) 호출. (차트 피드용)
  // field 예: tof 'distance', dial 'turn'/'turnSpeed', env 'temperature'..., imu 'pitch'... (값 객체의 키)
  // 구독을 보장(fast 요청)하고, 해제 함수를 반환한다.
  api.onValue = (type, index, field, cb, opts = {}) => {
    // field 는 훅과 동일한 친숙명('x','y','distance','speed'…) 또는 raw 키 모두 허용 (SENSOR_ACCESSORS로 매핑).
    // opts.pollMs: 실시간 조작 입력은 낮게(예: 40). 미지정이면 기본 POLL_MS.
    const key = (SENSOR_ACCESSORS[type] || {})[field] || field;
    const prop = (SENSOR_SCHEMA[type] || {})[key] || 2;
    bridge._subscribe(type, index, prop, opts.pollMs);
    return bridge.onValueRaw((d) => {
      if (
        d.moduleType === type &&
        Number(d.index) === Number(index) &&
        d.propertyNum === prop
      ) {
        cb(d.value ? d.value[key] : undefined, d.value);
      }
    });
  };

  return api;
}

/* ───────────────────────── React 훅 (React 가 있을 때만) ───────────────────────── */

function attachReactHooks(api, React) {
  if (!React || !React.useSyncExternalStore) return api;
  const bridge = api._bridge;
  const sub = (cb) => bridge.onChange(cb);

  function useSnapshot(read) {
    // read() 가 매번 새 객체를 만들면 무한 렌더 → 원시값만 스냅샷.
    return React.useSyncExternalStore(sub, read, read);
  }

  api.useModi = () => {
    React.useSyncExternalStore(
      sub,
      () => bridge.modules.length,
      () => bridge.modules.length,
    );
    return { modules: bridge.modules, connected: bridge.connected };
  };
  api.useTof = (i) => useSnapshot(() => api.tof(i).distance);
  api.useButton = (i) => {
    const b = api.button(i);
    useSnapshot(
      () => `${b.clicked}|${b.pressed}|${b.toggled}|${b.doubleClicked}`,
    );
    return {
      clicked: b.clicked,
      pressed: b.pressed,
      toggled: b.toggled,
      doubleClicked: b.doubleClicked,
    };
  };
  api.useDial = (i) => {
    const d = api.dial(i);
    useSnapshot(() => `${d.turn}|${d.speed}`);
    return { turn: d.turn, speed: d.speed };
  };
  api.useJoystick = (i) => {
    const j = api.joystick(i);
    useSnapshot(() => `${j.x}|${j.y}|${j.direction}`);
    return { x: j.x, y: j.y, direction: j.direction };
  };
  api.useEnv = (i, field) => useSnapshot(() => api.env(i)[field]);
  api.useImu = (i, field) => useSnapshot(() => api.imu(i)[field]);
  // 임의 getter 구독용 (원시값 반환 함수 전달)
  api.useModiValue = (readFn) => useSnapshot(readFn);
  return api;
}

/* ───────────────────────── 인스턴스 생성 & 내보내기 ───────────────────────── */

const _bridge = new ModiBridge();
const MODI = buildPublicApi(_bridge);

// 전역 React 가 있으면 훅 자동 부착 (런타임 Babel iframe 환경).
if (typeof window !== "undefined" && window.React) {
  attachReactHooks(MODI, window.React);
}
// SDK 사용자가 직접 React 를 주입할 수도 있게 노출.
MODI.attachReact = (React) => attachReactHooks(MODI, React);

if (typeof window !== "undefined") {
  window.MODI = MODI;
  // 편의를 위해 훅들을 전역으로도 노출 (window.React 가 있을 때).
  if (window.React) {
    for (const h of [
      "useModi",
      "useTof",
      "useButton",
      "useDial",
      "useJoystick",
      "useEnv",
      "useImu",
      "useModiValue",
    ]) {
      if (MODI[h]) window[h] = MODI[h];
    }
  }
}

export default MODI;
export {
  MODI,
  ModiBridge,
  PostMessageTransport,
  MockTransport,
  MSG,
  TYPE,
  SENSOR_SCHEMA,
  WRITE_PROP,
  attachReactHooks,
};
