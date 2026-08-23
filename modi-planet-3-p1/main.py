import os
import sys
from dotenv import load_dotenv
from agent.orchestrator import Orchestrator


def print_header():
    print("\n" + "=" * 50)
    print("  교육용 설계 & 바이브코딩 에이전트")
    print("=" * 50)
    print("  만들고 싶은 서비스를 말해보세요!")
    print("  예: \"배달 앱 만들고 싶어\"")
    print()
    print("  명령어:")
    print("    /diagram  — 현재 구조도 보기")
    print("    /phase    — 현재 Phase 확인")
    print("    /files    — 생성된 파일 목록")
    print("    /reset    — 처음부터 다시")
    print("    /quit     — 종료")
    print("=" * 50 + "\n")


def handle_event(event: dict):
    """스트리밍 이벤트를 CLI에 출력."""
    etype = event.get("type")
    if etype == "token":
        print(event["text"], end="", flush=True)
    elif etype == "status":
        print(f"\n  ... {event['message']}", flush=True)
    elif etype == "log":
        print(f"  [{event['message']}]", flush=True)
    elif etype == "agent_step":
        print(f"\n  > {event['description']}", flush=True)
    elif etype == "agent_step_update":
        status = "완료" if event.get("status") == "success" else "실패"
        print(f"  [{status}]", flush=True)
    elif etype == "done":
        print()


def main():
    load_dotenv()

    # Sentry 관측(에러/성능). SENTRY_DSN 없으면 no-op. 환경값은 SENTRY_ENVIRONMENT(dev/onprem/release).
    from agent.observability import init_sentry, capture_chat_exception, flush
    init_sentry("cli")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        print("   .env 파일에 API 키를 넣어주세요.")
        sys.exit(1)

    orchestrator = Orchestrator(api_key=api_key)
    print_header()
    print(f"현재 Phase: {orchestrator.get_phase()}\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n안녕히 가세요!")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            print("\n안녕히 가세요!")
            break

        if user_input == "/diagram":
            print(f"\n현재 다이어그램:\n\n{orchestrator.get_diagram()}\n")
            continue

        if user_input == "/phase":
            print(f"\n현재 Phase: {orchestrator.get_phase()}\n")
            continue

        if user_input == "/files":
            print(f"\n생성된 파일:\n{orchestrator.state.get_files_summary()}\n")
            continue

        if user_input == "/reset":
            orchestrator.reset()
            print("\n초기화되었습니다. 처음부터 다시 시작합니다.\n")
            print(f"현재 Phase: {orchestrator.get_phase()}\n")
            continue

        try:
            print(f"\n(Phase: {orchestrator.get_phase()})")
            for event in orchestrator.chat_stream(user_input):
                handle_event(event)
            print()
        except Exception as e:
            capture_chat_exception(e, source="cli")
            print(f"\n오류가 발생했습니다: {e}\n")

    flush()  # 종료 직전 버퍼된 이벤트 강제 전송


if __name__ == "__main__":
    main()
