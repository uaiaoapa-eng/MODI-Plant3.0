"""무료(BGE-m3, 로컬) vs 유료 최고(Gemini embedding, API) 실측 비교.

동일 코퍼스(812 학습노트)·동일 centroid 방식·동일 패러프레이즈 질문 8개.
Gemini 임베딩은 data/chunk_emb_gemini.npy 로 캐시(최초 1회만 API 호출).
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request

import numpy as np
from dotenv import load_dotenv

from bge_match_test import TESTS, load_chunk_cache
from embed_bge import cosine as bcos
from embed_bge import embed as bge_embed
from ontology_lib import load_seed

load_dotenv()
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEMB = os.path.join(BASE, "data", "chunk_emb_gemini.npy")
KEY = os.getenv("GEMINI_API_KEY")
try:
    import certifi

    CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl._create_unverified_context()
URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key=" + (KEY or "")


def gemini_embed(texts: list[str], task: str, sleep: float = 0.45) -> np.ndarray:
    """단일 embedContent 호출 + 스로틀 (batch 엔드포인트는 무료쿼터 429)."""
    out = []
    for i, t in enumerate(texts):
        body = json.dumps({"model": "models/gemini-embedding-001",
                           "content": {"parts": [{"text": t[:2000]}]}, "taskType": task}).encode()
        for attempt in range(6):
            try:
                req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
                r = json.load(urllib.request.urlopen(req, timeout=60, context=CTX))
                out.append(r["embedding"]["values"])
                break
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(3 * (attempt + 1))
        if (i + 1) % 50 == 0:
            print(f"  gemini {i+1}/{len(texts)}")
        time.sleep(sleep)
    v = np.array(out, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def centroids(vecs, meta):
    idx = {}
    for j, r in enumerate(meta):
        idx.setdefault(r["concept_key"], []).append(j)
    keys = list(idx)
    cen = np.vstack([vecs[idx[k]].mean(0) for k in keys])
    return keys, cen / np.linalg.norm(cen, axis=1, keepdims=True)


def main() -> None:
    concepts = load_seed()
    label = {c["key"]: c["label"] for c in concepts}
    bvec, meta = load_chunk_cache()
    texts = [f"{r['title']}. {r['content'][:120]}" for r in meta]

    if os.path.exists(GEMB):
        gvec = np.load(GEMB)
    else:
        print(f"Gemini로 청크 {len(texts)}개 임베딩 (최초 1회)…")
        gvec = gemini_embed(texts, "RETRIEVAL_DOCUMENT")
        np.save(GEMB, gvec)
    print(f"BGE-m3 dim={bvec.shape[1]} · Gemini dim={gvec.shape[1]}\n")

    bkeys, bcen = centroids(bvec, meta)
    gkeys, gcen = centroids(gvec, meta)
    qs = [q for q, _ in TESTS]
    bq = bge_embed(qs)
    gq = gemini_embed(qs, "RETRIEVAL_QUERY")

    hb = hg = 0
    print(f"{'질문':32s} {'정답':16s} {'무료 BGE-m3':18s} {'유료 Gemini':18s}")
    print("-" * 92)
    for i, (q, gold) in enumerate(TESTS):
        bk = bkeys[int(np.argmax(bcos(bq[i:i+1], bcen)))]
        gk = gkeys[int(np.argmax((gq[i:i+1] @ gcen.T).ravel()))]
        hb += bk == gold
        hg += gk == gold
        def m(v):
            return ("✅" if v == gold else "❌") + label.get(v, "-")[:13]
        print(f"{q[:30]:32s} {label[gold][:14]:16s} {m(bk):18s} {m(gk):18s}")
    print("-" * 92)
    n = len(TESTS)
    print(f"정확도 →  무료 BGE-m3: {hb}/{n}   |   유료 Gemini: {hg}/{n}")


if __name__ == "__main__":
    main()
