"""저장된 세션 데이터 → 온톨로지 그래프(SQLite) 구성.

노드: concept(시드), modi_module(데이터). 청크: learning_note.
엣지: prerequisite(시드) / contains(coding_type→concept) / realized_by(concept→청크)
     / uses(청크→modi_module) / relates_to(같은 세션 공출현 concept 쌍).
개념 배정은 alias 부분일치(결정적, LLM 無). 미매칭은 리포트.

실행:  python scripts/build_ontology.py
"""

from __future__ import annotations

import glob
import itertools
import json
import os
from collections import Counter, defaultdict

import chunk_fields
from ontology_lib import DB_PATH, concept_levels, connect, load_seed, match_concepts

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def note_text(n) -> tuple[str, str]:
    if isinstance(n, dict):
        title = (n.get("title") or "").strip()
        body = " ".join(str(n.get(k, "")) for k in ("what", "why", "where"))
        return title, body
    if isinstance(n, str):
        return n.strip()[:80], n.strip()
    return "", ""


def main() -> None:
    concepts = load_seed()
    levels = concept_levels(concepts)
    by_key = {c["key"]: c for c in concepts}

    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE sessions(session_id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
                              coding_type TEXT, phase TEXT);
        CREATE TABLE chunks(chunk_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                            chunk_type TEXT, coding_type TEXT, title TEXT, content TEXT,
                            concept_key TEXT, intent TEXT, domain TEXT, difficulty TEXT,
                            modi_keys TEXT, payload TEXT);
        CREATE TABLE ontology_nodes(key TEXT PRIMARY KEY, node_type TEXT, label TEXT,
                                    level INTEGER, meta TEXT);
        CREATE TABLE ontology_edges(src TEXT, dst TEXT, rel TEXT, weight REAL,
                                    PRIMARY KEY(src,dst,rel));
        CREATE INDEX idx_edge_src ON ontology_edges(src, rel);
        CREATE INDEX idx_chunk_concept ON chunks(concept_key);
        """
    )

    # 1) concept 노드 (meta: 시드 alias/axes → 노드 부가정보, #44)
    for c in concepts:
        conn.execute(
            "INSERT INTO ontology_nodes(key,node_type,label,level,meta) VALUES(?,?,?,?,?)",
            (c["key"], "concept", c["label"], levels[c["key"]],
             json.dumps({"aliases": c.get("aliases", []), "axes": c.get("axes", []),
                         "difficulty": chunk_fields.derive_difficulty(levels[c["key"]])},
                        ensure_ascii=False)),
        )
    # 1b) axis(코딩축) 노드 — contains 엣지의 src(axis:blockly/hybrid/react)가
    #     노드로 존재하도록 등록(누락 시 contains src 가 dangling). node_type='axis'.
    axis_label = {"blockly": "블록코딩", "hybrid": "하이브리드", "react": "React"}
    for ax in sorted({a for c in concepts for a in c.get("axes", [])}):
        conn.execute(
            "INSERT OR IGNORE INTO ontology_nodes(key,node_type,label,level,meta) VALUES(?,?,?,?,?)",
            (f"axis:{ax}", "axis", axis_label.get(ax, ax), 0, None),
        )
    # 2) prerequisite / contains 엣지 (시드)
    for c in concepts:
        for p in c.get("prerequisite", []):
            if p in by_key:
                conn.execute(
                    "INSERT OR IGNORE INTO ontology_edges VALUES(?,?,?,?)",
                    (c["key"], p, "prerequisite", 1.0),
                )
        for ax in c.get("axes", []):
            conn.execute(
                "INSERT OR IGNORE INTO ontology_edges VALUES(?,?,?,?)",
                (f"axis:{ax}", c["key"], "contains", 1.0),
            )

    # 3) 세션 순회 → 청크 + 개념 배정 + modi
    total_notes = assigned = 0
    total_code = total_design = 0
    per_concept = Counter()
    modi_seen: set[str] = set()
    cooccur: Counter = Counter()
    unassigned_samples: list[str] = []

    for f in glob.glob(os.path.join(BASE, "projects", "*", "session_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        sid = d.get("session_id") or os.path.basename(f)
        ct = d.get("coding_type", "")
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)",
            (sid, d.get("user_id", ""), d.get("title", ""), ct, d.get("phase", "")),
        )
        session_concepts: set[str] = set()
        # 세션 하드웨어 연계(#44): 이 세션 청크들이 공유하는 MODI 모듈 key.
        mk_json = json.dumps(chunk_fields.modi_module_keys(d.get("modi_modules")),
                             ensure_ascii=False)
        domain = chunk_fields.derive_domain(ct)
        intent = chunk_fields.derive_intent("learning_note")

        for n in d.get("learning_notes") or []:
            title, body = note_text(n)
            if not title:
                continue
            total_notes += 1
            m = match_concepts(title + " " + body, concepts, top=1)
            ck = m[0][0] if m else None
            difficulty = chunk_fields.derive_difficulty(levels.get(ck) if ck else None)
            payload = json.dumps(chunk_fields.note_payload(n, coding_type=ct, concept_key=ck),
                                 ensure_ascii=False)
            cur = conn.execute(
                "INSERT INTO chunks(session_id,chunk_type,coding_type,title,content,concept_key,"
                "intent,domain,difficulty,modi_keys,payload)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "learning_note", ct, title, body[:500], ck,
                 intent, domain, difficulty, mk_json, payload),
            )
            if ck:
                assigned += 1
                per_concept[ck] += 1
                session_concepts.add(ck)
                # realized_by: concept → chunk
                conn.execute(
                    "INSERT OR IGNORE INTO ontology_edges VALUES(?,?,?,?)",
                    (ck, str(cur.lastrowid), "realized_by", 1.0),
                )
            elif len(unassigned_samples) < 25:
                unassigned_samples.append(title)

        # #3(#44): 기존 세션의 생성 코드·설계문서도 재사용 청크로 추출 → 콜드스타트 없이
        # 기존 데이터만으로 재사용 코퍼스를 채운다(빌드 턴 재사용의 실질 재료).
        dd = d.get("design_doc") if isinstance(d.get("design_doc"), dict) else None
        goal = ((dd.get("description") or dd.get("project_name")) if dd else "") or d.get("title") or ""
        goal = goal.strip()
        gc = d.get("generated_code") or {}
        if isinstance(gc, dict) and gc:
            files = list(gc.keys())
            ctitle = ("코드: " + (goal[:40] if goal else ", ".join(files[:2])))[:200]
            ccontent = (goal + " " + " ".join(files)).strip()[:500] or ctitle
            m = match_concepts(goal + " " + " ".join(files), concepts, top=1)
            cck = m[0][0] if m else None
            cur = conn.execute(
                "INSERT INTO chunks(session_id,chunk_type,coding_type,title,content,concept_key,"
                "intent,domain,difficulty,modi_keys,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "code", ct, ctitle, ccontent, cck,
                 chunk_fields.derive_intent("code"), domain,
                 chunk_fields.derive_difficulty(levels.get(cck) if cck else None), mk_json,
                 json.dumps(chunk_fields.code_payload(gc), ensure_ascii=False)),
            )
            total_code += 1
            if cck:
                conn.execute("INSERT OR IGNORE INTO ontology_edges VALUES(?,?,?,?)",
                             (cck, str(cur.lastrowid), "realized_by", 1.0))
        if dd and (dd.get("features") or dd.get("pages")):
            dtitle = ("설계: " + (dd.get("project_name") or goal or "프로젝트"))[:200]
            parts = [dd.get("description") or ""]
            parts += [f.get("name", "") for f in (dd.get("features") or [])]
            parts += [p.get("name", "") for p in (dd.get("pages") or [])]
            dcontent = " ".join(x for x in parts if x).strip()[:500] or dtitle
            m = match_concepts(dcontent, concepts, top=1)
            dck = m[0][0] if m else None
            cur = conn.execute(
                "INSERT INTO chunks(session_id,chunk_type,coding_type,title,content,concept_key,"
                "intent,domain,difficulty,modi_keys,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "design_doc", ct, dtitle, dcontent, dck,
                 chunk_fields.derive_intent("design_doc"), domain,
                 chunk_fields.derive_difficulty(levels.get(dck) if dck else None), mk_json,
                 json.dumps(chunk_fields.design_doc_payload(dd), ensure_ascii=False)),
            )
            total_design += 1
            if dck:
                conn.execute("INSERT OR IGNORE INTO ontology_edges VALUES(?,?,?,?)",
                             (dck, str(cur.lastrowid), "realized_by", 1.0))

        # modi 노드 + uses 엣지
        mm = d.get("modi_modules") or {}
        if isinstance(mm, dict):
            for m in mm.get("modules") or []:
                if isinstance(m, dict) and m.get("key"):
                    mk = f"modi:{m['key']}"
                    if mk not in modi_seen:
                        conn.execute(
                            "INSERT OR IGNORE INTO ontology_nodes(key,node_type,label,level,meta)"
                            " VALUES(?,?,?,?,?)",
                            (mk, "modi_module", m.get("role") or m["key"], 0,
                             json.dumps({"key": m["key"], "role": m.get("role")},
                                        ensure_ascii=False)),
                        )
                        modi_seen.add(mk)
                    for ck in session_concepts:
                        conn.execute(
                            "INSERT OR IGNORE INTO ontology_edges VALUES(?,?,?,?)",
                            (ck, mk, "uses", 1.0),
                        )
        # relates_to 공출현
        for a, b in itertools.combinations(sorted(session_concepts), 2):
            cooccur[(a, b)] += 1

    # 4) relates_to 엣지 (양방향)
    for (a, b), w in cooccur.items():
        if w >= 2:  # 노이즈 제거: 2세션 이상 공출현만
            conn.execute("INSERT OR IGNORE INTO ontology_edges VALUES(?,?,?,?)", (a, b, "relates_to", float(w)))
            conn.execute("INSERT OR IGNORE INTO ontology_edges VALUES(?,?,?,?)", (b, a, "relates_to", float(w)))

    conn.commit()

    # 5) 리포트
    ec = defaultdict(int)
    for (r,) in conn.execute("SELECT rel FROM ontology_edges").fetchall():
        ec[r] += 1
    print("=" * 56)
    print(f"온톨로지 빌드 완료 → {DB_PATH}")
    print(f"개념 노드: {len(concepts)} · MODI 노드: {len(modi_seen)}")
    print(f"학습노트 청크: {total_notes} · 개념배정: {assigned} "
          f"({assigned*100//max(total_notes,1)}%) · 미배정: {total_notes-assigned}")
    print(f"재사용 청크(#3): code {total_code} · design_doc {total_design} "
          f"(기존 세션 결과물 → 빌드 재사용 재료)")
    print("엣지:", dict(ec))
    print("-" * 56)
    print("개념별 청크 수 (상위 20):")
    for k, v in per_concept.most_common(20):
        print(f"  {v:4d}  {k:22s} L{levels[k]}  {by_key[k]['label']}")
    if unassigned_samples:
        print("-" * 56)
        print("미배정 제목 샘플 (alias 보강 후보):")
        for t in unassigned_samples[:15]:
            print("   -", t)
    conn.close()


if __name__ == "__main__":
    main()
