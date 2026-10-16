// ── 기억 지도 탭 ────────────────────────────────────────────
// 캐릭터가 실제로 들고 있는 벡터(768차원)를 그대로 받아 두 가지로 본다.
//   벡터 지도 — 2차원으로 눕힌 점. 이어진 선은 서로 거리가 gate 이하인 짝
//               (검색이 "관련 있다"고 보는 것과 같은 눈금)
//   마인드맵 — 캐릭터 → 주제마다 한 갈래(about) / 사람마다 한 갈래 / 있었던 일 / 나 자신
//               → 기억 한 줄씩. 주제가 붙은 기억은 주제 쪽에만 달린다 (중복 방지).
//               방사형은 한국어 문장이 서로 겹쳐 못 읽어서 가로 트리로 편다.
const MAP_TYPE = {
    person: { ko: tr('사람에 대해 아는 것'), color: '#6ea8fe' },
    episode: { ko: tr('있었던 일'), color: '#f0a868' },
    self: { ko: tr('자기 자신'), color: '#8ee3b0' },
    // 갈래 색으로만 쓴다 — 노드의 type에는 'topic'이 없어서 범례에는 안 나온다
    topic: { ko: tr('주제'), color: '#c79bf0' },
    '': { ko: tr('분류 없음'), color: '#767e93' },
};
let mapData = null, mapView = 'vector', mapLayout = 'tsne';
let mapSel = -1, mapQuery = '', mapLoading = false;

// 마인드맵은 모든 기억을 한 줄씩 펼쳐 그렸다 — 한 사람 갈래만 86줄이라 주제를 맨 앞에 둬도
// 그 아래로 끝없이 밀렸고, 화면에 들어오는 건 한 갈래의 일부뿐이었다. 묶음이 먼저 보이고
// 눌러야 펼쳐지는 쪽이 맞다. 펼친 갈래는 재시작해도 유지한다 — 막힌 저장소가 화면을
// 통째로 죽이지 않도록 읽기·쓰기를 모두 감싼다 (앱의 companion.collapsed와 같은 방식)
const MAP_OPEN_KEY = 'companion.mapOpen';
const MAP_LEAF_MAX = 20;          // 펼쳐도 한 번에 이만큼만 — 86줄은 펼쳐도 못 읽는다
let mapOpen = new Set(), mapMore = new Set();
try { mapOpen = new Set(JSON.parse(localStorage.getItem(MAP_OPEN_KEY) || '[]')); } catch (e) { mapOpen = new Set(); }
const mapSaveOpen = () => {
    try { localStorage.setItem(MAP_OPEN_KEY, JSON.stringify([...mapOpen])); } catch (e) { /* 저장 못 해도 그린다 */ }
};

const mapType = (t) => MAP_TYPE[t] || MAP_TYPE[''];
const mapCut = (t, k) => (t.length > k ? t.slice(0, k - 1) + '…' : t);
const mapEsc = (t) => (t || '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const mapHit = (n) => !mapQuery || (n.text + ' ' + (n.name || '')).toLowerCase().includes(mapQuery);

async function loadMap() {
    if (mapLoading) return;
    mapLoading = true;
    $('map-hint').textContent = mapLayout === 'tsne'
        ? tr('기억을 불러와 덩어리를 계산하는 중...') : tr('기억을 불러오는 중...');
    try {
        const d = await window.companion.inspectMemoryMap(mapLayout);
        if (d.error) throw new Error(d.error);
        mapData = d;
        mapSel = -1;
        $('map-hint').textContent = d.nodes.length
            ? '' : tr('아직 저장된 기억이 없습니다. 대화가 한 덩어리 끝나면 쌓입니다.');
        $('map-stat').textContent = tr('{n}개 · 연결 {e} · {dims}차원 → 2차원 ({layout}{explained})', {
            n: d.nodes.length, e: d.edges.length, dims: d.dims, layout: d.layout,
            explained: d.explained ? tr(', {p}% 담김', { p: Math.round(d.explained * 100) }) : '',
        });
        drawMapLegend();
        drawMap();
    } catch (e) {
        $('map-hint').textContent =
            tr('API 서버가 꺼져 있거나 예전 버전입니다 — 서버를 다시 시작해 주세요. ({err})', { err: e.message });
    } finally {
        mapLoading = false;
    }
}

function drawMapLegend() {
    const counts = {};
    mapData.nodes.forEach((n) => { counts[n.type] = (counts[n.type] || 0) + 1; });
    $('map-legend').innerHTML = Object.keys(counts).sort().map((t) => {
        const i = mapType(t);
        return '<div class="map-lg"><span class="d" style="background:' + i.color + '"></span>'
            + i.ko + '<b>' + counts[t] + '</b></div>';
    }).join('')
    + '<div class="map-note">'
    + tr('점을 누르면 그 기억과 가까운 것들이 보입니다. 이어진 짝은 서로 거리가 {gate} 이하 — 검색이 “관련 있다”고 보는 것과 같은 눈금입니다. <b>덩어리 보기</b>는 뭉치는 모양만 맞고, 덩어리 사이 거리는 뜻이 없습니다.',
         { gate: mapData.gate })
    + '</div>';
}

function mapDetail() {
    const box = $('map-detail');
    box.className = 'map-detail';
    if (mapSel < 0) { box.innerHTML = ''; return; }
    const n = mapData.nodes[mapSel];
    const near = mapData.edges
        .filter((e) => e.a === mapSel || e.b === mapSel)
        .map((e) => ({ j: e.a === mapSel ? e.b : e.a, d: e.d }))
        .sort((a, b) => a.d - b.d);
    let html = '<h3>' + mapType(n.type).ko + (n.name ? ' · ' + mapEsc(n.name) : '')
        + (n.about ? ' <span style="color:' + MAP_TYPE.topic.color + '">· ' + mapEsc(n.about) + '</span>' : '')
        + '</h3>'
        + '<div class="txt">' + mapEsc(n.text) + '</div>'
        + '<div class="meta">' + tr('기록 ') + (mapEsc(n.ts).replace('T', ' ').slice(0, 16) || '—')
        + (n.due ? '<br>' + tr('예정일 ') + mapEsc(n.due) : '') + '</div><h3>' + tr('가까운 기억') + '</h3>';
    html += near.length
        ? '<div class="near">' + near.map((x) => '<div data-i="' + x.j + '"><b>' + x.d + '</b> · '
            + mapEsc(mapCut(mapData.nodes[x.j].text, 70)) + '</div>').join('') + '</div>'
        : '<div class="map-note">' + tr('가까운 기억이 없습니다 — 혼자 떨어져 있어, 관련 검색으로는 잘 걸리지 않습니다.') + '</div>';
    box.innerHTML = html;
    box.querySelectorAll('[data-i]').forEach((el2) => {
        el2.onclick = () => { mapSel = +el2.dataset.i; drawMap(); };
    });
}

function mapVectorSvg() {
    const N = mapData.nodes, E = mapData.edges, W = 1000, H = 700, pad = 44;
    const xs = N.map((n) => n.x), ys = N.map((n) => n.y);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const sx = (v) => pad + ((v - x0) / ((x1 - x0) || 1)) * (W - pad * 2);
    const sy = (v) => H - pad - ((v - y0) / ((y1 - y0) || 1)) * (H - pad * 2);
    const near = {};
    if (mapSel >= 0) {
        E.forEach((e) => {
            if (e.a === mapSel) near[e.b] = 1;
            if (e.b === mapSel) near[e.a] = 1;
        });
    }
    const my = sy((y0 + y1) / 2), mx = sx((x0 + x1) / 2);
    let s = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet">';
    s += '<line class="ax" x1="' + pad + '" y1="' + my + '" x2="' + (W - pad) + '" y2="' + my + '"/>';
    s += '<line class="ax" x1="' + mx + '" y1="' + pad + '" x2="' + mx + '" y2="' + (H - pad) + '"/>';
    s += '<text class="ax-t" x="' + pad + '" y="' + (H - 12) + '">'
        + tr('가까울수록 비슷한 이야기 · {dims}차원을 2차원으로 누른 그림 ({layout})', { dims: mapData.dims, layout: mapData.layout })
        + '</text>';
    if ($('map-edges').checked) {
        E.forEach((e) => {
            const hot = mapSel >= 0 && (e.a === mapSel || e.b === mapSel);
            const cls = 'lk' + (hot ? ' hot' : (mapSel >= 0 ? ' dim' : ''));
            s += '<line class="' + cls + '" x1="' + sx(N[e.a].x) + '" y1="' + sy(N[e.a].y)
                + '" x2="' + sx(N[e.b].x) + '" y2="' + sy(N[e.b].y) + '"/>';
        });
    }
    N.forEach((n, i) => {
        const dim = !mapHit(n) || (mapSel >= 0 && i !== mapSel && !near[i]);
        s += '<circle class="nd' + (dim ? ' dim' : '') + (i === mapSel ? ' sel' : '') + '" data-i="' + i
            + '" cx="' + sx(n.x) + '" cy="' + sy(n.y) + '" r="' + (n.type === 'episode' ? 7 : 5.5)
            + '" fill="' + mapType(n.type).color + '"/>';
    });
    if (mapSel >= 0) {
        const n = N[mapSel];
        s += '<text x="' + sx(n.x) + '" y="' + (sy(n.y) - 12) + '" text-anchor="middle"'
            + ' style="font-size:12px;fill:#fff">' + mapEsc(mapCut(n.text, 24)) + '</text>';
    }
    return s + '</svg>';
}

function mapMindSvg() {
    const N = mapData.nodes, groups = [], byPerson = {}, byTopic = {};
    // 주제(about)가 붙은 기억은 **주제 갈래로만** 간다. 사람 밑에도 같이 두면 중복이고,
    // 애초의 문제가 "작품에 대한 사실이 사람 밑에 섞여 있다"는 것이라 묶음이 보이지 않는다.
    // 주제를 맨 앞에 두는 이유도 같다 — 섞이는지 보려고 켜는 화면이다
    N.forEach((n, i) => { if (n.about) (byTopic[n.about] = byTopic[n.about] || []).push(i); });
    Object.keys(byTopic).sort((a, b) => byTopic[b].length - byTopic[a].length)
        .forEach((t) => groups.push({ label: t, kind: 'topic', items: byTopic[t] }));
    const free = (i) => !N[i].about;

    N.forEach((n, i) => {
        if (n.type === 'person' && free(i)) {
            const key = n.name || n.who || tr('이름 없음');
            (byPerson[key] = byPerson[key] || []).push(i);
        }
    });
    Object.keys(byPerson).sort((a, b) => byPerson[b].length - byPerson[a].length)
        .forEach((name) => groups.push({ label: name, kind: 'person', items: byPerson[name] }));
    const pick = (t) => N.map((n, i) => (n.type === t && free(i) ? i : -1)).filter((i) => i >= 0);
    const eps = pick('episode').sort((a, b) => (N[b].ts || '').localeCompare(N[a].ts || ''));
    if (eps.length) groups.push({ label: tr('있었던 일'), kind: 'episode', items: eps });
    // 자기 기억은 취향·설정과 '겪은 일'이 성격이 달라 갈래를 나눈다
    const selfs = pick('self');
    const lived = selfs.filter((i) => N[i].kind === 'experience');
    const tastes = selfs.filter((i) => N[i].kind !== 'experience');
    if (tastes.length) groups.push({ label: tr('나 자신'), kind: 'self', items: tastes });
    if (lived.length) groups.push({ label: tr('나에게 있었던 일'), kind: 'self', items: lived });
    const rest = N.map((n, i) => (free(i) && n.type !== 'person' && n.type !== 'episode'
        && n.type !== 'self' ? i : -1)).filter((i) => i >= 0);
    if (rest.length) groups.push({ label: tr('그 밖'), kind: '', items: rest });

    const ROW = 21, GAP = 22, TOP = 30, HUB_X = 82, GRP_X = 240, LEAF_X = 420, W = 1180;
    let y = TOP, leaves = '';
    const anchors = [];
    // 찾는 말이 있으면 걸리는 갈래는 저절로 펼친다 — 접혀 있어서 못 찾는 일이 없게
    const hitIn = (g) => mapQuery && g.items.some((i) => mapHit(N[i]));
    groups.forEach((g) => {
        const y0 = y;
        const open = mapOpen.has(g.label) || hitIn(g);
        const shown = !open ? [] : (mapMore.has(g.label) ? g.items : g.items.slice(0, MAP_LEAF_MAX));
        const hidden = open ? g.items.length - shown.length : 0;
        shown.forEach((i, k) => {
            const ly = y0 + k * ROW, color = mapType(N[i].type).color;
            const label = N[i].text + (N[i].due ? tr('  (예정 {d})', { d: N[i].due }) : '');
            leaves += '<circle class="nd" data-i="' + i + '" cx="' + LEAF_X + '" cy="' + (ly - 4)
                + '" r="3.2" fill="' + color + '"/>';
            leaves += '<text class="leaf' + (mapHit(N[i]) ? '' : ' dim') + (i === mapSel ? ' sel' : '')
                + '" data-i="' + i + '" x="' + (LEAF_X + 10) + '" y="' + ly + '">'
                + mapEsc(mapCut(label, 62)) + '</text>';
        });
        if (hidden > 0) {
            const ly = y0 + shown.length * ROW;
            leaves += '<text class="leaf more" data-more="' + mapEsc(g.label) + '" x="' + (LEAF_X + 10)
                + '" y="' + ly + '">' + tr('+ {n}개 더 보기', { n: hidden }) + '</text>';
        }
        const rows = Math.max(1, shown.length + (hidden > 0 ? 1 : 0));
        anchors.push({
            kind: g.kind, label: g.label, n: g.items.length, open: open, rows: shown.length,
            y0: y0, y: y0 + ((rows - 1) * ROW) / 2, y1: y0 + (rows - 1) * ROW,
        });
        y = y0 + rows * ROW + GAP;
    });
    const H = y + 8, hy = TOP + (H - TOP) / 2 - 20;
    let pre = '';
    anchors.forEach((g) => {
        const c = mapType(g.kind).color;
        pre += '<path class="lk" d="M' + (HUB_X + 42) + ',' + hy + ' C' + (GRP_X - 60) + ',' + hy
            + ' ' + (GRP_X - 60) + ',' + g.y + ' ' + GRP_X + ',' + g.y
            + '" style="stroke:' + c + ';opacity:.5;stroke-width:1.6"/>';
        // 갈래 이름은 **접혀 있어도** 그린다 — 접기를 넣을 때 이 return을 이름보다 위에 두어
        // 접힌 갈래가 선만 남고 이름이 사라졌다 (10-03, 실제 화면에서 잡힘)
        pre += '<text class="grp grp-tap" data-grp="' + mapEsc(g.label) + '" x="' + (GRP_X + 6)
            + '" y="' + (g.y + 1) + '">' + (g.open ? '▾ ' : '▸ ') + mapEsc(g.label) + '</text>';
        pre += '<text class="grp-n grp-tap" data-grp="' + mapEsc(g.label) + '" x="' + (GRP_X + 6)
            + '" y="' + (g.y + 15) + '">' + tr('{n}개', { n: g.n }) + '</text>';
        if (!g.open) return;
        pre += '<path class="lk" d="M' + (GRP_X + 104) + ',' + g.y + ' C' + (LEAF_X - 46) + ',' + g.y
            + ' ' + (LEAF_X - 46) + ',' + g.y0 + ' ' + (LEAF_X - 12) + ',' + (g.y0 - 4)
            + '" style="stroke:' + c + ';opacity:.22"/>';
        pre += '<line class="lk" x1="' + (LEAF_X - 12) + '" y1="' + (g.y0 - 4) + '" x2="' + (LEAF_X - 12)
            + '" y2="' + (g.y1 - 4) + '" style="stroke:' + c + ';opacity:.35"/>';
        for (let k = 0; k < g.rows; k++) {
            const ly = g.y0 + k * ROW - 4;
            pre += '<line class="lk" x1="' + (LEAF_X - 12) + '" y1="' + ly + '" x2="' + LEAF_X
                + '" y2="' + ly + '" style="stroke:' + c + ';opacity:.35"/>';
        }
    });
    pre += '<circle cx="' + HUB_X + '" cy="' + hy + '" r="40" fill="#2c2c40" stroke="#3b3b52"/>';
    pre += '<text class="hub" x="' + HUB_X + '" y="' + (hy + 5) + '" text-anchor="middle">' + String(CHAR_NAME).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '">'
        + pre + leaves + '</svg>';
}

function drawMap() {
    if (!mapData) return;
    const stage = $('map-stage'), tip = $('map-tip');
    stage.querySelectorAll('svg').forEach((el2) => el2.remove());
    stage.insertAdjacentHTML('beforeend', mapView === 'vector' ? mapVectorSvg() : mapMindSvg());
    stage.classList.toggle('scrolly', mapView === 'mind');
    // 갈래 머리를 누르면 펼치고 접는다. 글자 두 줄(이름·개수) 모두 같은 대상이다
    stage.querySelectorAll('svg [data-grp]').forEach((el2) => {
        el2.onclick = () => {
            const g = el2.dataset.grp;
            if (mapOpen.has(g)) { mapOpen.delete(g); mapMore.delete(g); } else { mapOpen.add(g); }
            mapSaveOpen();
            drawMap();
        };
    });
    stage.querySelectorAll('svg [data-more]').forEach((el2) => {
        el2.onclick = () => { mapMore.add(el2.dataset.more); drawMap(); };
    });
    stage.querySelectorAll('svg [data-i]').forEach((el2) => {
        const i = +el2.dataset.i;
        el2.onclick = () => { mapSel = mapSel === i ? -1 : i; drawMap(); };
        el2.onmousemove = (ev) => {
            const n = mapData.nodes[i];
            tip.innerHTML = '<b>' + mapEsc(n.name || mapType(n.type).ko) + '</b><br>' + mapEsc(n.text);
            tip.style.opacity = 1;
            const r = stage.getBoundingClientRect();
            tip.style.left = Math.min(ev.clientX - r.left + 14, stage.clientWidth - 320) + 'px';
            tip.style.top = (ev.clientY - r.top + 14 + stage.scrollTop) + 'px';
        };
        el2.onmouseleave = () => { tip.style.opacity = 0; };
    });
    mapDetail();
}

document.querySelectorAll('#tab-map .seg-btn').forEach((b) => {
    b.onclick = () => {
        b.parentElement.querySelectorAll('.seg-btn').forEach((x) => x.classList.toggle('on', x === b));
        if (b.dataset.view) {
            mapView = b.dataset.view;
            drawMap();
        } else {
            mapLayout = b.dataset.layout;   // 배치는 서버가 다시 계산한다
            loadMap();
        }
    };
});
$('btn-map-refresh').onclick = loadMap;
$('map-edges').onchange = drawMap;
$('map-q').oninput = (e) => { mapQuery = e.target.value.trim().toLowerCase(); drawMap(); };
