import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
import os
import uuid

class VectorDB:
    def __init__(self, collection_name="memory", persist_directory="data/chroma_db",
                 embedding_model=None):
        """
        ChromaDB 및 임베딩 모델 초기화
        :param embedding_model: 이미 로드된 SentenceTransformer를 넘기면 재사용한다
            (예: lore 채널이 memory 것을 공유 → 모델 중복 로드 방지)
        """
        # 데이터 디렉토리 생성
        if not os.path.exists(persist_directory):
            os.makedirs(persist_directory)

        self.collection_name = collection_name

        # ChromaDB 클라이언트 초기화 (PersistentClient 사용)
        self.client = chromadb.PersistentClient(path=persist_directory)

        # 한국어 특화 임베딩 모델 (768차원 — 모델 변경 시 scripts/reembed.py로 재임베딩 필수)
        # CPU 고정: GPU는 Whisper(ctranslate2)가 쓰는데, 다른 스레드에서 torch가 동시에
        # GPU를 잡으면 경합/행의 원인이 됨. 쿼리 1건 인코딩은 CPU로도 수십 ms면 충분
        if embedding_model is not None:
            self.embedding_model = embedding_model
        else:
            print("Loading embedding model...")
            self.embedding_model = SentenceTransformer('jhgan/ko-sroberta-multitask', device='cpu')

        # 컬렉션 가져오기 또는 생성
        self.collection = self.client.get_or_create_collection(name=collection_name)
        print(f"Vector DB initialized. Collection: {collection_name}")

    def reset(self):
        """컬렉션을 비우고 다시 만든다 (재적재용 — lore ingestion 등)"""
        try:
            self.client.delete_collection(self.collection_name)
        except Exception:
            pass
        self.collection = self.client.get_or_create_collection(name=self.collection_name)

    def add_memory(self, text: str, metadata: dict = None):
        """
        텍스트를 임베딩하여 벡터 DB에 저장하고 id를 돌려준다
        """
        if not text:
            return None

        # 임베딩 생성
        embedding = self.embedding_model.encode(text).tolist()

        # ID 생성
        doc_id = str(uuid.uuid4())

        # 메타데이터 기본값
        if metadata is None:
            metadata = {}

        # 저장
        self.collection.add(
            documents=[text],
            embeddings=[embedding],
            metadatas=[metadata],
            ids=[doc_id]
        )
        return doc_id

    def embed(self, text: str) -> list:
        return self.embedding_model.encode(text).tolist()

    def query_memory(self, query_text: str, n_results: int = 3, where: dict = None, embedding: list = None):
        """
        쿼리와 유사한 기억 검색. where로 메타데이터를 거른다 (예: {"type": "person"}).
        같은 질의로 여러 번 거를 때는 embed()한 벡터를 embedding으로 넘겨 인코딩을 한 번만 한다
        """
        if not query_text and embedding is None:
            return []
        n = min(n_results, self.collection.count())
        if n <= 0:
            return []

        # 쿼리 임베딩
        query_embedding = embedding if embedding is not None else self.embed(query_text)

        # 검색
        kwargs = {"query_embeddings": [query_embedding], "n_results": n}
        if where:
            kwargs["where"] = where
        results = self.collection.query(**kwargs)

        # 결과 포맷팅
        formatted_results = []
        if results['documents']:
            for i, doc in enumerate(results['documents'][0]):
                meta = results['metadatas'][0][i] if results['metadatas'] else {}
                formatted_results.append({
                    "id": results['ids'][0][i],
                    "text": doc,
                    "metadata": meta,
                    "distance": results['distances'][0][i] if results['distances'] else 0
                })

        return formatted_results

    def get_where(self, where: dict):
        """조건에 맞는 문서 전부 (검색어 없이) — [{id, text, metadata}]"""
        data = self.collection.get(where=where, include=["documents", "metadatas"])
        return [{"id": i, "text": d, "metadata": m or {}}
                for i, d, m in zip(data.get("ids") or [], data.get("documents") or [], data.get("metadatas") or [])]

    def delete(self, ids: list):
        if ids:
            self.collection.delete(ids=ids)

    def count(self):
        return self.collection.count()

# 싱글톤 인스턴스 (필요시 사용)
# vector_db = VectorDB()
