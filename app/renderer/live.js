// ── 대화 탭 ──────────────────────────────────────────────────────────
// 서버 stdout 대신 턴 기록(metrics.jsonl)을 2초마다 받아, 채팅처럼 그린다.
// 사람 말은 왼쪽, 캐릭터 말은 오른쪽. 레이턴시는 캐릭터 말풍선 아래에만 작게.
let liveRows = [];
let liveLast = '';
let liveTimer = null;
let liveStick = true;      // 아래에 붙어 있으면 새 말이 올 때 따라 내려간다

const LIVE_MAX = 300;
const liveEsc = (t) => (t || '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const liveTime = (ts) => (ts || '').slice(11, 16);

// 같은 사람이 이어 말하면 이름을 다시 쓰지 않는다 (채팅 앱처럼)
function liveWho(r, prev) {
    const who = r.event === 'initiative' ? CHAR_NAME : (r.speaker || '');
    const prevWho = prev ? (prev.event === 'initiative' ? CHAR_NAME : (prev.speaker || '')) : null;
    const prevSide = prev ? (prev.event === 'initiative' ? 'char' : 'human') : null;
    const side = r.event === 'initiative' ? 'char' : 'human';
    return (who && (who !== prevWho || side !== prevSide)) ? who : '';
}

// 사람이 느끼는 시간 = 말이 끝난 순간부터 첫 소리까지.
// felt_s는 서버가 말끝(endpoint_s) + 실제 첫 소리(ttfa)로 계산해 준다.
// 예전엔 llm_first + tts_first만 더해 말끝·STT가 빠졌고, tts_first는 대개 비어 있어 실제보다 훨씬 짧게 보였다
function liveLatency(r) {
    const felt = r.felt_s || 0;
    if (!felt) return '';
    const cls = felt >= 4 ? 'lat-bad' : felt >= 2.5 ? 'lat-mid' : 'lat-ok';
    const bits = [];
    if (r.endpoint_s != null) bits.push(`말끝 ${r.endpoint_s.toFixed(2)}`);
    if (r.stt_s != null) bits.push(`STT ${r.stt_s.toFixed(2)}`);
    if (r.llm_first_s != null) bits.push(`생각 ${r.llm_first_s.toFixed(2)}`);
    if (r.ttfa_s != null) bits.push(`첫 소리까지 ${r.ttfa_s.toFixed(2)}`);
    if (r.first_chars) bits.push(`첫 조각 ${r.first_chars}자`);
    if (r.model) bits.push(shortModel(r.model));
    return `<span class="lat ${cls}" title="${liveEsc(bits.join(' · '))}">${felt.toFixed(2)}초</span>`;
}

// "hf.co/Mungert/A.X-4.0-Light-GGUF:Q4_K_M" → "A.X-4.0-Light-GGUF:Q4_K_M"
function shortModel(m) {
    return (m || '').split('/').pop();
}

// 프로필 사진 — 못 불러오면(오프라인·주소 만료) 이름 첫 글자 동그라미로 떨어진다
function liveAvatar(url, name, show) {
    if (!show) return '<span class="msg-av-gap"></span>';
    const initial = liveEsc((name || '?').trim().slice(0, 1));
    if (!url) return `<span class="msg-av msg-av-x">${initial}</span>`;
    // onerror를 속성에 쓰면 따옴표 이스케이프가 깨지기 쉽다 — 그린 뒤에 이벤트로 붙인다
    return `<img class="msg-av" src="${liveEsc(url)}" alt="" data-initial="${initial}">`;
}

function bubble(side, who, time, html, extra = '', avatar = null, name = '') {
    return `<div class="msg ${side}">`
        + (who ? `<div class="msg-who">${liveEsc(who)}</div>` : '')
        + `<div class="msg-row">${liveAvatar(avatar, name || who, !!who)}`
        + `<div class="msg-bubble">${html}</div>`
        + `<div class="msg-meta">${time}${extra}</div></div></div>`;
}

function liveBlock(r, prev) {
    const t = liveTime(r.ts);
    const who = liveWho(r, prev);

    if (r.event === 'stt_timeout' || r.event === 'stt_skipped' || r.event === 'stt_recovered') {
        const msg = r.event === 'stt_recovered'
            ? `음성 인식 복구 — 멈춰 있던 전사가 ${r.stuck_s}초 만에 끝남`
            : r.event === 'stt_timeout'
                ? `음성 인식 시간 초과 (${r.sec}초) — 이번 말은 넘김`
                : `음성 인식이 밀려 넘김 (${r.stuck_s || 0}초째, ${r.kind === 'partial' ? '중간' : '최종'})`;
        return `<div class="warn">${liveEsc(liveTime(r.ts))} · ${liveEsc(msg)}</div>`;
    }
    // 기억 정리에서 생긴 일 — 판정 모델이 실패하면 낱말 필터만 남는데, 그걸 콘솔만 알고 있었다
    if (r.event === 'memory_error') {
        return `<div class="warn">${liveEsc(liveTime(r.ts))} · ${liveEsc(`기억 판정 실패 (${r.model || '?'}) — 낱말 필터만 적용: ${r.error || ''}`)}</div>`;
    }
    if (r.event === 'memory_misheard') {
        return `<div class="warn">${liveEsc(liveTime(r.ts))} · ${liveEsc(`잘못 들었을 수 있는 ${r.dropped}줄은 기억에서 뺌: ${(r.texts || []).join(' / ')}`)}</div>`;
    }
    // 화면을 본 것 · 링크를 읽은 것. 대화 가운데 있어야 어느 턴이 그 때문에 느려졌는지 보인다
    if (r.event === 'look' || r.event === 'read') {
        const bits = r.event === 'look'
            ? [r.source, r.mode, `${r.chars}자`, r.ocr_lines ? `글자 ${r.ocr_lines}줄` : '',
               r.took ? Object.entries(r.took).map(([k, v]) => `${k} ${v}초`).join(' · ') : '']
            : [r.title || r.url, `${r.chars}자`];
        const note = (r.notes || []).join('; ');
        return `<div class="saw" title="${liveEsc(note)}">${liveEsc(liveTime(r.ts))} · `
            + `${r.event === 'look' ? '화면을 봄' : '링크를 읽음'} — `
            + `${liveEsc(bits.filter(Boolean).join(' · '))}</div>`;
    }
    if (r.event === 'spoke') {       // 캐릭터가 먼저 꺼낸 말 (인사 · 먼저 말 걸기)
        const why = r.kind === 'greet'
            ? `${r.kind_ko || '인사'}${r.who ? ' · ' + liveEsc(r.who) : ''}`
            : `먼저 말 걸기 · ${r.idle_s}초 조용함 (기준 ${r.wait_s}초, 관심 ${r.interest})`;
        return bubble('char first', CHAR_NAME, t,
            liveEsc(r.reply) + `<span class="first-tag">${why}</span>`,
            liveLatency(r), r.self_avatar, CHAR_NAME);
    }
    if (r.event === 'initiative') {   // 옛 기록 (말한 내용이 없던 시절)
        return bubble('char init', who, t,
            `<i>먼저 말을 꺼냄</i> · ${r.idle_s}초 조용함 (기준 ${r.wait_s}초, 관심 ${r.interest})`,
            '', r.self_avatar, CHAR_NAME);
    }
    if (r.event === 'gate') {      // 답하지 않은 발화
        return bubble('human skip', who, t,
            liveEsc(r.text || `(${r.chars}자)`) + `<span class="skip-tag">듣기만 · ${liveEsc(r.why)}</span>`,
            '', r.avatar, r.speaker);
    }
    let out = bubble('human', who, t, liveEsc(r.text), '', r.avatar, r.speaker);
    if (r.reply) out += bubble('char', CHAR_NAME, t, liveEsc(r.reply), liveLatency(r), r.self_avatar, CHAR_NAME);
    return out;
}

// 모델·엔진이 바뀐 지점에 줄을 긋는다 — 바꿔 가며 테스트할 때 어디서부터 다른 모델인지 보이게
function setupLine(cfg) {
    const bits = [shortModel(cfg.model)];
    if (cfg.tts) bits.push(`TTS ${cfg.tts}`);
    if (cfg.chunking) bits.push(`조각 ${cfg.chunking}`);
    return `<div class="setup"><span>${liveEsc(bits.filter(Boolean).join(' · '))}</span></div>`;
}

// 설정이 바뀌었나 — 그 줄에 있는 값만 비교한다.
// spoke(먼저 꺼낸 말)에는 tts·chunking이 없어서, 없는 값까지 비교하면 줄이 계속 그어졌다
function setupChanged(r, seen) {
    if (!r.model) return null;
    const now = { model: r.model, tts: r.tts, chunking: r.chunking };
    // 처음 알게 된 값은 '바뀐 것'이 아니다 — spoke에는 tts·chunking이 없어서,
    // 그것만으로 줄을 그으면 인사 다음 줄마다 또 그어졌다
    const diff = Object.keys(now).some(
        (k) => now[k] != null && seen[k] !== undefined && seen[k] !== now[k]);
    const fresh = seen.model === undefined;
    Object.keys(now).forEach((k) => { if (now[k] != null) seen[k] = now[k]; });
    return (diff || fresh) ? now : null;
}

function renderLive() {
    const box = $('live-list');
    const seen = {};
    box.innerHTML = liveRows.map((r, i) => {
        const cfg = setupChanged(r, seen);
        return (cfg ? setupLine(seen) : '') + liveBlock(r, liveRows[i - 1]);
    }).join('') || '<div class="rag-none">아직 대화가 없습니다.</div>';
    // 프로필 사진을 못 불러오면 이름 첫 글자로 바꾼다 (오프라인·주소 만료)
    box.querySelectorAll('img.msg-av').forEach((img) => {
        const fallback = () => {
            const span = document.createElement('span');
            span.className = 'msg-av msg-av-x';
            span.textContent = img.dataset.initial || '?';
            img.replaceWith(span);
        };
        img.onerror = fallback;
        if (img.complete && img.naturalWidth === 0) fallback();
    });
    if (liveStick) box.scrollTop = box.scrollHeight;

    const turns = liveRows.filter((r) => r.event === 'turn');
    const skips = liveRows.filter((r) => r.event === 'gate');
    const inits = liveRows.filter((r) => r.event === 'initiative' || r.event === 'spoke');
    const firsts = turns.map((r) => r.felt_s || 0).filter((v) => v > 0).sort((a, b) => a - b);
    const withModel = liveRows.filter((r) => r.model);
    const model = withModel.length ? shortModel(withModel[withModel.length - 1].model) : '';
    $('live-stat').textContent =
        `답함 ${turns.length} · 듣기만 ${skips.length} · 먼저 말함 ${inits.length}`
        + (firsts.length ? ` · 첫 소리 중앙값 ${firsts[Math.floor(firsts.length / 2)].toFixed(2)}초` : '')
        + (model ? ` · ${model}` : '');
}

async function pollLive() {
    try {
        const d = await window.companion.inspectConversation(liveLast);
        if (d && d.rows && d.rows.length) {
            liveRows = liveRows.concat(d.rows).slice(-LIVE_MAX);
            liveLast = d.last_ts || liveLast;
            renderLive();
        }
        $('live-hint').textContent = '';
    } catch (e) {
        $('live-hint').textContent = 'API 서버가 실행 중이어야 대화가 보입니다. (제어 탭에서 서버를 시작하세요)';
    }
}

function startLive() {
    if (!liveRows.length) pollLive();
    if (!liveTimer) liveTimer = setInterval(pollLive, 2000);
}

function stopLive() {
    if (liveTimer) {
        clearInterval(liveTimer);
        liveTimer = null;
    }
}

$('live-list').addEventListener('scroll', () => {
    const el = $('live-list');
    liveStick = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
});
$('btn-live-clear').onclick = () => {
    liveRows = [];
    renderLive();
};
