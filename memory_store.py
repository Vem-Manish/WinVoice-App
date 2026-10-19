# memory_store.py
import os
import threading

# Prevent Windows Symlink errors when downloading the embedding model
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
# Silence ONNXRuntime provider warnings (TensorRT missing DLLs)
os.environ["ORT_LOGGING_LEVEL"] = "3"
try:
    import onnxruntime as ort
    ort.set_default_logger_severity(3)
    import chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 as _onnx_mod
    _orig_onnx_init = _onnx_mod.ONNXMiniLM_L6_V2.__init__
    def _safe_onnx_init(self, preferred_providers=None):
        _orig_onnx_init(self, preferred_providers=['CPUExecutionProvider'])
    _onnx_mod.ONNXMiniLM_L6_V2.__init__ = _safe_onnx_init
except Exception:
    pass

_client = None
_collection = None
_lock = threading.Lock()


def init_memory():
    """Initializes the Vector DB in the background so it doesn't cause UI lag."""
    global _client, _collection
    with _lock:
        if _collection is not None:
            return
        try:
            import chromadb
            import logging
            # Silence chromadb telemetry/logs
            logging.getLogger("chromadb").setLevel(logging.ERROR)

            base_dir = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(base_dir, ".chroma_db")

            _client = chromadb.PersistentClient(path=db_path)
            _collection = _client.get_or_create_collection(name="winvoice_memory")
            print("[Memory] Long-Term Vector DB initialized.")
        except Exception as e:
            print(f"[Memory] Failed to init DB: {e}")


def remember(text: str) -> bool:
    """Saves a string of text into the vector database."""
    init_memory()
    if not _collection: return False
    try:
        import time
        doc_id = f"mem_{int(time.time() * 1000)}"
        _collection.add(
            documents=[text],
            metadatas=[{"timestamp": time.time()}],
            ids=[doc_id]
        )
        return True
    except Exception as e:
        print(f"[Memory] Save error: {e}")
        return False


def recall(query: str, k: int = 2) -> list[str]:
    """Searches the database for the most relevant memories."""
    init_memory()
    if not _collection: return []
    try:
        if _collection.count() == 0:
            return []

        results = _collection.query(
            query_texts=[query],
            n_results=min(k, _collection.count())
        )

        if results and results.get("documents") and results["documents"][0]:
            # Return the top K matched strings
            return results["documents"][0]
        return []
    except Exception as e:
        print(f"[Memory] Recall error: {e}")
        return []


def get_all_memories() -> list[dict]:
    """Retrieves all memories to display in the UI visualization."""
    init_memory()
    if not _collection: return []
    try:
        results = _collection.get()
        docs = results.get("documents", [])
        metas = results.get("metadatas", [])
        ids = results.get("ids", [])

        out = []
        for i in range(len(docs)):
            if not docs[i]: continue
            out.append({
                "id": ids[i],
                "text": docs[i],
                "timestamp": metas[i].get("timestamp", 0) if metas and metas[i] else 0
            })

        # Sort newest first
        out.sort(key=lambda x: x["timestamp"], reverse=True)
        return out
    except Exception as e:
        print(f"[Memory] GetAll error: {e}")
        return []


def delete_memory(mem_id: str) -> bool:
    """Erases a specific memory from the vector database."""
    init_memory()
    if not _collection: return False
    try:
        _collection.delete(ids=[mem_id])
        return True
    except Exception as e:
        print(f"[Memory] Delete error: {e}")
        return False