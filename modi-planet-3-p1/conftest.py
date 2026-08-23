"""pytest 전역 설정 — 테스트를 외부 관측(Sentry)과 격리한다.

왜 필요한가 (Sentry EDU-AGENT-8 / OSError: disk full 근본 원인):
    server.py 는 import 시 load_dotenv() 로 .env 를 읽은 뒤 init_sentry() 를 호출한다.
    개발자 PC 의 .env 에는 실제 SENTRY_DSN 이 들어 있으므로, 로컬에서 `pytest` 를 돌리면
    Sentry SDK 가 "실 서비스"로 초기화된다. 그 상태에서 회귀 테스트
    test_lock_released_even_when_autosave_raises 가 auto_save 를 일부러
    OSError("disk full") 로 터뜨리면, finally 의 capture_chat_exception 이 그 가짜 예외를
    운영 Sentry 로 전송해 매 테스트 실행마다 유령 이슈가 쌓였다.

    load_dotenv 기본값은 override=False(이미 존재하는 환경변수는 덮어쓰지 않음)라
    단순 pop 은 .env 재적재로 무효화된다. 그래서 여기서 빈 문자열로 "선점"해
    init_sentry 가 no-op(빈 DSN → False)이 되게 한다. Sentry 자체를 검증하는
    test_observability 는 monkeypatch 로 제 값을 넣었다 되돌리므로 영향받지 않는다.

이 파일은 레포 루트에 있어 어떤 테스트 모듈보다 먼저 임포트된다(= server import 이전).
"""
import os

# 테스트에서는 절대 실 Sentry 로 이벤트를 보내지 않는다. 빈 문자열이라 load_dotenv 도 못 덮는다.
os.environ["SENTRY_DSN"] = ""
