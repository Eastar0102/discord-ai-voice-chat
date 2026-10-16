// 1. 주요 클래스 가져오기
const { Client, Events, GatewayIntentBits } = require('discord.js');
const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

// 음성 핸들러 가져오기
const axios = require('axios');
// Python 쪽 주소 — transcribe_handler.js와 같은 규칙
const API_BASE_URL = process.env.API_BASE_URL || 'http://localhost:8000';

// 캐릭터 이름 — config/config.json의 character가 가리키는 팩에서 읽는다 (!<이름> 명령에 쓴다)
const fs = require('fs');
let CHARACTER_NAME = 'Companion';
try {
    const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'config', 'config.json'), 'utf8'));
    const pack = JSON.parse(fs.readFileSync(
        path.join(__dirname, '..', 'characters', cfg.character || 'example', 'character.json'), 'utf8'));
    CHARACTER_NAME = pack.name || CHARACTER_NAME;
} catch (e) {
    console.warn('[Config] 캐릭터 이름을 읽지 못했습니다:', e.message);
}

const VoiceHandler = require('./voice_handler');
const TranscribeHandler = require('./transcribe_handler');

// 환경변수에서 토큰 로드
const token = process.env.DISCORD_TOKEN;

// 2. 클라이언트 객체 생성 (Guilds관련, 메시지관련 인텐트 추가)
const client = new Client({ 
    intents: [
        GatewayIntentBits.Guilds, 
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent,
        GatewayIntentBits.GuildVoiceStates,
    ]
});

// VoiceHandler 인스턴스 생성 (클라이언트 전달)
const voiceHandler = new VoiceHandler(client);

// 전사(스크립트) 모드 핸들러 — 어시스턴트와 상호 배타적으로 동작
const transcribeHandler = new TranscribeHandler(client, voiceHandler);

// 3. 봇이 준비됐을때 한번만(once) 표시할 메시지
client.once(Events.ClientReady, readyClient => {
    console.log(`Ready! Logged in as ${readyClient.user.tag}`);

    // 의존성 리포트 출력
    const { generateDependencyReport } = require('@discordjs/voice');
    console.log('\n=== Discord Voice 의존성 리포트 ===');
    console.log(generateDependencyReport());
    console.log('===================================\n');

    // 컨트롤 앱용 로컬 제어 API 시작
    const { startControlServer } = require('./control_server');
    startControlServer(client, voiceHandler, transcribeHandler);
});

// 봇이 있는 음성 채널에 누가 들어오고 나가는지 — 참여자 명단과 [현재 상황]을 바로 갱신
client.on(Events.VoiceStateUpdate, (oldState, newState) => {
    try { voiceHandler.onVoiceStateUpdate(oldState, newState); } catch (e) { console.warn('[Voice] 입장·퇴장 처리 실패:', e.message); }
});

// 전사 프리셋 목록을 사람이 읽을 형태로 (config가 유일한 정의처이므로 서버에서 받아온다)
async function formatPresetList(handler) {
    const data = await handler.listPresets();
    if (!data || !data.presets?.length) {
        return '전사 모델 목록을 가져올 수 없습니다 (Python 서버가 켜져 있나요?).';
    }
    const lines = data.presets.map(p => {
        const mark = p.name === data.default ? ' ← 기본' : '';
        const vram = p.vram_mb ? `VRAM ~${(p.vram_mb / 1024).toFixed(1)}GB` : 'VRAM 미사용';
        return `• \`${p.name}\` — ${p.label} / ${vram}${mark}`;
    });
    return `**전사 모델 프리셋** (무거울수록 정확하고 느립니다)\n${lines.join('\n')}` +
           `\n\n사용법: \`!script <프리셋>\` (예: \`!script max\`)`;
}

// 4. 메시지 명령어 처리
/** 링크의 본문을 Python에 넘겨 [지금 보고 있는 것]에 걸고, 음성으로 한마디 하게 한다.
 *  기사를 읊지는 않는다 — 프롬프트가 "읽은 사람답게 한두 가지를 짚는다"고 지시한다. */
async function readLink(message, url) {
    const note = await message.reply(`읽어볼게요… ${url.slice(0, 60)}`);
    let info;
    try {
        const res = await axios.post(`${API_BASE_URL}/read`, { url }, { timeout: 45000 });
        info = res.data;
    } catch (e) {
        const why = e.response?.data?.detail || e.message;
        await note.edit(`읽지 못했어요: ${why}`);
        return;
    }
    await note.edit(`읽었어요 — ${info.title || info.url} (${info.chars}자)`);

    // 음성에 들어와 있으면 한마디 한다. 아니면 읽어만 둔다 (다음 대화에서 쓰인다)
    const connection = voiceHandler.connections.get(message.guild.id);
    if (connection) {
        await voiceHandler.processTextAndPlay(
            message.guild.id, '(지금 보고 있는 글을 방금 읽었다. 한두 가지를 짚어 말한다)', connection);
    }
}

/** 화면을 보게 한다. 소스 이름은 config의 screen.sources (OBS의 소스처럼 미리 정의). */
async function lookAt(message, sourceName, mode) {
    const note = await message.reply(
        `볼게요… ${sourceName || '(기본 소스)'}${mode === 'ocr' ? ' (글자만)' : ''}`);
    let info;
    try {
        const res = await axios.post(`${API_BASE_URL}/look`,
            { source: sourceName || null, mode: mode || 'both' }, { timeout: 240000 });
        info = res.data;
    } catch (e) {
        await note.edit(`못 봤어요: ${e.response?.data?.detail || e.message}`);
        return;
    }
    const took = Object.entries(info.took || {}).map(([k, v]) => `${k} ${v}s`).join(' · ');
    await note.edit(`봤어요 — ${info.source} · 글자 ${info.ocr_lines}줄 · ${took}`);

    const connection = voiceHandler.connections.get(message.guild.id);
    if (connection) {
        await voiceHandler.processTextAndPlay(
            message.guild.id, '(지금 보고 있는 화면을 방금 봤다. 한두 가지를 짚어 말한다)', connection);
    }
}

client.on('messageCreate', async (message) => {
    // 봇의 메시지는 무시
    if (message.author.bot) return;

    const content = message.content.toLowerCase().trim();

    // !join 명령어
    if (content === '!join') {
        // 사용자가 음성 채널에 있는지 확인
        const voiceChannel = message.member?.voice?.channel;
        if (!voiceChannel) {
            await message.reply('음성 채널에 먼저 들어가주세요.');
            return;
        }

        try {
            await voiceHandler.joinChannel(voiceChannel);
            await message.reply(`✅ ${voiceChannel.name}에 연결되었습니다. 말하기 시작하세요!`);
        } catch (error) {
            console.error('음성 채널 연결 오류:', error);
            await message.reply('음성 채널 연결에 실패했습니다.');
        }
    }

    // !leave 명령어
    else if (content === '!leave') {
        if (voiceHandler.isConnected(message.guild.id)) {
            if (transcribeHandler.isActive(message.guild.id)) {
                await transcribeHandler.stop(message.guild.id);
            }
            await voiceHandler.leaveChannel(message.guild.id);
            await message.reply('✅ 음성 채널에서 나갔습니다.');
        } else {
            await message.reply('음성 채널에 연결되어 있지 않습니다.');
        }
    }

    // 화면 보기 — !봐줘 [소스] [글자]. "글자"를 붙이면 OCR만 써서 1초에 끝난다
    else if (content.startsWith('!봐줘') || content.startsWith('!봐') || content.startsWith('!look')) {
        const args = message.content.trim().split(/\s+/).slice(1);
        const ocrOnly = args.some(a => /^(글자|ocr|텍스트)$/i.test(a));
        const name = args.find(a => !/^(글자|ocr|텍스트)$/i.test(a)) || null;
        await lookAt(message, name, ocrOnly ? 'ocr' : 'both');
    }

    // !소스 — 볼 수 있는 소스와 지금 열린 창 목록
    else if (content.startsWith('!소스') || content.startsWith('!sources')) {
        try {
            const [s, w] = await Promise.all([
                axios.get(`${API_BASE_URL}/look/sources`, { timeout: 15000 }),
                axios.get(`${API_BASE_URL}/look/windows`, { timeout: 15000 }),
            ]);
            const names = Object.keys(s.data.sources || {});
            const wins = (w.data.windows || []).slice(0, 12)
                .map(x => `  ${x.w}x${x.h}  ${x.title}`);
            const out = [
                `**소스** (기본: ${s.data.default})`,
                names.length ? names.map(n => '  ' + n).join('\n') : '  없음',
                '',
                '**지금 열린 창**',
                '```',
                wins.join('\n') || '  (없음)',
                '```',
            ].join('\n');
            await message.reply(out);
        } catch (e) {
            await message.reply(`목록을 못 가져왔어요: ${e.response?.data?.detail || e.message}`);
        }
    }

    // 링크 읽기 — 음성으로는 URL을 말할 수 없으니 텍스트 채널로 받는다.
    // !read <url>, !보기 <url>, 또는 링크만 붙여도 읽는다 (봇이 음성에 들어와 있을 때만)
    else if (content.startsWith('!read') || content.startsWith('!보기') || content.startsWith('!읽어')) {
        const m = message.content.match(/https?:\/\/\S+/);
        if (!m) {
            // 인자 없이 부르면 치운다 — "그 기사 얘기는 그만" 용도
            await axios.post(`${API_BASE_URL}/read`, {}).catch(() => {});
            await message.reply('보고 있던 글을 치웠어요.');
            return;
        }
        await readLink(message, m[0]);
    }

    // 링크만 붙였을 때도 읽는다. 명령을 외우지 않아도 되게 — 음성 대화 중에 링크를 던지는
    // 게 자연스러운 흐름이다. 음성에 들어와 있지 않으면 남들끼리 공유한 링크일 수 있어 건드리지 않는다
    else if (/^https?:\/\/\S+$/.test(message.content.trim())
             && voiceHandler.isConnected(message.guild.id)) {
        await readLink(message, message.content.trim());
    }

    // !talk <메시지> 명령어
    else if (content.startsWith('!talk ')) {
        const text = message.content.slice(6); // '!talk ' 제거

        if (!voiceHandler.isConnected(message.guild.id)) {
            await message.reply('먼저 `!join` 명령어로 음성 채널에 연결해주세요.');
            return;
        }

        // 연결 가져오기
        const connection = voiceHandler.connections.get(message.guild.id);
        if (!connection) {
            await message.reply('음성 연결이 없습니다. `!join`을 다시 실행해주세요.');
            return;
        }

        // 채팅 API로 처리하고 음성으로 재생
        console.log(`[Command] !talk with message: ${text}`);
        await voiceHandler.processTextAndPlay(message.guild.id, text, connection);
    }

           // !auto 명령어 - 스스로 판단해서 답함 (기본)
           else if (content === '!auto' || content === `!${CHARACTER_NAME}`) {
               if (!voiceHandler.isConnected(message.guild.id)) {
                   await message.reply('먼저 `!join` 명령어로 음성 채널에 연결해주세요.');
                   return;
               }

               if (transcribeHandler.isActive(message.guild.id)) {
                   await message.reply('전사 모드가 켜져 있습니다. `!scriptstop`으로 먼저 종료해주세요.');
                   return;
               }

               try {
                   const success = await voiceHandler.startListening(message.guild.id, message.author.id, 'auto');
                   if (success) {
                       await message.reply('✅ **자동 모드**를 시작했습니다. 들리는 말 중 자기에게 한 말에만 답합니다. `!always`(전부 답함) · `!call`(이름을 불러야) · `!stop`(중단)');
                   } else {
                       await message.reply('❌ 음성 감지를 시작할 수 없습니다.');
                   }
               } catch (error) {
                   console.error('음성 감지 시작 오류:', error);
                   await message.reply('음성 감지 시작에 실패했습니다.');
               }
           }

           // !always 명령어 - 항상 반응 모드
           else if (content === '!always') {
               if (!voiceHandler.isConnected(message.guild.id)) {
                   await message.reply('먼저 `!join` 명령어로 음성 채널에 연결해주세요.');
                   return;
               }

               if (transcribeHandler.isActive(message.guild.id)) {
                   await message.reply('전사 모드가 켜져 있습니다. `!scriptstop`으로 먼저 종료해주세요.');
                   return;
               }

               try {
                   const success = await voiceHandler.startListening(message.guild.id, message.author.id, 'always');
                   if (success) {
                       await message.reply('✅ **항상 반응 모드**를 시작했습니다. 모든 음성을 감지합니다. `!stop`으로 중단할 수 있습니다.');
                   } else {
                       await message.reply('❌ 음성 감지를 시작할 수 없습니다.');
                   }
               } catch (error) {
                   console.error('음성 감지 시작 오류:', error);
                   await message.reply('음성 감지 시작에 실패했습니다.');
               }
           }

           // !call 명령어 - 호출어 모드 (이름을 불러야만 반응)
           else if (content === '!call') {
               if (!voiceHandler.isConnected(message.guild.id)) {
                   await message.reply('먼저 `!join` 명령어로 음성 채널에 연결해주세요.');
                   return;
               }

               if (transcribeHandler.isActive(message.guild.id)) {
                   await message.reply('전사 모드가 켜져 있습니다. `!scriptstop`으로 먼저 종료해주세요.');
                   return;
               }

               try {
                   const success = await voiceHandler.startListening(message.guild.id, message.author.id, 'call');
                   if (success) {
                       await message.reply(`✅ **호출어 모드**를 시작했습니다. ${CHARACTER_NAME}에게 말을 걸 때 이름을 불러주십시오. \`!stop\`으로 중단할 수 있습니다.`);
                   } else {
                       await message.reply('❌ 음성 감지를 시작할 수 없습니다.');
                   }
               } catch (error) {
                   console.error('음성 감지 시작 오류:', error);
                   await message.reply('음성 감지 시작에 실패했습니다.');
               }
           }

           // !scriptmodels 명령어 - 고를 수 있는 전사 모델(프리셋) 목록
           // !원고 [소스] — 원고를 같이 읽기 시작. 화면 글자를 주기적으로 읽어 들고,
           // 새로 쓴 대목에 가끔 한마디 한다 (끜어들지 않는 줍으로 기울어 있다)
           else if (content === '!원고' || content.startsWith('!원고 ')
                    || content === '!manuscript' || content.startsWith('!manuscript ')) {
               const source = content.split(/\s+/)[1] || null;
               try {
                   const res = await axios.post(`${API_BASE_URL}/manuscript`,
                       { on: true, source }, { timeout: 60000 });
                   const d = res.data;
                   await message.reply(
                       `원고 보기 시작 — 소스 '${d.source}' · 지금 ${d.chars}자 읽었어요.`
                       + (d.chars ? '' : ` (${d.note || '아무것도 안 읽혔어요 — 그 창이 맨 앞인지 보여 주세요'})`)
                       + '  `!대화`로 끄고, `!자동`이면 알아서 전환합니다.');
               } catch (e) {
                   await message.reply(`원고 보기 실패: ${e.response?.data?.detail || e.message}`);
               }
           }

           // !대화 — 글쓰기 상태를 끊고 대화로 고정
           else if (content === '!대화' || content === '!원고중지' || content === '!manuscriptstop') {
               try {
                   await axios.post(`${API_BASE_URL}/manuscript`, { on: false }, { timeout: 30000 });
                   await message.reply('대화 상태로 뒀어요. (`!자동`이면 원고를 고칠 때 저절로 넘어갑니다)');
               } catch (e) {
                   await message.reply(`실패: ${e.response?.data?.detail || e.message}`);
               }
           }

           // !자동 — 고정을 풀고 알아서 전환하게 (원고 수정·소설 이야기로 들어간다)
           else if (content === '!자동') {
               try {
                   const res = await axios.post(`${API_BASE_URL}/manuscript`,
                       { auto: true }, { timeout: 30000 });
                   await message.reply(`자동으로 뒀어요 — 지금은 '${res.data.state}'. `
                       + '원고를 고치거나 글 얘기를 꺼내시면 글쓰기로 넘어갑니다.');
               } catch (e) {
                   await message.reply(`실패: ${e.response?.data?.detail || e.message}`);
               }
           }

           else if (content === '!scriptmodels' || content === '!기록모델') {
               await message.reply(await formatPresetList(transcribeHandler));
           }

           // !script [프리셋] 명령어 - 전사(회의록) 모드 시작
           // 예: !script / !script max / !script cpu — 무거운 모델일수록 정확하고 느리다
           else if (content === '!script' || content === '!transcribe' || content === '!기록'
                    || content.startsWith('!script ') || content.startsWith('!transcribe ')
                    || content.startsWith('!기록 ')) {
               const preset = content.split(/\s+/)[1] || null;
               const voiceChannel = message.member?.voice?.channel;
               if (!voiceChannel) {
                   await message.reply('음성 채널에 먼저 들어가주세요.');
                   return;
               }

               if (transcribeHandler.isActive(message.guild.id)) {
                   await message.reply('이미 전사 모드가 켜져 있습니다. `!scriptstop`으로 종료하세요.');
                   return;
               }

               try {
                   // 무거운 프리셋은 모델 로딩에 수십 초가 걸린다 — 먼저 알려준다
                   const loading = await message.reply(
                       `⏳ 전사 모델을 준비하는 중입니다${preset ? ` (\`${preset}\`)` : ''}...`);

                   const result = await transcribeHandler.start(voiceChannel, message.channel, preset);
                   if (!result.ok) {
                       if (result.reason === 'unknown_preset') {
                           await loading.edit(`❌ 알 수 없는 프리셋입니다.\n\n${await formatPresetList(transcribeHandler)}`);
                       } else {
                           await loading.edit(`❌ 전사 모드를 시작할 수 없습니다: ${result.detail || '알 수 없는 오류'}`);
                       }
                       return;
                   }

                   const stt = result.stt || {};
                   const note = result.stoppedAssistant
                       ? '\n(전사 전용 모드라서 캐릭터의 응답 모드는 중단했습니다.)'
                       : '';
                   await loading.edit(
                       `📝 **전사 모드**를 시작했습니다. \`${voiceChannel.name}\`의 대화를 이 채널에 받아 적습니다.` +
                       `\n모델: \`${stt.preset}\` — ${stt.label} (로딩 ${stt.load_sec}초)` +
                       `\n\`!scriptstop\`으로 종료하면 전체 기록을 파일로 올립니다.${note}`);
               } catch (error) {
                   console.error('전사 모드 시작 오류:', error);
                   await message.reply('전사 모드 시작에 실패했습니다.');
               }
           }

           // !scriptstop 명령어 - 전사 모드 종료
           else if (content === '!scriptstop' || content === '!기록중지') {
               if (!transcribeHandler.isActive(message.guild.id)) {
                   await message.reply('전사 모드가 켜져 있지 않습니다.');
                   return;
               }

               await message.channel.send('⏳ 남은 전사를 마무리하는 중...');
               const result = await transcribeHandler.stop(message.guild.id);
               if (!result.lines) {
                   await message.channel.send('⏹️ 전사를 마쳤습니다. 기록된 발화가 없습니다.');
               }
           }

           // !stop 명령어 - 음성 감지 중단
           else if (content === '!stop') {
               if (!voiceHandler.isConnected(message.guild.id)) {
                   await message.reply('음성 채널에 연결되어 있지 않습니다.');
                   return;
               }

               voiceHandler.stopListening(message.guild.id);
               await message.reply('⏹️ 음성 감지를 중단했습니다.');
           }

    // ping 테스트
    else if (content === 'ping') {
        await message.reply('pong');
    }
});

// 5. 시크릿키(토큰)을 통해 봇 로그인 실행
if (!token) {
    console.error('❌ DISCORD_TOKEN 환경변수가 설정되지 않았습니다.');
    process.exit(1);
}

client.login(token);