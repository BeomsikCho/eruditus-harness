# Eruditus-Harness

데스크탑/랩탑처럼 분리된 개발 환경에서 동일한 Codex 설정과 보조 도구를 사용하기 위한 harness repo입니다.

`codex-config/`는 배포할 Codex 설정 원본이고, 이 repo는 그 설정이 실제 환경의 `.codex/`에 들어갔을 때 필요한 memory/RAG 보조 기능을 함께 제공합니다.

## Requirements

- Python 3.11 이상
- 로컬 파일시스템에 `.codex/`를 만들 수 있는 권한

아래 Getting Started 명령은 Python 표준 라이브러리만 사용하는 fallback 경로입니다.
별도 embedding API key나 vector DB 서버가 필요 없습니다.

고성능 검색이 필요하면 optional dependency를 설치해 FAISS와 로컬 embedding model을 사용합니다.

## Getting Started

처음 clone한 사람은 이 순서대로 진행하면 됩니다.

### 1. Codex 설정 복사

repo-local `.codex`를 만들고 Codex 설정 원본을 복사합니다.

```bash
mkdir -p .codex
cp -R codex-config/* .codex/
```

이미 `.codex/AGENTS.md`나 rules 파일이 있다면 무조건 덮어쓰지 말고 필요한 섹션만 병합하세요.

### 2. 사내 문서 RAG 색인

검색하고 싶은 문서 디렉터리를 한 번 색인합니다.

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  rag-ingest /path/to/company-docs
```

지원 확장자:

- `.xml`
- `.pptx`
- `.docx`
- `.html`, `.htm`
- `.md`, `.markdown`

색인 결과는 `.codex/rag/` 아래에 저장됩니다. 원본 문서는 수정하지 않습니다.

### 3. 색인 상태 확인

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  rag-status
```

검색이 되는지 직접 확인합니다.

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  rag-search "검색할 내용" \
  --limit 5
```

### 4. Codex에 MCP 서버 등록

RAG 색인은 setup 단계이고, Codex가 검색하려면 MCP 서버를 Codex 설정에 등록해야 합니다.

Codex CLI TOML 설정 예시:

```toml
[mcp_servers.eruditus-harness]
command = "python3"
args = [
  "/absolute/path/to/eruditus-harness/codex-config/mcp/eruditus_harness_server.py",
  "--codex-home",
  "/absolute/path/to/eruditus-harness/.codex",
  "--embedding-backend",
  "hash",
  "--vector-backend",
  "python",
  "serve"
]
```

다른 MCP client의 JSON 설정 예시:

```json
{
  "mcpServers": {
    "eruditus-harness": {
      "command": "python3",
      "args": [
        "/absolute/path/to/eruditus-harness/codex-config/mcp/eruditus_harness_server.py",
        "--codex-home",
        "/absolute/path/to/eruditus-harness/.codex",
        "--embedding-backend",
        "hash",
        "--vector-backend",
        "python",
        "serve"
      ]
    }
  }
}
```

`serve`를 tmux에서 직접 계속 실행해둘 필요는 없습니다.
stdio 방식 MCP에서는 Codex가 MCP client이고, Codex가 시작될 때 위 command로 서버 프로세스를 실행합니다.
Codex는 그 프로세스의 stdin/stdout으로 `rag_search`, `memory_search`, `memory_add` 같은 tool을 호출합니다.

흐름은 다음과 같습니다.

```text
Codex 시작
-> MCP 설정 읽기
-> eruditus_harness_server.py ... serve 실행
-> tools/list로 사용 가능한 tool 확인
-> 필요할 때 rag_search 또는 memory_search 호출
-> Codex 종료 시 MCP server process도 종료
```

### 5. 문서가 바뀌었을 때

문서 내용이 바뀌면 자동 반영되지 않습니다. 다시 ingest하세요.

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  rag-ingest /path/to/company-docs
```

원본 문서를 지워도 기존 검색은 동작합니다. chunk text는 `.codex/rag/metadata.sqlite3`에 저장되어 있고, vector는 `.codex/rag/index.pkl` 또는 `index.faiss`에 저장되어 있기 때문입니다.

다만 원본을 지우면 검색 결과의 `path`가 더 이상 열리지 않고, 원문 확인도 할 수 없습니다. 또한 현재 구현은 삭제된 원본 문서를 index에서 자동 제거하지 않으므로 stale 결과가 남을 수 있습니다.

## How It Works

이 repo는 두 가지를 제공합니다.

1. `codex-config/`: Codex에 복사해서 쓸 AGENTS/rules/MCP 설정 원본
2. `eruditus_harness_server.py`: local memory와 문서 RAG를 제공하는 MCP stdio server

`rag-ingest`는 문서를 읽어서 `.codex/rag/`에 저장합니다.
`serve`는 Codex가 실행하는 MCP server entrypoint입니다.
`memory-add`는 재사용할 know-how를 `.codex/memory/`에 Markdown note로 저장하고 memory index도 갱신합니다.

## Repository Layout

```text
.
├── codex-config/
│   ├── AGENTS.md
│   ├── mcp/
│   │   └── eruditus_harness_server.py
│   └── rules/
│       └── default.rules
├── docs/
│   └── harness-engineering.md
├── pyproject.toml
└── README.md
```

Runtime data는 적용 대상 환경의 `.codex/` 아래에 생성됩니다.

```text
.codex/
├── memory/
│   └── YYYYMMDD-title.md
└── rag/
    ├── metadata.sqlite3
    ├── index.faiss
    ├── index.pkl
    ├── memory_index.faiss
    ├── memory_index.pkl
    └── manifest.json
```

RAG backend는 환경에 따라 자동 선택할 수도 있습니다.

- `sentence-transformers`가 있으면 로컬 embedding model 사용
- 없으면 dependency 없는 hashing embedding fallback
- FAISS가 있으면 FAISS vector index 사용
- FAISS GPU가 있고 GPU가 보이면 검색 시 GPU 사용
- FAISS가 없으면 pure Python exact search fallback

## Config Locations

전역 Codex 환경에 적용:

```bash
mkdir -p ~/.codex
cp -R codex-config/* ~/.codex/
```

특정 repo에만 적용:

```bash
mkdir -p .codex
cp -R codex-config/* /path/to/your/repo/.codex/
```

이미 `.codex/AGENTS.md`나 rules 파일이 있다면 무조건 덮어쓰지 말고 필요한 섹션만 병합하세요.

## Environment

`eruditus_harness_server.py`는 기본적으로 현재 디렉터리의 `.codex`를 사용합니다.

우선순위:

1. CLI 인자 `--codex-home`
2. 환경 변수 `CODEX_HOME`
3. 기본값 `.codex`

예:

```bash
CODEX_HOME=~/.codex uv run python codex-config/mcp/eruditus_harness_server.py tools
```

repo-local `.codex`를 명시:

```bash
uv run python codex-config/mcp/eruditus_harness_server.py --codex-home .codex tools
```

## Memory

작업 중 다시 쓸 가치가 있는 know-how는 `.codex/memory`에 Markdown으로 저장합니다.

저장:

```bash
uv run python codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  memory-add \
  --title "build command" \
  --body "이 repo는 변경 후 uv run python -m py_compile ... 로 최소 검증한다." \
  --tag build
```

검색:

```bash
uv run python codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  memory-search "build 검증"
```

`.codex/memory`의 Markdown 파일을 사람이 직접 수정하거나 추가한 경우:

```bash
uv run python codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  memory-sync
```

`memory-search`는 검색할 때마다 파일시스템을 전수 스캔하지 않습니다. `memory-add`로 만든 note는 즉시 index되고, 수동 편집분은 `memory-sync`로 반영합니다.

저장하지 않을 것:

- credential, token, private key
- 검증되지 않은 추측
- 일회성 작업 로그

## Reference RAG Details

수정 권한이 없지만 자주 참조해야 하는 문서는 로컬 RAG index로 만듭니다.

지원 확장자:

- `.xml`
- `.pptx`
- `.docx`
- `.html`, `.htm`
- `.md`, `.markdown`

문서 또는 디렉터리 ingest:

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  rag-ingest /path/to/reference-docs
```

고성능 backend를 사용해 ingest:

```bash
uv run --extra perf python codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend sentence-transformers \
  --vector-backend faiss \
  rag-ingest /path/to/reference-docs
```

검색:

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  rag-search "검색할 내용" \
  --limit 5
```

Backend 상태 확인:

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  rag-status
```

Vector index와 metadata 불일치가 `rag-status`의 `index_errors`에 기록된 경우:

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  index-repair
```

원본 문서는 수정하지 않습니다. Index와 metadata는 `.codex/rag/` 아래에 저장됩니다.

## MCP Server Details

이 서버는 기본적으로 stdio MCP server입니다. tmux나 systemd로 계속 실행해두는 서비스가 아닙니다.
Codex 또는 다른 MCP client가 설정에 적힌 command를 실행하고, stdin/stdout으로 tool call을 주고받습니다.

MCP client가 실행할 command:

```bash
python3 codex-config/mcp/eruditus_harness_server.py \
  --codex-home .codex \
  --embedding-backend hash \
  --vector-backend python \
  serve
```

제공 tool:

- `memory_add`
- `memory_search`
- `memory_sync`
- `rag_ingest`
- `rag_search`
- `rag_status`
- `index_repair`

MCP client 설정 예시:

```json
{
  "mcpServers": {
    "eruditus-harness": {
      "command": "python3",
      "args": [
        "/absolute/path/to/codex-config/mcp/eruditus_harness_server.py",
        "--codex-home",
        "/absolute/path/to/.codex",
        "--embedding-backend",
        "hash",
        "--vector-backend",
        "python",
        "serve"
      ]
    }
  }
}
```

전역 `~/.codex`에 복사해서 사용할 경우:

```json
{
  "mcpServers": {
    "eruditus-harness": {
      "command": "python3",
      "args": [
        "/home/USER/.codex/mcp/eruditus_harness_server.py",
        "--codex-home",
        "/home/USER/.codex",
        "--embedding-backend",
        "hash",
        "--vector-backend",
        "python",
        "serve"
      ]
    }
  }
}
```

## Troubleshooting

### MCP startup failed: expect initialized result

Codex 시작 시 다음과 비슷한 오류가 나오면 MCP server의 initialize 응답이 client가 기대하는 스키마와 맞지 않는 상태입니다.

```text
MCP client for `eruditus-harness` failed to start:
handshaking with MCP server failed: expect initialized result
```

이 repo의 `eruditus_harness_server.py`는 `initialize` 응답에 `protocolVersion`, `capabilities`, `serverInfo`를 반환해야 합니다. 특히 `capabilities`가 빠진 오래된 복사본을 `.codex/mcp/` 또는 `~/.codex/mcp/`에서 실행하면 이 문제가 날 수 있습니다.

확인할 항목:

- MCP 설정의 `args`가 최신 `codex-config/mcp/eruditus_harness_server.py` 또는 최신으로 복사된 `.codex/mcp/eruditus_harness_server.py`를 가리키는지 확인합니다.
- repo-local `.codex`를 쓰고 있고 `codex-config/`만 업데이트했다면 서버 파일을 다시 복사합니다.

```bash
cp codex-config/mcp/eruditus_harness_server.py .codex/mcp/eruditus_harness_server.py
```

수동 handshake 확인:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
| python3 codex-config/mcp/eruditus_harness_server.py \
    --codex-home .codex \
    --embedding-backend hash \
    --vector-backend python \
    serve
```

응답의 `result` 안에 `"capabilities": {"tools": {}}`가 포함되어야 합니다.

### Codex sandbox bubblewrap error

다음 오류는 MCP 설정 문제가 아니라 Codex Linux sandbox가 user namespace 또는 loopback network namespace를 만들지 못하는 호스트 환경 문제입니다.

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

Ubuntu/Debian 계열 호스트에서는 아래 값을 확인합니다.

```bash
sysctl kernel.unprivileged_userns_clone
sysctl user.max_user_namespaces
```

필요하면 호스트에서 활성화합니다.

```bash
sudo sysctl -w kernel.unprivileged_userns_clone=1
sudo sysctl -w user.max_user_namespaces=28633
```

컨테이너 안에서 Codex를 실행하는 경우에는 컨테이너가 unprivileged user namespace와 loopback 설정을 허용하도록 실행되어야 합니다.

## Development

fallback 경로는 dependency 없이 실행되도록 유지합니다. 검증 명령은 `pyproject.toml` 환경을 확인하기 위해 `uv run`을 사용합니다.

`pyproject.toml` 유효성 확인:

```bash
uv run python -c 'import tomllib; tomllib.load(open("pyproject.toml", "rb")); print("pyproject ok")'
```

문법 검사:

```bash
uv run python -m py_compile codex-config/mcp/eruditus_harness_server.py
```

Tool 목록 확인:

```bash
uv run python codex-config/mcp/eruditus_harness_server.py --codex-home /tmp/eruditus-harness-test tools
```

Backend 상태 확인:

```bash
uv run python codex-config/mcp/eruditus_harness_server.py --codex-home /tmp/eruditus-harness-test rag-status
```

README를 샘플 문서로 ingest:

```bash
uv run python codex-config/mcp/eruditus_harness_server.py \
  --codex-home /tmp/eruditus-harness-test \
  rag-ingest README.md
```

검색 확인:

```bash
uv run python codex-config/mcp/eruditus_harness_server.py \
  --codex-home /tmp/eruditus-harness-test \
  rag-search "vector"
```

## Design Notes

RAG는 성능 경로와 fallback 경로를 모두 둡니다.

고성능 경로:

```text
sentence-transformers embedding
-> FAISS vector index
-> SQLite FTS5 keyword index
-> Reciprocal Rank Fusion
```

Fallback 경로:

```text
hashing embedding
-> pure Python exact vector search
-> SQLite FTS5 keyword index
-> Reciprocal Rank Fusion
```

CPU만 있는 환경에서는 `faiss-cpu` 또는 pure Python fallback으로 동작합니다. GPU FAISS가 설치되어 있고 GPU가 보이면 검색 시 GPU index를 사용하지만, 저장되는 `index.faiss`는 CPU 호환 index입니다.

자세한 구조는 [docs/harness-engineering.md](docs/harness-engineering.md)를 참고하세요.

## Acknowledgement
