# Usage guide

**English** · [한국어](GUIDE.ko.md) · [← README](../README.md)

From a fresh install to running your own character, in order.

- [1. Requirements](#1-requirements)
- [2. Create the Discord bot](#2-create-the-discord-bot)
- [3. Install](#3-install)
- [4. Choose a model](#4-choose-a-model)
- [5. First run](#5-first-run)
- [6. Talking to it — listening modes](#6-talking-to-it--listening-modes)
- [7. All commands](#7-all-commands)
- [8. Control app](#8-control-app)
- [9. Make your own character](#9-make-your-own-character)
- [10. A custom voice (GPT-SoVITS)](#10-a-custom-voice-gpt-sovits)
- [11. Reading together — links, screen, Google Docs](#11-reading-together--links-screen-google-docs)
- [12. Transcription mode](#12-transcription-mode)
- [13. Memory](#13-memory)
- [14. Configuration reference](#14-configuration-reference)
- [15. Troubleshooting](#15-troubleshooting)

---

## 1. Requirements

| | needed | notes |
|---|---|---|
| OS | Windows 10/11 | Voice conversation also works on Linux. Screen OCR, the `.bat` launchers and parts of the control app are Windows-only |
| Python | 3.10+ | |
| Node.js | 18+ | Discord bot and control app |
| GPU | NVIDIA, 8 GB+ VRAM recommended | Whisper large-v3-turbo ≈ 1.6 GB, a local 8B LLM ≈ 5 GB. CPU-only works, with slow replies |
| LLM | one of Ollama / llama.cpp / an OpenAI key / an Anthropic key | [4. Choose a model](#4-choose-a-model) |
| Discord | a bot token and a server to invite it to | [2. Create the Discord bot](#2-create-the-discord-bot) |

## 2. Create the Discord bot

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Go to **Bot**:
   - **Reset Token** and copy the token. You can't see it again, so put it in `.env` right away.
   - Under **Privileged Gateway Intents**, enable **Message Content Intent**. Without it the bot can't read commands like `!join`.
3. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `View Channels`, `Send Messages`, `Read Message History`, `Attach Files` (transcript upload), `Connect`, `Speak`, `Use Voice Activity`
4. Open the generated URL to invite the bot to your server.

> As a permission integer: `36801536` (the sum of the seven above).

## 3. Install

```bash
git clone https://github.com/Eastar0102/discord-ai-voice-chat.git
cd discord-ai-voice-chat

python -m venv venv
venv\Scripts\activate                 # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
```

**For GPU**, install a CUDA build of PyTorch. If `pip install -r requirements.txt` gave you the CPU build, reinstall with the command from [pytorch.org](https://pytorch.org/get-started/locally/) (e.g. `pip install torch --index-url https://download.pytorch.org/whl/cu121`). faster-whisper needs CUDA 12 and cuDNN 9.

```bash
cd discord_bot && npm install && cd ..
cd app && npm install && cd ..        # optional: control app
```

Secrets go in `.env`:

```bash
copy .env.example .env
```

```ini
DISCORD_TOKEN=your_bot_token
OPENAI_API_KEY=          # only for openai_chat
ANTHROPIC_API_KEY=       # only for claude
```

`.env` is git-ignored. Never commit it.

## 4. Choose a model

`llm.provider` in `config/config.json` picks the chat model. You can also switch at runtime from the control app; the choice is saved back to `config.json`.

### Ollama (default, local)

```bash
# after installing from https://ollama.com
ollama pull qwen3:8b
```

```json
"llm": { "provider": "ollama", "ollama": { "model": "qwen3:8b", "num_ctx": 8192, "think": false } }
```

- Installed models show up in the control app automatically. `ollama pull` is all it takes.
- `think: false` stops reasoning models (qwen3 etc.) from printing their thinking first, so the first sound comes sooner.
- If Ollama is not running, the server starts it (local URL only).
- **Keep one model in VRAM at a time.** Loading a second model pushes the first partly to CPU and makes it ~10× slower.
- For Korean, choose a model that is good at Korean. Smaller models tend to produce awkward sentences.

### llama.cpp (local, large MoE models)

For MoE models that don't fit in VRAM through Ollama (e.g. 30B-A3B). `--cpu-moe` keeps only the expert layers on the CPU.

```json
"llamacpp": {
  "server": "C:/llama.cpp/llama-server.exe",
  "model": "my-moe",
  "models": { "my-moe": "C:/models/my-moe-Q4_K_M.gguf" },
  "args": ["--cpu-moe", "-ub", "4096", "-b", "4096", "-t", "14"],
  "parallel": 2
}
```

- The server runs as a child process, logging to `data/logs/llamacpp.log`.
- `-ub` (micro-batch) is what cuts time-to-first-token under `--cpu-moe`.
- `parallel: 2` gives the conversation and the judge/memory calls separate slots, so they don't evict each other's cache.

### OpenAI / Claude (cloud)

```json
"llm": { "provider": "claude", "claude": { "model": "claude-haiku-4-5", "effort": "low", "cache": true } }
```

Put the key in `.env`. Cloud models hold conversational context far better than small local ones. The trade-off: everything in the prompt goes to that API (conversation, memory, documents being read).

### A different model just for memory

Keep chat local and run only the background memory extraction on a stronger model. It isn't latency-sensitive, so a slow model is fine.

```json
"memory": { "summarizer_model": "claude:claude-haiku-4-5" }
```

## 5. First run

```bash
start_all.bat
```

Two windows open.

**API server**
- The first start downloads the Whisper and embedding models, which takes a few minutes.
- It's ready when you see `Uvicorn running on http://127.0.0.1:8000`, followed by the LLM and TTS warm-up lines.

**Discord bot**
- It's connected when you see `Ready! Logged in as ...`.

Or separately:

```bash
python -m uvicorn src.api_server:app --host 127.0.0.1 --port 8000
cd discord_bot && node index.js
```

The control app can also start and stop both ([8. Control app](#8-control-app)).

**First conversation**
1. Join a voice channel in Discord.
2. Type `!join` in any text channel. The bot joins your voice channel and starts in the default `auto` mode.
3. Speak, e.g. "모모, 안녕?"
4. The server window prints `[WS] User: …` → `Response Latency: …ms`, and you hear the reply.

## 6. Talking to it — listening modes

| mode | command | answers when | use it for |
|---|---|---|---|
| `auto` (default) | `!auto` or `!<character name>` | It decides. With one person it answers nearly everything; with several, it answers its name, "you" and questions | normal use |
| `always` | `!always` | every utterance | 1:1 testing |
| `call` | `!call` | only when called by name. Other speech is still kept as context | gaming, meetings |

Behaviour worth knowing:
- **Barge-in**: speak while it's talking and it stops to listen.
- **Joining fragments**: if you pause mid-sentence ("아니 근데…"), it waits briefly and answers the joined sentence.
- **Speaking first**: after ~75 s of quiet it brings up a topic. The interval grows each time it's ignored, up to ~10 min.
- **Greetings**: when someone who hasn't been around for a while joins, it greets them. If it remembers a plan of theirs, it asks how it went.
- Utterances it lets pass show as `🤫 듣는 중 (reason)`, and are logged with the reason in the control app's state tab.

## 7. All commands

Type these in a text channel.

**Basics**

| command | action |
|---|---|
| `!join` | join your voice channel |
| `!leave` | leave (the conversation so far is saved to memory) |
| `!auto` / `!<name>` | decide-for-itself mode |
| `!always` | answer everything |
| `!call` | only when called by name |
| `!stop` | stop listening |
| `!talk <text>` | send text instead of speech; the reply is spoken |
| `ping` | is the bot alive (no exclamation mark) |

**Reading together**

| command | action |
|---|---|
| `!read <url>` (`!보기`, `!읽어`) | read an article. While in voice, pasting a bare link also works. `!read` alone clears it |
| `!봐줘 [source] [글자]` (`!look`) | look at the screen or a window. Add `글자` for OCR only (1–2 s) |
| `!소스` (`!sources`) | list named sources and currently open windows |

**Transcription**

| command | action |
|---|---|
| `!script [preset]` (`!transcribe`, `!기록`) | start transcription mode |
| `!scriptmodels` (`!기록모델`) | list presets |
| `!scriptstop` (`!기록중지`) | stop and upload the full transcript as `.txt` |

**Writing**

| command | action |
|---|---|
| `!원고 [source]` | pin the "writing" state and hold the document |
| `!대화` (`!원고중지`) | pin the "conversation" state |
| `!자동` | unpin; document edits and talk about writing switch it automatically |

## 8. Control app

```bash
cd app
npm start
```

| tab | what it does |
|---|---|
| **제어** (control) | start/stop the API server and the bot, join a server/voice channel, listening mode, **switch character, LLM model and TTS voice**, live logs |
| **보기** (look) | show it the screen or a window, give it a link, see/clear what it is holding, preview the last capture |
| **전사** (transcription) | start/stop transcription mode, choose a preset (with VRAM) |
| **대화** (conversation) | the live conversation as a chat. Each reply carries its **felt latency** (from the moment you stopped speaking to the first sound), color-coded, with endpoint / STT / thinking / voice in the tooltip. You can also type into it |
| **상태** (state) | the current prompt, recent memory retrievals, long-term memory, and the **turn-taking log** (what it let pass, and why) |
| **기억** (memory) | memory map: a vector map (t-SNE/PCA) and a mind map. Click a point to highlight what would be retrieved with it |

The **EN / 한국어** button at the top right switches the UI language. The choice is remembered. Switching reloads the window; the server and bot keep running. This is the app's language, not the language the character speaks.

**Switching characters**: pick a pack in the **Character** panel of the control tab and apply. Wake words, prompts and memory are fixed when the server starts, so the app restarts the server. If the app didn't start the server, it only saves the setting; restart the server yourself. Conversation history and memory are kept per character (`data/characters/<id>/`).

`npm run dist` builds a portable exe. Keep it inside the project (`app/dist/`), or point `COMPANION_ROOT` at the project.

## 9. Make your own character

```bash
xcopy /E /I characters\example characters\mychar
```

Select it in `config/config.json`, or from the control app's Character panel:

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
  "initiative_topics": ["share one of your tastes, then ask about theirs"]
}
```

| key | meaning |
|---|---|
| `name` | used in prompts, the UI and memory |
| `wake_words` | if a transcript contains one, it was called. Add common mishearings too |
| `hotwords` | recognition hint for Whisper, usually just the name |
| `stt_fixes` | common mishearing → correct name (replaced right after STT) |
| `initiative_topics` | topics to pick from when speaking first (defaults if empty) |

**Choosing a name**: a name that is also an everyday word will trigger on ordinary speech. An uncommon two- or three-syllable name works best.

### `prompt.txt`

Personality and speaking rules. Keep these intact:

- **The `$TOPIC`, `$MEMORY` and `$LORE` slots.** They receive tone examples, retrieved memory and retrieved lore. Keep the `[heading]` line directly above each one.
- **The output format.** A reply starts with one emotion tag and ends with `[importance: 0~3]`:

  ```
  (curious) 오늘 비 온다던데, 우산은 챙기셨어요? [importance: 0]
  ```

  The emotion tag is one of `neutral curious happy sad angry admiring sleepy` and picks the TTS style. Tags are removed before speech.
- **Everything is spoken.** Say so in the prompt, and ban stage directions, emoji, lists and markdown. The code strips or cuts them anyway, but it's better if they never appear.
- **`[name]` before a message** tells the model who said it.

**Tips**
- Write what to do, not lists of what not to do. Small models often repeat the very phrases a "don't say …" rule names.
- Asking for one opinion, taste or observation per reply keeps the conversation from dying in agreement.
- If you don't want assistant phrasing ("How can I help you?"), say so explicitly.

### `topic.txt`

A few example lines for tone. **Small models copy examples verbatim**, so pick subjects that don't overlap with what you'll test. The prompt marks this section as tone-only.

### `lore.txt`

Background. Each turn, up to two blocks relevant to what was said are retrieved into the prompt.

```
[Title]
One or two paragraphs.

[Another title]
Text.
```

- Blank lines separate blocks; lines starting with `#` are comments.
- Restart the server after editing and changed lore is re-ingested automatically. To ingest with the server stopped, run `python scripts/ingest_lore.py`.

### Telling it who built it

To let the character know who its creator is, put your Discord display name(s) here:

```json
"identity": { "owner_names": ["MyNick", "OtherNick"] }
```

The first entry is the name the prompt uses for you.

### Keeping personal values out of git

`config/private.json` is layered over `config.json`. It, and every character pack except `example`, is git-ignored:

```json
{ "identity": { "owner_names": ["MyNick"] },
  "manuscript": { "title": "My novel", "gdocs": { "folder_id": "..." } } }
```

## 10. A custom voice (GPT-SoVITS)

The default TTS is Edge TTS (`ko-KR-SunHiNeural`). For the character's own voice:

1. Get the [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) v2Pro package and train a voice model. Only use audio you have the rights to.
2. Prepare a 3–10 s reference clip per emotion, with its transcript, inside the character pack:

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
       "neutral": { "reference_wav": "neutral.wav", "line": "exactly what is said in the clip" },
       "happy":   { "reference_wav": "happy.wav",   "line": "..." }
     }
   }
   ```

   `neutral` is required; missing emotions fall back to it.
3. In `config.json`:

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

The server launches GPT-SoVITS's `api_v2.py` as a child process (log in `data/logs/gpt_sovits.log`, ~1.6 GB VRAM).

- `aux_neutral` mixes in the neutral clip as an auxiliary reference, so the timbre holds when the emotion changes.
- `emotion_weight` 3 mixes emotion and neutral 3:1. At 1:1 more than half of the emotion washes out.

## 11. Reading together — links, screen, Google Docs

**Links**
- While it's in voice, paste a link in a text channel and it reads the article.
- It forgets the article after 30 minutes.
- Private IPs and localhost are refused, with 2 MB / 8 s limits.

**Screen** (Windows)
- Name what it can look at in `screen.sources`:

  ```json
  "screen": { "default": "전체", "sources": {
    "전체": { "type": "screen", "max_width": 1280 },
    "browser": { "type": "window", "title": "Chrome|Edge", "focus": true },
    "top-left": { "type": "region", "bbox": [0, 0, 1280, 720] }
  } }
  ```

- Text is read with built-in Windows OCR (0 VRAM, 1–2 s).
- A description comes from the Ollama vision model `qwen3-vl:4b` (`ollama pull qwen3-vl:4b`; without it, text only). Next to a loaded chat model it slows replies badly, so it's unloaded after each look. With spare VRAM set `vision.keep_vlm: true`.
- A covered window captures whatever is in front of it; use `focus: true`.

**Google Docs** — it reads along while you write and says something now and then.
1. Create an OAuth client (desktop app) in Google Cloud Console and put `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env`.
2. Link once with `python scripts/google_login.py`. Scopes are read-only (document bodies + folder listing).
3. Set `manuscript.gdocs.folder_id` (follows the most recently edited doc in that folder) or `doc_id`, and set `manuscript.watch: true`.
4. When the doc changes, it enters the "writing" state on its own. It speaks only when all of these hold:
   - at least `min_new_chars` (150) new characters,
   - the new text ends a sentence,
   - `quiet_sec` (180 s) have passed since it last spoke,
   - nobody is talking.

## 12. Transcription mode

A meeting logger that runs **fully separate** from the assistant. Starting it stops listening; the character stays silent meanwhile.

```
!script accurate
```

| preset | model | notes |
|---|---|---|
| `fast` | large-v3-turbo, int8 | 1.6 GB VRAM, alongside a game |
| `balanced` | large-v3-turbo, float16 | |
| `accurate` (default) | large-v3, beam 5 | 3.4 GB VRAM |
| `max` | large-v3, beam 8 + temperature fallback | most accurate, slow |
| `cpu` | large-v3, CPU int8 | 0 VRAM, not real-time |

- Lines are kept in speaking order and posted to the text channel in batches every few seconds.
- `!scriptstop` attaches the full `.txt`; files are also kept in `data/transcripts/`.
- Starting a session frees the chat model's and TTS's VRAM (`transcribe.free_assistant_vram`). They reload on the next reply.

## 13. Memory

**When it saves**: when the conversation pauses for 5 min (`episode_idle_sec`), the bot leaves, or 20 user lines pile up (`episode_max_turns`).

**What it saves**

| kind | example | in the prompt |
|---|---|---|
| episode | "Luka talked about interview prep" | chosen by time words ("yesterday", "last time"), recency and relevance |
| person fact | "has an interview on Fri, Sep 12" | always, for the current speaker (relevant ones first) |
| self fact | "said it likes rainy days" | always, so it doesn't contradict itself |
| experience | "heard from Luka that the contest didn't work out" | recent + relevant |

- People are keyed by **Discord user ID**, so a nickname change keeps the same person.
- New facts are compared with old ones and added, replaced or skipped ("has an interview" → "passed the interview" is a replace).
- "Next Tuesday" is stored as an absolute date. A plan whose date has passed is asked about first the next time they meet.
- Lines Whisper was unsure about (`min_stt_logprob`) are left out of memory.

**Inspecting**
- Use the control app's **상태** and **기억** tabs.
- As a file: `python scripts/memory_map.py --tsne --open`.

**Resetting**
- To reset one character, stop the server and delete `data/characters/<id>/`. Its `memory_<id>` collection in the memory DB (`data/chroma_db`) remains, though. To wipe everything, delete the whole `data/` folder (history, memory, metrics and the people list all go).

> Don't open `data/chroma_db` from another process while the server is running. Two processes on the same store break reads.

## 14. Configuration reference

Every section of `config/config.json` carries a `_comment`. The ones you'll touch most:

| key | default | meaning |
|---|---|---|
| `character` | `example` | which character pack |
| `llm.provider` | `ollama` | chat model provider |
| `llm.prefetch` | true | warm the prompt cache from partial transcripts while you speak |
| `stt.whisper_local.model` | large-v3-turbo | Whisper for conversation |
| `stt.name_hotwords` | true | feed channel members' names to Whisper |
| `tts.provider` | `edge` | voice engine |
| `tts.chunking` | `sentence` | TTS unit. `comma` gives a faster first sound, `off` gives no seams |
| `memory.judge` | true | in auto mode, an LLM judge on top of the rules |
| `memory.engaged_sec` | 12 | the last person it answered gets answered without judging for this long |
| `memory.initiative_after_sec` | [75] | base wait before speaking first |
| `memory.greet_after_hours` | 6 | greet people who have been away this long |
| `filters.ignore_phrases` | broadcast phrases etc. | drop things Whisper invents from noise |

## 15. Troubleshooting

| symptom | check |
|---|---|
| no response to `!join` | Message Content Intent enabled; the bot can see the channel and send messages |
| joins but doesn't understand | does the server window show `[듣는중]` partial transcripts? If not: the bot's `Connect` / `Use Voice Activity` permissions, your mic input level |
| replies are slow | the latency tooltip in the conversation tab shows which stage. Long thinking → model size or VRAM shortage (model spilled to CPU); long voice → `tts.chunking: comma` |
| doesn't answer in a group | expected in auto mode. Call its name or use `!always`. The reason is in the state tab's turn log |
| answers people talking to themselves | use `!call`, or check `memory.judge: true` |
| its name "heard" from silence | hotwords make noise decode as names. Transcripts made only of 2+ names are dropped automatically; if it persists, `stt.name_hotwords: false` |
| `CUDA` / `cudnn` errors | CUDA build of PyTorch and cuDNN 9. Or fall back: `stt.whisper_local.device: "cpu"`, `compute_type: "int8"` |
| Ollama connection refused | is `ollama serve` running; `llm.ollama.base_url` |
| memory `Error finding id` | another script opened `data/chroma_db` while the server is up. Keep it to one process |
| lore edits not reflected | restart the server (changed lore.txt is re-ingested automatically) |
