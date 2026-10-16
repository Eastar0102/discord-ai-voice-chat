"""
💬 LLM 진입점 (오케스트레이션 래퍼)
- 엔진 호출은 config의 llm.provider로 선택된 Provider에 위임 (providers/llm/)
- 여기 남는 것은 LLM-비종속 오케스트레이션: RAG 검색, 프롬프트/히스토리 조립,
  대화 저장, 응답 태그 파싱
"""

import difflib
import json
import re
import os
import asyncio
import threading
import time
from datetime import datetime

import character
from config_loader import load_config
from providers.llm import get_provider

# 설정 로딩 (환경변수 포함)
config = load_config()

# LLM Provider 선택 (config의 llm 섹션)
_llm_cfg = config.get("llm", {})
_provider_name = _llm_cfg.get("provider", "openai_chat")
_provider = get_provider(_provider_name, _llm_cfg.get(_provider_name, {}))
_switch_lock = threading.Lock()

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "config.json")


def current_llm() -> dict:
    """현재 사용 중인 provider/모델과 생성 파라미터 (api_key 제외)"""
    opts = _llm_cfg.get(_provider_name, {})
    return {"provider": _provider_name,
            **{k: opts.get(k) for k in ("model", "temperature", "max_tokens", "top_p")}}


_warm = False    # 기동 워밍업이 끝났는가 (입장 인사가 이걸 기다린다)


def is_warm() -> bool:
    """기동 워밍업이 끝났는지. 실패해도 True가 된다 — 기다릴 이유가 없어졌다는 뜻이다"""
    return _warm


def warmup_current():
    """현재 provider를 미리 로드 (서버 기동 시). 로컬 모델은 첫 요청 때 VRAM 로딩을 기다리느라
    첫 응답이 15초씩 걸렸다 — 모델 교체 때만 하던 워밍업을 기동 시에도 한다."""
    global _warm
    if not hasattr(_provider, "warmup"):
        _warm = True
        return
    try:
        t = time.time()
        _provider.warmup()
        _warm_prompt(_provider)
        print(f"LLM warmup: {_provider_name}/{getattr(_provider, 'model', None)} ({time.time() - t:.1f}s)")
    except Exception as e:
        print(f"Warning: LLM warmup failed (첫 응답이 느릴 수 있음): {e}")
    finally:
        _warm = True


def _warm_prompt(prov):
    """cache 배치 provider면 성격 프롬프트와 기록을 미리 prefill해 둔다. 서버를 띄운 직후의 첫 턴은
    GGUF가 디스크에서 처음 올라오는 값까지 치러 10초 넘게 걸릴 수 있다 — 입장 인사가 바로 그 첫
    턴이다. 실패해도 대화에는 영향이 없다"""
    if not getattr(prov, "cache_layout", False) or not hasattr(prov, "prefetch"):
        return
    try:
        t = time.time()
        asyncio.run(prov.prefetch(build_messages("안녕")))
        print(f"LLM prompt warmup ({time.time() - t:.1f}s)")
    except Exception as e:
        print(f"Warning: prompt warmup failed: {e}")


def set_llm(provider: str, model: str, persist: bool = True) -> dict:
    """
    런타임 LLM 교체 (컨트롤 패널용). 새 provider를 먼저 만들고 로컬 모델이면 VRAM 로딩까지
    끝낸 뒤 바꿔 끼운다 — 실패하면 기존 provider가 그대로 남는다. 진행 중인 스트림은
    이미 잡고 있는 이전 인스턴스로 끝까지 간다. 선택은 config.json에 저장돼 재시작 후에도 유지.

    **모델은 사람만 바꾼다.** 상태(activity)에 따라 클라우드 모델로 자동 전환하면 모르는 사이에
    토큰이 나가고, 개인 문서가 외부 API로 간다. 어느 모델로 돌릴지는 컨트롤 패널에서 고른다
    """
    global _provider, _provider_name
    with _switch_lock:
        options = {**_llm_cfg.get(provider, {}), "model": model}
        new = get_provider(provider, options)
        if hasattr(new, "warmup"):
            new.warmup()

        old, old_name = _provider, _provider_name
        _provider, _provider_name = new, provider
        _llm_cfg["provider"] = provider
        _llm_cfg[provider] = options
        _warm_prompt(new)      # 바꿔 끼운 뒤에 — build_messages가 지금 provider의 배치·프롬프트를 쓴다

        # 다른 로컬 모델로 바꿨으면 이전 모델의 VRAM 반환 (STT와 같은 GPU를 쓰므로)
        if hasattr(old, "release") and (old_name, getattr(old, "model", None)) != (provider, model):
            old.release()

        if persist:
            _persist_selection(provider, model)
        print(f"LLM switched: {old_name}/{getattr(old, 'model', None)} → {provider}/{model}"
              + ("" if persist else " (자동 — 저장하지 않음)"))
        return current_llm()


def _persist_selection(provider: str, model: str):
    """config.json의 llm.provider / llm.<provider>.model만 갱신 (주입된 api_key가 새지 않게 원본을 다시 읽음)"""
    with open(CONFIG_PATH, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    raw = json.loads(text)
    raw.setdefault("llm", {})["provider"] = provider
    raw["llm"].setdefault(provider, {})["model"] = model
    # 기존 줄바꿈 유지 — Windows 텍스트 모드 기본값(CRLF)으로 쓰면 git diff가 파일 전체가 된다
    eol = "\r\n" if "\r\n" in text else "\n"
    with open(CONFIG_PATH, "w", encoding="utf-8", newline=eol) as f:
        f.write(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


# 만든 사람(주인)으로 인식할 닉네임 (대소문자·앞뒤공백 무시). 여러 닉네임을 같은 사람으로 본다.
# 다른 사람의 닉네임을 넣으면 그 사람을 만든 사람으로 대하니, 본인 것만 넣는다
OWNER_NAMES = {n.strip().lower() for n in character.OWNER_NAMES}

# prompt 텍스트 불러오기
def read_config_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

# 성격 프롬프트와 말투 예시는 캐릭터 팩에서 읽는다
base_dir = os.path.dirname(os.path.dirname(__file__))
PROMPT_TEMPLATE = character.read("prompt.txt")
TOPIC = character.read("topic.txt")

# 모델마다 다른 프롬프트를 쓰고 싶을 때 (예: 짧은 프롬프트로 학습한 모델).
# config llm.prompt_profiles: {"모델 이름 접두사": "프롬프트 파일"} — 접두사가 가장 긴 것을 쓴다
PROMPT_PROFILES = {prefix: path for prefix, path in _llm_cfg.get("prompt_profiles", {}).items()
                   if not prefix.startswith("_")}
_profile_cache = {}


def prompt_for_model(model: str):
    """(프롬프트 틀, 말투 예시) — prompt_profiles에 걸리면 그 파일, 아니면 캐릭터 팩의 prompt.txt"""
    matches = [p for p in PROMPT_PROFILES if model and model.startswith(p)]
    if not matches:
        return PROMPT_TEMPLATE, TOPIC
    path = PROMPT_PROFILES[max(matches, key=len)]
    if path not in _profile_cache:
        _profile_cache[path] = read_config_file(path if os.path.isabs(path) else os.path.join(base_dir, path))
    return _profile_cache[path], ""

HISTORY_PATH = os.path.join(base_dir, "data", "conversation_history.jsonl")
# 최근 20줄(약 10왕복). 10줄일 땐 끊긴 발화 조각 몇 개만으로 맥락이 밀려났다
HISTORY_LIMIT = 20
HISTORY_STEP = 10      # cache 배치에서 창이 한 번에 밀리는 줄 수 (load_conversation_history)


# 🔁 대화 기록 로딩
def load_conversation_history(stepped: bool = False):
    """모델에게 보여 줄 최근 기록. stepped=True면 창이 매 턴 한 줄씩 밀리지 않고
    HISTORY_STEP줄마다 한 번에 밀린다 (길이 HISTORY_LIMIT ~ HISTORY_LIMIT+HISTORY_STEP-1) —
    맨 앞줄이 바뀌면 그 뒤가 전부 다시 prefill되므로, cache 배치(build_messages)가 쓴다"""
    history = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    history.append(json.loads(line.strip()))
                except json.JSONDecodeError as e:
                    print(f"Warning: JSON parse error: {e}")
                    continue
                except Exception as e:
                    print(f"Warning: Load error: {e}")
                    continue
    if not stepped:
        return prepare_history(trim_window(history))
    # 기준은 파일의 줄 수 — 창 안에서 중복을 걸러 내도 시작점이 흔들리지 않게
    extra = (len(history) - HISTORY_LIMIT) % HISTORY_STEP if len(history) > HISTORY_LIMIT else 0
    return prepare_history(trim_window(history, HISTORY_LIMIT + HISTORY_STEP)[-(HISTORY_LIMIT + extra):])


# 지난 세션에서 가져올 최대 줄 수. 지난 세션 대답이 창을 채우고 있으면 작은 모델은 그걸 그대로
# 베낀다 (같은 질문에 지난 세션 20줄을 주면 이전 대답을 토씨까지 복사했고, 창이 깨끗하면 안 그랬다).
# 다만 창을 통째로 비우면 대답이 장황해져서(형식은 유지) 몇 줄은 남긴다
PREV_SESSION_KEEP = 6


def _ts_of(row: dict):
    try:
        return datetime.fromisoformat(row.get("ts"))
    except (TypeError, ValueError):
        return None


def trim_window(history: list, limit: int = None) -> list:
    """모델에게 보여 줄 최근 대화 — 세션이 바뀌었으면 지난 세션은 조금만, 같은 대답은 한 번만"""
    limit = limit or HISTORY_LIMIT
    rows = history[-(limit * 2):]

    # 1) 세션 경계(30분 공백) 뒤가 이번 세션 — 그 앞은 PREV_SESSION_KEEP 줄까지만
    cut = 0
    for i in range(len(rows) - 1, 0, -1):
        a, b = _ts_of(rows[i - 1]), _ts_of(rows[i])
        if a and b and (b - a).total_seconds() >= HISTORY_GAP_NOTE_MIN * 60:
            cut = i
            break
    if cut:
        rows = rows[max(0, cut - PREV_SESSION_KEEP):]

    # 2) 같은 대답이 여러 번 있으면 마지막 것만 남긴다 (그 앞의 사용자 줄과 함께 버린다) —
    #    같은 문장이 창에 두 번 들어가면 다음 대답도 그 문장이 된다
    seen, keep, i = set(), [], len(rows) - 1
    while i >= 0:
        r = rows[i]
        if r.get("role") == "assistant":
            key = re.sub(r"\s+|\[importance:[^\]]*\]|^\s*\(\w+\)", "", r.get("content", ""))[:60]
            if key and key in seen:
                i -= 1
                if i >= 0 and rows[i].get("role") == "user":
                    i -= 1  # 짝이 되는 앞의 사용자 줄까지 함께 버린다 (짝이 어긋나면 더 이상해진다)
                continue
            seen.add(key)
        keep.append(r)
        i -= 1
    return list(reversed(keep))[-limit:]


def prepare_history(history: list) -> list:
    """
    과거 대화를 모델에 넣기 전에 현재 출력 규격으로 정규화한다.
    히스토리는 형식 few-shot으로 작용한다 — 모델은 규칙보다 직전 대화의 모양을 따라 한다.
    - 태그를 지우면 안 된다: 예전엔 태그를 떼고 넣었더니 대화가 길어질수록 태그 없는 답만 나왔다
      (41턴 중 감정 태그 0, 중요도 태그 3 → 감정 음성·장기기억 저장이 사실상 꺼짐)
    - 괄호 지문은 지운다: 남겨두면 그 형식을 따라 하고 다시 저장돼 스스로 강화된다
    - 구형식·누락·한국어 태그는 모두 "(style) 대사 [importance: N]" 하나로 맞춘다
    """
    cleaned = []
    for m in history:
        m = dict(m)
        if m.get("role") == "assistant" and m.get("content"):
            text, style, importance = extract_emotion_and_clean(m["content"])
            text = strip_stage_directions(cut_document(text))
            if not text:
                continue  # 지문뿐이던 응답은 빈 턴이 되므로 버린다
            m["content"] = f"({style}) {text} [importance: {importance}]"
        cleaned.append({"role": m.get("role"), "content": m.get("content", "")})  # ts 등은 모델에 넘기지 않는다
    return cleaned


# 대답 안에서 여기부터는 말이 아니라 문서다 — 구분선, 굵은 머리말, 목록, 번호 매김.
# 모델이 프롬프트에 들어간 요약 블록을 그대로 옮겨 적을 때 이 모양이 된다.
# 읽을 때(스트리밍)와 과거 기록을 다시 넣을 때 같은 기준을 써야 한다 — 기록에 남은 문서를
# 그대로 되먹이면 작은 모델이 그 모양을 따라 한다
DOC_START_RE = re.compile(r"(\s*-{3,}|\s*\*\*[^*\n]{1,20}\*\*\s*:|\n\s*[-•*]\s|\n\s*\d+[.)]\s)")
# 굵은 머리말은 **를 떼고 나면 남지 않는다 — 원문에서만 보이는 신호
DOC_BOLD_RE = re.compile(r"\*\*[^*\n]{1,20}\*\*\s*:")


def cut_document(text: str) -> str:
    """문서 모양이 시작되면 그 앞까지만 남긴다"""
    m = DOC_START_RE.search(text)
    return text[:m.start()].rstrip(" -•*\t") if m else text


# 괄호 지문 "(당황한 듯 눈을 깜빡이며)"과 별표 행동 "*헤드셋을 만진다*" — 음성으로 읽을 내용이 아니다.
# 감정 태그 (happy) 도 괄호라 함께 지워지므로, 태그가 필요하면 먼저 추출한 뒤 호출한다
STAGE_DIRECTION_RE = re.compile(r"\([^()]*\)|\*[^*\n]*\*")


def strip_stage_directions(text: str) -> str:
    text = STAGE_DIRECTION_RE.sub(" ", text.replace("**", ""))
    return re.sub(r"\s{2,}", " ", text).strip()

# 💾 대화 저장
def save_to_history(role, content, uid=None, stt=None) -> str:
    # ts: 기록만으로는 어제 밤 대화의 끝과 오늘 첫마디가 바로 이어진 것처럼 보였다 — 공백을 알리는 데 쓴다
    # uid: 말한 사람의 디스코드 ID — 기억은 닉네임이 아니라 ID로 묶는다
    # stt: 그 줄을 받아 적은 Whisper의 확신도 — 잘못 들은 줄이 기억으로 굳지 않게 memory가 읽는다
    row = {"role": role, "content": content, "ts": datetime.now().isoformat(timespec="seconds")}
    if uid:
        row["uid"] = str(uid)
    if stt and stt.get("avg_logprob") is not None:
        row["stt_lp"] = stt["avg_logprob"]
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row["ts"]


# 지금 같이 보고 있는 글 (링크로 받은 기사 등). 한 편만 들고 있는다 — 여러 개를 쌓으면
# num_ctx를 기사로 다 쓴다. 오래되면 잊는다: 어제 본 기사를 오늘 얘기하면 이상하다
READING_TTL_MIN = int(_llm_cfg.get("reading_ttl_min", 30))
_reading = None   # {"url", "title", "text", "at": datetime}
# 화면을 가져오는 데 걸리는 시간 (both는 VLM을 매번 새로 올려 30초쯤) 동안 그 사실을 알린다 —
# 없으면 "제가 못 봤을 텐데, 다시 보여주세요"라고 답하거나 30분 TTL이 남은 **지난 화면**을
# 설명한다. 가져오기가 끝나면 _pending은 치워진다
_pending_look = None   # {"what": "화면 — 전체", "at": datetime}
PENDING_LOOK_TTL_SEC = 180


def set_reading(url: str, title: str, text: str) -> None:
    """같이 볼 글을 걸어 둔다 (POST /read · /look). None을 주면 치운다"""
    global _reading, _pending_look
    _pending_look = None
    _reading = None if not text else {"url": url, "title": title or "", "text": text,
                                      "at": datetime.now()}


def set_pending_look(what: str) -> None:
    """지금 화면(또는 링크)을 가져오는 중이라고 표시한다. None이면 치운다"""
    global _pending_look
    _pending_look = {"what": what, "at": datetime.now()} if what else None


def pending_look():
    """아직 가져오는 중인 것 (너무 오래되면 잊는다 — 실패했는데 영구히 남지 않게)"""
    global _pending_look
    if _pending_look and (datetime.now() - _pending_look["at"]).total_seconds() > PENDING_LOOK_TTL_SEC:
        _pending_look = None
    return _pending_look


def current_reading():
    """아직 유효한 읽을거리 (시간이 지나면 저절로 없어진다)"""
    global _reading
    if _reading and (datetime.now() - _reading["at"]).total_seconds() > READING_TTL_MIN * 60:
        _reading = None
    return _reading


# 마지막 대화에서 이만큼 지났으면 "오랜만에 다시 시작한 대화"로 알린다
HISTORY_GAP_NOTE_MIN = 30


def ago_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}분"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간"
    return f"{hours // 24}일"


def history_gap_note():
    """대화 기록의 마지막 줄이 오래전이면 그 공백을 알리는 문장 (시각 없는 옛 기록이면 None)"""
    if not os.path.exists(HISTORY_PATH):
        return None
    with open(HISTORY_PATH, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        tail = f.read().decode("utf-8", errors="ignore").strip().splitlines()
    try:
        ts = json.loads(tail[-1]).get("ts") if tail else None
    except json.JSONDecodeError:
        ts = None
    if not ts:
        return None
    gap = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
    if gap < HISTORY_GAP_NOTE_MIN * 60:
        return None
    return (f"[대화 공백] 위 대화 기록의 마지막 말은 {ago_text(gap)} 전이다. 지금은 그 뒤 처음 다시 말을 건 것이니, "
            "앞 대화를 방금 이어지던 것처럼 받지 마라.")


def retrieve_memory(user_input: str, speaker: str = None, prev_user: str = None, speaker_id: str = None) -> str:
    """
    장기기억 조회 (RAG) — 지금 말하는 사람에 대한 사실, 캐릭터가 전에 한 말, 관련 있었던 일.
    직전 사용자 발화를 검색어에 합쳐 "그거" 같은 지시어에도 걸리게 한다
    """
    try:
        from memory import memory_manager
        return memory_manager.retrieve_relevant_memory(user_input, speaker=speaker, prev_user=prev_user,
                                                       speaker_id=speaker_id)
    except Exception as e:
        print(f"Warning: memory retrieval failed: {e}")
        return ""


def session_summary() -> str:
    """이번 대화에서 20줄 창 밖으로 밀려난 앞부분의 요약"""
    try:
        from memory import memory_manager
        return memory_manager.session_summary()
    except Exception as e:
        print(f"Warning: session summary failed: {e}")
        return ""


_SPEAKER_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")


def retrieve_lore(user_input: str) -> str:
    """
    사용자 발화와 관련된 캐릭터 세계관(자체 설정)을 의미 검색으로 조회 ($MEMORY와 별도 채널)
    """
    try:
        from lore import get_lore_manager
        return get_lore_manager().retrieve_relevant_lore(user_input, n_results=2)
    except Exception as e:
        print(f"Warning: lore retrieval failed: {e}")
        return ""


# prefetch가 부분 문장으로 검색한 기억 — 최종 턴이 다시 쓴다. 최종 문장으로 다시 검색하면 칸이
# 한 글자만 달라도 거기서 캐시가 끊겨 prefetch가 무의미해진다 (실측: 0.15s가 1.58s로).
# 부분 문장은 최종의 60~90%라 검색 결과가 거의 같다. 같은 사람이 같은 기록 위에서 한 말일 때만,
# 그리고 PREFETCH_REUSE_SEC 안에서만 쓴다
PREFETCH_REUSE_SEC = 30
_prefetched = {}


def _retrieve_for_turn(user_input, speaker, prev_user, speaker_id, past, prefetching, cache) -> tuple:
    key = (speaker_id or speaker, len(past), past[-1].get("ts") if past else None)
    if cache and not prefetching:
        hit = _prefetched.pop("turn", None)
        if hit and hit["key"] == key and time.time() - hit["at"] < PREFETCH_REUSE_SEC:
            return hit["memory"], hit["lore"]
    memory_text = retrieve_memory(user_input, speaker, prev_user, speaker_id)
    lore_text = retrieve_lore(user_input)
    if cache and prefetching:
        _prefetched["turn"] = {"key": key, "at": time.time(), "memory": memory_text, "lore": lore_text}
    return memory_text, lore_text


def build_messages(user_input: str, context: list = None, speaker: str = None, situation: str = None,
                   prefix_speaker: bool = True, speaker_id: str = None, history_limit: int = None,
                   prefetching: bool = False) -> list:
    """
    시스템 프롬프트(RAG 기억 포함) + 상황 정보 + 대화 기록 + 사용자 발화로 메시지 목록 구성
    prefix_speaker=False: 사용자 발화가 아닌 지시문(입장 인사 등)이라 [이름]을 붙이지 않는다
    history_limit=0: 대화 기록을 아예 넣지 않는다. 채널에 막 들어왔을 때처럼 '진행 중인 대화'가
        없는 경우다 — 기록을 주면 마지막 줄을 방금 들은 말로 받아 거기에 대꾸한다
        (두 시간 전 대화의 마지막 줄에 "저 역시…"로 답하며 입장한 적이 있다)
    prefetching=True: 말하는 중의 부분 문장으로 미리 먹이는 호출 — 검색한 기억을 남겨 두고
        최종 턴이 그대로 다시 쓴다 (cache 배치에서만. 한 글자만 달라도 거기서 캐시가 끊긴다)

    provider에 cache_layout이 켜져 있으면 배치가 바뀐다 (llama.cpp):
        system = 성격 프롬프트(기억·세계관 칸을 뺀 것) + 대화 요약 + [지금 보고 있는 것]
        기록   = 10줄 단위로만 밀리는 창 (load_conversation_history(stepped=True))
        system = [기억] [세계관] [현재 상황] [화자 정보] [현재 시각]   ← 턴마다 바뀌는 칸
        user
    실측(실제 세션 24턴 재생, 30B MoE `--cpu-moe`): 첫 토큰 2.59s → 1.6s, 다시 계산하는
    양 2,900 → 900~1,400토큰. prefetch가 남긴 기억을 다시 쓰면 0.15s
    """
    cache = bool(getattr(_provider, "cache_layout", False))
    past = context if context else load_conversation_history(stepped=cache)
    if history_limit is not None:
        past = past[-history_limit:] if history_limit > 0 else []
    prev_user = next((m["content"] for m in reversed(past) if m.get("role") == "user"), None)
    if prev_user:
        prev_user = _SPEAKER_PREFIX_RE.sub("", prev_user)

    template, topic = prompt_for_model(getattr(_provider, "model", None))
    system_prompt = template.replace("$TOPIC", topic)
    memory_text, lore_text = _retrieve_for_turn(user_input, speaker, prev_user, speaker_id, past,
                                                prefetching, cache)
    tail = []      # cache 배치에서 기록 뒤로 보낼 칸 (턴마다 바뀌는 것)
    if cache:
        # 템플릿 안의 기억·세계관 칸은 머리줄째 들어내 끝으로 보낸다
        for ph, text in (("$MEMORY", memory_text), ("$LORE", lore_text)):
            m = re.search(r"\n*(\[[^\n]*\])\n" + re.escape(ph), system_prompt)
            if m:
                system_prompt = system_prompt[:m.start()] + system_prompt[m.end():]
                tail.append(f"{m.group(1)}\n{text}")
    else:
        system_prompt = system_prompt.replace("$MEMORY", memory_text)
        system_prompt = system_prompt.replace("$LORE", lore_text)

    def add(block: str, volatile: bool):
        nonlocal system_prompt
        if cache and volatile:
            tail.append(block)
        else:
            system_prompt += "\n\n" + block

    # 저장된 기록을 불러와 쓸 때만 — 평가처럼 맥락을 직접 준 경우엔 해당 없음
    if context is None and history_limit != 0:
        note = history_gap_note()
        if note:
            add(note, False)
        else:
            earlier = session_summary()
            if earlier:
                add("[이번 대화 앞부분] 아래 대화 기록보다 앞서 이번 대화에서 오간 이야기다. "
                    "\"아까 그거\"는 여기서도 찾는다.\n" + earlier, False)

    # 발화 판단용 상황 정보 (채널 참여자, 화자, 멀티유저 시 pass 지시 등)
    if situation:
        add(f"[현재 상황]\n{situation}", True)

    # 같이 보고 있는 글. 네 것이 아니라 방금 받아 읽은 것이라고 못박는다 — 안 그러면
    # 기사 내용을 자기 기억이나 자기 의견으로 말한다
    reading = current_reading()
    if reading:
        head = f"{reading['title']} ({reading['url']})" if reading["title"] else reading["url"]
        add(f"[지금 보고 있는 것] 방금 받아 같이 읽고 있는 글이다. "
            f"네 기억이 아니라 눈앞에 있는 글이니, 여기 없는 내용을 지어내지 않고 "
            f"여기 있는 것은 읽었다고 말한다. 요약해 달라지 않으면 통째로 읊지 말고, "
            f"읽은 사람답게 한두 가지를 짚는다.\n{head}\n{reading['text']}", False)

    # 가져오는 중이면 그렇다고 말한다 — 아직 도착하지 않은 것을 "못 봤다"고 하거나
    # 지난 화면을 설명하는 것보다, 기다려 달라고 하는 쪽이 사실이다
    waiting = pending_look()
    if waiting:
        add(f"[지금 보고 있는 것] 지금 가져오는 중이다 — '{waiting['what']}', "
            "아직 도착하지 않았다. 못 봤다고 하거나 볼 수 없다고 하지 말고, "
            "곧 보인다고 한마디 하고 기다린다. 이미 본 다른 화면을 대신 설명하지 않는다.", True)

    # 화자가 만든 사람인지 닉네임으로 판정해 알려줌 (identity.owner_names가 설정된 경우에만)
    if speaker and OWNER_NAMES:
        owner = character.OWNER
        if speaker.strip().lower() in OWNER_NAMES:
            add(f"[화자 정보] 지금 말하는 '{speaker}'은(는) 너를 만들고 있는 {owner}이다. "
                "너와 가장 가까운 사람 — 닉네임이 달라도 같은 사람이다. "
                "'창조자'나 '주인'이라고 부르지 않는다 — 이름으로 부른다.", True)
        else:
            add(f"[화자 정보] 지금 말하는 '{speaker}'은(는) {owner}이(가) 아니라 다른 사람이다. "
                f"친구처럼 대하되 {owner}(으)로 착각하지 마라.", True)

    # 모델은 오늘 날짜를 모른다 — "오늘 며칠이야?"에 "시간을 알 수 없어요"로 답하던 것.
    # 프롬프트 맨 끝에 둔다: 중간에 뒀더니 [기억]에 붙은 지난 날짜("9월 21일(월)에 들음")에 끌려
    # 하루씩 어긋난 날짜와 엉뚱한 요일을 답했다 (끝으로 옮긴 뒤 날짜 퀴즈 4/8 → 12/12)
    now = datetime.now()
    add((f"[현재 시각] {now.year}년 {now.month}월 {now.day}일 ({'월화수목금토일'[now.weekday()]}요일) "
                      f"{'오전' if now.hour < 12 else '오후'} {now.hour % 12 or 12}시 {now.minute}분\n"
                      "날짜나 시각을 물으면 이 줄을 그대로 읽는다 — 더하거나 빼거나 고치지 않는다. "
                      "위 [기억]에 붙은 날짜는 지난 일이지 지금이 아니다.\n"
                      # 시각을 맨 끝에 두니 첫 토큰이 "(current time)"처럼 끌려갔다 — 형식 줄로 닫는다
                      # 맨 끝은 가장 잘 지켜지는 자리라, 여기 적은 길이 규칙이 프롬프트 본문의
                      # 길이 규칙을 이긴다 — 둘이 같은 말을 하게 유지할 것
                      "대답은 (neutral)처럼 감정 태그로 시작하고 [importance: 0~3]으로 끝낸다. "
                      "길이는 할 말이 정한다 — 일상적인 주고받음은 한두 문장이면 되고, "
                      "설명·정리·요약을 부탁받으면 할 말을 다 할 때까지 말한다. 목록·번호는 쓰지 않는다."), True)

    history = [{"role": "system", "content": system_prompt}] + list(past)
    if tail:
        # 기록 뒤, 사용자 줄 바로 앞 — 여기까지가 지난 턴과 같으면 prefill이 캐시에서 끝난다
        history.append({"role": "system", "content": "[이번 턴에 바뀌는 정보 — 위 규칙은 그대로 지킨다]\n\n"
                                                     + "\n\n".join(tail)})
    # 멀티 화자 구분을 위해 발화자 이름을 메시지에 표기
    content = f"[{speaker}] {user_input}" if speaker and prefix_speaker else user_input
    history.append({"role": "user", "content": content})
    return history


# 💬 LLM 호출 (화자별 컨텍스트 지원)
def chat_with_gpt(user_input: str, context: list = None, speaker: str = None) -> str:
    try:
        # 입력 검증 (간소화)
        if not user_input or len(user_input.strip()) < 1:
            return "ERROR_EMPTY_INPUT"

        history = build_messages(user_input, context, speaker)

        assistant_reply = _provider.chat(history)

        if not assistant_reply:
            return "ERROR_EMPTY_CONTENT"

        # 🔐 대화 저장
        save_to_history("user", user_input)
        save_to_history("assistant", assistant_reply)

        return assistant_reply

    except Exception as e:
        print(f"Error: LLM call failed: {e}")
        return "ERROR_API"


# 같은 대답을 되풀이하지 않기 — 8B급은 창 안에 비슷한 대답이 있으면 그걸 토씨까지 베낀다.
# 프롬프트로 막거나(3/6 → 3/6) 반복 페널티를 넓혀도(2/4 → 3/4) 안 줄었고, 다시 뽑으면 고쳐졌다
# (재시도 2/2가 새 대답, 비용 약 1초 — prefill이 캐시에 있어 싸다)
REPEAT_CHECK_CHARS = 14        # 이만큼 나왔을 때 판단한다 (첫 TTS 조각은 8자부터라 그 전에)
REPEAT_SIMILARITY = 0.85
REPEAT_RECENT = 10             # 창 안의 대답 전부와 비교한다 (4개만 봤더니 6번째 대답을 그대로 베꼈다)
REPEAT_RETRY_OPTIONS = {"temperature": 0.95, "repeat_penalty": 1.2, "repeat_last_n": 2048}


def _reply_body(text: str) -> str:
    """감정 태그·중요도를 뺀 알맹이"""
    return re.sub(r"^\s*\(\w+\)\s*|\[importance:[^\]]*\]", "", text).strip()


def _recent_replies(history: list) -> list:
    return [_reply_body(m.get("content", "")) for m in history
            if m.get("role") == "assistant"][-REPEAT_RECENT:]


def _looks_copied(head: str, recent: list) -> bool:
    """이번 대답의 앞부분이 최근 대답의 앞부분과 사실상 같은가"""
    body = _reply_body(head)
    if len(body) < REPEAT_CHECK_CHARS:
        return False
    n = len(body)
    return any(difflib.SequenceMatcher(None, body, p[:n]).ratio() >= REPEAT_SIMILARITY
               for p in recent if len(p) >= n)


async def _stream_reply(history: list):
    """대답 스트림. 첫 조각을 TTS로 내보내기 전에 지난 대답을 베꼈는지 보고, 베꼈으면 한 번만 다시 뽑는다
    (내보낸 뒤에는 되돌릴 수 없으므로 판단은 REPEAT_CHECK_CHARS 글자에서 한 번만 한다)"""
    recent = _recent_replies(history)
    for options in (None, REPEAT_RETRY_OPTIONS):
        try:
            agen = _provider.stream_chat(history) if options is None                 else _provider.stream_chat(history, options=options)
        except TypeError:  # options를 받지 않는 provider
            agen = _provider.stream_chat(history)
        copied, held = False, ""
        checked = not recent or options is not None
        try:
            async for content in agen:
                if not checked:
                    held += content
                    if len(_reply_body(held)) < REPEAT_CHECK_CHARS:
                        continue
                    checked = True
                    if _looks_copied(held, recent):
                        copied = True
                        print(f"[LLM] 지난 대답을 그대로 베낌 — 다시 뽑는다: {_reply_body(held)[:24]}…")
                        break
                    yield held
                    held = ""
                    continue
                yield content
        finally:
            await agen.aclose()
        if not copied:
            if held:
                yield held
            return


# 이 말이 나에게 한 말인가 — 성격 프롬프트를 벗기고 따로 묻는다.
# 스트림 안에서 (pass)를 시키는 방식은 실측 0%였다 (7천 자 성격 규칙이 이긴다).
# 설명만 주면 둘만 있을 때의 하소연·근황까지 N으로 삼켜서, 예시를 붙였다.
JUDGE_SYSTEM = f"""너는 음성 채널의 발화가 AI "{character.NAME}"에게 한 말인지 판정한다.
{character.NAME}에게 한 말이면 Y, 아니면 N. 다른 글자는 쓰지 않는다.

N: 다른 사람에게 한 말, 혼잣말, 전화 통화, 게임 중 중얼거림, 노래, 남들끼리의 잡담.
Y: {character.NAME}를 부르거나 가리킨 말, {character.NAME}에게 하는 질문,
   그리고 둘만 있을 때 {character.NAME}에게 들려주는 이야기(하소연·근황·감상)."""

# 평가에 쓰는 발화와 겹치면 "외워서 맞히는" 점수가 나온다 — 평가 케이스와 다른 문장을 쓴다
JUDGE_SHOTS = [
    ("음 이게 어디 갔더라", "N"),
    ("아 또 튕겼네", "N"),
    ("야 너 지금 어디야 빨리 와", "N"),
    ("나는 새를 노래하고 꽃은 웃는데", "N"),
    ("잠깐 고칠 게 있어서 서버 다시 올린다", "Y"),
    ("오늘 점심 뭐 먹을지 아직도 못 정했어", "Y"),
    ("발표 자료가 생각보다 별로인데 이거 어쩌지", "Y"),
    ("오케이 말이 너무 많아 너", "Y"),
]

# 대화용 온도(0.6)를 그대로 쓰면 같은 말에 답이 갈린다 — 판정은 매번 같아야 한다
JUDGE_OPTIONS = {"temperature": 0.0, "top_p": 1.0, "num_predict": 2}


async def judge_directed_at_me(text: str, prev_lines: list = None, people: int = 1) -> bool:
    """True면 나에게 한 말. 판단이 안 되면 True (답하는 쪽으로 기운다 — 씹히는 게 더 나쁘다)"""
    who = f"{character.NAME}와 한 사람뿐이다" if people <= 1 else f"{character.NAME} 외에 {people}명이 함께 있다"
    body = f"지금 채널에는 {who}."
    if prev_lines:
        body += "\n직전 대화:\n" + "\n".join(prev_lines[-3:])
    body += f"\n판정할 발화: {text}\nY 또는 N?"

    messages = [{"role": "system", "content": JUDGE_SYSTEM}]
    for q, a in JUDGE_SHOTS:
        messages.append({"role": "user", "content": f"판정할 발화: {q}\nY 또는 N?"})
        messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": body})

    raw = ""
    # 슬롯이 여럿인 서버(llama.cpp)에선 대화 슬롯을 피한다 — 판정 한 번이 대화의 KV 캐시를 밀어낸다
    side = {"slot": 1} if getattr(_provider, "slots", 1) > 1 else {}
    try:
        async for delta in _provider.stream_chat(messages, options=JUDGE_OPTIONS, **side):
            raw += delta
            if raw.strip():
                break
    except Exception as e:
        print(f"[LLM] 발화 판정 실패 (답하는 쪽으로): {e}")
        return True
    return raw.strip()[:1].upper() != "N"


async def prefetch_prompt(user_input: str, speaker: str = None, situation: str = None,
                          speaker_id: str = None) -> None:
    """사용자가 아직 말하는 중에 지금까지 들린 말로 프롬프트를 미리 먹여 둔다.
    프롬프트 앞부분(성격·기억·기록)이 최종 프롬프트와 같아서 prefill이 캐시에 남는다.
    대답은 받지 않는다 — 실패해도 조용히 넘어간다"""
    fn = getattr(_provider, "prefetch", None)
    if fn is None or not user_input or not user_input.strip():
        return
    try:
        loop = asyncio.get_event_loop()
        messages = await loop.run_in_executor(None, build_messages, user_input, None, speaker,
                                              situation, True, speaker_id, None, True)
        await fn(messages)
    except Exception as e:
        print(f"Warning: prompt prefetch failed: {e}")


async def chat_with_gpt_stream(user_input: str, context: list = None, speaker: str = None, situation: str = None,
                              save_as: str = None, speaker_id: str = None, history_limit: int = None,
                              stt: dict = None):
    """
    LLM 호출 및 스트리밍 응답 생성 (비동기 제너레이터)
    save_as: 입력이 사람 말이 아닌 지시문일 때(입장 인사) 기록에 대신 남길 사용자 줄.
             지시문을 그대로 남기면 다음 턴부터 모델이 그걸 누군가 한 말로 읽는다
    """
    try:
        if not user_input or len(user_input.strip()) < 1:
            yield "ERROR_EMPTY_INPUT"
            return

        # RAG 검색은 동기 I/O이므로 이벤트 루프를 막지 않게 executor에서 실행
        loop = asyncio.get_event_loop()
        history = await loop.run_in_executor(None, build_messages, user_input, context, speaker, situation,
                                             save_as is None, speaker_id, history_limit)

        full_response = ""
        async for content in _stream_reply(history):
            full_response += content
            yield content

        # 대화 저장 (완료 후) — 멀티 화자 구분을 위해 발화자 이름 포함
        if full_response.strip():
            # 입장 인사 줄(save_as)엔 ID를 달지 않는다 — "그 사람이 마지막으로 말한 때"로 세지 않게
            save_to_history("user", save_as or (f"[{speaker}] {user_input}" if speaker else user_input),
                            speaker_id if save_as is None else None, None if save_as else stt)
            save_to_history("assistant", full_response.strip())

    except Exception as e:
        print(f"Stream Error: {e}")
        yield f"ERROR_STREAM: {e}"


# 모델이 규격(영어 태그) 대신 한국어 감정 단어를 괄호로 낼 때의 매핑 (예: "(중립)")
_STYLE_KO = {
    "중립": "neutral", "평범": "neutral", "담담": "neutral", "담담함": "neutral",
    "졸림": "sleepy", "졸려": "sleepy", "나른": "sleepy", "나른함": "sleepy",
    "호기심": "curious", "궁금": "curious", "궁금함": "curious",
    "감탄": "admiring", "존경": "admiring", "뿌듯": "admiring", "감동": "admiring",
    "기쁨": "happy", "행복": "happy", "기뻐": "happy", "신남": "happy", "즐거움": "happy",
    "슬픔": "sad", "슬퍼": "sad", "우울": "sad", "미안": "sad",
    "화남": "angry", "분노": "angry", "화나": "angry", "짜증": "angry",
}


def extract_emotion_and_clean(text: str):
    """
    응답에 붙은 감정 태그 (예: (happy) / (중립)) 와 중요도 태그 [importance: x] 를 추출하고,
    텍스트는 정제하여 반환. 선두에 남은 괄호 태그는 TTS가 읽지 않도록 제거한다.
    """
    style = "neutral"
    importance = 0  # 기본값 설정

    # 감정 태그 추출 (영어 우선, 없으면 한국어 감정 단어)
    emotion_match = re.search(r"\(\s*(sleepy|neutral|curious|admiring|happy|sad|angry)\s*\)", text, re.IGNORECASE)
    if emotion_match:
        style = emotion_match.group(1).lower()
        text = re.sub(re.escape(emotion_match.group(0)), "", text)
    else:
        ko_match = re.search(r"\(\s*([가-힣]{1,5})\s*\)", text)
        if ko_match and ko_match.group(1) in _STYLE_KO:
            style = _STYLE_KO[ko_match.group(1)]
            text = re.sub(re.escape(ko_match.group(0)), "", text)

    # 🎯 중요도 태그 추출: [importance: x] 형태
    # 모델이 가끔 "0.5"처럼 소수를 쓴다 — 정수만 받던 정규식은 이를 놓쳐 0으로 처리했다
    importance_match = re.search(r"\[importance:\s*(\d+(?:\.\d+)?)\]", text, re.IGNORECASE)
    if importance_match:
        importance = min(3, max(0, round(float(importance_match.group(1)))))
        text = re.sub(re.escape(importance_match.group(0)), "", text)

    # 기타 불필요 태그 제거
    text = re.sub(r"\$[A-Z]+", "", text)      # $TAG 제거
    text = re.sub(r"\[[^\[\]]+\]", "", text)  # 기타 [태그] 제거
    # 선두에 남은 한 단어짜리 괄호 태그 제거 — 규격 밖 단어((웃으며) 등)가 TTS로
    # 읽히는 것 방지. 공백 없는 짧은 토큰만 잘라내 실제 말풍선 괄호는 보존.
    text = re.sub(r"^\s*\(\s*[^()\s]{1,10}\s*\)\s*", "", text)
    text = re.sub(r"\s{2,}", " ", text)       # 연속 공백 제거

    return text.strip(), style, importance
