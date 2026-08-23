"""온톨로지 그래프 → 자체완결 인터랙티브 HTML (외부 의존 없음).

concept 노드(레벨별 색) + prerequisite(방향 실선) + relates_to(가중 실선).
vanilla JS 포스 레이아웃. 노드 클릭 → 이웃 강조.
실행: python scripts/export_ontology_graph.py  →  data/ontology_graph.html
"""

from __future__ import annotations

import json
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "ontology.db")
OUT = os.path.join(BASE, "data", "ontology_graph.html")

REL_MIN = 3  # relates_to 노이즈 컷


def main() -> None:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    nodes = [
        {"id": r["key"], "label": r["label"], "level": r["level"]}
        for r in c.execute("SELECT key,label,level FROM ontology_nodes WHERE node_type='concept'")
    ]
    deg = {n["id"]: 0 for n in nodes}
    edges = []
    for r in c.execute("SELECT src,dst,rel,weight FROM ontology_edges WHERE rel IN ('prerequisite','relates_to')"):
        if r["rel"] == "relates_to" and r["weight"] < REL_MIN:
            continue
        if r["src"] in deg and r["dst"] in deg:
            edges.append({"s": r["src"], "t": r["dst"], "rel": r["rel"], "w": r["weight"]})
            deg[r["src"]] += 1
            deg[r["dst"]] += 1
    for n in nodes:
        n["deg"] = deg[n["id"]]
    # 개념별 청크 수 (노드 크기)
    for r in c.execute("SELECT concept_key k,count(*) n FROM chunks WHERE concept_key IS NOT NULL GROUP BY concept_key"):
        for n in nodes:
            if n["id"] == r["k"]:
                n["chunks"] = r["n"]
    c.close()
    data = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)

    html = _HTML.replace("__DATA__", data)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"그래프 HTML 생성: {OUT}")
    print(f"  concept 노드 {len(nodes)} · 표시 엣지 {len(edges)} (prerequisite + relates_to≥{REL_MIN})")


_HTML = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>edu-agent 온톨로지 토폴로지</title>
<style>
 html,body{margin:0;height:100%;background:#0f1420;color:#e6ecf7;font-family:system-ui,"Apple SD Gothic Neo",sans-serif;overflow:hidden}
 #hud{position:fixed;top:10px;left:12px;z-index:10;font-size:12px;background:rgba(26,34,51,.85);border:1px solid #2a3550;border-radius:8px;padding:10px 12px;max-width:280px}
 #hud b{font-size:13px} .lg{margin-top:6px;color:#8ea1c4;line-height:1.7}
 .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
 canvas{display:block}
 #sel{position:fixed;bottom:12px;left:12px;z-index:10;font-size:12px;background:rgba(26,34,51,.9);border:1px solid #2a3550;border-radius:8px;padding:10px 12px;max-width:320px;display:none}
</style></head><body>
<div id="hud"><b>🧩 온톨로지 토폴로지</b>
 <div class="lg" id="stat"></div>
 <div class="lg">실선 화살표 = 선수학습 · 얇은 선 = 연관(공출현)<br>노드 클릭 → 이웃 강조 · 드래그 이동</div>
 <div class="lg">레벨: <span class="sw" style="background:#39d98a"></span>L0
  <span class="sw" style="background:#5b9dff"></span>L1
  <span class="sw" style="background:#b98cff"></span>L2
  <span class="sw" style="background:#ffb454"></span>L3
  <span class="sw" style="background:#ff6b8a"></span>L4+</div></div>
<div id="sel"></div>
<canvas id="cv"></canvas>
<script>
const G=__DATA__;
const COL=["#39d98a","#5b9dff","#b98cff","#ffb454","#ff6b8a"];
const cv=document.getElementById("cv"),ctx=cv.getContext("2d");
let W,H;function resize(){W=cv.width=innerWidth;H=cv.height=innerHeight}resize();addEventListener("resize",resize);
document.getElementById("stat").textContent=`개념 ${G.nodes.length}개 · 엣지 ${G.edges.length}개`;
const N=G.nodes,byId={};N.forEach((n,i)=>{n.x=W/2+Math.cos(i)*200*Math.random();n.y=H/2+Math.sin(i)*200*Math.random();n.vx=0;n.vy=0;byId[n.id]=n});
const E=G.edges.map(e=>({s:byId[e.s],t:byId[e.t],rel:e.rel,w:e.w}));
const adj={};N.forEach(n=>adj[n.id]=new Set());E.forEach(e=>{adj[e.s.id].add(e.t.id);adj[e.t.id].add(e.s.id)});
let sel=null,drag=null,ox=0,oy=0;
function step(){
 for(const a of N){a.fx=(W/2-a.x)*0.0015;a.fy=(H/2-a.y)*0.0015}
 for(let i=0;i<N.length;i++)for(let j=i+1;j<N.length;j++){const a=N[i],b=N[j];let dx=a.x-b.x,dy=a.y-b.y,d=Math.hypot(dx,dy)||1;const f=1600/(d*d);a.fx+=dx/d*f;a.fy+=dy/d*f;b.fx-=dx/d*f;b.fy-=dy/d*f}
 for(const e of E){const k=e.rel==="prerequisite"?0.02:0.006;let dx=e.t.x-e.s.x,dy=e.t.y-e.s.y,d=Math.hypot(dx,dy)||1,L=e.rel==="prerequisite"?90:150;const f=(d-L)*k;e.s.fx+=dx/d*f;e.s.fy+=dy/d*f;e.t.fx-=dx/d*f;e.t.fy-=dy/d*f}
 for(const a of N){if(a===drag)continue;a.vx=(a.vx+a.fx)*0.85;a.vy=(a.vy+a.fy)*0.85;a.x+=a.vx;a.y+=a.vy}
}
function rad(n){return 5+Math.sqrt(n.chunks||1)*1.6}
function draw(){
 ctx.clearRect(0,0,W,H);
 for(const e of E){const hl=sel&&(e.s.id===sel||e.t.id===sel);
  ctx.strokeStyle=e.rel==="prerequisite"?(hl?"#cfe0ff":"rgba(120,140,180,.35)"):(hl?"rgba(255,180,84,.5)":"rgba(90,110,150,.12)");
  ctx.lineWidth=e.rel==="prerequisite"?1.4:0.8;ctx.beginPath();ctx.moveTo(e.s.x,e.s.y);ctx.lineTo(e.t.x,e.t.y);ctx.stroke();
  if(e.rel==="prerequisite"){const a=Math.atan2(e.t.y-e.s.y,e.t.x-e.s.x),r=rad(e.t)+3,tx=e.t.x-Math.cos(a)*r,ty=e.t.y-Math.sin(a)*r;
   ctx.fillStyle=hl?"#cfe0ff":"rgba(120,140,180,.5)";ctx.beginPath();ctx.moveTo(tx,ty);ctx.lineTo(tx-Math.cos(a-.4)*6,ty-Math.sin(a-.4)*6);ctx.lineTo(tx-Math.cos(a+.4)*6,ty-Math.sin(a+.4)*6);ctx.fill()}}
 for(const n of N){const dim=sel&&n.id!==sel&&!adj[sel].has(n.id);ctx.globalAlpha=dim?0.25:1;
  ctx.fillStyle=COL[Math.min(n.level,4)];ctx.beginPath();ctx.arc(n.x,n.y,rad(n),0,7);ctx.fill();
  if(!dim&&(n.deg>4||n.id===sel||rad(n)>10)){ctx.globalAlpha=1;ctx.fillStyle="#e6ecf7";ctx.font="11px system-ui";ctx.fillText(n.label,n.x+rad(n)+3,n.y+3)}}
 ctx.globalAlpha=1;
}
function loop(){step();draw();requestAnimationFrame(loop)}loop();
function pick(x,y){let best=null,bd=1e9;for(const n of N){const d=Math.hypot(n.x-x,n.y-y);if(d<rad(n)+6&&d<bd){bd=d;best=n}}return best}
cv.addEventListener("mousedown",e=>{const n=pick(e.clientX,e.clientY);if(n){drag=n;sel=n.id;ox=e.clientX-n.x;oy=e.clientY-n.y;showSel(n)}else sel=null});
addEventListener("mousemove",e=>{if(drag){drag.x=e.clientX-ox;drag.y=e.clientY-oy;drag.vx=drag.vy=0}});
addEventListener("mouseup",()=>drag=null);
function showSel(n){const d=document.getElementById("sel");const nb=[...adj[n.id]].map(id=>byId[id].label);
 d.style.display="block";d.innerHTML=`<b>${n.label}</b> · L${n.level} · 청크 ${n.chunks||0}개<br><span style="color:#8ea1c4">이웃 ${nb.length}: ${nb.slice(0,8).join(", ")}${nb.length>8?"…":""}</span>`}
</script></body></html>"""


if __name__ == "__main__":
    main()
