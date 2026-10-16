"""구글 계정을 한 번 연결한다 — 원고(구글 문서)를 읽기 위해.

라이브러리를 더 깔지 않는다: 표준 라이브러리로 로컬 되돌림(loopback) 서버를 띄우고,
브라우저에서 동의를 받고, 코드를 refresh_token으로 바꿔 data/google_token.json에 저장한다.
그 파일은 .gitignore에 있다.

범위는 좁게 — 문서 본문과 폴더 목록만. 드라이브의 다른 파일(사진·서류)은 읽지 않는다.

먼저 할 일 (한 번):
  1. https://console.cloud.google.com 에서 프로젝트를 만든다
  2. API 및 서비스 → 라이브러리 → "Google Docs API"와 "Google Drive API"를 켠다
  3. OAuth 동의 화면 → 외부 → 테스트 사용자에 본인 계정을 넣는다
  4. 사용자 인증 정보 → OAuth 클라이언트 ID → **데스크톱 앱**
  5. 받은 값을 .env에 적는다:
         GOOGLE_CLIENT_ID=...
         GOOGLE_CLIENT_SECRET=...
  6. python scripts/google_login.py

사용법:
    python scripts/google_login.py            # 연결
    python scripts/google_login.py --check    # 연결됐는지, 무엇이 보이는지만 확인
"""
import argparse
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))

import httpx  # noqa: E402
import gdocs  # noqa: E402

NL = chr(10)
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"


class Catch(http.server.BaseHTTPRequestHandler):
    code = None
    state = None
    error = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        Catch.code = (q.get("code") or [None])[0]
        Catch.state = (q.get("state") or [None])[0]
        Catch.error = (q.get("error") or [None])[0]
        body = ("연결됐습니다. 창을 닫고 터미널로 돌아가세요."
                if Catch.code else f"실패: {Catch.error or '코드를 받지 못했습니다'}")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body style='font-family:sans-serif'><h3>{body}</h3>"
                         "</body></html>".encode("utf-8"))

    def log_message(self, *a):
        pass        # 요청 로그를 터미널에 뿌리지 않는다


def parse_code(s: str) -> tuple:
    """주소창에 붙은 것에서 (code, state). 주소 전체든 code 값만이든 받는다"""
    s = (s or "").strip().strip('"').strip("'")
    if "code=" in s:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(s).query or s.split("?", 1)[-1])
        return (q.get("code") or [None])[0], (q.get("state") or [None])[0]
    return (s or None), None


def check() -> int:
    """지금 연결 상태와, 설정된 원고가 실제로 읽히는지"""
    from config_loader import load_config
    try:
        gdocs.access_token()
    except gdocs.NotLinked as e:
        print(f"연결 안 됨 — {e}")
        return 1
    print("연결됨 (access token 발급 성공)")
    cfg = (load_config().get("manuscript") or {}).get("gdocs") or {}
    folder, doc = cfg.get("folder_id"), cfg.get("doc_id")
    if not folder and not doc:
        print("config의 manuscript.gdocs에 folder_id나 doc_id가 비어 있습니다.")
        print("문서 주소에서 /document/d/<이 부분>/edit 이 doc_id,")
        print("폴더 주소에서 /folders/<이 부분> 이 folder_id입니다.")
        return 0
    try:
        text, note = gdocs.read(folder, doc, max_chars=400)
    except Exception as e:
        print(f"읽기 실패: {type(e).__name__}: {e}")
        return 1
    print(f"읽었습니다 — {note}")
    print(f"{NL}--- 끝부분 ---{NL}{text}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="연결 상태만 확인한다")
    ap.add_argument("--code", help="브라우저가 돌아왔는데 받는 쪽이 놓쳤을 때, 그 주소 전체를 붙여 넣는다")
    args = ap.parse_args()
    if args.check:
        sys.exit(check())

    cid = os.getenv("GOOGLE_CLIENT_ID")
    csec = os.getenv("GOOGLE_CLIENT_SECRET")
    if not cid or not csec:
        print("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET이 .env에 없습니다.")
        print(__doc__.split("먼저 할 일")[1].split("사용법")[0])
        sys.exit(1)

    state = secrets.token_urlsafe(16)
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": " ".join(gdocs.SCOPES), "access_type": "offline",
        "prompt": "consent", "state": state})

    if args.code:
        # 브라우저는 돌아왔는데 받는 쪽이 놓쳤을 때 — 주소창 전체를 그대로 붙여 넣으면 된다
        code, got_state = parse_code(args.code)
        if not code:
            print("그 주소에서 code를 찾지 못했습니다 — localhost:8765/?...code=... 전체를 주세요")
            sys.exit(1)
        state = got_state or state
        Catch.code, Catch.state = code, state
    else:
        srv = http.server.HTTPServer(("localhost", PORT), Catch)
        srv.timeout = 1
        print(f"브라우저에서 동의해 주세요. 안 열리면 이 주소를 직접 여세요:{NL}{url}{NL}")
        webbrowser.open(url)
        # **코드를 받을 때까지 열어 둔다.** 전에는 여기서 바로 server_close()를 불러
        # 구글이 되돌아왔을 때 포트가 닫혀 있었다 (ERR_CONNECTION_REFUSED). 한 번만 받는
        # handle_request로는 파비콘 요청이 먼저 먹을 수 있어, 받을 때까지 돈다
        import time
        deadline = time.time() + 300
        while time.time() < deadline and not (Catch.code or Catch.error):
            srv.handle_request()       # timeout=1이라 아무도 안 오면 1초 뒤 돌아온다
        srv.server_close()
        if not Catch.code:
            print(f"코드를 받지 못했습니다: {Catch.error or '시간 초과'}")
            print("브라우저 주소창에 code=... 가 보이면 그 주소 전체로 다시 해 보세요:")
            print("  python scripts/google_login.py --code \"<주소 전체>\"")
            sys.exit(1)
        if Catch.state != state:
            print("state가 일치하지 않습니다 — 중단합니다")
            sys.exit(1)

    r = httpx.post(TOKEN, timeout=30, data={
        "client_id": cid, "client_secret": csec, "code": Catch.code,
        "grant_type": "authorization_code", "redirect_uri": REDIRECT})
    if r.status_code != 200:
        print(f"토큰 교환 실패 ({r.status_code}): {r.text[:300]}")
        sys.exit(1)
    d = r.json()
    if not d.get("refresh_token"):
        print("refresh_token이 없습니다 — 이미 동의한 계정이면 구글 계정 설정에서 "
              "이 앱의 접근 권한을 지우고 다시 실행하세요")
        sys.exit(1)
    os.makedirs(os.path.dirname(gdocs.TOKEN_PATH), exist_ok=True)
    with open(gdocs.TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"client_id": cid, "client_secret": csec,
                   "refresh_token": d["refresh_token"], "scopes": gdocs.SCOPES}, f,
                  ensure_ascii=False, indent=2)
    print(f"저장했습니다: {gdocs.TOKEN_PATH} (.gitignore에 있습니다){NL}")
    sys.exit(check())


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
