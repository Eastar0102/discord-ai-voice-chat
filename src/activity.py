"""지금 무엇을 하는 중인가 — 캐릭터의 상태 하나.

모드 스위치가 늘어나면서 저마다 명령이 생겼다(듣기 모드·원고). 그걸 상태 하나로 묶고,
들어가고 나오는 것을 가능하면 **사실**로 정한다.

상태는 둘뿐이다:
  대화   — 기본
  글쓰기 — 만든 사람이 글(소설·원고)을 쓰는 중이거나 그 이야기를 하는 중

전사(기록) 모드는 여기에 없다. 그건 캐릭터의 지능과 무관한 별개 기능이고(회의록),
듣기와 배타적이라 자동으로 드나들면 위험하다 — 지금처럼 명령으로만 켠다.

**왜 두 갈래로 들어오는가.** 글을 쓰면 구글 문서가 바뀌므로 그건 추론이 아니라 사실이고,
틀릴 여지가 없다. 하지만 "어제 쓴 거 어때?"처럼 **안 쓰면서 이야기만 할 때**는 문서가
가만히 있다. 그 경우만 낱말로 받는다. 낱말 목록은 쓴 날의 어휘만 잡아 늘 문제였지만
여기서는 틀려도 피해가 작다 — 잘못 들어가 봐야 원고를 들고
있는 정도고, 조용히 있어야 할 때 조용히 못 하는 쪽의 실수가 아니다.
"""
import re
import time

import character

TALK = "대화"
WRITING = "글쓰기"

# 소설 이야기로만 쓰이는 말. 실제 기록 1,000발화에 대고 골랐다 — `캐릭터`와 `설정`은
# 전부 **봇 캐릭터 자신** 이야기였고, `전개`·`문장`은 한 번도 안 나왔다. 그래서 오해 없는 것만 남긴다. 전환은 모두 metrics에 남으므로,
# 실제로 쓰다 보면 무엇이 빠졌는지 보고 늘리면 된다
_GENERIC = (r"원고|웹소설|소설|연재|집필|플롯|복선|등장인물|주인공|프롤로그|"
            r"\d+\s*화(?![가-힣])|다음\s*화|이번\s*화")


def _words(title: str = "", aliases=None):
    """작품 이름은 설정에서 받는다 — 코드에 박아 두면 작품이 바뀔 때 같이 못 바꾼다.
    캐릭터 자신이 나오는 작품(manuscript.other_work)은 넣지 않는다: 그 얘기는 글쓰기가 아니다"""
    parts = [_GENERIC]
    for w in [title] + list(aliases or []):
        w = (w or "").strip()
        if len(w) >= 2:
            parts.append(re.escape(w))
    return re.compile("(" + "|".join(parts) + ")")


class Activity:
    """상태 하나와, 그리로 들어오고 나간 이유"""

    def __init__(self, cfg: dict = None):
        cfg = cfg or {}
        self.state = TALK
        self.why = "시작"
        self.since = time.time()
        self.pinned = False          # 명령으로 고정하면 자동 전환을 멈춘다
        self.last_signal = 0.0       # 마지막으로 "글쓰기다" 싶었던 때
        # 원고가 바뀐 뒤 이만큼은 글쓰기로 본다 (말로 들어왔을 때도 같은 시계를 쓴다)
        self.hold_sec = float(cfg.get("writing_hold_sec", 600))
        self.title = (cfg.get("title") or "").strip()
        self.words = _words(self.title, cfg.get("aliases"))

    # ── 신호 ────────────────────────────────────────────────────────
    def saw_edit(self) -> str:
        """원고가 바뀌었다 — 사실. 글쓰기로 (이미 글쓰기면 시계만 민다)"""
        return self._signal("원고를 고쳤다")

    def heard(self, text: str) -> str:
        """사람이 한 말. 소설 이야기면 글쓰기로"""
        if text and self.words.search(text):
            return self._signal("소설 이야기를 꺼냈다")
        return ""

    def _signal(self, why: str) -> str:
        self.last_signal = time.time()
        if self.state != WRITING and not self.pinned:
            self.state, self.why, self.since = WRITING, why, time.time()
            return why
        return ""

    def tick(self) -> str:
        """시간이 지나 상태가 식었는지. 바뀌었으면 이유를, 아니면 빈 문자열"""
        if self.pinned or self.state != WRITING:
            return ""
        quiet = time.time() - self.last_signal
        if quiet >= self.hold_sec:
            self.state, self.why, self.since = TALK, f"{int(quiet / 60)}분째 조용함", time.time()
            return self.why
        return ""

    # ── 명령 ────────────────────────────────────────────────────────
    def pin(self, state: str, why: str = "직접 켬") -> None:
        self.state, self.why, self.since, self.pinned = state, why, time.time(), True
        self.last_signal = time.time()

    def unpin(self) -> None:
        self.pinned = False
        self.why = "자동으로 돌림"

    # ── 프롬프트·표시 ───────────────────────────────────────────────
    def writing(self) -> bool:
        return self.state == WRITING

    def status(self) -> dict:
        return {"state": self.state, "why": self.why, "pinned": self.pinned,
                "for_s": round(time.time() - self.since)}

    def situation_line(self) -> str:
        """[현재 상황]에 한 줄. 지금 무엇을 하는 중인지 캐릭터가 알게 한다"""
        if self.state != WRITING:
            return ""
        work = f"글 『{self.title}』" if self.title else "글"
        return (f"{character.OWNER} 님은 지금 {work}를 쓰는 중이고, 너는 그 원고를 같이 보고 있다. "
                "글의 흐름을 끊지 않는 게 먼저다 — 묻지 않으면 길게 말하지 않는다.")
