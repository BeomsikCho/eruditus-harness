# Harness Engineering

이 프로젝트는 `codex-config/`를 Codex 환경에 복사했을 때 필요한 보조 기능을 함께 제공한다.

## Goals

1. 작업 중 발견한 know-how를 `.codex/memory`에 누적한다.
2. 수정 권한이 없는 참조 문서를 로컬 RAG index로 만든다.
3. Codex가 MCP tool을 통해 memory와 RAG를 검색할 수 있게 한다.

## Storage Layout

```text
.codex/
  memory/
    YYYYMMDD-title.md
  rag/
    metadata.sqlite3
    index.faiss
    index.pkl
    memory_index.faiss
    memory_index.pkl
    manifest.json
```

- `.codex/memory`: 사람이 직접 읽고 수정할 수 있는 Markdown note 저장소.
- `.codex/rag/metadata.sqlite3`: 참조 문서, chunk, FTS keyword index를 저장하는 SQLite DB.
- `.codex/rag/index.faiss`: FAISS가 설치된 경우 사용하는 vector index.
- `.codex/rag/index.pkl`: FAISS가 없는 환경에서 사용하는 pure Python fallback vector index.
- `.codex/rag/memory_index.faiss`: memory note용 FAISS vector index.
- `.codex/rag/memory_index.pkl`: memory note용 pure Python fallback vector index.
- `.codex/rag/manifest.json`: embedding/vector backend와 schema 정보를 기록한다.
- 참조 문서 원본은 수정하지 않는다.

## MCP Tools

`codex-config/mcp/eruditus_harness_server.py`가 제공하는 tool:

- `memory_add`: know-how를 `.codex/memory/*.md`로 저장한다.
- `memory_search`: 저장된 memory note를 vector + keyword RRF로 검색한다.
- `memory_sync`: 사람이 직접 수정한 `.codex/memory/*.md`를 memory index에 반영한다.
- `rag_ingest`: `xml`, `pptx`, `docx`, `html`, `md`, `markdown` 파일을 RAG index에 추가한다.
- `rag_search`: RAG index에서 관련 chunk를 검색한다.
- `rag_status`: 현재 embedding/vector backend, GPU 사용 여부, 문서/chunk 수를 확인한다.
- `index_repair`: SQLite metadata와 memory 파일에서 vector index를 재생성한다.

## RAG Design

RAG는 고성능 경로와 fallback 경로를 분리한다.

- Text extraction
  - `docx`, `pptx`: zip 내부 XML 텍스트 추출
  - `xml`: XML text node 추출
  - `html`: HTML tag 제거 후 텍스트 추출
  - `md`, `markdown`: plain text로 취급
- Chunking
  - 기본 chunk 크기 900자
  - 기본 overlap 120자
- Embedding
  - `sentence-transformers`가 설치되어 있으면 로컬 embedding model 사용
  - 없으면 deterministic hashing vector fallback 사용
- Vector search
  - FAISS가 설치되어 있으면 FAISS 사용
  - FAISS GPU가 설치되고 GPU가 보이면 GPU index 사용
  - 저장 파일은 CPU 호환 `index.faiss`로 기록
  - FAISS가 없으면 pure Python exact search fallback 사용
- Keyword search
  - SQLite FTS5로 keyword 후보를 검색한다.
- Hybrid ranking
  - vector 후보와 keyword 후보를 Reciprocal Rank Fusion으로 합친다.
- Memory search
  - `memory_add`는 Markdown 파일 저장과 동시에 FTS/vector index를 갱신한다.
  - `memory_search`는 검색 시 파일시스템을 전수 스캔하지 않는다.
  - 사람이 직접 `.codex/memory/*.md`를 수정한 경우 `memory_sync`로 반영한다.
  - vector index 업데이트 실패는 `rag_status`의 `index_errors`에 기록하고, `index_repair`로 복구한다.
- Schema migration
  - SQLite `user_version`으로 schema version을 관리한다.
  - migration은 버전별 단계로 수행하고, 필요한 version 변경 시에만 FTS를 재작성한다.

고성능 환경에서는 `uv run --extra perf ...`로 실행한다. CPU만 있는 환경도 `faiss-cpu` 또는 pure Python fallback으로 동작한다.

## Safety Rules

- 참조 문서 원본은 read-only input으로 취급한다.
- ingest는 `.codex/rag/` 아래 metadata/index 파일만 갱신한다.
- memory는 명시적으로 `memory_add`를 호출할 때만 추가한다.
- 검색 결과는 출처 path와 chunk index를 함께 반환한다.
