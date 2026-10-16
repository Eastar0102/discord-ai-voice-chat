# Discord AI Voice Chat

**English** · [한국어](README.ko.md)

**A reference pipeline for real-time AI voice conversation in Discord voice channels.**

Someone speaks in a voice channel. A few seconds later an AI character answers out loud. It decides on its own whether it was being spoken to, remembers people across sessions, and starts a conversation when the room goes quiet.

This repository is **not a finished character or a hosted bot**. It is the plumbing between Discord audio and an LLM, with each stage replaceable:

- **The engine is yours.** Speech-to-text, the language model and the voice are all providers you choose in one config file. Local (Ollama, llama.cpp) and cloud (OpenAI, Claude) LLMs are both supported.
- **The character is a folder.** Name, personality, background and wake words live in `characters/<name>/`. Copy the sample, edit text files, done.
- **The hard parts are already solved.** Endpointing on Discord's bursty audio, deciding when *not* to answer, barge-in, sentence-level streaming to TTS, and memory that doesn't fill up with junk. Each solution is documented with the failure it fixed, in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

> **Korean-first.** Prompts, turn-taking rules (sentence endings, particles), memory extraction and date parsing are written for Korean. The pipeline itself is language-agnostic; [ARCHITECTURE.md](docs/ARCHITECTURE.md#adapting-to-another-language) lists what to change for another language.

## The pipeline

![Pipeline](docs/images/pipeline.svg)

| stage | what it does | where | swap it by |
|---|---|---|---|
| **Capture** | joins the voice channel, receives Opus per speaker, forwards 48 kHz PCM over a WebSocket, plays replies back | `discord_bot/voice_handler.js` | — |
| **Endpointing** | WebRTC VAD + energy gate; decides when an utterance has ended (silence, stalled stream, or a finished sentence in the partial transcript) | `src/vad.py`, `src/websocket_handler.py` | thresholds in code |
| **STT** | faster-whisper, run repeatedly *while you speak* so the final transcript is often ready the moment you stop; names in the channel are fed in as hotwords | `src/providers/stt/` | `stt.provider` |
| **Turn-taking** | should I answer this? head count, name, second person and question endings first; an optional LLM judge runs in parallel with the reply | `src/websocket_handler.py`, `src/llm.py` | `memory.judge`, listening mode |
| **Prompt** | character pack + retrieved memory + retrieved lore + live situation (who is here, since when) + normalized history + clock | `src/llm.py` | `characters/<name>/` |
| **LLM** | streams the reply; a copy of a recent reply is caught and regenerated before any audio is sent | `src/providers/llm/` | `llm.provider` |
| **TTS** | sentence-by-sentence synthesis; the leading emotion tag picks the voice style | `src/providers/tts/` | `tts.provider` |
| **Memory** | after each conversation episode: what happened, facts per person (keyed by Discord ID), what the character said about itself | `src/memory.py` | `memory.*` |

Around the main loop are a few extras. It greets people who join and speaks first when it's quiet. It can read a link, the screen, or a Google Doc together with you. It has a separate meeting-transcription mode. And an Electron control app shows every turn with its latency split.

## Screenshots

<!--
  Add images to docs/images/ and uncomment.
  Avoid real names, avatars and server names in the shots — the control app shows them.
-->
<!-- ![Control app — live conversation](docs/images/control-live.png) -->
<!-- ![Control app — turn-taking log and memory](docs/images/control-state.png) -->
<!-- ![Memory map](docs/images/memory-map.png) -->
<!-- ![Discord — commands and transcription](docs/images/discord.png) -->

## Project structure

```
discord-ai-voice-chat/
├─ discord_bot/                Node.js — Discord side
│  ├─ index.js                 text commands (!join, !auto, !read, !script …)
│  ├─ voice_handler.js         voice join/leave, PCM capture, playback queue, barge-in
│  ├─ transcribe_handler.js    meeting-transcription mode (separate path, no assistant)
│  └─ control_server.js        localhost API the control app uses (guilds, join, mode, talk)
├─ src/                        Python — everything that thinks
│  ├─ api_server.py            FastAPI app: WebSocket /ws, REST for the control app
│  ├─ websocket_handler.py     the turn loop: endpointing, STT, gate, streaming, TTS queue,
│  │                           greetings, initiative, memory flush
│  ├─ llm.py                   prompt assembly, history normalization, judge, tag parsing
│  ├─ memory.py                episode extraction, fact reconciliation, retrieval
│  ├─ character.py · character_api.py   loads the character pack · lists/switches packs
│  ├─ lore.py                  background retrieval from the pack's lore.txt
│  ├─ vad.py · stt.py · tts.py audio stages (thin wrappers over providers/)
│  ├─ providers/
│  │  ├─ base.py               the three interfaces: STT, LLM, TTS
│  │  ├─ stt/                  whisper_local
│  │  ├─ llm/                  ollama · llamacpp · openai_chat · claude
│  │  └─ tts/                  edge · gpt_sovits
│  ├─ read_url.py              link → article text (SSRF-guarded)
│  ├─ screen.py · vision.py    window/screen capture, Windows OCR, optional VLM
│  ├─ gdocs.py · manuscript.py · activity.py   follow a Google Doc while you write
│  └─ transcribe_api.py        dedicated Whisper instance for transcription mode
├─ characters/example/         sample character pack ("모모")
├─ config/config.json          every knob, with a _comment on each section
├─ app/                        Electron control app (Korean / English UI)
├─ scripts/                    google_login.py · ingest_lore.py · memory_map.py
└─ data/                       created at runtime: per-character history (characters/<id>/), memory DB, metrics (git-ignored)
```

A deeper walk-through of one turn, the memory lifecycle and how to add a provider is in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Quick start

You need Windows 10/11 (the core loop also runs on Linux), Python 3.10+, Node.js 18+, a Discord bot token, and an LLM (Ollama locally, or an API key). An NVIDIA GPU is recommended.

```bash
git clone https://github.com/Eastar0102/discord-ai-voice-chat.git
cd discord-ai-voice-chat

python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
cd discord_bot && npm install && cd ..

copy .env.example .env            # put DISCORD_TOKEN in it
ollama pull qwen3:8b              # or pick another provider in config/config.json

start_all.bat                     # API server + bot
```

In Discord: join a voice channel, type `!join` in a text channel, and start talking.

**The full step-by-step guide** covers creating the bot, choosing a model, making a character, a custom voice, every command, the control app and troubleshooting: **[docs/GUIDE.md](docs/GUIDE.md)**.

## Commands at a glance

| command | action |
|---|---|
| `!join` / `!leave` | join / leave your voice channel |
| `!auto` · `!always` · `!call` · `!stop` | decide when to answer (default) · answer everything · only when named · stop listening |
| `!talk <text>` | type instead of speaking |
| `!read <url>` | read an article together (a bare link also works) |
| `!봐줘 [source] [글자]` | look at the screen or a window (Windows) |
| `!script [preset]` · `!scriptstop` | transcription mode · end it and upload the transcript |
| `!원고` · `!대화` · `!자동` | writing mode · conversation mode · switch automatically |

## Privacy

Everything the bot hears and remembers is stored locally under `data/`, which is git-ignored. If you choose a cloud LLM, whatever goes into the prompt goes to that API: the conversation, retrieved memory, and any article, screen text or document it is reading. Character packs other than `example` are git-ignored too, so your own character stays out of the repo.

## License

[Apache License 2.0](LICENSE)
