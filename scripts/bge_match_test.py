"""BGE-m3 검색 방식 비교 테스트 (로컬).

세 가지 매칭을 같은 패러프레이즈 질문으로 비교:
  A) alias 부분일치        (현재 프로토타입)
  B) 벡터·개념 프로토타입   (label+alias 임베딩)
  C) 벡터·청크 검색         (실제 학습노트 812개 임베딩 → 최근접 투표)  ← 운영 방식

C는 최초 1회 임베딩을 data/chunk_emb.npy 로 캐시.
"""

from __future__ import annotations

import json
import os

import numpy as np

from embed_bge import cosine, embed
from ontology_lib import connect, load_seed, match_concepts

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMB = os.path.join(BASE, "data", "chunk_emb.npy")
META = os.path.join(BASE, "data", "chunk_meta.json")

TESTS = [
    ("화면 여러 개를 복사해서 똑같이 쓰고 싶어요", "component_reuse"),
    ("로봇이 앞에 장애물 있으면 알아서 서게", "distance_sensing"),
    ("빙글빙글 돌리는 손잡이로 숫자 바꾸기", "dial_input"),
    ("스마트폰마다 게임 빠르기가 달라져요", "frame_independence"),
    ("스크롤 내려도 위 메뉴는 그대로 붙어있게", "sticky_header"),
    ("좋아요 누르면 하트 빨개지게", "button_input"),
    ("깜깜해지면 불이 켜지는 장치", "env_sensor"),
    ("숫자를 천 단위 콤마로 예쁘게", "number_format"),
]


def build_chunk_cache():
    conn = connect()
    rows = conn.execute(
        "SELECT chunk_id,title,content,concept_key,coding_type FROM chunks WHERE concept_key IS NOT NULL"
    ).fetchall()
    conn.close()
    meta = [dict(r) for r in rows]
    texts = [f"{r['title']}. {r['content'][:120]}" for r in meta]
    print(f"청크 {len(texts)}개 임베딩 중… (최초 1회, 캐시 저장)")
    vecs = embed(texts, batch=32)
    np.save(EMB, vecs)
    json.dump(meta, open(META, "w"), ensure_ascii=False)
    return vecs, meta


def load_chunk_cache():
    if os.path.exists(EMB) and os.path.exists(META):
        return np.load(EMB), json.load(open(META))
    return build_chunk_cache()


def main() -> None:
    concepts = load_seed()
    label = {c["key"]: c["label"] for c in concepts}

    xvec, meta = load_chunk_cache()
    # 개념 centroid = 그 개념에 배정된 청크 임베딩 평균 (방식 D)
    idx_by_key: dict[str, list[int]] = {}
    for j, r in enumerate(meta):
        idx_by_key.setdefault(r["concept_key"], []).append(j)
    ckeys = list(idx_by_key)
    cent = np.vstack([xvec[idx_by_key[k]].mean(0) for k in ckeys])
    cent /= np.linalg.norm(cent, axis=1, keepdims=True)
    print(f"개념 {len(ckeys)} · 청크 {len(meta)} (dim={xvec.shape[1]})\n")

    qvec = embed([q for q, _ in TESTS])
    hits = {"A": 0, "C": 0, "D": 0}
    print(f"{'질문':32s} {'정답':15s} {'A 부분일치':11s} {'C top5투표':15s} {'D centroid':15s}")
    print("-" * 100)
    for i, (q, gold) in enumerate(TESTS):
        sub = match_concepts(q, concepts, top=1)
        ak = sub[0][0] if sub else None
        xsim = cosine(qvec[i : i + 1], xvec)
        topidx = np.argsort(-xsim)[:5]
        vote: dict[str, float] = {}
        for j in topidx:
            vote[meta[j]["concept_key"]] = vote.get(meta[j]["concept_key"], 0.0) + float(xsim[j])
        ck = max(vote, key=vote.get)
        dk = ckeys[int(np.argmax(cosine(qvec[i : i + 1], cent)))]
        for k, v in (("A", ak), ("C", ck), ("D", dk)):
            hits[k] += v == gold
        def m(v):
            return ("✅" if v == gold else ("❌" if v else "✗")) + (label.get(v, "-")[:11] if v else "실패")
        print(f"{q[:30]:32s} {label[gold][:13]:15s} {m(ak):13s} {m(ck):17s} {m(dk):15s}")
        print("    └ 검색된 노트 top3: " + " | ".join(meta[int(j)]["title"][:20] for j in topidx[:3]))
    print("-" * 100)
    n = len(TESTS)
    print(f"정확도 →  A 부분일치 {hits['A']}/{n}   C top5투표 {hits['C']}/{n}   D centroid {hits['D']}/{n}")


if __name__ == "__main__":
    main()
