"""BGE-m3 dense 임베딩 (transformers 직접, FlagEmbedding 불필요).

BGE-m3 dense = CLS 토큰 풀링 + L2 정규화. 한국어·다국어·1024차원.
최초 1회 모델(~2.3GB) 다운로드 후 캐시.

용도: alias 부분일치를 대체하는 의미 기반 개념 매칭.
"""

from __future__ import annotations

import functools

import numpy as np

MODEL_NAME = "BAAI/bge-m3"
_MODEL = None
_TOK = None


def _load():
    global _MODEL, _TOK
    if _MODEL is None:
        import torch  # noqa
        from transformers import AutoModel, AutoTokenizer

        _TOK = AutoTokenizer.from_pretrained(MODEL_NAME)
        _MODEL = AutoModel.from_pretrained(MODEL_NAME)
        _MODEL.eval()
    return _MODEL, _TOK


def embed(texts: list[str], batch: int = 16) -> np.ndarray:
    """텍스트 리스트 → (N,1024) 정규화 임베딩."""
    import torch

    model, tok = _load()
    out = []
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        enc = tok(chunk, padding=True, truncation=True, max_length=512, return_tensors="pt")
        with torch.no_grad():
            hid = model(**enc).last_hidden_state[:, 0]  # CLS
            hid = torch.nn.functional.normalize(hid, p=2, dim=1)
        out.append(hid.cpu().numpy())
    return np.vstack(out)


@functools.lru_cache(maxsize=4096)
def embed_one(text: str) -> tuple:
    return tuple(embed([text])[0].tolist())


def cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """정규화 가정 → 내적 = 코사인. a:(1,d) b:(N,d) → (N,)"""
    return (a @ b.T).ravel()
