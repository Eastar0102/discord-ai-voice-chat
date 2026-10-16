// Companion Control — 메인 프로세스
// Python API 서버와 Discord 봇을 자식 프로세스로 관리하고,
// 상태 폴링·로그 중계·봇 제어 API 프록시를 담당한다.

const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn, exec } = require('child_process');
const path = require('path');
const fs = require('fs');

const API_URL = 'http://127.0.0.1:8000';
const CONTROL_URL = 'http://127.0.0.1:3939';

// ── 프로젝트 루트 탐색 ──────────────────────────────────────
// 실행 위치에서 위로 올라가며 src/api_server.py가 있는 디렉토리를 찾는다.
// 포터블 exe는 app/dist/ 안에서 실행되므로 2~3단계 위가 루트.
function findProjectRoot() {
    const bases = [];
    if (process.env.COMPANION_ROOT) bases.push(process.env.COMPANION_ROOT);
    if (process.env.PORTABLE_EXECUTABLE_DIR) bases.push(process.env.PORTABLE_EXECUTABLE_DIR);
    bases.push(__dirname);

    for (const base of bases) {
        let dir = base;
        for (let i = 0; i < 6; i++) {
            if (fs.existsSync(path.join(dir, 'src', 'api_server.py'))) return dir;
            const parent = path.dirname(dir);
            if (parent === dir) break;
            dir = parent;
        }
    }
    return null;
}

const ROOT = findProjectRoot();

// 화면 언어 — 렌더러가 고른 값을 알려 준다 (로그·오류 문구만 여기서 만든다)
let LANG = 'ko';
const MSG = {
    exited: { ko: (c) => `\n[프로세스 종료 (code ${c})]\n`, en: (c) => `\n[process exited (code ${c})]\n` },
    started: { ko: (cmd) => `[시작: ${cmd}]\n`, en: (cmd) => `[started: ${cmd}]\n` },
    waiting: { ko: () => 'API 서버 기동 대기 중 (모델 로딩에 수십 초 걸릴 수 있음)...\n',
               en: () => 'Waiting for the API server (loading models can take tens of seconds)...\n' },
    ready: { ko: () => 'API 서버 준비 완료 — 봇을 시작합니다.\n', en: () => 'API server ready — starting the bot.\n' },
    diedEarly: { ko: () => 'API 서버가 시작 직후 종료됐습니다. 서버 로그를 확인하세요.',
                 en: () => 'The API server exited right after starting. Check the server log.' },
    timeout: { ko: () => 'API 서버가 2분 내에 준비되지 않았습니다.', en: () => 'The API server was not ready within 2 minutes.' },
    restarting: { ko: () => '\n[캐릭터를 바꿔 API 서버를 다시 시작합니다]\n', en: () => '\n[Restarting the API server for the new character]\n' },
    notOwned: { ko: () => '이 앱이 띄운 서버가 아닙니다. 서버를 직접 다시 시작하세요.',
                en: () => 'This server was not started by the app. Restart it yourself.' },
    noRoot: { ko: () => '프로젝트 루트를 찾을 수 없습니다.\nexe를 프로젝트 폴더(app/dist) 안에 두거나, COMPANION_ROOT 환경변수를 설정하세요.',
              en: () => 'Could not find the project root.\nKeep the exe inside the project (app/dist), or set the COMPANION_ROOT environment variable.' },
};
const M = (key, ...a) => (MSG[key][LANG] || MSG[key].ko)(...a);

// ── 자식 프로세스 관리 ──────────────────────────────────────
const procs = { server: null, bot: null };
let win = null;

function send(channel, data) {
    if (win && !win.isDestroyed()) win.webContents.send(channel, data);
}

function procState() {
    return { server: !!procs.server, bot: !!procs.bot };
}

function wire(name, p) {
    procs[name] = p;
    p.stdout.on('data', d => send('log', { source: name, line: d.toString() }));
    p.stderr.on('data', d => send('log', { source: name, line: d.toString() }));
    p.on('exit', code => {
        procs[name] = null;
        send('log', { source: name, line: M('exited', code) });
        send('proc-state', procState());
    });
    send('proc-state', procState());
}

function startServer() {
    if (procs.server) return;
    const venvPy = path.join(ROOT, 'venv', 'Scripts', 'python.exe');
    const py = fs.existsSync(venvPy) ? venvPy : 'python';
    // --reload는 자식 프로세스를 또 만들어 종료가 꼬이므로 앱에서는 사용하지 않음
    // --no-access-log: 상태 폴링(/health)이 로그를 도배하는 것 방지
    const p = spawn(py, ['-m', 'uvicorn', 'src.api_server:app', '--host', '127.0.0.1', '--port', '8000', '--no-access-log'], {
        cwd: ROOT,
        env: { ...process.env, PYTHONUTF8: '1' }, // 파이프 stdout이 cp949가 되는 것 방지
    });
    send('log', { source: 'server', line: M('started', py) });
    wire('server', p);
}

function startBot() {
    if (procs.bot) return;
    const p = spawn('node', ['index.js'], { cwd: path.join(ROOT, 'discord_bot'), shell: true });
    send('log', { source: 'bot', line: M('started', 'node index.js') });
    wire('bot', p);
}

function stopProc(name) {
    const p = procs[name];
    if (!p) return;
    // uvicorn/node가 자식을 가질 수 있으므로 트리 전체 강제 종료
    exec(`taskkill /pid ${p.pid} /T /F`);
}

async function jfetch(url, options = {}) {
    const res = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        signal: AbortSignal.timeout(5000),
        ...options,
    });
    return res.json();
}

async function apiHealthy() {
    try {
        const res = await fetch(`${API_URL}/health`, { signal: AbortSignal.timeout(1500) });
        return res.ok;
    } catch {
        return false;
    }
}

// "한번에 시작": 서버 기동 → 헬스 통과 대기 → 봇 기동
async function startAll() {
    startServer();
    send('log', { source: 'app', line: M('waiting') });
    for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 2000));
        if (await apiHealthy()) {
            send('log', { source: 'app', line: M('ready') });
            startBot();
            return { ok: true };
        }
        if (!procs.server) return { ok: false, error: M('diedEarly') };
    }
    return { ok: false, error: M('timeout') };
}

// 캐릭터를 바꾼 뒤 — 호출어·프롬프트·기억이 서버가 뜰 때 정해지므로 다시 띄운다.
// 이 앱이 띄운 서버만 다시 시작할 수 있다 (다른 곳에서 띄운 서버는 손대지 않는다)
async function restartServer() {
    if (!procs.server) return { ok: false, owned: false, error: M('notOwned') };
    send('log', { source: 'server', line: M('restarting') });
    stopProc('server');
    for (let i = 0; i < 50 && procs.server; i++) await new Promise(r => setTimeout(r, 200));
    // 포트가 풀릴 때까지 조금 더 (taskkill 직후엔 8000이 잠깐 남아 있다)
    for (let i = 0; i < 25 && await apiHealthy(); i++) await new Promise(r => setTimeout(r, 200));
    startServer();
    return { ok: true, owned: true };
}

// ── 상태 폴링 ───────────────────────────────────────────────
setInterval(async () => {
    const api = await apiHealthy();
    let bot = null;
    try { bot = await jfetch(`${CONTROL_URL}/status`); } catch {}
    send('status', { api, bot, procs: procState() });
}, 3000);

// ── IPC ─────────────────────────────────────────────────────
ipcMain.handle('proc:start', (e, name) => (name === 'server' ? startServer() : startBot()));
ipcMain.handle('proc:stop', (e, name) => stopProc(name));
ipcMain.handle('proc:startAll', () => startAll());
ipcMain.handle('discord:guilds', () => jfetch(`${CONTROL_URL}/guilds`));
ipcMain.handle('discord:join', (e, channelId) =>
    jfetch(`${CONTROL_URL}/join`, { method: 'POST', body: JSON.stringify({ channelId }) }));
ipcMain.handle('discord:leave', (e, guildId) =>
    jfetch(`${CONTROL_URL}/leave`, { method: 'POST', body: JSON.stringify({ guildId }) }));
// 대화 탭에서 타이핑한 말 — LLM·TTS를 거쳐 목소리로 나가므로 오래 걸린다
ipcMain.handle('discord:talk', (e, guildId, text) =>
    jfetch(`${CONTROL_URL}/talk`, {
        method: 'POST',
        body: JSON.stringify({ guildId, text }),
        signal: AbortSignal.timeout(120000),
    }));
ipcMain.handle('discord:mode', (e, guildId, mode) =>
    jfetch(`${CONTROL_URL}/mode`, { method: 'POST', body: JSON.stringify({ guildId, mode }) }));
// 전사 모드 — 프리셋은 Python이 들고 있고, 시작·중지는 봇의 control 서버가 받는다.
// 세션 시작 때 모델을 올리므로 5초로는 모자란다 (accurate/max는 +3.6GB를 읽어 들인다)
// 화면 보기 — VLM은 8~21초라 기본 5초 타임아웃으로는 모자란다
ipcMain.handle('look:sources', () => jfetch(`${API_URL}/look/sources`));
ipcMain.handle('look:windows', () => jfetch(`${API_URL}/look/windows`));
ipcMain.handle('look:now', (e, body) =>
    jfetch(`${API_URL}/look`, {
        method: 'POST',
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(240000),
    }));
ipcMain.handle('look:reading', () => jfetch(`${API_URL}/read`));
ipcMain.handle('look:readUrl', (e, url) =>
    jfetch(`${API_URL}/read`, {
        method: 'POST',
        body: JSON.stringify({ url }),
        signal: AbortSignal.timeout(60000),
    }));
ipcMain.handle('look:clear', () =>
    jfetch(`${API_URL}/read`, { method: 'POST', body: JSON.stringify({}) }));

ipcMain.handle('transcribe:presets', () => jfetch(`${API_URL}/transcribe/presets`));
ipcMain.handle('transcribe:control', (e, body) =>
    jfetch(`${CONTROL_URL}/transcribe`, {
        method: 'POST',
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(180000),
    }));
ipcMain.handle('inspect:state', () => jfetch(`${API_URL}/inspect/state`));
ipcMain.handle('inspect:memory', () => jfetch(`${API_URL}/inspect/memory`));
ipcMain.handle('inspect:turns', () => jfetch(`${API_URL}/inspect/turns`));
ipcMain.handle('inspect:conversation', (e, after) =>
    jfetch(`${API_URL}/inspect/conversation?after=${encodeURIComponent(after || '')}`));
ipcMain.handle('inspect:memoryMap', (e, layout) =>
    jfetch(`${API_URL}/inspect/memory/map?layout=${layout === 'tsne' ? 'tsne' : 'pca'}`));
ipcMain.handle('llm:models', () => jfetch(`${API_URL}/llm/models`));
// 로컬 모델은 VRAM 로딩까지 기다렸다 응답하므로 기본 5초 타임아웃으로는 부족
ipcMain.handle('llm:select', (e, provider, model) =>
    jfetch(`${API_URL}/llm/select`, {
        method: 'POST',
        body: JSON.stringify({ provider, model }),
        signal: AbortSignal.timeout(300000),
    }));
ipcMain.handle('tts:engines', () => jfetch(`${API_URL}/tts/engines`));
// 캐릭터 팩 — 고르면 config에 저장되고, 서버를 다시 시작해야 적용된다
ipcMain.handle('char:list', () => jfetch(`${API_URL}/characters`));
ipcMain.handle('char:select', (e, id) =>
    jfetch(`${API_URL}/characters/select`, { method: 'POST', body: JSON.stringify({ id }) }));
ipcMain.handle('proc:restartServer', () => restartServer());
ipcMain.handle('app:lang', (e, lang) => { LANG = lang === 'en' ? 'en' : 'ko'; });
// GPT-SoVITS는 별도 서버 실행·모델 로딩까지 기다렸다 응답한다
ipcMain.handle('tts:select', (e, provider) =>
    jfetch(`${API_URL}/tts/select`, {
        method: 'POST',
        body: JSON.stringify({ provider }),
        signal: AbortSignal.timeout(300000),
    }));


// ── 윈도우 ──────────────────────────────────────────────────
function createWindow() {
    win = new BrowserWindow({
        width: 1150,
        height: 800,
        title: 'Companion Control',
        backgroundColor: '#16161e',
        webPreferences: { preload: path.join(__dirname, 'preload.js') },
    });
    win.setMenuBarVisibility(false);
    win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

app.whenReady().then(() => {
    if (!ROOT) {
        dialog.showErrorBox('Companion Control', M('noRoot'));
        app.quit();
        return;
    }
    createWindow();
});

app.on('before-quit', () => {
    stopProc('server');
    stopProc('bot');
});

app.on('window-all-closed', () => app.quit());
