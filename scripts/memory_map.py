# -*- coding: utf-8 -*-
"""캐릭터의 기억을 눈으로 — 마인드맵 + 벡터 지도 HTML을 만든다.

    python scripts/memory_map.py            # 서버가 켜져 있으면 서버에서, 아니면 직접 읽어서
    python scripts/memory_map.py --open     # 만들고 브라우저로 열기

API 서버가 떠 있으면 /inspect/memory/map을 쓴다. ChromaDB는 두 프로세스가 같이 열면
읽기가 깨지므로, 서버가 켜져 있는 동안에는 반드시 서버를 거쳐야 한다.
"""
import argparse
import json
import os
import sys
import webbrowser

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

API = "http://127.0.0.1:8000"


def from_server(collection: str, tsne: bool = False):
    import httpx
    r = httpx.get(f"{API}/inspect/memory/map",
                  params={"layout": "tsne" if tsne else "pca"}, timeout=120.0)
    r.raise_for_status()
    return r.json()


def from_disk(collection: str, db_dir: str = None):
    """서버가 꺼져 있을 때 — 직접 Chroma를 읽고 같은 형식으로 만든다"""
    import numpy as np
    from vector_db import VectorDB

    store = VectorDB(collection_name=collection,
                     persist_directory=db_dir or os.path.join(BASE, "data", "chroma_db"))
    data = store.collection.get(include=["documents", "metadatas", "embeddings"], limit=2000)
    ids = list(data.get("ids") or [])
    docs = list(data.get("documents") or [])
    metas = [m or {} for m in (data.get("metadatas") or [])]
    raw = data.get("embeddings")
    embs = np.asarray(raw if raw is not None else [], dtype="float32")
    if not ids or embs.size == 0:
        return {"nodes": [], "edges": [], "people": {}, "explained": 0.0, "dims": 0}

    centered = embs - embs.mean(axis=0)
    u, sv, _ = np.linalg.svd(centered, full_matrices=False)
    coords = u[:, :2] * sv[:2]
    var = sv ** 2
    explained = float(var[:2].sum() / var.sum()) if float(var.sum()) else 0.0

    sq = (embs ** 2).sum(axis=1)
    d2 = sq[:, None] + sq[None, :] - 2.0 * embs @ embs.T
    np.fill_diagonal(d2, np.inf)
    seen = {}
    for i in range(len(ids)):
        for j in np.argsort(d2[i])[:3]:
            j = int(j)
            if float(d2[i, j]) > 120.0:
                continue
            seen[(min(i, j), max(i, j))] = round(float(d2[i, j]), 1)

    people = {}
    try:
        with open(os.path.join(BASE, "data", "people.json"), "r", encoding="utf-8") as f:
            people = {k: (v.get("name") or k) for k, v in (json.load(f) or {}).items()}
    except Exception:
        pass

    nodes = []
    for i, doc in enumerate(docs):
        meta = metas[i]
        who = meta.get("who") or ""
        nodes.append({"id": ids[i], "text": doc or "", "type": meta.get("type") or "",
                      "kind": meta.get("kind") or "",
                      "who": who, "name": meta.get("name") or people.get(who) or who,
                      "ts": meta.get("ts") or "", "due": meta.get("due") or "",
                      "x": round(float(coords[i, 0]), 4), "y": round(float(coords[i, 1]), 4)})
    return {"nodes": nodes, "edges": [{"a": a, "b": b, "d": d} for (a, b), d in sorted(seen.items())],
            "people": people, "explained": round(explained, 3), "dims": int(embs.shape[1]),
            "gate": 120.0, "vectors": embs.tolist()}


def _project_tsne(data: dict):
    """PCA는 768차원 중 20%밖에 못 담는다. t-SNE는 가까운 것끼리 뭉쳐 보여 준다
    (대신 덩어리 사이의 거리는 뜻이 없다 — 판단은 연결선의 실제 거리로)"""
    import numpy as np
    vecs = data.pop("vectors", None)
    if not vecs:
        print("! 원벡터가 없어 t-SNE를 건너뜁니다")
        return
    from sklearn.manifold import TSNE
    x = np.asarray(vecs, dtype="float32")
    n = len(x)
    coords = TSNE(n_components=2, perplexity=max(2, min(30, (n - 1) // 3)),
                  init="pca", random_state=0).fit_transform(x)
    for i, node in enumerate(data["nodes"]):
        node["x"], node["y"] = round(float(coords[i, 0]), 3), round(float(coords[i, 1]), 3)
    data["explained"] = None
    data["layout"] = "t-SNE"


def server_alive() -> bool:
    try:
        import httpx
        return httpx.get(f"{API}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--open", action="store_true", help="만든 뒤 브라우저로 연다")
    ap.add_argument("--out", default=os.path.join(BASE, "data", "memory_map.html"))
    ap.add_argument("--tsne", action="store_true",
                    help="PCA 대신 t-SNE로 눕힌다 — 덩어리(주제)가 훨씬 잘 보인다")
    ap.add_argument("--db", default=None,
                    help="다른 chroma_db 경로를 읽는다 (서버가 켜져 있을 때 복사본을 보는 용도)")
    args = ap.parse_args()

    collection = "memory"
    alive = server_alive() and not args.db
    if args.archive or not alive:
        if alive:
            print("! 서버가 켜져 있는데 보관 컬렉션을 직접 읽습니다. 실패하면 서버를 끄고 다시 실행하세요")
        data = from_disk(collection, args.db)
        source = "복사본" if args.db else "직접 읽음"
    else:
        try:
            data = from_server(collection, tsne=args.tsne)
            source = "API 서버"
        except Exception as e:
            print(f"! 서버에 지도 엔드포인트가 없습니다 ({e}). API 서버를 재시작하세요")
            return

    if args.tsne and data.get("layout") != "t-SNE":
        _project_tsne(data)   # 직접 읽은 경우 (서버가 이미 했으면 건너뛴다)
    nodes = data["nodes"]
    if not nodes:
        print(f"기억이 비어 있습니다 ({collection})")
        return
    counts = {}
    for n in nodes:
        counts[n["type"]] = counts.get(n["type"], 0) + 1

    data.pop("vectors", None)
    data.setdefault("layout", "PCA")
    data["collection"] = collection
    data["source"] = source
    from datetime import datetime
    data["built"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_map_template.html")
    with open(tpl, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    try:
        sys.path.insert(0, os.path.join(BASE, "src"))
        import character
        name = character.NAME
    except Exception:
        name = "캐릭터"
    html = html.replace("__NAME__", name.replace("<", "&lt;"))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"기억 {len(nodes)}개 ({collection}, {source}): " +
          " · ".join(f"{k or '?'} {v}" for k, v in sorted(counts.items())))
    how = data["layout"] + (f", 2차원이 담아낸 비율 {data['explained']:.0%}" if data.get("explained") else "")
    print(f"연결선 {len(data['edges'])}개, {data['dims']}차원을 2차원으로 ({how})")
    print(f"→ {args.out}")
    if args.open:
        webbrowser.open("file:///" + args.out.replace("\\", "/"))


if __name__ == "__main__":
    main()
