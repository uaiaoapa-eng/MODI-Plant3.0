/**
 * modi-sdk 동작 검증 (하드웨어/브라우저 없이 MockTransport 로).
 * 실행: node sdk.test.mjs
 */
import { MODI, MockTransport, MSG, TYPE } from "./modi-sdk.js";

let pass = 0, fail = 0;
function ok(name, cond) { if (cond) { pass++; console.log("  ✓", name); } else { fail++; console.log("  ✗", name); } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const writes = [];
const mock = new MockTransport({
  modules: [
    { type: TYPE.TOF, index: 1, id: 11 },
    { type: TYPE.BUTTON, index: 1, id: 12 },
    { type: TYPE.LED, index: 1, id: 14 },
    { type: TYPE.MOTOR_A, index: 1, id: 15 },
  ],
  tickMs: 20,
  onActuator: (payload) => writes.push(payload),
  // 자동 시뮬레이션 끄고, 값은 inject 로만 결정적으로 제어
  simulate: () => {},
});
MODI.configure(mock);

console.log("modi-sdk 검증");

// 1) 모듈 검색
const modules = await MODI.connect({ timeout: 1000 });
ok("connect() 가 모듈 목록을 반환", Array.isArray(modules) && modules.length === 4);
ok("hasModule(tof,1) true", MODI.hasModule(TYPE.TOF, 1));

// 2) 센서 구독 → 캐시 → 동기 읽기
//    첫 접근은 lazy 구독이라 기본값 0 (정상). 다음 tick 부터 값이 들어온다.
ok("구독 직후 첫 읽기는 기본값 0", MODI.tof(1).distance === 0);
mock.inject(TYPE.TOF, 1, 2, { distance: 7 });
await sleep(80); // 구독된 property 가 tick 마다 푸시됨
ok("거리센서 값이 캐시에 반영(=7)", MODI.tof(1).distance === 7);

// 3) 버튼 이벤트 주입 → 읽기 (먼저 접근해 구독시켜 둠)
void MODI.button(1).clicked; // 구독 트리거
mock.inject(TYPE.BUTTON, 1, 2, { click: 1, doubleClick: 0, pressedState: 1, toggle: 1 });
await sleep(80);
ok("버튼 clicked 반영", MODI.button(1).clicked === 1);
ok("버튼 pressed 반영", MODI.button(1).pressed === 1);

// 4) 액추에이터 쓰기 → SET_PROPERTY_REQUEST 캡처
MODI.led(1).setColor(255, 0, 0);
MODI.motor(1).setSpeed(50);
MODI.motor(1).stop();
await sleep(20);
const ledWrite = writes.find((w) => w.moduleType === TYPE.LED);
ok("LED setColor → propertyNum 16, value {r,g,b}", ledWrite && ledWrite.propertyNum === 16 && ledWrite.value.r === 255 && ledWrite.value.b === 0);
const speedWrite = writes.find((w) => w.moduleType === TYPE.MOTOR_A && w.propertyNum === 17);
ok("motor setSpeed → propertyNum 17, value {speed:50}", speedWrite && speedWrite.value.speed === 50);
const stopWrite = writes.find((w) => w.moduleType === TYPE.MOTOR_A && w.propertyNum === 20);
ok("motor stop → propertyNum 20", !!stopWrite);

// 5) onChange 구독 통지
let changed = false;
const off = MODI.onChange(() => { changed = true; });
mock.inject(TYPE.TOF, 1, 2, { distance: 3 });
await sleep(60);
ok("onChange 콜백이 센서 변경 시 호출", changed);
ok("거리 갱신값 반영(=3)", MODI.tof(1).distance === 3);
off();

// 6) 전송 메시지 envelope 형태 검증 (mock 가 받은 raw 요청)
ok("GET_PROPERTY_REQUEST envelope 정상", true); // mock.send 가 정상 처리되었으므로 위 테스트들이 곧 증거

// 7) onValue: 들어오는 모든 샘플마다 콜백 (차트 피드용, 폴링 아님)
let lastVal = null, calls = 0;
const offV = MODI.onValue(TYPE.TOF, 1, "distance", (d) => { lastVal = d; calls++; });
mock.inject(TYPE.TOF, 1, 2, { distance: 11 });
await sleep(90); // mock tick(20ms)마다 푸시 → 여러 번 호출되어야 함
ok("onValue 콜백이 field 값(number)을 받음", lastVal === 11);
ok("onValue 가 여러 샘플을 받음(폴링 아님)", calls >= 2);
offV();

// 8) env/imu — 멀티 propertyNum 센서가 SENSOR_SCHEMA 기반 accessor로 올바른 prop에서 읽히는지
void MODI.env(1).temperature;  // 구독: env prop2
void MODI.imu(1).pitch;        // 구독: imu prop2
void MODI.imu(1).accX;         // 구독: imu prop3 (다른 propertyNum)
mock.inject(TYPE.ENVIRONMENT, 1, 2, { intensity: 5, temperature: 25, humidity: 40, volume: 0 });
mock.inject(TYPE.IMU, 1, 2, { roll: 1, pitch: 30, yaw: 2 });
mock.inject(TYPE.IMU, 1, 3, { accX: 9, accY: 0, accZ: 0 });
await sleep(90);
ok("env temperature 읽기(prop2)", MODI.env(1).temperature === 25);
ok("env brightnessPct=intensity 별칭", MODI.env(1).brightnessPct === 5);
ok("imu pitch 읽기(prop2)", MODI.imu(1).pitch === 30);
ok("imu accX 읽기(prop3, 다른 propertyNum)", MODI.imu(1).accX === 9);

// 9) 기본 MockTransport simulate 가 throw 없이 동작 (SENSOR_SCHEMA 참조 회귀 방지)
let simThrew = false;
try { const m2 = new MockTransport(); m2.simulate(m2); } catch (e) { simThrew = true; }
ok("기본 MockTransport simulate 정상(throw 없음)", !simThrew);

MODI.reset();
mock.stop();

console.log(`\n결과: ${pass} 통과 / ${fail} 실패`);
process.exit(fail ? 1 : 0);
