"""원고를 같이 읽는 상태 — 구글 문서(또는 웹 에디터 화면 OCR)를 주기적으로 읽어 새로 쓴 글만 가려낸다.

웹 에디터(구글독스·노션 등)에 쓰면 로컬 파일이 없다. 그래서 파일 감시가 아니라 구글 문서 API
(src/gdocs.py)나 화면 OCR이다 — 윈도우 내장 OCR은 VRAM을 쓰지 않고 2560x1440에서 1~2초라,
주기적으로 돌려도 대답이 느려지지 않는다 (VLM은 쓰지 않는다 — 대화 모델 옆에 3.5GB를 올리면
첫 토큰이 10초가 된다).

**말을 걸지 말아야 할 때 조용한 쪽으로 기운다.** 글 쓰는 중에 끼어드는 건 되돌릴 수 없고,
조용히 있어서 잃는 건 없다. 그래서 문턱이 넷이다: 새 글이 충분히 쌓였나 · 문장이 끝났나 ·
직전에 말한 지 충분히 지났나 · 사람이 지금 말하고 있지 않나.
"""
import re
import time

import character

NL = chr(10)

# 문장이 끝난 자리인가 — 쓰는 중간(조사나 어간에서 끊긴 자리)에 말을 걸면 생각을 끊는다
_SENT_END = re.compile(r'[.!?…"\'”’」』\)]\s*$|[다요죠까]\.?\s*$')
# "003화", "3 화", "제 12화" — 문서 이름이나 본문 첫머리에서 몇 화인지.
# (뒤에 한글이 오면 "화요일"·"화가" 같은 말이므로 제외)
_CHAPTER_RE = re.compile(r"(?:제\s*)?(\d{1,4})\s*화(?![가-힣])")
# 늘어난 글자 수와 "새 글"이 이만큼까지 어긋나는 건 봐준다 (구글 문서는 정확하지만
# 화면 OCR은 같은 글도 조금씩 다르게 읽는다)
_GROWTH_SLACK = 40


class Manuscript:
    """원고 세션 하나. 읽은 글과, 어디까지 캐릭터가 봤는지를 들고 있다"""

    def __init__(self, cfg: dict = None):
        cfg = cfg or {}
        # 어디서 원고를 가져오는가 — gdocs(구글 문서, 정확) 또는 screen(화면 OCR, 글자 크기 제약)
        self.provider = (cfg.get("provider") or "screen").lower()
        self.gdocs = cfg.get("gdocs") or {}
        # 작품 이름. 캐릭터 설정에 캐릭터 자신이 나오는 작품(other_work)이 있으면, 지금 쓰는
        # 작품을 밝히지 않을 때 둘을 섞는다
        self.title = (cfg.get("title") or "").strip()
        self.kind = (cfg.get("kind") or "글").strip()            # "웹소설", "에세이" 등 — 프롬프트에 쓰는 이름
        # 본문이 1인칭이면 서술자의 "나"를 자기로 읽는다 — 켜 두면 그걸 막는 한 줄을 넣는다
        self.first_person = bool(cfg.get("first_person", False))
        self.other_work = (cfg.get("other_work") or "").strip()
        self.aliases = [a for a in (cfg.get("aliases") or []) if a]
        self.source = cfg.get("source")            # screen.sources의 이름 (없으면 마지막 소스)
        self.max_chars = int(cfg.get("chars", 3000))
        # 화면에 그려진 글자 높이의 하한 — 이보다 작으면 읽지 않는다 (vision.read_text 주석에 실측)
        self.min_glyph_px = int(cfg.get("min_glyph_px", 24))
        # 음성 채널에 있는 동안 원고가 바뀌었는지 늘 살필지 — 글쓰기 상태로 들어오는 신호다
        self.watch = bool(cfg.get("watch", True))
        self.poll_sec = float(cfg.get("poll_sec", 20))
        self.quiet_min = float(cfg.get("quiet_sec", 180))      # 한 번 말하면 이만큼은 조용히
        self.min_new = int(cfg.get("min_new_chars", 150))      # 이만큼 새로 쓰여야 입을 열 생각을 한다
        self.on = False
        self.primed = False     # 한 번이라도 읽었는가 — 첫 읽기는 기준선이지 "새 글"이 아니다
        self.last_ident = ""    # 어느 문서를 보고 있었나 (바뀌면 기준선을 다시 잡는다)
        self.text = ""          # 지금 화면에서 읽은 원고 (프롬프트에 들어가는 것)
        self.seen = ""          # 그녀가 이미 말을 건 시점까지의 원고
        self.last_spoke = 0.0
        self.last_note = ""
        self.started = 0.0
        self.captures = 0
        self.spoke_count = 0

    # ── 상태 ────────────────────────────────────────────────────────
    def start(self, source: str = None, keep: bool = False) -> None:
        """세션을 연다. keep이면 이미 읽어 둔 원고를 버리지 않는다 —
        저절로 글쓰기로 들어갈 때는 방금 읽은 것을 그대로 이어 쓴다"""
        self.on = True
        self.started = time.time()
        self.last_spoke = time.time()   # 켜자마자 말을 걸지는 않는다
        if source:
            self.source = source
        if not keep:
            self.text = self.seen = ""
            self.captures = self.spoke_count = 0
            self.primed = False
            self.last_ident = ""

    def stop(self) -> None:
        self.on = False
        self.text = self.seen = ""
        self.primed = False
        self.last_ident = ""

    def status(self) -> dict:
        return {"on": self.on, "provider": self.provider, "source": self.source, "chars": len(self.text),
                "unseen_chars": len(self.new_text()), "captures": self.captures,
                "spoke": self.spoke_count, "note": self.last_note,
                "since_spoke_s": round(time.time() - self.last_spoke) if self.on else None}

    # ── 새로 쓴 글 ──────────────────────────────────────────────────
    def update(self, text: str, note: str = "") -> int:
        """방금 읽은 원고를 넣고, 새로 늘어난 글자 수를 돌려준다.

        **처음 읽는 것은 '방금 썼다'가 아니다.** 기준선만 잡고 0을 돌려준다 — 안 그러면
        서버를 켤 때 원고가 통째로 "새 글"이 되어, 글을 쓰고 있지도 않은데 글쓰기 상태로
        들어가고 이미 쓴 장을 가지고 말을 건다
        """
        # 보던 문서가 **바뀌면** 기준선을 다시 잡는다. 화가 넘어가거나 다른 파일을 열었을
        # 뿐인데 "방금 2,897자를 썼다"가 되어 장 전체를 두고 말을 건다
        # ('제목 없는 문서' → 진짜 제목으로 바뀌자 문서 전체가 새 글이 됐다)
        ident = (note or "").split("(")[0].strip()
        switched = bool(ident and self.last_ident and ident != self.last_ident)
        self.last_note = note
        if ident:
            self.last_ident = ident
        if not text:
            return 0
        self.captures += 1
        if switched:
            self.primed, self.text, self.seen = True, text, text
            return 0
        if not self.primed:
            self.primed = True
            self.text = self.seen = text
            return 0
        before = len(self.new_text())
        grew = len(text) - len(self.text)
        self.text = text
        added = len(self.new_text()) - before
        # **퇴고는 새로 쓴 게 아니다.** 꼬리 앵커가 고쳐진 구간에 걸리면 자리를 잃고 원고
        # 전체가 "새 글"로 보인다 — 앞 문단 오타 하나 고쳤는데 장 전체를 두고 말을 걸게 된다.
        # 실제로 늘어난 글자 수로 교차 검증해서, 어긋나면 조용히 기준선만 다시 잡는다
        if added > max(grew, 0) + _GROWTH_SLACK:
            self.seen = text
            self.last_note = (note or "") + " (고쳐 쓴 듯 — 기준선 다시 잡음)"
            return 0
        return added

    def new_text(self) -> str:
        """캐릭터가 아직 말을 걸지 않은 부분. OCR은 같은 화면도 조금씩 다르게 읽으므로
        글자 단위 diff가 아니라 '본 적 있는 꼬리' 뒤부터로 센다"""
        if not self.text:
            return ""
        if not self.seen:
            return self.text
        # 이미 본 원고의 끝 40자가 지금 글에 있으면 그 뒤가 새 글이다
        anchor = self.seen[-40:]
        at = self.text.rfind(anchor)
        if at >= 0:
            return self.text[at + len(anchor):]
        # 앵커를 못 찾으면 스크롤이 많이 움직인 것 — 겹치는 만큼만 빼고 본다
        return "" if self.text in self.seen else self.text

    def mark_seen(self) -> None:
        self.seen = self.text
        self.last_spoke = time.time()
        self.spoke_count += 1

    # ── 말을 걸 만한가 ──────────────────────────────────────────────
    def may_speak(self, busy: bool = False) -> tuple:
        """(말을 걸어도 되는가, 이유). 조용한 쪽으로 기운다"""
        # `on`은 보지 않는다 — 말을 걸지 말지는 **상태**(Activity)가 정하고 부르는 쪽이 건다.
        # 전에는 여기서 on을 봤는데, 원고가 바뀌어 저절로 글쓰기로 들어가면 start()를 거치지
        # 않아 on이 False로 남았고, 그래서 한마디를 영영 안 했다
        if not self.text:
            return False, "원고 없음"
        if busy:
            return False, "사람이 말하는 중"
        new = self.new_text()
        if len(new) < self.min_new:
            return False, f"새 글 {len(new)}자 (문턱 {self.min_new})"
        if not _SENT_END.search(new.rstrip()):
            return False, "문장이 끝나지 않았다 (쓰는 중)"
        quiet = time.time() - self.last_spoke
        if quiet < self.quiet_min:
            return False, f"말한 지 {int(quiet)}초 (문턱 {int(self.quiet_min)})"
        return True, f"새 글 {len(new)}자"

    # ── 작품·화 ─────────────────────────────────────────────────────
    def chapter(self) -> str:
        """몇 화인가 — 문서 이름에서, 없으면 본문 첫 줄에서.

        구글 문서 이름이 "제목 없는 문서"인 채로 쓰기 시작하는 일이 흔해서(실제로 그랬다),
        파일 이름만 믿으면 "제목 없는 문서"를 작품명으로 말한다
        """
        for where in (self.last_note or "", self.text[:120]):
            m = _CHAPTER_RE.search(where)
            if m:
                return f"{int(m.group(1))}화"
        return ""

    def what(self) -> str:
        """지금 보고 있는 것의 이름 — 「작품 N화」. 제목이 설정에 없으면 작품명 없이"""
        bits = [f"{self.kind} 『{self.title}』" if self.title else self.kind]
        ch = self.chapter()
        if ch:
            bits.append(ch)
        return " ".join(bits)

    # ── 프롬프트 ────────────────────────────────────────────────────
    def reading_block(self) -> tuple:
        """[지금 보고 있는 것] 칸에 넣을 (제목, 본문).

        **어느 작품인지 반드시 밝힌다.** 캐릭터 설정에 캐릭터 자신이 나오는 작품이 있으면
        (manuscript.other_work), 안 밝힐 때 원고를 보면서 자기 출신 설정 이야기로 받는다
        """
        owner = character.OWNER
        head = f"{owner} 님이 지금 쓰고 있는 {self.what()}의 원고다."
        if self.title and self.other_work:
            head += (f" 네가 나오는 『{self.other_work}』와는 **다른 작품**이다 — "
                     f"이 원고에 네 설정이나 네 이야기를 끌어오지 않는다.")
        # 1인칭으로 쓰인 원고면 서술자의 "저/나"를 자기로 읽는다 — 주인공이 한 일을
        # "제가 ~했어요"로 말하고, 바로잡아 줘도 다음 턴에 또 했다
        if self.first_person:
            head += (" 이 원고는 1인칭으로 쓰여 있다. 본문의 '나'와 '저'는 **이 글의 주인공**이고"
                     " 너가 아니다 — 주인공이 한 일을 '제가 했다'고 말하지 않고 '주인공이'라고 부른다.")
        # 원고를 읊는 것도 같은 자리에서 막는다 — 바깥 래퍼의 "통째로 읊지 말고"만으로는 224자를 읊었다
        head += (f" 등장인물 중 누구도 너나 {owner} 님이 아니다. 그리고 내용을 묻더라도 줄줄 읊지 않는다:"
                 " 한두 문장으로, 읽은 사람이 말하듯 짚는다.")
        return (head, self.text)

    def cue(self) -> str:
        """먼저 말을 걸 때 모델에게 줄 지시. 기록에 남길 말은 따로다 (take_initiative와 같은 이유)"""
        new = self.new_text()
        return ("(네가 먼저 원고에 대해 한마디 한다)" if new else "(네가 먼저 말을 꺼낸다)")

    def situation(self) -> str:
        new = self.new_text().strip()
        tail = new[-600:] if new else ""
        return (
            f"{character.OWNER} 님이 지금 {self.what()}를 쓰고 있고, 너는 그 원고를 같이 보고 있다. "
            "방금 새로 쓴 부분은 이것이다:" + NL + tail + NL
            + "이 부분에 대해 읽은 사람으로서 한마디 한다 — 한두 문장. "
            "맞춤법이나 문법을 고치지 말고, 요약하지도 말고, 칭찬만 하지도 않는다. "
            "눈에 걸린 것 하나를 짚거나, 궁금해진 것 하나를 묻거나, 네 감상을 말한다. "
            "글 쓰는 사람을 멈춰 세우는 질문(설명해 달라, 어떻게 할 거냐)은 하지 않는다. "
            "다음 전개를 네가 정해 주지도 않는다.")
