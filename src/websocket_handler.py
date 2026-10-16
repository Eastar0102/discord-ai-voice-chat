from fastapi import WebSocket, WebSocketDisconnect
import numpy as np
import asyncio
import json
import os
import traceback
import base64
import time
from datetime import datetime
import scipy.signal
from concurrent.futures import ThreadPoolExecutor
import re
import random
from collections import deque

from vad import StreamingVAD
from stt import transcribe, set_hotword_names, last_stats as stt_last_stats
from llm import (chat_with_gpt, chat_with_gpt_stream, extract_emotion_and_clean, save_to_history,
                 strip_stage_directions, prefetch_prompt, STAGE_DIRECTION_RE, ago_text,
                 load_conversation_history, judge_directed_at_me as llm_judge,
                 DOC_START_RE, DOC_BOLD_RE, cut_document)
from tts import synthesize
from memory import memory_manager, MEMORY_MAX_DISTANCE as MEMORY_GATE
from manuscript import Manuscript
from activity import Activity, TALK, WRITING
from config_loader import load_config
import character

NAME = character.NAME

_BASE_DIR = os.path.dirname(os.path.dirname(__file__))
METRICS_PATH = os.path.join(_BASE_DIR, "data", "metrics.jsonl")
VOICE_EVENTS_PATH = os.path.join(_BASE_DIR, "data", "voice_events.jsonl")

class WebSocketHandler:
    def __init__(self):
        # 48kHz -> 16kHz 변환을 위해 VAD는 16kHz로 설정
        self.vad = StreamingVAD(sample_rate=16000, frame_duration=30)
        self.processing_task = None
        self.speech_queue = asyncio.Queue()

        # 듣기 모드
        #   auto   — 답할지 스스로 고른다 (기본). 둘뿐이면 거의 다 답하고, 여럿이면 가려 답한다
        #   always — 들어온 발화마다 답한다 (1:1로 붙잡고 테스트할 때)
        #   call   — 이름을 불러야 답한다 (게임·회의 중)
        # auto가 생기기 전에는 always와 call로 갈라 두 극단만 쓸 수 있었다
        self.mode = "auto"   # auto: 스스로 고름 · always: 무조건 답함 · call: 이름을 불러야
        # 원고 같이 읽기 — 웹 에디터 화면을 주기적으로 OCR해, 새로 쓴 대목에 가끔 한마디 한다
        self.manuscript = Manuscript(load_config().get("manuscript", {}))
        self.manuscript_task = None
        # 지금 무엇을 하는 중인가 (대화 / 글쓰기). 전사는 여기 없다 — 지능과 무관한 별개 기능
        self.activity = Activity(load_config().get("manuscript", {}))
        # 접속마다 handle_connection이 0으로 되돌리지만, 접속 밖에서 불릴 수도 있으니 여기도 둔다
        self.turn_seq = 0

        # STT 직렬화 락: 취소된 턴의 Whisper 작업(스레드)은 즉시 죽지 않으므로,
        # 락 없이는 좀비 STT들이 GPU를 점유하며 새 STT를 수 초씩 지연시킴.
        # 락 대기 중 취소된 턴은 transcribe를 시작하지 않고 깨끗하게 종료됨.
        self.stt_lock = asyncio.Lock()
        # Whisper 전용 스레드 하나 — 기본 실행기를 쓰면 타임아웃 뒤에도 스레드가 살아 있어
        # 다음 호출과 같이 돌고, 그때부터 전부 느려져 연쇄로 시간 초과가 난다
        self.stt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._stt_inflight = None      # 아직 돌고 있는 전사 (타임아웃이 나도 스레드는 산다)
        self.stt_stuck_since = None
        _stt_cfg = load_config().get("stt", {})
        self.STT_TIMEOUT_SEC = float(_stt_cfg.get("timeout_sec", self.STT_TIMEOUT_SEC))
        self.STT_QUEUE_WAIT_SEC = float(_stt_cfg.get("queue_wait_sec", self.STT_QUEUE_WAIT_SEC))

        # 롤링 부분 전사 (RealtimeSTT 패턴): 발화 중 누적 버퍼를 ~0.5초 간격으로
        # 미리 전사해 두고, 발화 종료 시 음성 구간을 전부 덮은 결과면 재사용한다.
        # 침묵 시작 시점에 부분 전사가 종결부호로 끝나면 발화를 조기 확정한다.
        self.partial_task = None          # 진행 중인 부분 전사 태스크
        self.partial_text = None          # 마지막 부분 전사 결과
        self.partial_utt_id = -1          # 결과가 속한 발화 id
        self.partial_covered_frames = 0   # 결과가 커버한 프레임 수
        self._partial_sched_utt = -1      # 마지막으로 스케줄한 발화 id / 프레임 수
        self._partial_sched_frames = 0

        # 생성한 태스크의 강한 참조 보관 — 이벤트 루프는 약한 참조만 들고 있어서
        # 참조 없는 태스크는 실행 도중에도 GC로 소리 없이 사라질 수 있음
        self.background_tasks = set()

        # 발화 판단(turn-taking)용 상태 — Node가 보내는 speaker 메시지로 갱신
        self.current_speaker = None
        self.current_speaker_id = None  # 디스코드 사용자 ID — 기억은 닉네임이 아니라 ID로 묶는다
        self.channel_members = []
        self.human_count = 1
        self.last_reply_speaker = None  # 마지막으로 응답한 상대
        self.last_reply_time = 0

        # 지금 있는 곳과 최근 입장·퇴장 — 봇이 voice_event로 알려준다. 캐릭터는 어느 서버에서든
        # 같은 한 명이라 기록·기억은 나누지 않고, 위치는 [현재 상황]의 맥락으로만 넣는다
        self.guild_name = None
        self.channel_name = None
        self.voice_events = deque(maxlen=6)   # (시각, 문장)
        self.last_leave_note = None           # 이번 입장 직전의 퇴장이 얼마 전이었는지
        # 채널에 있는 사람별 표 — 흘러가는 사건 목록(최근 6건·2시간)은 여러 번 드나들면 밀려나고
        # 오래 있으면 사라져서, "누가 언제부터 있는지"를 사람마다 따로 들고 있는다
        self.joined_at = None                 # 봇이 이 채널에 들어온 시각
        self.presence = {}                    # 이름 → {"since": datetime | None(봇보다 먼저), "joins": 들어온 횟수}
        self.recent_leaves = deque(maxlen=4)  # (이름, 나간 시각)

        # 진행 중인 턴의 사용자 발화 — 말하다 잠깐 멈춰 발화가 두 조각으로 확정되면
        # 뒷조각이 앞 턴을 취소하는데, 이때 앞조각 텍스트를 이어붙여 문장을 복원한다
        self.inflight_text = None
        self.inflight_wake = False
        self.inflight_stt = None          # 앞조각의 STT 확신도 (병합하면 둘 중 나쁜 쪽)
        self.partial_stats = None         # 마지막 부분 전사의 확신도 (부분 전사를 재사용할 때 쓴다)

        # 기억 정리 시점·입장 인사 (config memory 섹션)
        _mem = load_config().get("memory", {})
        self.episode_idle_sec = _mem.get("episode_idle_sec", 300)
        self.episode_max_turns = _mem.get("episode_max_turns", 20)
        self.greet_after_sec = _mem.get("greet_after_hours", 6) * 3600
        self.greet_cooldown_sec = _mem.get("greet_cooldown_min", 30) * 60
        self.greeted = {}        # 사람 키(ID) → 마지막으로 먼저 인사한 시각 (time.time)
        # 금방 다시 들어온 사람에게도 짧게 인사한다 — 같은 사람에겐 이 간격에 한 번까지
        self.greet_rejoin_cooldown_sec = _mem.get("greet_rejoin_cooldown_min", 5) * 60
        # 봇 자신이 채널에 들어왔을 때는 오랜만인지와 무관하게 있던 사람들에게 한 번 인사한다
        self.arrival_pending = False
        # 이번 턴의 첫 TTS 조각 계측 (metrics의 llm_first_s / tts_first_s)
        self.turn_first_queued_at = self.turn_first_tts_s = self.turn_first_chars = None
        # 말하는 중 프롬프트 미리 먹이기 (prefill 캐시 데우기) — 발화마다 한 번
        self.prefetch_enabled = load_config().get("llm", {}).get("prefetch", True)
        self._prefetch_utt = -1
        # 먼저 말 걸기 — 아무도 말하지 않으면 캐릭터도 가만히 있어서 대화가 늘 수동적이었다
        self.initiative_on = _mem.get("initiative", True)
        # 관심이 0일 때 먼저 말을 꺼내기까지의 시간. 관심이 있으면 절반까지 줄고,
        # 대답이 없으면 backoff만큼 늘어난다 (예전엔 [60, 180] 두 번 뒤 영영 멈췄다)
        _after = _mem.get("initiative_after_sec", [75])
        self.initiative_base = float(_after[0] if isinstance(_after, list) else _after)
        self.initiative_backoff = float(_mem.get("initiative_backoff", 1.8))
        self.initiative_backoff_max = float(_mem.get("initiative_backoff_max", 8.0))
        self.initiative_count = 0       # 대답 없이 연달아 먼저 말을 건 횟수
        self.last_activity = None       # 마지막으로 누가(캐릭터 포함) 말한 시각 — 없으면 이번 접속에서 대화 전
        self.initiative_task = None
        self.websocket = None
        self.memory_task = None

        # 방금 나와 대화하던 상대로 보는 시간. 30초는 1:1에서 사실상 "항상"이라
        # 게이트가 죽었다 (실사용 286발화에서 보류 0건)
        self.ENGAGED_SEC = float(_mem.get("engaged_sec", 12))
        # auto에서 규칙 위에 모델 판정을 한 번 더 두를지 (config: memory.judge).
        # must_answer() 안전장치와 함께 쓸 때만 켤 만하다 — 판정만 쓰면 질문까지 삼킨다
        self.judge_on = bool(_mem.get("judge", True))

        # 채널 사람 이름을 STT 힌트로 줄지 (config: stt.name_hotwords). 소음이 이름으로
        # 둔갑하는 부작용이 심하면 끈다
        self.name_hotwords = bool(load_config().get("stt", {}).get("name_hotwords", True))
        self.hotword_set = {NAME}

        # 필터 설정 (호출어는 캐릭터 팩, 무시 문구는 config)
        _config = load_config()
        _filters = _config.get("filters", {})
        self.wake_words = [w.lower() for w in (_filters.get("wake_words") or character.WAKE_WORDS)]
        # 이름이 자주 잘못 들리는 꼴 → 바른 이름 (캐릭터 팩 stt_fixes)
        self.stt_fixes = dict(character._pack.get("stt_fixes", {}))
        self.ignore_phrases = set(_filters.get("ignore_phrases", []))
        self.ignore_partial = _filters.get("ignore_partial", [])

        # 48k→16k 데시메이션용 안티앨리어싱 로우패스 (스트리밍이므로 필터 상태 유지)
        self.aa_b, self.aa_a = scipy.signal.butter(4, 7200, btype="low", fs=48000)
        self.aa_zi = scipy.signal.lfilter_zi(self.aa_b, self.aa_a) * 0.0

        # 오디오 스트림 stall 감지용 — 디스코드는 말이 끝나면 침묵 패킷 전송을 곧
        # 끊으므로, "침묵 오디오 600ms"가 영영 안 채워져 발화가 확정되지 않을 수 있다
        self.last_chunk_time = time.time()

        # 부분 전사 한 줄 갱신 출력 상태 (\r 덮어쓰기)
        self._partial_line_len = 0

    def spawn(self, coro):
        """GC로 사라지지 않도록 강한 참조를 유지하며 태스크 생성"""
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    # ── 발화 게이트 ────────────────────────────────────────────────────
    # "지금 이 말에 답할 것인가". 예전에는 LLM에게 (pass)를 출력하게 했는데 실사용
    # 286발화에서 0건, 고정 케이스로 재도 0%였다 — 7천 자짜리 성격 규칙이 "감정 태그로
    # 시작"을 계속 이긴다. 성격을 벗겨 따로 묻는 방식은 반대로 답해야 할 말의 절반을
    # 삼켰다. 남은 것이 규칙인데, 가장 센 신호는 사람 수다:
    #   둘뿐이면 거의 다 나에게 하는 말 (실제 기록 489발화 중 삼킬 것 1건)
    #   여럿이면 대부분 자기들끼리 하는 말
    _SECOND_PERSON_RE = re.compile(r"(^|[^가-힣])(너|넌|널|니|네가|니가|당신|그대)(가|는|를|한테|에게|랑|와|과|도|만|의|희)?([^가-힣]|$)")
    _QUESTION_RE = re.compile(r"[?？]|((나요|까요|세요|을래|줄래|줘|니|냐|래|지)[.!?…]*$)")
    # 혼잣말·전화 통화 신호. "잠깐만"은 넣으면 안 된다 — 실제 기록에서 22번 중 21번이
    # 그대로 캐릭터에게 하는 말이었다 ("잠깐만, 지금 몇 시야?" 같은)
    _SOLO_RE = re.compile(r"(여보세요|여보세여|이게 왜 안|왜 안 되지|어디 갔지|어디 갔더라|"
                          r"여기가 아닌|이거 맞나|^어디 보자|^자 보자|^가만있어 보자|^(저|나) 말고)")

    # 모델 판정에 맡기면 안 되는 말 — 물어보지도 않고 답한다.
    # 이것 없이 판정만 쓰면 "17 곱하기 5가 왜 80이야?" 같은 질문까지 삼켰다 (오탐 18% → 8%)
    # "딸기가 좋아 수박이 좋아"처럼 물음표 없는 질문이 많다 — 아/봐도 넣는다.
    # 넘겨야 할 말 8개 중 이 어미로 끝나는 건 없어서 잃는 것이 없다
    #
    # 까·냐고는 "…해야 될까" 같은 질문이 판정으로 넘어가 묵살된 뒤에 넣었다. 맨 "까"는
    # "그러니까"를 통째로 끌고 오므로 앞의 "니"를 제외한다. "냐고"는 되물음이다.
    # 실제 기록 841줄에 대보니 새로 걸리는 11줄이 전부 진짜 질문이었고,
    # 넘겨야 할 21개 중 새로 걸리는 것은 0이었다
    _MUST_ANSWER_RE = re.compile(
        r"[?？]|((나요|까요|세요|을래|줄래|줘|니|냐|래|지|야|어|데|아|봐|(?<!니)까|냐고|나고)[.!?…]*$)")

    def must_answer(self, text: str) -> bool:
        """이름·2인칭·질문 — 판정 결과와 무관하게 답한다"""
        return bool(self._SECOND_PERSON_RE.search(text) or self._MUST_ANSWER_RE.search(text))

    def should_reply(self, text: str, wake: bool, engaged: bool) -> tuple:
        """(답할지, 이유). mode가 always면 늘 답하고, call이면 이름을 불러야 답한다"""
        if self.mode == "always":
            return True, "always"
        if wake:
            return True, "이름"
        if self.mode == "call":
            return False, "호출어 없음"
        # auto — 스스로 고른다
        body = text.strip()
        if self._SOLO_RE.search(body):
            return False, "혼잣말·통화"
        others = [m for m in self.channel_members if m and m != self.current_speaker]
        if len(others) >= 1:
            # 다른 사람을 부른 말이면 확실히 내 차례가 아니다
            for name in others:
                if re.search(re.escape(name) + r"(야|아|님|이)?[ ,]", body):
                    return False, f"{name}에게 한 말"
            if engaged:
                return True, "대화 중"
            if self._SECOND_PERSON_RE.search(body) and self._QUESTION_RE.search(body):
                return True, "나에게 물음"
            return False, "사람들끼리"
        return True, "둘뿐"

    def listening(self) -> bool:
        """사람이 부르지 않아도 말을 걸 수 있는 상태인가 (call 모드에서는 하지 않는다)"""
        return self.mode in ("auto", "always")

    async def check_verdict(self, task) -> bool:
        """나란히 돌린 판정 결과 — 늦으면 기다리지 않고 답한다"""
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.JUDGE_WAIT_SEC)
        except asyncio.TimeoutError:
            task.cancel()
            print("[WS] 발화 판정이 늦어 그냥 답합니다")
            return True
        except Exception as e:
            print(f"[WS] 발화 판정 오류 (답하는 쪽으로): {e}")
            return True

    def refresh_hotwords(self):
        """채널에 있는 사람 이름을 STT에 알려 준다.

        힌트가 없으면 한국어 닉네임이 계속 비슷한 소리의 낱말로 뭉개진다.
        닉네임 자체와, 사람들이 실제로 부르는 호칭(세 글자 이름의 뒤 두 글자)을 함께 준다"""
        if not self.name_hotwords:
            self.hotword_set = {NAME}
            return
        names = []
        for n in self.channel_members or []:
            n = (n or "").strip()
            if len(n) < 2:
                continue
            names.append(n)
            # 세 글자 이름은 성을 뗀 호칭을 실제로 쓴다. 짧은 만큼 소음이 그 단어로
            # 디코딩되기도 쉬우므로, 아래 hotword_set에 같이 넣어 소음 판정에도 쓰인다
            if len(n) >= 3:
                names.append(n[-2:])
        self.hotword_set = {NAME} | set(names)
        # 문서 제목 같은 긴 복합어는 STT에 주지 않는다. 넣어 봤더니 제목이 단독 발화로 10번 넘게
        # 나오고 핫워드 목록 전체를 읽는 환각까지 붙었다 — 긴 핫워드는 모호한 소리를 전부
        # 그쪽으로 끌고 가고 주변 문장 인식도 같이 흔든다. 제목이 뭉개지는 쪽이 낫다
        try:
            got = set_hotword_names(names)
            if got != getattr(self, "_hotwords_last", None):
                self._hotwords_last = got
                print(f"[STT] 이름 힌트: {got}")
        except Exception as e:
            print(f"[STT] 이름 힌트 갱신 실패: {e}")

    def log_spoke(self, kind: str, seq: int, **extra):
        """캐릭터가 먼저 꺼낸 말 — 무슨 말을 했는지까지 남긴다 (대화 탭이 이걸 그린다)"""
        reply, _, _ = extract_emotion_and_clean(getattr(self, "last_reply_text", "") or "")
        from llm import current_llm
        self.log_metrics({"event": "spoke", "kind": kind, "seq": seq,
                          "reply": reply[:300], "model": current_llm().get("model"),
                          "self_avatar": self.self_avatar, "people": self.human_count, **extra})

    # config: stt.timeout_sec / stt.queue_wait_sec
    STT_TIMEOUT_SEC = 20       # 이만큼 넘으면 이번 전사는 포기 (스레드는 계속 돈다)
    STT_QUEUE_WAIT_SEC = 3     # 앞 전사가 끝나기를 기다려 주는 시간

    def _stt_done(self, fut):
        """전사 스레드가 끝났다 — 멈춰 있었다면 복구를 알린다"""
        if self._stt_inflight is fut:
            self._stt_inflight = None
        if self.stt_stuck_since:
            stuck = time.time() - self.stt_stuck_since
            self.stt_stuck_since = None
            print(f"[WS] STT 복구 — 멈춰 있던 전사가 {stuck:.0f}초 만에 끝났습니다")
            self.log_metrics({"event": "stt_recovered", "stuck_s": round(stuck, 1)})

    async def run_stt(self, audio, kind: str = "final"):
        """Whisper 호출. 스레드 하나로 직렬화하고, 앞 호출이 안 끝났으면 건너뛴다.

        None을 돌려주면 이번 전사는 없던 것으로 친다 (20초씩 얼어붙는 것보다 낫다)."""
        if self._stt_inflight is not None and not self._stt_inflight.done():
            # 앞 전사가 곧 끝날 수도 있으니 잠깐은 기다린다 — 바로 버리면 말이 통째로 사라진다.
            # 그래도 안 끝나면 넘긴다. 20초씩 얼어붙는 것이 "먹통"의 정체였다
            t0 = time.time()
            try:
                await asyncio.wait_for(asyncio.shield(self._stt_inflight),
                                       timeout=self.STT_QUEUE_WAIT_SEC)
            except (asyncio.TimeoutError, Exception):
                pass
            if self._stt_inflight is not None and not self._stt_inflight.done():
                stuck = time.time() - (self.stt_stuck_since or t0)
                print(f"[WS] STT가 아직 돌고 있어 이번 {kind} 전사는 넘깁니다 ({stuck:.0f}초째)")
                self.log_metrics({"event": "stt_skipped", "kind": kind,
                                  "stuck_s": round(stuck, 1), "samples": int(len(audio))})
                return None
        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(self.stt_pool, transcribe, audio)
        self._stt_inflight = fut
        fut.add_done_callback(self._stt_done)
        try:
            # shield: 타임아웃이 퓨처를 취소해 완료 콜백이 먼저 불리지 않게
            return await asyncio.wait_for(asyncio.shield(fut), timeout=self.STT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            self.stt_stuck_since = time.time()
            print(f"[WS] ⚠ STT 시간 초과 ({self.STT_TIMEOUT_SEC}초) — 스레드는 계속 돌고 있습니다. "
                  f"끝날 때까지 새 전사는 건너뜁니다 ({len(audio)} samples)")
            self.log_metrics({"event": "stt_timeout", "kind": kind, "samples": int(len(audio)),
                              "sec": self.STT_TIMEOUT_SEC})
            return None

    _WORD_RE = re.compile(r"[가-힣A-Za-z]+")

    def looks_like_name_noise(self, text: str) -> bool:
        """아는 이름들로만 이루어졌고 두 개 이상이면 소음으로 본다.

        핫워드는 무음·소음에서도 그 단어를 뱉게 만든다 — "<캐릭터> <사람 이름>"이 실제로 여러 번 나왔다.
        캐릭터 이름 하나는 진짜 부르는 말이라 남긴다."""
        words = self._WORD_RE.findall(text or "")
        return len(words) >= 2 and all(w in self.hotword_set for w in words)

    def log_metrics(self, record: dict):
        """턴 단위 메트릭을 data/metrics.jsonl에 기록 (베이스라인/튜닝 측정용)"""
        try:
            record["ts"] = datetime.now().isoformat()
            with open(METRICS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def print_partial(self, text: str):
        """부분 전사를 같은 줄에 덮어쓰기 (\r) — 로그 도배 방지"""
        line = f"[듣는중] {text}"
        pad = max(0, self._partial_line_len - len(line))
        print("\r" + line + " " * pad, end="", flush=True)
        self._partial_line_len = len(line)

    def close_partial_line(self):
        """열린 부분 전사 줄을 개행으로 마감 (다음 로그가 이어붙지 않게)"""
        if self._partial_line_len:
            print(flush=True)
            self._partial_line_len = 0

    async def handle_connection(self, websocket: WebSocket):
        await websocket.accept()
        print("[WS] Client connected")

        # 오디오 전송 큐 (Future/Task를 저장)
        self.audio_task_queue = asyncio.Queue()
        self.sending_task = asyncio.create_task(self.send_audio_loop(websocket))
        self.stall_task = asyncio.create_task(self.stall_watchdog(websocket))
        self.memory_task = asyncio.create_task(self.memory_watchdog())
        self.initiative_task = asyncio.create_task(self.initiative_watchdog())
        self.manuscript_task = asyncio.create_task(self.manuscript_watchdog(websocket))
        self.websocket = websocket

        # GPT-SoVITS api_v2는 동시 요청을 잘 소화하지 못한다 — 3개를 한꺼번에 던지면 14자짜리 합성이
        # 1.1초에서 13~30초로 늘어 재생이 끊겼다. 순차로 돌리면 조각마다 1.1~1.5초, 끊김 0곳 (실측 09-24)
        self.last_reply_text = ""
        self.turn_seq = 0          # ttfa 이벤트와 턴 기록을 묶는 번호
        self.avatars = {}          # 이름 → 디스코드 프로필 사진 주소 (대화 화면용)
        self.self_avatar = None    # 봇 자신의 것
        # 문장을 어디까지 쪼개 TTS로 보낼지 — 이음매와 첫 음성 지연의 맞바꿈
        #   off / sentence(기본) / comma(문장 + 쉼표) / aggressive(연결어미·글자 수까지)
        # 쉼표는 원래 숨 쉬는 자리라 문장 중간을 자르는 것과 다르다. 긴 첫 문장의 최악을 줄인다
        # (실측: 첫 조각 중앙 51자·최대 92자 → 쉼표까지 쓰면 43자·51자, 합성 5.7초 → 3.4초)
        self.chunking = (load_config().get("tts", {}).get("chunking") or "sentence").lower()
        self.tts_semaphore = asyncio.Semaphore(
            max(1, int(load_config().get("tts", {}).get("concurrency", 1))))

        try:
            while True:
                message = await websocket.receive()
                
                if "bytes" in message:
                    await self.process_audio_chunk(message["bytes"], websocket)
                elif "text" in message:
                    await self.process_text_message(message["text"], websocket)
                    
        except WebSocketDisconnect:
            print("[WS] Client disconnected")
        except RuntimeError as e:
            # starlette: disconnect 후 receive() 호출 시 RuntimeError 발생 — 정상 종료로 처리
            print(f"[WS] Connection closed: {e}")
        except Exception as e:
            print(f"[WS] Error: {e}")
            traceback.print_exc()
        finally:
            if self.sending_task:
                self.sending_task.cancel()
            if self.stall_task:
                self.stall_task.cancel()
            if self.memory_task:
                self.memory_task.cancel()
            if self.initiative_task:
                self.initiative_task.cancel()
            if self.manuscript_task:
                self.manuscript_task.cancel()
            self.websocket = None
            # 봇이 끊겨도 그때까지의 대화는 기억으로 정리해 둔다
            self.spawn(self.flush_memory("disconnect"))

    # 패킷이 이만큼 끊기면 발화를 확정한다. 말이 아직 안 끝난 것처럼 들리면 더 기다린다 —
    # 실측(646턴): stall이 전체의 48%로 가장 흔한데 끝점까지 0.92초로 가장 느렸고(디스코드가
    # 말끝 뒤 0.5초쯤 더 보내다 끊는다), 중간에 숨을 고르면 그 순간 문장이 잘렸다
    STALL_SEC = 0.4
    STALL_SEC_UNFINISHED = 1.0

    def stall_limit(self) -> float:
        """지금까지 들린 말이 이어질 것 같으면 더 기다린다 ("아니 근데", "…들어간 이후에", 이름만)"""
        if (self.partial_text and self.partial_utt_id == self.vad.utterance_id
                and self.looks_unfinished(self.partial_text)):
            return self.STALL_SEC_UNFINISHED
        return self.STALL_SEC

    async def stall_watchdog(self, websocket: WebSocket):
        """
        녹음 중인데 오디오 청크가 STALL_SEC 동안 안 들어오면 발화를 강제 확정.
        디스코드가 발화 종료 후 침묵 전송을 끊는 경우, 침묵 프레임 기반 확정(600ms)이
        다음 소리가 날 때까지 일어나지 않는 문제를 막는다.
        """
        while True:
            await asyncio.sleep(0.1)
            if self.vad.recording and time.time() - self.last_chunk_time > self.stall_limit():
                segment = self.vad.force_endpoint("stall")
                if segment is not None:
                    self.close_partial_line()
                    print("[WS] Stall endpoint — 오디오 스트림 중단, 발화 강제 확정")
                    self.spawn(self.process_speech(segment, websocket,
                                                   self.vad.last_utterance_id,
                                                   self.vad.last_speech_end_frame,
                                                   endpoint="stall"))
                    self.partial_text = None  # 다음 발화의 판단에 쓰이지 않게

    async def send_audio_loop(self, websocket: WebSocket):
        """
        순서대로 완료된 TTS 오디오를 전송하는 루프
        """
        print("[WS] Audio sending loop started")
        while True:
            # 큐에서 TTS 태스크 가져오기 (순서대로)
            # (text, tts_task, start_time) 튜플
            item = await self.audio_task_queue.get()

            if item is None: # 종료 신호
                self.audio_task_queue.task_done()
                break

            text, tts_task, start_time, style, seq = item
            
            try:
                # 태스크 완료 대기
                audio_bytes = await tts_task
                
                if audio_bytes:
                    # 레이턴시 측정 (첫 번째 청크인 경우)
                    if start_time:
                        latency_ms = (time.time() - start_time) * 1000
                        print(f"[WS] ⚡ Response Latency: {latency_ms:.0f}ms")
                        await websocket.send_json({"type": "latency", "duration": latency_ms})
                                # 턴 기록보다 먼저 나가므로 번호로 묶는다 (대화 화면이 실제 첫 소리를 쓰도록).
                        # 번호는 조각을 큐에 넣을 때의 것이다 — 보낼 때 읽으면 그 사이 인사·먼저 말 걸기가
                        # 번호를 올려 엉뚱한 턴에 붙는다
                        self.log_metrics({"event": "ttfa", "ms": round(latency_ms), "seq": seq})

                    # 텍스트 전송 (자막) - 오디오 전송 직전에 보냄 (싱크 맞춤)
                    cleaned_text, _, _ = extract_emotion_and_clean(text)
                    await websocket.send_json({
                        "type": "ai_response",
                        "text": cleaned_text,
                        "style": style
                    })
                    
                    # 오디오 전송
                    await websocket.send_bytes(audio_bytes)
                    print(f"[WS] Sent audio chunk for '{cleaned_text}' ({len(audio_bytes)} bytes)")
                else:
                    print(f"[WS] TTS failed for: {text}")
                    
            except asyncio.CancelledError:
                print("[WS] Sending task cancelled")
                raise
            except Exception as e:
                print(f"[WS] Error sending audio: {e}")
            finally:
                self.audio_task_queue.task_done()

    async def process_text_message(self, text: str, websocket: WebSocket):
        msg_type = None
        try:
            data = json.loads(text)
            msg_type = data.get("type")
            
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            
            elif msg_type == "stop_audio":
                print("[WS] Received stop_audio (Barge-in)")
                
                # 1. 현재 생성 중인 LLM 스트림 취소
                if self.processing_task:
                    self.processing_task.cancel()

                # 2. 대기 중인 오디오 전송 큐 비우기 & TTS 태스크 취소.
                # 조각 모양이 바뀌면(seq 추가) 여기서 ValueError가 나고, 그게 바깥
                # handle_connection까지 올라가 세션을 통째로 내렸다 (전송·감시 태스크 취소,
                # flush_memory("disconnect")) — 끼어들 때마다 연결이 끊긴 것이 이것이다.
                # 모양에 의존하지 않게 인덱스로 꺼내고, 어떤 예외도 비우기를 멈추지 못하게 한다
                while not self.audio_task_queue.empty():
                    try:
                        item = self.audio_task_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        if item:
                            item[1].cancel()   # 실행 중인 TTS 취소 (안 하면 세마포어를 계속 쥔다)
                    except Exception as e:
                        print(f"[WS] 바지인 — 큐 조각 취소 실패: {e}")
                    finally:
                        self.audio_task_queue.task_done()
                
                # 3. 전송 루프는 유지 (다음 대화를 위해)
                
            elif msg_type == "user_speech":
                pass
                
            elif msg_type == "text_input":
                text = data.get("text")
                print(f"[WS] Received text input: {text}")
                # 텍스트 입력은 별도 처리 (여기서는 단순화)
                self.processing_task = self.spawn(self.process_text_input(text, websocket))
                
            elif msg_type == "set_mode":
                prev_mode, self.mode = self.mode, data.get("mode") or "auto"
                print(f"[WS] Listening mode set: {self.mode}")
                # 듣기를 막 켰으면 채널에 있던 사람들에게 인사 — 방금 들어왔으면 모두에게,
                # 이미 있던 채널에서 듣기만 켠 거면 오랜만인 사람에게만
                if self.listening() and prev_mode not in ("auto", "always") and self.channel_members:
                    arrived, self.arrival_pending = self.arrival_pending, False
                    self.spawn(self.maybe_greet([(n, None) for n in self.channel_members],
                                                delay=1.5, arrival=arrived))

            elif msg_type == "speaker":
                self.current_speaker = data.get("user_name")
                self.current_speaker_id = data.get("user_id")
                memory_manager.note_person(self.current_speaker_id, self.current_speaker)
                self.channel_members = data.get("members") or []
                self.human_count = data.get("human_count") or 1
                self.avatars.update(data.get("avatars") or {})
                if data.get("avatar") and self.current_speaker:
                    self.avatars[self.current_speaker] = data["avatar"]
                self.refresh_hotwords()
                self.guild_name = data.get("guild_name") or self.guild_name
                self.channel_name = data.get("channel_name") or self.channel_name

            elif msg_type == "voice_event":
                self.handle_voice_event(data)

            elif msg_type == "vad_reset":
                print("[WS] Received VAD reset request")
                self.vad.reset()
                
        except json.JSONDecodeError:
            pass
        except Exception as e:
            # 제어 메시지 하나를 처리하다 터진 것이 세션을 끝낼 이유는 없다 — 바깥
            # handle_connection의 except Exception은 전송·감시 태스크를 모두 취소하고
            # 에피소드를 "disconnect"로 닫는다. 여기서 막아 두고 로그만 남긴다
            print(f"[WS] 제어 메시지 처리 실패 ({msg_type}): {e}")
            traceback.print_exc()

    def handle_voice_event(self, data: dict):
        """봇·다른 사람의 음성 채널 입장·퇴장 — 참여자 명단을 바로 갱신하고, 상황 설명과 파일에 남긴다"""
        event = data.get("event")
        guild, channel, who = data.get("guild_name"), data.get("channel_name"), data.get("user_name")
        if data.get("members") is not None:
            self.channel_members = data["members"]
            self.human_count = data.get("human_count") or len(self.channel_members) or 1
            self.avatars.update(data.get("avatars") or {})
            self.self_avatar = data.get("self_avatar") or self.self_avatar
            self.refresh_hotwords()
        now = datetime.now()
        if data.get("listening"):
            self.mode = data["listening"]
        if event in ("self_join", "sync"):
            self.guild_name, self.channel_name = guild, channel
        if event == "self_join":
            self.voice_events.clear()
            self.last_leave_note = self.last_self_leave_note(now)
            self.joined_at = now
            self.recent_leaves.clear()
            # 봇이 들어올 때 이미 있던 사람 — 언제 왔는지는 알 수 없다
            self.presence = {m: {"since": None, "joins": 0} for m in self.channel_members if m != who}
            if who:  # 같은 순간 들어온 사람 (보통 봇을 부른 사람)
                self.presence[who] = {"since": now, "joins": 1}
            self.arrival_pending = True
        elif event == "sync":
            self.sync_presence()
        elif event == "self_leave":
            self.joined_at = None
            self.presence = {}
        elif event == "member_join" and who:
            p = self.presence.get(who) or {"since": None, "joins": 0}
            left_before = [t for n, t in self.recent_leaves if n == who]
            self.presence[who] = {"since": now, "joins": p["joins"] + 1,
                                  "left_at": left_before[-1] if left_before else None}
        elif event == "member_leave" and who:
            self.presence.pop(who, None)
            self.recent_leaves.append((who, now))
        texts = {
            "self_join": f"{NAME}(너)가 {guild} 서버의 '{channel}' 음성 채널에 들어옴",
            "self_leave": f"{NAME}(너)가 '{channel}' 음성 채널에서 나감",
            "member_join": f"{who} 님이 채널에 들어옴",
            "member_leave": f"{who} 님이 채널에서 나감",
        }
        if event in texts:
            self.voice_events.append((now, texts[event]))
            print(f"[Voice] {texts[event]}")
            self.log_voice_event({"event": event, "guild": guild, "channel": channel, "user": who})
        if event == "self_leave":
            self.spawn(self.flush_memory("self_leave"))
        elif event == "self_join" and self.listening() and self.channel_members:
            # 이미 듣는 중에 채널만 옮겨 들어온 경우 — set_mode가 오지 않으므로 여기서 인사한다
            self.arrival_pending = False
            self.spawn(self.maybe_greet([(n, None) for n in self.channel_members], delay=2.5, arrival=True))
        elif event == "member_join" and who:
            memory_manager.note_person(data.get("user_id"), who)
            # 들어오자마자 말하면 상대 쪽 소리가 아직 안 붙어 첫마디가 잘린다
            self.spawn(self.maybe_greet([(who, data.get("user_id"))], delay=2.5, rejoin=True))

    def log_voice_event(self, record: dict):
        try:
            record["ts"] = datetime.now().isoformat(timespec="seconds")
            with open(VOICE_EVENTS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def last_self_leave_note(now: datetime):
        """직전에 채널을 나간 게 언제인지 — 봇·서버를 껐다 켜도 알 수 있게 파일에서 찾는다"""
        try:
            with open(VOICE_EVENTS_PATH, encoding="utf-8") as f:
                leaves = [json.loads(l) for l in f if '"self_leave"' in l]
        except (OSError, json.JSONDecodeError):
            return None
        if not leaves:
            return None
        gap = (now - datetime.fromisoformat(leaves[-1]["ts"])).total_seconds()
        return f"지난번에 음성 채널을 나간 건 {ago_text(gap)} 전"

    def sync_presence(self):
        """명단(channel_members)에 맞춰 표를 맞춘다 — 봇 재연결·서버 재시작 뒤, 또는 이벤트를 놓쳤을 때"""
        for m in self.channel_members:
            self.presence.setdefault(m, {"since": None, "joins": 0})
        for m in [m for m in self.presence if m not in self.channel_members]:
            self.presence.pop(m)

    @staticmethod
    def clock(t: datetime) -> str:
        return f"{'오전' if t.hour < 12 else '오후'} {t.hour % 12 or 12}:{t.minute:02d}"

    def situation_context_lines(self) -> list:
        """
        [현재 상황]: 위치, 봇이 들어온 시각, 채널에 있는 사람별 표, 최근에 나간 사람.
        첫 줄에 "네가 직접 보고 있는 정보"라고 못박는다 — 없으면 모델이(원본 gemma4도) 이 정보를 받고도
        "접속 기록은 확인할 수 없어요"라고 했다 (안내 한 줄로 인원 질문 0/2 → 3/3)
        """
        lines = ["(아래는 디스코드 봇이 실시간으로 알려 주는, 네가 지금 직접 보고 있는 정보다. "
                 "누가 있는지, 언제 들어오고 나갔는지 물으면 이걸 보고 그대로 답한다. "
                 "확인할 수 없다거나 기록이 없다고 하지 않는다.)"]
        # 지금 무엇을 하는 중인지 (글쓰기면 한 줄). 상태를 프롬프트에 넣어야 그녀도 안다
        now = self.activity.situation_line()
        if now:
            lines.append(now)
        # "이름을 숨기지 말라"는 안내를 여기에 더해 봤지만 이름을 말한 답이 4/6 → 3/6으로, 차이가 없었다.
        # 작은 모델은 "여기 누구누구 있어?"에 개인정보라며 답을 거부하기도 한다 — 프롬프트로는 안 고쳐진다
        if self.guild_name and self.channel_name:
            lines.append(f"지금 있는 곳: {self.guild_name} 서버의 '{self.channel_name}' 음성 채널")
        now = datetime.now()
        if self.joined_at:
            lines.append(f"너({NAME})는 {self.clock(self.joined_at)}에 들어와 "
                         f"{ago_text((now - self.joined_at).total_seconds())}째 있다")
        if self.channel_members:
            self.sync_presence()
            # "사람 1명"만 적었더니 "혼자 계세요" → "둘이지?" → 우기다 사과하고 대화를 끝냈다.
            # "…에 나갔다가 다시 들어옴"도 나간 상태로 읽었다 → 캐릭터까지 세고, 사람마다 "지금 있음"을 먼저 쓴다
            n = len(self.channel_members)
            lines.append(f"지금 채널에 모두 {n + 1}명 — 너({NAME})와 사람 {n}명")
            for name in self.channel_members:
                p = self.presence.get(name, {"since": None, "joins": 0})
                if p["since"] is None:
                    lines.append(f"- {name}: 지금 채널에 있음. 네가 들어오기 전부터 있었다")
                else:
                    how = "다시 들어왔다" if p.get("left_at") or p["joins"] > 1 else "들어왔다"
                    count = f" (네가 온 뒤로 {p['joins']}번째 들어옴)" if p["joins"] > 1 else ""
                    lines.append(f"- {name}: 지금 채널에 있음. {self.clock(p['since'])}에 {how}, "
                                 f"{ago_text((now - p['since']).total_seconds())}째{count}")
        left = [f"{n} ({self.clock(t)})" for n, t in self.recent_leaves
                if n not in self.channel_members and (now - t).total_seconds() < 7200]
        if left:
            lines.append("최근에 나간 사람: " + ", ".join(left))
        if self.last_leave_note and self.joined_at and (now - self.joined_at).total_seconds() < 1800:
            lines.append(self.last_leave_note)  # 들어온 지 얼마 안 됐을 때만 — 오랜만인지 알 수 있게
        return lines

    async def process_audio_chunk(self, chunk_bytes: bytes, websocket: WebSocket):
        try:
            audio_int16_48k_stereo = np.frombuffer(chunk_bytes, dtype=np.int16)
            if len(audio_int16_48k_stereo) % 2 != 0:
                audio_int16_48k_stereo = audio_int16_48k_stereo[:-1]
            audio_stereo = audio_int16_48k_stereo.reshape(-1, 2)
            audio_mono = audio_stereo.mean(axis=1)
            # 안티앨리어싱 로우패스 후 3:1 데시메이션 (필터 상태를 청크 간 유지)
            filtered, self.aa_zi = scipy.signal.lfilter(self.aa_b, self.aa_a, audio_mono, zi=self.aa_zi)
            audio_int16_16k = filtered[::3].astype(np.int16)

            self.last_chunk_time = time.time()
            chunk_16k_bytes = audio_int16_16k.tobytes()
            speech_segment = self.vad.process_chunk(chunk_16k_bytes)

            if speech_segment is not None:
                self.close_partial_line()
                print(f"[WS] Speech detected ({len(speech_segment)} samples)")
                # 여기서 이전 턴을 취소하지 않는다 — 잡음·빈 세그먼트가 생성 중인
                # 응답을 죽이는 것을 막기 위해, STT로 유효한 발화가 확정된 뒤 교체한다
                self.spawn(self.process_speech(speech_segment, websocket,
                                               self.vad.last_utterance_id,
                                               self.vad.last_speech_end_frame,
                                               endpoint=self.vad.last_endpoint))
            elif self.vad.recording:
                self.maybe_run_partial_stt(websocket)

        except Exception as e:
            print(f"[Audio] Error processing chunk: {e}")

    def maybe_prefetch(self, partial_text: str, utt_id: int):
        """사용자가 아직 말하는 중에 지금까지 들린 말로 프롬프트를 미리 먹여 둔다.

        최종 프롬프트와 앞부분(성격·기억·기록)이 같아서 prefill이 캐시에 남는다 —
        실측: 첫 조각 1.17s → 0.84s. 데우는 데 드는 0.44s는 사용자가 말하는 동안이라
        체감에 들어가지 않는다. 발화마다 한 번만, 대답을 만드는 중이면 하지 않는다 (GPU 경합)"""
        if not self.prefetch_enabled or self._prefetch_utt == utt_id or self.busy():
            return
        if len(partial_text.strip()) < self.PREFETCH_MIN_CHARS:
            return
        self._prefetch_utt = utt_id
        speaker = self.current_speaker
        lines = self.situation_context_lines()
        if speaker:
            lines.append(f"방금 말한 사람: {speaker}")
        self.spawn(prefetch_prompt(partial_text, speaker=speaker, situation="\n".join(lines),
                                   speaker_id=self.current_speaker_id))

    def maybe_run_partial_stt(self, websocket: WebSocket):
        """발화 중 롤링 부분 전사 스케줄링 — 평시 ~0.5초 간격, 침묵 시작 시엔 즉시"""
        if self.partial_task and not self.partial_task.done():
            return  # 한 번에 하나만 (GPU 직렬화)
        snap = self.vad.snapshot()
        if snap is None:
            return
        utt_id, audio, frames_count, speech_end_frame = snap
        if frames_count < 24:  # 0.7초 미만 버퍼는 전사 가치가 낮음
            return
        same_utt = (self._partial_sched_utt == utt_id)
        grown = frames_count - (self._partial_sched_frames if same_utt else 0)
        # 침묵이 시작됐는데 마지막 패스가 발화 끝(+여유 3프레임)을 못 덮었으면
        # 캐던스를 무시하고 즉시 — 조기 확정과 발화 종료 시 재사용의 전제 조건
        silence_kick = (self.vad.silence_run >= 5 and
                        (not same_utt or self._partial_sched_frames < speech_end_frame + 3))
        if grown < 17 and not silence_kick:
            return
        self._partial_sched_utt = utt_id
        self._partial_sched_frames = frames_count
        self.partial_task = self.spawn(
            self.run_partial_stt(utt_id, audio, frames_count, websocket))

    async def run_partial_stt(self, utt_id: int, audio: np.ndarray,
                              frames_count: int, websocket: WebSocket):
        """누적 버퍼 부분 전사 → 캐시. 침묵 중 종결부호로 끝나면 발화 조기 확정."""
        try:
            loop = asyncio.get_event_loop()
            async with self.stt_lock:
                # 락 대기 중 발화가 이미 끝났으면 스냅샷이 구식 — 최종 STT에 양보
                if not self.vad.recording or self.vad.utterance_id != utt_id:
                    return
                text = await self.run_stt(audio, kind="partial")
                if text is None:
                    return
            self.partial_text = text
            self.partial_stats = stt_last_stats()
            self.partial_utt_id = utt_id
            self.partial_covered_frames = frames_count
            if text:
                self.print_partial(text)
                try:
                    await websocket.send_json({"type": "user_partial", "text": text})
                except Exception:
                    pass
                self.maybe_prefetch(text, utt_id)
            # 시맨틱 조기 확정: 여전히 같은 발화의 침묵이 이어지는 중이고(150ms+),
            # 이 패스가 음성 구간 전체를 덮었고, 문장이 종결부호로 끝나면
            # 600ms 침묵 확정을 기다리지 않고 즉시 턴을 시작한다
            if (text and self.vad.recording and self.vad.utterance_id == utt_id
                    and self.vad.silence_run >= 5
                    and self.vad.speech_end_frame <= frames_count
                    and re.search(r"[.!?]\s*$", text)):
                segment = self.vad.force_endpoint("early")
                if segment is not None:
                    self.close_partial_line()
                    print("[WS] Early endpoint — 침묵+문장 종결, 발화 조기 확정")
                    self.spawn(self.process_speech(segment, websocket,
                                                   self.vad.last_utterance_id,
                                                   self.vad.last_speech_end_frame,
                                                   endpoint="early"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[WS] Partial STT error: {e}")

    async def process_speech(self, audio_np: np.ndarray, websocket: WebSocket,
                             utt_id: int = None, speech_end_frame: int = 0,
                             endpoint: str = "silence"):
        start_time = time.time()
        # 마지막 음성 프레임 → 턴 시작까지의 벽시계 지연 (확정 방식별 비교용)
        endpoint_s = max(0.0, start_time - getattr(self.vad, "last_speech_wall", start_time))
        self.turn_seq += 1
        seq = self.turn_seq
        self.close_partial_line()

        try:
            # 1. STT (직렬화 — 동시 실행 시 GPU 경합으로 전부 느려짐)
            await websocket.send_json({"type": "status", "message": "👂 듣는 중 (STT)..."})
            stt_start = time.time()
            stt_wait = 0.0
            stt_reused = False
            loop = asyncio.get_event_loop()

            # 이 발화의 마지막 롤링 패스가 아직 진행 중이면 완료를 기다린다
            # (보통 침묵 시작 시점에 시작된 패스 — 결과를 그대로 재사용 가능)
            if self.partial_task and not self.partial_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self.partial_task), timeout=20)
                except Exception:
                    pass

            # 롤링 부분 전사가 이 발화의 음성 구간을 전부 덮었으면 재사용 (STT ≈ 0초)
            if (utt_id is not None and self.partial_utt_id == utt_id
                    and self.partial_covered_frames >= speech_end_frame
                    and self.partial_text):
                text = self.partial_text
                stt_reused = True
            else:
                async with self.stt_lock:
                    stt_wait = time.time() - stt_start  # 락 대기 (앞선 세그먼트의 STT가 끝나길 기다린 시간)
                    text = await self.run_stt(audio_np, kind="final")
                    if text is None:
                        await websocket.send_json(
                            {"type": "status", "message": "⚠️ 음성 인식이 밀려 이번 말은 넘겼어요"})
                        return
            stt_duration = time.time() - stt_start
            stt_stats = (self.partial_stats if stt_reused else stt_last_stats()) or {}
            if stt_wait > 0.3:
                print(f"[WS] STT lock wait: {stt_wait:.2f}s (앞선 세그먼트 처리 대기)")
            print(f"[WS] STT Duration: {stt_duration:.2f}s"
                  + (" (partial 재사용)" if stt_reused else "")
                  + f" | endpoint={endpoint} +{endpoint_s:.2f}s")
            
            if not text or not text.strip():
                print("[WS] STT: No text")
                await websocket.send_json({"type": "status", "message": "❌ 아무 말도 안 들렸어요"})
                return
            
            # 이름 보정 (캐릭터 팩의 stt_fixes)
            for wrong, right in self.stt_fixes.items():
                text = text.replace(wrong, right)

            # 무시 문구 필터 (배경 소음·방송 환각)
            text_norm = text.lower().strip()
            if text_norm in self.ignore_phrases or any(p in text_norm for p in self.ignore_partial):
                print(f"[WS] Ignored phrase: {text}")
                return
            if self.looks_like_name_noise(text):
                print(f"[WS] 이름만 늘어놓은 전사 — 소음으로 보고 버림: {text}")
                self.log_metrics({"event": "name_noise", "text": text[:40], **stt_last_stats()})
                return
            # 문서 이야기만 하고 고치지는 않을 때는 문서가 가만히 있으므로, 말에서도 상태를 받는다
            self.note_activity(self.activity.heard(text))
            # 이름 하나뿐인 전사는 진짜 호출일 수도, 소음이 둔갑한 것일 수도 있다.
            # 지금은 버리지 않고 확신도만 남긴다 — 둘이 숫자로 갈리는지 보고 문턱을 정한다
            if self._WORD_RE.findall(text) and all(
                    w in self.hotword_set for w in self._WORD_RE.findall(text)):
                self.log_metrics({"event": "hotword_only", "text": text[:40], **stt_last_stats()})

            wake = any(w in text_norm for w in self.wake_words)

            # 끊긴 발화 복원: 빠른 확정(stall 0.4초·조기 확정)은 문장 중간의 짧은 멈춤에서도
            # 발화를 자른다. 앞조각의 턴이 아직 생성 중일 때 뒷조각이 오면, 앞 턴을 취소만 하지
            # 않고 두 텍스트를 이어붙여 한 문장으로 처리한다 (앞조각은 아직 기록에 저장 전)
            me = asyncio.current_task()
            prev_task = self.processing_task
            inflight = prev_task is not None and not prev_task.done() and prev_task is not me
            if inflight and self.inflight_text:
                text = f"{self.inflight_text} {text}"
                wake = wake or self.inflight_wake
                lps = [d.get("avg_logprob") for d in (stt_stats, self.inflight_stt or {})
                       if d.get("avg_logprob") is not None]
                if lps:
                    stt_stats = {**stt_stats, "avg_logprob": min(lps)}
                print(f"[WS] 끊긴 발화 병합 (이전 턴 취소): {text}")

            # 답할 차례인지 먼저 정한다. 답하지 않는 말도 기록에는 남겨,
            # 나중에 불렀을 때 "방금 한 얘기"를 알 수 있게 한다
            engaged_now = bool(self.current_speaker) and self.last_reply_speaker == self.current_speaker \
                and (time.time() - self.last_reply_time) < self.ENGAGED_SEC
            reply, why = self.should_reply(text, wake, engaged_now)
            if not reply:
                # 대답은 안 해도 방은 조용하지 않다 — 이걸 안 갱신하면 사람들끼리 대화하는 중에
                # "아무도 말하지 않는다"고 보고 먼저 말을 걸어 버린다.
                # 다만 initiative_count는 그대로다 (내 말에 대답이 온 게 아니다)
                self.last_activity = time.time()
                print(f"[WS] 듣기만 함 ({why}): {text}")
                self.log_metrics({"event": "gate", "replied": False, "why": why, "mode": self.mode,
                                  "people": self.human_count, "chars": len(text), "text": text[:60],
                                  "speaker": self.current_speaker,
                                  "avatar": self.avatars.get(self.current_speaker)})
                content = f"[{self.current_speaker}] {text}" if self.current_speaker else text
                await loop.run_in_executor(None, save_to_history, "user", content, self.current_speaker_id,
                                           stt_stats)
                await websocket.send_json({"type": "status", "message": f"🤫 듣는 중 ({why})"})
                return
            # 규칙이 답하라고 해도 auto에서는 한 번 더 묻는다 — 단, 이름·2인칭·질문이면 묻지 않는다.
            # 둘뿐인 대화에서도 혼잣말·노래·딴 사람에게 한 말을 넘기려면 이 경로가 필요하다.
            # 대답 생성과 나란히 돌아 레이턴시를 늘리지 않는다 — 첫 음성 직전에 결과를 본다
            verdict_task = None
            if self.judge_on and self.mode == "auto" and not wake and not self.must_answer(text):
                # 이름을 prev로 쓰면 안 된다 — 위에서 취소할 앞 턴을 담아 둔 이름이다.
                # 실제로 덮어써서 prev.cancel()이 AttributeError로 죽었고, 앞 턴이 취소되지 않아
                # 뒷말을 한 뒤에 앞말의 대답이 흘러나왔다
                recent_lines = [m.get("content", "") for m in load_conversation_history()[-3:]]
                verdict_task = asyncio.create_task(
                    llm_judge(f"[{self.current_speaker}] {text}" if self.current_speaker else text,
                              recent_lines, self.human_count))
            self.log_metrics({"event": "gate", "replied": True, "why": why,
                              "mode": self.mode, "people": self.human_count, "chars": len(text)})

            # 유효한 발화 확정 — 이 시점에야 이전 턴을 대체(취소)한다
            if inflight:
                prev_task.cancel()
                print("[WS] 이전 턴 취소 (새 발화로 대체)")
            self.processing_task = me
            self.last_activity, self.initiative_count = time.time(), 0  # 사람이 말했다 — 먼저 말 걸기 횟수 초기화
            self.inflight_text, self.inflight_wake, self.inflight_stt = text, wake, stt_stats

            print(f"[WS] User: {text}")
            await websocket.send_json({"type": "user_transcription", "text": text})

            # 이어질 말로 끝난 조각("아니 근데", "…들어간 이후에", 이름만 부름)은 바로 답하지 않고
            # 잠깐 기다린다. 그 사이 사용자가 이어 말하면 그 발화가 이 턴을 취소하고 위에서 이어붙인다.
            # 대답이 빨리 끝나 버리면 병합이 안 돼서 조각마다 따로 답하던 문제
            if self.looks_unfinished(text):
                print(f"[WS] 이어질 말로 보여 {self.FRAGMENT_HOLD_SEC}초 기다림: {text}")
                t0 = time.time()
                while (time.time() - t0 < self.FRAGMENT_HOLD_SEC
                       or (self.vad.recording and time.time() - t0 < 8)):
                    await asyncio.sleep(0.1)

            # 2. 맥락 구성 (답할지는 should_reply()에서 이미 정했다)
            speaker = self.current_speaker
            speaker_id = self.current_speaker_id
            engaged = engaged_now

            if speaker and speaker not in self.channel_members:
                # 명단이 늦게 와도 지금 말하는 사람은 채널에 있다 (들어오자마자 "나가신 상태"라고 한 적이 있다)
                self.channel_members = self.channel_members + [speaker]
            situation_lines = self.situation_context_lines()
            if speaker:
                situation_lines.append(f"방금 말한 사람: {speaker}")
            if engaged:
                situation_lines.append("이 사람은 방금까지 너와 대화하던 상대다. 자연스럽게 이어가라.")
            situation = "\n".join(situation_lines) if situation_lines else None

            # 3. LLM & TTS Streaming
            await websocket.send_json({"type": "status", "message": "🧠 생각하는 중 (LLM)..."})
            llm_start = time.time()
            spoke = await self.stream_llm_tts(text, websocket, start_time, speaker=speaker,
                                              situation=situation, speaker_id=speaker_id,
                                              verdict_task=verdict_task, stt=stt_stats)
            if not spoke:
                return          # 판정이 접었다 — 기록은 게이트 쪽에서 이미 남겼다
            llm_duration = time.time() - llm_start
            print(f"[WS] LLM+TTS Stream Duration: {llm_duration:.2f}s")
            
            total_time = time.time() - start_time
            print(f"[WS] Total processing time: {total_time:.2f}s")
            reply_clean, _, _ = extract_emotion_and_clean(getattr(self, "last_reply_text", "") or "")
            from llm import current_llm
            from tts import current_tts
            self.log_metrics({
                "event": "turn",
                "seq": seq,
                # 모델을 갈아 가며 테스트하니 어떤 것으로 답한 줄인지 같이 남긴다
                "model": current_llm().get("model"),
                "tts": current_tts().get("provider"),
                "chunking": self.chunking,
                "speaker": speaker,
                "avatar": self.avatars.get(speaker),
                "self_avatar": self.self_avatar,
                "text": text[:160],
                "reply": reply_clean[:300],
                "endpoint": endpoint,
                "endpoint_s": round(endpoint_s, 3),
                "stt_s": round(stt_duration, 3),
                "stt_wait_s": round(stt_wait, 3),
                "stt_reused": stt_reused,
                # 잘못 들은 줄을 걸러 낼 문턱을 데이터로 정하려고 모든 턴에 남긴다
                "stt_lp": stt_stats.get("avg_logprob"),
                "stt_nsp": stt_stats.get("no_speech_prob"),
                "llm_tts_s": round(llm_duration, 3),
                # 첫 소리 = 첫 조각이 나올 때까지 LLM이 쓴 시간 + 그 조각의 합성 시간
                "llm_first_s": round(self.turn_first_queued_at - llm_start, 3) if self.turn_first_queued_at else None,
                "tts_first_s": round(self.turn_first_tts_s, 3) if self.turn_first_tts_s else None,
                "first_chars": self.turn_first_chars,
                "total_s": round(total_time, 3),
            })
            
        except asyncio.CancelledError:
            print("[WS] Speech processing cancelled")
            await websocket.send_json({"type": "status", "message": "🛑 취소됨"})
        except Exception as e:
            print(f"[WS] Processing error: {e}")
            traceback.print_exc()
            await websocket.send_json({"type": "status", "message": f"⚠️ 오류: {e}"})

    async def process_text_input(self, text: str, websocket: WebSocket):
        """타이핑으로 들어온 말 (!talk · 컨트롤 앱 대화 탭의 /talk).

        음성과 같은 stream_llm_tts를 타므로 기록·기억에는 똑같이 남는다. turn 기록도 남겨야
        대화 탭에 뜬다. 말끝·STT가 없는 턴이라 그 칸은 비운다."""
        start_time = time.time()
        self.turn_seq += 1
        seq = self.turn_seq
        self.turn_first_queued_at = None
        self.turn_first_tts_s = None
        self.turn_first_chars = None
        try:
            print(f"[WS] Processing text input: {text}")
            await websocket.send_json({"type": "status", "message": "🧠 생각하는 중 (LLM)..."})
            llm_start = time.time()
            spoke = await self.stream_llm_tts(text, websocket, start_time)
            print(f"[WS] Total processing time: {time.time() - start_time:.2f}s")
            if spoke is False:
                return
            reply_clean, _, _ = extract_emotion_and_clean(getattr(self, "last_reply_text", "") or "")
            from llm import current_llm
            from tts import current_tts
            speaker = self.current_speaker or "타이핑"
            self.log_metrics({
                "event": "turn",
                "seq": seq,
                "model": current_llm().get("model"),
                "tts": current_tts().get("provider"),
                "chunking": self.chunking,
                "speaker": speaker,
                "avatar": self.avatars.get(speaker),
                "self_avatar": self.self_avatar,
                "typed": True,          # 말한 게 아니라 쓴 것 — 대화 탭에서 구분할 수 있게
                "text": text[:160],
                "reply": reply_clean[:300],
                "endpoint_s": 0.0,      # 말끝을 기다린 게 아니다
                "stt_s": 0.0,
                "llm_tts_s": round(time.time() - llm_start, 3),
                "llm_first_s": round(self.turn_first_queued_at - llm_start, 3) if self.turn_first_queued_at else None,
                "tts_first_s": round(self.turn_first_tts_s, 3) if self.turn_first_tts_s else None,
                "first_chars": self.turn_first_chars,
                "total_s": round(time.time() - start_time, 3),
            })
        except asyncio.CancelledError:
            print("[WS] Text processing cancelled")
        except Exception as e:
            print(f"[WS] Processing error: {e}")
            traceback.print_exc()

    EMOTION_PATTERN = r"sleepy|neutral|curious|admiring|happy|sad|angry"
    VALID_STYLES = {"sleepy", "neutral", "curious", "admiring", "happy", "sad", "angry"}
    # 모델이 규격 밖 감정 단어를 발명하는 경우의 동의어 매핑 (그 외는 neutral)
    STYLE_SYNONYMS = {
        "cheerful": "happy", "excited": "happy", "joyful": "happy", "playful": "happy",
        "curiously": "curious", "surprised": "curious", "interested": "curious",
        "understanding": "neutral", "calm": "neutral", "thoughtful": "neutral",
        "apologetic": "sad", "sorry": "sad", "worried": "sad",
        "annoyed": "angry", "mad": "angry", "frustrated": "angry",
        "tired": "sleepy", "drowsy": "sleepy",
        "proud": "admiring", "impressed": "admiring",
    }
    # 모델이 영어 규격 대신 한국어 감정 단어를 낼 때의 매핑 (예: "(중립)")
    STYLE_KO = {
        "중립": "neutral", "평범": "neutral", "담담": "neutral", "담담함": "neutral",
        "졸림": "sleepy", "졸려": "sleepy", "나른": "sleepy", "나른함": "sleepy",
        "호기심": "curious", "궁금": "curious", "궁금함": "curious",
        "감탄": "admiring", "존경": "admiring", "뿌듯": "admiring", "감동": "admiring",
        "기쁨": "happy", "행복": "happy", "기뻐": "happy", "신남": "happy", "즐거움": "happy",
        "슬픔": "sad", "슬퍼": "sad", "우울": "sad", "미안": "sad",
        "화남": "angry", "분노": "angry", "화나": "angry", "짜증": "angry",
    }
    # 지문 "(웃음을 참지 못하고)"에서 감정 추정용 어간 — 가장 앞에 나온 단서를 쓴다.
    # "화" 단독은 대화·화면과 겹치므로 "화가/화난/화를"만
    DIRECTION_HINTS = {
        "happy": ("웃", "미소", "신나", "기뻐", "활짝", "킥킥", "깔깔", "장난"),
        "curious": ("궁금", "갸웃", "호기심", "동그랗", "놀라", "놀란", "당황", "깜짝", "깜빡"),
        "sad": ("슬프", "슬픈", "시무룩", "한숨", "울먹", "풀이 죽", "속상", "걱정"),
        "angry": ("화가", "화난", "화를", "짜증", "째려", "발끈", "노려", "삐죽"),
        "sleepy": ("하품", "졸", "나른", "눈을 비비"),
        "admiring": ("감탄", "뿌듯", "감동", "반짝", "존경", "우쭐"),
    }
    # 이 길이가 넘도록 닫히지 않는 괄호는 지문이 아니라고 보고 읽기 시작한다
    MAX_DIRECTION_CHARS = 150
    # 첫 문장의 앞 절 — 8자 이상이고 연결어미로 끝나 뒤에 말이 더 오는 곳 ("…일부니까 ", "…전달되는데 ")
    # 말이 이어질 조각 — 마지막 낱말이 접속어이거나 연결어미·조사로 끝남
    FRAGMENT_HOLD_SEC = 1.2
    UNFINISHED_WORDS = {"근데", "그런데", "그리고", "그래서", "그러니까", "그니까", "그러면", "그럼", "아니", "아니면",
                        "그", "저", "이제", "막", "좀", "음", "어", "아", "뭐냐", "왜냐면",
                        # 홀로 떨어져 끝난 의존명사 — "내가 말한 게", "어제 산 걸"처럼 뒤가 붙는다
                        "게", "걸", "건", "거", "것", "수", "줄", "만큼", "대로"}
    # 끝에 오면 말이 이어질 가능성이 높은 어미·조사. 문장으로 끝날 수 있는 것(거든, 네요…)은 넣지 않는다
    UNFINISHED_END_RE = re.compile(r"(는데|은데|인데|던데|는지|인지|면서|이후에|다음에|때문에|해서|하고|이랑|한테|"
                                   r"에서|으로|위해|통해|대신|부터|까지|보다|처럼|만큼|라고|다가|든지|는게|은게|을게|"
                                   r"ㄴ게|는걸|은걸|을걸)$")

    def looks_unfinished(self, text: str) -> bool:
        t = text.strip()
        if not t or t.endswith("?"):
            return False
        t = t.rstrip(".,…~! ")
        if t.lower() in self.wake_words:  # 이름만 불렀다 — 보통 바로 본론이 이어진다
            return True
        last = t.split()[-1] if t.split() else ""
        return last in self.UNFINISHED_WORDS or bool(self.UNFINISHED_END_RE.search(last))

    # 첫 조각을 이보다 짧게 끊지 않는다 (연결어미 분할도 이미 8자 이상만 걸린다)
    MIN_FIRST_CHARS = 8
    # 쉼표·연결어미가 없어도 이만큼 길어지면 띄어쓰기에서 끊는다 — 합성 시간이 글자 수에 비례한다
    # (GPT-SoVITS 실측: 16~18자 2.1초, 29자 3.6초, 60자 4.9초, 75자 6.9초)
    MAX_FIRST_CHARS = 22
    # 첫 조각 뒤의 문장은 쉼표에서만 나눈다. 아무 띄어쓰기에서나 자르면 조각마다 억양이 새로 시작해
    # 경계가 뚝뚝 끊겨 들렸다 — 쉼표가 없으면 문장을 통째로 보낸다
    MAX_CHUNK_CHARS = 45
    # 이만큼은 들려야 프롬프트를 미리 먹인다 ("어", "아니" 한 마디에 GPU를 쓰지 않게)
    PREFETCH_MIN_CHARS = 6
    FIRST_CLAUSE_RE = re.compile(r"^(\s*\S.{6,}?(?:니까|는데|은데|인데|던데|지만|어서|아서|해서|라서|면서|더니|다가|아니고|보니|아닌지))\s+(?=\S)")

    @classmethod
    def infer_style(cls, direction: str) -> str:
        best, best_pos = "neutral", len(direction) + 1
        for style, hints in cls.DIRECTION_HINTS.items():
            for h in hints:
                pos = direction.find(h)
                if 0 <= pos < best_pos:
                    best, best_pos = style, pos
        return best

    # 문서 판정 기준은 llm.py에 하나만 둔다 — 읽을 때(여기)와 과거 기록을 다시 넣을 때
    # (llm.prepare_history) 같은 것을 써야 한다
    DOC_START_RE = DOC_START_RE
    DOC_BOLD_RE = DOC_BOLD_RE
    cut_document = staticmethod(cut_document)

    @classmethod
    def document_cut(cls, said: str, full: str = ""):
        """이번 턴에 읽은 말 전체(said)에서 문서가 시작되는 자리, 없으면 None.

        조각마다 부르는 것이 아니라 누적된 말에 걸어야 한다 — 스트리밍에서는 개행과
        뒤따르는 "- "가 서로 다른 토큰으로 오기 때문이다.
        """
        m = cls.DOC_START_RE.search(said)
        if m:
            return m.start()
        # "--- **추가 정보:** - …" 같은 머리말은 **가 지워진 said에는 흔적이 없다.
        # 자리를 정확히 옮길 수는 없으니 지금까지 읽은 데까지만 남기고 끊는다
        if full and cls.DOC_BOLD_RE.search(full):
            return len(said)
        return None

    @classmethod
    def split_speakable(cls, raw: str):
        """(읽을 텍스트, 보류할 나머지). 완성된 괄호 지문·*행동*은 지우고,
        닫히지 않은 '(' 또는 '*' 이후는 닫힐 때까지 보류한다 (MAX_DIRECTION_CHARS 초과 시 기호만 버리고 방출)."""
        text = STAGE_DIRECTION_RE.sub(" ", raw)
        opens = [i for i in (text.find("("), text.find("*")) if i >= 0]
        if not opens:
            return re.sub(r" {2,}", " ", text), ""
        cut = min(opens)
        if len(text) - cut > cls.MAX_DIRECTION_CHARS:
            return re.sub(r" {2,}", " ", text[:cut] + text[cut + 1:]), ""
        return re.sub(r" {2,}", " ", text[:cut]), text[cut:]

    JUDGE_WAIT_SEC = 0.6   # 첫 음성을 이만큼까지만 기다린다. 늦으면 그냥 답한다 (씹히는 게 더 나쁘다)

    async def stream_llm_tts(self, user_text: str, websocket: WebSocket, start_time: float = None,
                             speaker: str = None, situation: str = None, save_as: str = None,
                             speaker_id: str = None, verdict_task=None, history_limit: int = None,
                             stt: dict = None):
        full_response = ""
        spoke = False        # 실제로 입을 열었는가 (판정이 접으면 False — 턴 기록을 남기지 않는다)
        self.turn_first_queued_at = self.turn_first_tts_s = self.turn_first_chars = None
        raw = ""     # 도착했지만 아직 지문 처리 전인 텍스트
        buffer = ""  # 지문이 제거된, 읽을 텍스트 (문장이 큐로 나가면 비워진다)
        said = ""    # 이번 턴에 읽기로 넘긴 말 전체 — 문서 판정은 여기에 걸어야 한다
        doc_cut = False      # 문서가 시작돼 읽기를 멈췄는가
        first_sentence = True
        # 감정 태그는 응답 선두에 옴: "(happy) 안녕..." — 캡처해서 턴 전체 문장에 적용
        # (후미 태그 방식은 문장 단위 분할 시 마지막 조각에만 감정이 남아 전부 neutral로 합성됐음)
        turn_style = None

        print("[WS] Starting LLM stream...")

        # 제너레이터를 변수로 들고 있다가 종료/취소 시 반드시 닫는다 — 취소된 턴의
        # 열린 HTTP 스트림이 쌓이면 이후 LLM 요청이 수십 초씩 정체될 수 있음
        agen = chat_with_gpt_stream(user_text, speaker=speaker, situation=situation, save_as=save_as,
                                    speaker_id=speaker_id, history_limit=history_limit, stt=stt)
        try:
            async for chunk in agen:
                # LLM 실패는 llm.py가 "ERROR_STREAM: ..." 조각으로 알린다 — 응답 텍스트가 아니므로
                # TTS로 읽지 않는다 (Ollama가 꺼져 있을 때 영어 예외 문구를 소리 내 읽던 문제).
                # 이미 큐에 넣은 앞 문장들은 그대로 재생되고, 나머지 턴은 조용히 버린다.
                if chunk.startswith("ERROR_"):
                    print(f"[WS] LLM 실패 — 이번 턴 발화 생략: {chunk}")
                    await websocket.send_json({"type": "status", "message": "⚠️ LLM 응답 실패 (서버 로그 확인)"})
                    return False
                full_response += chunk
                if doc_cut:
                    continue    # 문서가 시작된 뒤로는 읽지 않는다 (스트림만 끝까지 받는다)
                raw += chunk.replace("**", "")  # 마크다운 굵게 표시는 읽을 내용이 아님

                # 선두 감정 태그 캡처 (첫 문장 전송 전)
                if turn_style is None:
                    # 나란히 돌린 판정이 "내게 한 말이 아니다"라고 하면 여기서 접는다.
                    # 첫 음성이 나가기 전이라 되돌릴 수 있는 마지막 지점이다
                    if verdict_task is not None:
                        mine = await self.check_verdict(verdict_task)
                        verdict_task = None
                        if not mine:
                            print(f"[WS] 듣기만 함 (판정: 내게 한 말 아님): {user_text}")
                            self.log_metrics({"event": "gate", "replied": False, "why": "판정",
                                              "mode": self.mode, "people": self.human_count,
                                              "chars": len(user_text), "text": user_text[:60]})
                            await websocket.send_json({"type": "status", "message": "🤫 듣는 중 (판정)"})
                            content = save_as or (f"[{speaker}] {user_text}" if speaker else user_text)
                            loop = asyncio.get_event_loop()
                            await loop.run_in_executor(None, save_to_history, "user", content,
                                                       None if save_as else speaker_id,
                                                       None if save_as else stt)
                            return False
                    # 안전망: 게이트는 should_reply()가 보지만, 기록을 흉내 내다 모델이
                    # (pass)를 그대로 뱉는 일이 있어 그때도 조용히 듣기만 한다
                    if re.match(r"\s*\(\s*pass\s*\)", raw, re.IGNORECASE):
                        print(f"[WS] Pass — 발화 보류 (사람들 간 대화로 판단): {user_text}")
                        await websocket.send_json({"type": "status", "message": "🤫 발화 보류 (듣는 중)"})
                        # 들은 내용은 기록에 남겨 나중에 끼어들 때 맥락으로 사용
                        content = save_as or (f"[{speaker}] {user_text}" if speaker else user_text)
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, save_to_history, "user", content,
                                                   None if save_as else speaker_id,
                                                   None if save_as else stt)
                        return False
                    # 선두 괄호 태그는 (영단어라면) 무엇이든 흡수 — 규격 밖 단어가
                    # TTS로 읽히는 것 방지 ("(cheerful) 천만에!" 사고)
                    m = re.match(r"\s*\(([^()]*)\)\s*", raw)
                    if m:
                        inner = m.group(1).strip()
                        word = inner.lower()  # 한글은 lower() 영향 없음
                        if word in self.VALID_STYLES:
                            turn_style = word
                        elif word in self.STYLE_SYNONYMS:
                            turn_style = self.STYLE_SYNONYMS[word]
                        elif word in self.STYLE_KO:
                            turn_style = self.STYLE_KO[word]
                        else:
                            # 태그 대신 지문 "(당황한 듯 눈을 깜빡이며)" — 읽지 않고 감정만 추정
                            turn_style = self.infer_style(inner)
                            print(f"[WS] Style tag 대신 지문 '({inner})' → {turn_style}")
                        raw = raw[m.end():]
                    elif raw.lstrip().startswith("(") and len(raw) < self.MAX_DIRECTION_CHARS:
                        continue  # 태그/지문이 닫힐 때까지 대기
                    elif raw.strip():
                        turn_style = "neutral"  # 태그 없이 시작한 응답
                    else:
                        continue

                # 읽을 수 있는 부분만 buffer로 넘긴다 — 완성된 지문은 지우고,
                # 아직 닫히지 않은 지문 "(순간적으로 주변을 둘러보더니," 는 닫힐 때까지 보류
                speak, raw = self.split_speakable(raw)
                # 문서 판정은 **이번 턴에 읽은 말 전체**에 대고 한다. 조각(speak)에만 걸었더니
                # 한 번도 작동하지 않았다 — 개행과 "- "가 다른 토큰으로 와서 "\n\s*[-•*]\s"가
                # 어느 조각에도 온전히 담기지 않는다 (불릿 7줄짜리 답이 그대로 읽히고
                # 기억까지 들어갔다). buffer는 문장이 큐로 나갈 때 비워지므로 따로 모아 둔다.
                # 굵은 머리말은 **를 떼기 전 원문(full_response)에서만 보인다
                cut_at = self.document_cut(said + speak, full_response)
                if cut_at is not None:
                    print(f"[WS] 대답에 문서가 섞여 끊음: {(said + speak)[cut_at:][:40]!r}")
                    if cut_at >= len(said):
                        buffer += speak[:cut_at - len(said)]
                    else:
                        # 목록 기호는 뒤따르는 공백까지 와야 걸린다 — "\n", "-"가 앞 조각에서
                        # 이미 buffer에 들어가 있다. 그만큼 되물러야 "다시볼게요.-"로 읽히지 않는다
                        back = len(said) - cut_at
                        buffer = buffer[:-back] if back < len(buffer) else ""
                    said = (said + speak)[:cut_at]
                    # 읽기는 여기서 멈추지만 스트림은 끝까지 받는다 — break로 빠져나오면
                    # chat_with_gpt_stream이 기록을 저장하기 전에 제너레이터가 닫혀
                    # 그 턴이 아예 기록에 안 남는다. 저장된 문서는 prepare_history가 떼어 낸다
                    doc_cut, raw = True, ""
                    continue
                buffer += speak
                said += speak

                # 첫 문장 한정: 쉼표 경계가 나오면 선행 조각을 먼저 TTS로 보내
                # 첫 오디오 시작을 앞당김 (요청 단위 TTS라 토큰 단위 합성은 불가,
                # 쉼표 단위는 운율 손상 없이 끊을 수 있는 최소 단위)
                if first_sentence and self.chunking == "aggressive":
                    # "네, 그렇게 할게요"의 "네,"처럼 너무 짧은 조각은 첫 소리를 당겨 주지 못한다 —
                    # 합성은 길이와 무관하게 1.0s쯤 걸리는데 "네,"는 소리가 0.66s뿐이라, 뒤 조각(1.4s+)이
                    # 준비되기 전에 소리가 끝나 말이 끊긴다. 11자면 소리 1.9s로 뒤를 덮는다.
                    # 글자를 지우는 게 아니라 자르는 위치만 미루는 것이라 "어…", "음…"은 그대로 읽힌다
                    m_comma = next((m for m in re.finditer(r",\s", buffer)
                                    if len(buffer[:m.start()].strip()) >= self.MIN_FIRST_CHARS), None)
                    if m_comma:
                        prefix = buffer[:m_comma.start() + 1]  # 쉼표 포함
                        await self.queue_tts_task(prefix, websocket, start_time, turn_style)
                        buffer = buffer[m_comma.end():]
                        first_sentence = False
                    else:
                        # 첫 문장이 짧게 끝나면(".") 아래 문장 분할에 맡기고, 길어지면 여기서 끊는다
                        m_end = re.search(r"[.!?\n]", buffer)
                        long_first = (m_end is None or m_end.start() >= self.MAX_FIRST_CHARS)
                        # 대답 대부분이 쉼표 없이 "…일부니까 당연한 수순이죠."처럼 이어져 쉼표 분할이
                        # 거의 안 걸렸다 — 연결어미에서도 끊는다 (41자 1.34s → 앞 16자 0.80s)
                        m_clause = self.FIRST_CLAUSE_RE.match(buffer) if m_end is None else None
                        if m_clause:
                            await self.queue_tts_task(m_clause.group(1), websocket, start_time, turn_style)
                            buffer = buffer[m_clause.end():]
                            first_sentence = False
                        elif long_first and len(buffer) >= self.MAX_FIRST_CHARS:
                            # 쉼표도 연결어미도 없이 길어지는 문장 — 그냥 기다리면 첫 소리가 그만큼 늦는다.
                            # 실사용에서 첫 조각이 29자(중앙)~75자까지 가서 합성이 3.6~6.9초 걸렸다.
                            # 띄어쓰기에서 끊어 앞부분만 먼저 보낸다 (조사가 잘리지 않게)
                            # 문장 끝을 넘어가서 자르지 않는다 ("…않으세요. 일상"처럼 두 문장에 걸치면 어색)
                            stop = m_end.start() if m_end else len(buffer)
                            cut = buffer.rfind(" ", self.MIN_FIRST_CHARS, min(self.MAX_FIRST_CHARS, stop))
                            if cut <= 0:  # 그 안에 띄어쓰기가 없으면 조금 더 뒤까지 본다
                                cut = buffer.rfind(" ", self.MIN_FIRST_CHARS,
                                                   min(self.MAX_FIRST_CHARS + 8, stop))
                            if cut > 0:
                                await self.queue_tts_task(buffer[:cut], websocket, start_time, turn_style)
                                buffer = buffer[cut + 1:]
                                first_sentence = False

                if self.chunking != "off" and any(punct in speak for punct in ['.', '!', '?', '\n']):
                    sentences = re.split(r'(?<=[.!?\n])\s+', buffer)

                    if len(sentences) > 1:
                        for sentence in sentences[:-1]:
                            if sentence.strip():
                                ts = start_time if first_sentence else None
                                await self.queue_sentence(sentence, websocket, ts, turn_style)
                                first_sentence = False
                        buffer = sentences[-1]
                    # '.'은 말줄임표("음...")의 첫 점일 수 있어 즉시 방출하지 않는다 —
                    # 다음 공백이 오면 위의 문장 분할에서 한 덩어리로 나간다
                    elif buffer.strip() and buffer.endswith(('!', '?', '\n')):
                         ts = start_time if first_sentence else None
                         await self.queue_sentence(buffer, websocket, ts, turn_style)
                         buffer = ""
                         first_sentence = False

            # 끝까지 닫히지 않은 지문(토큰 한도로 잘린 경우 등)은 버리고 나머지만 읽는다
            if not doc_cut:
                buffer += strip_stage_directions(re.sub(r"\([^()]*$|\*[^*\n]*$", "", raw))
            if buffer.strip():
                ts = start_time if first_sentence else None
                await self.queue_sentence(buffer, websocket, ts, turn_style or "neutral")

            self.last_activity = time.time()
            self.last_reply_text = full_response      # 컨트롤 앱 대화 화면용
            spoke = True
            # 응답 완료 — 대화 연속성 추적 (같은 상대의 후속 발화는 판단 없이 응답)
            if speaker:
                self.last_reply_speaker = speaker
                self.last_reply_time = time.time()

            # 장기기억은 턴마다가 아니라 대화 덩어리(에피소드)가 끝날 때 memory_watchdog가 정리한다

        except asyncio.CancelledError:
            print("[WS] Stream cancelled")
            raise
        finally:
            try:
                await agen.aclose()
            except Exception:
                pass
        return spoke

    # ------------------------------------------------------------------ 기억 정리 · 입장 인사
    def busy(self) -> bool:
        """누가 말하는 중이거나, 대답을 만들거나 읽는 중"""
        return bool((self.processing_task and not self.processing_task.done())
                    or self.vad.recording
                    or (getattr(self, "audio_task_queue", None) and not self.audio_task_queue.empty()))

    def interest(self) -> float:
        """방금 오간 이야기가 내 기억에 얼마나 걸리나 (0~1).

        매 턴 이미 돌린 RAG 검색 로그를 다시 읽는 것이라 추가 비용이 없다.
        가까운 기억이 있을수록(거리가 작을수록) 끼어들고 싶어진다."""
        try:
            hits = (memory_manager.recent_retrievals[0] or {}).get("hits") or []
        except (IndexError, AttributeError):
            return 0.0
        dists = [h["distance"] for h in hits if h.get("distance")]
        if not dists:
            return 0.0
        return max(0.0, min(1.0, 1.0 - min(dists) / MEMORY_GATE))

    def initiative_wait(self) -> float:
        """지금 얼마나 조용하면 먼저 말을 꺼낼 것인가 (초)"""
        backoff = min(self.initiative_backoff ** self.initiative_count, self.initiative_backoff_max)
        return self.initiative_base / (1.0 + self.interest()) * backoff

    async def initiative_watchdog(self):
        """조용해지면 캐릭터가 먼저 화제를 꺼낸다.

        기다리는 시간은 고정이 아니라 욕구다 — 방금 이야기가 기억에 걸릴수록 빨리, 대답이 없을수록
        점점 늦게. 예전에는 두 번 말을 걸고 대답이 없으면 사람이 말할 때까지 영영 멈췄다."""
        while True:
            await asyncio.sleep(5)
            try:
                if (not self.initiative_on or self.websocket is None or not self.listening()
                        or not self.channel_members or self.last_activity is None or self.busy()):
                    continue
                idle = time.time() - self.last_activity
                wait = self.initiative_wait()
                if idle >= wait:
                    self.take_initiative(idle, wait)
            except Exception as e:
                print(f"[WS] initiative error: {e}")

    async def manuscript_watchdog(self, websocket: WebSocket):
        """문서를 같이 읽는다 — 주기적으로 문서(구글 문서 또는 화면 글자)를 읽어 두고, 새로 쓴 대목에 가끔 한마디.

        읽기(OCR)는 VRAM을 쓰지 않고 1~2초라 자주 돌려도 대답이 느려지지 않는다. VLM은
        부르지 않는다 — 대화 모델 옆에 3.5GB를 올리는 순간 첫 토큰이 10초가 된다.

        판단은 조용한 쪽으로 기운다. 끼어들어 생각을 끊는 건 되돌릴 수 없고, 조용히 있어서
        잃는 것은 없다 — 모델에게 "말할까?"를 물었을 때 이 저장소에서 두 번 실패한 쪽
        (게이트는 286턴 중 0번, 판정은 답해야 할 100턴 중 14턴을 침묵)과 반대 방향이다.
        """
        m = self.manuscript
        while True:
            await asyncio.sleep(max(3.0, m.poll_sec))
            try:
                # **음성 채널에 실제로 들어가 있을 때만.** channel_members는 나간 뒤에도 남는다
                # (self_leave가 joined_at만 비운다) — 그걸 믿었더니 채널에 없는 동안에도
                # 문서를 폴링하고 "말하겠다"고 헛결정했다
                if self.websocket is None or not self.joined_at or not self.channel_members:
                    continue
                # 원고가 바뀌었는지는 **늘** 본다 — 그게 글쓰기 상태로 들어오는 신호다.
                # (`!원고`를 기다리면 "알아서 전환"이 되지 않는다)
                if not m.watch and not self.activity.writing():
                    continue
                text, note = await asyncio.get_event_loop().run_in_executor(
                    None, self.read_manuscript)
                added = m.update(text, note)
                if added:
                    self.note_activity(self.activity.saw_edit())
                else:
                    # 고친 게 없을 때만 식는지 본다. 먼저 보면 같은 한 바퀴 안에서
                    # "대화로 식음 → 바로 글쓰기"가 찍혀 기록이 깜빡인다
                    self.note_activity(self.activity.tick())
                if self.activity.writing() and not m.on:
                    m.start(keep=True)       # 저절로 들어왔으면 읽어 둔 것을 이어 쓴다
                elif not self.activity.writing() and m.on:
                    m.on = False             # 대화로 돌아가면 한마디는 멈춘다 (원고는 그대로)
                if text and self.activity.writing():
                    # 물어볼 때 답할 수 있게, 말을 걸지 않더라도 원고는 늘 들고 있는다
                    title, body = m.reading_block()
                    import llm as _llm
                    _llm.set_reading(f"manuscript:{m.provider}", title, body)
                can, why = m.may_speak(busy=self.busy() or not self.listening())
                can = can and self.activity.writing()
                if added or can:
                    self.log_metrics({"event": "manuscript", "new_chars": added,
                                      "chars": len(m.text), "spoke": can, "why": why,
                                      "state": self.activity.state, "note": note[:60]})
                if can:
                    self.speak_about_manuscript(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[WS] 원고 보기 오류: {e}")

    def note_activity(self, why: str) -> None:
        """상태가 바뀌었으면 남긴다. 조용히 바뀌면 왜 행동이 달라졌는지 알 수 없다 —
        게이트가 몇 달간 안 걸린 걸 아무도 몰랐던 것과 같은 이유로 보이게 둔다"""
        if not why:
            return
        print(f"[WS] 상태 → {self.activity.state} ({why})")
        self.log_metrics({"event": "activity", "state": self.activity.state, "why": why})
    def read_manuscript(self) -> tuple:
        """원고를 읽는다 (동기 — executor에서 돈다).

        `manuscript.provider`가 `gdocs`면 구글 문서에서 글을 그대로 가져온다 — OCR이 아니라
        정확하고, 보이는 화면이 아니라 문서 전체고, 어느 창이 앞이든 무관하다. `screen`이면
        화면을 OCR한다 (글자가 작으면 vision.read_text가 거른다)
        """
        from config_loader import load_config
        m = self.manuscript
        try:
            if m.provider == "gdocs":
                import gdocs
                g = m.gdocs or {}
                return gdocs.read(g.get("folder_id"), g.get("doc_id"), m.max_chars)
            import vision
            cfg = load_config().get("screen", {}) or {}
            sources = cfg.get("sources", {}) or {}
            name = m.source or cfg.get("default")
            src = sources.get(name)
            if not src:
                return "", f"'{name}' 소스가 설정에 없습니다"
            return vision.read_text(src, m.max_chars, min_glyph_px=m.min_glyph_px)
        except Exception as e:
            # 창을 못 찾거나 계정이 연결되지 않은 것 같은 설정 문제 — 20초마다 터뜨리지 않고
            # 메모로 돌린다. 켤 때는 이 메모가 그대로 사용자에게 간다 (/manuscript가 400으로)
            return "", str(e)

    def speak_about_manuscript(self, websocket: WebSocket):
        """방금 새로 쓴 대목에 대해 먼저 한마디 — take_initiative와 같은 모양"""
        m = self.manuscript
        speaker = self.current_speaker if self.current_speaker in self.channel_members else (
            self.channel_members[0] if self.channel_members else None)
        speaker_id = self.current_speaker_id if speaker == self.current_speaker else None
        # **프롬프트를 먼저 만들고 그 다음에 표시한다.** mark_seen()은 "여기까지 봤다"를
        # 옮기므로 new_text()가 비고, 먼저 부르면 "원고에 한마디 하라"면서 원고를 안 주게 된다
        new_chars = len(m.new_text())
        lines = self.situation_context_lines()
        lines.append(m.situation())
        cue, note = m.cue(), f"({character.OWNER} 님이 문서를 쓰는 중)"
        m.mark_seen()
        self.turn_seq += 1
        seq = self.turn_seq
        print(f"[WS] 원고에 한마디 (새 글 {new_chars}자 / {m.captures}번째 읽기)")

        async def run():
            try:
                self.last_reply_text = ""
                if await self.stream_llm_tts(cue, websocket, time.time(), speaker=speaker,
                                             situation=chr(10).join(lines), save_as=note,
                                             speaker_id=speaker_id):
                    self.log_spoke("manuscript", seq, who=speaker, kind_ko="원고 한마디")
            except asyncio.CancelledError:
                print("[WS] 원고 한마디 취소 (말이 들어옴)")
            except Exception as e:
                print(f"[WS] 원고 한마디 오류: {e}")

        self.inflight_text, self.inflight_wake = None, False
        self.processing_task = self.spawn(run())

    # 기억에서 꺼낼 게 없거나 두 번째로 말을 걸 때 고르는 화제 (캐릭터 팩 initiative_topics가 있으면 그걸 쓴다)
    INITIATIVE_TOPICS = character._pack.get("initiative_topics") or [
        "네 취향 하나(음식, 음악, 날씨, 계절 중에서)를 먼저 밝히고, 상대는 어떤지 묻는다",
        "지금 시각과 요일에 어울리는 한마디를 하고 상대의 오늘 하루를 묻는다",
        "방금까지 하던 이야기에서 네가 흥미로웠던 부분을 짚어 네 의견을 보탠다",
        "요즘 네가 서버에서 지내며 알게 된 소소한 사실이나 생각 하나를 말한다",
    ]

    def take_initiative(self, idle: float, wait: float = 0.0):
        websocket = self.websocket
        interest = self.interest()
        self.initiative_count += 1
        self.last_activity = time.time()  # 다음 시도는 이번에 말을 건 때부터 잰다
        # 마지막으로 말한 사람이 아직 있으면 그 사람의 [기억]을 쓴다
        if self.current_speaker in self.channel_members:
            speaker, speaker_id = self.current_speaker, self.current_speaker_id
        else:
            speaker, speaker_id = self.channel_members[0], None
            key = memory_manager.person_key(None, speaker)
            speaker_id = None if key.startswith("name:") else key
        # 무엇을 말할지 하나로 정해 준다. "조용하다", "무슨 생각 하세요" 같은 말을 지시문에 적었더니 작은 모델이
        # 오히려 그걸 따라 해 두 번째 말은 3/3이 "다들 조용하네요"였다 — 금지 문구 대신 구체적인 화제를 준다
        knows = bool(speaker_id) and bool(memory_manager.store.get_where(
            {"$and": [{"type": "person"}, {"who": speaker_id}]}))
        # 방금 이야기가 기억에 걸렸다면 그 실을 잡는다 — 조용해졌다고 화제를 갈아타지 않는다
        if interest >= 0.5 and self.initiative_count <= 1:
            topic = ("방금까지 하던 이야기에서 [기억]에 걸리는 부분 하나를 짚어, 네 의견이나 아는 것을 보탠다. "
                     "새 화제로 갈아타지 않는다")
        elif self.initiative_count == 1 and knows:
            topic = (f"[기억]에 있는 {speaker} 님의 일(일정, 취미, 최근 있었던 일) 하나를 꺼내 어떻게 됐는지, "
                     "요즘은 어떤지 묻는다. 날짜가 적혀 있으면 그 날짜에 맞게 말한다. "
                     "작업·서버·테스트 이야기 말고 생활 쪽을 고른다")
        else:
            topic = random.choice(self.INITIATIVE_TOPICS)
        lines = self.situation_context_lines()
        lines.append(f"지금은 네가 먼저 말을 꺼낼 차례다. 이번 화제: {topic}. 한두 문장, {NAME}답게.")
        if self.initiative_count > 1:
            lines.append("아까 네가 먼저 건 말에는 대답이 없었다. 그 얘기는 다시 꺼내지 않는다.")
        # 기록에 남길 말과 모델에게 줄 말을 나눈다 — "(잠시 아무도 말하지 않음)"을 그대로 주면 3/3이
        # "대화가 잠시 멈춰 있는 것 같네요. 질문 있으신가요?"였다. "(네가 먼저 말을 꺼낸다)"면 3/3이 진짜 화제
        note, cue = "(잠시 아무도 말하지 않음)", "(네가 먼저 말을 꺼낸다)"
        print(f"[WS] 먼저 말 걸기 ({self.initiative_count}번째, {int(idle)}초 조용함, "
              f"관심 {interest:.2f}, 기다린 기준 {int(wait)}초): {topic}")
        self.turn_seq += 1          # ttfa가 앞 턴에 붙지 않게
        seq = self.turn_seq

        async def run():
            try:
                self.last_reply_text = ""
                if await self.stream_llm_tts(cue, websocket, time.time(), speaker=speaker,
                                             situation="\n".join(lines), save_as=note,
                                             speaker_id=speaker_id):
                    self.log_spoke("initiative", seq, n=self.initiative_count, idle_s=round(idle),
                                   wait_s=round(wait), interest=round(interest, 2))
            except asyncio.CancelledError:
                print("[WS] 먼저 말 걸기 취소 (말이 들어옴)")
            except Exception as e:
                print(f"[WS] 먼저 말 걸기 오류: {e}")

        # 입장 인사와 같이 — 말하는 도중 누가 입을 열면 process_speech가 이 턴을 취소한다
        self.inflight_text, self.inflight_wake = None, False
        self.processing_task = self.spawn(run())

    async def memory_watchdog(self):
        """대화가 잠깐 멈추면 이번 세션 앞부분 요약, 오래 멈추거나 턴이 많이 쌓이면 에피소드 기억 저장.
        요약기는 대화와 같은 로컬 모델을 쓰므로 대화 중(busy)에는 돌리지 않는다"""
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(10)
            if self.busy():
                continue
            try:
                await loop.run_in_executor(None, self.memory_tick)
            except Exception as e:
                print(f"[Memory] watchdog error: {e}")

    def memory_tick(self):
        pending = memory_manager.pending_lines()
        if not pending:
            return
        idle = (datetime.now() - datetime.fromisoformat(pending[-1]["ts"])).total_seconds()
        if idle < 20:
            return
        memory_manager.update_session_summary()
        turns = sum(1 for m in pending if m.get("role") == "user")
        if idle >= self.episode_idle_sec:
            memory_manager.flush_episode("idle")
        elif turns >= self.episode_max_turns and idle >= 60:
            memory_manager.flush_episode("turns")

    async def flush_memory(self, reason: str):
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, memory_manager.flush_episode, reason)
        except Exception as e:
            print(f"[Memory] flush error ({reason}): {e}")

    def greet_targets(self, people: list, rejoin: bool = False, arrival: bool = False) -> list:
        """먼저 인사할 사람 [(이름, 키, 마지막 대화, 오랜만인가)]. 오랜만(greet_after_hours 이상 또는 처음)이면
        greet_cooldown_min, 금방 다시 온 사람(rejoin=True일 때만)이면 greet_rejoin_cooldown_min 간격으로.
        arrival(봇이 방금 들어옴)이면 오랜만인지와 상관없이 있는 사람 모두에게.
        ID가 없으면(듣기 시작 시 명단) 알려진 별칭으로 찾는다"""
        now_t, now = time.time(), datetime.now()
        out = []
        for name, uid in people:
            key = memory_manager.person_key(uid, name)
            last = memory_manager.last_talk_with(None if key.startswith("name:") else key, name)
            long_away = last is None or (now - last).total_seconds() >= self.greet_after_sec
            if not long_away and not rejoin and not arrival:
                continue
            cooldown = self.greet_cooldown_sec if long_away else self.greet_rejoin_cooldown_sec
            # 듣기 시작 명단에는 ID가 없어 키가 name:이름으로 잡힐 수 있다 — 두 키를 같은 사람으로 본다
            greeted = max(self.greeted.get(key, 0), self.greeted.get(f"name:{name}", 0))
            if now_t - greeted < cooldown:
                continue
            out.append((name, key, last, long_away))
        return out

    # 기동 워밍업을 이만큼까지만 기다린다 (그 뒤엔 느려도 인사한다)
    WARM_WAIT_SEC = 45

    async def wait_until_warm(self, why: str = "인사") -> None:
        """LLM·TTS 기동 워밍업이 끝날 때까지 (최대 WARM_WAIT_SEC) 기다린다.

        워밍업은 기동 시 백그라운드로 도는데 입장 인사는 그걸 기다리지 않아서,
        차가운 모델로 프롬프트를 돌리느라 첫마디가 13~17초 걸렸다 (TTS 몫은 3.7초뿐이었다).
        채널에 들어와 한참 말이 없는 것보다 조금 늦게 인사하는 쪽이 낫다.
        """
        import llm as _llm
        import tts as _tts
        t0 = time.time()
        while not (_llm.is_warm() and _tts.is_warm()):
            if time.time() - t0 > self.WARM_WAIT_SEC:
                print(f"[WS] 워밍업을 {self.WARM_WAIT_SEC}초 기다렸지만 안 끝났다 — 그냥 {why}한다")
                return
            await asyncio.sleep(0.5)
        if time.time() - t0 > 0.5:
            print(f"[WS] 워밍업 끝나기를 {time.time() - t0:.1f}초 기다린 뒤 {why}")

    async def maybe_greet(self, people: list, delay: float = 0, rejoin: bool = False, arrival: bool = False):
        """사람이 들어오면(또는 봇이 채널에 들어오거나 듣기를 막 켰으면) 먼저 한마디 건넨다"""
        if delay:
            await asyncio.sleep(delay)
        # 차가운 모델로 인사하면 첫마디가 13~17초 걸린다 — 워밍업을 기다린다
        await self.wait_until_warm("인사")
        websocket = self.websocket
        if websocket is None or not self.listening() or self.busy():
            return
        loop = asyncio.get_event_loop()
        targets = await loop.run_in_executor(None, self.greet_targets, people, rejoin, arrival)
        if not targets or self.busy() or not self.listening():
            return
        for name, key, _, _ in targets:
            self.greeted[key] = self.greeted[f"name:{name}"] = time.time()
        who = ", ".join(f"{n} 님" for n, _, _, _ in targets)
        since = [f"{n} 님과 마지막으로 이야기한 건 {ago_text((datetime.now() - last).total_seconds())} 전"
                 if last else f"{n} 님과는 아직 이야기한 기록이 없다" for n, _, last, _ in targets]
        long_away = any(t[3] for t in targets)
        # 지시문은 사용자 턴이 아니라 [현재 상황](시스템)에 둔다 — 사용자 턴에 넣었더니 작은 모델이
        # 그걸 상대가 한 말로 받아 "새로 오신 분은 누구시죠?"처럼 엉뚱하게 답하고 [기억]도 안 썼다
        # 기록용 문장과 모델에게 주는 말은 따로 — 상황을 그대로 주면 모델이 그 문장에 대꾸한다 (take_initiative 참고)
        note = f"({NAME}가 채널에 들어옴)" if arrival else f"({who}이 채널에 들어옴)"
        cue = "(네가 먼저 말을 꺼낸다)"
        situation_lines = self.situation_context_lines()
        if arrival:
            situation_lines.append(
                # 하지 말 것을 늘어놓으면(이름을 소개하지 마라 등) 작은 모델이 오히려 그걸 한다 —
                # 먼저 와 있던 사람에게 무엇을 할지만 적는다
                f"네가 방금 이 음성 채널에 들어왔다. 아직 아무도 너에게 말을 걸지 않았다 — "
                f"앞선 대화에 이어서 답하는 것이 아니라, 네가 먼저 입을 여는 것이다. "
                f"먼저 와 있던 {who}에게 왔다는 인사부터 건넨다. "
                f"첫 문장은 인사다 — 용건이나 일 이야기로 시작하지 않는다. "
                f"그다음 한 문장으로 안부를 묻는다. [기억]에 이 사람의 취향·일상·약속 같은 가벼운 것이 있으면 "
                f"그걸 꺼내도 좋지만, 작업·서버·테스트·프로젝트 이야기는 꺼내지 않는다 "
                f"(그건 상대가 먼저 말할 때 받는다). {'. '.join(since)}. 두 문장.")
        elif long_away:
            situation_lines.append(
                f"{who}이 방금 음성 채널에 들어왔고 아직 아무 말도 하지 않았다. {'. '.join(since)}. "
                "네가 먼저 짧게 한마디 건넨다 — 인사가 먼저다. [기억]에 이 사람의 약속이나 일정"
                "(시험, 면접, 여행 같은 생활의 일)이 있으면 그게 어떻게 됐는지 묻고, 없으면 그냥 인사한다. "
                "작업·서버·테스트 이야기는 네가 먼저 꺼내지 않는다.")
        else:
            situation_lines.append(
                f"{who}이 잠깐 나갔다가 방금 다시 들어왔고 아직 아무 말도 하지 않았다. {'. '.join(since)}. "
                "네가 먼저 다시 온 걸 알아보는 한마디를 건넨다 — 한 문장, 가볍게.")
        speaker = targets[0][0] if len(targets) == 1 else None
        speaker_key = targets[0][1] if len(targets) == 1 and not targets[0][1].startswith("name:") else None
        kind = "입장" if arrival else ("오랜만" if long_away else "다시 옴")
        print(f"[WS] 입장 인사 ({kind}): {who}")

        self.turn_seq += 1
        seq = self.turn_seq

        async def run():
            try:
                self.last_reply_text = ""
                # 들어오는 순간엔 진행 중인 대화가 없다 — 기록을 주면 그 마지막 줄에 대꾸한다
                if await self.stream_llm_tts(cue, websocket, time.time(), speaker=speaker,
                                             situation="\n".join(situation_lines), save_as=note,
                                             speaker_id=speaker_key, history_limit=0):
                    self.log_spoke("greet", seq, who=who, kind_ko=kind)
            except asyncio.CancelledError:
                print("[WS] 입장 인사 취소 (말이 들어옴)")
            except Exception as e:
                print(f"[WS] 입장 인사 오류: {e}")

        # 인사 중에 누가 말하면 process_speech가 이 턴을 취소한다 — 인사는 이어붙일 발화가 아니다
        self.inflight_text, self.inflight_wake = None, False
        self.processing_task = self.spawn(run())

    async def queue_sentence(self, sentence: str, websocket: WebSocket, start_time: float = None,
                             style: str = "neutral"):
        """문장 하나를 TTS 큐에 넣는다.

        aggressive에서만 쉼표에서 한 번 더 쪼갠다 — 첫 소리는 빨라지지만 이음매가 늘어난다.
        기본(sentence)은 문장을 통째로 보낸다: 억양이 한 번에 이어지고, 이음매는 문장 사이에만 남는다"""
        rest = sentence.strip()
        if self.chunking not in ("aggressive", "comma"):
            if rest:
                await self.queue_tts_task(rest, websocket, start_time, style)
            return
        while len(rest) > self.MAX_CHUNK_CHARS:
            cut = rest[:self.MAX_CHUNK_CHARS].rfind(", ")
            if cut < self.MIN_FIRST_CHARS:
                break  # 쉼표가 없으면 문장을 통째로 — 억양을 지키는 쪽이 낫다
            await self.queue_tts_task(rest[:cut + 1], websocket, start_time, style)
            start_time = None
            rest = rest[cut + 1:].lstrip()
        if rest:
            await self.queue_tts_task(rest, websocket, start_time, style)

    async def queue_tts_task(self, text: str, websocket: WebSocket, start_time: float = None, style: str = "neutral"):
        # 감정/중요도 태그만 남은 끝 조각은 합성할 내용이 없으므로 큐에 넣지 않음
        cleaned_text, _, _ = extract_emotion_and_clean(text)
        if not cleaned_text:
            return
        # 첫 소리까지 걸린 시간을 LLM 몫과 TTS 몫으로 나눠 본다 (ttfa가 왜 길어졌는지)
        first = self.turn_first_queued_at is None
        if first:
            self.turn_first_queued_at = time.time()
            self.turn_first_chars = len(cleaned_text)
        task = asyncio.create_task(self.synthesize_wrapper(text, websocket, style, measure=first))
        await self.audio_task_queue.put((text, task, start_time, style, self.turn_seq))

    async def synthesize_wrapper(self, text: str, websocket: WebSocket, style: str = "neutral",
                                 measure: bool = False):
        async with self.tts_semaphore:
            loop = asyncio.get_event_loop()
            cleaned_text, _, _ = extract_emotion_and_clean(text)
            if not cleaned_text:
                return None

            print(f"[WS] Starting TTS for: {cleaned_text} ({style})")
            t0 = time.time()
            audio_data = await loop.run_in_executor(None, synthesize, cleaned_text, style)
            if measure:
                # 합성이 턴 기록보다 늦게 끝나는 경우가 많아 (turn의 tts_first_s가 비었다) 별도 이벤트로 남긴다
                self.turn_first_tts_s = time.time() - t0
                self.log_metrics({"event": "tts_first", "ms": round(self.turn_first_tts_s * 1000),
                                  "chars": len(cleaned_text), "style": style})
            if audio_data:
                return audio_data.getvalue()
            return None

websocket_handler = WebSocketHandler()
