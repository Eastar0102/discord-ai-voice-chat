"""
링크를 받아 본문 글자만 남긴다 — 캐릭터가 기사를 같이 볼 수 있게.

도구가 하나도 없으면 뉴스 얘기를 할 때 사람이 눈 역할을 해야 하고, 정작 "어떻게 생각해?"에는
"그건 저도 잘 모르겠어요"가 나왔다 — 기사를 못 봤으니 당연하다.

기사는 그림이 아니라 글자라서 눈(VLM)이 필요 없다. URL → 본문이면 된다.

**의존성을 늘리지 않는다.** bs4나 trafilatura를 깔아도 어려운 부분(본문이 어디냐)은 그대로
남으므로, 표준 `html.parser`로 태그를 걷고 글자가 가장 촘촘한 덩어리를 고른다. 뉴스·블로그
정도에는 충분하고, 안 되는 사이트가 있으면 그때 파서를 늘리는 게 순서다.

**아무 URL이나 받는다는 건 위험하다** — 채널의 누구나 링크를 붙일 수 있다. 그래서
http/https만, 사설·loopback 주소는 거부, 응답 크기와 시간에 상한을 둔다. 리다이렉트도
매번 같은 검사를 거친다.
"""

import ipaddress
import re
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

MAX_BYTES = 2 * 1024 * 1024      # 2MB — 기사 한 편에 이보다 클 이유가 없다
TIMEOUT = 8.0
MAX_CHARS = 4000                 # 프롬프트에 넣을 상한 (num_ctx 8192을 기사로 다 쓰지 않게)
USER_AGENT = "Mozilla/5.0 (compatible; VoiceCompanion/1.0)"

# 본문일 리 없는 것
_DROP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form",
              "noscript", "svg", "button", "select", "iframe"}
# 글이 담기는 곳
_TEXT_TAGS = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "dd", "figcaption"}
_BLANKS_RE = re.compile(r"[ \t ]+")
_NEWLINES_RE = re.compile(r"\n{3,}")


class _Extract(HTMLParser):
    """태그를 걷고 문단만 모은다. 제목은 <title>이나 첫 <h1>에서."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks = []
        self._skip = 0
        self._buf = []
        self._in_text = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _TEXT_TAGS:
            self._in_text += 1
        elif tag == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _TEXT_TAGS:
            self._in_text = max(0, self._in_text - 1)
            if not self._in_text:
                self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title and not self.title:
            self.title = data.strip()
        elif self._in_text:
            self._buf.append(data)

    def _flush(self):
        text = _BLANKS_RE.sub(" ", "".join(self._buf)).strip()
        self._buf = []
        if len(text) >= 2:
            self.blocks.append(text)

    def close(self):
        super().close()
        self._flush()


def _safe_url(url: str) -> str:
    """http/https만, 사설·loopback 주소는 거부. 통과한 URL을 돌려준다"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"http(s) 주소만 읽습니다 (받은 것: {u.scheme or '없음'})")
    if not u.hostname:
        raise ValueError("주소에 호스트가 없습니다")
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except OSError as e:
        raise ValueError(f"주소를 찾지 못했습니다: {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # 내부망으로 요청을 보내게 하면 안 된다 (SSRF) — 링크는 채널의 누구나 붙일 수 있다
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"내부 주소는 읽지 않습니다 ({ip})")
    return url


def fetch(url: str) -> dict:
    """{url, title, text, chars} — 실패하면 ValueError/httpx 예외를 그대로 올린다"""
    url = _safe_url(url)
    with httpx.Client(follow_redirects=False, timeout=TIMEOUT,
                      headers={"User-Agent": USER_AGENT, "Accept-Language": "ko,en"}) as c:
        # 리다이렉트를 직접 따라간다 — 목적지도 같은 검사를 거쳐야 한다
        for _ in range(5):
            r = c.get(url)
            if r.status_code not in (301, 302, 303, 307, 308):
                break
            nxt = r.headers.get("location")
            if not nxt:
                break
            url = _safe_url(httpx.URL(url).join(nxt).__str__())
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            raise ValueError(f"글이 아닌 것 같습니다 ({ctype or '형식 불명'})")
        body = r.content[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")

    p = _Extract()
    p.feed(body)
    p.close()
    # 문단 중 긴 것들만 — 메뉴·저작권 줄은 짧다. 순서는 원문 그대로 둔다
    blocks = [b for b in p.blocks if len(b) >= 20]
    if not blocks:
        blocks = p.blocks
    text = _NEWLINES_RE.sub("\n\n", "\n".join(blocks)).strip()
    title = unescape(p.title or "")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n…(뒷부분 생략)"
    if not text:
        raise ValueError("본문을 찾지 못했습니다 (자바스크립트로 그리는 페이지일 수 있습니다)")
    return {"url": url, "title": title, "text": text, "chars": len(text)}
