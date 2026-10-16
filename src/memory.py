"""
🧠 장기 기억 (v2)
- 저장 단위는 턴이 아니라 "에피소드"(한 번의 대화 덩어리). 대화가 멈추거나(유휴), 봇이 나가거나,
  턴이 많이 쌓이면 websocket_handler가 flush_episode()를 부른다
- 에피소드 대화를 로컬 LLM(Ollama, format=json)에 한 번 넘겨 세 종류로 뽑는다
    episode      : 무슨 일이 있었는지 한두 문장
    person_facts : 누구(who)에 대한 사실 — 날짜는 절대 날짜로, 앞으로의 일정엔 due(예정일)
    self_said    : 캐릭터가 자기에 대해 한 말 — 다음에 말을 바꾸지 않게
    self_experience : 캐릭터 자신에게 있었던 일
- 사람은 디스코드 사용자 ID로 묶는다 (닉네임은 서버마다 다르고 바뀐다). 이름은 data/people.json의 별칭
- 새 사실은 그 사람의 기존 사실과 함께 모델에 보여 추가/교체/무시를 고르게 한다 (Mem0 방식) —
  임베딩 거리로는 "골드 2 → 플래티넘"(137), "짜장면 → 짬뽕"(67) 같은 갱신·모순을 잡을 수 없었다
- 불러올 때: 말하는 사람의 사실과 캐릭터 자기 설정은 검색 없이 늘 넣고(핵심 기억), 있었던 일은
  관련도 + "어제/지난주" 같은 시간 + 가장 최근 대화로 고른다
- 저장소는 ChromaDB 컬렉션 memory (metadata: type, who, name, ts, due, kind, about)
- 긴 대화에서 20줄 창 밖으로 밀려난 이번 세션 앞부분은 session summary로 따로 요약해 둔다
- 요약기는 기본으로 지금 대화에 쓰는 모델을 따라간다 (memory.summarizer_model로 따로 지정 가능)
"""

import json
import os
import re
import threading
from collections import deque
from datetime import date, datetime, timedelta

import httpx

import character
from config_loader import load_config
from vector_db import VectorDB

NAME = character.NAME

config = load_config()
_mem_cfg = config.get("memory", {})

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "conversation_history.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "memory_state.json")
SESSION_SUMMARY_PATH = os.path.join(DATA_DIR, "session_summary.json")
PEOPLE_PATH = os.path.join(DATA_DIR, "people.json")
# 사람이 읽는 백업 (벡터 DB가 깨져도 남도록)
LONG_TERM_FILE = os.path.join(DATA_DIR, "long_term_memory.txt")

# 관련도 게이트 — lore.py와 같은 방식 (ko-sroberta + ChromaDB L2). 실측:
# 관련 기억 톱매칭 ~60~100 ("짜장면 짬뽕" 60, "좋아하는 음식" 96),
# 무관한데 끌려온 기억 ~130~166 (비슷한 감정의 다른 사건 164, 다른 게임 158).
# 무관한 기억을 매번 3개씩 넣자 모델이 그걸 억지로 대화에 끌어왔다
MEMORY_MAX_DISTANCE = 120.0
# 교체 판단이 실패했을 때만 쓰는 예비 기준 — 이보다 가까우면 같은 사실로 보고 바꾼다
FACT_DEDUP_DISTANCE = float(_mem_cfg.get("fact_dedup_distance", 60.0))
# 핵심 기억으로 늘 넣는 개수 (그보다 많으면 관련 있는 것 + 최근 것)
PROFILE_MAX = 8
# 관련성이 자리를 다 가져가도 최근 것 몫으로 남겨 두는 바닥.
# 이게 없으면 주제가 분명한 턴마다 "뭐가 새로운지"를 통째로 잃는다
PROFILE_MIN_RECENT = int(_mem_cfg.get("profile_min_recent", 3))
# 그때의 컨디션·기분을 며칠까지 기억에 둘지 (config: memory.state_ttl_days)
STATE_TTL_DAYS = 2
STATE_TTL_DAYS = int(_mem_cfg.get("state_ttl_days", STATE_TTL_DAYS))
SELF_MAX = 6
# 자기 취향도 관련성 우선으로 뽑되, 최근 것 몫은 남긴다 (core()와 같은 이유)
SELF_MIN_RECENT = int(_mem_cfg.get("self_min_recent", 3))
# 기억 후보를 한 번에 몇 개씩 분류받을지 (_screen_facts). 전부 한 번에 물으면 답이
# summarizer_max_tokens를 넘겨 JSON이 깨지고 한 회차가 통째로 비었다 (실측: 후보 41개)
SCREEN_BATCH = int(_mem_cfg.get("screen_batch", 15))
# 후보 분류를 켜 둘지 (memory.screen). 쓸 모델은 memory.screen_model → summarizer_model 순으로 찾는다.
# 7B급 로컬 모델로는 쓸 만하지 않았다 — 진짜 사실 5개 중 2개를 버리고 버릴 것 6개 중 4개를 놓쳤다.
# claude-haiku-4-5로는 3회 모두 놓침 1/41 · 과잉 1~2/20. 쓸 모델이 마땅치 않으면 끈다 (screen: false)
SCREEN_ON = bool(_mem_cfg.get("screen", True))
# Whisper 확신도(avg_logprob)가 이보다 낮은 사람 줄은 기억 추출에 넣지 않는다. 대화 기록에는 남는다.
# 실측: 소음이 핫워드로 바뀐 "<캐릭터> <사람 이름>" 15건이 -0.65~-0.96, 제대로 들은 말이 -0.22 안팎.
# 잘못 들은 일반 문장이 어디쯤인지는 아직 모른다 — metrics.jsonl의 turn.stt_lp가 쌓이면 다시 잡는다.
# 확신도가 없는 줄(이 값이 생기기 전 기록, 타이핑한 말)은 거르지 않는다
MIN_STT_LP = float(_mem_cfg.get("min_stt_logprob", -0.6))
# 에피소드 하나에서 한 사람에 대해 저장할 사실의 상한. 판정할 모델이 없을 때(로컬만 쓸 때)는
# 덜 기억하는 쪽이 낫다 — 봇 기능을 시험하던 한 세션은 사람 사실 30개를 저장했고 그중 진짜 삶의 사실은 0개였다
MAX_PERSON_FACTS = int(_mem_cfg.get("max_person_facts", 4))
METRICS_PATH = os.path.join(DATA_DIR, "metrics.jsonl")


def _log_event(record: dict):
    """기억 쪽 사건을 metrics.jsonl에 남긴다 — 콘솔에만 찍으면 판정이 꺼져 있어도 아무도 모른다"""
    try:
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({**record, "ts": datetime.now().isoformat(timespec="seconds")},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def _misheard(m: dict) -> bool:
    lp = m.get("stt_lp")
    return m.get("role") == "user" and lp is not None and lp < MIN_STT_LP

# ── 주제 (about) ────────────────────────────────────────────────────────────────
# 분류가 "누구의 기억인가"(type: episode/person/self)로만 되어 있어서, 작품에 대한 사실이
# 둘 곳이 없어 사람에게 붙었다 — 이야기 속 인물의 설정이 그 이야기를 꺼낸 사람에 **대한** 사실로
# 저장됐다. 그러면 그 이야기 20행이 그 사람의 다른 사실 90여 개와 같은 8칸
# (PROFILE_MAX = 관련 5 + 최근 3)을 다투고, 최근 3칸이 무관한 것을 끌고 온다. 게임 규칙
# 13개를 저장해 놓고 2개만 올라왔던 것이 같은 원인이다.
#
# 특정 용도의 전용 칸을 두지 않고, 모든 기억에 "무엇에 관한 것인가"를 붙이는 일반 축으로
# 만든다 — 주제는 config(memory.topics)에 적힌 만큼 생기고 코드는 이름을 모른다.
#
# 모델을 부르지 않는다. 저장된 198행에 대고 재 보니 낱말로 충분했다 (14/14 · **과잉 0**).
# 이유는 `_BOT_WORK_RE`가 실패한 조건과 정반대라서다: "봇 작업"은 공유하는 낱말이 없지만
# **작품·게임에는 고유명사가 있다**.
# 단, '소설'·'게임'처럼 일반적인 낱말을 별칭에 넣으면 주제가 둘이 되는 날 섞인다 — 그때는
# config에서 빼야 한다. 그래서 코드가 아니라 config에 둔다.
def _fix_came_from_transcript(before: str, after: str, transcript: str) -> bool:
    """이름 정정을 받아들일지 — **지워지는 쪽이 전사에 실제로 있을 때만** 받는다.

    고치는 근거는 "음성 인식이 틀린 형태가 추출에 박혔다"는 것이므로, 틀린 형태는 당연히 전사에
    있어야 한다. 반대로 전사에 없는 것을 고치려 든다면 추출이 **이미 제대로 고쳐 쓴 것**을
    전사의 오류 쪽으로 되돌리는 것이다.

    예: 전사에 잘못 들린 "카단"이 있으므로 → "카단"을 실제 이름 "카탄"으로 고치는 것은 받는다.
    전사에는 "크리스토프"만 있고 문장은 "크리스토퍼 놀란"이라 맞게 적혀 있었는데, 모델이 전사에
    맞춰 "크리스토프"로 되돌린 일이 있었다 — 프롬프트에 X 예시를 넣어도 계속 그랬고, 이 규칙이 막는다.
    """
    import difflib
    for tag, a1, a2, _, _ in difflib.SequenceMatcher(None, before, after).get_opcodes():
        if tag == "equal":
            continue
        gone = before[a1:a2].strip()
        if not gone:
            continue               # 끼워 넣기만 한 것 — 지워지는 게 없다
        if len(gone) < 2:
            # 한 글자 차이는 그대로 찾으면 아무 데나 걸린다 ("퍼"). 그 글자가 속한 낱말로 넓혀
            # 통째로 전사에 있는지 본다 — "크리스토퍼"는 없고 "크리스토프"만 있는 경우를 잡는다
            lo, hi = a1, a2
            while lo > 0 and not before[lo - 1].isspace():
                lo -= 1
            while hi < len(before) and not before[hi].isspace():
                hi += 1
            gone = before[lo:hi].strip(" .,·…\"'()[]「」『』")
        if len(gone) >= 2 and gone not in transcript:
            return False
    return True


def _topic_patterns() -> list:
    """[(주제명, 정규식)] — config의 memory.topics에 manuscript의 제목·별칭을 합쳐 만든다"""
    topics = dict(_mem_cfg.get("topics") or {})
    ms = config.get("manuscript", {})
    title = (ms.get("title") or "").strip()
    if title:
        words = set(topics.get(title) or []) | {a for a in (ms.get("aliases") or []) if a}
        topics[title] = sorted(words)
    out = []
    for name, words in topics.items():
        alts = [name, *(words or [])]
        # 붙여 적어 둔 낱말이 띄어 쓴 꼴로 들어와도 잡히게 글자 사이 공백을 허용한다
        # (실측: 이 한 가지로 놓침 1 → 0)
        pats = [r"\s*".join(re.escape(ch) for ch in w) if len(w) <= 8 else re.escape(w)
                for w in alts if w and len(w) >= 2]
        if pats:
            out.append((name, re.compile("|".join(pats))))
    return out


_TOPICS = _topic_patterns()


def topic_of(text: str) -> str:
    """이 문장이 어느 주제에 관한 것인가 — 없으면 빈 문자열. 먼저 걸린 주제가 이긴다"""
    for name, rx in _TOPICS:
        if rx.search(text or ""):
            return name
    return ""


def with_topic(meta: dict, text: str) -> dict:
    """metadata에 about을 얹는다. 주제가 없으면 키를 넣지 않는다 — 빈 값으로 채우면
    Chroma의 where 필터에서 '없음'과 구별되지 않는다"""
    t = topic_of(text)
    return {**meta, "about": t} if t else meta
TOPIC_MAX = int(_mem_cfg.get("topic_max", 6))   # 이 턴의 주제에 주는 칸 (about이 붙은 기억)
SELF_EXP_MAX = 4   # 캐릭터가 겪은 일 — 취향과 달리 계속 쌓이므로 최근·관련 것 몇 개만
# 교체 판단에 보여 줄 기존 사실 수
RECONCILE_MAX = 12

# 한 번에 요약기에 넘기는 최대 줄 수 (밀린 기록을 처음 처리할 때 등)
EPISODE_CHUNK_LINES = 40
# 이번 세션이 이만큼 끊기면 새 세션 (llm.HISTORY_GAP_NOTE_MIN과 같은 값)
SESSION_GAP_SEC = 30 * 60
# llm.HISTORY_LIMIT과 같은 값 — 이 창 밖으로 밀려난 줄만 세션 요약 대상
HISTORY_WINDOW = 20
SESSION_SUMMARY_STEP = 10

SELF_KEY = "self"   # 캐릭터 자신의 기억을 묶는 who 값
WEEKDAYS = "월화수목금토일"

EXTRACT_PROMPT = """너는 대화 기록을 정리하는 서기다. 아래는 디스코드 음성 채널에서 AI 캐릭터 "$NAME"와 사람들이 나눈 대화다.
(음성 인식을 거친 문장이라 틀린 글자가 있을 수 있다. 뜻이 분명하지 않은 말은 사실로 적지 마라.)

JSON 하나로만 답한다. 키는 episode, person_facts, self_said, self_experience.
- episode: 무슨 이야기를 했는지 한두 문장. "사용자" 대신 대화에 나온 이름을 쓴다.
- person_facts: 사람에 대한 사실. 나중에 다시 만났을 때 쓸모 있는 것만 — 하는 일, 취미, 하는 게임과 실력, 좋아하고 싫어하는 것,
  계획과 약속, 일정, 가족·반려동물, 최근 있었던 큰 일. 여러 사람이 말했으면 사람마다 빠짐없이, 한 사실씩 따로 적는다.
  "인사했다", "질문했다", "테스트했다" 같은 대화 행위는 사실이 아니다. 없으면 빈 목록.
  who는 대화 속 이름 그대로. $NAME 자신은 넣지 않는다. 말을 그대로 옮기지 말고 제3자가 적는 문장으로 쓴다.
  이 대화·음성 채널·봇의 기능·음성 인식·들어오고 나가는 것에 대한 말은 사실이 아니다.
  $NAME를 만들고 고치는 이야기(서버, 테스트, 모델, 프롬프트, 레이턴시, 감정 태그)도 사실이 아니다 — 빼라.
  그 사람의 일이나 생활 이야기는 남긴다.
  날짜: "다음 주 화요일", "내일" 같은 말은 들은 그대로 쓴다 — 날짜로 바꾸는 건 코드가 한다.
  대화에 없는 날짜를 지어내지 않는다 (보기로 준 날짜 "9월 15일(화)"를 그대로 옮겨 적은 적이 있다).
  앞으로 있을 일정이면 "due"에 그 날짜를 "YYYY-MM-DD"로 적는다. 날짜를 모르면 due를 넣지 않는다.
- self_said: $NAME가 자기 취향·의견·약속·설정을 밝힌 것만, 제3자가 적은 짧은 요약으로. $NAME의 말을 그대로 옮기지 않는다.
  격려, 조언, 축하, 사과, 다짐, 대화 진행에 대한 말은 넣지 않는다. "그건 확인할 수 없다", "그런 기능이 없다"처럼
  자기 능력에 대한 말도 넣지 않는다. 많아야 두 개, 없으면 빈 목록.
- self_experience: 이 대화에서 **$NAME 자신에게** 있었던 일. 나중에 $NAME가 "그때 그랬지"라고 떠올릴 만한 것이다.
  이런 것을 적는다: 누구에게 무슨 말을 들었고 무엇이 인상에 남았는지, 누구와 무엇을 두고 부딪혔는지,
  무엇이 처음이었는지, 무엇이 잘 안 돼서 다시 했는지, 무엇을 놀리거나 칭찬받았는지.
  $NAME를 주어로, 상대의 이름을 넣어 쓴다. 많아야 두 개, 없으면 빈 목록 — 억지로 채우지 않는다.
  도우려 한 것은 사건이 아니다. 이렇게 쓰지 마라:
    X "루카의 문제를 해결하기 위해 여러 방법을 제안하며 도움을 주고자 했다." (조력 행위 — 무슨 일이 있었는지가 없다)
    X "질문에 답했다.", "대화를 나눴다." (알 수 있는 게 없다)
    O "루카가 목소리가 끊긴다고 해서 같은 말을 세 번 다시 했다."
    O "미르에게 반년 준비한 공모전에서 떨어졌다는 말을 들었다."
- 모든 문장은 "~한다", "~했다"로 끝나는 짧은 평서문. "아까", "방금", "오늘" 같은 말은 쓰지 않는다.

예시 (대화 날짜 9월 3일(수)):
{"episode": "루카가 면접 준비 이야기를 했고 미르는 새로 산 키보드를 자랑했다. $NAME는 둘 다에게 한마디씩 보탰다.",
 "person_facts": [{"who": "루카", "fact": "9월 12일(금)에 게임 회사 면접이 있다.", "due": "2026-09-12"},
                  {"who": "루카", "fact": "매운 음식을 못 먹는다."},
                  {"who": "미르", "fact": "기계식 키보드를 새로 샀다."}],
 "self_said": ["짜장면보다 짬뽕을 좋아한다고 했다.", "비 오는 날을 좋아한다고 했다."],
 "self_experience": ["루카의 면접 이야기를 듣고 잘되기를 바란다고 말했다.",
                     "미르에게 키보드 소리가 시끄럽겠다고 놀렸다."]}""".replace("$NAME", NAME)

RECONCILE_PROMPT = """너는 기억 정리 담당이다. {who}에 대해 이미 기억하는 사실과, 이번 대화에서 새로 알게 된 사실이 있다.
새 사실마다 하나를 고른다.
- add: 기존 기억에 없는 새로운 내용
- replace: 기존 기억의 같은 대상이 바뀌었거나 결과가 나왔다. old에 이제 틀린 기존 번호를 적는다.
  예: "골드 2이다" → "플래티넘을 달성했다", "면접이 있다" → "면접에 붙었다", "짜장면이 좋다" → "짬뽕이 좋다"
- skip: 기존 기억과 같은 내용 (표현만 다름)
JSON으로만 답한다: {{"decisions": [{{"new": 1, "op": "add"}}, {{"new": 2, "op": "replace", "old": [3]}}, {{"new": 3, "op": "skip"}}]}}"""

# 버릴지를 묻지 않고 무엇에 대한 문장인지만 묻는다 — 버리는 건 코드가 한다.
# "넣을지 버릴지 고르라"로 물었을 때(같은 모델·같은 사례) 진짜 사실을 20개 중 6~9개 버렸고,
# 조항을 빼도 줄지 않았다. 가치 판단을 시키면 "사소하다"며 평범한 생활 사실을 깎는다.
# 주제만 묻자 같은 자료에서 과잉 3/20, 놓침 1/41
SCREEN_PROMPT = """너는 기억 창고의 분류원이다. 아래 대화에서 뽑아 놓은 문장들을 하나씩 보고, 그 문장이
무엇에 대한 것인지만 고른다. 좋은 기억인지 쓸모 있는지는 판단하지 않는다 — 주제만 본다.

- life: 사람에 대한 것. 그 사람이 하는 일, 공부, 취미, 하는 게임, 좋아하고 싫어하는 것, 계획과
  약속, 일정, 가족과 반려동물, 있었던 일, 세상일에 대한 그 사람의 의견·감상. 사소해 보여도 life다.
- bot: "$NAME"라는 프로그램에 대한 것. $NAME가 무엇을 보고 듣고 읽는지, 잘 보이는지 빠른지
  틀렸는지, 말투·목소리·기억·대답이 어떤지, 그것을 만들고 고치고 시험하는 일.
- meta: 이 대화 자체에 대한 것. 누가 언제 무엇을 물어봤는지, 들어오고 나갔는지, 어느 날 대화했는지,
  서버를 올리고 내렸는지.
- nothere: 대화에 그런 이야기가 아예 없다. 다른 화제의 숫자나 낱말로 만들어진 딴 문장.
  이름이 잘못 적혔거나(음성 인식이 자주 틀린다) 날짜 같은 군더더기가 어긋난 것은 여기가 아니다 —
  핵심 이야기가 대화에 있으면 life다.

문장의 고유명사가 틀렸으면 "fix"에 고친 문장을 적는다. 목표는 **실제 이름으로 맞추는 것**이다.
음성 인식이 한 번 틀린 형태가 그대로 굳는 일이 있다 — "카탄"이라고 세 번 말했는데 한 번
잘못 들린 "카단"이 문장에 박힌 식이다.
근거는 둘이다: (1) 대화에서 더 자주 쓰인 표기, (2) 네가 아는 실제 이름.
**둘이 어긋나면 실제 이름을 따른다** — 대화도 음성 인식을 거쳤으므로 대화 쪽이 틀릴 수 있다.
  O "카단이라는 보드게임을 한다" → "카탄이라는 보드게임을 한다"  (대화에 카탄이 더 많고 실제 이름)
  X "크리스토퍼 놀란" → "크리스토프 놀란"  (대화가 잘못 들린 것이다. 문장이 이미 맞다)
확신이 없으면 고치지 않는다. 이름 표기만 고치고 뜻·길이·다른 낱말은 그대로 둔다.
고칠 것이 없으면 "fix"를 넣지 않는다.

JSON 하나로만 답한다. 모든 번호에 하나씩:
{"labels": [{"i": 0, "t": "life"}, {"i": 1, "t": "bot"},
            {"i": 2, "t": "life", "fix": "카탄이라는 보드게임을 친구들과 하려고 한다"}]}""".replace("$NAME", NAME)

SESSION_PROMPT = f"""너는 대화 기록을 정리하는 서기다. 디스코드 음성 채널에서 AI 캐릭터 "{NAME}"와 사람들이 나눈 대화의 앞부분을
두세 문장으로 요약한다. 누가 무슨 화제를 꺼냈고 무엇을 말했는지만, 나중에 "아까 그거"라고 할 때 찾을 수 있게 구체적인 이름과 대상을 남긴다.
누가 이겼는지, 다퉜는지, 속였는지 같은 평가나 해석은 쓰지 않는다. 제목, 머리말, 마크다운 없이 평범한 문장만 쓴다.
기존 요약이 주어지면 그 내용을 유지하면서 새 대화를 덧붙여 다시 쓴다. 요약문만 답한다."""


def _parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _ago(timestamp: str) -> str:
    """ISO 시각 → '오늘' / '어제' / 'N일 전' / 'N개월 전'"""
    t = _parse_ts(timestamp)
    if t is None:
        return "예전"
    days = (date.today() - t.date()).days
    if days <= 0:
        return "오늘"
    if days == 1:
        return "어제"
    if days < 30:
        return f"{days}일 전"
    return f"{days // 30}개월 전" if days < 365 else f"{days // 365}년 전"


def _md(d) -> str:
    return f"{d.month}월 {d.day}일({WEEKDAYS[d.weekday()]})"


def _calendar(day: date) -> str:
    """요약기가 "다음 주 화요일"을 날짜로 바꿀 수 있게 주 단위 달력 — 작은 모델에게 요일 계산을 시키면 틀린다"""
    monday = day - timedelta(days=day.weekday())
    lines = [f"대화한 날: {_md(day)}"]
    for label, k in (("지난주", -1), ("이번 주", 0), ("다음 주", 1), ("다다음 주", 2)):
        start = monday + timedelta(weeks=k)
        lines.append(f"{label}: " + " ".join(_md(start + timedelta(days=i)) for i in range(7)))
    return "\n".join(lines)


_TIME_WORDS_RE = re.compile(r"(아까|방금|조금 전에?)\s*")
_SPEAKER_RE = re.compile(r"^\[([^\]]+)\]\s*")
_TAG_RE = re.compile(r"^\s*\(\w+\)\s*|\s*\[importance:[^\]]*\]\s*$")
_DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 캐릭터의 대화 행위 — 자기 설정이 아니다 ("격려했다"가 기억으로 쌓이던 것)
_SELF_JUNK_RE = re.compile(r"(격려|조언|권유|권했|위로|공감|축하|인사|질문|반응|대답|사과|칭찬|응원|걱정|당부)")
# 캐릭터가 남에게 한 충고 ("준비가 필요하다고 언급했다") — 이것도 자기 설정이 아니다
_SELF_ADVICE_RE = re.compile(r"(필요하다고|중요하다고|해야 한다고|하라고|하지 말라고)")
# "나는 루카야" 같은 자기소개 — 이름은 이미 알고 있다
_INTRO_RE = re.compile(r"^(나는|저는|내 이름은)\s*\S{1,8}\.?$")
# 사람 사실로 저장하면 안 되는 것 (실사용에서 실제로 저장됐던 것들)
# - 말을 1인칭 그대로 옮긴 것: "나는 그 소설 속 캐릭터이다", "내가 쓴 소설의 배경이 …"
# - 이 대화·채널·봇 자체에 대한 말: "채널에서 나가겠다", "다음번에 이 기능을 추가할 것이다"
_FIRST_PERSON_RE = re.compile(r"^(나는|난|내가|나도|저는|제가|저도|내|제)\s")
# 캐릭터(봇)를 만들고 고치는 이야기 — 사람에 대한 사실이 아니다.
# 만든 사람이 개발자면 대화의 상당 부분이 이것인데, 그대로 쌓이면 인사할 때 꺼낼 게 이것뿐이 된다
# 낱말 목록은 그것을 쓴 날의 어휘만 잡는다 — 한 세션에 맞춰 쓴 목록이 다른 화제(화면·URL)의 세션에서
# 저장된 사람 사실 30개 중 0개를 걸렀다. 그래서 낱말은 "값이 싸고 모델 없이도 도는" 1층일
# 뿐이고, 일반화는 _screen_facts()의 판정이 맡는다. 아래 낱말은 저장된 232개에 대고 재서 고른 것:
# "테스트"는 8개를 잡고 그 8개가 모두 봇 시험 이야기였고, "성능"·"확인"은 진짜 사실
# ("그래픽 카드 성능이 부족하다")까지 깎아 넣지 않았다
_BOT_WORK_RE = re.compile(r"(API ?서버|서버를 (다시|재|내리|끄|올리|띄)|서버가 (나갔|내려)|재시작|테스트|"
                          r"발화자 구분|감정 태그|파인튜닝|레이턴시|프롬프트|모델을 (바꾸|교체|쓰)|평가 구문|"
                          r"학습 데이터|코드를|디버그|로그를 (확인|보)|버전|배포|빌드|"
                          r"대화 ?탭|화면|URL|버그)")
# 띄어쓰기는 모델이 쓰는 대로 온다 — "음성 인식"만 적어 두었더니 "음성인식이랑 화면인식이 잘
# 안 된다"가 그대로 사람 사실로 저장됐다
_SESSION_META_RE = re.compile(r"(채널|로그|기능을|기능이|마이크|STT|음성\s*인식|화면\s*인식|발음|끊기|끊겨|접속|"
                              r"나가겠|나간다|들어왔|대화를 (끝|마무리|이어)|말을 걸|잘게|자러|확인하는|"
                              r"과정을 거쳤|물어보|질문했|대화 상대)")
# 캐릭터 자기 설정은 취향·의견·바람이 드러난 말만 남긴다 — 막는 목록만으로는 "쉬는 것이 우선이라고 말했다",
# "휴식이라는 단어를 먼저 썼다고 주장했다" 같은 대화 중의 말이 계속 새어 들어왔다
_SELF_TASTE_RE = re.compile(r"(좋아|좋다|좋은|싫어|싫다|싫은|선호|취향|꿈|하고 싶|해 보고 싶|원한다|바란다|편하다|"
                            r"나쁘지 않|마음에 들|즐겨|제일|가장 좋|별로)")
# 취향 문장 꼴은 갖췄지만 취향이 아닌 말. _SELF_TASTE_RE가 낱말만 보기 때문에 새어 들어온다
# (실측: 저장된 7개 중 5개가 이런 꼴이었다)
_SELF_NOT_TASTE_RE = re.compile(
    # ① "취향이 없다"는 취향이 아니다 — "색깔에 대한 개인적 취향은 중요하지 않다고 했다"
    r"(취향|선호|기호)[^.]{0,12}(중요하지 않|상관없|의미 없|없다|없어|딱히)"
    # ② 자기 대화 방식·표현에 대한 메타 — "대화를 풍성하게 만드는 게 좋다", "세련된 단어를 쓰도록 노력하겠다"
    r"|(대화|말투|표현|단어|어투|문장)[^.]{0,16}(풍성|자연스럽|거칠|세련|노력하겠|쓰도록|고치|줄이겠|다듬)")
# 캐릭터가 "나는 그걸 확인할 수 없다"고 한 말 — 자기 설정으로 저장되면 [기억]에서 "말을 바꾸지 말 것"과 함께
# 매번 들어가 부정을 굳혔다 ("서버의 실시간 로그를 조회할 권한이 없다고 했다")
_SELF_CAPABILITY_RE = re.compile(r"(권한|기능|로그|시스템|실시간|확인할 수|알 수 없|알 수가 없|기억력|서버|채널|마이크|"
                                 r"기술적|접속|인식)")


# 겪은 일에서 걸러내는 것 — 사건이 아니라 "도우려 했다"는 조력 행위만 적은 문장.
# 작은 모델은 프롬프트로 막아도 자꾸 이 말투로 되돌아간다 (실측: 첫 시험에서 뽑힌 한 줄이 이것뿐이었다)
_EXP_HELPER_RE = re.compile("(도움을 주|도움이 되|도우려|해결하기 위해|해결하려|해결해 주|제안하며|제안하면서|"
                            "조언|격려|위로|응원|안내했|설명해 주려|지원했)")
# 겪은 일로 남길 게 없는 문장 — 무슨 일이 있었는지 알 수 없는 대화 행위만 적은 것
_EXP_EMPTY_RE = re.compile("^" + re.escape(NAME) + "(는|가|이|은|에게)?\\s*(대화를|이야기를|질문에|말을|답변을|대답을)?\\s*"
                           "(나눴다|나누었다|했다|답했다|대답했다|응답했다|참여했다)[.]?$")


# 그때의 컨디션·기분 — 며칠 지나면 꺼낼 이유가 없다.
# "하와이안 피자를 좋아한다"(취향)와 "술 마시고 와서 몸이 힘들다"(상태)를 가르는 선이다
_STATE_RE = re.compile(r"(피곤|힘들|지쳤|졸리|아프|몸살|숙취|술을 마시|취했|감기|"
                       r"화가 나|화를 내|짜증|우울|슬프|기분이|컨디션|바쁘|정신없|"
                       r"상태였|상태다|중이었|포기하려|불안|초조)")


def _looks_like_state(fact: str) -> bool:
    """오래 남을 사실이 아니라 그때의 상태인가"""
    return bool(_STATE_RE.search(fact or ""))


def _unfence(raw: str) -> str:
    """```json … ``` 펜스를 벗긴다 — format=json을 줘도 펜스를 붙여 보내는 모델이 있다
    (그런 모델로 바꾸자 에피소드 저장이 소리 없이 통째로 실패하고 있었다)"""
    t = (raw or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
    if not t.startswith("{"):                      # 앞뒤에 말이 붙은 경우 첫 { … 마지막 }만
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            t = t[a:b + 1]
    return t


def _read_history() -> list:
    lines = []
    if not os.path.exists(HISTORY_PATH):
        return lines
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return lines


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _transcript(lines: list) -> str:
    """기록 줄 → 요약기에 넘길 대본 ("<사람>: …" / "<캐릭터>: …")"""
    out = []
    for m in lines:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if m.get("role") == "assistant":
            out.append(f"{NAME}: {_TAG_RE.sub('', text).strip()}")
        else:
            sp = _SPEAKER_RE.match(text)
            out.append(f"{sp.group(1)}: {text[sp.end():]}" if sp else text)
    return "\n".join(out)


_WEEK_RE = re.compile(r"(이번|다음|담|다다음)\s?주\s?([월화수목금토일])요일")
_BARE_WEEKDAY_RE = re.compile(r"(?<![가-힣])([월화수목금토일])요일")
_REL_DAY = {"그저께": -2, "그제": -2, "어제": -1, "오늘": 0, "내일": 1, "모레": 2, "글피": 3}
_REL_DAY_RE = re.compile(r"(그저께|그제|어제|오늘|내일|모레|글피)")
_AFTER_RE = re.compile(r"(\d+)\s?일\s?(뒤|후)")
_MD_RE = re.compile(r"(\d{1,2})월\s?(\d{1,2})일")
# 이미 끝난 일을 적은 문장 — 날짜가 앞을 가리켜도 챙길 일정이 아니다
_DONE_RE = re.compile(r"(봤다|보았다|했다|했음|했었|다녀왔|갔다|왔다|끝났|마쳤|받았|먹었|만났|"
                      r"붙었|떨어졌|나왔다|샀다|팔았|이었다|였다)")
# 앞일을 과거형으로 말한 것은 일정이다 — "10월 10일에 **예정된** TRPG … 어렵다고 판단했다"
_PLAN_RE = re.compile(r"(예정|계획|약속|예약|앞두|할 것|하기로|열린다|나온다|있을|잡혀|기다리)")


def _resolve_dates(text: str, day: date):
    """
    "다음 주 화요일", "내일", "3일 뒤" → "9월 15일(화)" (말한 날 기준). 가장 먼 미래 날짜를 due로 돌려준다.
    작은 모델은 달력을 줘도 날짜를 바꾸지 않거나 연도를 틀려서(2024) 코드로 바꾼다
    """
    found = []

    def put(d):
        found.append(d)
        return _md(d)

    monday = day - timedelta(days=day.weekday())
    offset = {"이번": 0, "다음": 1, "담": 1, "다다음": 2}
    text = _WEEK_RE.sub(lambda m: put(monday + timedelta(weeks=offset[m.group(1)], days=WEEKDAYS.index(m.group(2)))), text)
    text = _AFTER_RE.sub(lambda m: put(day + timedelta(days=int(m.group(1)))), text)
    text = _REL_DAY_RE.sub(lambda m: put(day + timedelta(days=_REL_DAY[m.group(1)])), text)

    def bare(m):  # 그냥 "화요일에" — 말한 날 이후 가장 가까운 그 요일
        # 이미 "9월 15일(화)"로 바뀐 괄호 속 요일은 건드리지 않는다
        if m.start() > 0 and text[m.start() - 1] == "(":
            return m.group(0)
        ahead = (WEEKDAYS.index(m.group(1)) - day.weekday()) % 7 or 7
        return put(day + timedelta(days=ahead))
    text = _BARE_WEEKDAY_RE.sub(bare, text)
    for m in _MD_RE.finditer(text):
        try:
            d = date(day.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if (day - d).days > 180:  # 12월에 말한 "1월 5일"은 내년
            d = date(day.year + 1, d.month, d.day)
        found.append(d)
    # 말한 날 **다음**부터가 앞일이다. `>= day`로 두었더니 그 날 있었던 일이 전부 일정이 됐다 —
    # "9월 28일 아침에 영화를 봤다"가 due 09-28을 달고, 다음 날부터
    # [기억] 맨 위 "먼저 챙길 것"에 올라가 "그게 어떻게 됐는지 먼저 물어본다"가 됐다
    future = [d for d in found if d > day]
    # 날짜가 앞이어도 이미 끝난 일을 적은 문장은 일정이 아니다 ("다음 날이 9월 27일인지 확인했다").
    # 다만 앞일을 과거형으로 말한 것은 일정이다 — "10월 10일에 예정된 … 어렵다고 판단했다"
    if future and _DONE_RE.search(text) and not _PLAN_RE.search(text):
        return text, ""
    return text, (max(future).isoformat() if future else "")


_JOSA_RE = re.compile(r"(으로|에서|이다|이|가|은|는|을|를|에|의|도|과|와|로)$")
_TOKEN_STOP = {"다음", "이번", "다다음", "지난", "있다", "잡혀", "잡혀있다", "예약", "하는", "했다", "한다", "예정",
               "오늘", "내일", "모레", "요즘", "현재", "지금",
               # 아무 문장에나 들어가는 말 — 이것만 겹쳐도 같은 일로 보고 지난 일정을 덮어썼다
               # ("몸이 많이 힘들다" ← "조회수가 많이 떨어졌다", 공통어 "많이")
               "많이", "조금", "정말", "너무", "그냥", "다시", "아주", "약간", "계속", "어제", "그때", "때문",
               "생각", "같다", "같은", "느낀다", "느꼈다", "말했다", "이야기", "얘기", "대해", "대한", "부분"}


def _tokens(text: str) -> set:
    """"코딩 테스트가 있다" → {"코딩", "테스트"} — 날짜·조사·흔한 말을 뺀 핵심 낱말"""
    out = set()
    for w in re.findall(r"[가-힣A-Za-z]+", _MD_RE.sub(" ", text)):
        stem = _JOSA_RE.sub("", w)
        w = stem if len(stem) >= 2 else w  # "치과"의 "과"는 조사가 아니다
        if len(w) >= 2 and w not in _TOKEN_STOP and not w.endswith("요일"):
            out.add(w)
    return out


def _time_window(text: str):
    """질문 속 시간 표현 → (시작일, 끝일) 또는 "latest"(지난번) 또는 None"""
    today = date.today()
    if re.search(r"그저께|그제", text):
        d = today - timedelta(days=2)
        return d, d
    if "어제" in text:
        d = today - timedelta(days=1)
        return d, d
    monday = today - timedelta(days=today.weekday())
    if re.search(r"지난\s?주|저번\s?주", text):
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if re.search(r"이번\s?주", text):
        return monday, today
    if re.search(r"지난번|저번에|예전에|전에|그때|마지막으로", text):
        return "latest"
    return None


class MemoryManager:
    def __init__(self):
        # 임베딩 모델은 여기서 한 번 로드해 모두 공유한다 (lore가 self.vector_db.embedding_model을 쓴다)
        self.store = VectorDB(collection_name="memory")
        self.vector_db = self.store
        # 최근 RAG 검색 로그 (앱의 '상태' 탭에서 "지금 무엇을 참조하는지" 표시용)
        self.recent_retrievals = deque(maxlen=20)
        self.recent_saves = deque(maxlen=20)
        # 에피소드 flush와 세션 요약이 서로(또는 자기 자신과) 겹치지 않게
        self._lock = threading.Lock()
        self._people_lock = threading.Lock()

    # ------------------------------------------------------------------ 사람 (ID ↔ 이름)
    def note_person(self, uid, name):
        """ID와 지금 닉네임을 기록 — 닉네임이 바뀌어도 같은 사람의 기억을 찾도록"""
        if not uid or not name:
            return
        uid = str(uid)
        with self._people_lock:
            people = _load_json(PEOPLE_PATH, {})
            p = people.get(uid, {"aliases": []})
            if p.get("name") == name and name in p["aliases"]:
                return
            p["name"] = name
            if name not in p["aliases"]:
                p["aliases"].append(name)
            p["updated"] = datetime.now().isoformat(timespec="seconds")
            people[uid] = p
            _save_json(PEOPLE_PATH, people)

    def person_key(self, uid=None, name=None):
        """기억을 묶는 키: 사용자 ID. ID가 없는 예전 줄이면 알려진 별칭으로 찾고, 그래도 없으면 name:이름"""
        if uid:
            return str(uid)
        if not name:
            return None
        for pid, p in _load_json(PEOPLE_PATH, {}).items():
            if name in p.get("aliases", []):
                return pid
        return f"name:{name}"

    def display_name(self, key, fallback=None):
        if not key:
            return fallback
        if key.startswith("name:"):
            return key[5:]
        return _load_json(PEOPLE_PATH, {}).get(key, {}).get("name") or fallback or key

    def _speakers(self, lines: list) -> dict:
        """대화 속 이름 → 사람 키 (등장 순서). 줄에 ID가 있으면 별칭도 기록해 둔다"""
        out = {}
        for m in lines:
            if m.get("role") != "user":
                continue
            sp = _SPEAKER_RE.match(m.get("content") or "")
            if not sp:
                continue
            name = sp.group(1)
            if m.get("uid"):
                self.note_person(m["uid"], name)
            if name not in out or (m.get("uid") and out[name].startswith("name:")):
                out[name] = self.person_key(m.get("uid"), name)
        return out

    # ------------------------------------------------------------------ 요약기 (로컬 Ollama)
    def _summarizer(self):
        """(base_url, model, options) — 기본은 지금 대화에 쓰는 로컬 모델. 모델이 같아야 VRAM을 더 쓰지 않는다"""
        ollama_cfg = config.get("llm", {}).get("ollama", {})
        model = _mem_cfg.get("summarizer_model")
        if not model:
            # 기본은 "지금 대화에 쓰는 모델" — provider가 Ollama가 아니면 그쪽을 따라간다.
            # (예전엔 Claude로 바꿔도 Ollama로 보내서 꺼져 있으면 연결 거부로 실패했다)
            try:
                import llm  # 순환 import 방지: 호출 시점에만
                cur = llm.current_llm()
                if cur.get("provider") == "ollama":
                    model = cur.get("model")
                elif cur.get("provider") == "llamacpp":
                    model = f"llamacpp:{cur.get('model')}"   # 같은 서버의 부수 슬롯으로 (VRAM 추가 없음)
                elif cur.get("provider") == "claude":
                    model = f"claude:{cur.get('model')}"
                    if not getattr(self, "_claude_summarizer_noted", False):
                        self._claude_summarizer_noted = True
                        print(f"[Memory] 대화 모델이 Claude라 기억 추출도 {cur.get('model')}로 갑니다 "
                              f"(로컬로 묶어 두려면 config의 memory.summarizer_model에 Ollama 모델명을 적으세요)")
            except Exception:
                model = None
            model = model or ollama_cfg.get("model")
        # num_ctx가 대화용과 다르면 Ollama가 같은 모델을 다시 로드한다 → 대화용 값을 그대로 쓴다
        options = {"temperature": 0.2, "num_ctx": ollama_cfg.get("num_ctx", 8192), "num_predict": 600}
        return ollama_cfg.get("base_url", "http://127.0.0.1:11434").rstrip("/"), model, options, ollama_cfg

    def _claude_chat(self, system: str, user: str, json_mode: bool, model: str) -> str:
        """Claude API로 한 번 묻는다 (동기 — 기억 정리는 백그라운드 스레드에서 돈다).

        JSON은 도구/구조화 출력 대신 프롬프트로 받는다 — 기존 추출 프롬프트가 이미
        "JSON 하나로만 답한다"이고, _unfence가 펜스를 벗기므로 그대로 호환된다."""
        import anthropic
        # 어떤 모델이 effort를 받는지는 provider가 안다 — 여기서 또 판단하면 또 어긋난다.
        # 실제로 어긋나서 haiku로 기억을 뽑을 때 400이 났다
        from providers.llm.claude import ClaudeLLM
        client = anthropic.Anthropic(timeout=120)
        sys_text = system + ("\n\n오직 JSON 객체 하나만 출력한다. 설명도 코드 펜스도 붙이지 않는다."
                             if json_mode else "")
        res = client.messages.create(
            model=model,
            max_tokens=_mem_cfg.get("summarizer_max_tokens", 1500),
            system=[{"type": "text", "text": sys_text, "cache_control": {"type": "ephemeral"}}],
            **({"output_config": {"effort": _mem_cfg.get("summarizer_effort", "medium")}}
               if ClaudeLLM._supports_effort(model) else {}),
            messages=[{"role": "user", "content": user}],
        )
        if res.stop_reason == "refusal":
            print(f"[Memory] Claude 거절: {getattr(res.stop_details, 'category', None)}")
            return ""
        u = res.usage
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        print(f"[Memory] Claude {model} — 입력 {u.input_tokens + cached}토큰(캐시 {cached}) "
              f"· 출력 {u.output_tokens}토큰")
        return "".join(b.text for b in res.content if b.type == "text").strip()

    def _local_chat(self, system: str, user: str, json_mode: bool, model: str = None) -> str:
        base_url, default_model, options, ollama_cfg = self._summarizer()
        model = model or default_model
        # summarizer_model이 "claude:..."면 API로 보낸다 (백그라운드라 지연이 무관하고,
        # 로컬 모델이 가장 많이 틀리는 자리다)
        if isinstance(model, str) and model.startswith("claude"):
            return self._claude_chat(system, user, json_mode,
                                     model.split(":", 1)[1] if ":" in model else model)
        if isinstance(model, str) and model.startswith("llamacpp:"):
            # llama.cpp 서버는 대화 provider가 띄운 것 하나뿐이다 — 그게 지금 provider일 때만 쓸 수 있다
            import llm  # 순환 import 방지: 호출 시점에만
            prov = llm._provider
            if getattr(prov, "model", None) != model.split(":", 1)[1] or not hasattr(prov, "slots"):
                raise RuntimeError(f"기억 정리용 {model}가 지금 떠 있지 않습니다 (대화 모델이 바뀌었다)")
            return prov.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                             options={"json": json_mode, "temperature": options["temperature"],
                                      "max_tokens": _mem_cfg.get("summarizer_max_tokens", 1500)})
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            "keep_alive": ollama_cfg.get("keep_alive", "30m"),
            "options": options,
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = httpx.post(f"{base_url}/api/chat", json=payload, timeout=httpx.Timeout(120, connect=5))
        except httpx.ConnectError as e:
            raise RuntimeError(
                f"기억 추출용 로컬 모델({model})에 붙지 못했습니다 — Ollama가 {base_url}에서 돌고 있지 않습니다. "
                f"대화 모델을 Claude로 바꾸면 Ollama는 꺼질 수 있습니다. "
                f"config의 memory.summarizer_model을 \"claude:claude-haiku-4-5\"처럼 적으면 API로 돌립니다") from e
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content", "").strip()

    def _local_json(self, system: str, user: str, model: str = None) -> dict:
        raw = self._local_chat(system, user, json_mode=True, model=model)
        try:
            data = json.loads(_unfence(raw))
        except json.JSONDecodeError:
            print(f"[Memory] 결과가 JSON이 아님: {raw[:200]}")
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 추출
    def extract(self, lines: list) -> dict:
        """에피소드 대화 → {episode, person_facts[{who(키), name, fact, due}], self_said, self_experience}. 실패하면 빈 dict"""
        text = _transcript(lines)
        if not text:
            return {}
        day = (_parse_ts(lines[-1].get("ts")) or datetime.now()).date()
        data = self._local_json(EXTRACT_PROMPT, f"{_calendar(day)}\n\n대화:\n{text}")
        if not data:
            return {}
        speakers = self._speakers(lines)
        facts = []
        for f in data.get("person_facts") or []:
            if not isinstance(f, dict):
                continue
            name, fact = str(f.get("who", "")).strip().strip("[]"), str(f.get("fact", "")).strip()
            if not name or not fact or name == NAME:
                continue
            # 대화에 없던 이름(요약기가 지어낸 "사용자" 등)은 화자가 한 명일 때만 그 사람으로 본다
            if name not in speakers:
                if len(speakers) == 1:
                    name = next(iter(speakers))
                else:
                    continue
            fact, due = _resolve_dates(_TIME_WORDS_RE.sub("", fact), day)
            # 모델이 적은 due는 쓰지 않는다 — 날짜가 없는 사실("어떤 게임을 한다")에 옆 일정의 날짜를 붙인 적이 있다.
            # 문장 속 날짜 표현에서 코드로 계산한 것만 믿는다
            facts.append({"who": speakers[name], "name": name, "fact": fact, "due": due})
        # 한 번에 뽑으면 작은 모델이 사실을 자주 빠뜨렸다 (두 사람 중 한 명을 통째로, 또는 "승급했어"
        # 같은 핵심 소식을 에피소드 요약에만 적고) → 사람마다 그 사람이 한 말만 주고 한 번 더 뽑아 합친다.
        # 대화 전체를 주면 남의 사실까지 그 사람 것으로 적었다
        for name, key in speakers.items():
            own = [m for m in lines if m.get("role") == "user" and (m.get("content") or "").startswith(f"[{name}]")]
            if own:
                facts += self._extract_person(_transcript(own), name, key, day)
        facts = self._dedupe_facts([f for f in facts if self._keep_person_fact(f["fact"])])
        said = [_TIME_WORDS_RE.sub("", str(s).strip()) for s in data.get("self_said") or [] if str(s).strip()]
        # 프롬프트로 막아도 "격려했다" 같은 대화 행위를 자기 설정으로 적는다 → 거르고, 두 개까지만
        said = [s for s in said if not _SELF_JUNK_RE.search(s) and not _SELF_ADVICE_RE.search(s)
                and not _SELF_CAPABILITY_RE.search(s) and _SELF_TASTE_RE.search(s)
                and not _SELF_NOT_TASTE_RE.search(s)][:2]
        # 캐릭터에게 있었던 일 — 취향(self_said)과 달리 사실이 아니라 사건이라 취향 필터를 걸지 않는다.
        # 자기 능력을 부정하는 말만 막는다 ("로그를 볼 수 없다고 했다"가 자기 설정으로 굳었던 적이 있다)
        exp = [_TIME_WORDS_RE.sub("", str(x).strip()) for x in data.get("self_experience") or [] if str(x).strip()]
        exp = [_resolve_dates(x, day)[0] for x in exp
               if not _SELF_CAPABILITY_RE.search(x) and not _EXP_EMPTY_RE.match(x)
               and not _EXP_HELPER_RE.search(x) and len(x) >= 8][:2]
        # 낱말을 다 통과한 것들을 대화와 나란히 한 번 더 보여 주고 주제를 분류받는다. 겪은 일에는
        # 봇작업 필터가 아예 없었고("대화 탭에 기록이 안 남는 버그를 확인했다"가
        # "너에게 있었던 일"로 저장됐다), 사람 사실도 낱말로는 절반이 통과했다. 지어낸 문장
        # (다른 화제의 숫자로 만든 "300미터 달리기를 한다")은 대화를 같이 줘야만 걸린다
        episode, _ = _resolve_dates(_TIME_WORDS_RE.sub("", str(data.get("episode") or "").strip()), day)
        # 에피소드도 같은 호출에 태운다. [기억]의 "예전에 있었던 일"이 이 블록인데, 저장 시점에도
        # 소급 청소에도 필터를 통과한 적이 한 번도 없었다 — 낱말 필터(_BOT_WORK_RE·_SESSION_META_RE)는
        # _keep_fact에서 사람 사실에만 걸렸다. 그래서 화면 테스트만 하던 세션의 에피소드가
        # "누가 봇의 화면 보는 기능을 테스트했고…"로 남아 이틀 뒤까지 매 턴 프롬프트에 올라왔다.
        # 후보 하나를 덧붙이는 것이라 호출 횟수는 그대로다.
        # 주제가 섞인 에피소드는 지배적인 쪽으로 분류되므로, 다른 이야기에 작업 한 줄이 섞인 정도로는
        # 버려지지 않는다
        cands = [f["fact"] for f in facts] + exp + ([episode] if episode else [])
        drop, fix = self._screen_facts(cands, text)
        nf0, ne0 = len(facts), len(exp)
        for i, better in sorted(fix.items()):
            if i in drop:
                continue               # 버릴 것은 고칠 필요가 없다
            print(f"[Memory] 이름 정정: {cands[i][:46]} → {better[:46]}")
            if i < nf0:
                facts[i]["fact"] = better
            elif i < nf0 + ne0:
                exp[i - nf0] = better
            else:
                episode = better
            cands[i] = better
        if drop:
            for i, tag in sorted(drop.items()):
                print(f"[Memory] 후보 버림 ({tag}): {cands[i][:60]}")
            nf, ne = len(facts), len(exp)   # 먼저 재 둔다 — 걸러낸 뒤에는 뒤쪽 번호가 밀린다
            facts = [f for i, f in enumerate(facts) if i not in drop]
            exp = [x for j, x in enumerate(exp) if (nf + j) not in drop]
            if episode and (nf + ne) in drop:
                episode = ""
        # 한 사람에 대해 너무 많이 뽑힌 에피소드는 잘못 뽑았을 가능성이 크다 — 앞쪽 몇 개만 남긴다
        kept, per = [], {}
        for f in facts:
            per[f["who"]] = per.get(f["who"], 0) + 1
            if per[f["who"]] <= MAX_PERSON_FACTS:
                kept.append(f)
        if len(kept) < len(facts):
            print(f"[Memory] 사람 사실 {len(facts) - len(kept)}개는 상한({MAX_PERSON_FACTS})을 넘어 버림: "
                  + " / ".join(f["fact"][:30] for f in facts if f not in kept))
        return {"episode": episode, "person_facts": kept, "self_said": said, "self_experience": exp}

    def _screen_facts(self, cands: list, transcript: str) -> tuple:
        """기억 후보를 대화와 나란히 보여 주고, (버릴 것 {번호: 분류}, 고칠 것 {번호: 문장})을 돌려준다.

        **고칠 것**은 고유명사가 대화의 표기와 어긋난 경우다. 음성 인식이 한 번 틀리면 그 형태가
        기억에 굳는다 — 게임 이름을 세 번 말했는데 한 번 잘못 들린 형태가 추출에 들어가
        **사실 8개에 박힌** 적이 있다. 대화 전문이 이미 이 호출에 들어오므로 정답이 같은
        화면에 있고, 돌려주는 모델도 분류와 같은 것(memory.screen_model)이라 호출이 늘지 않는다.
        낱말 표를 손으로 들고 있는 것과 달리 새 이름에도 저절로 듣는다 — 그리고 편집거리로는
        못 잡는다 (잘못 들린 꼴과 바른 꼴의 유사도가 정상 표기끼리보다 멀 때가 많다).

        낱말 목록은 그것을 쓴 날의 어휘만 잡는다. _BOT_WORK_RE를 한 세션에 맞춰 써 두었더니
        다른 세션에서 저장된 30개 중 0개를 걸렀고, 낱말을 넓혀도 41개 중 16개까지였다 — 남은
        것들은 공유하는 낱말이 없다.
        한 에피소드에 한 번만 부르고, 대화를 함께 주므로 근거 없이 지어낸 문장도 같이 걸러진다.

        답이 없는 번호는 life로 둔다 — 빠뜨린 것을 버리면 진짜 사실이 사라진다. 호출이 실패하면
        빈 것을 돌려준다: 추출을 막지 않고, 낱말 필터만 걸린 상태(지금 동작)로 남는다.
        """
        if not cands or not SCREEN_ON:
            return {}, {}
        nl, out, fix = chr(10), {}, {}
        model = _mem_cfg.get("screen_model") or _mem_cfg.get("summarizer_model")
        # 한 번에 다 물으면 답이 max_tokens를 넘겨 JSON이 깨지고 통째로 비어 돌아온다 (실측: 후보
        # 41개에서 한 회차가 아무것도 못 걸렀다). 번호는 원래 자리를 유지한 채 나눠 묻는다
        for start in range(0, len(cands), SCREEN_BATCH):
            chunk = cands[start:start + SCREEN_BATCH]
            listing = nl.join(f"{start + j}. {c}" for j, c in enumerate(chunk))
            try:
                data = self._local_json(
                    SCREEN_PROMPT, f"대화:{nl}{transcript}{nl}{nl}문장:{nl}{listing}", model=model)
            except Exception as e:
                print(f"[Memory] 후보 분류 실패 — 낱말 필터만 적용합니다: {e}")
                _log_event({"event": "memory_error", "stage": "screen", "model": model,
                            "error": str(e)[:200]})
                return out, fix
            for d in (data.get("labels") or []):
                if not isinstance(d, dict):
                    continue
                try:
                    i = int(d.get("i"))
                except (TypeError, ValueError):
                    continue
                tag = str(d.get("t") or "life").strip().lower()
                if not (start <= i < start + len(chunk)):
                    continue
                if tag != "life":
                    out[i] = tag[:20]
                # 이름만 고치라고 했으므로 길이가 크게 달라지면 믿지 않는다 (문장을 새로 쓴 것)
                f = str(d.get("fix") or "").strip()
                if f and f != cands[i] and 0.6 <= len(f) / max(1, len(cands[i])) <= 1.6 \
                        and _fix_came_from_transcript(cands[i], f, transcript):
                    fix[i] = f
        return out, fix

    def _keep_experience(self, text: str) -> bool:
        """겪은 일은 쌓이는 게 정상이라 교체하지 않는다. 거의 같은 문장이 이미 있을 때만 버린다"""
        try:
            hits = self.store.query_memory(
                text, n_results=3, where={"$and": [{"type": "self"}, {"kind": "experience"}]})
        except Exception:
            return True
        return not any(h["distance"] <= FACT_DEDUP_DISTANCE for h in hits)

    def _dedupe_facts(self, facts: list) -> list:
        """두 번 뽑아 합친 사실에서 같은 사람의 거의 같은 문장은 하나만 (먼저 나온 것, 날짜가 있는 쪽 우선)"""
        out = []
        for f in sorted(facts, key=lambda f: not f.get("due")):
            emb = self.store.embed(f["fact"])
            dup = False
            for g in out:
                if g["who"] == f["who"]:
                    d = sum((a - b) ** 2 for a, b in zip(emb, g["_emb"]))
                    if d <= FACT_DEDUP_DISTANCE:
                        dup = True
                        break
            if not dup:
                out.append({**f, "_emb": emb})
        return [{k: v for k, v in f.items() if k != "_emb"} for f in out]

    @staticmethod
    def _keep_person_fact(fact: str) -> bool:
        # 캐릭터에 대한 말이 사람 사실로 저장된 적이 있다 ("<캐릭터>가 대화를 나누는 데 어려움을 느낀다고 말했다")
        return not (_INTRO_RE.match(fact) or _FIRST_PERSON_RE.match(fact) or _SESSION_META_RE.search(fact)
                    or _BOT_WORK_RE.search(fact) or NAME in fact)

    def _extract_person(self, text: str, name: str, key: str, day: date) -> list:
        prompt = (f"아래는 '{name}'이(가) 한 말이다. {name}에 대해 나중에 다시 만났을 때 기억해 둘 사실만 뽑는다 — "
                  "하는 일, 취미, 배우는 것, 게임과 실력, 취향, 계획과 약속, 일정, 가족·반려동물, 최근 있었던 큰 일.\n"
                  "- 말을 그대로 옮기지 않는다. 주어 없이 제3자가 적는 짧은 평서문(\"~한다\", \"~했다\")으로 쓴다. "
                  "\"나는\", \"내가\"로 시작하지 않는다\n"
                  f"- {NAME}, 이 대화, 음성 채널, 봇의 기능, 음성 인식, 들어오고 나가는 것에 대한 말은 사실이 아니다. 빼라\n"
                  "- 인사, 질문, 감탄, 대화 행위는 빼고, 없으면 빈 목록\n"
                  "- 날짜는 대화에서 들은 말 그대로 쓴다. 대화에 없는 날짜를 지어내지 않는다\n"
                  'JSON으로만: {"facts": ["...", "..."]}')
        try:
            data = self._local_json(prompt, f"{_calendar(day)}\n\n대화:\n{text}")
        except httpx.HTTPError as e:
            print(f"[Memory] {name} 사실 추출 실패: {e}")
            return []
        out = []
        for f in data.get("facts") or []:
            if str(f).strip():
                fact, due = _resolve_dates(_TIME_WORDS_RE.sub("", str(f).strip()), day)
                out.append({"who": key, "name": name, "fact": fact, "due": due})
        return out

    # ------------------------------------------------------------------ 에피소드 저장
    def wake_up(self) -> dict:
        """서버 기동 때 한 번 — 채널에 들어가기 전부터 준비를 끝내 둔다 (executor에서 부를 것).

        1) 임베딩·검색 예열: 첫 검색이 차가울 때 7.7초가 걸렸다 (그 시간이 첫 대답에 그대로 붙었다)
        2) 밀린 대화를 기억으로 정리: 예전엔 봇이 붙고 20초 조용해야 돌았다
        3) 지금 상황을 로그로: 마지막 대화가 언제인지, 아는 사람, 날짜가 지난 일정
        """
        import time
        info = {}
        t0 = time.time()
        try:
            self.retrieve_relevant_memory("안녕")      # 임베딩 모델·Chroma 예열
            info["warm_sec"] = round(time.time() - t0, 1)
        except Exception as e:
            info["warm_error"] = str(e)

        try:
            info["saved"] = self.flush_episode("startup")
        except Exception as e:
            info["flush_error"] = str(e)

        try:
            info.update(self.status())
        except Exception as e:
            info["status_error"] = str(e)
        return info

    def status(self) -> dict:
        """지금 기억이 어떤 상태인지 — 마지막 대화, 아는 사람, 날짜가 지난 일정"""
        last = None
        for row in reversed(_read_history()):
            if row.get("ts"):
                last = _parse_ts(row["ts"])
                break
        people = _load_json(PEOPLE_PATH, {})
        today = date.today().isoformat()
        overdue = []
        try:
            for h in self.store.get_where({"type": "person"}) or []:
                meta = h.get("metadata") or {}
                if (meta.get("due") or "9999") < today:
                    overdue.append(f"{meta.get('name') or meta.get('who')}: {h.get('text', '')[:40]}")
        except Exception:
            pass
        return {"last_talk": last.isoformat(timespec="minutes") if last else None,
                "last_talk_ago": _ago(last.isoformat()) if last else None,
                "people": sorted({p.get("name") for p in people.values() if p.get("name")}),
                "overdue": overdue}

    def pending_lines(self) -> list:
        """아직 기억으로 정리하지 않은 기록 줄 (ts 없는 예전 줄은 대상이 아니다)"""
        until = _load_json(STATE_PATH, {}).get("episode_until", "")
        return [m for m in _read_history() if m.get("ts") and m["ts"] > until]

    def flush_episode(self, reason: str = "") -> int:
        """쌓인 대화를 에피소드 기억으로 저장. 저장한 항목 수를 돌려준다 (블로킹 — executor에서 부를 것)"""
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            lines = self.pending_lines()
            if not lines:
                return 0
            saved = 0
            for i in range(0, len(lines), EPISODE_CHUNK_LINES):
                chunk = lines[i:i + EPISODE_CHUNK_LINES]
                # 잘못 들었을 가능성이 큰 줄은 요약기에 보여 주지 않는다 — 작은 모델은 그것도 사실로 적는다
                heard = [m for m in chunk if not _misheard(m)]
                if len(heard) < len(chunk):
                    dropped = [m.get("content", "")[:40] for m in chunk if _misheard(m)]
                    print(f"[Memory] 확신도 낮은 줄 {len(dropped)}개는 기억에서 뺍니다: {dropped}")
                    _log_event({"event": "memory_misheard", "dropped": len(dropped),
                                "lines": len(chunk), "texts": dropped[:10]})
                # 사람 말이 한두 마디뿐인 조각(인사만 하고 끝 등)은 기억할 게 없다
                if sum(1 for m in heard if m.get("role") == "user") >= 2:
                    try:
                        saved += self._save_episode(heard, reason)
                    except Exception as e:
                        # 요약기가 죽어 있으면 표시만 옮기지 않고 다음 기회에 다시 시도한다
                        print(f"[Memory] 에피소드 저장 실패 ({reason}): {e}")
                        return saved
                _save_json(STATE_PATH, {"episode_until": chunk[-1]["ts"]})
            return saved
        finally:
            self._lock.release()

    def _save_episode(self, lines: list, reason: str) -> int:
        data = self.extract(lines)
        ts = lines[-1]["ts"]
        speakers = self._speakers(lines)
        saved = 0
        if data.get("episode"):
            self.store.add_memory(data["episode"], with_topic(
                {"type": "episode", "who": ",".join(speakers.values()),
                 "name": ", ".join(speakers), "ts": ts}, data["episode"]))
            saved += 1
        ops = []
        by_person = {}
        for f in data.get("person_facts", []):
            by_person.setdefault((f["who"], f["name"]), []).append(f)
        for (key, name), facts in by_person.items():
            ops += self._reconcile("person", key, f"{name} 님", facts, ts)
        if data.get("self_said"):
            ops += self._reconcile("self", SELF_KEY, f"{NAME}(너 자신)",
                                   [{"fact": s, "due": "", "name": NAME} for s in data["self_said"]], ts)
        for x in data.get("self_experience", []):
            if self._keep_experience(x):
                self.store.add_memory(x, with_topic(
                    {"type": "self", "kind": "experience", "who": SELF_KEY,
                     "name": NAME, "ts": ts}, x))
                ops.append(("exp", x, []))
        saved += sum(1 for o in ops if o[0] in ("add", "replace", "exp"))
        self.recent_saves.appendleft({"ts": ts, "reason": reason, "lines": len(lines), **data,
                                      "person_facts": [f"{f['name']}: {f['fact']}" for f in data.get("person_facts", [])],
                                      "ops": [f"{op} {text}" + (f" ← {old}" if old else "") for op, text, old in ops]})
        print(f"[Memory] 에피소드 저장 ({reason}, {len(lines)}줄): {data.get('episode', '')[:80]}")
        for op, text, old in ops:
            print(f"[Memory]   {op:7s} {text}" + (f"  (← {' / '.join(old)})" if old else ""))
        try:
            with open(LONG_TERM_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] ({reason}) {', '.join(speakers)}\n")
                f.write(f"-> 에피소드: {data.get('episode', '')}\n")
                for op, text, old in ops:
                    f.write(f"-> {op}: {text}" + (f" (← {' / '.join(old)})" if old else "") + "\n")
                f.write("=" * 40 + "\n")
        except OSError:
            pass
        return saved

    def _reconcile(self, type_: str, key: str, label: str, new: list, ts: str) -> list:
        """
        새 사실을 그 사람의 기존 사실과 맞춰 본다 → [(op, 새 문장, [지운 옛 문장])].
        모델이 add / replace(old) / skip을 고른다. 모델 판단이 실패하면 거리 기준으로 합친다
        """
        where = {"$and": [{"type": type_}, {"who": key}]}
        existing = self.store.get_where(where)
        def meta(f):
            m = with_topic({"type": type_, "who": key, "name": f.get("name", ""), "ts": ts}, f["fact"])
            if _looks_like_state(f["fact"]):
                # 상태에는 일정 날짜를 붙이지 않는다 — "오늘 술 마셨다"가 지난 일정이 되던 문제
                m["kind"] = "state"
            elif f.get("due"):
                m["due"] = f["due"]
            return m
        if not existing:
            for f in new:
                self.store.add_memory(f["fact"], meta(f))
            return [("add", f["fact"], []) for f in new]

        if len(existing) > RECONCILE_MAX:
            # 새 사실과 가까운 것 위주로 보여 준다
            near = {}
            for f in new:
                for h in self.store.query_memory(f["fact"], n_results=4, where=where):
                    near[h["id"]] = h
            recent = sorted(existing, key=lambda h: h["metadata"].get("ts", ""), reverse=True)
            existing = (list(near.values()) + [h for h in recent if h["id"] not in near])[:RECONCILE_MAX]

        listing = "\n".join(f"{i + 1}. {h['text']} ({_md(_parse_ts(h['metadata'].get('ts')) or datetime.now())})"
                            for i, h in enumerate(existing))
        news = "\n".join(f"{i + 1}. {f['fact']}" for i, f in enumerate(new))
        try:
            data = self._local_json(RECONCILE_PROMPT.format(who=label), f"기존 기억:\n{listing}\n\n새 사실:\n{news}")
            decisions = {int(d.get("new")): d for d in data.get("decisions") or []
                         if isinstance(d, dict) and str(d.get("new", "")).isdigit()}
        except (httpx.HTTPError, ValueError, TypeError) as e:
            print(f"[Memory] 교체 판단 실패, 거리 기준으로 합침: {e}")
            decisions = None

        ops, deleted = [], set()
        day = (_parse_ts(ts) or datetime.now()).date()
        for i, f in enumerate(new, 1):
            if decisions is None:
                old = [h for h in self.store.query_memory(f["fact"], n_results=3, where=where)
                       if h["distance"] <= FACT_DEDUP_DISTANCE and h["id"] not in deleted]
                op = "replace" if old else "add"
            else:
                d = decisions.get(i, {"op": "add"})
                op = str(d.get("op", "add")).lower()
                olds = d.get("old") or []
                olds = olds if isinstance(olds, list) else [olds]
                old = [existing[int(o) - 1] for o in olds
                       if str(o).isdigit() and 1 <= int(o) <= len(existing) and existing[int(o) - 1]["id"] not in deleted]
                if op == "replace" and not old:
                    op = "add"
            if op == "skip":
                # "코딩 테스트에 합격했다"를 "코딩 테스트가 있다"와 같다며 건너뛴 적이 있다 —
                # 표현만 다른 거의 같은 문장(거리 90 이하)이 실제로 있을 때만 건너뛴다
                near = self.store.query_memory(f["fact"], n_results=1, where=where)
                if not near or near[0]["distance"] > FACT_DEDUP_DISTANCE * 1.5:
                    op = "add"
            if op == "add" and not f.get("due"):
                # 날짜가 지난 일정의 결과가 들어오면 그 일정을 바꾼다 ("코딩 테스트가 있다" → "합격했다")
                new_tokens = _tokens(f["fact"])
                for h in existing:
                    due = h["metadata"].get("due")
                    if h["id"] in deleted or not due or due > day.isoformat():
                        continue
                    if _tokens(h["text"]) & new_tokens:
                        op, old = "replace", [h]
                        break
            if op == "skip":

                ops.append(("skip", f["fact"], []))
                continue
            if op not in ("add", "replace"):
                op = "add"
            for h in old if op == "replace" else []:
                deleted.add(h["id"])
            self.store.delete([h["id"] for h in old] if op == "replace" else [])
            self.store.add_memory(f["fact"], meta(f))
            ops.append((op, f["fact"], [h["text"] for h in old] if op == "replace" else []))
        return ops

    # ------------------------------------------------------------------ 검색
    def _lived_line(self, h: dict) -> str:
        """겪은 일 한 줄 — 언제 있었던 일인지 붙인다 (지금 일로 착각하지 않게)"""
        ts = h["metadata"].get("ts", "")
        return f"- ({_ago(ts)}) {h['text']}" if _parse_ts(ts) else f"- {h['text']}"

    def _still_fresh(self, h: dict) -> bool:
        """그때의 상태는 며칠 지나면 빼준다. 취향·계획은 언제 들었든 남긴다"""
        if (h.get("metadata") or {}).get("kind") != "state":
            return True
        t = _parse_ts(h["metadata"].get("ts"))
        if not t:
            return True
        return (datetime.now() - t).days < STATE_TTL_DAYS

    def _fact_line(self, h: dict) -> str:
        t = _parse_ts(h["metadata"].get("ts"))
        line = f"- {h['text']}" + (f" ({_md(t)}에 들음)" if t else "")
        due = h["metadata"].get("due")
        if due:
            try:
                d = date.fromisoformat(due)
                if d < date.today():
                    line += " — 그 날짜가 지났는데 어떻게 됐는지 아직 못 들었다"
                elif d == date.today():
                    line += " — 바로 오늘이다"
                else:
                    # 표시가 없으면 "9월 23일에 코딩 테스트가 있다"를 두고 "오늘 테스트 어땠어요?"라고 물었다
                    line += f" — 아직 안 왔다 ({(d - date.today()).days}일 뒤)"
            except ValueError:
                pass
        return line

    def retrieve_relevant_memory(self, query: str, speaker: str = None, prev_user: str = None,
                                 speaker_id: str = None, max_distance: float = MEMORY_MAX_DISTANCE) -> str:
        """
        [기억] 세 칸
        - 지금 말하는 사람에 대해 아는 것: 검색 없이 늘 (많으면 최근 것 + 관련 있는 것)
        - 캐릭터가 자기에 대해 한 말: 늘 (말을 바꾸지 않게)
        - 캐릭터에게 있었던 일: 최근 + 관련 있는 것 (겪은 일은 계속 쌓인다)
        - 있었던 일: "어제/지난주/지난번" 같은 시간 + 가장 최근 대화 + 관련 있는 것
        """
        q = f"{prev_user} {query}".strip() if prev_user else query
        log = {"ts": datetime.now().strftime("%H:%M:%S"), "query": q, "hits": []}
        self.recent_retrievals.appendleft(log)
        if not q or self.store.count() == 0:
            return ""
        emb = self.store.embed(q)

        def pick(where, n, gate=True):
            hits = self.store.query_memory(q, n_results=n, where=where, embedding=emb)
            for h in hits:
                h["kept"] = not gate or h["distance"] <= max_distance
            return [h for h in hits if h["kept"]]

        def core(where, limit, min_recent):
            """이 턴에 관련 있는 사실이 자리를 먼저 가져간다. 최근 것은 바닥만 남긴다.

            예전엔 최근 6 : 관련 2로 고정이라, 같은 주제의 사실이 몇 개든 2개만 올라왔다
            (실측: 게임 규칙 13개 중 2개, 나머지 6자리는 무관한 최근 사실).

            **거리 게이트는 쓰지 않는다.** 게이트가 120인데 "<게임> 규칙 다시 알려줘"에
            대한 그 게임 사실들이 146~192였다 — 게임 이름을 그대로 말한 질문에도 전부
            걸러진다. 게이트를 올리는 건 따로 위험한 일이라(그 값이 120인 이유가 무관한 옛
            기억을 꺼내던 문제다) 여기서는 그냥 가까운 순으로 자리를 준다. 주제가 없는 턴이면
            '가장 가까운 것'도 약하게 관련된 것들인데, 그건 아무 최근 사실보다 나쁘지 않다."""
            rows = sorted(self.store.get_where(where), key=lambda h: h["metadata"].get("ts", ""), reverse=True)
            if len(rows) <= limit:
                return rows
            keep = pick(where, limit, gate=False)[:max(0, limit - min_recent)]
            ids = {h["id"] for h in keep}
            keep += [h for h in rows if h["id"] not in ids][:limit - len(keep)]
            return keep

        def logged(rows, kind):
            for h in rows:
                log["hits"].append({"summary": f"[{kind}] {h['text']}"[:120],
                                    "distance": round(h.get("distance", 0.0), 3), "kept": True})
            return rows

        parts = []
        # 이 턴이 어떤 주제에 관한 것이면 그 주제에 **자기 칸**을 준다. 주제 기억은 type이
        # person/episode/self로 흩어져 있어서 사람 8칸·에피소드 칸을 서로 다투고, 거리 게이트에도
        # 걸린다 — 실측: "<게임> 규칙 다시 알려줘"에 그 게임 9행이 125~179로 나와
        # 게이트(120)를 **1개만** 통과했다. 이름을 그대로 말한 질문인데도 그렇다.
        # 그래서 게이트를 올리는 대신(그 값이 120인 이유가 무관한 옛 기억을 꺼내던 문제다)
        # 주제가 걸린 턴에만 그 주제의 기억을 가까운 순으로 꺼내 따로 싣는다.
        topic = topic_of(q)
        topic_ids = set()
        if topic:
            rows = self.store.get_where({"about": topic})
            if rows:
                near = pick({"about": topic}, TOPIC_MAX, gate=False)
                ids = {h["id"] for h in near}
                near += [h for h in sorted(rows, key=lambda h: h["metadata"].get("ts", ""), reverse=True)
                         if h["id"] not in ids][:TOPIC_MAX - len(near)]
                near = logged(near[:TOPIC_MAX], "주제")
                topic_ids = {h["id"] for h in near}
                parts.append(f"지금 이야기에 나온 '{topic}'에 대해 아는 것:\n"
                             + "\n".join(self._fact_line(h) for h in near))
        key = self.person_key(speaker_id, speaker)
        if key:
            person = logged(core({"$and": [{"type": "person"}, {"who": key}]},
                                 PROFILE_MAX, PROFILE_MIN_RECENT), "사람")
            # 주제 칸에 이미 실은 것은 빼서 같은 줄이 두 번 들어가지 않게 한다
            person = [h for h in person if self._still_fresh(h) and h["id"] not in topic_ids]
            if person:
                name = self.display_name(key, speaker)
                today = date.today().isoformat()
                overdue = [h for h in person if (h["metadata"].get("due") or "9999") < today]
                if overdue:
                    # 사실 목록 속에 "날짜가 지났다"고 표시만 해 두면 원본 gemma4도 먼저 꺼내지 않았다 (0/4)
                    # → [기억] 맨 위에 할 일로 따로 둔다
                    parts.append("먼저 챙길 것: " + " / ".join(
                        f"{name} 님이 말했던 '{h['text'].rstrip('.')}' — 날짜가 지났는데 어떻게 됐는지 아직 못 들었다"
                        for h in overdue) + ". 인사나 가벼운 말이 오면 그게 어떻게 됐는지 먼저 물어본다.")
                parts.append(f"{name} 님에 대해 아는 것:\n" + "\n".join(self._fact_line(h) for h in person))
        # 자기 기억은 두 칸이다. 취향·설정에는 "말을 바꾸지 말 것"이 붙고, 겪은 일에는 붙지 않는다
        # (사건에까지 그 말이 붙으면 지난 일을 지금 일처럼 우긴다). 예전 줄은 kind가 없어 취향으로 본다
        self_rows = sorted(self.store.get_where({"$and": [{"type": "self"}, {"who": SELF_KEY}]}),
                           key=lambda h: h["metadata"].get("ts", ""), reverse=True)
        # 겪은 일이 계속 쌓이므로 한 번에 뽑아 최신순으로 자르면 취향이 밀려난다 → 칸마다 따로 센다
        # 취향·설정도 같은 배분을 쓴다. 최근순으로만 자르면 "에어컨 바람이 편하다"가
        # 6번째로 최근이라는 이유로 무인도 대화에까지 "말을 바꾸지 말 것" 아래 들어왔다
        said_all = [h for h in self_rows if h["metadata"].get("kind") != "experience"]
        if len(said_all) <= SELF_MAX:
            said = said_all
        else:
            said = pick({"$and": [{"type": "self"}, {"who": SELF_KEY}]},
                        SELF_MAX * 2, gate=False)
            said = [h for h in said if h["metadata"].get("kind") != "experience"][:SELF_MAX - SELF_MIN_RECENT]
            said_ids = {h["id"] for h in said}
            said += [h for h in said_all if h["id"] not in said_ids][:SELF_MAX - len(said)]
        said = logged([h for h in said if h["id"] not in topic_ids], "자기")
        lived = [h for h in self_rows if h["metadata"].get("kind") == "experience"][:SELF_EXP_MAX - 1]
        seen_ids = {h["id"] for h in lived}
        lived += [h for h in pick({"$and": [{"type": "self"}, {"kind": "experience"}]},
                                  SELF_EXP_MAX, gate=False) if h["id"] not in seen_ids][:1]
        lived = [h for h in lived if h["id"] not in topic_ids]
        lived = logged(sorted(lived, key=lambda h: h["metadata"].get("ts", ""), reverse=True), "겪은 일")
        if said:
            parts.append("네가 전에 너 자신에 대해 한 말 (말을 바꾸지 말 것 — 같은 대상이면 가장 최근 것이 지금 생각.\n"
                         "단 취향은 그게 가능한 자리에서만 성립한다 — 없는 것을 두고 취향을 우기지 않는다):\n"
                         + "\n".join(self._fact_line(h) for h in said))
        if lived:
            parts.append("너에게 있었던 일 (네 기억이다 — 남에게 들은 게 아니라 네가 겪은 것):\n" + "\n".join(
                self._lived_line(h) for h in lived))

        episodes = {}
        all_eps = None
        window = _time_window(query)
        if window:
            all_eps = sorted(self.store.get_where({"type": "episode"}),
                             key=lambda h: h["metadata"].get("ts", ""), reverse=True)
            if window == "latest":
                chosen = all_eps[:2]
            else:
                lo, hi = window
                chosen = [h for h in all_eps if (t := _parse_ts(h["metadata"].get("ts"))) and lo <= t.date() <= hi][:3]
            episodes.update({h["id"]: h for h in logged(chosen, "시간")})
        # 가장 최근 대화는 늘 — 새로 시작한 대화가 지난번과 이어지게
        latest = (all_eps or sorted(self.store.get_where({"type": "episode"}),
                                    key=lambda h: h["metadata"].get("ts", ""), reverse=True))[:1]
        episodes.update({h["id"]: h for h in logged(latest, "최근")})
        for h in pick({"type": "episode"}, 2):
            if h["id"] not in episodes and len(episodes) < 4:
                episodes[h["id"]] = logged([h], "관련")[0]
        if episodes:
            rows = sorted(episodes.values(), key=lambda h: h["metadata"].get("ts", ""), reverse=True)
            # 모델이 요약 속 "아까"를 보고 며칠 전 일을 방금 일처럼 꺼냈다 → 언제 일인지 붙인다
            parts.append("예전에 있었던 일 (지금 대화가 아니다 — '아까'라고 하지 말 것):\n" + "\n".join(
                f"- ({_ago(h['metadata'].get('ts', ''))}, {_md(_parse_ts(h['metadata']['ts']))}) {h['text']}"
                if _parse_ts(h["metadata"].get("ts")) else f"- {h['text']}" for h in rows))
        return "\n".join(parts)

    def last_talk_with(self, key: str = None, name: str = None):
        """그 사람이 마지막으로 말한 시각 (datetime) — 기록에 없으면 None. ID가 없는 예전 줄은 이름으로"""
        prefix = f"[{name}]" if name else None
        for m in reversed(_read_history()):
            if m.get("role") != "user" or not m.get("ts"):
                continue
            if (key and m.get("uid") == key) or (prefix and not m.get("uid") and (m.get("content") or "").startswith(prefix)):
                return _parse_ts(m["ts"])
        return None

    # ------------------------------------------------------------------ 이번 세션 요약
    def _session_lines(self, history: list = None) -> list:
        """마지막 공백(30분+) 이후의 줄 = 이번 세션. 마지막 줄이 이미 30분 넘게 지났으면 세션 없음"""
        history = history if history is not None else _read_history()
        if not history or not history[-1].get("ts"):
            return []
        now = datetime.now()
        times = [_parse_ts(m.get("ts")) for m in history]
        if times[-1] is None or (now - times[-1]).total_seconds() >= SESSION_GAP_SEC:
            return []
        start = len(history) - 1
        while start > 0 and times[start - 1] is not None and \
                (times[start] - times[start - 1]).total_seconds() < SESSION_GAP_SEC:
            start -= 1
        return history[start:]

    def update_session_summary(self) -> bool:
        """이번 세션에서 20줄 창 밖으로 밀려난 줄이 10줄 더 쌓였으면 요약을 갱신 (블로킹)"""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            session = self._session_lines()
            if not session:
                return False
            out = session[:-HISTORY_WINDOW] if len(session) > HISTORY_WINDOW else []
            state = _load_json(SESSION_SUMMARY_PATH, {})
            if state.get("session_start") != session[0]["ts"]:
                state = {"session_start": session[0]["ts"], "covered": 0, "summary": ""}
            new = out[state["covered"]:]
            if len(new) < SESSION_SUMMARY_STEP:
                return False
            prompt = (f"기존 요약: {state['summary']}\n\n" if state["summary"] else "") + f"새 대화:\n{_transcript(new)}"
            try:
                summary = self._local_chat(SESSION_PROMPT, prompt, json_mode=False)
            except Exception as e:
                print(f"[Memory] 세션 요약 실패: {e}")
                return False
            if summary:
                # 요약기가 "**[새로운 요약]**" 같은 머리말을 붙였다 — 기호와 머리말 줄을 걷어 낸다
                summary = "\n".join(l for l in summary.replace("**", "").replace("#", "").splitlines()
                                    if l.strip() and not re.match(r"^\s*\[[^\]]*\]\s*$", l))
                state.update(covered=len(out), summary=_TAG_RE.sub("", summary).strip(),
                             updated=datetime.now().isoformat(timespec="seconds"))
                _save_json(SESSION_SUMMARY_PATH, state)
                print(f"[Memory] 세션 요약 갱신 ({len(out)}줄): {state['summary'][:80]}")
            return True
        finally:
            self._lock.release()

    def session_summary(self) -> str:
        """지금 이어지는 세션의 앞부분 요약 (세션이 끝났거나 창 안에 다 들어가면 빈 문자열)"""
        state = _load_json(SESSION_SUMMARY_PATH, {})
        if not state.get("summary"):
            return ""
        session = self._session_lines()
        if not session or session[0].get("ts") != state.get("session_start"):
            return ""
        return state["summary"]

    # ------------------------------------------------------------------ 예전 인터페이스 (main.py CLI)
    def summarize_conversation(self, conversation: str) -> str:
        if len(conversation.strip().split()) < 3:
            return ""
        try:
            return self._local_chat("다음 대화를 한 줄로 요약해라. 중요한 정보만 간단명료하게.", conversation, json_mode=False)
        except Exception as e:
            print(f"❌ 요약 오류: {e}")
            return ""

    def save_memory(self, conversation: str, summary: str):
        self.store.add_memory(summary, with_topic(
            {"type": "episode", "who": "", "name": "",
             "ts": datetime.now().isoformat(timespec="seconds")}, summary))


# 전역 인스턴스
memory_manager = MemoryManager()


def summarize_conversation(conversation: str) -> str:
    return memory_manager.summarize_conversation(conversation)


def append_to_long_term_memory(conversation: str, summary: str):
    return memory_manager.save_memory(conversation, summary)
