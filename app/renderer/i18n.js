// 화면 언어 (한국어 / English)
//
// 원문은 한국어로 두고, 그 문장을 열쇠로 영어를 찾는다. 열쇠가 곧 원문이라 코드를 읽을 때
// 무슨 말이 나가는지 바로 보이고, 번역이 빠진 문장은 한국어 그대로 나온다 (깨지지 않는다).
//   tr('읽었습니다 — {title} ({n}자)', { title, n })   — JS에서 만드는 문장
//   HTML의 고정 문구는 아래 translateDom()이 글자 마디·속성을 그대로 찾아 바꾼다
//   한 문단 안에 <code> 같은 태그가 섞인 안내문은 data-i18n-html="열쇠"로 통째로 바꾼다
//
// 언어를 바꾸면 화면을 다시 불러온다 — 서버·봇 프로세스는 메인 프로세스에 있어 그대로 돈다.
// 캐릭터가 말하는 언어가 아니라 이 앱의 화면 언어다.

const LANG_KEY = 'companion.lang';
let LANG = (() => {
    try {
        const saved = localStorage.getItem(LANG_KEY);
        if (saved === 'ko' || saved === 'en') return saved;
    } catch { /* 저장소를 못 써도 화면은 뜬다 */ }
    return (navigator.language || '').toLowerCase().startsWith('ko') ? 'ko' : 'en';
})();

const EN = {
    // ── 탭·머리 ──
    '제어': 'Control', '보기': 'Look', '전사': 'Transcribe', '대화': 'Conversation', '상태': 'State',
    '기억': 'Memory', '▶ 한번에 시작': '▶ Start all', '언어 / Language': 'Language / 언어',

    // ── 제어 탭 ──
    '프로세스': 'Processes', 'API 서버 (Python)': 'API server (Python)', '중지됨': 'Stopped',
    '시작': 'Start', '중지': 'Stop', 'Discord 봇 (Node)': 'Discord bot (Node)',
    '서버': 'Server', '보이스 채널': 'Voice channel', '목록 새로고침': 'Refresh list',
    '참여': 'Join', '나가기': 'Leave', '듣기 모드': 'Listening', '자동': 'Auto',
    '자기에게 한 말에만 답함': 'Answers only what is said to it', '항상 반응': 'Always',
    '호출어': 'Wake word', '듣기 중지': 'Stop listening',
    '봇이 실행되면 서버 목록이 나타납니다.': 'Servers appear once the bot is running.',
    'LLM 모델': 'LLM model', '모델': 'Model', '적용': 'Apply',
    'API 서버가 실행되면 모델 목록이 나타납니다.': 'Models appear once the API server is running.',
    'TTS 목소리': 'TTS voice', '엔진': 'Engine',
    'API 서버가 실행되면 엔진 목록이 나타납니다.': 'Engines appear once the API server is running.',
    'API 서버 로그': 'API server log', 'Discord 봇 로그': 'Discord bot log',
    '캐릭터': 'Character', '팩': 'Pack', '적용 (서버 다시 시작)': 'Apply (restarts server)',
    'API 서버가 실행되면 캐릭터 목록이 나타납니다.': 'Characters appear once the API server is running.',
    '캐릭터를 바꾸면 API 서버가 다시 시작됩니다. 대화 기록과 기억은 캐릭터마다 따로 쌓입니다.':
        'Switching characters restarts the API server. Conversation history and memory are kept per character.',
    '현재: {name} ({id})': 'Current: {name} ({id})',
    '{name}(으)로 바꾸는 중 — 서버를 다시 시작합니다…': 'Switching to {name} — restarting the server…',
    '저장했습니다. 서버를 다시 시작하면 {name}(으)로 바뀝니다.': 'Saved. {name} takes effect when you restart the server.',
    '이미 이 캐릭터입니다.': 'Already using this character.',
    '세계관': 'lore', '목소리': 'voice',
    '실행 중 (정상)': 'Running (healthy)', '기동 중 (모델 로딩)...': 'Starting (loading models)...',
    '로그인: {user}': 'Logged in: {user}', ' · 음성 참여 중 ({n})': ' · in voice ({n})',
    '기동 중...': 'Starting...', '[한번에 시작 실패] {err}': '[Start all failed] {err}',
    ' ✓ 참여 중': ' ✓ joined',
    '봇이 속한 서버가 없습니다. 초대 링크로 봇을 서버에 먼저 초대하세요.':
        'The bot is not in any server. Invite it with an invite link first.',
    '(올리지 않음 — 파일로만 저장)': "(don't post — save to file only)",
    '자동 (스스로 판단)': 'Auto (decides itself)', '현재: {x}': 'Current: {x}',
    '[참여 실패] {err}': '[Join failed] {err}', '[모드 변경 실패] {err}': '[Mode change failed] {err}',
    'API 서버가 모델 교체를 지원하지 않습니다. 서버를 재시작하세요.':
        'The API server does not support model switching. Restart it.',
    '클라우드 (OpenAI)': 'Cloud (OpenAI)', '클라우드 (Claude)': 'Cloud (Claude)',
    '로컬 (Ollama)': 'Local (Ollama)', '로컬 (llama.cpp)': 'Local (llama.cpp)',
    '{model}  (Ollama 꺼짐 — 선택하면 자동 실행)': '{model}  (Ollama off — starts when selected)',
    ' ⚠ 추론 모델': ' ⚠ reasoning model', ' ● 로드됨': ' ● loaded',
    '{model}  (전문가 층 CPU)': '{model}  (experts on CPU)',
    'Ollama가 꺼져 있습니다 — 로컬 모델을 선택해 적용하면 자동으로 실행합니다.':
        'Ollama is off — selecting a local model starts it automatically.',
    '설치된 Ollama 모델이 없습니다. (ollama pull <모델>)': 'No Ollama models installed. (ollama pull <model>)',
    '로컬 모델은 STT(Whisper)와 같은 GPU를 씁니다 — 모델 크기만큼 VRAM이 추가로 필요합니다.':
        'Local models share the GPU with STT (Whisper) — they need their size in extra VRAM.',
    '{model} 로딩 중 (Ollama 실행·VRAM 로딩, 수 초~수십 초)...':
        'Loading {model} (starting Ollama, loading into VRAM — seconds to tens of seconds)...',
    '{model}(으)로 교체 중...': 'Switching to {model}...',
    '[LLM 교체] {x}': '[LLM switched] {x}', '교체 실패 — 기존 모델 유지': 'Switch failed — keeping the current model',
    'API 서버가 TTS 교체를 지원하지 않습니다. 서버를 재시작하세요.':
        'The API server does not support TTS switching. Restart it.',
    'GPT-SoVITS는 별도 서버로 GPU를 약 1.6GB 씁니다 — 게임 중 VRAM이 부족하면 Edge로 바꾸면 반환됩니다.':
        'GPT-SoVITS runs as a separate server using ~1.6 GB of GPU — switch to Edge to free it if VRAM runs short.',
    'Edge TTS는 GPU를 쓰지 않는 클라우드 음성입니다.': 'Edge TTS is a cloud voice that uses no GPU.',
    'GPT-SoVITS 서버 실행·모델 로딩 중 (10~20초)...': 'Starting GPT-SoVITS and loading the model (10–20 s)...',
    '교체 중...': 'Switching...', '[TTS 교체] {x}': '[TTS switched] {x}',
    '교체 실패 — 기존 엔진 유지': 'Switch failed — keeping the current engine',
    ' (기본)': ' (default)', '아래로': 'Down', '위로': 'Up',

    // ── 보기 탭 ──
    '화면 보기': 'Look at the screen', '소스': 'Source', '창 목록 새로고침': 'Refresh windows',
    '글자만 (1초, VRAM 0)': 'Text only (1 s, 0 VRAM)', '치우기': 'Clear',
    '방금 찍은 화면': 'Last capture', '클릭하면 크게 봅니다': 'Click to enlarge',
    '지금 보고 있는 것': 'What it is looking at', '링크': 'Link',
    'https://… 기사나 글 주소를 붙이세요': 'https://… paste an article or page address',
    '읽기': 'Read', '아직 없습니다.': 'Nothing yet.', '새로고침': 'Refresh',
    'look-hint': 'Define sources in <code>screen.sources</code> in the config, or pick an open window from the list. A covered window captures whatever is in front of it.',
    'reading-hint': 'A link pasted in a Discord text channel, or a screen looked at above, lands here. It disappears after <code>llm.reading_ttl_min</code> (30 min by default).',
    '(API 서버 꺼짐)': '(API server off)', '지금 열린 창': 'Open windows',
    '읽는 중…': 'Reading…', '보는 중… (VLM은 10~20초 걸립니다)': 'Looking… (the VLM takes 10–20 s)',
    ' (깨진 줄 {n}개 버림)': ' ({n} broken lines dropped)',
    '{source} · 글자 {n}줄{dropped} · {took}': '{source} · {n} text lines{dropped} · {took}',
    '글자 {n}줄을 읽었습니다 (설명 없음 — 글자만 모드)': 'Read {n} lines of text (no description — text-only mode)',
    '못 봤습니다: ': 'Could not look: ', '{what} · {n}자 · {when}': '{what} · {n} chars · {when}',
    'API 서버가 실행 중이어야 확인됩니다.': 'The API server must be running to check.',
    'http(s)로 시작하는 주소를 넣으세요.': 'Enter an address starting with http(s).',
    '못 읽었습니다: ': 'Could not read: ', '보고 있던 것을 치웠습니다.': 'Cleared what it was looking at.',
    '링크를 읽는 중…': 'Reading the link…', '읽었습니다 — {title} ({n}자)': 'Read — {title} ({n} chars)',
    '서버를 먼저 고르세요 (제어 탭).': 'Pick a server first (Control tab).',
    '보내지 못했습니다: ': 'Could not send: ',

    // ── 전사 탭 ──
    '전사 모드': 'Transcription mode', '기록할 텍스트 채널': 'Text channel to post to',
    '기록 시작': 'Start recording', '기록 중지': 'Stop recording',
    'script-hint': "Transcribes everyone in the voice channel above. Lines are posted live to the chosen text channel, and the full log is attached as .txt when you stop. Logs are also kept in <code>data/transcripts/</code> regardless of the channel. It can't run together with the character's listening; starting it stops listening.",
    '기록 중': 'Recording', '(API 서버 꺼짐 — 기본값)': '(API server off — default)',
    '모델 불러오는 중…': 'Loading model…', '[기록 시작 실패] {err}': '[Recording failed to start] {err}',
    '[기록 중지 실패] {err}': '[Recording failed to stop] {err}',

    // ── 대화 탭 ──
    '지우기': 'Clear', '말을 걸거나 링크를 붙이세요 (Enter)': 'Say something or paste a link (Enter)',
    '보내기': 'Send', 'API 서버가 실행되면 대화가 실시간으로 나타납니다.': 'The conversation appears here live once the API server is running.',
    '말끝 {s}': 'endpoint {s}', '생각 {s}': 'thinking {s}', '첫 소리까지 {s}': 'to first sound {s}',
    '첫 조각 {n}자': 'first chunk {n} chars', '{s}초': '{s}s',
    '음성 인식 복구 — 멈춰 있던 전사가 {s}초 만에 끝남': 'STT recovered — the stuck transcription finished after {s}s',
    '음성 인식 시간 초과 ({s}초) — 이번 말은 넘김': 'STT timed out ({s}s) — this utterance was skipped',
    '음성 인식이 밀려 넘김 ({s}초째, {kind})': 'STT backed up, skipped ({s}s, {kind})', '중간': 'partial', '최종': 'final',
    '기억 판정 실패 ({model}) — 낱말 필터만 적용: {err}': 'Memory screening failed ({model}) — keyword filter only: {err}',
    '잘못 들었을 수 있는 {n}줄은 기억에서 뺌: {texts}': 'Left {n} possibly misheard lines out of memory: {texts}',
    '{n}자': '{n} chars', '글자 {n}줄': '{n} text lines', '{k} {v}초': '{k} {v}s',
    '화면을 봄': 'Looked at the screen', '링크를 읽음': 'Read a link', '인사': 'greeting',
    '먼저 말 걸기 · {idle}초 조용함 (기준 {wait}초, 관심 {interest})': 'Spoke first · quiet {idle}s (threshold {wait}s, interest {interest})',
    '먼저 말을 꺼냄': 'Started a topic', '{idle}초 조용함 (기준 {wait}초, 관심 {interest})': 'quiet {idle}s (threshold {wait}s, interest {interest})',
    '({n}자)': '({n} chars)', '듣기만 · {why}': 'listened only · {why}', '조각 {x}': 'chunking {x}',
    '아직 대화가 없습니다.': 'No conversation yet.',
    '답함 {a} · 듣기만 {b} · 먼저 말함 {c}': 'answered {a} · listened {b} · spoke first {c}',
    ' · 첫 소리 중앙값 {s}초': ' · median to first sound {s}s',
    'API 서버가 실행 중이어야 대화가 보입니다. (제어 탭에서 서버를 시작하세요)': 'The API server must be running to show the conversation. (Start it in the Control tab.)',

    // ── 상태 탭 ──
    '⟳ 새로고침': '⟳ Refresh', '성격 프롬프트': 'Personality prompt', '말투 예시': 'Tone examples',
    '관심 키워드': 'Interest keywords', '장기기억 + 최근 대화 빈도': 'long-term memory + recent frequency',
    '방금 참조한 기억': 'Recently retrieved memory', 'RAG 검색 로그 · 최신순': 'RAG log · newest first',
    '장기 기억': 'Long-term memory', '말할 차례 판단': 'Turn-taking log', '최근 대화': 'Recent conversation',
    '단기 기억': 'short-term memory', '로컬 ': 'local ', '장기기억': 'Long-term', '{n}개': '{n}',
    '아직 데이터가 없습니다.': 'No data yet.',
    '아직 참조 기록이 없습니다. 대화를 하면 매 턴 어떤 기억을 검색했는지 여기 표시됩니다.':
        'No retrievals yet. Each turn, the memory searched for it shows up here.',
    '(거리 {d})': '(distance {d})', '↳ 관련 기억 없음': '↳ nothing relevant',
    '{n}개 · 최신순': '{n} · newest first', '저장된 장기 기억이 없습니다.': 'No long-term memory saved.',
    '답함 {a} · 듣기만 {b} · 먼저 건 말 {c}': 'answered {a} · listened {b} · spoke first {c}',
    '아직 판단 기록이 없습니다. 대화가 오가면 쌓입니다.': 'No decisions yet. They build up as people talk.',
    '먼저 말함 ': 'spoke first ', '답함 ': 'answered ', '듣기만 ': 'listened ',
    '{t} · {idle}초 조용함 (기준 {wait}초, 관심 {interest})': '{t} · quiet {idle}s (threshold {wait}s, interest {interest})',
    '최근 대화 기록이 없습니다.': 'No recent conversation.', '사용자': 'User',
    '(prompt.txt 비어 있음)': '(prompt.txt is empty)', '(topic.txt 비어 있음)': '(topic.txt is empty)',
    '갱신: ': 'Updated: ',
    'API 서버가 실행 중이어야 상태를 볼 수 있습니다. (제어 탭에서 서버를 시작하세요)': 'The API server must be running to show the state. (Start it in the Control tab.)',

    // ── 기억 탭 ──
    '벡터 지도': 'Vector map', '마인드맵': 'Mind map', '덩어리 보기': 'Clusters', '거리 그대로': 'True distances',
    '연결선': 'Links', '기억에서 찾기': 'Search memory',
    'API 서버가 실행되면 기억이 그려집니다.': 'Memory is drawn once the API server is running.',
    '사람에 대해 아는 것': 'About people', '있었던 일': 'Episodes', '자기 자신': 'Itself', '주제': 'Topics',
    '분류 없음': 'Unclassified',
    '기억을 불러와 덩어리를 계산하는 중...': 'Loading memory and computing clusters...',
    '기억을 불러오는 중...': 'Loading memory...',
    '아직 저장된 기억이 없습니다. 대화가 한 덩어리 끝나면 쌓입니다.': 'No memory saved yet. It builds up after each conversation episode.',
    '{n}개 · 연결 {e} · {dims}차원 → 2차원 ({layout}{explained})': '{n} · {e} links · {dims}D → 2D ({layout}{explained})',
    ', {p}% 담김': ', {p}% captured',
    'API 서버가 꺼져 있거나 예전 버전입니다 — 서버를 다시 시작해 주세요. ({err})': 'The API server is off or outdated — please restart it. ({err})',
    '점을 누르면 그 기억과 가까운 것들이 보입니다. 이어진 짝은 서로 거리가 {gate} 이하 — 검색이 “관련 있다”고 보는 것과 같은 눈금입니다. <b>덩어리 보기</b>는 뭉치는 모양만 맞고, 덩어리 사이 거리는 뜻이 없습니다.':
        'Click a point to see the memories near it. Linked pairs are within distance {gate} — the same scale retrieval uses for "relevant". <b>Clusters</b> gets the grouping right, but distances between clusters mean nothing.',
    '기록 ': 'Saved ', '예정일 ': 'Due ', '가까운 기억': 'Nearby memories',
    '가까운 기억이 없습니다 — 혼자 떨어져 있어, 관련 검색으로는 잘 걸리지 않습니다.': 'No nearby memories — it sits alone and is rarely retrieved by relevance.',
    '가까울수록 비슷한 이야기 · {dims}차원을 2차원으로 누른 그림 ({layout})': 'Closer means more similar · {dims}D flattened to 2D ({layout})',
    '이름 없음': 'unnamed', '나 자신': 'Itself', '나에게 있었던 일': 'What happened to it', '그 밖': 'Other',
    '  (예정 {d})': '  (due {d})', '+ {n}개 더 보기': '+ {n} more',
};

function tr(ko, vars) {
    let s = LANG === 'en' && Object.prototype.hasOwnProperty.call(EN, ko) ? EN[ko] : ko;
    if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
    return s;
}

// HTML의 고정 문구 바꾸기 — 대화·기억·로그처럼 내용을 보여 주는 칸은 건드리지 않는다
const I18N_SKIP = new Set(['PRE', 'TEXTAREA', 'SCRIPT', 'STYLE', 'CODE']);
function translateDom(root) {
    if (LANG === 'ko') return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode: (n) => {
            for (let p = n.parentElement; p; p = p.parentElement) {
                if (I18N_SKIP.has(p.tagName) || p.hasAttribute('data-no-i18n')) return NodeFilter.FILTER_REJECT;
            }
            return NodeFilter.FILTER_ACCEPT;
        },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const n of nodes) {
        const key = n.nodeValue.replace(/\s+/g, ' ').trim();
        if (key && Object.prototype.hasOwnProperty.call(EN, key)) {
            const lead = n.nodeValue.match(/^\s*/)[0], tail = n.nodeValue.match(/\s*$/)[0];
            n.nodeValue = lead + EN[key] + tail;
        }
    }
    for (const attr of ['title', 'placeholder', 'alt', 'data-name']) {
        root.querySelectorAll(`[${attr}]`).forEach((el) => {
            const v = el.getAttribute(attr);
            if (Object.prototype.hasOwnProperty.call(EN, v)) el.setAttribute(attr, EN[v]);
        });
    }
    root.querySelectorAll('[data-i18n-html]').forEach((el) => {
        const v = EN[el.getAttribute('data-i18n-html')];
        if (v) el.innerHTML = v;
    });
}

function setupLangToggle() {
    document.documentElement.lang = LANG;
    try { window.companion.setLang(LANG); } catch { /* 메인 프로세스가 예전 버전이어도 화면은 뜬다 */ }
    const btn = document.getElementById('btn-lang');
    if (!btn) return;
    btn.textContent = LANG === 'ko' ? 'EN' : '한국어';
    btn.onclick = () => {
        try { localStorage.setItem(LANG_KEY, LANG === 'ko' ? 'en' : 'ko'); } catch { /* 무시 */ }
        location.reload();
    };
}

translateDom(document.body);
setupLangToggle();
