// 전사(스크립트) 모드 — Scripty류 회의록 봇처럼 보이스 채널의 모든 사람을
// 화자별로 받아 적어 텍스트 채널에 흘리고, 세션이 끝나면 전체 로그를 파일로 남긴다.
//
// 어시스턴트 경로(voice_handler.js)와 의도적으로 분리했다:
//  - 어시스턴트는 "한 번에 한 명"(화자 잠금) + 단일 VAD + barge-in 전제
//  - 전사는 동시 발화를 각각 살려야 하고, 응답/TTS가 없어야 한다
// 그래서 여기서는 디스코드가 사용자별로 끊어주는 발화 단위(AfterSilence)를 그대로
// 쓰고, Python의 전용 STT 엔드포인트(/transcribe/utterance)에 덩어리째 보낸다.

const fs = require('fs');
const path = require('path');
const axios = require('axios');
const prism = require('prism-media');
const { EndBehaviorType, VoiceConnectionStatus } = require('@discordjs/voice');
const { AttachmentBuilder } = require('discord.js');

require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

const API_BASE_URL = process.env.API_BASE_URL || 'http://localhost:8000';
const BASE_DIR = path.join(__dirname, '..');
const TRANSCRIPT_DIR = path.join(BASE_DIR, 'data', 'transcripts');

// config/config.json의 transcribe 섹션 (없으면 기본값)
function loadTranscribeConfig() {
    const defaults = {
        silence_ms: 900,
        flush_ms: 2000,
        order_hold_sec: 25,
        request_timeout_sec: 300,
        post_to_channel: true,
        save_transcript: true,
    };
    try {
        const raw = fs.readFileSync(path.join(BASE_DIR, 'config', 'config.json'), 'utf-8');
        return { ...defaults, ...(JSON.parse(raw).transcribe || {}) };
    } catch (e) {
        console.warn('[전사] config 로드 실패, 기본값 사용:', e.message);
        return defaults;
    }
}

function two(n) { return String(n).padStart(2, '0'); }

function clockOf(date) {
    return `${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`;
}

function elapsedOf(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h > 0 ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
}

class TranscribeHandler {
    constructor(client, voiceHandler) {
        this.client = client;
        this.voiceHandler = voiceHandler;
        this.sessions = new Map(); // guildId -> session
    }

    isActive(guildId) {
        return this.sessions.has(guildId);
    }

    /** 고를 수 있는 전사 프리셋 목록 (Python의 config가 유일한 정의처) */
    async listPresets() {
        try {
            const res = await axios.get(`${API_BASE_URL}/transcribe/presets`, { timeout: 10000 });
            return res.data;
        } catch (e) {
            console.error('[전사] 프리셋 조회 실패:', e.message);
            return null;
        }
    }

    /**
     * 전사 세션 시작. 음성 연결이 없으면 해당 보이스 채널로 먼저 들어간다.
     * @param preset 무게 프리셋 이름 (생략 시 config의 transcribe.preset)
     * @returns {Promise<{ok: boolean, reason?: string, stt?: object, stoppedAssistant?: boolean}>}
     */
    async start(voiceChannel, textChannel, preset = null) {
        const guildId = voiceChannel.guild.id;
        if (this.sessions.has(guildId)) {
            return { ok: false, reason: 'already' };
        }

        // 모델을 먼저 고르고 올린다 — 무거운 프리셋은 로딩이 수십 초 걸리므로
        // 첫 발화가 그걸 기다리지 않게 하고, 실패하면 세션을 아예 시작하지 않는다
        let stt;
        try {
            const res = await axios.post(`${API_BASE_URL}/transcribe/select`, {}, {
                params: { preset: preset || undefined, preload: true },
                timeout: 600000,  // 모델 로딩(최초 다운로드 포함) 대기
            });
            stt = res.data;
            console.log(`[전사] 모델 준비 완료: [${stt.preset}] ${stt.model} ` +
                        `(${stt.device}/${stt.compute_type}, 로딩 ${stt.load_sec}초)`);
        } catch (e) {
            const status = e.response?.status;
            const detail = e.response?.data?.detail || e.message;
            console.error('[전사] 모델 준비 실패:', detail);
            return {
                ok: false,
                reason: status === 404 ? 'unknown_preset' : 'stt',
                detail,
            };
        }

        let connection = this.voiceHandler.connections.get(guildId);
        if (!connection) {
            connection = await this.voiceHandler.joinChannel(voiceChannel);
        }

        // 전사 전용 모드: 어시스턴트 듣기가 돌고 있으면 내린다 (같은 유저 스트림을
        // 두 경로가 동시에 구독하면 @discordjs/voice가 같은 스트림을 돌려주어 충돌)
        const stoppedAssistant = !!this.voiceHandler.isListening.get(guildId);
        if (stoppedAssistant) {
            this.voiceHandler.stopListening(guildId);
        }
        this.voiceHandler.transcribing.add(guildId);

        const cfg = loadTranscribeConfig();
        const startedAt = new Date();
        const session = {
            guildId,
            connection,
            voiceChannelName: voiceChannel.name,
            textChannel,
            cfg,
            stt,
            startedAt,
            seq: 0,
            capturing: new Set(),   // 지금 녹음 중인 userId
            streams: new Map(),     // userId -> {audio, decoder}
            pending: new Map(),     // seq -> entry (전사 완료 대기 + 순서 보정)
            nextEmit: 0,            // 다음에 내보낼 seq (시간순 보장)
            outbox: [],             // 채널 게시 대기 줄
            lineCount: 0,
            logPath: path.join(TRANSCRIPT_DIR,
                `${guildId}-${startedAt.toISOString().replace(/[:.]/g, '-')}.jsonl`),
            txtLines: [],
            speakingListener: null,
            flushTimer: null,
        };

        if (cfg.save_transcript) {
            fs.mkdirSync(TRANSCRIPT_DIR, { recursive: true });
            this._appendLog(session, {
                event: 'session_start',
                t: startedAt.toISOString(),
                guild: voiceChannel.guild.name,
                voice_channel: voiceChannel.name,
                preset: stt.preset,
                model: stt.model,
            });
        }

        // 사용자가 말을 시작할 때마다 그 사람 전용 발화 녹음을 건다
        session.speakingListener = (userId) => {
            this._capture(session, userId).catch(err =>
                console.error('[전사] 캡처 오류:', err.message));
        };
        connection.receiver.speaking.on('start', session.speakingListener);

        session.flushTimer = setInterval(() => {
            this._drainPending(session);
            this._flushOutbox(session).catch(() => {});
        }, cfg.flush_ms);

        this.sessions.set(guildId, session);
        console.log(`[전사] 세션 시작 — ${voiceChannel.guild.name} / ${voiceChannel.name}`);
        return { ok: true, stoppedAssistant, stt };
    }

    /**
     * 한 사용자의 발화 하나를 녹음 → 전사 요청.
     * 디스코드가 침묵 silence_ms 후 스트림을 끝내주므로 VAD가 필요 없다.
     */
    async _capture(session, userId) {
        if (session.capturing.has(userId)) return;           // 이미 녹음 중
        if (userId === this.client.user?.id) return;         // 봇 자신은 제외

        // await 전에 먼저 잠근다 — 같은 유저의 speaking 이벤트가 연달아 오면
        // 중복 구독이 생기고, receiver가 같은 스트림을 돌려주어 서로를 끊는다
        session.capturing.add(userId);

        const startedAt = new Date();
        const member = await this._resolveMember(session.guildId, userId);
        if (member?.user?.bot) {                             // 다른 봇도 제외
            session.capturing.delete(userId);
            return;
        }
        if (!this.sessions.has(session.guildId)) {           // 그 사이 세션 종료됨
            session.capturing.delete(userId);
            return;
        }

        const seq = session.seq++;
        const speaker = member?.displayName || member?.user?.username || userId;
        session.pending.set(seq, {
            seq, startedAt, userId, speaker, text: null, done: false,
        });

        let audio;
        try {
            audio = session.connection.receiver.subscribe(userId, {
                end: { behavior: EndBehaviorType.AfterSilence, duration: session.cfg.silence_ms },
                mode: 'opus',
            });
        } catch (e) {
            session.capturing.delete(userId);
            this._resolve(session, seq, '');
            return;
        }

        const decoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
        const chunks = [];
        let bytes = 0;

        decoder.on('data', (pcm) => { chunks.push(pcm); bytes += pcm.length; });

        const finish = async () => {
            if (!session.capturing.has(userId)) return; // 중복 종료 방지
            session.capturing.delete(userId);
            session.streams.delete(userId);
            try { audio.destroy(); } catch (e) { /* 이미 닫힘 */ }
            try { decoder.destroy(); } catch (e) { /* 이미 닫힘 */ }

            // 48kHz 스테레오 int16 → 0.4초 미만은 Python에서도 버리지만 왕복을 아낀다
            if (bytes < 48000 * 2 * 2 * 0.4) {
                this._resolve(session, seq, '');
                return;
            }

            const text = await this._requestTranscription(session, Buffer.concat(chunks), speaker);
            this._resolve(session, seq, text);
        };

        decoder.on('end', () => { finish().catch(() => this._resolve(session, seq, '')); });
        decoder.on('error', () => { finish().catch(() => this._resolve(session, seq, '')); });
        audio.on('error', () => { finish().catch(() => this._resolve(session, seq, '')); });

        audio.pipe(decoder);
        session.streams.set(userId, { audio, decoder });
    }

    async _resolveMember(guildId, userId) {
        try {
            const guild = this.client.guilds.cache.get(guildId);
            if (!guild) return null;
            return guild.members.cache.get(userId) || await guild.members.fetch(userId);
        } catch (e) {
            return null;
        }
    }

    async _requestTranscription(session, pcmBuffer, speaker) {
        try {
            const res = await axios.post(`${API_BASE_URL}/transcribe/utterance`, pcmBuffer, {
                headers: { 'Content-Type': 'application/octet-stream' },
                params: { sr: 48000, ch: 2, speaker },
                maxBodyLength: Infinity,
                maxContentLength: Infinity,
                // 무거운 프리셋은 발화 하나에 수십 초가 걸릴 수 있다 (config에서 조절)
                timeout: (session.cfg.request_timeout_sec || 300) * 1000,
            });
            return (res.data && res.data.text) || '';
        } catch (e) {
            const status = e.response?.status;
            if (status === 503) {
                console.warn('[전사] 백로그 가득 — 이 발화는 건너뜀');
            } else {
                console.error('[전사] STT 요청 실패:', e.message);
            }
            return '';
        }
    }

    /** 전사 결과 도착 — 순서 보정 큐에 채워 넣는다 */
    _resolve(session, seq, text) {
        const entry = session.pending.get(seq);
        if (!entry) return;
        entry.text = text || '';
        entry.done = true;
        this._drainPending(session);
    }

    /**
     * 시간순 보장: 발화 시작 시점에 매긴 seq의 연속 구간만 내보낸다.
     * (동시 발화는 STT 완료 순서가 뒤섞이므로) 앞 발화가 order_hold_sec 넘게
     * 안 끝나면 기다리지 않고 건너뛴다.
     */
    _drainPending(session) {
        const holdMs = session.cfg.order_hold_sec * 1000;

        while (session.nextEmit < session.seq) {
            const entry = session.pending.get(session.nextEmit);
            if (!entry) { session.nextEmit++; continue; }

            if (!entry.done) {
                if (Date.now() - entry.startedAt.getTime() < holdMs) return; // 더 기다림
                console.warn(`[전사] seq ${entry.seq} (${entry.speaker}) 지연 — 순서에서 제외`);
            }

            session.pending.delete(session.nextEmit);
            session.nextEmit++;
            if (entry.done && entry.text) this._emit(session, entry);
        }
    }

    /** 한 줄 확정 — 콘솔 + 파일 + 채널 게시 대기열 */
    _emit(session, entry) {
        const clock = clockOf(entry.startedAt);
        const elapsed = elapsedOf(entry.startedAt.getTime() - session.startedAt.getTime());

        console.log(`[전사] [${clock}] ${entry.speaker}: ${entry.text}`);
        session.lineCount++;
        session.txtLines.push(`[${elapsed}] ${entry.speaker}: ${entry.text}`);

        if (session.cfg.save_transcript) {
            this._appendLog(session, {
                t: entry.startedAt.toISOString(),
                elapsed,
                user_id: entry.userId,
                speaker: entry.speaker,
                text: entry.text,
            });
        }

        if (session.cfg.post_to_channel) {
            session.outbox.push(`\`[${clock}]\` **${entry.speaker}**: ${entry.text}`);
        }
    }

    _appendLog(session, record) {
        try {
            fs.appendFileSync(session.logPath, JSON.stringify(record) + '\n', 'utf-8');
        } catch (e) {
            console.error('[전사] 로그 기록 실패:', e.message);
        }
    }

    /** 모아둔 줄을 하나의 메시지로 게시 (레이트 리밋 회피) */
    async _flushOutbox(session) {
        if (!session.outbox.length || !session.textChannel) return;

        const lines = session.outbox;
        session.outbox = [];

        // 디스코드 메시지 2000자 제한 — 1900자 단위로 묶어 보낸다
        const blocks = [];
        let block = '';
        for (const line of lines) {
            const piece = line.length > 1900 ? line.slice(0, 1897) + '...' : line;
            if (block.length + piece.length + 1 > 1900) {
                blocks.push(block);
                block = piece;
            } else {
                block = block ? `${block}\n${piece}` : piece;
            }
        }
        if (block) blocks.push(block);

        for (const text of blocks) {
            try {
                await session.textChannel.send(text);
            } catch (e) {
                console.error('[전사] 채널 게시 실패:', e.message);
            }
        }
    }

    /**
     * 세션 종료 — 남은 전사를 짧게 기다린 뒤 전체 로그를 파일로 첨부한다.
     * @returns {Promise<{ok: boolean, lines?: number, file?: string}>}
     */
    async stop(guildId) {
        const session = this.sessions.get(guildId);
        if (!session) return { ok: false };

        this.sessions.delete(guildId);
        this.voiceHandler.transcribing.delete(guildId);

        // 새 발화 캡처 중단
        if (session.speakingListener) {
            session.connection.receiver.speaking.off('start', session.speakingListener);
        }
        for (const { audio, decoder } of session.streams.values()) {
            try { audio.destroy(); } catch (e) { /* 이미 닫힘 */ }
            try { decoder.destroy(); } catch (e) { /* 이미 닫힘 */ }
        }

        // 진행 중인 전사를 최대 15초까지 기다린다 (마지막 발화 유실 방지)
        const deadline = Date.now() + 15000;
        while (session.nextEmit < session.seq && Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 300));
            this._drainPending(session);
        }
        clearInterval(session.flushTimer);
        this._drainPending(session);
        await this._flushOutbox(session);

        const endedAt = new Date();
        if (session.cfg.save_transcript) {
            this._appendLog(session, {
                event: 'session_end',
                t: endedAt.toISOString(),
                lines: session.lineCount,
            });
        }

        // 전용 STT 인스턴스 VRAM 반환
        try {
            await axios.post(`${API_BASE_URL}/transcribe/release`, {}, { timeout: 15000 });
        } catch (e) {
            console.warn('[전사] STT 인스턴스 해제 요청 실패:', e.message);
        }

        const result = { ok: true, lines: session.lineCount };

        // 전체 로그를 텍스트 파일로 첨부
        if (session.textChannel && session.lineCount > 0) {
            const header = [
                `# 전사 기록 — ${session.voiceChannelName}`,
                `시작: ${session.startedAt.toLocaleString('ko-KR')}`,
                `종료: ${endedAt.toLocaleString('ko-KR')}`,
                `모델: [${session.stt?.preset}] ${session.stt?.model} (${session.stt?.compute_type})`,
                `줄 수: ${session.lineCount}`,
                '',
            ].join('\n');
            const body = header + session.txtLines.join('\n') + '\n';
            const fileName = `transcript-${session.startedAt.toISOString().slice(0, 19).replace(/[:]/g, '-')}.txt`;

            try {
                const attachment = new AttachmentBuilder(Buffer.from(body, 'utf-8'), { name: fileName });
                await session.textChannel.send({
                    content: `⏹️ 전사를 마쳤습니다. 총 **${session.lineCount}줄**.`,
                    files: [attachment],
                });
                result.file = fileName;
            } catch (e) {
                console.error('[전사] 파일 첨부 실패:', e.message);
            }
        }

        console.log(`[전사] 세션 종료 — ${session.lineCount}줄, 로그: ${session.logPath}`);
        return result;
    }

    /** 봇이 채널을 떠나거나 종료될 때 모든 세션 정리 */
    async stopAll() {
        for (const guildId of [...this.sessions.keys()]) {
            await this.stop(guildId).catch(() => {});
        }
    }
}

module.exports = TranscribeHandler;
