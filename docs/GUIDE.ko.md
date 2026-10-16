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

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**에서 이름을 정해 만듭니다.
2. 왼쪽 **Bot** 메뉴로 갑니다.
   - **Reset Token**으로 토큰을 복사해 둡니다. 다시 볼 수 없으니 바로 `.env`에 넣으세요.
   - **Privileged Gateway Intents**에서 **Message Content Intent**를 켭니다. 이게 꺼져 있으면 `!join` 같은 명령을 읽지 못합니다.
3. 왼쪽 **OAuth2 → URL Generator**에서 초대 주소를 만듭니다.
   - Scopes: `bot`
   - Bot Permissions: `View Channels`, `Send Messages`, `Read Message History`, `Attach Files`(전사 파일 업로드), `Connect`, `Speak`, `Use Voice Activity`
4. 만들어진 주소로 봇을 내 서버에 초대합니다.

> 권한 숫자를 직접 쓰려면 `36801536`입니다 (위 7개의 합).

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

```bash
start_all.bat
```

창 두 개가 뜹니다.

**API 서버**
- 처음에는 Whisper 모델과 임베딩 모델을 내려받느라 몇 분 걸립니다.
- `Uvicorn running on http://127.0.0.1:8000`이 보이면 준비된 것입니다.
- 이어서 LLM과 TTS 워밍업 로그가 나옵니다.

**디스코드 봇**
- `Ready! Logged in as ...`가 보이면 연결된 것입니다.

따로 띄우려면:

```bash
python -m uvicorn src.api_server:app --host 127.0.0.1 --port 8000
cd discord_bot && node index.js
```

컨트롤 앱으로 둘 다 켜고 끌 수도 있습니다 ([8. 컨트롤 앱](#8-컨트롤-앱)).

**첫 대화**
1. 디스코드에서 음성 채널에 들어갑니다.
2. 아무 텍스트 채널에 `!join`을 칩니다. 봇이 내가 있는 음성 채널로 들어오고, 기본 듣기 모드(`auto`)로 시작합니다.
3. 말을 겁니다. "모모, 안녕?"
4. 서버 창에 `[WS] User: …` → `Response Latency: …ms`가 찍히고 대답이 들립니다.

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

| 탭 | 하는 일 |
|---|---|
| **제어** | API 서버·봇 켜고 끄기, 서버·음성 채널 골라 입장, 듣기 모드, **캐릭터·LLM 모델·TTS 목소리 바꾸기**, 실시간 로그 |
| **보기** | 화면·창 골라 보여 주기, 링크 읽히기, 지금 들고 있는 것 확인·치우기, 방금 찍은 화면 미리보기 |
| **전사** | 전사 모드 시작·중지, 프리셋 고르기 (VRAM 표시) |
| **대화** | 실시간 대화를 채팅처럼 표시합니다. 대답마다 **체감 지연**(말이 끝난 순간부터 첫 소리까지)이 색으로 붙고, 툴팁에 말끝·STT·생각·목소리 시간이 나뉩니다. 아래 입력창으로 글을 보낼 수 있습니다 |
| **상태** | 지금 프롬프트, 최근 기억 검색 결과, 장기 기억 목록, **말할 차례 판단 기록**(답하지 않은 말과 이유) |
| **기억** | 기억 지도: 벡터 지도(t-SNE/PCA)와 마인드맵. 점을 누르면 검색에 같이 걸리는 기억이 표시됩니다 |

오른쪽 위 **EN / 한국어** 버튼으로 화면 언어를 바꿉니다. 고른 언어는 기억됩니다. 바꾸면 화면을 다시 불러오지만, 서버와 봇은 그대로 돕니다. 캐릭터가 말하는 언어가 아니라 앱 화면의 언어입니다.

**캐릭터 바꾸기**: 제어 탭의 **캐릭터** 패널에서 팩을 고르고 적용합니다. 호출어·프롬프트·기억은 서버가 뜰 때 정해지므로, 앱이 서버를 다시 시작합니다. 앱이 띄운 서버가 아니면 설정만 저장하니 서버를 직접 다시 시작하세요. 대화 기록과 기억은 캐릭터마다 따로 쌓입니다 (`data/characters/<id>/`).

포터블 exe로 만들려면 `npm run dist`를 실행합니다. exe는 프로젝트 폴더 안(`app/dist/`)에 두거나 `COMPANION_ROOT` 환경변수로 위치를 알려 주세요.

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
