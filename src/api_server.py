"""
Discord Bot용 Python API 서버
- STT: 음성 파일을 텍스트로 변환
- Chat: 텍스트를 LLM에 보내고 응답 받기
- TTS: 텍스트를 음성 파일로 변환
"""

from fastapi import FastAPI, File, UploadFile, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import os
import sys
import traceback

# 파이프로 실행될 때(Electron 앱 등)의 stdout 문제 방지:
# - cp949 인코딩 → 이모지/대시 print가 UnicodeEncodeError로 턴을 죽임
# - 블록 버퍼링 → 로그가 8KB 찰 때까지 안 보임 (line_buffering으로 즉시 출력)
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

# src 디렉토리를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(__file__))

# Pydantic 모델 정의
class ChatRequest(BaseModel):
    text: str
    context: list = []  # 화자별 대화 컨텍스트
    speaker: str = ""   # 화자 이름

class ChatResponse(BaseModel):
    text: str
    style: str
    importance: int

class TTSRequest(BaseModel):
    text: str
    style: str = "neutral"

# config 먼저 로드 (OpenAI 클라이언트 초기화를 위해)
from config_loader import load_config
from stt import transcribe
from llm import chat_with_gpt
from tts import synthesize
import numpy as np
import soundfile as sf

app = FastAPI(title="Discord AI Voice Chat API")


@app.on_event("startup")
async def warmup_llm():
    """로컬 LLM을 백그라운드로 미리 로드 — 기동(헬스 체크)은 막지 않는다"""
    import asyncio
    from llm import warmup_current
    import tts
    asyncio.get_event_loop().run_in_executor(None, warmup_current)
    # 로컬 TTS(GPT-SoVITS)는 별도 서버 실행·모델 로딩이 필요 — 첫 대답 전에 미리
    asyncio.get_event_loop().run_in_executor(None, tts.warmup_current)
    # 기억도 같이 깨운다 — 채널에 들어가기 전부터 밀린 대화를 정리하고 검색을 데워 둔다
    asyncio.get_event_loop().run_in_executor(None, _wake_memory)


def _wake_memory():
    """기억 예열·정리 (백그라운드). LLM 워밍업과 같은 executor라 순서대로 돈다"""
    try:
        from memory import memory_manager
        info = memory_manager.wake_up()
        print(f"[Memory] 깨어남 — 검색 예열 {info.get('warm_sec')}초 · 새로 저장 {info.get('saved', 0)}개")
        if info.get("last_talk_ago"):
            print(f"[Memory] 마지막 대화 {info['last_talk_ago']} ({info['last_talk']})"
                  f" · 아는 사람 {', '.join(info.get('people') or []) or '없음'}")
        for line in info.get("overdue") or []:
            print(f"[Memory] 날짜가 지난 일정 — {line}")
    except Exception as e:
        print(f"Warning: 기억 깨우기 실패: {e}")

# 설정 로드 (전역)
try:
    config = load_config()
    print("Success: Config loaded")
except Exception as e:
    print(f"Error: Config load failed: {e}")
    config = None

# CORS 설정 (로컬 프런트엔드만 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Discord 봇이 메모리 스트림을 사용하므로 임시 파일 불필요


# 무시할 STT 입력 목록 (config/config.json filters 섹션에서 로드)
_filters = (config or {}).get("filters", {})
IGNORE_PHRASES = _filters.get("ignore_phrases", [])
IGNORE_PARTIAL = _filters.get("ignore_partial", [])

@app.post("/stt")
async def speech_to_text(request: Request):
    """
    음성 바이너리 데이터를 텍스트로 변환
    :param request: 바이너리 오디오 데이터
    :return: {"text": "인식된 텍스트"}
    """
    try:
        # 바이너리 데이터 직접 읽기
        audio_data = await request.body()
        
        print(f"[STT Debug] 받은 데이터 크기: {len(audio_data)} bytes")
        
        # Discord는 16bit signed int PCM (little-endian, 48kHz mono)
        if len(audio_data) < 2:
            print("[STT Debug] 데이터가 너무 짧음")
            return {"text": ""}
        
        # 2의 배수가 아니면 패딩
        if len(audio_data) % 2 != 0:
            audio_data = audio_data[:-1]  # 마지막 바이트 제거
            print("[STT Debug] 홀수 바이트 제거")
        
        audio_np = np.frombuffer(audio_data, dtype=np.int16)
        
        # RMS 체크 (볼륨 확인) - int16을 float32로 변환 후
        audio_float = audio_np.astype(np.float32) / 32768.0
        rms = np.sqrt(np.mean(audio_float**2))
        
        # 원본 main.py처럼 RMS 임계값을 높게 설정하여 배경 소음 차단
        if np.isnan(rms) or rms < 0.005:
            return {"text": ""}
        
        # Discord는 48kHz이므로 16kHz로 리샘플링
        if len(audio_np) > 0:
            from scipy import signal
            ratio = 48000 / 16000
            new_length = int(len(audio_np) / ratio)
            audio_np = signal.resample(audio_np, new_length).astype(np.int16)
        else:
            return {"text": ""}
        
        # int16 범위 체크
        max_val = np.abs(audio_np).max()
        
        if max_val < 100:
            return {"text": ""}
        
        # transcribe는 int16 numpy 배열을 받아서 내부에서 float32로 변환
        text = transcribe(audio_np)
        
        if not text:
            return {"text": ""}
        
        # 무시할 문구 체크
        text_lower = text.lower().strip()
        should_ignore = False
        
        if text_lower in IGNORE_PHRASES:
            should_ignore = True

        for phrase in IGNORE_PARTIAL:
            if phrase in text_lower:
                should_ignore = True
                break
        
        if should_ignore:
            return {"text": ""}
        
        print(f"[STT] {text}")
        return {"text": text}
        
    except Exception as e:
        print(f"[STT Error] {e}")
        traceback.print_exc()
        return {"text": ""}  # 오류 시 빈 문자열 반환


@app.post("/chat", response_model=ChatResponse)
async def chat(message: ChatRequest):
    """
    텍스트를 LLM에 보내고 응답 받기 (화자별 컨텍스트 포함)
    :param message: {"text": "사용자 메시지", "context": [], "speaker": "화자이름"}
    :return: {"text": "AI 응답", "style": "감정", "importance": 0}
    """
    try:
        user_input = message.text
        context = message.context or []
        speaker = message.speaker or "사용자"
        
        if not user_input:
            raise HTTPException(status_code=400, detail="텍스트가 없습니다")
        
        # 화자별 컨텍스트가 있으면 LLM에 전달
        if context:
            print(f"[Chat] {speaker}의 대화 컨텍스트 포함 ({len(context)}개 메시지)")
            # LLM 처리 (컨텍스트 포함)
            response = chat_with_gpt(user_input, context=context, speaker=speaker)
        else:
            # LLM 처리 (기본)
            response = chat_with_gpt(user_input)
        
        # 오류 체크
        if response.startswith("ERROR_"):
            raise HTTPException(status_code=500, detail="LLM 처리 중 오류 발생")
        
        # 감정 및 중요도 추출
        from llm import extract_emotion_and_clean
        cleaned_text, style, importance = extract_emotion_and_clean(response)
        
        return {
            "text": cleaned_text,
            "style": style,
            "importance": importance
        }
        
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Chat 오류: {str(e)}")


@app.post("/tts")
async def text_to_speech(message: TTSRequest):
    """
    텍스트를 음성 스트림으로 변환
    :param message: {"text": "텍스트", "style": "neutral"}
    :return: wav 스트림
    """
    try:
        text = message.text
        style = message.style
        
        if not text:
            raise HTTPException(status_code=400, detail="텍스트가 없습니다")
        
        # TTS 처리
        audio_data = synthesize(text, style=style)
        
        if audio_data is None:
            raise HTTPException(status_code=500, detail="TTS 처리 실패")
        
        # 스트림 반환 (Discord 봇이 메모리에서 직접 재생)
        return StreamingResponse(
            audio_data,
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=response.wav"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS 오류: {str(e)}")


@app.get("/health")
async def health_check():
    """헬스 체크"""
    return {"status": "ok"}


# ── 상태 점검 (Electron 앱 '상태' 탭용) ──────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _read_text(rel_path: str) -> str:
    try:
        with open(os.path.join(_BASE_DIR, rel_path), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

@app.get("/inspect/state")
async def inspect_state():
    """현재 페르소나 프롬프트와 모델 설정 (api_key 등 비밀값 제외)"""
    import character
    from llm import current_llm
    from tts import current_tts
    cfg = config or {}
    stt_cfg = cfg.get("stt", {}).get(cfg.get("stt", {}).get("provider", ""), {})
    return {
        "character": character.NAME,
        "prompt": character.read("prompt.txt"),
        "topic": character.read("topic.txt"),
        "llm": current_llm(),  # 런타임 교체가 반영되도록 config 스냅샷이 아닌 실제 provider 기준
        "stt": {k: stt_cfg.get(k) for k in
                ("model", "device", "compute_type", "beam_size", "hotwords")},
        "tts": current_tts(),  # 런타임 교체 반영
    }

# 관심 키워드 추출용 — 조사 제거(거친 휴리스틱)와 불용어
_JOSA = ("에서", "에게", "한테", "까지", "부터", "하고", "이랑", "처럼", "보다",
         "은", "는", "이", "가", "을", "를", "에", "의", "와", "과", "도", "로", "만", "랑")
_STOPWORDS = {
    "사용자", "요약", "대화", "user", "ai", "그리고", "그래서", "하지만",
    "지금", "오늘", "내일", "어제", "너무", "정말", "진짜", "그냥", "이제", "근데",
    "그런", "이런", "저런", "뭔가", "어떤", "대해", "대한", "있다", "없다", "했다",
    "한다", "하는", "같아", "같다", "좋아", "좋다", "위해", "통해", "함께", "다시",
    "약속", "이야기", "얘기", "말함", "물어봄", "질문", "답변", "응답", "확인",
}

# 요약문 특유의 동사형 꼬리 — 명사 키워드가 아니므로 제외
_VERB_TAILS = ("했다", "한다", "하다", "된다", "됐다", "였다", "이다", "니다",
               "어요", "아요", "해요", "하며", "하고", "해서", "되어", "했음", "함", "됨", "할")

def _extract_keywords(texts, top_n=20):
    """한국어 키워드 빈도 (형태소 분석기 없이 — 조사·동사 꼬리만 잘라내는 근사)"""
    import re as _re
    from collections import Counter
    counter = Counter()
    for text, weight in texts:
        for token in _re.findall(r"[가-힣A-Za-z]{2,}", text):
            low = token.lower()
            if any(low.endswith(t) for t in _VERB_TAILS):
                continue
            for josa in _JOSA:
                if low.endswith(josa) and len(low) - len(josa) >= 2:
                    low = low[: -len(josa)]
                    break
            if len(low) < 2 or low in _STOPWORDS:
                continue
            counter[low] += weight
    return [{"word": w, "count": c} for w, c in counter.most_common(top_n)]

@app.get("/inspect/conversation")
async def inspect_conversation(limit: int = 60, after: str = ""):
    """대화 한 줄씩 — 누가 무슨 말을 했고, 답했는지 넘겼는지, 얼마나 걸렸는지.

    컨트롤 앱이 2초마다 부른다. 서버 stdout을 읽는 것보다 정확하고, 화자·지연이 이미 나뉘어 있다.
    after에 마지막으로 받은 ts를 주면 그 뒤만 돌려준다.
    """
    import json as _json
    path = os.path.join(_BASE_DIR, "data", "metrics.jsonl")
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f.readlines()[-3000:]:
                try:
                    events.append(_json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass

    # 실제로 첫 소리가 나간 시각은 ttfa가 잰다 (합성이 턴 기록보다 늦게 끝나 tts_first_s는 자주 빈다).
    # 사람이 느끼는 시간 = 말끝(endpoint_s) + ttfa
    #
    # seq로 묶되 **가까운 것끼리** 묶는다. seq(turn_seq)는 접속마다 0으로 되돌아가므로
    # {seq: ms} 평평한 표를 쓰면 다른 세션의 값이 덮어쓴다 — 10-01 하루만 봐도 86턴 중
    # 29턴이 남의 지연을 달고 있었다 (먼저 말 걸기 실제 47.8초가 2.08초로 보였다).
    # ttfa는 턴 기록보다 먼저 나갈 수도, 늦게 나갈 수도 있어(긴 답은 첫 소리가 먼저다)
    # 앞뒤로 같이 찾는다. 같은 seq가 다시 나오기까지는 사건이 수십 개 끼므로 창은 좁게 둔다
    ttfa_at = {i: r for i, r in enumerate(events)
               if r.get("event") == "ttfa" and r.get("seq")}
    taken = set()
    for i, r in enumerate(events):
        if r.get("event") not in ("turn", "spoke") or not r.get("seq"):
            continue
        best = None
        for d in range(1, 13):                      # 가까운 쪽부터 — 뒤를 먼저 본다
            for j in (i + d, i - d):
                t = ttfa_at.get(j)
                if t is not None and j not in taken and t.get("seq") == r["seq"]:
                    best = j
                    break
            if best is not None:
                break
        if best is None:
            continue
        taken.add(best)
        ms = ttfa_at[best].get("ms")
        if ms:
            r["ttfa_s"] = round(ms / 1000.0, 3)
            r["felt_s"] = round((r.get("endpoint_s") or 0) + ms / 1000.0, 3)

    rows = []
    for r in events:
        if r.get("event") not in ("turn", "gate", "initiative", "spoke", "look", "read",
                                  "stt_timeout", "stt_skipped", "stt_recovered",
                                  "memory_error", "memory_misheard"):
            continue
        if after and (r.get("ts") or "") <= after:
            continue
        # 답한 턴은 turn 기록이 전부를 담고 있으므로 gate 쪽은 버린다 (중복 방지)
        if r.get("event") == "gate" and r.get("replied"):
            continue
        rows.append(r)
    return {"rows": rows[-limit:], "last_ts": rows[-1].get("ts") if rows else after}

@app.get("/inspect/turns")
async def inspect_turns(limit: int = 40):
    """최근 '말할 차례' 판단 — 답한 것/듣기만 한 것과 먼저 건 말 (metrics.jsonl).

    게이트가 몇 달간 한 번도 안 걸린 걸 아무도 몰랐던 이유가 볼 자리가 없어서였다.
    """
    import json as _json
    path = os.path.join(_BASE_DIR, "data", "metrics.jsonl")
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f.readlines()[-4000:]:
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("event") in ("gate", "initiative"):
                    rows.append(r)
    except OSError:
        pass
    rows = rows[-limit:][::-1]
    gates = [r for r in rows if r.get("event") == "gate"]
    return {
        "rows": rows,
        "replied": sum(1 for r in gates if r.get("replied")),
        "listened": sum(1 for r in gates if not r.get("replied")),
        "initiatives": sum(1 for r in rows if r.get("event") == "initiative"),
    }

@app.get("/inspect/memory")
async def inspect_memory():
    """장기기억 목록 + 단기 대화 + 관심 키워드 + 최근 RAG 참조 로그"""
    from memory import memory_manager

    # 장기기억 (에피소드·사람별 사실·캐릭터 자기 설정 — 최신순, 최대 200개)
    type_ko = {"episode": "있었던 일", "person": "사람", "self": "자기"}
    long_term = []
    try:
        col = memory_manager.store.collection
        data = col.get(include=["documents", "metadatas"], limit=500)
        for doc, meta in zip(data.get("documents") or [], data.get("metadatas") or []):
            meta = meta or {}
            label = type_ko.get(meta.get("type"), meta.get("type", ""))
            if meta.get("type") == "person":
                label += f" · {meta.get('name') or meta.get('who', '')}"  # who는 사용자 ID
            long_term.append({
                "timestamp": (meta.get("ts") or "").replace("T", " "),
                "summary": f"[{label}] {doc or ''}"[:300],
                "text": (doc or "")[:300],
                "type": meta.get("type", ""),
                "who": meta.get("name") or meta.get("who", ""),
                "due": meta.get("due", ""),
            })
        long_term.sort(key=lambda m: m["timestamp"], reverse=True)
        long_term = long_term[:200]
    except Exception as e:
        print(f"[Inspect] long-term read error: {e}")

    # 단기 대화 (최근 30개)
    short_term = []
    try:
        import json as _json
        path = os.path.join(_BASE_DIR, "data", "conversation_history.jsonl")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-30:]
            for line in lines:
                try:
                    short_term.append(_json.loads(line))
                except Exception:
                    pass
    except Exception as e:
        print(f"[Inspect] short-term read error: {e}")

    # 관심 키워드: 장기기억 요약(가중 2) + 단기 사용자 발화(가중 1)
    texts = [(m["summary"] or m["text"], 2) for m in long_term]
    texts += [(m.get("content", ""), 1) for m in short_term if m.get("role") == "user"]
    keywords = _extract_keywords(texts)

    return {
        "long_term": long_term,
        "long_term_count": len(long_term),
        "short_term": short_term,
        "keywords": keywords,
        "recent_retrievals": list(memory_manager.recent_retrievals),
        "recent_saves": list(memory_manager.recent_saves),
        "session_summary": memory_manager.session_summary(),
    }

@app.get("/inspect/memory/map")
async def inspect_memory_map(neighbors: int = 3, gate: float = 120.0,
                              layout: str = "pca", vectors: bool = False):
    """기억 지도 — 768차원 벡터를 2차원으로 눕히고(PCA), 가까운 것끼리 이어 준다.

    scripts/memory_map.py가 이걸 받아 마인드맵·벡터 지도 HTML을 만든다.
    거리는 검색에 쓰는 것과 같은 제곱 L2라, gate(기본 120)는 retrieve의 관련도 기준과 같은 눈금이다.
    """
    import numpy as np
    from memory import memory_manager

    col = memory_manager.store.collection
    data = col.get(include=["documents", "metadatas", "embeddings"], limit=2000)
    ids = list(data.get("ids") or [])
    docs = list(data.get("documents") or [])
    metas = [m or {} for m in (data.get("metadatas") or [])]
    raw = data.get("embeddings")
    embs = np.asarray(raw if raw is not None else [], dtype="float32")
    if not ids or embs.size == 0:
        return {"nodes": [], "edges": [], "people": {}, "explained": 0.0, "dims": 0}

    # 2차원으로 눕히기 — 기본은 SVD(PCA), layout=tsne면 덩어리가 잘 보이는 t-SNE
    centered = embs - embs.mean(axis=0)
    u, sv, _ = np.linalg.svd(centered, full_matrices=False)
    coords = u[:, :2] * sv[:2]
    var = sv ** 2
    explained = float(var[:2].sum() / var.sum()) if float(var.sum()) else 0.0
    used = "PCA"
    if layout == "tsne" and len(ids) >= 5:
        try:
            from sklearn.manifold import TSNE
            coords = TSNE(n_components=2, perplexity=max(2, min(30, (len(ids) - 1) // 3)),
                          init="pca", random_state=0).fit_transform(embs)
            explained, used = None, "t-SNE"
        except Exception as e:
            print(f"[Inspect] t-SNE 실패, PCA로 대신합니다: {e}")

    # 서로의 거리 (검색과 같은 제곱 L2)
    sq = (embs ** 2).sum(axis=1)
    d2 = sq[:, None] + sq[None, :] - 2.0 * embs @ embs.T
    np.fill_diagonal(d2, np.inf)

    k = max(1, min(int(neighbors), len(ids) - 1)) if len(ids) > 1 else 0
    seen = {}
    for i in range(len(ids)):
        if k <= 0:
            break
        for j in np.argsort(d2[i])[:k]:
            j = int(j)
            dist = float(d2[i, j])
            if dist > gate:
                continue
            key = (min(i, j), max(i, j))
            seen[key] = round(dist, 1)

    people = {}
    try:
        import json as _json
        with open(os.path.join(_BASE_DIR, "data", "people.json"), "r", encoding="utf-8") as f:
            people = {k: (v.get("name") or k) for k, v in (_json.load(f) or {}).items()}
    except Exception:
        pass

    nodes = []
    for i, doc in enumerate(docs):
        meta = metas[i]
        who = meta.get("who") or ""
        nodes.append({
            "id": ids[i],
            "text": doc or "",
            "type": meta.get("type") or "",
            "kind": meta.get("kind") or "",
            # 무엇에 관한 기억인가 (memory.topics). 마인드맵이 이걸로 갈래를 묶는다 —
            # type은 "누구의 기억인가"뿐이어서 작품에 대한 사실이 사람 밑에 섞여 있었다
            "about": meta.get("about") or "",
            "who": who,
            "name": meta.get("name") or people.get(who) or who,
            "ts": meta.get("ts") or "",
            "due": meta.get("due") or "",
            "x": round(float(coords[i, 0]), 4),
            "y": round(float(coords[i, 1]), 4),
        })

    out = {
        "nodes": nodes,
        "edges": [{"a": a, "b": b, "d": dist} for (a, b), dist in sorted(seen.items())],
        "people": people,
        "explained": round(explained, 3) if explained is not None else None,
        "layout": used,
        "dims": int(embs.shape[1]),
        "gate": gate,
    }
    if vectors:  # t-SNE처럼 다른 방식으로 눕혀 보려면 원벡터가 필요하다
        out["vectors"] = np.round(embs, 4).tolist()
    return out

def _log_event(record: dict) -> None:
    """보기·읽기 같은 사건을 대화 기록과 같은 metrics.jsonl에 남긴다.
    기록 자체가 실패해도 기능을 멈추지는 않는다"""
    try:
        from websocket_handler import websocket_handler as _wh
        _wh.log_metrics(record)
    except Exception as e:
        print(f"[Metrics] 기록 실패(무시): {type(e).__name__}: {e}")


# Echo 테스트 엔드포인트
class EchoRequest(BaseModel):
    text: str

@app.post("/echo")
async def echo(inp: EchoRequest):
    """에코 테스트"""
    return {"reply": f"echo: {inp.text}"}


# 링크로 받은 글을 같이 읽기 — 음성으로는 URL을 말할 수 없어 디스코드 텍스트 채널로 들어온다
class ReadRequest(BaseModel):
    url: str = None     # 비우면 지금 보고 있는 것을 치운다


@app.post("/read")
async def read(inp: ReadRequest):
    """링크의 본문을 뽑아 [지금 보고 있는 것]에 걸어 둔다. url 없이 부르면 치운다"""
    import asyncio
    import llm as _llm
    if not (inp.url or "").strip():
        _llm.set_reading(None, None, None)
        return {"ok": True, "cleared": True}
    import read_url
    # 받아오는 동안(최대 8초) 대화는 계속 돌아간다 — 화면 보기와 같은 이유로 알려 둔다
    _llm.set_pending_look("링크의 글")
    try:
        d = await asyncio.get_event_loop().run_in_executor(None, read_url.fetch, inp.url.strip())
    except ValueError as e:
        _llm.set_pending_look(None)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _llm.set_pending_look(None)
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
    _llm.set_reading(d["url"], d["title"], d["text"])
    print(f"[Read] {d['title'][:40] or d['url'][:40]} — {d['chars']}자")
    _log_event({"event": "read", "url": d["url"][:200], "title": d["title"][:80],
                "chars": d["chars"]})
    return {"ok": True, "title": d["title"], "chars": d["chars"], "url": d["url"]}


@app.get("/read")
async def read_status():
    """지금 무엇을 보고 있는지 (컨트롤 앱·디버그용)"""
    import llm as _llm
    r = _llm.current_reading()
    p = _llm.pending_look()
    return {"reading": None if not r else
            {"url": r["url"], "title": r["title"], "chars": len(r["text"]),
             "at": r["at"].isoformat(timespec="seconds")},
            # 아직 가져오는 중인 것 — 보기 탭이 "가져오는 중"을 보여줄 수 있게
            "pending": None if not p else
            {"what": p["what"], "at": p["at"].isoformat(timespec="seconds")}}


# 화면 보기 — OBS처럼 이름 붙인 소스에서 찍어 OCR(글자)·VLM(설명)으로 읽는다.
# 결과는 링크 읽기와 같은 [지금 보고 있는 것] 칸으로 들어간다 (기능별 자리를 파지 않는다)
class LookRequest(BaseModel):
    source: str = None       # config의 screen.sources 이름
    override: dict = None    # 그때만 쓰는 정의 ({"type":"region","bbox":[...]})
    mode: str = "both"       # ocr · vlm · both


def _screen_cfg():
    return (load_config() or {}).get("screen", {}) or {}


# 마지막으로 본 소스. "이거 봐줘"라고 할 때 보여주려는 건 앞에 띄운 창인데, 명령을 치는
# 순간 디스코드(또는 컨트롤 앱)가 앞으로 온다 — 그래서 "지금 앞 창"은 답이 아니다.
# 한 번 고르면 그 뒤로는 그걸 쓰는 게 실제 쓰임에 맞는다 (10-01: 계속 전체 화면이 찍혔다)
_last_source = None


@app.get("/look/windows")
async def look_windows():
    """지금 보이는 창 목록 — 컨트롤 앱에서 소스를 고를 때 쓴다"""
    import screen
    return {"windows": screen.list_windows()}


@app.get("/look/last.png")
async def look_last_shot():
    """마지막으로 찍은 화면 — 보여주려던 것과 찍힌 것이 다를 때 눈으로 확인하는 용도"""
    import os
    from fastapi.responses import FileResponse
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "logs", "last_look.png")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="아직 찍은 화면이 없습니다")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@app.get("/look/sources")
async def look_sources():
    """설정에 정의된 소스들"""
    cfg = _screen_cfg()
    return {"default": cfg.get("default"), "last": _last_source,
            "sources": cfg.get("sources", {})}


@app.post("/look")
async def look(inp: LookRequest):
    """화면을 보고 [지금 보고 있는 것]에 걸어 둔다"""
    import asyncio
    import llm as _llm
    import vision
    cfg = _screen_cfg()
    sources = cfg.get("sources", {}) or {}
    global _last_source
    name = inp.source or _last_source or cfg.get("default") or "전체"
    if name not in sources and not inp.override:
        name = cfg.get("default") or next(iter(sources), "전체")   # 지워진 소스를 계속 붙잡지 않게
    src = dict(inp.override) if inp.override else dict(sources.get(name) or {})
    if not src:
        raise HTTPException(status_code=400,
                            detail=f"'{name}' 소스가 설정에 없습니다 (있는 것: {', '.join(sources) or '없음'})")
    mode = inp.mode if inp.mode in ("ocr", "vlm", "both") else "both"
    # both는 VLM을 매번 새로 올려 30초쯤 걸린다 — 그 동안에도 대화는 돌아가므로
    # (1) 가져오는 중이라고 알리고 (2) OCR이 끝나는 대로 글자부터 걸어 둔다.
    # 없을 때 10-01에 "방금 보신 거면 제가 못 봤을 텐데"로 답했다
    title = f"화면 — {name}"
    _llm.set_pending_look(title)

    def partial(text):
        _llm.set_reading(f"screen:{name}", title + " (글자만, 설명은 아직)", text)
        _llm.set_pending_look(title)   # 설명은 여전히 오는 중이다

    try:
        r = await asyncio.get_event_loop().run_in_executor(
            None, vision.look, src, mode, partial)
    except ValueError as e:
        _llm.set_pending_look(None)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _llm.set_pending_look(None)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    if not (r.get("text") or "").strip():
        _llm.set_pending_look(None)
        raise HTTPException(status_code=422,
                            detail="화면에서 읽을 것이 없습니다: " + "; ".join(r.get("notes") or []))
    if not inp.override:
        _last_source = name        # 다음 번 "봐줘"가 같은 것을 본다
    _llm.set_reading(f"screen:{name}", title, r["text"])
    print(f"[Look] {name} ({mode}) · {r.get('took')} · {len(r['text'])}자")
    # 본 것을 metrics에 남긴다 — 10-01 세션은 전부 화면 테스트였는데 언제 무엇을 몇 초에
    # 봤는지 기록이 없어서, 느려진 턴과 겹쳐 보는 것조차 불가능했다 (print만 있었다)
    _log_event({"event": "look", "source": name, "mode": mode, "chars": len(r["text"]),
                "ocr_lines": len(r.get("ocr") or []), "took": r.get("took"),
                "notes": r.get("notes")})
    return {"ok": True, "source": name, "mode": mode, "took": r.get("took"),
            "notes": r.get("notes"), "chars": len(r["text"]),
            "seen": r.get("seen"), "ocr_lines": len(r.get("ocr") or [])}


# 원고 같이 읽기 — 구글 문서(또는 웹 에디터 화면 OCR)를 주기적으로 읽어, 새로 쓴 대목에 가끔 한마디 한다.
# 파일 감시가 아닌 이유는 웹 에디터에 쓰면 로컬 파일이 없기 때문이고, VLM을 안 쓰는 이유는 VRAM이다
class ManuscriptRequest(BaseModel):
    on: bool = True          # true면 글쓰기 상태로 고정, false면 대화로 고정
    source: str = None       # screen 모드일 때 config의 screen.sources 이름
    auto: bool = False       # true면 고정을 풀고 자동 전환(원고 수정·소설 이야기)으로 돌린다


@app.post("/manuscript")
async def manuscript(inp: ManuscriptRequest):
    """원고 세션을 켜고 끈다"""
    import asyncio
    from websocket_handler import websocket_handler as _wh
    import llm as _llm
    m, act = _wh.manuscript, _wh.activity
    if inp.auto:
        act.unpin()
        print("[Manuscript] 자동 전환으로 돌림")
        return {"ok": True, **m.status(), **act.status()}
    if not inp.on:
        from activity import TALK
        act.pin(TALK, "직접 끔")
        m.stop()
        _llm.set_reading(None, None, None)   # 들고 있던 원고를 놓는다
        print("[Manuscript] 끔 (대화 상태로 고정)")
        return {"ok": True, **m.status(), **act.status()}
    name = inp.source
    if m.provider != "gdocs":
        # 화면에서 읽을 때만 소스 이름이 필요하다. 구글 문서는 config의 manuscript.gdocs를 쓴다
        cfg = _screen_cfg()
        sources = cfg.get("sources", {}) or {}
        name = name or m.source or _last_source or cfg.get("default")
        if name not in sources:
            raise HTTPException(
                status_code=400,
                detail=f"'{name}' 소스가 설정에 없습니다 (있는 것: {', '.join(sources) or '없음'})")
    from activity import WRITING
    act.pin(WRITING, "직접 켬")
    m.start(name)
    # 켠 직후 한 번 읽어 둔다 — 첫 한마디까지 poll_sec을 기다리지 않게.
    # 창을 못 찾는 것 같은 설정 문제는 그 문장을 그대로 보여 준다 (500으로 삼키면
    # "왜 안 되는지"가 서버 로그에만 남는다 — 보기 패널에서 같은 교훈을 얻었다)
    try:
        text, note = await asyncio.get_event_loop().run_in_executor(None, _wh.read_manuscript)
    except Exception as e:
        m.stop(); act.unpin()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    if not text and ("소스" in note or "찾지 못" in note or "연결" in note
                     or "folder_id" in note or "권한" in note):
        m.stop(); act.unpin()         # 설정이 틀린 채로 켜 두지 않는다
        raise HTTPException(status_code=400, detail=note)
    m.update(text, note)
    if text:
        title, body = m.reading_block()
        _llm.set_reading(f"manuscript:{name or m.provider}", title, body)
    print(f"[Manuscript] 켬 — {m.provider}/{name or '-'} · {len(text)}자 · {note}")
    return {"ok": True, **m.status(), **act.status()}


@app.get("/manuscript")
async def manuscript_status():
    """원고·상태 (컨트롤 앱·디버그용)"""
    from websocket_handler import websocket_handler as _wh
    return {**_wh.manuscript.status(), **_wh.activity.status()}


# 전사(스크립트) 모드 — 어시스턴트와 분리된 전용 STT 경로
from transcribe_api import router as transcribe_router
app.include_router(transcribe_router)

# LLM 모델 교체 (컨트롤 패널)
from llm_api import router as llm_router
app.include_router(llm_router)

# TTS 엔진 교체 (컨트롤 패널)
from tts_api import router as tts_router
app.include_router(tts_router)



# WebSocket Endpoint
from websocket_handler import websocket_handler

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket_handler.handle_connection(websocket)

if __name__ == "__main__":
    # 로컬 전용 바인딩 (무인증 WS/REST이므로 외부 노출 금지)
    uvicorn.run("api_server:app", host="127.0.0.1", port=8000, reload=True)

