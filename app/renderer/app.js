const $ = (id) => document.getElementById(id);

// 캐릭터 이름 — API 서버가 캐릭터 팩에서 읽어 /inspect/state로 알려 준다 (live.js · map.js도 쓴다)
let CHAR_NAME = '캐릭터';
function refreshCharName() {
    window.companion.inspectState()
        .then((s) => { if (s && s.character) CHAR_NAME = s.character; })
        .catch(() => { setTimeout(refreshCharName, 5000); });   // 서버가 아직 안 떴으면 다시
}
refreshCharName();

// ── 로그 ────────────────────────────────────────────────────
const MAX_LOG_CHARS = 200000; // 패널당 보존 한도

function appendLog(target, line) {
    const el = target === 'bot' ? $('log-bot') : $('log-server');
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.textContent += line;
    if (el.textContent.length > MAX_LOG_CHARS) {
        el.textContent = el.textContent.slice(-MAX_LOG_CHARS);
    }
    if (atBottom) el.scrollTop = el.scrollHeight;
}

window.companion.onLog(({ source, line }) => {
    appendLog(source === 'bot' ? 'bot' : 'server', line);
});

// ── 상태 표시 ───────────────────────────────────────────────
function setDot(id, state) {
    $(id).className = 'dot ' + state;
}

window.companion.onStatus(({ api, bot, procs }) => {
    // API 서버: 프로세스 살아있음 + 헬스 통과 = on, 프로세스만 = starting
    if (api) {
        setDot('dot-api', 'on');
        $('detail-api').textContent = '실행 중 (정상)';
    } else if (procs.server) {
        setDot('dot-api', 'starting');
        $('detail-api').textContent = '기동 중 (모델 로딩)...';
    } else {
        setDot('dot-api', 'off');
        $('detail-api').textContent = '중지됨';
    }

    // 봇
    if (bot && bot.ready) {
        setDot('dot-bot', 'on');
        $('detail-bot').textContent = `로그인: ${bot.user}` + (bot.connected.length ? ` · 음성 참여 중 (${bot.connected.length})` : '');
    } else if (procs.bot) {
        setDot('dot-bot', 'starting');
        $('detail-bot').textContent = '기동 중...';
    } else {
        setDot('dot-bot', 'off');
        $('detail-bot').textContent = '중지됨';
    }

    // 봇이 준비됐는데 길드 목록이 비어 있으면 자동 로드
    if (bot && bot.ready && !guildsLoaded) refreshGuilds();

    // API 서버가 올라오면 LLM 모델 목록 자동 로드, 내려가면 다음 기동 때 다시 로드
    if (api && !llmLoaded) loadLlmModels();
    if (!api) llmLoaded = false;
    if (api && !ttsLoaded) loadTtsEngines();
    if (!api) ttsLoaded = false;
    if (api && !scriptLoaded) { scriptLoaded = true; loadScriptPresets(); }
    if (!api) scriptLoaded = false;
    if (api && !lookLoaded) { lookLoaded = true; loadLookSources(); }
    if (!api) lookLoaded = false;
});

// ── 프로세스 버튼 ───────────────────────────────────────────
$('btn-server-start').onclick = () => window.companion.start('server');
$('btn-server-stop').onclick = () => window.companion.stop('server');
$('btn-bot-start').onclick = () => window.companion.start('bot');
$('btn-bot-stop').onclick = () => window.companion.stop('bot');
$('btn-start-all').onclick = async () => {
    $('btn-start-all').disabled = true;
    try {
        const r = await window.companion.startAll();
        if (r && !r.ok) appendLog('server', `\n[한번에 시작 실패] ${r.error}\n`);
    } finally {
        $('btn-start-all').disabled = false;
    }
};

// ── Discord 제어 ────────────────────────────────────────────
let currentGuilds = [];
let guildsLoaded = false;
let scriptLoaded = false;

async function refreshGuilds() {
    try {
        currentGuilds = await window.companion.guilds();
        guildsLoaded = true;
        const sel = $('guild');
        const prev = sel.value;
        sel.innerHTML = '';
        for (const g of currentGuilds) {
            const o = document.createElement('option');
            o.value = g.id;
            o.textContent = g.name + (g.connected ? ' ✓ 참여 중' : '');
            sel.appendChild(o);
        }
        if (prev && currentGuilds.some(g => g.id === prev)) sel.value = prev;
        fillChannels();
        updateModeButtons();
        $('discord-hint').textContent = currentGuilds.length
            ? ''
            : '봇이 속한 서버가 없습니다. 초대 링크로 봇을 서버에 먼저 초대하세요.';
    } catch {
        guildsLoaded = false;
        $('discord-hint').textContent = '봇이 실행되면 서버 목록이 나타납니다.';
    }
}

function selectedGuild() {
    return currentGuilds.find(g => g.id === $('guild').value);
}

function fillChannels() {
    const g = selectedGuild();
    const sel = $('channel');
    sel.innerHTML = '';
    if (!g) return;
    for (const c of g.voiceChannels) {
        const o = document.createElement('option');
        o.value = c.id;
        o.textContent = c.name;
        sel.appendChild(o);
    }
    // 전사 기록을 올릴 텍스트 채널. 비우면 봇은 채널을 고르지 않는다 — 실시간 전송도 .txt 첨부도
    // 하지 않고 data/transcripts/*.jsonl 만 남는다 (!script는 명령을 친 채널을 쓰지만
    // 컨트롤 앱에는 그 맥락이 없다). 그래서 "알아서"가 아니라 있는 그대로 적는다
    const t = $('script-text-channel');
    const prevText = t.value;
    t.innerHTML = '<option value="">(올리지 않음 — 파일로만 저장)</option>';
    for (const c of g.textChannels || []) {
        const o = document.createElement('option');
        o.value = c.id;
        o.textContent = '#' + c.name;
        t.appendChild(o);
    }
    if (prevText && (g.textChannels || []).some(c => c.id === prevText)) t.value = prevText;
}

function updateModeButtons() {
    const g = selectedGuild();
    const mode = g ? g.mode : null;
    $('btn-mode-auto').classList.toggle('active', mode === 'auto');
    $('btn-mode-always').classList.toggle('active', mode === 'always');
    $('btn-mode-call').classList.toggle('active', mode === 'call');
    const MODE_KO = { auto: '자동 (스스로 판단)', always: '항상 반응', call: '호출어' };
    $('detail-mode').textContent = mode ? `현재: ${MODE_KO[mode] || mode}` : '';
    updateScriptButtons();
}

// ── 대화 탭에서 타이핑 ──────────────────────────────────────
// 마이크로 말한 것과 같은 경로를 탄다 (봇의 /talk → processTextAndPlay). 링크를 넣으면
// 읽게 한다 — URL은 음성으로 말할 수 없어서 애초에 텍스트로 받는 것이었다
async function sayText() {
    const box = $('say-text');
    const text = box.value.trim();
    if (!text) return;
    const g = selectedGuild();
    const url = (text.match(/https?:\/\/\S+/) || [])[0];
    const rest = url ? text.replace(url, '').trim() : text;

    $('btn-say').disabled = true;
    const hint = $('live-hint');
    try {
        if (url) {
            hint.textContent = '링크를 읽는 중…';
            const r = await window.companion.lookReadUrl(url);
            if (r && (r.detail || r.error)) throw new Error(r.detail || r.error);
            hint.textContent = `읽었습니다 — ${r.title || r.url} (${r.chars}자)`;
        }
        // 링크만 붙였으면 한마디 하게 하고, 말이 같이 있으면 그 말을 보낸다
        const msg = rest || (url ? '(지금 보고 있는 글을 방금 읽었다. 한두 가지를 짚어 말한다)' : '');
        if (msg) {
            if (!g) { hint.textContent = '서버를 먼저 고르세요 (제어 탭).'; return; }
            const r = await window.companion.talk(g.id, msg);
            if (r && r.error) throw new Error(r.error);
        }
        box.value = '';
    } catch (e) {
        hint.textContent = '보내지 못했습니다: ' + (e.message || e);
    } finally {
        $('btn-say').disabled = false;
    }
}

$('btn-say').onclick = sayText;
$('say-text').onkeydown = (e) => { if (e.key === 'Enter') sayText(); };

// ── 제어 탭 레이아웃 ────────────────────────────────────────
// 패널이 6개로 늘면서 아래쪽이 잘려 있었다. 이제 탭이 스크롤되고, 넓은 창에서는 2열이 되고,
// 안 쓰는 패널은 접을 수 있다 (접은 상태는 브라우저에 기억 — 다시 켤 때 그대로 남는다)
const COLLAPSE_KEY = 'companion.collapsed';

function readCollapsed() {
    try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]')); }
    catch { return new Set(); }   // 저장소를 못 읽어도 화면은 정상이어야 한다
}

function writeCollapsed(set) {
    try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set])); } catch { /* 무시 */ }
}

function setupControlLayout() {
    const tab = $('tab-control');
    // 패널을 쌓는 탭 전부 (.stack) — 제어·보기·전사. 접기와 스크롤을 같이 쓴다
    const stacks = [...document.querySelectorAll('.tab.stack')];
    const panels = stacks.flatMap(t => [...t.querySelectorAll('.panel[data-name]')]);
    const collapsed = readCollapsed();

    // 바로가기는 제어 탭만 — 패널이 하나뿐인 탭에는 쓸 데가 없다
    const bar = $('jump-bar');
    bar.innerHTML = '';
    for (const p of panels) {
        if (collapsed.has(p.id)) p.classList.add('collapsed');
        if (p.closest('.tab') === tab) {
            const b = document.createElement('button');
            b.textContent = p.dataset.name;
            b.onclick = () => {
                p.classList.remove('collapsed');
                collapsed.delete(p.id);
                writeCollapsed(collapsed);
                p.scrollIntoView({ behavior: 'smooth', block: 'start' });
            };
            bar.appendChild(b);
        }
        // 제목을 누르면 접힌다
        const h = p.querySelector('h2');
        if (h) h.onclick = () => {
            p.classList.toggle('collapsed');
            if (p.classList.contains('collapsed')) collapsed.add(p.id); else collapsed.delete(p.id);
            writeCollapsed(collapsed);
            updateScroller();
        };
    }

    // 위·아래 버튼 — 지금 열린 스택 탭에 스크롤할 게 있을 때만 뜬다
    const btn = $('scroller');
    const activeStack = () => stacks.find(t => t.classList.contains('active'));
    const atBottom = (t) => t.scrollTop + t.clientHeight >= t.scrollHeight - 8;
    function updateScroller() {
        const t = activeStack();
        const shown = !!t && t.scrollHeight - t.clientHeight > 20;
        btn.classList.toggle('on', shown);
        if (!shown) return;
        const down = !atBottom(t);
        btn.textContent = down ? '⌄' : '⌃';
        btn.title = down ? '아래로' : '위로';
    }
    btn.onclick = () => {
        const t = activeStack();
        if (t) t.scrollTo({ top: atBottom(t) ? 0 : t.scrollHeight, behavior: 'smooth' });
    };
    for (const t of stacks) t.addEventListener('scroll', updateScroller);
    window.addEventListener('resize', updateScroller);
    updateScroller();
    return updateScroller;
}

// 스크립트가 </body> 직전에 있어 DOM은 이미 준비돼 있다 — 이 파일의 다른 핸들러도
// 최상위에서 바로 붙는다. 이벤트를 기다리면 한 겹 더 어긋날 여지만 생긴다
let updateScroller = setupControlLayout();

// ── 화면 보기 ───────────────────────────────────────────────
// 소스는 config에 정의된 것 + 지금 열린 창. 창을 고르면 그 제목으로 override를 보낸다 —
// 제목은 정규식으로 쓰이므로 특수문자를 막아 준다
let lookLoaded = false;

function rxEscape(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

async function loadLookSources() {
    const sel = $('look-source');
    const keep = sel.value;
    sel.innerHTML = '';
    let named = {}, def = null;
    try {
        const r = await window.companion.lookSources();
        named = r.sources || {};
        def = r.default;
    } catch {
        sel.innerHTML = '<option value="">(API 서버 꺼짐)</option>';
        return;
    }
    for (const name of Object.keys(named)) {
        const o = document.createElement('option');
        o.value = 'src:' + name;
        o.textContent = name + (name === def ? ' (기본)' : '');
        sel.appendChild(o);
    }
    try {
        const w = await window.companion.lookWindows();
        const wins = (w.windows || []).filter(x => !x.minimized);
        if (wins.length) {
            const g = document.createElement('optgroup');
            g.label = '지금 열린 창';
            for (const x of wins) {
                const o = document.createElement('option');
                o.value = 'win:' + x.title;
                o.textContent = `${x.title}  (${x.w}x${x.h})`;
                g.appendChild(o);
            }
            sel.appendChild(g);
        }
    } catch { /* 창 목록은 없어도 소스로 볼 수 있다 */ }
    // 고른 소스를 기억한다 — 다시 켤 때마다 전체 화면으로 돌아가면 "봐줘"가 매번 엉뚱한 것을 본다
    let saved = null;
    try { saved = localStorage.getItem('companion.lookSource'); } catch { /* 무시 */ }
    const pick = [keep, saved, def && 'src:' + def].find(v => v && [...sel.options].some(o => o.value === v));
    if (pick) sel.value = pick;
}

$('btn-look-refresh').onclick = loadLookSources;
$('look-source').onchange = () => {
    try { localStorage.setItem('companion.lookSource', $('look-source').value); } catch { /* 무시 */ }
};

$('btn-look').onclick = async () => {
    const v = $('look-source').value;
    if (!v) return;
    const mode = $('look-ocr-only').checked ? 'ocr' : 'both';
    const body = { mode };
    if (v.startsWith('src:')) body.source = v.slice(4);
    else body.override = { type: 'window', title: rxEscape(v.slice(4)), focus: true, max_width: 1280 };

    const seen = $('look-seen');
    $('btn-look').disabled = true;
    $('look-hint').textContent = mode === 'ocr' ? '읽는 중…' : '보는 중… (VLM은 10~20초 걸립니다)';
    try {
        const r = await window.companion.lookNow(body);
        if (r && r.error) throw new Error(r.error);
        const took = Object.entries(r.took || {}).map(([k, x]) => `${k} ${x}s`).join(' · ');
        const dropped = r.ocr_dropped ? ` (깨진 줄 ${r.ocr_dropped}개 버림)` : '';
        $('look-hint').textContent = `${r.source} · 글자 ${r.ocr_lines}줄${dropped} · ${took}`;
        seen.style.display = 'block';
        seen.textContent = r.seen || `글자 ${r.ocr_lines}줄을 읽었습니다 (설명 없음 — 글자만 모드)`;
        // 찍힌 그림을 보여 준다 — 보여주려던 화면과 찍힌 화면이 다른 일이 있었다 (10-01).
        // 캐시를 피하려고 시각을 붙인다
        showShot();
    } catch (e) {
        $('look-hint').textContent = '못 봤습니다: ' + (e.message || e);
        seen.style.display = 'none';
    } finally {
        $('btn-look').disabled = false;
    }
};

// 마지막으로 찍힌 화면을 띄운다. 찍은 주체가 앱이든 디스코드 !봐줘든 같은 파일을 본다
// (없으면 404 → onerror로 숨긴다). 쿼리에 시각을 붙여 캐시를 피한다
function showShot() {
    const shot = $('look-shot');
    // 'none' / 'block'을 직접 넣는다. style.display = '' 는 인라인 값을 지우는 것이라
    // CSS의 규칙이 되살아난다 — 그래서 미리보기가 한 번도 뜨지 않았다 (10-01)
    shot.onerror = () => { shot.style.display = 'none'; };
    shot.onload = () => { shot.style.display = 'block'; };
    shot.src = `http://127.0.0.1:8000/look/last.png?t=${Date.now()}`;
}

$('look-shot').onclick = () => $('look-shot').classList.toggle('big');

// 지금 들고 있는 것 — 화면이든 링크로 받은 글이든 같은 칸이라 한 줄로 보여 준다
async function loadReading() {
    const el = $('reading-what');
    try {
        const r = await window.companion.lookReading();
        const d = r && r.reading;
        if (!d) { el.textContent = '아직 없습니다.'; return; }
        const when = (d.at || '').replace('T', ' ').slice(0, 16);
        el.textContent = `${d.title || d.url} · ${d.chars}자 · ${when}`;
        el.title = d.url || '';
    } catch {
        el.textContent = 'API 서버가 실행 중이어야 확인됩니다.';
    }
}

async function readUrlNow() {
    const url = $('read-url').value.trim();
    const el = $('reading-what');
    if (!/^https?:\/\/\S+$/i.test(url)) {
        el.textContent = 'http(s)로 시작하는 주소를 넣으세요.';
        return;
    }
    $('btn-read-url').disabled = true;
    el.textContent = '읽는 중…';
    try {
        const r = await window.companion.lookReadUrl(url);
        // Python이 400/502를 detail로 돌려준다 (내부 주소·본문 없음 등)
        if (r && r.detail) throw new Error(r.detail);
        if (r && r.error) throw new Error(r.error);
        $('read-url').value = '';
        $('look-shot').style.display = 'none';   // 화면이 아니라 글을 보고 있다
        await loadReading();
    } catch (e) {
        el.textContent = '못 읽었습니다: ' + (e.message || e);
    } finally {
        $('btn-read-url').disabled = false;
    }
}

$('btn-read-url').onclick = readUrlNow;
$('read-url').onkeydown = (e) => { if (e.key === 'Enter') readUrlNow(); };
$('btn-reading-refresh').onclick = () => { loadReading(); showShot(); };
$('btn-reading-clear').onclick = async () => {
    await window.companion.lookClear().catch(() => {});
    $('look-shot').style.display = 'none';
    $('look-seen').style.display = 'none';
    loadReading();
};

$('btn-look-clear').onclick = async () => {
    await window.companion.lookClear().catch(() => {});
    $('look-seen').style.display = 'none';
    $('look-shot').style.display = 'none';
    $('look-hint').textContent = '보고 있던 것을 치웠습니다.';
};

// ── 전사 모드 ───────────────────────────────────────────────
// 캐릭터의 듣기와 배타적이다 — 같은 사용자 스트림을 둘이 구독하면 서로를 끊는다.
// 그래서 시작하면 듣기가 멈추고, 버튼도 둘 중 하나만 켜진 것처럼 보이게 한다.
function updateScriptButtons() {
    const g = selectedGuild();
    const on = !!(g && g.transcribing);
    $('btn-script-start').disabled = on;
    $('btn-script-stop').disabled = !on;
    $('detail-script').textContent = on ? '기록 중' : '';
}

async function loadScriptPresets() {
    const sel = $('script-preset');
    try {
        // {default, active, presets: [{name, label, model, vram_mb, is_default}, ...]}
        const r = await window.companion.transcribePresets();
        const items = r.presets || [];
        if (!items.length) throw new Error('empty');
        sel.innerHTML = '';
        for (const p of items) {
            const o = document.createElement('option');
            o.value = p.name;
            // label이 이미 "정확도 우선 (large-v3, beam 5)"처럼 모델을 담고 있어 경로는 뺀다
            const vram = p.vram_mb ? ` · ${(p.vram_mb / 1024).toFixed(1)}GB` : '';
            o.textContent = (p.label || p.name) + vram + (p.is_default ? ' (기본)' : '');
            o.title = p.model || '';
            sel.appendChild(o);
        }
        const want = r.active || r.default;
        if (want && items.some(p => p.name === want)) sel.value = want;
    } catch {
        // API 서버가 안 떠 있으면 고를 수 없다 — 봇이 기본 프리셋을 쓴다
        sel.innerHTML = '<option value="">(API 서버 꺼짐 — 기본값)</option>';
    }
}

$('btn-script-start').onclick = async () => {
    const g = selectedGuild();
    const voiceChannelId = $('channel').value;
    if (!g || !voiceChannelId) return;
    $('detail-script').textContent = '모델 불러오는 중…';
    const r = await window.companion.transcribeControl({
        action: 'start',
        guildId: g.id,
        voiceChannelId,
        textChannelId: $('script-text-channel').value || null,
        preset: $('script-preset').value || null,
    });
    if (r && r.error) {
        $('detail-script').textContent = '';
        appendLog('bot', `\n[기록 시작 실패] ${r.error}\n`);
    }
    setTimeout(refreshGuilds, 800);
};

$('btn-script-stop').onclick = async () => {
    const g = selectedGuild();
    if (!g) return;
    const r = await window.companion.transcribeControl({ action: 'stop', guildId: g.id });
    if (r && r.error) appendLog('bot', `\n[기록 중지 실패] ${r.error}\n`);
    setTimeout(refreshGuilds, 800);
};

$('guild').onchange = () => { fillChannels(); updateModeButtons(); };
$('btn-refresh').onclick = refreshGuilds;

$('btn-join').onclick = async () => {
    const channelId = $('channel').value;
    if (!channelId) return;
    const r = await window.companion.join(channelId);
    if (r.error) appendLog('bot', `\n[참여 실패] ${r.error}\n`);
    setTimeout(refreshGuilds, 800);
};

$('btn-leave').onclick = async () => {
    const g = selectedGuild();
    if (!g) return;
    await window.companion.leave(g.id);
    setTimeout(refreshGuilds, 500);
};

async function setMode(mode) {
    const g = selectedGuild();
    if (!g) return;
    const r = await window.companion.setMode(g.id, mode);
    if (r.error) appendLog('bot', `\n[모드 변경 실패] ${r.error}\n`);
    setTimeout(refreshGuilds, 500);
}

$('btn-mode-auto').onclick = () => setMode('auto');
$('btn-mode-always').onclick = () => setMode('always');
$('btn-mode-call').onclick = () => setMode('call');
$('btn-mode-stop').onclick = () => setMode('stop');

// ── LLM 모델 ────────────────────────────────────────────────
let llmLoaded = false;
let llmBusy = false;

const PROVIDER_LABEL = { openai_chat: 'OpenAI', ollama: 'Ollama', llamacpp: 'llama.cpp', claude: 'Claude' };

function gb(bytes) {
    return (bytes / 1024 ** 3).toFixed(1) + 'GB';
}

function describeLlm(cur) {
    return `${PROVIDER_LABEL[cur.provider] || cur.provider} · ${cur.model}`;
}

async function loadLlmModels() {
    if (llmBusy) return;
    llmLoaded = true; // 실패 시 아래에서 되돌림 — 폴링마다 중복 요청 방지
    let data;
    try {
        data = await window.companion.llmModels();
    } catch {
        llmLoaded = false;
        $('llm-hint').textContent = 'API 서버가 실행되면 모델 목록이 나타납니다.';
        return;
    }
    if (!data.current) {
        // 모델 교체 기능 추가 전에 띄운 서버 (404) — 재시작하면 생긴다
        $('llm-hint').textContent = 'API 서버가 모델 교체를 지원하지 않습니다. 서버를 재시작하세요.';
        return;
    }

    const sel = $('llm-model');
    sel.innerHTML = '';
    const addGroup = (label, provider, models, describe) => {
        if (!models.length) return;
        const group = document.createElement('optgroup');
        group.label = label;
        for (const m of models) {
            const o = document.createElement('option');
            o.value = JSON.stringify({ provider, model: m.model });
            o.textContent = describe(m);
            group.appendChild(o);
        }
        sel.appendChild(group);
    };

    addGroup('클라우드 (OpenAI)', 'openai_chat', data.openai_chat.models, m => m.model);
    if (data.claude) {
        addGroup('클라우드 (Claude)', 'claude', data.claude.available ? data.claude.models : [],
                 m => m.model);
    }
    addGroup('로컬 (Ollama)', 'ollama', data.ollama.models, m => {
        if (m.offline) return `${m.model}  (Ollama 꺼짐 — 선택하면 자동 실행)`;
        const parts = [m.parameter_size, gb(m.size)].filter(Boolean).join(', ');
        // 추론 전용 모델(qwen3:4b, deepseek-r1 등)은 think=false를 무시해 사고 과정이 답변에
        // 섞이거나 토큰 한도를 사고로 다 써서 빈 응답이 나온다 — 음성 대화용으로 부적합
        return `${m.model}  (${parts})` + (m.thinking ? ' ⚠ 추론 모델' : '') + (m.loaded ? ' ● 로드됨' : '');
    });
    // Ollama로는 못 돌리는 MoE(전문가 층을 RAM에 둔다) — 첫 로딩은 17GB를 읽느라 수십 초
    if (data.llamacpp && data.llamacpp.available) {
        addGroup('로컬 (llama.cpp)', 'llamacpp', data.llamacpp.models.filter(m => !m.missing),
                 m => `${m.model}  (전문가 층 CPU)`);
    }

    const cur = data.current;
    sel.value = JSON.stringify({ provider: cur.provider, model: cur.model });
    $('detail-llm').textContent = `현재: ${describeLlm(cur)}`;

    const hints = [];
    if (data.claude && !data.claude.available) hints.push(data.claude.error);
    if (!data.ollama.available) hints.push('Ollama가 꺼져 있습니다 — 로컬 모델을 선택해 적용하면 자동으로 실행합니다.');
    else if (!data.ollama.models.length) hints.push('설치된 Ollama 모델이 없습니다. (ollama pull <모델>)');
    else hints.push('로컬 모델은 STT(Whisper)와 같은 GPU를 씁니다 — 모델 크기만큼 VRAM이 추가로 필요합니다.');
    $('llm-hint').textContent = hints.join(' ');
}

$('btn-llm-refresh').onclick = loadLlmModels;

$('btn-llm-apply').onclick = async () => {
    const value = $('llm-model').value;
    if (!value || llmBusy) return;
    const { provider, model } = JSON.parse(value);

    llmBusy = true;
    $('btn-llm-apply').disabled = true;
    $('detail-llm').textContent = (provider === 'ollama' || provider === 'llamacpp')
        ? `${model} 로딩 중 (Ollama 실행·VRAM 로딩, 수 초~수십 초)...`
        : `${model}(으)로 교체 중...`;
    let error = null;
    try {
        const r = await window.companion.llmSelect(provider, model);
        if (r.detail) error = r.detail; // FastAPI HTTPException
        else appendLog('server', `\n[LLM 교체] ${describeLlm(r.current)}\n`);
    } catch (e) {
        error = String(e);
    } finally {
        llmBusy = false;
        $('btn-llm-apply').disabled = false;
    }
    if (error) {
        $('detail-llm').textContent = '교체 실패 — 기존 모델 유지';
        $('llm-hint').textContent = error;
    } else {
        loadLlmModels();
    }
};

// ── TTS 엔진 ────────────────────────────────────────────────
let ttsLoaded = false;
let ttsBusy = false;

async function loadTtsEngines() {
    if (ttsBusy) return;
    ttsLoaded = true;
    let data;
    try {
        data = await window.companion.ttsEngines();
    } catch {
        ttsLoaded = false;
        $('tts-hint').textContent = 'API 서버가 실행되면 엔진 목록이 나타납니다.';
        return;
    }
    if (!data.current) {
        // TTS 교체 기능 추가 전에 띄운 서버 (404)
        $('tts-hint').textContent = 'API 서버가 TTS 교체를 지원하지 않습니다. 서버를 재시작하세요.';
        return;
    }
    const sel = $('tts-engine');
    sel.innerHTML = '';
    for (const e of data.engines) {
        const o = document.createElement('option');
        o.value = e.provider;
        o.textContent = e.label;
        sel.appendChild(o);
    }
    sel.value = data.current.provider;
    $('detail-tts').textContent = `현재: ${data.current.label}`;
    $('tts-hint').textContent = data.current.provider === 'gpt_sovits'
        ? 'GPT-SoVITS는 별도 서버로 GPU를 약 1.6GB 씁니다 — 게임 중 VRAM이 부족하면 Edge로 바꾸면 반환됩니다.'
        : 'Edge TTS는 GPU를 쓰지 않는 클라우드 음성입니다.';
}

$('btn-tts-apply').onclick = async () => {
    const provider = $('tts-engine').value;
    if (!provider || ttsBusy) return;
    ttsBusy = true;
    $('btn-tts-apply').disabled = true;
    $('detail-tts').textContent = provider === 'gpt_sovits'
        ? 'GPT-SoVITS 서버 실행·모델 로딩 중 (10~20초)...'
        : '교체 중...';
    let error = null;
    try {
        const r = await window.companion.ttsSelect(provider);
        if (r.detail) error = r.detail;
        else appendLog('server', `
[TTS 교체] ${r.current.label}
`);
    } catch (e) {
        error = String(e);
    } finally {
        ttsBusy = false;
        $('btn-tts-apply').disabled = false;
    }
    if (error) {
        $('detail-tts').textContent = '교체 실패 — 기존 엔진 유지';
        $('tts-hint').textContent = error;
    } else {
        loadTtsEngines();
    }
};

// ── 탭 전환 ─────────────────────────────────────────────────
let stateTimer = null;

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.onclick = () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
        document.querySelectorAll('.tab').forEach(t =>
            t.classList.toggle('active', t.id === 'tab-' + btn.dataset.tab));

        // 캐릭터 상태 탭: 열릴 때 로드 + 10초 자동 갱신
        if (btn.dataset.tab === 'char') {
            loadState();
            stateTimer = setInterval(loadState, 10000);
        } else if (stateTimer) {
            clearInterval(stateTimer);
            stateTimer = null;
        }

        if (btn.dataset.tab === 'look') { loadReading(); showShot(); }
        updateScroller();   // 제어 탭이 아니면 위·아래 버튼을 숨긴다
        if (btn.dataset.tab === 'map' && !mapData) loadMap();
        if (btn.dataset.tab === 'live') startLive(); else stopLive();
    };
});

// ── 캐릭터 상태 탭 ──────────────────────────────────────────
function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
}

function renderChips(state, mem) {
    const chips = $('char-chips');
    chips.innerHTML = '';
    const add = (label, value) => {
        if (value === undefined || value === null || value === '') return;
        const c = el('span', 'chip');
        c.append(label + ' ');
        c.appendChild(el('b', '', String(value)));
        chips.appendChild(c);
    };
    add('LLM', ((state.llm.provider === 'ollama' || state.llm.provider === 'llamacpp') ? '로컬 ' : '') + state.llm.model);
    add('temp', state.llm.temperature);
    add('STT', (state.stt.model || '').split('/').pop());
    add('TTS', state.tts.label || state.tts.provider);
    add('장기기억', mem.long_term_count + '개');
}

function renderInterests(keywords) {
    const box = $('char-interests');
    box.innerHTML = '';
    if (!keywords.length) {
        box.appendChild(el('div', 'rag-none', '아직 데이터가 없습니다.'));
        return;
    }
    const max = keywords[0].count;
    for (const k of keywords) {
        const row = el('div', 'kw-row');
        row.appendChild(el('span', 'kw-word', k.word));
        const wrap = el('div', 'kw-bar-wrap');
        const bar = el('div', 'kw-bar');
        bar.style.width = Math.max(4, Math.round(k.count / max * 100)) + '%';
        wrap.appendChild(bar);
        row.appendChild(wrap);
        row.appendChild(el('span', 'kw-count', String(k.count)));
        box.appendChild(row);
    }
}

function renderRag(retrievals) {
    const box = $('char-rag');
    box.innerHTML = '';
    if (!retrievals.length) {
        box.appendChild(el('div', 'rag-none',
            '아직 참조 기록이 없습니다. 대화를 하면 매 턴 어떤 기억을 검색했는지 여기 표시됩니다.'));
        return;
    }
    for (const r of retrievals) {
        const item = el('div', 'mem-item');
        const head = el('div');
        head.appendChild(el('span', 'mem-ts', `[${r.ts}] `));
        head.appendChild(el('span', 'rag-query', r.query));
        item.appendChild(head);
        if (r.hits.length) {
            for (const h of r.hits) {
                const hit = el('div', 'rag-hit', '↳ ' + h.summary + ' ');
                hit.appendChild(el('span', 'rag-dist', `(거리 ${h.distance})`));
                item.appendChild(hit);
            }
        } else {
            item.appendChild(el('div', 'rag-none', '↳ 관련 기억 없음'));
        }
        box.appendChild(item);
    }
}

function renderLongTerm(longTerm) {
    const box = $('char-ltm');
    box.innerHTML = '';
    $('char-ltm-count').textContent = longTerm.length ? `${longTerm.length}개 · 최신순` : '';
    if (!longTerm.length) {
        box.appendChild(el('div', 'rag-none', '저장된 장기 기억이 없습니다.'));
        return;
    }
    for (const m of longTerm) {
        const item = el('div', 'mem-item');
        item.appendChild(el('div', 'mem-ts', m.timestamp));
        const body = el('div', 'mem-text', m.summary || m.text);
        body.title = m.text; // 원문은 툴팁으로
        item.appendChild(body);
        box.appendChild(item);
    }
}

function renderTurns(data) {
    const box = $('char-turns');
    const rows = (data && data.rows) || [];
    box.innerHTML = '';
    $('char-turns-count').textContent = data
        ? `답함 ${data.replied} · 듣기만 ${data.listened} · 먼저 건 말 ${data.initiatives}` : '';
    if (!rows.length) {
        box.appendChild(el('div', 'rag-none', '아직 판단 기록이 없습니다. 대화가 오가면 쌓입니다.'));
        return;
    }
    for (const r of rows) {
        const line = el('div', 'mem-item');
        const t = (r.ts || '').slice(11, 16);
        if (r.event === 'initiative') {
            line.appendChild(el('span', 'turn-init', '먼저 말함 '));
            line.append(`${t} · ${r.idle_s}초 조용함 (기준 ${r.wait_s}초, 관심 ${r.interest})`);
        } else if (r.replied) {
            line.appendChild(el('span', 'turn-yes', '답함 '));
            line.append(`${t} · ${r.why} · ${r.chars}자`);
        } else {
            line.appendChild(el('span', 'turn-no', '듣기만 '));
            line.append(`${t} · ${r.why} · ${r.text || r.chars + '자'}`);
        }
        box.appendChild(line);
    }
}

function renderShortTerm(shortTerm) {
    const box = $('char-stm');
    box.innerHTML = '';
    if (!shortTerm.length) {
        box.appendChild(el('div', 'rag-none', '최근 대화 기록이 없습니다.'));
        return;
    }
    for (const m of shortTerm) {
        const who = m.role === 'user' ? '사용자' : CHAR_NAME;
        const line = el('div', 'mem-item');
        line.appendChild(el('span', m.role === 'user' ? 'stm-user' : 'stm-assistant', who + ': '));
        line.append(m.content || '');
        box.appendChild(line);
    }
    box.scrollTop = box.scrollHeight; // 최신 대화가 아래
}

async function loadState() {
    try {
        const [state, mem, turns] = await Promise.all([
            window.companion.inspectState(),
            window.companion.inspectMemory(),
            window.companion.inspectTurns().catch(() => null),
        ]);
        $('char-prompt').textContent = state.prompt || '(prompt.txt 비어 있음)';
        $('char-topic').textContent = state.topic || '(topic.txt 비어 있음)';
        renderChips(state, mem);
        renderInterests(mem.keywords || []);
        renderRag(mem.recent_retrievals || []);
        renderLongTerm(mem.long_term || []);
        renderTurns(turns);
        renderShortTerm(mem.short_term || []);
        $('char-updated').textContent = '갱신: ' + new Date().toLocaleTimeString();
        $('char-hint').textContent = '';
    } catch {
        $('char-hint').textContent = 'API 서버가 실행 중이어야 상태를 볼 수 있습니다. (제어 탭에서 서버를 시작하세요)';
    }
}

$('btn-char-refresh').onclick = loadState;

