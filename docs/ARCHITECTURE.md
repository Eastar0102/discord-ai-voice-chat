# Architecture and design

**English** · [한국어](ARCHITECTURE.ko.md) · [← README](../README.md)

This document covers *how* the pipeline runs and *why* it is built that way. For how to use it, see [GUIDE.md](GUIDE.md).

- [Two processes](#two-processes)
- [The life of one turn](#the-life-of-one-turn)
- [Prompt layout](#prompt-layout)
- [The life of a memory](#the-life-of-a-memory)
- [Background loops](#background-loops)
- [Observability — metrics.jsonl](#observability--metricsjsonl)
- [Extending](#extending)
- [Adapting to another language](#adapting-to-another-language)
- [Design notes — easy things to get wrong](#design-notes--easy-things-to-get-wrong)

---

## Two processes

![Pipeline](images/pipeline.svg)

| | Node.js (`discord_bot/`) | Python (`src/`) |
|---|---|---|
| role | Discord gateway, voice receive/playback, text commands | all listening, thinking, speaking and remembering |
| why | `@discordjs/voice` is the most mature way to receive Discord audio | Whisper, embeddings and LLM tooling live in Python |
| link | WebSocket `/ws` (binary = audio, text = control JSON) | REST (`/read`, `/look`, `/transcribe/*`, `/inspect/*`, `/llm/*`, `/tts/*`) |

The Node side does almost no thinking. It has four jobs:
- a speaker lock (one person's audio at a time)
- the playback queue
- barge-in signals
- telling Python who is in the channel (`speaker`, `voice_event`)

## The life of one turn

From the moment someone starts speaking to the first sound out. The code is in `src/websocket_handler.py`.

```
 speech ──┬─ 20 ms PCM chunks (48k stereo)
          │   → anti-aliasing + 16k mono
          │   → StreamingVAD (WebRTC + RMS)
          │
          ├─ while speaking (every ~0.5 s)
          │    partial transcript (Whisper) ──→ prefetch: send the prompt built from the
          │                                     partial text to warm the LLM's prefill cache
          │
          ├─ endpoint (whichever comes first)
          │    · early   — silence begins + partial ends with sentence punctuation   (~0.3 s)
          │    · silence — 600 ms of silence
          │    · stall   — packets stop for 0.4 s (1.0 s if it sounds unfinished)
          │
          ├─ final transcript — reuse the partial if it covered all speech (≈0 s)
          ├─ filters — broadcast hallucinations, names-only noise
          ├─ fragment merge — if the previous turn is still generating, cancel it and
          │                   prepend its text
          │
          ├─ turn-taking (should_reply)
          │    always → answer / name → answer / call → don't
          │    auto: talking-to-self cues → don't
          │          addressed someone else by name → don't
          │          one person → answer / several → only if engaged or "you"+question
          │    + unless must_answer (name, "you", question ending), start the LLM judge in parallel
          │
          ├─ ends like it continues ("아니 근데") → wait 1.2 s for the rest; merge if it comes
          │
          ├─ prompt assembly (llm.build_messages), including memory and lore retrieval
          ├─ LLM stream
          │    · first 14 chars: if it copies a recent reply, regenerate once
          │    · leading emotion tag → TTS style for the turn
          │    · just before the first chunk: check the judge (wait ≤0.6 s); fold if "not for me"
          │    · strip stage directions/*actions*; cut at a list or rule line
          │
          └─ each finished sentence → TTS queue → synthesized in order → audio + subtitle over WS
```

**Barge-in.** When Node detects new speech during playback, it sends `stop_audio`. Python cancels the generating stream and the queued TTS tasks, which releases their semaphore.

**Turn records.** Only turns that actually spoke are logged as `turn`; a turn the judge folded is logged as `gate`.

## Prompt layout

`llm.build_messages()` assembles this every turn:

```
[system]
  character pack prompt.txt
    $TOPIC  ← topic.txt (tone examples)
    $MEMORY ← memory.retrieve_relevant_memory()  — speaker, self, experiences, episodes
    $LORE   ← lore.retrieve_relevant_lore()      — up to 2 relevant blocks
  [gap note] or [earlier in this conversation]  ← if >30 min since last line; else session summary
  [current situation] ← server/channel, when it joined, per-person "here now" table, recent leaves, activity
  [what you're looking at] ← article / screen / document (if any; 30-min TTL)
  [speaker info]       ← whether this is the owner (identity.owner_names)
  [current time]       ← last. "read this line as-is when asked the date" + a one-line format reminder
[history]  last 20 lines — normalized to the output format, ≤6 from a previous session, duplicates removed
[user]     "[speaker name] utterance"
```

**`cache_layout`** (enabled by the llama.cpp provider) moves the per-turn sections (memory, lore, situation, clock) into a second system message *after* the history. The front of the prompt then stays identical across turns and its prefill stays cached; measured first token went from 2.6 s to 1.6 s. The history window also slides in steps of 10 lines, so the front doesn't shift every turn.

## The life of a memory

The code is in `src/memory.py`.

```
conversation history (data/characters/<id>/conversation_history.jsonl — one per character)
   │  idle 5 min · bot leaves · 20 user lines
   ▼
flush_episode()
   │  drop lines Whisper was unsure about
   ▼
extract()  ── summarizer model (default: the chat model, format=json)
   │  episode / person_facts / self_said / self_experience
   │  + a second pass per person with only their own lines (one pass drops whole people)
   │  + relative dates → absolute; future ones get `due`
   │  + keyword filters (work on the bot, the session itself, first-person quotes, capability denials)
   │  + optional screen: show candidates next to the transcript, classify by subject, keep "life"
   ▼
_reconcile()  ── next to that person's existing facts: add / replace(old) / skip
   ▼
ChromaDB collection `memory_<character id>`
   metadata: type(episode|person|self), who(Discord ID | "self"), name, ts, due, kind(state|experience), about(topic)
```

**On recall**, `retrieve_relevant_memory()` picks:

| block | how |
|---|---|
| topic | if the utterance hits a `memory.topics` entry, that topic's memories, nearest first |
| speaker | always, no search gate. 8 slots = 5 relevant + 3 recent. Overdue plans go on top as "check first" |
| self facts | always, with "don't contradict yourself" |
| experiences | 3 recent + 1 relevant, *without* "don't contradict" (it made past events sound current) |
| episodes | time words ("yesterday", "last time") + the latest one + relevant ones (distance ≤120) |

## Background loops

Each connection (`handle_connection`) starts four loops:

| loop | period | job |
|---|---|---|
| `stall_watchdog` | 0.1 s | force an endpoint when packets stop |
| `memory_watchdog` | 10 s | update the session summary; flush an episode when due (never mid-conversation) |
| `initiative_watchdog` | 5 s | speak first when quiet longer than `base / (1 + interest) × backoff^tries` |
| `manuscript_watchdog` | 20 s | watch the Google Doc, switch to "writing", comment when the gates pass |

At server start, LLM/TTS warm-up and `memory_manager.wake_up()` run in the background. The arrival greeting waits up to 45 s for warm-up to finish.

## Observability — metrics.jsonl

One JSON line per event in `data/metrics.jsonl`; the control app's conversation and state tabs read it.

| event | contents |
|---|---|
| `turn` | speaker, utterance, reply, model, `endpoint_s` · `stt_s` · `llm_first_s` · `tts_first_s` · `total_s`, STT confidence |
| `ttfa` | when the first audio left (felt latency = endpoint + ttfa) |
| `gate` | answered or not, why, mode, head count |
| `spoke` | things it said unprompted (greeting, initiative, writing comment) |
| `initiative` · `activity` · `look` · `read` · `manuscript` | each feature's decisions |
| `stt_timeout` · `stt_skipped` · `memory_error` · `name_noise` | moments something went wrong |

Code that makes a decision logs it with its reason. A gate that logged nothing once went months without firing, and nobody noticed.

## Extending

### Adding a provider

`src/providers/base.py` defines three interfaces:

```python
class STTProvider:  def transcribe(self, audio_np) -> str
class LLMProvider:  def chat(self, messages) -> str
                    async def stream_chat(self, messages, options=None) -> AsyncIterator[str]
class TTSProvider:  def synthesize(self, text, style="neutral") -> bytes | None   # WAV bytes
```

1. Implement it in `src/providers/<kind>/<name>.py`. The constructor takes `options: dict` (the config section of the same name).
2. Register the name in `get_provider` in `src/providers/<kind>/__init__.py`.
3. Add a `"<name>": {...}` section to `config.json` and set `provider` to it.

Optional members unlock more:

| member | effect |
|---|---|
| `warmup()` | preload at startup and on model switch |
| `release()` | free VRAM when switching away or when transcription mode needs it |
| `prefetch(messages)` | warm the prompt cache while the user is still speaking |
| `cache_layout = True` | the per-turn-sections-after-history layout |
| `slots > 1` | send judge/memory calls to a different slot than the conversation |
| STT `set_names(names)`, `last_stats` | channel-name hotwords, confidence logging |

### New commands and features

- Text commands go in `discord_bot/index.js`. Python features are exposed as REST endpoints in `src/api_server.py` and called from Node with `axios`.
- To add a prompt section, use `add(block, volatile)` inside `llm.build_messages()`. Mark per-turn content `volatile=True` so `cache_layout` moves it after the history.

## Adapting to another language

The pipeline stays; only the Korean-bound parts change:

| where | what |
|---|---|
| character pack | `prompt.txt`, `topic.txt`, `lore.txt`, wake words |
| `websocket_handler.py` | `_SECOND_PERSON_RE`, `_MUST_ANSWER_RE`, `_SOLO_RE` (talking to self), `UNFINISHED_WORDS`/`UNFINISHED_END_RE` (sounds unfinished), `FIRST_CLAUSE_RE`, emotion-word maps |
| `llm.py` | system instructions (speaker info, clock, judge prompt and shots), `_STYLE_KO` |
| `memory.py` | extraction/reconcile/screen prompts, date expressions (`_WEEK_RE`, `_REL_DAY` …), filter regexes |
| config | Whisper language, `transcribe.defaults.language`, the Edge TTS voice, the embedding model (`jhgan/ko-sroberta-multitask` in `vector_db.py`) |

## Design notes — easy things to get wrong

The code comments record which failure each decision fixed, with measurements. These are the ones likely to apply elsewhere.

**Turn-taking**
- **Don't ask the persona model whether to speak.** Telling it to emit `(pass)` inside its reply fired 0 times in 286 real utterances; a long persona prompt beats the instruction.
- **A persona-free yes/no judge fails the other way.** It stayed quiet when it should have, but silenced 14 of 100 utterances that deserved an answer.
- **So rules come first, and head count is the strongest signal.** With one person present, only 1 of 489 real utterances should have been left unanswered. The judge sees only what the rules pass through, and is never consulted on a name, "you" or a question ending.
- **History measures only one of the two errors.** Every logged line was answered, so a rule that wrongly *silences* can be validated against history, but one that wrongly *answers* only shows up in live sessions.

**Streaming and latency**
- **Serialize Whisper on its own single thread.** `asyncio.wait_for` cancels the future, not the thread. Time out inside `async with lock` and the lock is released while transcription keeps running; the next call overlaps it, everything slows, and timeouts cascade.
- **Discord keeps sending ~0.5 s of silence after speech, then stops.** A "600 ms of silence" rule therefore rarely completes, so watch for stalled packets separately.
- **Cut TTS only at sentence ends.** Cutting mid-sentence for a faster first sound restarts the intonation at every chunk, and every seam is audible.
- **Don't send concurrent requests to GPT-SoVITS.** Three at once turned a 1.1 s synthesis of 14 characters into 13–30 s.
- **Benchmark local LLMs through the real provider.** A request whose `num_ctx` differs from the resident model's makes Ollama reload it, which shows up as latency that doesn't exist in production.

**Prompt**
- **History is a format few-shot.** Small models follow the shape of the last few turns more than the rules. Past replies are normalized to the exact output format; stripping the tags instead made tag compliance decay to zero over a long session.
- **Put the clock at the very end.** In the middle, dates attached to memories pulled answers about "today" a day off (4/8). At the end: 12/12.
- **Separate what is logged from what the model is told.** Handing it "(nobody has said anything for a while)" produced "seems like the conversation has paused." "(You start the conversation)" produced a real topic.
- **On arrival, give it no history.** With history, it walked in replying to the last line of a conversation from hours earlier.

**Memory**
- **Keyword filters only catch the vocabulary of the day they were written.** A list written against one session caught 0 of 30 junk facts from the next. Generalizing needs a judge that sees the transcript and is asked what each sentence is *about*.
- **Ask "what is this about?", not "should I drop this?".** Asked to choose what to drop, the same model discarded 6–9 of 20 genuine facts as "trivial". Asked only for the subject, over-dropping fell to 1–2.
- **Without a good judge, remember less.** Drop low-confidence lines and cap facts per person per episode.
- **Embedding distance can't detect updates.** "Gold 2" → "Platinum" were 137 apart, so both stayed. Show the model the old facts and let it choose add/replace/skip.
- **Hotwords make noise decode as those words.** Drop transcripts made only of two or more known names. Keep a single name, because that's how people call it.
- **Don't treat a date equal to the day it was said as a plan.** Otherwise everything that happened that day becomes an "overdue plan", and the next greeting asks how a finished thing went.
