#!/usr/bin/env python3
"""Nginx Proxy Manager 자동 등록 — edu-agent.luxrobo.net

필수 — NPM 관리자 로그인 2개만:
  NPM_EMAIL, NPM_PASS
    → scripts/npm.env 파일에 적어두면 매번 export 안 해도 됨 (git 제외됨)

선택 (안 주면 알아서 처리):
  AUTH_USER                  : 도메인 기본인증 아이디 (기본 'edu')
  AUTH_PASS                  : 도메인 기본인증 비번 (안 주면 자동생성 후 화면 출력)
  LE_EMAIL                   : Let's Encrypt 동의 이메일 (기본 walter.jung@luxrobo.com)
  DOMAIN (기본 edu-agent.luxrobo.net), FWD_HOST(192.168.0.102), FWD_PORT(18080)
  ENABLE_SSL (기본 true)
"""
import os
import sys
import json
import secrets
import urllib.request
import urllib.error

# scripts/npm.env 가 있으면 먼저 로드 (이미 export 된 환경변수가 우선).
_envfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "npm.env")
if os.path.exists(_envfile):
    with open(_envfile, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

NPM = "http://192.168.0.102:81/api"
DOMAIN   = os.environ.get("DOMAIN", "edu-agent.luxrobo.net")
FWD_HOST = os.environ.get("FWD_HOST", "192.168.0.102")
FWD_PORT = int(os.environ.get("FWD_PORT", "18080"))
LE_EMAIL = os.environ.get("LE_EMAIL", "walter.jung@luxrobo.com")
ENABLE_SSL = os.environ.get("ENABLE_SSL", "true").lower() == "true"

def need(k):
    v = os.environ.get(k)
    if not v:
        sys.exit(f"[!] 환경변수 {k} 가 필요합니다. (예: export {k}=...)")
    return v

def req(method, path, body=None, token=None):
    url = NPM + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"[!] {method} {path} -> HTTP {e.code}: {e.read().decode()[:500]}")

def main():
    email, pw = need("NPM_EMAIL"), need("NPM_PASS")
    # 접속 계정은 선택 — 안 주면 기본 아이디 'edu' + 자동생성 비번
    auth_user = os.environ.get("AUTH_USER", "edu")
    auth_pass = os.environ.get("AUTH_PASS") or secrets.token_urlsafe(12)
    auto_pw = "AUTH_PASS" not in os.environ

    print(f"[1/5] 토큰 발급 ({email}) ...")
    token = req("POST", "/tokens", {"identity": email, "secret": pw})["token"]
    print("      OK")

    # --- Access List (기본인증) : 같은 이름 있으면 재사용 ---
    print("[2/5] Access List 확인/생성 ...")
    lists = req("GET", "/nginx/access-lists?expand=items", token=token)
    al = next((x for x in lists if x["name"] == "edu-agent"), None)
    created_al = al is None
    if al:
        al_id = al["id"]
        print(f"      기존 Access List 재사용 (id={al_id}) — 계정/비번은 기존 값 유지")
    else:
        al = req("POST", "/nginx/access-lists", {
            "name": "edu-agent",
            # NPM v2.14: satisfy_any=false + 빈 client 목록은 deny-all(403)이 됨.
            # 기본인증만으로 통과시키려면 satisfy_any=true 여야 401(로그인창)이 뜬다.
            "satisfy_any": True,
            "pass_auth": False,
            "items": [{"username": auth_user, "password": auth_pass}],
            "clients": [],
        }, token=token)
        al_id = al["id"]
        print(f"      생성됨 (id={al_id}, user={auth_user})")

    # --- 기존 프록시 호스트 있나 확인 ---
    print("[3/5] 프록시 호스트 확인 ...")
    hosts = req("GET", "/nginx/proxy-hosts", token=token)
    existing = next((h for h in hosts if DOMAIN in h.get("domain_names", [])), None)

    host_body = {
        "domain_names": [DOMAIN],
        "forward_scheme": "http",
        "forward_host": FWD_HOST,
        "forward_port": FWD_PORT,
        "access_list_id": al_id,
        "certificate_id": 0,
        "ssl_forced": False,
        "http2_support": False,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "caching_enabled": False,
        "block_exploits": True,
        "allow_websocket_upgrade": True,  # SSE/스트리밍(/chat) 대비
        "advanced_config": "",
        "locations": [],
        "meta": {"letsencrypt_agree": False, "dns_challenge": False},
    }

    if existing:
        host_id = existing["id"]
        req("PUT", f"/nginx/proxy-hosts/{host_id}", host_body, token=token)
        print(f"      기존 호스트 업데이트 (id={host_id})")
    else:
        h = req("POST", "/nginx/proxy-hosts", host_body, token=token)
        host_id = h["id"]
        print(f"      생성됨 (id={host_id}) {DOMAIN} -> {FWD_HOST}:{FWD_PORT}")

    def creds_line():
        if created_al and auto_pw:
            return f"\n  ⚠️ 접속 계정 (자동생성, 지금 저장해두세요):\n     아이디: {auth_user}\n     비번  : {auth_pass}"
        return f"\n  접속 계정: {auth_user}"

    if not ENABLE_SSL:
        print("[4/5] SSL 건너뜀 (ENABLE_SSL=false) — HTTP 프록시만 등록 완료.")
        print(f"\n[완료] http://{DOMAIN}{creds_line()}")
        return

    # --- Let's Encrypt 인증서 : 도메인용이 이미 있으면 재사용(중복발급/레이트리밋 방지) ---
    print(f"[4/5] 인증서 확인/발급 ({DOMAIN}) ...")
    certs = req("GET", "/nginx/certificates", token=token)
    cert = next((c for c in certs if DOMAIN in c.get("domain_names", [])), None)
    if cert:
        cert_id = cert["id"]
        print(f"      기존 인증서 재사용 (id={cert_id})")
    else:
        # NPM v2.14 인증서 meta 스키마는 dns_challenge 등만 허용
        # (letsencrypt_email/agree 는 거부됨 — additionalProperties:false).
        cert = req("POST", "/nginx/certificates", {
            "provider": "letsencrypt",
            "domain_names": [DOMAIN],
            "meta": {"dns_challenge": False},
        }, token=token)
        cert_id = cert["id"]
        print(f"      발급됨 (id={cert_id})  (HTTP-01, 수십 초 소요됨)")

    # --- 호스트에 인증서 연결 + SSL 강제 ---
    print("[5/5] 호스트에 SSL 적용 ...")
    host_body.update({
        "certificate_id": cert_id,
        "ssl_forced": True,
        "http2_support": True,
        "hsts_enabled": True,
    })
    req("PUT", f"/nginx/proxy-hosts/{host_id}", host_body, token=token)
    print("      OK")
    print(f"\n[완료] https://{DOMAIN} -> {FWD_HOST}:{FWD_PORT}{creds_line()}")

if __name__ == "__main__":
    main()
