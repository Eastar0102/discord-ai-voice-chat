// Discord 음성 핸들러
// 음성 연결, 스트림 수신, 재생 처리

const {
    VoiceConnectionStatus,
    createAudioPlayer,
    createAudioResource,
    AudioPlayerStatus,
    entersState,
    joinVoiceChannel,
    createListeningStream,
    EndBehaviorType,
    StreamType,
} = require('@discordjs/voice');
const axios = require('axios'); // 백업용 또는 초기 설정용
const WebSocket = require('ws');
const { pipeline } = require('stream/promises');
const { Transform, Readable } = require('stream');
const FormData = require('form-data');
const fs = require('fs');
const path = require('path');
const prism = require('prism-media');

// DAVE (E2EE) 초기화
try {
    const davey = require('@snazzah/davey');
    console.log('DAVE (E2EE) 로드 완료');
} catch (e) {
    console.warn('DAVE 로드 실패 (선택적):', e.message);
}

// 환경변수 로드
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const API_BASE_URL = process.env.API_BASE_URL || 'http://localhost:8000';
const WS_URL = API_BASE_URL.replace(/^http/, 'ws') + '/ws';

class VoiceHandler {
    constructor(client) {
        this.client = client;
        this.connections = new Map();
        this.announcedJoin = new Set();  // 입장을 이미 알린 길드 (Ready 재발생 시 중복 방지)
        this.recordingStreams = new Map();
        this.isRecording = new Map();
        this.isListening = new Map();
        this.listeningMode = new Map();
        // 전사 모드가 돌고 있는 길드 — 같은 유저 스트림을 두 경로가 동시에 구독하면
        // @discordjs/voice가 같은 스트림 객체를 돌려주어 서로를 끊어먹는다 (상호 배타)
        this.transcribing = new Set();
        this.userNames = new Map();
        this.conversationContext = new Map();

        // WebSocket 초기화
        this.ws = null;
        this.wsConnected = false;
        this.connectWebSocket();

        // 오디오 재생 큐 (순차 재생 보장)
        this.audioQueue = [];
        this.isPlaying = false;
        this.currentAudioKind = null; // 'filler' | 'response'
        this.nextAudioKind = null;    // 다음 바이너리 청크의 종류 (메타 메시지로 지정)

        // 오래된 WAV 파일 정리
        this.cleanupOldWavFiles();
        setInterval(() => this.cleanupOldWavFiles(), 5 * 60 * 1000);
    }

    connectWebSocket() {
        console.log(`[WS] Connecting to ${WS_URL}...`);
        this.ws = new WebSocket(WS_URL);

        this.ws.on('open', () => {
            console.log('[WS] Connected');
            this.wsConnected = true;
            this.startPing();
            // 파이썬 서버가 재시작되면 지금 어느 채널에 있는지 모른다 — 들어가 있는 채널을 다시 알린다
            for (const guildId of this.connections.keys()) this.sendVoiceEvent('sync', guildId);
        });

        this.ws.on('message', async (data, isBinary) => {
            if (isBinary) {
                // 바이너리 오디오 데이터 수신 -> 재생
                console.log(`[WS] Received audio chunk (${data.length} bytes)`);
                const kind = this.nextAudioKind || 'response';
                this.nextAudioKind = null;
                this.queueAudio(data, kind);
            } else {
                // 텍스트 메시지
                try {
                    const msg = JSON.parse(data.toString());
                    if (msg.type === 'pong') {
                        // pong
                    } else if (msg.type === 'audio_kind') {
                        this.nextAudioKind = msg.kind;
                    } else if (msg.type === 'ai_response') {
                        this.closePartialLine();
                        console.log(`[AI] ${msg.text} (${msg.style})`);
                        this.nextAudioKind = 'response';
                    } else if (msg.type === 'user_partial') {
                        // 부분 전사는 같은 줄에 덮어쓰기 (로그 도배 방지)
                        const line = `[User...] ${msg.text}`;
                        const pad = Math.max(0, (this.partialLineLen || 0) - line.length);
                        process.stdout.write('\r' + line + ' '.repeat(pad));
                        this.partialLineLen = line.length;
                    } else if (msg.type === 'user_transcription') {
                        this.closePartialLine();
                        const speakerName = this.currentSpeaker || 'Unknown';
                        console.log(`[User: ${speakerName}] ${msg.text}`);
                    } else if (msg.type === 'status') {
                        this.closePartialLine();
                        console.log(`[Status] ${msg.message}`);
                    } else if (msg.type === 'latency') {
                        console.log(`[Latency] ⚡ 응답 속도: ${msg.duration.toFixed(0)}ms`);
                    }
                } catch (e) {
                    console.error('[WS] Message parse error:', e);
                }
            }
        });

        this.ws.on('close', () => {
            console.log('[WS] Disconnected. Reconnecting in 3s...');
            this.wsConnected = false;
            this.stopPing();
            setTimeout(() => this.connectWebSocket(), 3000);
        });

        this.ws.on('error', (err) => {
            console.error('[WS] Error:', err.message);
        });
    }

    closePartialLine() {
        // 열린 부분 전사 줄을 개행으로 마감 (다음 로그가 이어붙지 않게)
        if (this.partialLineLen) {
            process.stdout.write('\n');
            this.partialLineLen = 0;
        }
    }

    startPing() {
        this.pingInterval = setInterval(() => {
            if (this.wsConnected) {
                this.ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 30000);
    }

    stopPing() {
        if (this.pingInterval) clearInterval(this.pingInterval);
    }

    queueAudio(audioBuffer, kind = 'response') {
        console.log(`[Audio] Queueing ${kind} audio, size: ${audioBuffer.length}`);

        // 응답 오디오가 도착하면 추임새는 즉시 양보:
        // 대기 중인 추임새는 큐에서 제거하고, 재생 중인 추임새는 중단
        // (추임새가 끝날 때까지 응답이 기다리며 체감 레이턴시를 키우는 것 방지)
        if (kind === 'response') {
            this.audioQueue = this.audioQueue.filter(item => item.kind !== 'filler');
            if (this.isPlaying && this.currentAudioKind === 'filler') {
                console.log('[Audio] Response arrived - cutting filler short');
                for (const [guildId, connection] of this.connections) {
                    if (connection.state.subscription) {
                        connection.state.subscription.player.stop(); // Idle 이벤트가 다음 큐 항목 재생
                    }
                }
            }
        }

        this.audioQueue.push({ buffer: audioBuffer, kind: kind });

        // 재생 중이 아니면 재생 시작
        if (!this.isPlaying) {
            this.processQueue();
        }
    }

    processQueue() {
        if (this.audioQueue.length === 0) {
            this.isPlaying = false;
            this.currentAudioKind = null;
            return;
        }

        this.isPlaying = true;
        const item = this.audioQueue.shift();
        this.currentAudioKind = item.kind;

        // 모든 연결된 길드에 재생 (방송 모드)
        for (const [guildId, connection] of this.connections) {
            this.playAudioStream(guildId, item.buffer);
        }
    }

    playAudioStream(guildId, audioBuffer) {
        const connection = this.connections.get(guildId);
        if (!connection) return;

        try {
            const audioPlayer = createAudioPlayer();
            const stream = Readable.from(audioBuffer);
            const resource = createAudioResource(stream, {
                inputType: StreamType.Arbitrary,
                inlineVolume: true,
            });

            audioPlayer.play(resource);
            const subscription = connection.subscribe(audioPlayer);

            audioPlayer.on(AudioPlayerStatus.Idle, () => {
                subscription.unsubscribe();
                audioPlayer.stop();

                if (this.isPlaying) {
                    this.isPlaying = false;
                    this.processQueue();
                }

                // 재생이 끝났으므로 VAD 리셋 요청 (에코/잔향 제거)
                if (this.wsConnected && this.audioQueue.length === 0) {
                    this.ws.send(JSON.stringify({ type: 'vad_reset' }));
                }
            });

            audioPlayer.on('error', error => {
                console.error('Audio player error:', error);
                if (this.isPlaying) {
                    this.isPlaying = false;
                    this.processQueue();
                }
            });

        } catch (e) {
            console.error('Play audio error:', e);
            this.isPlaying = false;
        }
    }

    /**
     * 사용자 닉네임 가져오기 (화자 구분용)
     */
    async getUserName(guildId, userId) {
        if (this.userNames.has(userId)) {
            return this.userNames.get(userId);
        }

        try {
            if (!this.client) return userId;

            const guild = this.client.guilds.cache.get(guildId);
            if (!guild) return userId;

            const member = await guild.members.fetch(userId);
            const nickname = member.displayName || member.user.username;

            this.userNames.set(userId, nickname);
            return nickname;
        } catch (error) {
            console.warn(`[Speaker] 사용자 ${userId} 닉네임 가져오기 실패:`, error.message);
            return userId;
        }
    }

    cleanupOldWavFiles() {
        // ... (기존 로직 유지)
    }

    async joinChannel(voiceChannel) {
        // ... (기존 로직 유지)
        const connection = joinVoiceChannel({
            channelId: voiceChannel.id,
            guildId: voiceChannel.guild.id,
            adapterCreator: voiceChannel.guild.voiceAdapterCreator,
            selfDeaf: false,
            selfMute: false,
        });

        this.connections.set(voiceChannel.guild.id, connection);

        connection.on(VoiceConnectionStatus.Ready, () => {
            console.log(`음성 채널에 연결됨: ${voiceChannel.name}`);
            // Ready는 재연결 때도 다시 온다 — 입장은 처음 한 번만 알린다
            if (!this.announcedJoin.has(voiceChannel.guild.id)) {
                this.announcedJoin.add(voiceChannel.guild.id);
                this.sendVoiceEvent('self_join', voiceChannel.guild.id);
            }

            // Speaking event listener for Barge-in and User ID tracking
            // 중복 리스너 방지: 이전에 이 핸들러가 붙인 리스너만 제거
            // (removeAllListeners는 전사 핸들러의 리스너까지 날려버린다)
            if (this.speakingListeners?.has(voiceChannel.guild.id)) {
                connection.receiver.speaking.off(
                    'start', this.speakingListeners.get(voiceChannel.guild.id));
            }

            // 디바운싱을 위한 상태 맵 (userId -> lastSpeakingTime)
            this.speakingStates = new Map();

            // 화자 잠금 (한 번에 한 명만 처리)
            this.activeUserId = null;
            this.activeUserTimeout = null;

            const onSpeakingStart = async (userId) => {
                // 전사 모드 중에는 어시스턴트 쪽 화자 잠금/barge-in 판정을 하지 않는다
                if (this.transcribing.has(voiceChannel.guild.id)) return;

                const now = Date.now();
                const lastTime = this.speakingStates.get(userId) || 0;

                // 200ms 이내 재발화 무시 (디바운싱)
                if (now - lastTime < 200) {
                    return;
                }
                this.speakingStates.set(userId, now);

                // 화자 잠금 확인
                if (this.activeUserId && this.activeUserId !== userId) {
                    console.log(`[Voice] Ignored ${userId} (Active: ${this.activeUserId})`);
                    return;
                }

                // 새로운 화자 또는 기존 화자 연장
                this.activeUserId = userId;

                // 타임아웃 갱신 (말이 끊기면 1초 후 잠금 해제)
                if (this.activeUserTimeout) clearTimeout(this.activeUserTimeout);
                this.activeUserTimeout = setTimeout(() => {
                    if (this.activeUserId === userId) {
                        console.log(`[Voice] Speaker lock released for ${userId}`);
                        this.activeUserId = null;
                    }
                }, 1000);

                console.log(`[Voice] ${userId} 말하기 시작`);

                // 현재 발화자 정보 저장 (서버 별명 사용)
                const nickname = await this.getUserName(voiceChannel.guild.id, userId);
                this.currentSpeaker = nickname;

                // 화자 + 채널 참여자 명단을 Python에 전달 (발화 타이밍 판단용)
                try {
                    const ch = voiceChannel.guild.channels.cache.get(connection.joinConfig.channelId) || voiceChannel;
                    const humans = ch.members.filter(m => !m.user.bot);
                    if (this.wsConnected) {
                        this.ws.send(JSON.stringify({
                            type: 'speaker',
                            user_name: nickname,
                            // 기억은 사용자 ID로 묶는다 — 닉네임은 서버마다 다르고 바뀔 수 있다
                            user_id: userId,
                            avatar: ch.members.get(userId)?.displayAvatarURL({ extension: 'png', size: 64 }) || null,
                            members: humans.map(m => m.displayName),
                            avatars: Object.fromEntries(humans.map(m =>
                                [m.displayName, m.displayAvatarURL({ extension: 'png', size: 64 })])),
                            human_count: humans.size,
                            guild_name: voiceChannel.guild.name,
                            channel_name: ch.name,
                        }));
                    }
                } catch (e) { /* 명단 조회 실패는 치명적이지 않음 */ }

                // 봇이 말하고 있었다면 중단 (Barge-in) -> RMS 기반으로 이동됨
                // if (this.isPlaying) {
                //     console.log(`[Voice] Barge-in Triggered (Bot was playing)`);
                //     this.stopCurrentAudio(voiceChannel.guild.id); // 로컬 재생 중단
                // }

                // Python 서버에 오디오 생성 중단 요청 -> RMS 기반으로 이동됨
                // if (this.wsConnected) {
                //     this.ws.send(JSON.stringify({ type: 'stop_audio' }));
                // }
            };

            if (!this.speakingListeners) this.speakingListeners = new Map();
            this.speakingListeners.set(voiceChannel.guild.id, onSpeakingStart);
            connection.receiver.speaking.on('start', onSpeakingStart);
        });

        connection.on(VoiceConnectionStatus.Disconnected, async () => {
            try {
                await Promise.race([
                    entersState(connection, VoiceConnectionStatus.Signalling, 5_000),
                    entersState(connection, VoiceConnectionStatus.Connecting, 5_000),
                ]);
            } catch (error) {
                this.sendVoiceEvent('self_leave', voiceChannel.guild.id);
                this.announcedJoin.delete(voiceChannel.guild.id);
                connection.destroy();
                this.connections.delete(voiceChannel.guild.id);
                this.recordingStreams.delete(voiceChannel.guild.id);
                this.isRecording.delete(voiceChannel.guild.id);
                console.log(`음성 채널 연결 해제`);
            }
        });

        return connection;
    }

    async leaveChannel(guildId) {
        const connection = this.connections.get(guildId);
        if (connection) {
            this.sendVoiceEvent('self_leave', guildId);  // 연결을 끊기 전에 — 끊고 나면 채널 정보를 못 읽는다
            this.announcedJoin.delete(guildId);
            connection.destroy();
            this.connections.delete(guildId);
            this.recordingStreams.delete(guildId);
            this.isRecording.delete(guildId);
            console.log(`음성 채널에서 나감: ${guildId}`);
        }
    }

    isConnected(guildId) {
        return this.connections.has(guildId);
    }

    /** 봇이 들어가 있는 음성 채널과 그 안의 사람들 (봇 제외) */
    channelInfo(guildId) {
        const connection = this.connections.get(guildId);
        const guild = this.client?.guilds.cache.get(guildId);
        const channel = connection && guild?.channels.cache.get(connection.joinConfig.channelId);
        if (!channel) return null;
        const humans = channel.members.filter(m => !m.user.bot);
        return {
            guild, channel, members: humans.map(m => m.displayName), humanCount: humans.size,
            avatars: Object.fromEntries(humans.map(m =>
                [m.displayName, m.displayAvatarURL({ extension: 'png', size: 64 })])),
            // 봇 자신의 프로필 사진 — 대화 화면에서 캐릭터 말풍선에 붙는다
            selfAvatar: this.client?.user?.displayAvatarURL({ extension: 'png', size: 64 }) || null,
        };
    }

    /**
     * 입장·퇴장을 파이썬에 알린다 — 봇이 지금 어느 서버·채널에 있는지, 누가 막 들어오고
     * 나갔는지를 [현재 상황]에 넣고 참여자 명단을 바로 갱신하기 위해 (전엔 누가 말해야만 갱신됐다)
     * event: self_join | self_leave | member_join | member_leave | sync
     */
    sendVoiceEvent(event, guildId, userName = null, userId = null) {
        if (!this.wsConnected) return;
        const info = this.channelInfo(guildId);
        if (!info) return;
        this.ws.send(JSON.stringify({
            type: 'voice_event', event,
            guild_name: info.guild.name, channel_name: info.channel.name,
            user_name: userName, user_id: userId, members: info.members, human_count: info.humanCount,
            avatars: info.avatars, self_avatar: info.selfAvatar,
            // 듣기 상태 — 파이썬은 이걸로 먼저 말 걸어도 되는지(입장 인사) 판단한다
            listening: this.isListening.get(guildId) ? (this.listeningMode.get(guildId) || 'always') : 'off',
        }));
    }

    /** index.js의 voiceStateUpdate — 봇이 있는 채널에 사람이 들어오거나 나가면 알린다 */
    onVoiceStateUpdate(oldState, newState) {
        const guildId = newState.guild.id;
        const connection = this.connections.get(guildId);
        const member = newState.member || oldState.member;
        if (!connection || !member || member.user.bot) return;
        const here = connection.joinConfig.channelId;
        if (newState.channelId === here && oldState.channelId !== here) {
            this.sendVoiceEvent('member_join', guildId, member.displayName, member.id);
        } else if (oldState.channelId === here && newState.channelId !== here) {
            this.sendVoiceEvent('member_leave', guildId, member.displayName, member.id);
        }
    }

    async startListening(guildId, userId, mode = 'auto') {
        const connection = this.connections.get(guildId);
        if (!connection || connection.state.status !== VoiceConnectionStatus.Ready) {
            return false;
        }

        // 전사 모드 중에는 어시스턴트 듣기를 켜지 않는다 (전사 전용 모드)
        if (this.transcribing.has(guildId)) {
            console.log('[Voice] 전사 모드 중 — 어시스턴트 듣기 시작 거부');
            return false;
        }

        if (this.isListening.get(guildId)) {
            return true;
        }

        this.isListening.set(guildId, true);
        this.listeningMode.set(guildId, mode);

        // Python 서버에 듣기 모드 전달 (call: 호출어 필요, always: 모든 발화 반응)
        if (this.wsConnected) {
            this.ws.send(JSON.stringify({ type: 'set_mode', mode: mode }));
        }

        try {
            this._startListeningForChannel(guildId);
            return true;
        } catch (error) {
            console.error('음성 수신 시작 실패:', error);
            this.isListening.set(guildId, false);
            return false;
        }
    }

    _startListeningForChannel(guildId) {
        console.log('[Voice] 채널 전체 음성 감지 시작');
        this._checkAndListen(guildId).catch(console.error);
    }

    async _checkAndListen(guildId) {
        const connection = this.connections.get(guildId);
        if (!connection || !this.isListening.get(guildId)) return;

        const voiceChannel = connection.joinConfig.channelId;
        const guild = this.client.guilds.cache.get(guildId);

        if (!guild) {
            setTimeout(() => this._checkAndListen(guildId).catch(console.error), 1000);
            return;
        }

        const channel = guild.channels.cache.get(voiceChannel);
        if (!channel || !channel.members) {
            setTimeout(() => this._checkAndListen(guildId).catch(console.error), 1000);
            return;
        }

        const channelMembers = channel.members.filter(member => !member.user.bot && member.id !== this.client.user.id);

        for (const [userId, member] of channelMembers) {
            const streamKey = `${guildId}_${userId}`;
            if (!this.recordingStreams.has(streamKey)) {
                this._recordUserVoice(guildId, userId);
            }
        }

        setTimeout(() => this._checkAndListen(guildId).catch(console.error), 500);
    }

    _recordUserVoice(guildId, userId) {
        const connection = this.connections.get(guildId);
        if (!connection) return;

        const streamKey = `${guildId}_${userId}`;
        if (this.recordingStreams.has(streamKey)) return;

        try {
            const audio = connection.receiver.subscribe(userId, {
                end: { behavior: EndBehaviorType.Manual },
                mode: 'opus',
            });

            let bargeInTriggered = false;
            let loudChunks = 0; // 연속으로 임계값을 넘은 청크 수 (지속 발화 판정용)

            const opusDecoder = new prism.opus.Decoder({
                rate: 48000,
                channels: 2,
                frameSize: 960
            });

            audio.pipe(opusDecoder);

            opusDecoder.on('data', (pcmChunk) => {
                // 실시간 스트리밍: 버퍼링 없이 바로 전송
                // 화자 잠금 확인: 현재 활성화된 화자의 오디오만 전송
                if (this.wsConnected && this.activeUserId === userId) {
                    // 타임아웃 갱신 (계속 말하고 있음)
                    if (this.activeUserTimeout) clearTimeout(this.activeUserTimeout);
                    this.activeUserTimeout = setTimeout(() => {
                        if (this.activeUserId === userId) {
                            console.log(`[Voice] Speaker lock released for ${userId}`);
                            this.activeUserId = null;
                            // 새 발화 세션을 위해 플래그 초기화는 여기서 할 수 없으므로(클로저),
                            // speaking 이벤트에서 activeUserId가 변경될 때 자연스럽게 처리됨.
                            // 하지만 같은 유저가 다시 말할 때를 대비해 로직 점검 필요.
                            // 여기서는 단순히 잠금만 해제.
                        }
                    }, 1000);

                    // Barge-in 판정: 봇이 실제로 말하는 중일 때만, 지속적인 발화일 때만.
                    // (이전에는 봇이 조용할 때도 stop_audio를 보내서 생성 중인 응답을 죽였음)
                    if (this.isPlaying) {
                        // RMS 계산 (PCM 16bit Little Endian)
                        let sumSquares = 0;
                        const numSamples = pcmChunk.length / 2;
                        for (let i = 0; i < pcmChunk.length; i += 2) {
                            const val = pcmChunk.readInt16LE(i);
                            sumSquares += val * val;
                        }
                        const rms = Math.sqrt(sumSquares / numSamples);

                        // 연속 5청크(약 100ms) 이상 임계값을 넘어야 트리거 — 기침/잡음 무시
                        if (rms > 300) {
                            loudChunks++;
                            if (loudChunks >= 5 && !bargeInTriggered) {
                                console.log(`[Voice] Barge-in Triggered by sustained RMS: ${rms.toFixed(2)}`);
                                bargeInTriggered = true;
                                this.stopCurrentAudio(guildId);
                                this.ws.send(JSON.stringify({ type: 'stop_audio' }));
                            }
                        } else {
                            loudChunks = 0;
                        }
                    } else {
                        // 봇이 조용하면 barge-in 상태 초기화 (다음 재생을 위해)
                        loudChunks = 0;
                        bargeInTriggered = false;
                    }

                    // PCM 데이터를 그대로 전송 (Python에서 VAD 처리)
                    this.ws.send(pcmChunk);
                } else {
                    // 잠금 상태가 아니거나 다른 유저인 경우, bargeInTriggered 초기화
                    // (다음에 다시 말할 때를 위해)
                    if (this.activeUserId !== userId) {
                        bargeInTriggered = false;
                    }
                }
            });

            opusDecoder.on('error', (error) => {
                console.error(`[Voice] Opus 디코더 오류:`, error);
                this.recordingStreams.delete(streamKey);
            });

            audio.on('error', (error) => {
                opusDecoder.destroy();
                this.recordingStreams.delete(streamKey);
            });

            this.recordingStreams.set(streamKey, audio);

        } catch (error) {
            console.error(`[Voice] 사용자 ${userId} 녹음 시작 실패:`, error);
        }
    }

    stopCurrentAudio(guildId) {
        // 큐 초기화
        this.audioQueue = [];
        this.isPlaying = false;

        const connection = this.connections.get(guildId);
        if (connection && connection.state.subscription) {
            console.log('[Voice] Stopping current audio playback');
            connection.state.subscription.player.stop();
        }
    }

    stopListening(guildId) {
        this.isListening.set(guildId, false);
        this.listeningMode.set(guildId, null);

        // 스트림 키는 `${guildId}_${userId}` — 해당 길드의 모든 사용자 구독을 끊는다.
        // (남겨두면 receiver가 같은 스트림을 재사용해 전사 모드 구독과 충돌한다)
        for (const [key, stream] of [...this.recordingStreams]) {
            if (key === guildId || key.startsWith(`${guildId}_`)) {
                try { stream.destroy(); } catch (e) { /* 이미 닫힘 */ }
                this.recordingStreams.delete(key);
            }
        }
        this.activeUserId = null;
        // 듣지 않는 동안엔 파이썬이 먼저 말을 걸지 않게 (입장 인사)
        if (this.wsConnected) {
            this.ws.send(JSON.stringify({ type: 'set_mode', mode: 'off' }));
        }
        console.log('음성 감지 중단됨');
    }

    async processAudioBuffer(guildId, audioChunks, userId) {
        if (!this.wsConnected) {
            console.log('[WS] Not connected, skipping audio');
            return;
        }

        try {
            const buffer = Buffer.concat(audioChunks);

            // Stereo -> Mono 변환
            const monoLength = Math.floor(buffer.length / 4) * 2;
            const monoBuffer = Buffer.alloc(monoLength);

            for (let i = 0; i < monoLength; i += 2) {
                const stereoIdx = i * 2;
                if (stereoIdx + 3 >= buffer.length) break;

                const left = buffer.readInt16LE(stereoIdx);
                const right = buffer.readInt16LE(stereoIdx + 2);
                const mono = Math.floor((left + right) / 2);
                monoBuffer.writeInt16LE(mono, i);
            }

            if (monoBuffer.length < 14400) { // 0.3초 미만 무시
                return;
            }

            // WebSocket으로 전송
            const base64Audio = monoBuffer.toString('base64');
            const userName = await this.getUserName(guildId, userId);

            this.ws.send(JSON.stringify({
                type: 'user_speech',
                user_id: userId,
                user_name: userName,
                audio_data: base64Audio
            }));

            console.log(`[WS] Sent audio for ${userName} (${monoBuffer.length} bytes)`);

        } catch (error) {
            console.error('[Audio] 처리 중 오류:', error);
        }
    }

    // !talk 명령어용
    async processTextAndPlay(guildId, text, connection) {
        if (this.wsConnected) {
            this.ws.send(JSON.stringify({
                type: 'text_input',
                text: text,
                user_id: 'COMMAND',
                user_name: 'COMMAND'
            }));
            console.log(`[WS] Sent text input: ${text}`);
        } else {
            console.log('[WS] Not connected, cannot process text');
        }
    }
}

module.exports = VoiceHandler;

