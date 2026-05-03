#!/usr/bin/env python3
"""Local memory and RAG MCP-style server for Eruditus Harness."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html.parser
import importlib.util
import json
import math
import os
import pickle
import re
import sqlite3
import sys
import textwrap
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


SUPPORTED_EXTENSIONS = {".xml", ".pptx", ".docx", ".html", ".htm", ".md", ".markdown"}
DEFAULT_HASH_VECTOR_DIMS = 384
RAG_SCHEMA_VERSION = 3
RRF_K = 60.0


class TextHTMLParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return normalize_text(" ".join(self.parts))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]+|[가-힣]+", text.lower())


def fts_text(text: str) -> str:
    tokens = tokenize(text)
    if not tokens:
        return text
    return " ".join(tokens)


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def hash_embed(text: str, dims: int = DEFAULT_HASH_VECTOR_DIMS) -> list[float]:
    vector = [0.0] * dims
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dims
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        vector[index] += sign

    return normalize_vector(vector)


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def rrf_score(rank: int) -> float:
    return 1.0 / (RRF_K + rank)


class EmbeddingBackend:
    def __init__(self, requested: str = "auto", model: str | None = None) -> None:
        self.requested = requested
        self.model_name = model or os.environ.get("ERUDITUS_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        self.kind = "hash"
        self.dimension = DEFAULT_HASH_VECTOR_DIMS
        self._model: Any | None = None

        if requested in {"auto", "sentence-transformers"} and importlib.util.find_spec("sentence_transformers"):
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            self.dimension = int(self._model.get_sentence_embedding_dimension())
            self.kind = "sentence-transformers"
        elif requested == "sentence-transformers":
            raise RuntimeError("sentence-transformers is not installed")

    def embed(self, text: str) -> list[float]:
        if self.kind == "sentence-transformers":
            vector = self._model.encode([text], normalize_embeddings=True)[0]
            return [float(value) for value in vector]
        return hash_embed(text, self.dimension)

    def fingerprint(self) -> str:
        return f"{self.kind}:{self.model_name if self.kind != 'hash' else self.dimension}"


class VectorIndex:
    def __init__(
        self,
        rag_dir: Path,
        dimension: int,
        backend: str = "auto",
        use_gpu: str = "auto",
        name: str = "index",
    ) -> None:
        self.rag_dir = rag_dir
        self.dimension = dimension
        self.backend_request = backend
        self.use_gpu = use_gpu
        self.faiss_path = rag_dir / f"{name}.faiss"
        self.fallback_path = rag_dir / f"{name}.pkl"
        self.backend = "python"
        self.gpu = False
        self._faiss: Any | None = None
        self._index: Any | None = None
        self._ids: list[int] = []
        self._vectors: list[list[float]] = []
        self._load()

    def _load(self) -> None:
        if self.backend_request in {"auto", "faiss"} and importlib.util.find_spec("faiss"):
            import faiss

            self._faiss = faiss
            self.backend = "faiss"
            if self.faiss_path.exists():
                self._index = faiss.read_index(str(self.faiss_path))
                if int(self._index.d) != self.dimension:
                    self._index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dimension))
            else:
                self._index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dimension))
            self._maybe_move_to_gpu()
            return
        if self.backend_request == "faiss":
            raise RuntimeError("faiss is not installed")

        self.backend = "python"
        if self.fallback_path.exists():
            with self.fallback_path.open("rb") as file:
                payload = pickle.load(file)
            if payload.get("dimension") == self.dimension:
                self._ids = payload.get("ids", [])
                self._vectors = payload.get("vectors", [])

    def _maybe_move_to_gpu(self) -> None:
        if self.use_gpu == "never" or self._faiss is None:
            return
        if not hasattr(self._faiss, "get_num_gpus") or self._faiss.get_num_gpus() <= 0:
            return
        try:
            resources = self._faiss.StandardGpuResources()
            self._index = self._faiss.index_cpu_to_gpu(resources, 0, self._index)
            self.gpu = True
        except Exception:
            self.gpu = False

    def reset(self) -> None:
        if self.backend == "faiss":
            self._index = self._faiss.IndexIDMap(self._faiss.IndexFlatIP(self.dimension))
            self._maybe_move_to_gpu()
        else:
            self._ids = []
            self._vectors = []

    def add(self, ids: list[int], vectors: list[list[float]]) -> None:
        if not ids:
            return
        if self.backend == "faiss":
            numpy = __import__("numpy")
            id_array = numpy.array(ids, dtype="int64")
            vector_array = numpy.array(vectors, dtype="float32")
            self._index.add_with_ids(vector_array, id_array)
        else:
            self._ids.extend(ids)
            self._vectors.extend(vectors)

    def remove(self, ids: list[int]) -> None:
        if not ids:
            return
        if self.backend == "faiss":
            numpy = __import__("numpy")
            self._index.remove_ids(numpy.array(ids, dtype="int64"))
            return

        remove_ids = set(ids)
        kept = [(chunk_id, vector) for chunk_id, vector in zip(self._ids, self._vectors) if chunk_id not in remove_ids]
        self._ids = [chunk_id for chunk_id, _ in kept]
        self._vectors = [vector for _, vector in kept]

    def search(self, vector: list[float], limit: int) -> list[tuple[int, float]]:
        if limit <= 0:
            return []
        if self.backend == "faiss":
            numpy = __import__("numpy")
            query = numpy.array([vector], dtype="float32")
            scores, ids = self._index.search(query, limit)
            return [(int(chunk_id), float(score)) for chunk_id, score in zip(ids[0], scores[0]) if int(chunk_id) >= 0]

        scored = [(chunk_id, cosine(vector, candidate)) for chunk_id, candidate in zip(self._ids, self._vectors)]
        return sorted(scored, key=lambda row: row[1], reverse=True)[:limit]

    def save(self) -> None:
        if self.backend == "faiss":
            index = self._index
            if self.gpu:
                index = self._faiss.index_gpu_to_cpu(index)
            self._faiss.write_index(index, str(self.faiss_path))
            return
        with self.fallback_path.open("wb") as file:
            pickle.dump({"dimension": self.dimension, "ids": self._ids, "vectors": self._vectors}, file)

    def status(self) -> dict[str, Any]:
        if self.backend == "faiss":
            size = int(self._index.ntotal)
        else:
            size = len(self._ids)
        return {"backend": self.backend, "gpu": self.gpu, "dimension": self.dimension, "vectors": size}


def chunk_text(text: str, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if chunk_size <= overlap:
        raise ValueError("chunk_size must be greater than overlap")

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = end - overlap
    return [chunk for chunk in chunks if chunk]


def infer_heading(chunk: str) -> str | None:
    for line in chunk.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
        if line:
            return textwrap.shorten(line, width=100)
    return None


def extract_xml_text(content: bytes) -> str:
    root = ElementTree.fromstring(content)
    return normalize_text(" ".join(part.strip() for part in root.itertext() if part.strip()))


def extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name == "word/document.xml" or name.startswith("word/header") or name.startswith("word/footer")
        ]
        parts = [extract_xml_text(archive.read(name)) for name in names]
    return normalize_text(" ".join(parts))


def extract_pptx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = sorted(name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
        parts = [extract_xml_text(archive.read(name)) for name in names]
    return normalize_text(" ".join(parts))


def extract_text(path: Path) -> str:
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported file extension: {path.suffix}")
    if extension in {".md", ".markdown"}:
        return normalize_text(path.read_text(encoding="utf-8", errors="ignore"))
    if extension in {".html", ".htm"}:
        parser = TextHTMLParser()
        parser.feed(path.read_text(encoding="utf-8", errors="ignore"))
        return parser.text()
    if extension == ".xml":
        return extract_xml_text(path.read_bytes())
    if extension == ".docx":
        return extract_docx_text(path)
    if extension == ".pptx":
        return extract_pptx_text(path)
    raise ValueError(f"unsupported file extension: {path.suffix}")


def slugify(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9가-힣._-]+", "-", title.strip()).strip("-").lower()
    return slug or "memory"


class HarnessStore:
    def __init__(
        self,
        codex_home: Path,
        embedding_backend: str = "auto",
        embedding_model: str | None = None,
        vector_backend: str = "auto",
        use_gpu: str = "auto",
    ) -> None:
        self.codex_home = codex_home
        self.memory_dir = codex_home / "memory"
        self.rag_dir = codex_home / "rag"
        self.db_path = self.rag_dir / "metadata.sqlite3"
        self.manifest_path = self.rag_dir / "manifest.json"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.rag_dir.mkdir(parents=True, exist_ok=True)
        self.embedding = EmbeddingBackend(embedding_backend, embedding_model)
        self.vector_index = VectorIndex(self.rag_dir, self.embedding.dimension, vector_backend, use_gpu, "index")
        self.memory_vector_index = VectorIndex(
            self.rag_dir,
            self.embedding.dimension,
            vector_backend,
            use_gpu,
            "memory_index",
        )
        self.index_errors: list[dict[str, str]] = []
        self._init_db()
        self._write_manifest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            self._migrate_schema(connection)

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > RAG_SCHEMA_VERSION:
            raise RuntimeError(f"metadata schema version {version} is newer than supported {RAG_SCHEMA_VERSION}")

        if version == 0:
            self._create_schema(connection)
            self._migrate_legacy_chunks(connection)
            self._rebuild_fts_for_migration(connection)
            self._rebuild_memory_fts_for_migration(connection)
            connection.execute(f"PRAGMA user_version = {RAG_SCHEMA_VERSION}")
            return

        if version < RAG_SCHEMA_VERSION:
            self._create_schema(connection)
            if version < 2:
                self._migrate_legacy_chunks(connection)
            if version < 3:
                self._rebuild_fts_for_migration(connection)
                self._rebuild_memory_fts_for_migration(connection)
            connection.execute(f"PRAGMA user_version = {RAG_SCHEMA_VERSION}")

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                heading TEXT,
                text TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, chunk_index)
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(text, path UNINDEXED, chunk_id UNINDEXED)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
            USING fts5(title, body, path UNINDEXED, note_id UNINDEXED)
            """
        )

    def _migrate_legacy_chunks(self, connection: sqlite3.Connection) -> None:
        existing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(chunks)").fetchall()}
        if "vector_json" not in existing_columns:
            if existing_columns and "heading" not in existing_columns:
                connection.execute("ALTER TABLE chunks ADD COLUMN heading TEXT")
            return

        connection.execute("ALTER TABLE chunks RENAME TO chunks_legacy")
        connection.execute(
            """
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                heading TEXT,
                text TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, chunk_index)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO chunks(id, document_id, chunk_index, heading, text)
            SELECT id, document_id, chunk_index, NULL, text
            FROM chunks_legacy
            """
        )
        connection.execute("DROP TABLE chunks_legacy")

    def _rebuild_fts_for_migration(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM chunks_fts")
        rows = connection.execute(
            """
            SELECT chunks.id, chunks.text, documents.path
            FROM chunks
            JOIN documents ON documents.id = chunks.document_id
            """
        ).fetchall()
        for row in rows:
            connection.execute(
                "INSERT INTO chunks_fts(rowid, text, path, chunk_id) VALUES (?, ?, ?, ?)",
                (row["id"], fts_text(row["text"]), row["path"], row["id"]),
            )

    def _rebuild_memory_fts_for_migration(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM memory_fts")
        rows = connection.execute("SELECT id, path, title FROM memory_notes").fetchall()
        for row in rows:
            path = Path(row["path"])
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            connection.execute(
                "INSERT INTO memory_fts(rowid, title, body, path, note_id) VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["title"], fts_text(text), row["path"], row["id"]),
            )

    def _write_manifest(self) -> None:
        manifest = {
            "schema_version": RAG_SCHEMA_VERSION,
            "embedding": self.embedding.fingerprint(),
            "vector_index": self.vector_index.status(),
            "memory_vector_index": self.memory_vector_index.status(),
            "index_errors": self.index_errors,
            "metadata": str(self.db_path),
        }
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _record_index_error(self, operation: str, exc: Exception) -> None:
        self.index_errors.append({"operation": operation, "error": str(exc)})
        self._write_manifest()

    def memory_add(self, title: str, body: str, tags: list[str] | None = None) -> dict[str, Any]:
        today = dt.datetime.now().strftime("%Y%m%d")
        base = f"{today}-{slugify(title)}"
        path = self.memory_dir / f"{base}.md"
        suffix = 2
        while path.exists():
            path = self.memory_dir / f"{base}-{suffix}.md"
            suffix += 1

        tag_line = ""
        if tags:
            tag_line = "tags: " + ", ".join(tag.strip() for tag in tags if tag.strip()) + "\n"
        content = f"# {title.strip()}\n\n{tag_line}created_at: {dt.datetime.now().isoformat(timespec='seconds')}\n\n{body.strip()}\n"
        path.write_text(content, encoding="utf-8")
        self._index_memory_note(path)
        return {"path": str(path), "title": title}

    def memory_search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_vector = self.embedding.embed(query)
        vector_hits = [
            (note_id, score)
            for note_id, score in self.memory_vector_index.search(query_vector, max(limit * 8, 20))
            if score > 0
        ]
        keyword_hits = self._memory_keyword_search(query, max(limit * 8, 20))

        scores: dict[int, dict[str, float]] = {}
        for rank, (note_id, score) in enumerate(vector_hits, start=1):
            scores.setdefault(
                note_id,
                {"rrf": 0.0, "vector": 0.0, "keyword": 0.0, "vector_rank": 0.0, "keyword_rank": 0.0},
            )
            scores[note_id]["rrf"] += rrf_score(rank)
            scores[note_id]["vector"] = max(scores[note_id]["vector"], score)
            scores[note_id]["vector_rank"] = float(rank)
        for rank, (note_id, score) in enumerate(keyword_hits, start=1):
            scores.setdefault(
                note_id,
                {"rrf": 0.0, "vector": 0.0, "keyword": 0.0, "vector_rank": 0.0, "keyword_rank": 0.0},
            )
            scores[note_id]["rrf"] += rrf_score(rank)
            scores[note_id]["keyword"] = max(scores[note_id]["keyword"], score)
            scores[note_id]["keyword_rank"] = float(rank)

        ranked = sorted(
            ((note_id, parts["rrf"], parts) for note_id, parts in scores.items()),
            key=lambda row: row[1],
            reverse=True,
        )[:limit]
        rows = self._memory_rows([note_id for note_id, _, _ in ranked])

        results: list[dict[str, Any]] = []
        for note_id, score, parts in ranked:
            row = rows.get(note_id)
            if not row:
                continue
            text = Path(row["path"]).read_text(encoding="utf-8", errors="ignore")
            results.append(
                {
                    "path": row["path"],
                    "title": row["title"],
                    "score": score,
                    "vector_score": parts["vector"],
                    "keyword_score": parts["keyword"],
                    "vector_rank": int(parts["vector_rank"]) if parts["vector_rank"] else None,
                    "keyword_rank": int(parts["keyword_rank"]) if parts["keyword_rank"] else None,
                    "preview": textwrap.shorten(text, width=360),
                }
            )
        return results

    def memory_sync(self) -> dict[str, Any]:
        indexed_paths: dict[str, sqlite3.Row]
        with self._connect() as connection:
            rows = connection.execute("SELECT id, path, mtime_ns, sha256 FROM memory_notes").fetchall()
            indexed_paths = {row["path"]: row for row in rows}

        indexed = 0
        removed = 0
        seen: set[str] = set()
        for path in sorted(self.memory_dir.glob("*.md")):
            resolved = str(path.resolve())
            seen.add(resolved)
            stat = path.stat()
            existing = indexed_paths.get(resolved)
            if existing and existing["mtime_ns"] == stat.st_mtime_ns:
                continue

            content = path.read_bytes()
            content_hash = hashlib.sha256(content).hexdigest()
            if existing and existing["sha256"] == content_hash:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE memory_notes SET mtime_ns = ?, updated_at = ? WHERE id = ?",
                        (stat.st_mtime_ns, dt.datetime.now().isoformat(timespec="seconds"), existing["id"]),
                    )
                continue

            self._index_memory_note(path, content)
            indexed += 1

        stale_paths = set(indexed_paths) - seen
        if stale_paths:
            with self._connect() as connection:
                for stale_path in stale_paths:
                    note_id = int(indexed_paths[stale_path]["id"])
                    connection.execute("DELETE FROM memory_fts WHERE rowid = ?", (note_id,))
                    connection.execute("DELETE FROM memory_notes WHERE id = ?", (note_id,))
                    try:
                        self.memory_vector_index.remove([note_id])
                    except Exception as exc:
                        self._record_index_error("memory_remove", exc)
                    removed += 1
            try:
                self.memory_vector_index.save()
                self._write_manifest()
            except Exception as exc:
                self._record_index_error("memory_save", exc)

        return {"indexed": indexed, "removed": removed}

    def _sync_memory_index(self) -> None:
        self.memory_sync()

    def _index_memory_note(self, path: Path, content: bytes | None = None) -> None:
        path = path.resolve()
        if content is None:
            content = path.read_bytes()
        text = content.decode("utf-8", errors="ignore")
        stat = path.stat()
        content_hash = hashlib.sha256(content).hexdigest()
        title = self._memory_title(text, path)
        updated_at = dt.datetime.now().isoformat(timespec="seconds")

        with self._connect() as connection:
            existing = connection.execute("SELECT id FROM memory_notes WHERE path = ?", (str(path),)).fetchone()
            if existing:
                note_id = int(existing["id"])
                connection.execute("DELETE FROM memory_fts WHERE rowid = ?", (note_id,))
                try:
                    self.memory_vector_index.remove([note_id])
                except Exception as exc:
                    self._record_index_error("memory_remove", exc)
                connection.execute(
                    "UPDATE memory_notes SET mtime_ns = ?, sha256 = ?, title = ?, updated_at = ? WHERE id = ?",
                    (stat.st_mtime_ns, content_hash, title, updated_at, note_id),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO memory_notes(path, mtime_ns, sha256, title, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (str(path), stat.st_mtime_ns, content_hash, title, updated_at),
                )
                note_id = int(cursor.lastrowid)

            connection.execute(
                "INSERT INTO memory_fts(rowid, title, body, path, note_id) VALUES (?, ?, ?, ?, ?)",
                (note_id, title, fts_text(text), str(path), note_id),
            )
        try:
            self.memory_vector_index.add([note_id], [self.embedding.embed(text)])
            self.memory_vector_index.save()
            self._write_manifest()
        except Exception as exc:
            self._record_index_error("memory_add", exc)

    def _memory_keyword_search(self, query: str, limit: int) -> list[tuple[int, float]]:
        terms = tokenize(query)
        if not terms:
            return []
        fts_query = " OR ".join(terms)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT note_id, bm25(memory_fts) AS score
                    FROM memory_fts
                    WHERE memory_fts MATCH ?
                    ORDER BY score
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(row["note_id"]), 1.0 / (1.0 + abs(float(row["score"])))) for row in rows]

    def _memory_rows(self, note_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not note_ids:
            return {}
        placeholders = ",".join("?" for _ in note_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, path, title
                FROM memory_notes
                WHERE id IN ({placeholders})
                """,
                note_ids,
            ).fetchall()
        return {int(row["id"]): row for row in rows}

    def _memory_title(self, text: str, path: Path) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or path.stem
        return path.stem

    def rag_ingest(self, paths: list[str], chunk_size: int = 900, overlap: int = 120) -> dict[str, Any]:
        ingested: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        needs_save = False

        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            try:
                files = [candidate for candidate in path.rglob("*") if candidate.suffix.lower() in SUPPORTED_EXTENSIONS] if path.is_dir() else [path]
                for file_path in files:
                    result = self._ingest_file(file_path, chunk_size, overlap)
                    ingested.append(result)
                    needs_save = needs_save or result["status"] == "indexed"
            except Exception as exc:
                skipped.append({"path": str(path), "reason": str(exc)})

        if needs_save:
            try:
                self.vector_index.save()
                self._write_manifest()
            except Exception as exc:
                self._record_index_error("rag_save", exc)
        return {"ingested": ingested, "skipped": skipped}

    def _ingest_file(self, path: Path, chunk_size: int, overlap: int) -> dict[str, Any]:
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"unsupported file extension: {path.suffix}")
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        stat = path.stat()
        text = extract_text(path)
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        old_chunk_ids: list[int] = []
        new_ids: list[int] = []
        new_vectors: list[list[float]] = []

        with self._connect() as connection:
            existing = connection.execute("SELECT id, sha256 FROM documents WHERE path = ?", (str(path),)).fetchone()
            if existing and existing["sha256"] == content_hash:
                return {"path": str(path), "chunks": len(chunks), "status": "unchanged"}

            if existing:
                document_id = existing["id"]
                old_chunk_ids = [
                    int(row["id"])
                    for row in connection.execute("SELECT id FROM chunks WHERE document_id = ?", (document_id,)).fetchall()
                ]
                if old_chunk_ids:
                    placeholders = ",".join("?" for _ in old_chunk_ids)
                    connection.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", old_chunk_ids)
                connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
                connection.execute(
                    "UPDATE documents SET mtime_ns = ?, sha256 = ?, ingested_at = ? WHERE id = ?",
                    (stat.st_mtime_ns, content_hash, dt.datetime.now().isoformat(timespec="seconds"), document_id),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO documents(path, mtime_ns, sha256, ingested_at) VALUES (?, ?, ?, ?)",
                    (str(path), stat.st_mtime_ns, content_hash, dt.datetime.now().isoformat(timespec="seconds")),
                )
                document_id = cursor.lastrowid

            for index, chunk in enumerate(chunks):
                cursor = connection.execute(
                    "INSERT INTO chunks(document_id, chunk_index, heading, text) VALUES (?, ?, ?, ?)",
                    (document_id, index, infer_heading(chunk), chunk),
                )
                chunk_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO chunks_fts(rowid, text, path, chunk_id) VALUES (?, ?, ?, ?)",
                    (chunk_id, fts_text(chunk), str(path), chunk_id),
                )
                new_ids.append(chunk_id)
                new_vectors.append(self.embedding.embed(chunk))

        try:
            self.vector_index.remove(old_chunk_ids)
            self.vector_index.add(new_ids, new_vectors)
        except Exception as exc:
            self._record_index_error("rag_update", exc)
        return {"path": str(path), "chunks": len(chunks), "status": "indexed"}

    def _rebuild_vector_index(self) -> None:
        """Repair path: rebuild the RAG vector index from SQLite metadata."""
        self.vector_index.reset()
        ids: list[int] = []
        vectors: list[list[float]] = []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, text
                FROM chunks
                ORDER BY id
                """
            ).fetchall()
        for row in rows:
            ids.append(int(row["id"]))
            vectors.append(self.embedding.embed(row["text"]))
        self.vector_index.add(ids, vectors)
        self.vector_index.save()
        self._write_manifest()

    def _rebuild_memory_vector_index(self) -> None:
        """Repair path: rebuild the memory vector index from memory note files."""
        self.memory_vector_index.reset()
        ids: list[int] = []
        vectors: list[list[float]] = []
        with self._connect() as connection:
            rows = connection.execute("SELECT id, path FROM memory_notes ORDER BY id").fetchall()
        for row in rows:
            path = Path(row["path"])
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            ids.append(int(row["id"]))
            vectors.append(self.embedding.embed(text))
        self.memory_vector_index.add(ids, vectors)
        self.memory_vector_index.save()
        self._write_manifest()

    def index_repair(self) -> dict[str, Any]:
        self.index_errors = []
        self._rebuild_vector_index()
        self._rebuild_memory_vector_index()
        return self.rag_status()

    def _chunk_rows(self, chunk_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chunks.id, documents.path, chunks.chunk_index, chunks.heading, chunks.text
                FROM chunks
                JOIN documents ON documents.id = chunks.document_id
                WHERE chunks.id IN ({placeholders})
                """,
                chunk_ids,
            ).fetchall()
        return {int(row["id"]): row for row in rows}

    def _keyword_search(self, query: str, limit: int) -> list[tuple[int, float]]:
        terms = tokenize(query)
        if not terms:
            return []
        fts_query = " OR ".join(terms)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT chunk_id, bm25(chunks_fts) AS score
                    FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                    ORDER BY score
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(row["chunk_id"]), 1.0 / (1.0 + abs(float(row["score"])))) for row in rows]

    def rag_search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_vector = self.embedding.embed(query)
        vector_hits = [
            (chunk_id, score)
            for chunk_id, score in self.vector_index.search(query_vector, max(limit * 8, 20))
            if score > 0
        ]
        keyword_hits = self._keyword_search(query, max(limit * 8, 20))

        scores: dict[int, dict[str, float]] = {}
        for rank, (chunk_id, score) in enumerate(vector_hits, start=1):
            scores.setdefault(
                chunk_id,
                {"rrf": 0.0, "vector": 0.0, "keyword": 0.0, "vector_rank": 0.0, "keyword_rank": 0.0},
            )
            scores[chunk_id]["rrf"] += rrf_score(rank)
            scores[chunk_id]["vector"] = max(scores[chunk_id]["vector"], score)
            scores[chunk_id]["vector_rank"] = float(rank)
        for rank, (chunk_id, score) in enumerate(keyword_hits, start=1):
            scores.setdefault(
                chunk_id,
                {"rrf": 0.0, "vector": 0.0, "keyword": 0.0, "vector_rank": 0.0, "keyword_rank": 0.0},
            )
            scores[chunk_id]["rrf"] += rrf_score(rank)
            scores[chunk_id]["keyword"] = max(scores[chunk_id]["keyword"], score)
            scores[chunk_id]["keyword_rank"] = float(rank)

        ranked = sorted(
            ((chunk_id, parts["rrf"], parts) for chunk_id, parts in scores.items()),
            key=lambda row: row[1],
            reverse=True,
        )[:limit]
        rows = self._chunk_rows([chunk_id for chunk_id, _, _ in ranked])

        results: list[dict[str, Any]] = []
        for chunk_id, score, parts in ranked:
            row = rows.get(chunk_id)
            if not row:
                continue
            results.append(
                {
                    "path": row["path"],
                    "chunk_index": row["chunk_index"],
                    "heading": row["heading"],
                    "score": score,
                    "vector_score": parts["vector"],
                    "keyword_score": parts["keyword"],
                    "vector_rank": int(parts["vector_rank"]) if parts["vector_rank"] else None,
                    "keyword_rank": int(parts["keyword_rank"]) if parts["keyword_rank"] else None,
                    "text": row["text"],
                }
            )
        return results

    def rag_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            document_count = connection.execute("SELECT COUNT(*) AS count FROM documents").fetchone()["count"]
            chunk_count = connection.execute("SELECT COUNT(*) AS count FROM chunks").fetchone()["count"]
            memory_count = connection.execute("SELECT COUNT(*) AS count FROM memory_notes").fetchone()["count"]
        return {
            "codex_home": str(self.codex_home),
            "rag_dir": str(self.rag_dir),
            "documents": document_count,
            "chunks": chunk_count,
            "memory_notes": memory_count,
            "embedding": {"backend": self.embedding.kind, "dimension": self.embedding.dimension},
            "vector_index": self.vector_index.status(),
            "memory_vector_index": self.memory_vector_index.status(),
            "index_errors": self.index_errors,
        }


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "memory_add",
            "description": "Store project know-how as a Markdown note under .codex/memory.",
            "inputSchema": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
                "required": ["title", "body"],
            },
        },
        {
            "name": "memory_search",
            "description": "Search saved know-how notes with vector and keyword retrieval.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
                "required": ["query"],
            },
        },
        {
            "name": "memory_sync",
            "description": "Synchronize manually edited .codex/memory Markdown notes into the memory indexes.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "rag_ingest",
            "description": "Index read-only reference documents into the local RAG vector database.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "chunk_size": {"type": "integer", "default": 900},
                    "overlap": {"type": "integer", "default": 120},
                },
                "required": ["paths"],
            },
        },
        {
            "name": "rag_search",
            "description": "Search indexed reference documents and return source paths with matching chunks.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
                "required": ["query"],
            },
        },
        {
            "name": "rag_status",
            "description": "Show active RAG embedding, vector index, GPU, and index statistics.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "index_repair",
            "description": "Rebuild RAG and memory vector indexes from SQLite metadata and memory files.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def call_tool(store: HarnessStore, name: str, arguments: dict[str, Any]) -> Any:
    if name == "memory_add":
        return store.memory_add(arguments["title"], arguments["body"], arguments.get("tags"))
    if name == "memory_search":
        return store.memory_search(arguments["query"], int(arguments.get("limit", 5)))
    if name == "memory_sync":
        return store.memory_sync()
    if name == "rag_ingest":
        return store.rag_ingest(arguments["paths"], int(arguments.get("chunk_size", 900)), int(arguments.get("overlap", 120)))
    if name == "rag_search":
        return store.rag_search(arguments["query"], int(arguments.get("limit", 5)))
    if name == "rag_status":
        return store.rag_status()
    if name == "index_repair":
        return store.index_repair()
    raise ValueError(f"unknown tool: {name}")


def success(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def failure(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle_message(store: HarnessStore, message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}
    try:
        if method == "initialize":
            return success(
                message_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "eruditus-harness", "version": "0.1.0"},
                },
            )
        if method == "tools/list":
            return success(message_id, {"tools": tool_definitions()})
        if method == "tools/call":
            result = call_tool(store, params["name"], params.get("arguments") or {})
            return success(message_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]})
        if message_id is None:
            return None
        return failure(message_id, -32601, f"method not found: {method}")
    except Exception as exc:
        return failure(message_id, -32000, str(exc))


def run_server(store: HarnessStore) -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = handle_message(store, json.loads(line))
        except Exception as exc:
            response = failure(None, -32700, str(exc))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


def run_cli(store: HarnessStore, args: argparse.Namespace) -> None:
    if args.command == "tools":
        print(json.dumps(tool_definitions(), ensure_ascii=False, indent=2))
    elif args.command == "memory-add":
        print(json.dumps(store.memory_add(args.title, args.body, args.tag), ensure_ascii=False, indent=2))
    elif args.command == "memory-search":
        print(json.dumps(store.memory_search(args.query, args.limit), ensure_ascii=False, indent=2))
    elif args.command == "memory-sync":
        print(json.dumps(store.memory_sync(), ensure_ascii=False, indent=2))
    elif args.command == "rag-ingest":
        print(json.dumps(store.rag_ingest(args.paths, args.chunk_size, args.overlap), ensure_ascii=False, indent=2))
    elif args.command == "rag-search":
        print(json.dumps(store.rag_search(args.query, args.limit), ensure_ascii=False, indent=2))
    elif args.command == "rag-status":
        print(json.dumps(store.rag_status(), ensure_ascii=False, indent=2))
    elif args.command == "index-repair":
        print(json.dumps(store.index_repair(), ensure_ascii=False, indent=2))
    else:
        run_server(store)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Eruditus Harness memory and RAG server")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", ".codex"))
    parser.add_argument(
        "--embedding-backend",
        choices=["auto", "hash", "sentence-transformers"],
        default=os.environ.get("ERUDITUS_EMBEDDING_BACKEND", "auto"),
    )
    parser.add_argument("--embedding-model", default=os.environ.get("ERUDITUS_EMBEDDING_MODEL"))
    parser.add_argument(
        "--vector-backend",
        choices=["auto", "faiss", "python"],
        default=os.environ.get("ERUDITUS_VECTOR_BACKEND", "auto"),
    )
    parser.add_argument(
        "--gpu",
        choices=["auto", "never"],
        default=os.environ.get("ERUDITUS_GPU", "auto"),
        help="Use GPU for FAISS when available. Stored index is always CPU-compatible.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve")
    subparsers.add_parser("tools")
    subparsers.add_parser("rag-status")
    subparsers.add_parser("memory-sync")
    subparsers.add_parser("index-repair")

    memory_add = subparsers.add_parser("memory-add")
    memory_add.add_argument("--title", required=True)
    memory_add.add_argument("--body", required=True)
    memory_add.add_argument("--tag", action="append", default=[])

    memory_search = subparsers.add_parser("memory-search")
    memory_search.add_argument("query")
    memory_search.add_argument("--limit", type=int, default=5)

    rag_ingest = subparsers.add_parser("rag-ingest")
    rag_ingest.add_argument("paths", nargs="+")
    rag_ingest.add_argument("--chunk-size", type=int, default=900)
    rag_ingest.add_argument("--overlap", type=int, default=120)

    rag_search = subparsers.add_parser("rag-search")
    rag_search.add_argument("query")
    rag_search.add_argument("--limit", type=int, default=5)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = HarnessStore(
        Path(args.codex_home),
        embedding_backend=args.embedding_backend,
        embedding_model=args.embedding_model,
        vector_backend=args.vector_backend,
        use_gpu=args.gpu,
    )
    run_cli(store, args)


if __name__ == "__main__":
    main()
