"""Vector Database utilities for storing and querying developer knowledge.

Supports:
- Milvus Lite (embedded mode - runs in-process, no server needed)
- Milvus Server (production mode - connects to external Milvus server)
- ChromaDB (fallback if Milvus not available)
- JSON-based fallback with cosine similarity

Stores:
- Functionality documents with embeddings
- Developer skill vectors
- WI-to-functionality mappings

Single-Server Deployment:
- Uses Milvus Lite by default for embedded vector DB
- Data persisted to data/vector_db/milvus_lite/
- No external dependencies required
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import hashlib

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
VECTOR_DB_DIR = DATA_DIR / "vector_db"

# Try to import Milvus (supports both Milvus Lite and Milvus Server)
try:
    from pymilvus import (
        connections,
        Collection,
        CollectionSchema,
        FieldSchema,
        DataType,
        utility,
        MilvusClient,
    )
    MILVUS_AVAILABLE = True
    
    # Check if Milvus Lite is available (pymilvus >= 2.4.0)
    try:
        from pymilvus import MilvusClient
        MILVUS_LITE_AVAILABLE = True
        # Additional check: try to see if milvus-lite native library is available
        try:
            # On Windows, milvus-lite native binaries are not available
            import sys
            if sys.platform == "win32":
                MILVUS_LITE_AVAILABLE = False
                logger.info("Milvus Lite not available on Windows, will use ChromaDB")
        except Exception:
            pass
    except ImportError:
        MILVUS_LITE_AVAILABLE = False
except ImportError:
    MILVUS_AVAILABLE = False
    MILVUS_LITE_AVAILABLE = False
    logger.info("pymilvus not installed, will try ChromaDB or JSON fallback")

# Milvus Lite data directory
MILVUS_LITE_DIR = VECTOR_DB_DIR / "milvus_lite"


def _get_vector_db_config() -> Dict[str, Any]:
    """Load vector_db configuration from config.yaml."""
    try:
        from config import config as app_config
        return {
            "mode": getattr(app_config, "vector_db_mode", "auto"),
            "milvus_host": getattr(app_config, "vector_db_milvus_host", "122.175.13.154"),
            "milvus_port": getattr(app_config, "vector_db_milvus_port", 32030),
            "collection_name": getattr(app_config, "vector_db_collection_name", "pmagent"),
            "embedding_dim": getattr(app_config, "vector_db_embedding_dim", 1536),
        }
    except Exception as e:
        logger.debug("Could not load vector_db config: %s, using defaults", e)
        return {
            "mode": os.environ.get("MILVUS_MODE", "auto"),
            "milvus_host": os.environ.get("MILVUS_HOST", "122.175.13.154"),
            "milvus_port": int(os.environ.get("MILVUS_PORT", "32030")),
            "collection_name": os.environ.get("MILVUS_COLLECTION", "pmagent"),
            "embedding_dim": int(os.environ.get("MILVUS_DIM", "1536")),
        }


# Try to import chromadb
try:
    import chromadb
    from chromadb.config import Settings
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False
    logger.info("chromadb not installed, using JSON fallback")


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VECTOR_DB_DIR.mkdir(parents=True, exist_ok=True)


def _get_embedding_fn():
    """Get embedding function based on available API keys."""
    from config import config as app_config
    openai_key = app_config.openai_api_key
    
    if openai_key:
        try:
            from openai import OpenAI
            import httpx
            model = app_config.openai_embedding_model
            client = OpenAI(api_key=openai_key, timeout=httpx.Timeout(10.0, connect=5.0))
            
            def embed_openai(texts: List[str]) -> List[List[float]]:
                embeddings = []
                for text in texts:
                    if len(text) > 8000:
                        text = text[:8000]
                    resp = client.embeddings.create(model=model, input=text)
                    embeddings.append(resp.data[0].embedding)
                return embeddings
            
            return embed_openai, "openai"
        except Exception as e:
            logger.warning("OpenAI embedding failed: %s", e)
    
    # Fallback: simple TF-IDF-like hashing (no external API needed)
    logger.info("Using TF-IDF hash fallback for embeddings")
    
    def embed_hash(texts: List[str]) -> List[List[float]]:
        embeddings = []
        for text in texts:
            # Simple 256-dim hash-based embedding
            words = text.lower().split()
            vec = [0.0] * 256
            for word in words:
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                for i in range(256):
                    vec[i] += ((h >> i) & 1) * 0.1
            # Normalize
            mag = math.sqrt(sum(x*x for x in vec)) or 1.0
            vec = [x / mag for x in vec]
            embeddings.append(vec)
        return embeddings
    
    return embed_hash, "tfidf_hash"


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


class VectorStore:
    """Abstract interface for vector storage."""
    
    def add_documents(self, docs: List[Dict[str, Any]], collection: str = "default") -> int:
        raise NotImplementedError
    
    def query(self, query_text: str, collection: str = "default", top_k: int = 5) -> List[Dict[str, Any]]:
        raise NotImplementedError
    
    def get_all(self, collection: str = "default") -> List[Dict[str, Any]]:
        raise NotImplementedError
    
    def delete_collection(self, collection: str = "default") -> bool:
        """Delete a collection. Returns True if successful."""
        raise NotImplementedError
    
    def count(self, collection: str = "default") -> int:
        """Get document count in collection."""
        raise NotImplementedError


class MilvusVectorStore(VectorStore):
    """Milvus-based vector store for production-grade similarity search.
    
    Supports two modes:
    1. Milvus Lite (embedded) - runs in-process, no server needed
       - Use mode='lite' or set MILVUS_MODE=lite env var
       - Data stored in data/vector_db/milvus_lite/
       - Best for single-server deployments
       
    2. Milvus Server - connects to external Milvus instance
       - Use mode='server' with host/port
       - Best for distributed/production deployments
       - Default: 122.175.13.154:32030 (team's Milvus server)
    
    Best practices implemented:
    - Connection pooling via singleton pattern
    - Configurable via environment or config.yaml
    - Proper index creation for efficient search
    - Batch insert for performance
    - Graceful fallback if Milvus unavailable
    """
    
    # Milvus connection settings
    _connection_alias = "pm_agent_milvus"
    _connected = False
    _client: Optional["MilvusClient"] = None  # For Milvus Lite
    _mode: str = "server"  # 'lite' or 'server' - default to server now
    _collection_prefix: str = "pmagent"  # Collection name prefix from team
    
    def __init__(self, host: str = None, port: int = None, mode: str = None):
        _ensure_dirs()
        MILVUS_LITE_DIR.mkdir(parents=True, exist_ok=True)
        self.embed_fn, self.embed_type = _get_embedding_fn()
        
        # Load config from config.yaml
        cfg = _get_vector_db_config()
        
        # Set embedding dimension from config
        self.EMBEDDING_DIM = cfg.get("embedding_dim", 1536)
        MilvusVectorStore._collection_prefix = cfg.get("collection_name", "pmagent")
        
        # Determine mode: lite (embedded) or server
        MilvusVectorStore._mode = mode or cfg.get("mode", "server")
        
        if MilvusVectorStore._mode == "lite" and MILVUS_LITE_AVAILABLE:
            # Use Milvus Lite (embedded mode)
            self._init_lite()
        else:
            # Use Milvus Server mode
            self.host = host or cfg.get("milvus_host", "localhost")
            self.port = port or cfg.get("milvus_port", 19530)
            self._connect_server()
            logger.info("MilvusVectorStore (server mode) initialized with %s embeddings (host=%s, port=%s)", 
                        self.embed_type, self.host, self.port)
    
    def _init_lite(self):
        """Initialize Milvus Lite (embedded mode)."""
        db_path = str(MILVUS_LITE_DIR / "milvus_lite.db")
        MilvusVectorStore._client = MilvusClient(db_path)
        MilvusVectorStore._connected = True
        logger.info("MilvusVectorStore (lite mode) initialized with %s embeddings at %s", 
                    self.embed_type, db_path)
    
    def _connect_server(self):
        """Establish connection to Milvus server."""
        if MilvusVectorStore._connected:
            return
        
        try:
            connections.connect(
                alias=self._connection_alias,
                host=self.host,
                port=self.port,
                timeout=30  # Increased timeout for remote server
            )
            MilvusVectorStore._connected = True
            MilvusVectorStore._mode = "server"
            logger.info("✓ Connected to Milvus server at %s:%s", self.host, self.port)
        except Exception as e:
            logger.error("Failed to connect to Milvus server at %s:%s - %s", self.host, self.port, e)
            raise
    
    def _get_collection_name(self, name: str) -> str:
        """Get collection name using configured prefix.
        
        Uses the collection_name from config.yaml as prefix.
        Example: 'developer_skills' -> 'pmagent_developer_skills'
        """
        prefix = MilvusVectorStore._collection_prefix
        # If name is 'default', just use the prefix
        if name == "default":
            return prefix
        # Otherwise append the name
        full_name = f"{prefix}_{name}".replace("-", "_").replace(" ", "_")
        return full_name
    
    def _ensure_collection_lite(self, collection: str) -> str:
        """Ensure collection exists in Milvus Lite mode."""
        coll_name = self._get_collection_name(collection)
        
        # Check if collection exists
        existing = MilvusVectorStore._client.list_collections()
        if coll_name not in existing:
            # Create collection with schema for Milvus Lite
            MilvusVectorStore._client.create_collection(
                collection_name=coll_name,
                dimension=self.EMBEDDING_DIM,
                metric_type="COSINE",
            )
            logger.info("Created Milvus Lite collection: %s", coll_name)
        
        return coll_name
    
    def _get_or_create_collection(self, collection: str) -> Collection:
        """Get existing collection or create new one with proper schema (server mode)."""
        coll_name = self._get_collection_name(collection)
        
        if utility.has_collection(coll_name, using=self._connection_alias):
            coll = Collection(coll_name, using=self._connection_alias)
            coll.load()
            return coll
        
        # Define schema
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=256),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="metadata_json", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.EMBEDDING_DIM),
            FieldSchema(name="updated_at", dtype=DataType.VARCHAR, max_length=64),
        ]
        
        schema = CollectionSchema(
            fields=fields,
            description=f"PM Agent {collection} collection"
        )
        
        coll = Collection(
            name=coll_name,
            schema=schema,
            using=self._connection_alias
        )
        
        # Create IVF_FLAT index for efficient similarity search
        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128}
        }
        coll.create_index(field_name="embedding", index_params=index_params)
        coll.load()
        
        logger.info("Created Milvus collection: %s", coll_name)
        return coll
    
    def add_documents(self, docs: List[Dict[str, Any]], collection: str = "default") -> int:
        """Add or update documents in the collection."""
        if not docs:
            return 0
        
        # Use appropriate mode
        if MilvusVectorStore._mode == "lite" and MilvusVectorStore._client:
            return self._add_documents_lite(docs, collection)
        else:
            return self._add_documents_server(docs, collection)
    
    def _add_documents_lite(self, docs: List[Dict[str, Any]], collection: str) -> int:
        """Add documents using Milvus Lite client."""
        coll_name = self._ensure_collection_lite(collection)
        
        data = []
        now = datetime.now(timezone.utc).isoformat()
        
        for doc in docs:
            doc_id = str(doc.get("id") or doc.get("developer") or doc.get("path") or 
                        hashlib.md5(str(doc).encode()).hexdigest())
            text = doc.get("text") or doc.get("content") or doc.get("summary") or str(doc)
            meta = {k: str(v)[:500] for k, v in doc.items() 
                    if k not in ("embedding", "text", "content", "id")}
            
            # Compute embedding
            embedding = self.embed_fn([text[:8000]])[0]
            
            data.append({
                "id": doc_id[:256],
                "text": text[:65000],
                "metadata_json": json.dumps(meta)[:65000],
                "vector": embedding,
                "updated_at": now,
            })
        
        # Upsert (Milvus Lite supports upsert natively)
        MilvusVectorStore._client.upsert(collection_name=coll_name, data=data)
        
        logger.info("Inserted %d documents into Milvus Lite collection '%s'", len(data), collection)
        return len(data)
    
    def _add_documents_server(self, docs: List[Dict[str, Any]], collection: str) -> int:
        """Add documents using Milvus Server."""
        coll = self._get_or_create_collection(collection)
        
        ids = []
        texts = []
        metadata_jsons = []
        embeddings = []
        timestamps = []
        
        now = datetime.now(timezone.utc).isoformat()
        
        for doc in docs:
            doc_id = str(doc.get("id") or doc.get("developer") or doc.get("path") or 
                        hashlib.md5(str(doc).encode()).hexdigest())
            text = doc.get("text") or doc.get("content") or doc.get("summary") or str(doc)
            meta = {k: str(v)[:500] for k, v in doc.items() 
                    if k not in ("embedding", "text", "content", "id")}
            
            ids.append(doc_id[:256])
            texts.append(text[:65000])
            metadata_jsons.append(json.dumps(meta)[:65000])
            timestamps.append(now)
        
        # Compute embeddings in batch
        embeddings = self.embed_fn([t[:8000] for t in texts])
        
        # Prepare data for insert
        data = [ids, texts, metadata_jsons, embeddings, timestamps]
        
        # Delete existing documents with same IDs first (upsert behavior)
        try:
            expr = f'id in {ids}'
            coll.delete(expr)
        except Exception as e:
            logger.debug("Delete before upsert failed (may be empty collection): %s", e)
        
        # Insert new data
        coll.insert(data)
        coll.flush()
        
        logger.info("Inserted %d documents into Milvus collection '%s'", len(ids), collection)
        return len(ids)
    
    def query(self, query_text: str, collection: str = "default", top_k: int = 5) -> List[Dict[str, Any]]:
        """Query similar documents using vector similarity."""
        if MilvusVectorStore._mode == "lite" and MilvusVectorStore._client:
            return self._query_lite(query_text, collection, top_k)
        else:
            return self._query_server(query_text, collection, top_k)
    
    def _query_lite(self, query_text: str, collection: str, top_k: int) -> List[Dict[str, Any]]:
        """Query using Milvus Lite client."""
        coll_name = self._ensure_collection_lite(collection)
        
        query_embedding = self.embed_fn([query_text[:8000]])[0]
        
        results = MilvusVectorStore._client.search(
            collection_name=coll_name,
            data=[query_embedding],
            limit=top_k,
            output_fields=["id", "text", "metadata_json"]
        )
        
        output = []
        for hits in results:
            for hit in hits:
                metadata = {}
                try:
                    meta_str = hit.get("entity", {}).get("metadata_json", "{}")
                    metadata = json.loads(meta_str)
                except Exception:
                    pass
                
                # Milvus Lite uses 'distance' for COSINE (0 = identical)
                distance = hit.get("distance", 0)
                output.append({
                    "id": hit.get("entity", {}).get("id"),
                    "document": hit.get("entity", {}).get("text"),
                    "metadata": metadata,
                    "similarity": 1 - distance,  # Convert distance to similarity
                    "distance": distance,
                })
        
        return output
    
    def _query_server(self, query_text: str, collection: str, top_k: int) -> List[Dict[str, Any]]:
        """Query using Milvus Server."""
        coll = self._get_or_create_collection(collection)
        
        query_embedding = self.embed_fn([query_text[:8000]])[0]
        
        search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
        
        results = coll.search(
            data=[query_embedding],
            anns_field="embedding",
            param=search_params,
            limit=top_k,
            output_fields=["id", "text", "metadata_json"]
        )
        
        output = []
        for hits in results:
            for hit in hits:
                metadata = {}
                try:
                    metadata = json.loads(hit.entity.get("metadata_json", "{}"))
                except Exception:
                    pass
                
                output.append({
                    "id": hit.entity.get("id"),
                    "document": hit.entity.get("text"),
                    "metadata": metadata,
                    "similarity": 1 - hit.distance,  # Convert distance to similarity
                    "distance": hit.distance,
                })
        
        return output
    
    def get_all(self, collection: str = "default") -> List[Dict[str, Any]]:
        """Get all documents in collection."""
        if MilvusVectorStore._mode == "lite" and MilvusVectorStore._client:
            return self._get_all_lite(collection)
        else:
            return self._get_all_server(collection)
    
    def _get_all_lite(self, collection: str) -> List[Dict[str, Any]]:
        """Get all documents using Milvus Lite client."""
        coll_name = self._get_collection_name(collection)
        
        # Check if collection exists
        existing = MilvusVectorStore._client.list_collections()
        if coll_name not in existing:
            return []
        
        # Query all documents
        results = MilvusVectorStore._client.query(
            collection_name=coll_name,
            filter="",
            output_fields=["id", "text", "metadata_json", "updated_at"],
            limit=10000  # Max limit
        )
        
        output = []
        for item in results:
            metadata = {}
            try:
                metadata = json.loads(item.get("metadata_json", "{}"))
            except Exception:
                pass
            
            output.append({
                "id": item.get("id"),
                "document": item.get("text"),
                "metadata": metadata,
                "updated_at": item.get("updated_at"),
            })
        
        return output
    
    def _get_all_server(self, collection: str) -> List[Dict[str, Any]]:
        """Get all documents using Milvus Server."""
        coll = self._get_or_create_collection(collection)
        
        # Get count first
        count = coll.num_entities
        if count == 0:
            return []
        
        # Query all documents
        results = coll.query(
            expr="id != ''",
            output_fields=["id", "text", "metadata_json", "updated_at"],
            limit=count
        )
        
        output = []
        for item in results:
            metadata = {}
            try:
                metadata = json.loads(item.get("metadata_json", "{}"))
            except Exception:
                pass
            
            output.append({
                "id": item.get("id"),
                "document": item.get("text"),
                "metadata": metadata,
                "updated_at": item.get("updated_at"),
            })
        
        return output
    
    def delete_collection(self, collection: str = "default") -> bool:
        """Delete a collection."""
        coll_name = self._get_collection_name(collection)
        
        if MilvusVectorStore._mode == "lite" and MilvusVectorStore._client:
            try:
                existing = MilvusVectorStore._client.list_collections()
                if coll_name in existing:
                    MilvusVectorStore._client.drop_collection(collection_name=coll_name)
                    logger.info("Deleted Milvus Lite collection: %s", coll_name)
                    return True
            except Exception as e:
                logger.error("Failed to delete Lite collection %s: %s", coll_name, e)
            return False
        else:
            try:
                if utility.has_collection(coll_name, using=self._connection_alias):
                    utility.drop_collection(coll_name, using=self._connection_alias)
                    logger.info("Deleted Milvus collection: %s", coll_name)
                    return True
            except Exception as e:
                logger.error("Failed to delete collection %s: %s", coll_name, e)
            return False
    
    def count(self, collection: str = "default") -> int:
        """Get document count in collection."""
        coll_name = self._get_collection_name(collection)
        
        if MilvusVectorStore._mode == "lite" and MilvusVectorStore._client:
            try:
                existing = MilvusVectorStore._client.list_collections()
                if coll_name not in existing:
                    return 0
                stats = MilvusVectorStore._client.get_collection_stats(collection_name=coll_name)
                return stats.get("row_count", 0)
            except Exception:
                return 0
        else:
            try:
                coll = self._get_or_create_collection(collection)
                return coll.num_entities
            except Exception:
                return 0


class ChromaVectorStore(VectorStore):
    """ChromaDB-based vector store."""
    
    def __init__(self, persist_dir: Optional[Path] = None):
        _ensure_dirs()
        self.persist_dir = persist_dir or VECTOR_DB_DIR / "chroma"
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        
        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False)
        )
        self.embed_fn, self.embed_type = _get_embedding_fn()
        logger.info("ChromaDB initialized with %s embeddings", self.embed_type)
    
    def _get_collection(self, name: str):
        return self.client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"}
        )
    
    def add_documents(self, docs: List[Dict[str, Any]], collection: str = "default") -> int:
        if not docs:
            return 0
        
        coll = self._get_collection(collection)
        
        ids = []
        texts = []
        metadatas = []
        
        for doc in docs:
            doc_id = doc.get("id") or doc.get("path") or hashlib.md5(str(doc).encode()).hexdigest()
            text = doc.get("text") or doc.get("content") or doc.get("summary") or str(doc)
            meta = {k: str(v)[:500] for k, v in doc.items() if k not in ("embedding", "text", "content")}
            
            ids.append(str(doc_id))
            texts.append(text[:10000])
            metadatas.append(meta)
        
        # Compute embeddings
        embeddings = self.embed_fn(texts)
        
        # Upsert
        coll.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=texts
        )
        
        return len(ids)
    
    def query(self, query_text: str, collection: str = "default", top_k: int = 5) -> List[Dict[str, Any]]:
        coll = self._get_collection(collection)
        
        query_embedding = self.embed_fn([query_text])[0]
        
        results = coll.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"]
        )
        
        output = []
        for i, doc_id in enumerate(results.get("ids", [[]])[0]):
            item = {
                "id": doc_id,
                "document": results.get("documents", [[]])[0][i] if results.get("documents") else None,
                "metadata": results.get("metadatas", [[]])[0][i] if results.get("metadatas") else {},
                "distance": results.get("distances", [[]])[0][i] if results.get("distances") else None,
            }
            output.append(item)
        
        return output
    
    def get_all(self, collection: str = "default") -> List[Dict[str, Any]]:
        coll = self._get_collection(collection)
        results = coll.get(include=["documents", "metadatas"])
        
        output = []
        for i, doc_id in enumerate(results.get("ids", [])):
            output.append({
                "id": doc_id,
                "document": results.get("documents", [])[i] if results.get("documents") else None,
                "metadata": results.get("metadatas", [])[i] if results.get("metadatas") else {},
            })
        return output
    
    def delete_collection(self, collection: str = "default") -> bool:
        """Delete a collection."""
        try:
            self.client.delete_collection(name=collection)
            logger.info("Deleted ChromaDB collection: %s", collection)
            return True
        except Exception as e:
            logger.error("Failed to delete collection %s: %s", collection, e)
            return False
    
    def count(self, collection: str = "default") -> int:
        """Get document count in collection."""
        try:
            coll = self._get_collection(collection)
            return coll.count()
        except Exception:
            return 0


class JSONVectorStore(VectorStore):
    """JSON file-based vector store (fallback when Chroma not available)."""
    
    def __init__(self, data_dir: Optional[Path] = None):
        _ensure_dirs()
        self.data_dir = data_dir or VECTOR_DB_DIR / "json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.embed_fn, self.embed_type = _get_embedding_fn()
        logger.info("JSON VectorStore initialized with %s embeddings", self.embed_type)
    
    def _collection_path(self, collection: str) -> Path:
        return self.data_dir / f"{collection}.json"
    
    def _load_collection(self, collection: str) -> List[Dict[str, Any]]:
        path = self._collection_path(collection)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    
    def _save_collection(self, collection: str, data: List[Dict[str, Any]]):
        path = self._collection_path(collection)
        path.write_text(json.dumps(data, indent=2))
    
    def add_documents(self, docs: List[Dict[str, Any]], collection: str = "default") -> int:
        if not docs:
            return 0
        
        existing = self._load_collection(collection)
        existing_ids = {d.get("id") for d in existing}
        
        new_docs = []
        for doc in docs:
            doc_id = doc.get("id") or doc.get("path") or hashlib.md5(str(doc).encode()).hexdigest()
            text = doc.get("text") or doc.get("content") or doc.get("summary") or str(doc)
            
            embedding = self.embed_fn([text[:10000]])[0]
            
            new_doc = {
                "id": str(doc_id),
                "text": text[:10000],
                "embedding": embedding,
                "metadata": {k: str(v)[:500] for k, v in doc.items() if k not in ("embedding", "text", "content")},
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
            
            # Remove existing doc with same ID
            existing = [d for d in existing if d.get("id") != doc_id]
            new_docs.append(new_doc)
        
        existing.extend(new_docs)
        self._save_collection(collection, existing)
        return len(new_docs)
    
    def query(self, query_text: str, collection: str = "default", top_k: int = 5) -> List[Dict[str, Any]]:
        data = self._load_collection(collection)
        if not data:
            return []
        
        query_embedding = self.embed_fn([query_text])[0]
        
        # Compute similarities
        scored = []
        for doc in data:
            emb = doc.get("embedding")
            if emb:
                sim = cosine_similarity(query_embedding, emb)
                scored.append((sim, doc))
        
        # Sort by similarity descending
        scored.sort(key=lambda x: x[0], reverse=True)
        
        output = []
        for sim, doc in scored[:top_k]:
            output.append({
                "id": doc.get("id"),
                "document": doc.get("text"),
                "metadata": doc.get("metadata", {}),
                "similarity": sim,
            })
        
        return output
    
    def get_all(self, collection: str = "default") -> List[Dict[str, Any]]:
        data = self._load_collection(collection)
        return [
            {
                "id": d.get("id"),
                "document": d.get("text"),
                "metadata": d.get("metadata", {}),
            }
            for d in data
        ]
    
    def delete_collection(self, collection: str = "default") -> bool:
        """Delete a collection."""
        try:
            path = self._collection_path(collection)
            if path.exists():
                path.unlink()
                logger.info("Deleted JSON collection: %s", collection)
            return True
        except Exception as e:
            logger.error("Failed to delete collection %s: %s", collection, e)
            return False
    
    def count(self, collection: str = "default") -> int:
        """Get document count in collection."""
        return len(self._load_collection(collection))


def get_vector_store(prefer_milvus: bool = True, prefer_chroma: bool = True) -> VectorStore:
    """Get a vector store instance based on configuration.
    
    Respects config.yaml vector_db.mode setting:
    - 'auto': Automatically select best available (Milvus Lite on Linux/Mac, ChromaDB on Windows)
    - 'lite': Milvus Lite embedded (Linux/Mac only)
    - 'server': Milvus Server (requires external Milvus instance)
    - 'chroma': ChromaDB (works everywhere, good for single-server)
    - 'json': JSON file-based (always available, simplest option)
    
    Fallback order when 'auto':
    1. Milvus Lite (if available and not Windows)
    2. ChromaDB (if available)
    3. JSON fallback
    
    Args:
        prefer_milvus: If True and Milvus is available and connected, use Milvus
        prefer_chroma: If True and ChromaDB is available, use ChromaDB
    
    Returns:
        VectorStore instance
    """
    cfg = _get_vector_db_config()
    mode = cfg.get("mode", "auto")
    
    # Explicit JSON mode - skip all other checks
    if mode == "json":
        logger.info("Using JSON VectorStore (explicit config)")
        return JSONVectorStore()
    
    # Explicit ChromaDB mode
    if mode == "chroma":
        if CHROMA_AVAILABLE:
            try:
                logger.info("Using ChromaDB (explicit config)")
                return ChromaVectorStore()
            except Exception as e:
                logger.warning("ChromaDB failed: %s, falling back to JSON", e)
        return JSONVectorStore()
    
    # Explicit Milvus Server mode
    if mode == "server":
        if MILVUS_AVAILABLE:
            try:
                logger.info("Using Milvus Server (explicit config)")
                return MilvusVectorStore(
                    host=cfg.get("milvus_host", "localhost"),
                    port=cfg.get("milvus_port", 19530),
                    mode="server"
                )
            except Exception as e:
                logger.warning("Milvus Server not available: %s, falling back", e)
    
    # Explicit Milvus Lite mode
    if mode == "lite":
        if MILVUS_LITE_AVAILABLE:
            try:
                logger.info("Using Milvus Lite (explicit config)")
                return MilvusVectorStore(mode="lite")
            except Exception as e:
                logger.warning("Milvus Lite not available: %s, falling back", e)
    
    # Auto mode: try best available
    if mode == "auto":
        # Try Milvus Lite first (if not Windows)
        if prefer_milvus and MILVUS_LITE_AVAILABLE:
            try:
                return MilvusVectorStore(mode="lite")
            except Exception as e:
                logger.debug("Milvus Lite not available: %s", e)
        
        # Try Milvus Server (only if explicitly preferred, skip noisy connection attempts)
        # Note: In auto mode, we don't try server by default to avoid connection errors
        
        # Try ChromaDB
        if prefer_chroma and CHROMA_AVAILABLE:
            try:
                return ChromaVectorStore()
            except Exception as e:
                logger.debug("ChromaDB not available: %s", e)
    
    # Fallback to JSON (always available)
    logger.info("Using JSON VectorStore (fallback)")
    return JSONVectorStore()


# Convenience functions
_default_store: Optional[VectorStore] = None


def get_default_store() -> VectorStore:
    global _default_store
    if _default_store is None:
        _default_store = get_vector_store()
    return _default_store


def add_functionality_docs(docs: List[Dict[str, Any]]) -> int:
    """Add functionality documents to the 'functionality' collection."""
    store = get_default_store()
    return store.add_documents(docs, collection="functionality")


def add_developer_skills(skills: List[Dict[str, Any]]) -> int:
    """Add developer skill profiles to the 'skills' collection."""
    store = get_default_store()
    return store.add_documents(skills, collection="skills")


def query_functionality(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Query functionality knowledge."""
    store = get_default_store()
    return store.query(query, collection="functionality", top_k=top_k)


def query_developer_skills(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Query developer skills."""
    store = get_default_store()
    return store.query(query, collection="skills", top_k=top_k)


def get_all_developers() -> List[Dict[str, Any]]:
    """Get all developer skill profiles."""
    store = get_default_store()
    return store.get_all(collection="skills")
