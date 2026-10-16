"""구글 문서에서 원고를 그대로 읽어 온다 — OCR 없이.

화면 OCR은 글자가 24px 아래면 쓸 수 없고(실측: 17px 87%, 21px 91%), 보이는 화면만
읽히고, 창이 맨 앞이어야 한다. 원고가 구글 문서라면 그 셋 다 필요 없다 — 정확한 글자,
문서 전체, 어느 창이 앞이든 무관.

라이브러리를 더 쓰지 않는다. REST라서 httpx로 충분하고, 로그인(한 번)만
`scripts/google_login.py`가 맡는다 — read_url.py가 표준 라이브러리로 본문을 뽑는 것과 같은 결.

**범위는 좁게 잡는다.** `drive.readonly`면 드라이브의 모든 파일을 읽을 수 있어서,
문서만 읽는 두 범위를 쓴다:
  · documents.readonly      — 구글 문서 본문
  · drive.metadata.readonly — 폴더 안 목록과 수정 시각 (본문은 못 읽는다)
"""
import json
import os
import time

import httpx

SCOPES = ["https://www.googleapis.com/auth/documents.readonly",
          "https://www.googleapis.com/auth/drive.metadata.readonly"]
TOKEN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "google_token.json")
DOC_MIME = "application/vnd.google-apps.document"
_TOKEN = {"access": None, "expires": 0.0}


class NotLinked(RuntimeError):
    """아직 구글 계정을 연결하지 않았다 — 한 번 로그인하면 된다"""


def _saved() -> dict:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise NotLinked(
            "구글 계정이 연결되지 않았습니다 — `python scripts/google_login.py`를 한 번 실행하세요")


def access_token() -> str:
    """갖고 있는 refresh_token으로 access_token을 받아 둔다 (만료 1분 전에 갱신)"""
    if _TOKEN["access"] and time.time() < _TOKEN["expires"] - 60:
        return _TOKEN["access"]
    saved = _saved()
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "client_id": saved["client_id"], "client_secret": saved["client_secret"],
        "refresh_token": saved["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise NotLinked(f"토큰 갱신 실패 ({r.status_code}) — 다시 로그인해야 할 수 있습니다: "
                        f"{r.text[:160]}")
    d = r.json()
    _TOKEN["access"] = d["access_token"]
    _TOKEN["expires"] = time.time() + float(d.get("expires_in", 3600))
    return _TOKEN["access"]


def _get(url: str, **params):
    r = httpx.get(url, timeout=30, params=params,
                  headers={"Authorization": f"Bearer {access_token()}"})
    if r.status_code == 403 and "insufficient" in r.text.lower():
        raise NotLinked("권한이 모자랍니다 — scripts/google_login.py를 다시 실행해 "
                        "문서·메타데이터 읽기를 허용하세요")
    r.raise_for_status()
    return r.json()


def latest_doc(folder_id: str) -> dict:
    """그 폴더에서 **가장 최근에 고친** 구글 문서. 화별로 문서를 따로 쓰므로,
    지금 쓰고 있는 화를 이걸로 따라간다 (문서 id를 매번 바꿔 적지 않게)"""
    d = _get("https://www.googleapis.com/drive/v3/files",
             q=f"'{folder_id}' in parents and mimeType='{DOC_MIME}' and trashed=false",
             orderBy="modifiedTime desc", pageSize=1,
             fields="files(id,name,modifiedTime)")
    files = d.get("files") or []
    if not files:
        raise ValueError(f"폴더에 구글 문서가 없습니다 ({folder_id})")
    return files[0]


def doc_meta(doc_id: str) -> dict:
    return _get(f"https://www.googleapis.com/drive/v3/files/{doc_id}",
                fields="id,name,modifiedTime")


def doc_text(doc_id: str) -> str:
    """문서 본문을 평범한 글로. 표·각주는 건너뛴다 — 원고는 문단이다"""
    d = _get(f"https://docs.googleapis.com/v1/documents/{doc_id}")
    out = []
    for block in (d.get("body") or {}).get("content") or []:
        para = block.get("paragraph")
        if not para:
            continue
        line = "".join((e.get("textRun") or {}).get("content", "")
                       for e in para.get("elements") or [])
        if line.strip():
            out.append(line.rstrip())
    return (chr(10)).join(out)


# 문서를 마지막으로 가져온 결과 — {문서 id: (수정 시각, 본문)}.
# 본문 가져오기는 호출 둘 중 비싼 쪽이고, 안 고쳤으면 가져올 이유가 없다
_CACHE = {}


def read(folder_id: str = None, doc_id: str = None, max_chars: int = 3000) -> tuple:
    """(원고 끝 max_chars, 메모). doc_id가 있으면 그것, 없으면 폴더의 최신 문서.

    **안 바뀌었으면 본문을 다시 가져오지 않는다.** 목록 조회가 이미 수정 시각을 주므로
    그걸로 거른다 — 20초마다 폴링하면서 안 고친 문서를 계속 내려받을 이유가 없다
    (호출이 폴링당 2회에서 1회로, 글을 쓸 때만 2회가 된다)
    """
    if not doc_id and not folder_id:
        raise ValueError("manuscript.gdocs에 folder_id나 doc_id를 적어 주세요")
    info = doc_meta(doc_id) if doc_id else latest_doc(folder_id)
    stamp = info.get("modifiedTime") or ""
    cached = _CACHE.get(info["id"])
    if cached and cached[0] == stamp:
        text = cached[1]
    else:
        text = doc_text(info["id"])
        _CACHE[info["id"]] = (stamp, text)
    note = f"구글 문서 '{info.get('name')}' ({len(text)}자, 수정 {stamp[:16]})"
    return text[-max_chars:], note
