"""
화면을 잡아 온다 — OBS의 "소스"처럼, 무엇을 볼지 이름 붙여 골라 쓴다.

세 가지를 볼 수 있다.
  screen  전체 화면 (또는 모니터 하나)
  window  창 제목으로 찾아 그 창 영역만 — 웹툰 창, 뉴스 창처럼 따라간다
  region  좌표로 지정한 영역

해상도는 소스마다 정한다. **올려 보내는 쪽에 따라 정답이 다르다**:
  OCR  → 줄이지 않는다. 줄이면 작은 글자가 먼저 죽는다 (09-30 실측: 2560→1024로 줄여
         게임 UI를 읽히려다 전부 실패했고, 그걸 두고 "4B는 한글을 못 읽는다"고 잘못 결론냈다)
  VLM  → 1280 정도로 줄인다. 폭이 늘면 토큰과 시간이 같이 늘고, 그림·배치를 보는 데는
         원본 해상도가 필요하지 않다

**의존성을 늘리지 않는다.** 창 목록과 좌표는 ctypes로 user32를 직접 부르고, 캡처는
이미 있는 PIL.ImageGrab을 쓴다.

창 캡처는 **그 창이 놓인 화면 영역을 찍는 방식**이다 (PrintWindow가 아니라). 가려져 있으면
가린 것이 찍힌다 — 하드웨어 가속을 쓰는 브라우저·게임에서 PrintWindow가 검은 화면을 주는
경우가 많아, 확실히 보이는 쪽을 택했다. 볼 창은 앞에 띄워 두면 된다.
"""

import ctypes
import ctypes.wintypes as wt
import re

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()          # DPI 배율이 걸린 화면에서 좌표가 어긋나지 않게

# 소스마다 기본 폭. None이면 원본 그대로
DEFAULT_MAX_WIDTH = {"ocr": None, "vlm": 1280}
MIN_WINDOW_PX = 120                  # 이보다 작은 창은 목록에서 뺀다 (트레이·숨은 창)


def list_windows() -> list:
    """보이는 창들 — [{title, rect: [l,t,r,b], w, h}] (앞에 있는 것부터)"""
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        r = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < MIN_WINDOW_PX or h < MIN_WINDOW_PX:
            return True
        out.append({"title": buf.value, "rect": [r.left, r.top, r.right, r.bottom],
                    "w": w, "h": h, "hwnd": int(hwnd), "minimized": _is_minimized(hwnd)})
        return True

    user32.EnumWindows(cb, 0)
    return out


def _is_minimized(hwnd) -> bool:
    return bool(user32.IsIconic(hwnd))


def _raise_window(hwnd) -> bool:
    """창을 앞으로 가져온다. 성공했는지 돌려준다.

    캡처가 '그 좌표의 화면'을 찍는 방식이라 가려져 있으면 가린 것이 찍힌다 — 볼 창은 앞에
    있어야 한다. 그런데 **윈도우는 백그라운드 프로세스의 포커스 훔치기를 막는다**:
    API 서버(uvicorn)는 앞에 없으므로 `SetForegroundWindow`가 조용히 0을 돌려주고 끝난다
    (10-01: focus: true를 줬는데도 빈 화면이 찍혔다). 그래서 세 가지를 차례로 시도하고,
    **됐는지 확인해서** 안 됐으면 호출자에게 알린다 — 안 된 것을 됐다고 하는 게 제일 나쁘다."""
    import time as _t
    SW_RESTORE, SW_SHOW, SW_MINIMIZE = 9, 5, 6
    if _is_minimized(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    _t.sleep(0.35)          # 그려질 시간 — 바로 찍으면 아직 빈 화면이 잡힌다
    if user32.GetForegroundWindow() == hwnd:
        return True
    # 최소화했다 복원하면 포커스 제한을 우회하는 경우가 있다 (창이 한 번 깜빡인다)
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    user32.ShowWindow(hwnd, SW_RESTORE)
    _t.sleep(0.45)
    return user32.GetForegroundWindow() == hwnd


def looks_blank(img, tol: int = 6) -> bool:
    """거의 한 색인 그림인가 — 가려진 창을 찍었을 때 나오는 결과다.
    이걸 걸러내지 않으면 VLM이 "화면이 완전히 검은색입니다"를 성실하게 설명한다"""
    small = img.convert("L").resize((48, 48))
    px = list(small.getdata())
    return max(px) - min(px) <= tol


def is_foreground(hwnd) -> bool:
    """그 창이 지금 맨 앞인가.

    가려진 창을 찍으면 그 자리의 다른 화면이 찍힌다 (PrintWindow가 아니라 ImageGrab이라).
    원고를 주기적으로 들여다볼 때 이걸 안 보면, 디스코드로 탭을 옮긴 순간부터 디스코드를
    원고로 읽는다. 앞으로 끌어오는 것(focus)은 글 쓰는 사람의 손을 빼앗으므로 쓰지 않는다
    """
    return bool(user32.GetForegroundWindow() == hwnd)


def window_rect(hwnd) -> list:
    """그 창의 현재 좌표. 앞으로 가져오면 좌표가 바뀔 수 있어 다시 읽는다"""
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise ValueError("창 좌표를 읽지 못했습니다")
    return [r.left, r.top, r.right, r.bottom]


def find_window(pattern: str) -> dict:
    """제목에 이 말이 든 창. 여러 개면 가장 앞의 것. 정규식도 받는다"""
    if not pattern:
        raise ValueError("창 제목을 지정하세요")
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    hits = [w for w in list_windows() if rx.search(w["title"])]
    if hits:
        w = dict(hits[0])
        # 패턴이 여러 창에 걸리면 어느 것을 골랐는지 드러나야 한다 — 프로젝트 폴더명이
        # 'my-discord-bot'처럼 'Discord'가 들어가면 "Discord"가 IDE 창에 걸리는 일이 있었다
        w["matched"] = len(hits)
        return w
    # 최소화된 창은 list_windows()의 크기 조건에 걸려 빠지므로, 못 찾았을 때 따로 알려 준다
    raise ValueError(f"'{pattern}'에 맞는 창을 찾지 못했습니다 "
                     f"(최소화돼 있으면 복원하거나 source에 focus: true를 주세요)")


def capture(source: dict, purpose: str = "ocr"):
    """소스 정의대로 찍어 (PIL.Image, 설명) 을 돌려준다.

    source = {"type": "screen"|"window"|"region", "title": ..., "bbox": [l,t,r,b],
              "monitor": 0, "max_width": 1280}
    purpose 는 폭 기본값을 고르는 데만 쓴다 (source의 max_width가 있으면 그게 이긴다)
    """
    from PIL import ImageGrab

    kind = (source or {}).get("type", "screen")
    raised = None
    if kind == "window":
        w = find_window(source.get("title", ""))
        if source.get("focus"):
            raised = _raise_window(w["hwnd"])
            # **같은 hwnd의 좌표를 다시 읽는다.** 제목으로 다시 찾으면 안 된다 —
            # "Chrome"처럼 여러 창에 걸리는 패턴이면 앞으로 올리며 z-order가 바뀌어
            # 엉뚱한 창의 좌표를 집는다 (10-01: 세 번 다 다른 창을 찍었다)
            w = {**w, "rect": window_rect(w["hwnd"])}
        bbox, what = tuple(w["rect"]), f"창 '{w['title']}'"
        if w.get("matched", 1) > 1:
            what += f" (패턴에 {w['matched']}개 걸려 맨 앞의 것)"
    elif kind == "region":
        bbox = tuple(source.get("bbox") or ())
        if len(bbox) != 4:
            raise ValueError("region에는 bbox [left, top, right, bottom]이 필요합니다")
        what = f"영역 {bbox}"
    else:
        mon = int(source.get("monitor", 0) or 0)
        if mon:
            # all_screens로 전부 잡은 뒤 그 모니터만 자른다 — ImageGrab에 모니터 인수가 없다
            img = ImageGrab.grab(all_screens=True)
            bbox, what = None, "전체 화면(여러 모니터)"
        else:
            bbox, what = None, "전체 화면"

    # all_screens=True 여야 보조 모니터에 있는 창도 찍힌다 (없으면 주 모니터 밖은 검게 나온다)
    img = ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab()
    native = img.size

    # 폭은 보내는 쪽에 따라 다르다. `max_width`는 **VLM에 보낼 폭**을 뜻하고, OCR은 기본이
    # 원본이다 — 줄이면 작은 글자가 먼저 죽는다. 굳이 OCR도 줄이려면 ocr_max_width를 준다
    if f"{purpose}_max_width" in source:
        max_w = source[f"{purpose}_max_width"]
    elif purpose == "vlm":
        max_w = source.get("max_width", DEFAULT_MAX_WIDTH["vlm"])
    else:
        max_w = DEFAULT_MAX_WIDTH.get(purpose)
    if max_w and img.width > int(max_w):
        max_w = int(max_w)
        img = img.resize((max_w, max(1, round(img.height * max_w / img.width))))

    note = f"{what} {native[0]}x{native[1]}"
    if img.size != native:
        note += f" → {img.width}x{img.height}"
    img = img.convert("RGB")
    if looks_blank(img):
        # 왜 빈 화면인지에 따라 할 일이 다르다 — focus를 이미 줬는데 실패한 경우에
        # "focus를 주세요"라고 하면 쓸모없는 안내가 된다 (10-01에 그랬다)
        if raised is False:
            hint = ("윈도우가 포커스 전환을 막아 창을 앞으로 가져오지 못했습니다 — "
                    "그 창을 직접 앞에 띄워 두고 다시 해 보세요")
        elif kind == "window" and not source.get("focus"):
            hint = "창이 가려졌거나 최소화된 것 같습니다 — source에 focus: true를 주면 앞으로 가져와 찍습니다"
        else:
            hint = "창이 가려졌거나 아직 그려지지 않은 것 같습니다"
        raise ValueError(f"{what}을 찍었는데 거의 빈 화면입니다. {hint}")
    return img, note
