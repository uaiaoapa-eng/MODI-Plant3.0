"""강원도 논·밭 피해 동물 카탈로그 (30종).

카메라 인식 클래스의 단일 출처(single source of truth)다.
- detector.py 는 `prompts`(영어 문구)로 제로샷 인식(YOLO-World)을 한다.
- train.py 는 이 순서(index)대로 커스텀 학습용 클래스 번호를 만든다.

risk: 3=즉시 대응(대형·안전 위협), 2=주요 작물 피해, 1=국지적·경미 피해
protected: 멸종위기 등 보호종 — 포획·위해 금지, 기록·알림·비살상 퇴치만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Animal:
    key: str            # 영문 식별자 (학습 클래스명)
    ko: str             # 한국어 이름
    group: str          # mammal | bird
    risk: int           # 1~3
    damage: str         # 주요 피해
    season: str         # 주 출현 시기
    prompts: tuple[str, ...]  # 제로샷 인식용 영어 문구 (첫 번째가 대표)
    protected: bool = False


ANIMALS: tuple[Animal, ...] = (
    # ── 대형 포유류 ────────────────────────────────────────────────
    Animal("wild_boar", "멧돼지", "mammal", 3,
           "벼·옥수수·감자·고구마 파헤침, 논둑 붕괴, 사람 공격 위험", "연중 (7~11월 최다)",
           ("wild boar", "boar", "pig")),
    Animal("water_deer", "고라니", "mammal", 2,
           "콩·고구마·배추 순 뜯어먹음, 어린 벼 짓밟음", "연중 (4~6월 새순기 최다)",
           ("water deer", "small deer", "deer")),
    Animal("roe_deer", "노루", "mammal", 2,
           "새순·콩잎 섭식, 산간 밭 피해", "연중 (봄·가을)",
           ("roe deer", "deer with antlers")),
    Animal("black_bear", "반달가슴곰", "mammal", 3,
           "옥수수·과수·양봉 피해, 사람 안전 위협", "4~11월",
           ("asiatic black bear", "bear"), protected=True),
    # ── 중소형 포유류 ──────────────────────────────────────────────
    Animal("raccoon_dog", "너구리", "mammal", 2,
           "옥수수·수박·참외·고구마 섭식", "연중 (여름~가을)",
           ("raccoon dog", "raccoon")),
    Animal("badger", "오소리", "mammal", 2,
           "땅 파헤침, 고구마·옥수수·땅콩 피해", "3~11월",
           ("badger",)),
    Animal("hare", "멧토끼", "mammal", 1,
           "콩·배추 새싹 갉아먹음", "연중",
           ("hare", "rabbit")),
    Animal("red_squirrel", "청설모", "mammal", 1,
           "잣·밤·호두·옥수수 피해", "8~10월",
           ("red squirrel", "squirrel")),
    Animal("chipmunk", "다람쥐", "mammal", 1,
           "파종 종자·콩·옥수수 섭식", "4~10월",
           ("chipmunk",)),
    Animal("field_mouse", "등줄쥐(들쥐)", "mammal", 2,
           "볏단·낟알·고구마 섭식, 쯔쯔가무시·유행성출혈열 매개", "가을 수확기",
           ("field mouse", "mouse")),
    Animal("brown_rat", "시궁쥐(집쥐)", "mammal", 1,
           "저장 곡물·창고 피해", "연중",
           ("rat",)),
    Animal("mole", "두더지", "mammal", 1,
           "땅굴로 뿌리 들뜸, 논둑 누수", "봄·가을",
           ("mole", "molehill")),
    Animal("weasel", "족제비", "mammal", 1,
           "닭·오리 등 가금 피해", "연중",
           ("weasel",)),
    Animal("marten", "담비(노란목도리담비)", "mammal", 1,
           "과수·양봉 피해", "연중",
           ("yellow-throated marten", "marten"), protected=True),
    Animal("leopard_cat", "삵", "mammal", 1,
           "가금 피해", "연중 (야간)",
           ("leopard cat", "wild cat"), protected=True),
    Animal("feral_cat", "들고양이", "mammal", 1,
           "가금 피해, 하우스 시설 훼손", "연중",
           ("cat",)),
    Animal("feral_dog", "들개", "mammal", 2,
           "가축 피해, 사람 위협, 밭 짓밟음", "연중",
           ("dog",)),
    # ── 조류 ───────────────────────────────────────────────────────
    Animal("sparrow", "참새", "bird", 2,
           "벼 이삭(유숙기) 낟알 쪼아먹음 — 논 피해 1순위 조류", "8~10월",
           ("sparrow", "small brown bird")),
    Animal("magpie", "까치", "bird", 2,
           "과일·옥수수·콩 쪼아먹음, 비닐 훼손", "연중",
           ("magpie",)),
    Animal("large_billed_crow", "큰부리까마귀", "bird", 2,
           "파종 종자·옥수수·과일 피해", "연중",
           ("crow", "black crow")),
    Animal("rook", "떼까마귀", "bird", 2,
           "보리·밀·벼 낟알, 새싹 떼지어 섭식", "11~3월 (월동)",
           ("rook", "flock of crows")),
    Animal("jay", "어치", "bird", 1,
           "콩·옥수수·도토리 피해", "가을",
           ("eurasian jay", "jay")),
    Animal("bulbul", "직박구리", "bird", 1,
           "과일·배추 잎 쪼아먹음", "연중",
           ("brown-eared bulbul", "bulbul")),
    Animal("turtle_dove", "멧비둘기", "bird", 2,
           "콩·팥 파종 직후 종자·떡잎 섭식", "연중 (5~6월 파종기)",
           ("turtle dove", "dove")),
    Animal("pigeon", "집비둘기", "bird", 1,
           "종자·곡물 섭식, 분변 오염", "연중",
           ("pigeon",)),
    Animal("pheasant", "꿩", "bird", 2,
           "파종 종자·옥수수·콩 새싹 섭식", "연중 (봄 파종기)",
           ("pheasant",)),
    Animal("mallard", "청둥오리", "bird", 2,
           "이앙 직후 어린 벼 짓밟음·볍씨 섭식", "10~4월 (월동)",
           ("mallard", "duck")),
    Animal("spot_billed_duck", "흰뺨검둥오리", "bird", 2,
           "어린 벼·볍씨 섭식, 논 짓밟음", "연중",
           ("spot-billed duck", "duck")),
    Animal("goose", "기러기(쇠기러기)", "bird", 1,
           "보리·밀 새싹, 수확 후 논 낟알 섭식", "10~3월 (월동)",
           ("wild goose", "goose")),
    Animal("heron", "왜가리·백로", "bird", 1,
           "이앙 직후 논 짓밟음, 양어장 피해", "4~10월",
           ("grey heron", "egret", "heron")),
)

# 오탐 방지용 '배경' 클래스 — 인식되면 무시한다(농민·차량을 멧돼지·곰으로 착각하지 않도록).
IGNORE_PROMPTS: tuple[str, ...] = ("person", "car", "tractor")

BY_KEY: dict[str, Animal] = {a.key: a for a in ANIMALS}
