// 로컬 제어 API — 컨트롤 앱이 봇에게 명령을 내리는 통로
// 127.0.0.1 전용 (외부 노출 금지)

const http = require('http');

const PORT = process.env.CONTROL_PORT ? parseInt(process.env.CONTROL_PORT) : 3939;

function json(res, status, data) {
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(data));
}

function readBody(req) {
    return new Promise((resolve, reject) => {
        let body = '';
        req.on('data', c => { body += c; });
        req.on('end', () => {
            try { resolve(body ? JSON.parse(body) : {}); }
            catch (e) { reject(e); }
        });
        req.on('error', reject);
    });
}

function startControlServer(client, voiceHandler, transcribeHandler) {
    const server = http.createServer(async (req, res) => {
        try {
            // GET /status — 봇 준비 상태와 참여 중인 길드
            if (req.method === 'GET' && req.url === '/status') {
                const connected = [...voiceHandler.connections.keys()];
                return json(res, 200, {
                    ready: client.isReady(),
                    user: client.user ? client.user.tag : null,
                    connected: connected,
                });
            }

            // GET /guilds — 길드 목록 + 보이스 채널 + 연결/모드 상태
            if (req.method === 'GET' && req.url === '/guilds') {
                const guilds = client.guilds.cache.map(g => ({
                    id: g.id,
                    name: g.name,
                    connected: voiceHandler.isConnected(g.id),
                    mode: voiceHandler.listeningMode.get(g.id) || null,
                    transcribing: transcribeHandler ? transcribeHandler.isActive(g.id) : false,
                    textChannels: g.channels.cache
                        .filter(c => c.type === 0) // ChannelType.GuildText
                        .map(c => ({ id: c.id, name: c.name })),
                    voiceChannels: g.channels.cache
                        .filter(c => c.type === 2) // ChannelType.GuildVoice
                        .map(c => ({ id: c.id, name: c.name })),
                }));
                return json(res, 200, guilds);
            }

            if (req.method === 'POST') {
                const data = await readBody(req);

                // POST /join {channelId} — 보이스 채널 참여
                if (req.url === '/join') {
                    const channel = await client.channels.fetch(data.channelId);
                    if (!channel || channel.type !== 2) {
                        return json(res, 400, { error: '보이스 채널이 아닙니다' });
                    }
                    await voiceHandler.joinChannel(channel);
                    return json(res, 200, { ok: true, guildId: channel.guild.id });
                }

                // POST /leave {guildId}
                if (req.url === '/leave') {
                    if (transcribeHandler && transcribeHandler.isActive(data.guildId)) {
                        await transcribeHandler.stop(data.guildId);
                    }
                    await voiceHandler.leaveChannel(data.guildId);
                    return json(res, 200, { ok: true });
                }

                // POST /transcribe {action: "start"|"stop", voiceChannelId, textChannelId, guildId}
                if (req.url === '/transcribe') {
                    if (!transcribeHandler) {
                        return json(res, 501, { error: '전사 핸들러가 없습니다' });
                    }

                    if (data.action === 'stop') {
                        const result = await transcribeHandler.stop(data.guildId);
                        return json(res, 200, result);
                    }

                    const voiceChannel = await client.channels.fetch(data.voiceChannelId);
                    if (!voiceChannel || voiceChannel.type !== 2) {
                        return json(res, 400, { error: '보이스 채널이 아닙니다' });
                    }
                    const textChannel = data.textChannelId
                        ? await client.channels.fetch(data.textChannelId)
                        : null;
                    if (data.textChannelId && (!textChannel || textChannel.type !== 0)) {
                        return json(res, 400, { error: '텍스트 채널이 아닙니다' });
                    }

                    const result = await transcribeHandler.start(
                        voiceChannel, textChannel, data.preset || null);
                    if (!result.ok) {
                        if (result.reason === 'already') {
                            return json(res, 409, { error: '이미 전사 모드가 켜져 있습니다' });
                        }
                        return json(res, 409, {
                            error: result.detail || '전사 모드를 시작할 수 없습니다',
                            reason: result.reason,
                        });
                    }
                    return json(res, 200, result);
                }

                // POST /mode {guildId, mode: "auto"|"always"|"call"|"stop"}
                // POST /talk {guildId, text} — 컨트롤 앱 대화 탭에서 타이핑한 말.
                // 음성으로 말한 것과 같은 경로를 탄다 (!talk과 동일) — 기억·기록에도 남는다
                if (req.url === '/talk') {
                    const text = (data.text || '').trim();
                    if (!text) return json(res, 400, { error: '보낼 말이 비어 있습니다' });
                    const connection = voiceHandler.connections.get(data.guildId);
                    if (!connection) {
                        return json(res, 409, { error: '음성 채널에 먼저 참여하세요 (목소리로 답합니다)' });
                    }
                    if (transcribeHandler && transcribeHandler.isActive(data.guildId)) {
                        return json(res, 409, { error: '전사 모드 중에는 대화할 수 없습니다' });
                    }
                    await voiceHandler.processTextAndPlay(data.guildId, text, connection);
                    return json(res, 200, { ok: true });
                }

                if (req.url === '/mode') {
                    if (data.mode === 'stop') {
                        voiceHandler.stopListening(data.guildId);
                    } else if (transcribeHandler && transcribeHandler.isActive(data.guildId)) {
                        return json(res, 409, { error: '전사 모드 중에는 응답 모드를 켤 수 없습니다' });
                    } else {
                        const ok = await voiceHandler.startListening(data.guildId, 'CONTROL', data.mode);
                        if (!ok) return json(res, 409, { error: '음성 연결이 준비되지 않았습니다 (먼저 채널에 참여하세요)' });
                    }
                    return json(res, 200, { ok: true });
                }
            }

            return json(res, 404, { error: 'not found' });
        } catch (e) {
            console.error('[Control] Error:', e);
            return json(res, 500, { error: e.message });
        }
    });

    server.listen(PORT, '127.0.0.1', () => {
        console.log(`[Control] API listening on http://127.0.0.1:${PORT}`);
    });
    return server;
}

module.exports = { startControlServer };
