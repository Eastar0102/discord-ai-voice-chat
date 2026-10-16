"""
찍은 화면을 글로 바꾼다 — 캐릭터가 읽을 수 있는 형태로.

두 갈래를 쓴다. **둘이 대체가 아니라 역할이 다르다** (09-30 실측).

  ocr  윈도우 내장 OCR (Windows.Media.Ocr)
       40ms · VRAM 0 · 한글 5/5 정확 · 그림은 못 봄
       언어 모델이 없어서 파인튜닝 모델과 자리를 다투지 않는다. 게임 중에도 쓸 수 있다.
       한국어는 이 기기에서 언어팩 없이 이미 지원된다

  vlm  qwen3-vl:4b (Ollama)
       12~21초 · VRAM 3.0GB · 그림·배치·상황을 이해한다
       글자는 "읽는" 게 아니라 알아보고 다시 쓰는 것이라 근사값이다 (봐줄래 → 보졸래).
       A.X와 동시 적재해도 A.X 속도는 71 tok/s로 떨어지지 않는다 (실측) — 다만 남는
       여유가 1.1GB뿐이라 게임까지 켜면 넘친다

그래서 기본은 `both`다: OCR로 글자를 정확히 뜨고, VLM으로 무엇이 보이는지 한 줄 얻는다.
글자만 필요하면 `ocr`이 즉시 끝난다.
"""

import asyncio
import base64
import io
import json
import time

import httpx

import re

from config_loader import load_config

_cfg = (load_config() or {}).get("vision", {}) or {}
# VLM을 보고 난 뒤에도 VRAM에 둘지. 기본은 내린다 — 상시 적재는 10-01 실사용에서
# 생각 시간을 0.72s → 10.18s로 만들었다 (남는 VRAM 628MB, KV 캐시가 공유 메모리로 샘)
KEEP_VLM = bool(_cfg.get("keep_vlm", False))
# 프롬프트에 넣을 OCR 줄 수. 220줄을 그대로 넣으면 프리필이 폭발한다
OCR_MAX_LINES = int(_cfg.get("ocr_max_lines", 40))

OLLAMA_URL = "http://127.0.0.1:11434"
VLM_MODEL = "qwen3-vl:4b"
VLM_TIMEOUT = 180.0
VLM_PROMPT = ("이 화면에 무엇이 보이는지 한국어로 두 문장 이내로 말해라. "
              "글자를 옮겨 적지는 말고, 무엇을 하는 화면인지와 눈에 띄는 것을 말해라.")


# 작은 UI 글자에서 OCR이 흘리는 찌꺼기 — 한 글자, 기호 뭉치, 한글이 거의 없는 줄.
# 10-01 로그의 "개화 11", "나무정렴 11 성-", "아의 여단 최종 보상"이 그 결과였고,
# 캐릭터는 그걸 성실하게 설명했다
_JUNK_RE = re.compile(r"^[\W\d_]*$")


def useful_lines(lines: list, max_lines: int = None) -> list:
    """읽을 만한 줄만. 짧은 조각과 기호 뭉치를 버리고 개수를 제한한다"""
    out = []
    for ln in lines:
        t = (ln or "").strip()
        if len(t) < 3 or _JUNK_RE.match(t):
            continue
        # 한글·영문 글자가 절반도 안 되면 인식이 깨진 줄로 본다
        letters = sum(ch.isalpha() for ch in t)
        if letters * 2 < len(t):
            continue
        out.append(t)
    return out[:max_lines or OCR_MAX_LINES]


def ocr_image(img, lang: str = "ko", with_size: bool = False):
    """윈도우 내장 OCR — 줄 단위 텍스트. winsdk가 없으면 ImportError를 그대로 올린다.
    with_size면 (줄 목록, 낱말 네모 높이의 중앙값) 을 돌려준다"""
    import winsdk.windows.media.ocr as ocr
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winsdk.windows.security.cryptography import CryptographicBuffer

    engine = ocr.OcrEngine.try_create_from_language(Language(lang))
    if engine is None:
        raise RuntimeError(f"윈도우에 {lang} OCR 엔진이 없습니다 (언어팩 확인)")

    rgba = img.convert("RGBA")
    buf = CryptographicBuffer.create_from_byte_array(rgba.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(
        buf, BitmapPixelFormat.RGBA8, rgba.width, rgba.height)

    async def go():
        return await engine.recognize_async(bmp)

    res = asyncio.run(go())
    if with_size:
        # 낱말의 네모 높이 = 화면에 실제로 그려진 글자 크기. 정확도를 가르는 것이 이것이라
        # (아래 glyph_px 실측) 원고를 읽기 전에 이걸로 먼저 판단한다
        hs = [w.bounding_rect.height for ln in res.lines for w in ln.words]
        hs.sort()
        return [ln.text for ln in res.lines], (hs[len(hs) // 2] if hs else 0.0)
    return [ln.text for ln in res.lines]


def vlm_describe(img, prompt: str = VLM_PROMPT, model: str = VLM_MODEL) -> str:
    """VLM에게 화면을 설명시킨다. 사고 모델이라 num_predict를 넉넉히 준다 —
    적게 주면 사고에 다 쓰고 답이 빈 문자열로 온다 (09-30에 그렇게 속았다)"""
    b = io.BytesIO()
    img.save(b, format="PNG")
    b64 = base64.b64encode(b.getvalue()).decode()
    r = httpx.post(f"{OLLAMA_URL}/api/chat", timeout=VLM_TIMEOUT, json={
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
        "options": {"temperature": 0.2, "num_predict": 2500},
    })
    r.raise_for_status()
    return (r.json().get("message", {}).get("content") or "").strip()


SHOT_PATH = None    # 마지막으로 찍은 그림 (컨트롤 앱에서 확인용)


def save_shot(img):
    """방금 찍은 것을 파일로 남긴다. 10-01에 보여주려던 화면과 찍힌 화면이 달랐는데
    확인할 방법이 없어서 네 턴을 허비했다 — 한 장만 덮어쓴다"""
    global SHOT_PATH
    import os
    try:
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "last_look.png")
        img.save(path)
        SHOT_PATH = path
        return path
    except Exception as e:
        print(f"[Vision] 캡처 저장 실패(무시): {e}")
        return None


def release_vlm(model: str = None) -> None:
    """VLM을 VRAM에서 내린다 (keep_alive 0). 실패해도 조용히 넘어간다 —
    목적은 자리를 비우는 것이고, 못 내려도 본 결과는 이미 손에 있다"""
    try:
        httpx.post(f"{OLLAMA_URL}/api/chat", timeout=30.0,
                   json={"model": model or VLM_MODEL, "messages": [], "keep_alive": 0})
    except Exception as e:
        print(f"[Vision] VLM 언로드 실패(무시): {type(e).__name__}: {e}")


# OCR이 한글을 놓치면 모양이 닮은 **한자**로 떨어진다 — 실측에서 "창조자"가 "참조자",
# "건조" 가 "건 조 喬卜" 로 나왔다. 원고에 한자가 섞일 일은 드물므로 이게 깨짐의 신호다.
# (게임 UI에서는 "개화 11" 처럼 한글로도 그럴듯하게 깨져 못 잡지만, 산문에서는 잡힌다)
_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")
_HANGUL_RE = re.compile(r"[가-힣]")


def looks_garbled(text: str, max_cjk_ratio: float = 0.01) -> tuple:
    """(깨진 것 같은가, 이유). 한국어 원고에 한자가 섞이면 OCR이 글자를 놓친 것이다.

    글자 크기별 실측 (맑은 고딕, 윈도우 내장 OCR):
      11px 53% · 13px 79% · 15px 76% · 18px 87% · 22px 91% · **28px 97%** · 36px 96%
    즉 28px 아래로는 산문을 다룰 만한 정확도가 안 나온다. 웹 에디터 기본값이 16px 안팎이라
    확대(175~200%)가 필요하고, 안 하면 쓰지 않은 낱말로 감상을 말하게 된다
    """
    han = len(_HANGUL_RE.findall(text))
    cjk = len(_CJK_RE.findall(text))
    if han < 20:
        return True, f"한글이 {han}자뿐 — 원고로 보기 어렵다"
    if cjk > max(1, int(han * max_cjk_ratio)):
        return True, f"한자가 {cjk}자 섞였다 (한글 {han}자) — 글자가 작아 OCR이 깨진 듯"
    return False, ""


# 글자가 이만큼(화면에 그려진 높이, px)은 돼야 산문을 다룰 만하다. 실측 — 네모 높이별 정확도:
#   10px 53% · 12px 79% · 14px 76% · 17px 87% · 21px 91% · **24px 99% · 26px 97%** · 34px 96%
# 웹 에디터 기본값이 16px 안팎이라 확대(175~200%)가 필요하다. 안 하면 일곱 자에 한 자가
# 틀린 글로 감상을 말한다 — 게임 UI와 달리 원고에서는 그게 치명적이다
MIN_GLYPH_PX = int(_cfg.get("min_glyph_px", 24))


def read_text(source: dict, max_chars: int = 3000, min_glyph_px: int = None) -> tuple:
    """그 소스의 글자만 읽어 (텍스트, 메모) 를 돌려준다. VLM도 줄 수 제한도 쓰지 않는다.

    원고를 들여다보는 길이다. `look(mode="ocr")`과 다른 점:
      · 줄 수가 아니라 **글자 수**로 자른다 — 원고는 40줄로는 한 페이지도 안 들어간다
      · 끝에서 자른다 — 지금 쓰고 있는 자리가 화면 아래쪽이다
      · 창이 맨 앞이 아니면 찍지 않는다 — 가려진 창은 그 자리의 다른 화면이 찍히므로,
        탭을 옮긴 순간부터 디스코드를 원고로 읽게 된다
      · 앞으로 끌어오지(focus) 않는다 — 글 쓰는 사람의 손을 빼앗는다
      · 글자가 작으면 **아무것도 돌려주지 않는다** — 깨진 글로 원고를 얘기하는 것보다 낫다
    """
    import screen

    src = {k: v for k, v in (source or {}).items() if k != "focus"}
    if src.get("type") == "window":
        w = screen.find_window(src.get("title", ""))
        if not screen.is_foreground(w["hwnd"]):
            return "", f"창 '{w['title']}'이 맨 앞이 아니라 건너뜀"
    img, note = screen.capture(src, purpose="ocr")
    if screen.looks_blank(img):
        return "", f"{note} — 거의 한 색이라 건너뜀 (가려진 듯)"
    raw, glyph = ocr_image(img, with_size=True)
    floor = MIN_GLYPH_PX if min_glyph_px is None else min_glyph_px
    if glyph and glyph < floor:
        return "", (f"{note} — 글자가 {glyph:.0f}px뿐이라 읽지 않았습니다 "
                    f"({floor}px 이상 필요). 에디터를 175~200%로 확대해 주세요")
    lines = useful_lines(raw, max_lines=10_000)
    text = (chr(10)).join(lines)[-max_chars:]
    bad, why = looks_garbled(text)
    if bad:
        # 깨진 글을 원고로 넘기면 쓰지 않은 낱말로 감상을 말한다. 차라리 아무것도 안 준다
        return "", f"{note} — {why}"
    return text, note


def look(source: dict, mode: str = "both", on_partial=None) -> dict:
    """화면을 보고 {title, text, ocr_lines, seen, took} 를 돌려준다.

    mode: ocr(글자만) · vlm(설명만) · both
    OCR은 원본 해상도로, VLM은 줄여서 찍는다 — 같은 그림이라도 필요한 해상도가 다르다

    on_partial(text): OCR이 끝난 순간 글자만이라도 먼저 넘긴다. both는 VLM을 매번 새로
    올리므로 30초쯤 걸리는데, 그 동안 대화는 계속 돌아간다 — 글자는 2초면 나오므로
    먼저 걸어 두고 설명이 오면 덮어쓴다 (10-01: 보여줬는데 "못 봤다"고 답했다)
    """
    import screen

    out = {"ocr": None, "seen": None, "notes": [], "took": {}, "shot": None}

    if mode in ("ocr", "both"):
        img, note = screen.capture(source, purpose="ocr")
        out["shot"] = save_shot(img)
        t0 = time.time()
        try:
            raw = ocr_image(img)
            lines = useful_lines(raw)
            out["ocr"] = lines
            out["ocr_dropped"] = len(raw) - len(lines)
            out["took"]["ocr"] = round(time.time() - t0, 3)
        except Exception as e:
            out["notes"].append(f"OCR 실패: {type(e).__name__}: {e}")
        out["notes"].append(f"OCR용 {note}")
        # 설명을 기다리지 않고 글자부터 걸어 둔다 (both일 때만 의미가 있다)
        if on_partial and out["ocr"] and mode == "both":
            try:
                on_partial("화면의 글자:\n" + "\n".join(out["ocr"]))
            except Exception as e:
                print(f"[Vision] 글자 먼저 넘기기 실패: {e}")

    if mode in ("vlm", "both"):
        img, note = screen.capture(source, purpose="vlm")
        t0 = time.time()
        try:
            out["seen"] = vlm_describe(img)
            out["took"]["vlm"] = round(time.time() - t0, 1)
        except Exception as e:
            out["notes"].append(f"VLM 실패: {type(e).__name__}: {str(e)[:120]}")
        finally:
            # 상시 적재하면 A.X의 프리필이 무너진다 (10-01 실측). 보고 나면 바로 내린다
            if not KEEP_VLM:
                release_vlm()
                out["notes"].append("VLM 내림 (vision.keep_vlm: true로 두면 남긴다)")
        out["notes"].append(f"VLM용 {note}")

    # 캐릭터에게 넘길 한 덩어리. 무엇이 보이는지 먼저, 글자는 그 아래
    parts = []
    if out["seen"]:
        parts.append(out["seen"])
    if out["ocr"]:
        parts.append("화면의 글자:\n" + "\n".join(out["ocr"]))
    out["text"] = "\n\n".join(parts)
    return out
