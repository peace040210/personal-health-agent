"""Local evidence corpus with SQLite FTS5 and optional dense retrieval."""

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .evidence_sources import EvidenceDocument, _https


LOCAL_KNOWLEDGE = "本地知识库"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_sources (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_passages (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, text TEXT NOT NULL, embedding BLOB,
    FOREIGN KEY (source_id) REFERENCES kb_sources(id)
);
CREATE INDEX IF NOT EXISTS kb_passages_source ON kb_passages(source_id);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_terms USING fts5(tokens);
"""
_WORDS = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")


def _tokens(text: str) -> list[str]:
    tokens = []
    for value in _WORDS.findall(text.lower()):
        if value.isascii() or len(value) == 1:
            tokens.append(value)
        else:
            tokens.extend(value[i:i + 2] for i in range(len(value) - 1))
    return tokens


def _passages(text: str) -> tuple[str, ...]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + 650, len(text))
        if end < len(text):
            boundary = max(text.rfind(mark, start + 380, end) for mark in ("。", "！", "？", "\n", ". "))
            if boundary >= 0:
                end = boundary + 1
        part = text[start:end].strip()
        if part:
            chunks.append(part)
        if end == len(text):
            break
        start = end - 65
    return tuple(chunks)


def _vector(values: object) -> list[float]:
    if not isinstance(values, list) or not values or any(type(v) not in (int, float) for v in values):
        raise ValueError("Embedding service returned an invalid vector.")
    try:
        size = math.sqrt(sum(value * value for value in values))
    except (OverflowError, ValueError) as error:
        raise ValueError("Embedding service returned invalid values.") from error
    if not size or not math.isfinite(size):
        raise ValueError("Embedding service returned invalid values.")
    return [value / size for value in values]


class OllamaEmbeddings:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}))

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        host = os.getenv("OLLAMA_HOST") or "127.0.0.1:11434"
        address = urlsplit(host if "://" in host else f"http://{host}")
        if (address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}
                or address.username or address.password or address.path not in {"", "/"} or address.query or address.fragment):
            raise ValueError("Ollama embeddings require a loopback OLLAMA_HOST.")
        body = {"model": model, "input": texts}
        request = Request(f"{address.scheme}://{address.netloc}/api/embed", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=20) as response:
                vectors = json.load(response)["embeddings"]
        except (URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as error:
            raise ValueError(f"Ollama embedding model is unavailable: {model}") from error
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Embedding service returned an unexpected batch size.")
        vectors = [_vector(vector) for vector in vectors]
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError("Embedding service returned inconsistent vector dimensions.")
        return vectors


@dataclass(frozen=True)
class KnowledgeResult:
    documents: tuple[EvidenceDocument, ...]
    mode: str


class KnowledgeBase:
    def __init__(self, path: Path, embeddings: OllamaEmbeddings | None = None):
        self.path = path.resolve()
        self.embeddings = embeddings or OllamaEmbeddings()

    def import_jsonl(self, filename: Path, *, embedding_model: str | None = None) -> tuple[int, int]:
        rows = self._read_sources(filename)
        with closing(sqlite3.connect(self.path, timeout=30)) as database:
            database.executescript(_SCHEMA)
            database.execute("BEGIN IMMEDIATE")
            setting = database.execute("SELECT value FROM kb_settings WHERE name = 'embedding_model'").fetchone()
            previous_model = setting[0] if setting else ""
            model = embedding_model if embedding_model is not None else previous_model
            if setting and model != previous_model:
                raise ValueError("Use the existing index's embedding model or create a new index.")
            updates = []
            for key, title, url, text in rows:
                fingerprint = hashlib.sha256(json.dumps([title, url, text], ensure_ascii=False).encode()).hexdigest()
                existing = database.execute("SELECT fingerprint FROM kb_sources WHERE id = ?", (key,)).fetchone()
                if existing is None or existing[0] != fingerprint:
                    updates.append((key, title, url, fingerprint, _passages(text)))
            texts = [part for _, _, _, _, parts in updates for part in parts]
            vectors = self._embed_passages(model, texts)
            if vectors:
                width = len(vectors[0])
                stored = database.execute("SELECT length(embedding) FROM kb_passages WHERE embedding IS NOT NULL LIMIT 1").fetchone()
                if (stored and stored[0] != 4 * width) or any(len(v) != width for v in vectors):
                    raise ValueError("Embedding dimensions changed. Use a new index for this model.")
            packed = iter(struct.pack(f"<{len(v)}f", *v) for v in vectors)
            with database:
                database.execute("INSERT OR IGNORE INTO kb_settings VALUES ('embedding_model', ?)", (model,))
                for key, title, url, fingerprint, parts in updates:
                    database.execute("""DELETE FROM kb_terms WHERE rowid IN
                                     (SELECT id FROM kb_passages WHERE source_id = ?)""", (key,))
                    database.execute("DELETE FROM kb_passages WHERE source_id = ?", (key,))
                    database.execute("""INSERT INTO kb_sources VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                                     title = excluded.title, url = excluded.url, fingerprint = excluded.fingerprint""",
                                     (key, title, url, fingerprint))
                    for part in parts:
                        key_row = database.execute("INSERT INTO kb_passages(source_id, text, embedding) VALUES (?, ?, ?)",
                                                   (key, part, next(packed, None))).lastrowid
                        database.execute("INSERT INTO kb_terms(rowid, tokens) VALUES (?, ?)",
                                         (key_row, " ".join(_tokens(title + " " + part))))
        return len(updates), len(texts)

    def _embed_passages(self, model: str, texts: list[str]) -> list[list[float]]:
        if not model:
            return []
        return [vector for position in range(0, len(texts), 32)
                for vector in self.embeddings.embed(model, texts[position:position + 32])]

    def _read_sources(self, filename: Path) -> list[tuple[str, str, str, str]]:
        rows = []
        seen = set()
        with filename.open(encoding="utf-8-sig") as file:
            for number, line in enumerate(file, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError("每行需要一个 JSON 对象")
                    title, body = item.get("title"), item.get("text")
                    url = _https(item.get("url"))
                    if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip() or not url:
                        raise ValueError("需要 title、text 和有效的 HTTPS 来源 url")
                    key = item.get("id", url)
                    if not isinstance(key, str) or not key.strip() or key in seen:
                        raise ValueError("来源 id 必须非空且在本次导入中唯一")
                except ValueError as error:
                    raise ValueError(f"知识库第 {number} 行无效：{error}") from error
                seen.add(key)
                rows.append((key, title.strip(), url, body.strip()))
        return rows

    def search(self, query: str, *, limit: int = 4) -> KnowledgeResult:
        if limit < 1:
            raise ValueError("The evidence result limit must be positive.")
        tokens = list(dict.fromkeys(_tokens(query)))[:64]
        if not tokens:
            return KnowledgeResult((), "bm25")
        uri = self.path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as database:
            setting = database.execute("SELECT value FROM kb_settings WHERE name = 'embedding_model'").fetchone()
            model = setting[0] if setting else ""
            expression = " OR ".join(f'"{token}"' for token in tokens)
            candidates = limit * 5
            lexical = [rowid for rowid, in database.execute("""
                SELECT rowid FROM kb_terms WHERE kb_terms MATCH ?
                ORDER BY bm25(kb_terms), rowid LIMIT ?
            """, (expression, candidates))]
            dense = []
            mode = "bm25"
            if model:
                try:
                    dense = self._dense(database, model, query, candidates)
                    mode = "hybrid"
                except ValueError:
                    mode = "bm25_fallback"
            ranks: dict[int, float] = {}
            for ranking in (lexical, dense):
                for rank, rowid in enumerate(ranking, 1):
                    ranks[rowid] = ranks.get(rowid, 0.0) + 1 / (60 + rank)
            results = []
            seen = set()
            for rowid in sorted(ranks, key=ranks.__getitem__, reverse=True):
                title, url, passage = database.execute("""
                    SELECT source.title, source.url, passage.text
                    FROM kb_passages AS passage JOIN kb_sources AS source ON source.id = passage.source_id
                    WHERE passage.id = ?
                """, (rowid,)).fetchone()
                if url not in seen:
                    seen.add(url)
                    results.append(EvidenceDocument(LOCAL_KNOWLEDGE, title, url, passage))
                if len(results) == limit:
                    break
        return KnowledgeResult(tuple(results), mode)

    def _dense(self, database: sqlite3.Connection, model: str, query: str, limit: int) -> list[int]:
        query_vector, = self.embeddings.embed(model, [query])
        length = len(query_vector)
        scored = []
        for rowid, blob in database.execute("SELECT id, embedding FROM kb_passages WHERE embedding IS NOT NULL"):
            if len(blob) != 4 * length:
                raise ValueError("Embedding index and query have different dimensions.")
            vector = struct.unpack(f"<{length}f", blob)
            score = sum(left * right for left, right in zip(query_vector, vector))
            if score >= 0.34:
                scored.append((score, rowid))
        return [rowid for _, rowid in sorted(scored, reverse=True)[:limit]]
