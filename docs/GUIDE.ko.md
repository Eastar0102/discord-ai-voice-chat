# 사용 가이드

[English](GUIDE.md) · **한국어** · [← README](../README.ko.md)

처음 설치부터 내 캐릭터를 만들어 굴리기까지 순서대로 정리했습니다.

- [1. 준비물](#1-준비물)
- [2. 디스코드 봇 만들기](#2-디스코드-봇-만들기)
- [3. 설치](#3-설치)
- [4. 모델 고르기](#4-모델-고르기)
- [5. 처음 실행하기](#5-처음-실행하기)
- [6. 대화하는 법 — 듣기 모드](#6-대화하는-법--듣기-모드)
- [7. 명령 전체](#7-명령-전체)
- [8. 컨트롤 앱](#8-컨트롤-앱)
- [9. 내 캐릭터 만들기](#9-내-캐릭터-만들기)
- [10. 내 목소리 쓰기 (GPT-SoVITS)](#10-내-목소리-쓰기-gpt-sovits)
- [11. 같이 보기 — 링크·화면·구글 문서](#11-같이-보기--링크화면구글-문서)
- [12. 전사 모드](#12-전사-모드)
- [13. 기억 다루기](#13-기억-다루기)
- [14. 설정 파일 안내](#14-설정-파일-안내)
- [15. 문제 해결](#15-문제-해결)

---

## 1. 준비물

| | 필요한 것 | 비고 |
|---|---|---|
| OS | Windows 10/11 | 리눅스에서도 음성 대화는 됩니다. 화면 보기(OCR)·`.bat`·컨트롤 앱의 일부는 윈도우 전용 |
| Python | 3.10 이상 | |
| Node.js | 18 이상 | 디스코드 봇과 컨트롤 앱 |
| GPU | NVIDIA, VRAM 8GB+ 권장 | Whisper large-v3-turbo ≈ 1.6GB. 로컬 8B LLM ≈ 5GB. CPU만으로도 돌지만 대답이 느립니다 |
| LLM | Ollama / llama.cpp / OpenAI 키 / Anthropic 키 중 하나 | [4. 모델 고르기](#4-모델-고르기) |
| 디스코드 | 봇 토큰, 봇을 초대할 서버 | [2. 디스코드 봇 만들기](#2-디스코드-봇-만들기) |

## 2. 디스코드 봇 만들기

봇 계정은 한 번만 만들면 됩니다. 순서대로 따라 하세요.

**2-1. 애플리케이션 만들기**
1. [Discord Developer Portal](https://discord.com/developers/applications)에 로그인합니다.
2. 오른쪽 위 **New Application**을 누르고 이름을 정합니다 (예: `Momo`). 이 이름이 디스코드에서 봇 이름으로 보입니다.
3. 왼쪽 **General Information**에서 아이콘을 올려 두면 디스코드에서 봇 프로필 사진이 됩니다 (선택).

**2-2. 토큰 받기**
1. 왼쪽 메뉴 **Bot**으로 갑니다.
2. **Reset Token** → **Yes, do it!**을 누르면 토큰이 한 번만 보입니다. **Copy**로 복사해 두세요.
   - 토큰은 봇 계정의 비밀번호입니다. 채팅·스크린샷·깃허브 어디에도 올리지 마세요.
   - 잃어버리면 다시 Reset Token을 누르면 됩니다 (이전 토큰은 무효가 됩니다).

**2-3. 인텐트 켜기**
1. 같은 **Bot** 페이지 아래쪽 **Privileged Gateway Intents**로 내려갑니다.
2. **Message Content Intent**를 켜고 **Save Changes**를 누릅니다.
   - 이게 꺼져 있으면 봇이 `!join` 같은 명령을 읽지 못합니다. 봇이 서버에 있는데 명령에 반응이 없으면 십중팔구 이것입니다.

**2-4. 서버에 초대하기**
1. 왼쪽 **OAuth2** → **URL Generator**로 갑니다.
2. **Scopes**에서 `bot`을 체크합니다.
3. 아래 **Bot Permissions**에서 7개를 체크합니다:

   | 권한 | 왜 필요한가 |
   |---|---|
   | View Channels | 채널을 보려면 |
   | Send Messages | 명령에 답하고 전사를 올리려면 |
   | Read Message History | 링크·명령 메시지를 읽으려면 |
   | Attach Files | 전사 `.txt`를 올리려면 |
   | Connect | 음성 채널에 들어가려면 |
   | Speak | 대답을 말하려면 |
   | Use Voice Activity | 음성 감지로 말하려면 |

   (권한 숫자로는 `36801536`입니다.)
4. 맨 아래 생성된 주소를 브라우저에 붙여 넣고, 봇을 넣을 서버를 골라 **승인**합니다.
5. 디스코드 서버 멤버 목록에 봇이 오프라인으로 보이면 성공입니다. 아직 프로그램을 안 켰으니 오프라인이 정상입니다.

> 처음 시험할 때는 혼자 있는 테스트 서버를 하나 만들어 쓰는 걸 권합니다. 다른 사람의 대화가 기억에 쌓이지 않고, 스크린샷에도 남의 이름이 나오지 않습니다.

<!-- 참고 이미지 자리: Developer Portal의 Bot 페이지(인텐트), OAuth2 URL Generator 화면.
     docs/images/guide-portal-bot.png, guide-portal-oauth.png 로 넣고 주석을 푼다.
![Bot 페이지 — Message Content Intent](images/guide-portal-bot.png)
![OAuth2 URL Generator — bot 범위와 권한](images/guide-portal-oauth.png)
-->

## 3. 설치

```bash
git clone https://github.com/Eastar0102/discord-ai-voice-chat.git
cd discord-ai-voice-chat

# Python 가상환경
python -m venv venv
venv\Scripts\activate                 # 리눅스/맥: source venv/bin/activate
pip install -r requirements.txt
```

**GPU를 쓰려면** PyTorch를 CUDA 빌드로 깔아야 합니다. `pip install -r requirements.txt`가 CPU 빌드를 깔았다면 [pytorch.org](https://pytorch.org/get-started/locally/)의 명령으로 다시 설치하세요 (예: `pip install torch --index-url https://download.pytorch.org/whl/cu121`). faster-whisper는 CUDA 12와 cuDNN 9가 필요합니다.

```bash
# 디스코드 봇
cd discord_bot
npm install
cd ..

# 컨트롤 앱 (선택)
cd app
npm install
cd ..
```

비밀값은 `.env`에 둡니다:

```bash
copy .env.example .env
```

```ini
DISCORD_TOKEN=여기에_봇_토큰
OPENAI_API_KEY=          # openai_chat을 쓸 때만
ANTHROPIC_API_KEY=       # claude를 쓸 때만
```

`.env`는 git이 무시합니다. 절대 커밋하지 마세요.

## 4. 모델 고르기

`config/config.json`의 `llm.provider`가 대화 모델을 정합니다. 실행 중에도 컨트롤 앱에서 바꿀 수 있고, 바꾼 것은 `config.json`에 저장됩니다.

### Ollama (기본, 로컬)

```bash
# https://ollama.com 에서 설치한 뒤
ollama pull qwen3:8b
```

```json
"llm": { "provider": "ollama", "ollama": { "model": "qwen3:8b", "num_ctx": 8192, "think": false } }
```

- 설치된 모델은 컨트롤 앱 목록에 자동으로 뜹니다. `ollama pull`만 하면 바로 고를 수 있습니다.
- `think: false`는 추론 모델(qwen3 등)이 생각 과정을 먼저 출력하지 않게 해서 첫 소리를 빠르게 합니다.
- Ollama가 꺼져 있으면 서버가 시작할 때 띄웁니다 (로컬 주소일 때만).
- **한 번에 하나만 VRAM에 올리세요.** 두 번째 모델을 올리면 앞의 것이 CPU로 밀려나 10배 느려집니다.
- 한국어 대화에는 한국어를 잘하는 모델을 고르세요. 작은 모델일수록 문장이 어색해지기 쉽습니다.

### llama.cpp (로컬, MoE 큰 모델)

Ollama로는 VRAM에 안 들어가는 MoE 모델(예: 30B-A3B)을 `--cpu-moe`로 전문가 층만 CPU에 두고 돌릴 때 씁니다.

```json
"llamacpp": {
  "server": "C:/llama.cpp/llama-server.exe",
  "model": "my-moe",
  "models": { "my-moe": "C:/models/my-moe-Q4_K_M.gguf" },
  "args": ["--cpu-moe", "-ub", "4096", "-b", "4096", "-t", "14"],
  "parallel": 2
}
```

- 서버를 자식 프로세스로 띄웁니다. 로그는 `data/logs/llamacpp.log`에 남습니다.
- `-ub`(마이크로 배치)가 `--cpu-moe`에서 첫 토큰 지연을 크게 줄입니다.
- `parallel: 2`는 대화용 슬롯과 판정·기억용 슬롯을 나눠, 서로의 캐시를 밀어내지 않게 합니다.

### OpenAI / Claude (클라우드)

```json
"llm": { "provider": "claude", "claude": { "model": "claude-haiku-4-5", "effort": "low", "cache": true } }
```

`.env`에 키를 넣으면 됩니다. 클라우드 모델은 대화의 맥락을 지키는 능력이 작은 로컬 모델보다 훨씬 낫습니다. 대신 프롬프트에 들어가는 모든 것(대화, 기억, 같이 읽는 문서)이 그 API로 갑니다.

### 기억 추출만 다른 모델로

대화 모델은 로컬로 두고, 백그라운드에서 도는 기억 정리만 더 똑똑한 모델로 돌릴 수 있습니다. 지연과 무관한 작업이라 느린 모델이어도 됩니다.

```json
"memory": { "summarizer_model": "claude:claude-haiku-4-5" }
```

## 5. 처음 실행하기

여기서는 컨트롤 앱으로 켜는 방법을 따라갑니다. 앱 없이 터미널로 켜는 방법은 [5-6](#5-6-터미널로-켜기-앱-없이)에 있습니다.

### 5-1. 실행 전 점검

| 확인 | 명령 | 이렇게 나오면 됨 |
|---|---|---|
| Python | `python --version` | `Python 3.10.x` 이상 |
| Node | `node --version` | `v18` 이상 |
| 패키지 | `venv\Scripts\python -c "import faster_whisper, chromadb, edge_tts"` | 아무것도 안 나옴 (오류 없음) |
| GPU (선택) | `venv\Scripts\python -c "import torch; print(torch.cuda.is_available())"` | `True` |
| Ollama (로컬 모델이면) | `ollama list` | 받아 둔 모델이 보임 |
| 토큰 | `.env`를 열어 봄 | `DISCORD_TOKEN=` 뒤에 값이 있음 |

### 5-2. 컨트롤 앱 열기

```bash
cd app
npm start
```

**Companion Control** 창이 뜹니다. 처음 열면 컴퓨터 언어에 맞춰 한국어나 영어로 뜹니다. 오른쪽 위 버튼으로 언어를 바꿀 수 있습니다.

### 5-3. 서버와 봇 켜기

![제어 탭 — 번호는 아래 순서](images/guide-control.png)

1. **① 한번에 시작 (Start all)** 을 누릅니다. API 서버가 먼저 뜨고, 준비되면 봇이 이어서 뜹니다.
2. **②** 의 두 점을 봅니다.

   | 점 | 뜻 |
   |---|---|
   | 회색 | 꺼짐 |
   | 노란색 | 켜지는 중 — 처음엔 Whisper·임베딩 모델을 내려받느라 몇 분 걸립니다 |
   | 초록색 | 준비됨. 봇 줄에 `Logged in: <봇 이름>`이 보입니다 |

   아래로 스크롤하면 서버 로그 창에 이런 줄들이 지나갑니다:

   ```
   로컬 Whisper 모델(...) 로딩 완료 (Device: cuda, Type: float16)
   Vector DB initialized. Collection: memory_example
   INFO:     Uvicorn running on http://127.0.0.1:8000
   LLM warmup: ollama/qwen3:8b (4.5s)
   ```

   `LLM warmup`이 보이면 첫 대답이 빨라질 준비까지 끝난 것입니다. 세계관(`lore.txt`)은 첫 대화 때 `[Lore] lore.txt를 다시 넣었습니다`가 한 번 찍히며 들어갑니다.

### 5-4. 음성 채널에 들어가기

1. 디스코드에서 **내가 먼저** 음성 채널에 들어갑니다.
2. 앱에서 **③ 서버**와 **④ 보이스 채널**을 고릅니다. 목록이 비어 있으면 옆의 ⟳를 누르세요.
3. **⑤ 참여 (Join)** 를 누릅니다. 봇이 음성 채널에 들어오고, 서버 이름 옆에 `✓ 참여 중`이 붙습니다.
4. **⑥ 듣기 모드**가 `자동`으로 켜져 있는지 봅니다 (기본값). 꺼져 있으면 `자동`을 누르세요.

> 디스코드 텍스트 채널에 `!join`을 쳐도 같습니다. 이때는 명령을 친 사람이 있는 음성 채널로 들어갑니다.

### 5-5. 첫 대화

1. 이름을 부르며 말을 겁니다: "**모모, 안녕?**"
2. 서버 로그에 이런 순서로 찍힙니다:

   ```
   [VAD] Speech started
   [듣는중] 모모 안녕
   [WS] STT Duration: 0.15s (partial 재사용) | endpoint=silence +0.60s
   [WS] User: 모모 안녕?
   [WS] Starting LLM stream...
   [TTS:edge] 1.03s, 15552 bytes (...)
   [WS] ⚡ Response Latency: 2400ms
   ```

   (시간 숫자는 예시입니다)

   | 줄 | 뜻 |
   |---|---|
   | `[VAD] Speech started` | 목소리를 들었다 |
   | `[듣는중]` | 말하는 동안 받아 적는 중 |
   | `User:` | 최종으로 알아들은 문장 |
   | `Response Latency` | 말이 끝나고 첫 소리가 나가기까지 |

3. 몇 초 뒤 봇이 대답합니다. 앱의 **대화** 탭에 주고받은 말이 채팅처럼 쌓이고, 대답마다 체감 지연이 색으로 붙습니다 (초록 2.5초 미만, 노랑, 빨강 4초 초과).

**대답이 안 나오면** 로그에서 어디서 멈췄는지 봅니다:

| 로그 상태 | 원인 | 할 일 |
|---|---|---|
| `[VAD]` 줄이 안 나옴 | 소리가 안 들어옴 | 봇의 `Connect`·`Use Voice Activity` 권한, 디스코드 입력 감도 |
| `User:`까지 나오고 멈춤 | LLM 문제 | Ollama가 떠 있는지, 모델 이름이 맞는지 |
| `TTS failed` | 목소리 엔진 문제 | `edge-tts`가 7.3.1 이상인지 (`pip install -U edge-tts`) |
| `🤫 듣는 중 (이유)` | 스스로 대답하지 않기로 함 | 정상 동작입니다. [6장](#6-대화하는-법--듣기-모드) 참고 |

### 5-6. 터미널로 켜기 (앱 없이)

```bash
start_all.bat
```

창 두 개가 뜹니다 (API 서버, 디스코드 봇). 따로 띄우려면:

```bash
venv\Scripts\python -m uvicorn src.api_server:app --host 127.0.0.1 --port 8000
cd discord_bot && node index.js
```

이 경우 앱에서 **캐릭터를 바꿀 때 서버를 자동으로 다시 시작하지 못합니다.** 앱은 자기가 띄운 서버만 다시 시작합니다. 앱을 쓸 거면 서버도 앱으로 켜는 게 편합니다.

> **같은 봇 토큰으로 두 군데서 켜지 마세요.** 앱의 "한번에 시작"과 터미널의 `node index.js`를 동시에 켜면 봇이 둘이 되어 명령에 두 번 반응합니다.

## 6. 대화하는 법 — 듣기 모드

| 모드 | 명령 | 언제 답하나 | 쓰는 때 |
|---|---|---|---|
| `auto` (기본) | `!auto` 또는 `!<캐릭터 이름>` | 스스로 판단합니다. 둘뿐이면 거의 다 답하고, 여럿이면 이름·2인칭·질문에 답합니다 | 평소 |
| `always` | `!always` | 들리는 말마다 | 1:1 테스트 |
| `call` | `!call` | 이름(호출어)을 불렀을 때만. 안 부른 말도 맥락으로는 기억합니다 | 게임·회의 중 |

알아 두면 좋은 동작:
- **끼어들기**: 캐릭터가 말하는 중에 말하면 멈추고 들습니다.
- **말 이어 붙이기**: "아니 근데…"처럼 이어질 것 같은 말에서 멈추면 잠깐 기다리고, 이어진 말과 합쳐 한 문장으로 답합니다.
- **먼저 말 걸기**: 조용하면 75초쯤 뒤 화제를 꺼냅니다. 대답이 없을수록 간격이 길어집니다 (최대 약 10분).
- **입장 인사**: 오랜만에 들어온 사람에게 인사하고, 기억하는 일(약속·일정)이 있으면 어떻게 됐는지 묻습니다.
- 답하지 않은 말은 `🤫 듣는 중 (이유)`로 표시되고, 컨트롤 앱의 상태 탭에 이유와 함께 남습니다.

## 7. 명령 전체

텍스트 채널에 칩니다.

**기본**

| 명령 | 동작 |
|---|---|
| `!join` | 내가 있는 음성 채널에 들어갑니다 |
| `!leave` | 나갑니다 (그때까지의 대화를 기억으로 정리) |
| `!auto` / `!<이름>` | 스스로 판단 모드 |
| `!always` | 모두 답함 |
| `!call` | 이름을 불러야 답함 |
| `!stop` | 듣기 중지 |
| `!talk <글>` | 말 대신 글로 보냅니다. 대답은 음성으로 |
| `ping` | 봇이 살아 있는지 확인 (느낌표 없이) |

**같이 보기**

| 명령 | 동작 |
|---|---|
| `!read <url>` (`!보기`, `!읽어`) | 기사를 읽어 둡니다. 음성 채널에 있으면 링크만 붙여도 됩니다. `!read`만 치면 치웁니다 |
| `!봐줘 [소스] [글자]` (`!look`) | 화면·창을 봅니다. `글자`를 붙이면 OCR만 써서 1~2초에 끝납니다 |
| `!소스` (`!sources`) | 볼 수 있는 소스와 지금 열린 창 목록 |

**전사**

| 명령 | 동작 |
|---|---|
| `!script [프리셋]` (`!transcribe`, `!기록`) | 전사 모드 시작 |
| `!scriptmodels` (`!기록모델`) | 프리셋 목록 |
| `!scriptstop` (`!기록중지`) | 끝내고 전체 전사 `.txt` 올리기 |

**글쓰기**

| 명령 | 동작 |
|---|---|
| `!원고 [소스]` | 글쓰기 상태로 고정하고 문서를 들고 있습니다 |
| `!대화` (`!원고중지`) | 대화 상태로 고정 |
| `!자동` | 고정을 풀고 문서 수정·글 이야기로 저절로 전환 |

## 8. 컨트롤 앱

```bash
cd app
npm start
```

탭은 여섯 개입니다. 탭마다 번호를 붙인 그림을 보면서 따라가세요.

### 8-1. 제어 탭 — 켜고, 들어가고, 고르기

![제어 탭](images/guide-control.png)

| 번호 | 이름 | 하는 일 |
|---|---|---|
| ① | 한번에 시작 | API 서버를 띄우고, 준비되면 봇까지 띄웁니다 |
| ② | 프로세스 | 서버·봇 상태 (회색 꺼짐 · 노랑 켜지는 중 · 초록 준비). 줄마다 따로 시작·중지할 수 있습니다 |
| ③ | 서버 | 봇이 초대된 디스코드 서버. `✓ 참여 중`이면 이미 음성 채널에 있습니다 |
| ④ | 보이스 채널 | 들어갈 음성 채널 |
| ⑤ | 참여 / 나가기 | 음성 채널 입장·퇴장. 나갈 때 그때까지의 대화를 기억으로 정리합니다 |
| ⑥ | 듣기 모드 | `자동` 스스로 판단 · `항상 반응` 모두 답함 · `호출어` 이름 불러야 · `듣기 중지` |
| ⑦ | 캐릭터 | 팩을 고르고 적용하면 서버를 다시 시작해 그 캐릭터로 바뀝니다. 기록과 기억은 캐릭터마다 따로입니다 |
| ⑧ | LLM 모델 | 클라우드(OpenAI·Claude)와 로컬(Ollama·llama.cpp) 모델 목록. 적용하면 바로 바뀌고 `config.json`에 저장됩니다. 로컬 모델은 VRAM 로딩까지 수 초~수십 초 걸립니다 |
| ⑨ | TTS 목소리 | Edge TTS 또는 GPT-SoVITS. GPT-SoVITS는 처음 고르면 별도 서버를 띄우느라 10~20초 걸립니다 |
| ⑩ | 언어 | 화면 언어 (한국어 / English). 서버·봇은 영향 없습니다 |

맨 아래에는 API 서버 로그와 봇 로그 창이 있습니다. 위쪽의 작은 버튼들(Processes, Discord …)을 누르면 그 패널로 바로 갑니다. 패널 제목을 누르면 접힙니다.

### 8-2. 보기 탭 — 화면과 링크

![보기 탭](images/guide-look.png)

| 번호 | 이름 | 하는 일 |
|---|---|---|
| ① | 소스 | 볼 곳. `config.json`의 `screen.sources`에 이름 붙인 것과, 지금 열린 창 목록이 함께 뜹니다 |
| ② | 글자만 | 켜면 OCR만 써서 1~2초에 끝납니다 (VRAM 0). 끄면 비전 모델(`qwen3-vl:4b`)이 화면 설명까지 해서 수십 초 걸립니다. 글자는 먼저 들어가니 그동안에도 대화할 수 있습니다 |
| ③ | 보기 | 지금 찍습니다. 찍힌 화면이 아래에 미리보기로 뜹니다 |
| ④ | 링크 | 기사·글 주소를 붙입니다 |
| ⑤ | 읽기 | 본문을 읽어 둡니다. 다음 대화부터 캐릭터가 그 글을 보고 말합니다 |
| ⑥ | 지금 보고 있는 것 | 캐릭터가 들고 있는 글·화면의 제목과 글자 수. 30분이 지나면 저절로 사라집니다 |
| ⑦ | 치우기 | 들고 있는 것을 지금 치웁니다 |

**해 보기**: 뉴스 기사 주소를 ④에 붙이고 ⑤ → 음성으로 "이 기사 어떻게 생각해?"

### 8-3. 전사 탭 — 회의록

![전사 탭](images/guide-transcribe.png)

| 번호 | 이름 | 하는 일 |
|---|---|---|
| ① | 텍스트 채널 | 받아 적은 줄을 실시간으로 올릴 채널. `올리지 않음`이면 파일로만 남깁니다 |
| ② | 모델 | Whisper 프리셋과 VRAM. `fast`는 게임과 같이 쓸 때, `accurate`(기본)는 정확도 우선 |
| ③ | 기록 시작 | 제어 탭에서 고른 음성 채널의 모든 사람을 받아 적기 시작합니다. 이때 캐릭터 듣기는 멈춥니다 |
| ④ | 기록 중지 | 끝내고 전체 기록을 `.txt`로 첨부합니다. `data/transcripts/`에도 남습니다 |

### 8-4. 대화 탭 — 실시간 대화

- 사람의 말은 왼쪽, 캐릭터의 말은 오른쪽에 쌓입니다.
- 캐릭터 말풍선 아래 숫자가 **체감 지연**입니다 (말이 끝난 순간 → 첫 소리). 마우스를 올리면 말끝·STT·생각·첫 소리까지가 나뉘어 보입니다.
- 점선 테두리 말풍선은 **대답하지 않은 말**이고, 옆에 이유가 붙습니다 (예: `듣기만 · 사람들끼리`).
- 모델이나 목소리를 바꾼 지점에는 가로줄이 그어집니다.
- 아래 입력창에 글을 치면 말한 것과 똑같이 처리되고, 캐릭터는 음성으로 대답합니다. 링크를 넣으면 읽습니다.

<!-- 참고 이미지 자리: docs/images/guide-live.png
![대화 탭](images/guide-live.png)
-->

### 8-5. 상태 탭 — 지금 머릿속

![상태 탭](images/guide-state.png)

| 번호 | 이름 | 무엇을 보나 |
|---|---|---|
| ① | 칩 | 지금 쓰는 LLM·온도·STT·TTS, 장기 기억 개수 |
| ② | 성격 프롬프트·말투 예시 | 캐릭터 팩의 `prompt.txt`, `topic.txt` 그대로 |
| ③ | 관심 키워드 | 기억과 최근 대화에 자주 나온 낱말 |
| ④ | 방금 참조한 기억 | 턴마다 무엇을 검색했고 무엇이 걸렸는지. `↳ 관련 기억 없음`이면 이번 말에 붙일 기억이 없었다는 뜻 |
| ⑤ | 장기 기억 | 저장된 기억 (있었던 일 · 사람 · 자기). 대화가 5분 멈추거나 봇이 나가면 쌓입니다 |
| ⑥ | 말할 차례 판단 | `답함` / `듣기만` / `먼저 말함`과 그 이유. 대답을 안 했으면 여기서 왜인지 봅니다 |
| ⑦ | 최근 대화 | 모델에게 들어가는 단기 기록 |

10초마다 저절로 갱신됩니다.

### 8-6. 기억 탭 — 기억 지도

- **벡터 지도**: 기억 하나가 점 하나입니다. 가까울수록 비슷한 이야기이고, 점을 누르면 같이 검색되는 기억이 보입니다. `덩어리 보기`(t-SNE)는 묶음이 잘 보이고, `거리 그대로`(PCA)는 실제 거리에 가깝습니다.
- **마인드맵**: 캐릭터 → 사람별 / 있었던 일 / 자기 자신으로 갈래를 나눠 보여 줍니다. 갈래 이름을 누르면 펼치고 접습니다.
- 위의 검색창으로 기억을 찾을 수 있습니다.

<!-- 참고 이미지 자리: docs/images/guide-map.png
![기억 탭](images/guide-map.png)
-->

### 8-7. 그 밖

- **언어**: 오른쪽 위 버튼으로 바꿉니다. 고른 언어는 기억되고, 바꾸면 화면만 다시 불러옵니다. 캐릭터가 말하는 언어가 아니라 앱 화면의 언어입니다.
- **캐릭터를 바꿀 때**: 앱이 띄운 서버만 자동으로 다시 시작합니다. 터미널로 띄운 서버라면 설정만 저장되니 직접 다시 시작하세요.
- **포터블 exe**: `npm run dist`로 만듭니다. exe는 프로젝트 폴더 안(`app/dist/`)에 두거나 `COMPANION_ROOT` 환경변수로 위치를 알려 주세요.

## 9. 내 캐릭터 만들기

```bash
xcopy /E /I characters\example characters\mychar
```

`config/config.json`에서 고르거나, 컨트롤 앱의 캐릭터 패널에서 고릅니다:

```json
"character": "mychar"
```

### `character.json`

```json
{
  "name": "모모",
  "wake_words": ["모모", "모모야", "momo"],
  "hotwords": "모모",
  "stt_fixes": { "모무": "모모" },
  "initiative_topics": ["네 취향 하나를 밝히고 상대는 어떤지 묻는다"]
}
```

| 키 | 의미 |
|---|---|
| `name` | 프롬프트·화면·기억에 쓰이는 이름 |
| `wake_words` | 들린 문장에 이 말이 있으면 "나를 불렀다". 잘못 들리는 꼴도 넣어 두면 좋습니다 |
| `hotwords` | Whisper에 주는 인식 힌트. 보통 이름 하나 |
| `stt_fixes` | 자주 잘못 들리는 꼴 → 바른 이름 (전사 직후 치환) |
| `initiative_topics` | 먼저 말을 걸 때 고르는 화제 (없으면 기본 목록) |

**이름 고르는 요령**: 일상어와 겹치는 이름(예: "하루")은 "하루 종일"에서도 불린 줄 압니다. 흔하지 않은 두세 글자가 좋습니다.

### `prompt.txt`

성격과 말하는 규칙입니다. 지켜야 할 것:

- `$TOPIC`, `$MEMORY`, `$LORE` 자리는 남겨 둡니다. 각각 말투 예시, 검색한 기억, 검색한 세계관이 들어갑니다. 바로 위에 `[제목]` 줄을 두는 형식을 유지하세요.
- **출력 형식**: 대답은 감정 태그 하나로 시작하고 `[importance: 0~3]`으로 끝납니다.

  ```
  (curious) 오늘 비 온다던데, 우산은 챙기셨어요? [importance: 0]
  ```

  감정 태그는 `neutral curious happy sad angry admiring sleepy` 중 하나이고, TTS 스타일을 고릅니다. 태그는 읽기 전에 지워집니다.
- 모든 말이 소리로 읽힌다는 점을 적어 두세요. 괄호 지문·이모지·목록·마크다운은 금지입니다. 들어와도 코드가 지우거나 끊지만, 프롬프트에서 막는 게 낫습니다.
- 메시지 앞의 `[이름]`은 누가 말했는지 알려 주는 표시입니다.

**잘 쓰는 요령**
- 금지 목록보다 **무엇을 하라**를 적으세요. 작은 모델은 "~라고 하지 마라"에 적힌 말을 오히려 따라 합니다.
- 대답마다 의견·취향·관찰 하나를 보태라고 하면 대화가 맞장구로 끝나지 않습니다.
- 비서 말투("무엇을 도와드릴까요?")를 막고 싶으면 명시적으로 적으세요.

### `topic.txt`

말투 예시 몇 줄입니다. **작은 모델은 예시를 그대로 베낍니다.** 테스트할 질문과 겹치지 않는 소재로 쓰고, 프롬프트에 "분위기만 참고하라"는 말이 붙어 있습니다.

### `lore.txt`

캐릭터의 배경 설정입니다. 매 턴 대화와 관련 있는 블록 두 개까지만 검색해 넣습니다.

```
[제목]
본문 한두 문단

[다른 제목]
본문
```

- 빈 줄로 블록을 나눕니다. `#`로 시작하는 줄은 주석입니다.
- 고친 뒤 서버를 다시 시작하면 바뀐 것을 저절로 다시 넣습니다. 서버를 끈 채로 바로 넣으려면 `python scripts/ingest_lore.py`를 실행합니다.

### 만든 사람 알려 주기

캐릭터가 누가 자기를 만들었는지 알게 하려면 디스코드 표시 이름을 적습니다:

```json
"identity": { "owner_names": ["내닉네임", "다른닉네임"] }
```

첫 항목이 프롬프트에서 그 사람을 부르는 이름이 됩니다.

### 개인 값 숨기기

`config/private.json`을 만들면 `config.json` 위에 겹쳐 읽습니다. 이 파일과 `example` 외의 캐릭터 팩은 git이 무시합니다:

```json
{ "identity": { "owner_names": ["내닉네임"] },
  "manuscript": { "title": "내 소설 제목", "gdocs": { "folder_id": "..." } } }
```

## 10. 내 목소리 쓰기 (GPT-SoVITS)

기본 TTS는 Edge TTS(`ko-KR-SunHiNeural`)입니다. 캐릭터만의 목소리를 쓰려면:

1. [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) v2Pro 통합 패키지를 받아 목소리 모델을 학습합니다. 쓸 권리가 있는 음성으로만 학습하세요.
2. 감정별 참조 음성(3~10초)과 그 대사를 준비해 캐릭터 팩 아래에 둡니다:

   ```
   characters/mychar/voice/
   ├─ voice.json
   ├─ neutral.wav
   ├─ happy.wav
   └─ ...
   ```

   ```json
   {
     "emotions": {
       "neutral": { "reference_wav": "neutral.wav", "line": "참조 음성에서 실제로 말한 문장" },
       "happy":   { "reference_wav": "happy.wav",   "line": "..." }
     }
   }
   ```

   `neutral`은 꼭 있어야 합니다. 없는 감정은 neutral로 대신합니다.
3. `config.json`:

   ```json
   "tts": {
     "provider": "gpt_sovits",
     "gpt_sovits": {
       "install_dir": "C:/GPT-SoVITS-v2pro",
       "gpt_weights": "GPT_weights_v2Pro/mychar-e15.ckpt",
       "sovits_weights": "SoVITS_weights_v2Pro/mychar_e10.pth",
       "voice_preset": "characters/mychar/voice"
     }
   }
   ```

서버가 GPT-SoVITS의 `api_v2.py`를 자식 프로세스로 띄웁니다 (로그 `data/logs/gpt_sovits.log`, VRAM 약 1.6GB).

- `aux_neutral`: 평소 음성을 보조 참조로 섞어, 감정이 바뀌어도 음색을 붙잡습니다.
- `emotion_weight` 3: 감정 대 평소 비율을 3:1로 섞습니다. 1:1이면 감정이 절반 넘게 옅어집니다.

## 11. 같이 보기 — 링크·화면·구글 문서

컨트롤 앱에서 하는 법은 [8-2 보기 탭](#8-2-보기-탭--화면과-링크)의 그림을 보세요. 여기서는 설정과 디스코드에서 쓰는 법을 다룹니다.

**링크**
- 음성 채널에 있을 때 텍스트 채널에 링크를 붙이면 기사 본문을 읽어 둡니다.
- 30분이 지나면 저절로 잊습니다.
- 사설 IP·localhost는 거부하고, 2MB·8초 상한이 있습니다.

**화면** (윈도우)
- `config.json`의 `screen.sources`에 볼 곳을 이름 붙여 둡니다:

  ```json
  "screen": { "default": "전체", "sources": {
    "전체": { "type": "screen", "max_width": 1280 },
    "브라우저": { "type": "window", "title": "Chrome|Edge", "focus": true },
    "왼쪽 위": { "type": "region", "bbox": [0, 0, 1280, 720] }
  } }
  ```

- 글자는 윈도우 내장 OCR로 읽습니다 (VRAM 0, 1~2초).
- 화면 묘사는 Ollama 비전 모델 `qwen3-vl:4b`가 합니다 (`ollama pull qwen3-vl:4b` 필요, 없으면 글자만 읽습니다). 대화 모델 옆에 올라가 있으면 대답이 크게 느려져서, 보고 나면 바로 내립니다. VRAM이 넉넉하면 `vision.keep_vlm: true`.
- 가려진 창은 그 자리에 있는 다른 창이 찍힙니다. `focus: true`로 앞으로 가져오세요.

**구글 문서** — 글을 쓰는 동안 옆에서 읽다가 가끔 한마디 합니다.
1. Google Cloud Console에서 OAuth 클라이언트(데스크톱 앱)를 만들고, `.env`에 `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`을 넣습니다.
2. `python scripts/google_login.py`로 한 번 연결합니다. 권한은 읽기 전용입니다(문서 본문 + 폴더 목록).
3. `config.json`의 `manuscript.gdocs.folder_id`(그 폴더의 가장 최근 문서를 따라감) 또는 `doc_id`를 적고, `manuscript.watch: true`로 켭니다.
4. 문서가 바뀌면 저절로 "글쓰기" 상태로 들어갑니다.
   - 새로 쓴 대목이 `min_new_chars`(150자)를 넘었을 때만 말합니다.
   - 그 대목이 문장으로 끝나야 하고, 직전에 말한 지 `quiet_sec`(180초)가 지나야 하고, 아무도 말하고 있지 않아야 합니다.

## 12. 전사 모드

컨트롤 앱에서 하는 법은 [8-3 전사 탭](#8-3-전사-탭--회의록)의 그림을 보세요.

회의록용으로, 어시스턴트와 **완전히 따로** 돕니다. 시작하면 듣기 모드가 멈추고, 그동안 캐릭터는 말하지 않습니다.

```
!script accurate
```

| 프리셋 | 모델 | 특징 |
|---|---|---|
| `fast` | large-v3-turbo, int8 | VRAM 1.6GB, 게임과 같이 쓸 때 |
| `balanced` | large-v3-turbo, float16 | |
| `accurate` (기본) | large-v3, beam 5 | VRAM 3.4GB |
| `max` | large-v3, beam 8 + 온도 폴백 | 가장 정확, 느림 |
| `cpu` | large-v3, CPU int8 | VRAM 0, 실시간 아님 |

- 말한 순서대로 정렬해 텍스트 채널에 몇 초마다 묶어 올립니다.
- `!scriptstop`에 전체 `.txt`를 첨부합니다. 파일은 `data/transcripts/`에도 남습니다.
- 시작할 때 대화 모델과 TTS의 VRAM을 내려놓고(`transcribe.free_assistant_vram`), 끝나면 다음 대답 때 다시 올립니다.

## 13. 기억 다루기

**언제 저장되나**: 대화가 5분(`episode_idle_sec`) 멈추거나, 봇이 나가거나, 사람 발화가 20줄(`episode_max_turns`) 쌓이면 그 덩어리를 정리합니다.

**무엇이 저장되나**

| 종류 | 예 | 프롬프트에서 |
|---|---|---|
| 있었던 일 (episode) | "루카가 면접 준비 이야기를 했다" | 시간 표현("어제", "지난번")·최근·관련도로 골라 넣음 |
| 사람별 사실 (person) | "9월 12일(금)에 면접이 있다" | 말하는 사람 것을 늘 넣음 (관련 있는 것 우선) |
| 자기 설정 (self) | "비 오는 날을 좋아한다고 했다" | 늘 넣음 — 말을 바꾸지 않게 |
| 겪은 일 (self/experience) | "루카에게 공모전에서 떨어졌다는 말을 들었다" | 최근 + 관련 |

- 사람은 **디스코드 사용자 ID**로 묶습니다. 닉네임이 바뀌어도 같은 사람입니다.
- 새 사실은 기존 사실과 비교해 추가/교체/무시합니다 ("면접이 있다" → "면접에 붙었다"는 교체).
- "다음 주 화요일"은 절대 날짜로 바꿔 저장합니다. 날짜가 지난 일정은 다음에 만났을 때 먼저 물어봅니다.
- Whisper가 확신하지 못한 줄(`min_stt_logprob` 미만)은 기억에서 뺍니다.

**들여다보기**
- 컨트롤 앱의 **상태**·**기억** 탭에서 볼 수 있습니다.
- 파일로 보려면 `python scripts/memory_map.py --tsne --open`을 실행합니다.

**지우기**
- 한 캐릭터만 처음부터 다시 하려면 서버를 끄고 `data/characters/<id>/`를 지웁니다. 다만 기억 DB(`data/chroma_db`)의 `memory_<id>` 컬렉션은 남습니다. 전부 지우려면 `data/` 폴더를 통째로 지웁니다 (기록·기억·지표·사람 목록이 모두 사라짐).

> 서버가 켜져 있는 동안 다른 프로세스로 `data/chroma_db`를 열지 마세요. 같은 저장소를 두 프로세스가 열면 읽기가 깨집니다.

## 14. 설정 파일 안내

`config/config.json`의 각 섹션에는 `_comment`로 설명이 붙어 있습니다. 자주 만지는 것:

| 키 | 기본 | 의미 |
|---|---|---|
| `character` | `example` | 쓸 캐릭터 팩 |
| `llm.provider` | `ollama` | 대화 모델 provider |
| `llm.prefetch` | true | 말하는 중 부분 전사로 프롬프트 캐시를 미리 데움 |
| `stt.whisper_local.model` | large-v3-turbo | 대화용 Whisper |
| `stt.name_hotwords` | true | 채널 사람 이름을 인식 힌트로 |
| `tts.provider` | `edge` | 목소리 엔진 |
| `tts.chunking` | `sentence` | TTS로 보낼 단위. `comma`는 첫 소리가 빠르고, `off`는 이음매가 없음 |
| `memory.judge` | true | auto 모드에서 규칙 위에 LLM 판정을 한 번 더 |
| `memory.engaged_sec` | 12 | 방금 대화한 상대의 다음 말은 판단 없이 답하는 시간 |
| `memory.initiative_after_sec` | [75] | 먼저 말을 걸기까지 기본 대기 |
| `memory.greet_after_hours` | 6 | 이만큼 오랜만이면 입장 인사 |
| `filters.ignore_phrases` | 방송 문구 등 | Whisper가 잡음에서 지어내는 말 거르기 |

## 15. 문제 해결

| 증상 | 확인할 것 |
|---|---|
| `!join`에 반응이 없음 | Message Content Intent가 켜졌는지, 봇에 채널 보기·메시지 권한이 있는지 |
| 들어오는데 말을 못 알아들음 | 서버 창에 `[듣는중]` 부분 전사가 찍히는지. 안 찍히면 봇의 `Connect`·`Use Voice Activity` 권한, 마이크 입력 감도 |
| 대답이 너무 느림 | 컨트롤 앱 대화 탭의 지연 툴팁에서 어느 단계가 긴지 봅니다. 생각이 길면 모델 크기나 VRAM 부족(모델이 CPU로 밀림), 목소리가 길면 `tts.chunking: comma` |
| 여럿이 있을 때 대답을 안 함 | auto 모드의 정상 동작입니다. 이름을 부르거나 `!always`. 이유는 상태 탭의 판단 기록에 있습니다 |
| 혼잣말에도 대답함 | `!call` 모드, 또는 `memory.judge: true` 확인 |
| 아무 말 안 했는데 이름만 불림 | 핫워드가 잡음에서 이름을 만들어 낸 것입니다. 이름 두 개 이상만 나온 전사는 자동으로 버립니다. 심하면 `stt.name_hotwords: false` |
| `CUDA` / `cudnn` 오류 | PyTorch CUDA 빌드와 cuDNN 9 설치. 안 되면 `stt.whisper_local.device: "cpu"`, `compute_type: "int8"` |
| Ollama 연결 거부 | `ollama serve`가 떠 있는지, `llm.ollama.base_url` |
| 기억 관련 `Error finding id` | 서버가 켜진 채 다른 스크립트가 `data/chroma_db`를 열었습니다. 하나만 열리게 하세요 |
| 세계관을 고쳤는데 반영이 안 됨 | 서버를 다시 시작 (바뀐 lore.txt를 저절로 다시 넣음) |
