# Architecture

This document covers: schema design, repo layout rationale, integration points, and what "working" looks like at each boundary.

---

## System overview

```
GitHub repo (local clone)
        │
        ▼
┌─────────────────┐
│  ingestion/     │  parser.py walks .py files via ast module
│  parser.py      │  extracts: files, functions, classes, imports
└────────┬────────┘
         │ entities dict
         ▼
┌─────────────────┐
│  ingestion/     │  loader.py upserts nodes + RELATE edges
│  loader.py      │  deterministic IDs → idempotent runs
└────────┬────────┘
         │ SurrealDB writes
         ▼
┌─────────────────────────────────────────┐
│              SurrealDB Cloud            │
│  Tables: file, function, class          │
│  Edges:  imports, contains, calls       │
│  Also:   checkpoints, checkpoint_writes │
└────────┬──────────────┬─────────────────┘
         │ graph queries │ checkpoint read/write
         ▼               ▼
┌──────────────┐  ┌──────────────────────┐
│  agent/      │  │  langgraph-           │
│  tools.py    │  │  checkpoint-surrealdb │
└──────┬───────┘  └──────────┬───────────┘
       │ tool results         │ state persistence
       ▼                      ▼
┌─────────────────────────────────────────┐
│            LangGraph StateGraph          │
│  llm_node → tools_node → llm_node ...  │
│  Checkpoints at every node transition   │
└────────────────┬────────────────────────┘
                 │ agent responses
                 ▼
┌─────────────────┐
│   ui/app.py     │  Streamlit: graph viz + chat
│   Streamlit     │  streamlit-agraph for visualisation
└─────────────────┘
```

---

## SurrealDB schema

### Namespace + database
```
namespace: hackathon
database:  deadreckoning
```

### Entity tables (nodes)

```sql
-- FILE: represents a .py source file
DEFINE TABLE file SCHEMAFULL;
  DEFINE FIELD path      ON file TYPE string;   -- relative path: "auth/login.py"
  DEFINE FIELD language  ON file TYPE string DEFAULT 'python';
  DEFINE FIELD line_count ON file TYPE int;
  DEFINE FIELD embedding ON file TYPE option<array>;  -- future use

DEFINE INDEX file_path_idx ON file FIELDS path UNIQUE;

-- FUNCTION: a named function or method
DEFINE TABLE function SCHEMAFULL;
  DEFINE FIELD name       ON function TYPE string;
  DEFINE FIELD file       ON function TYPE record<file>;
  DEFINE FIELD lineno     ON function TYPE int;
  DEFINE FIELD docstring  ON function TYPE option<string>;
  DEFINE FIELD embedding  ON function TYPE option<array>;  -- from docstring
  DEFINE FIELD is_method  ON function TYPE bool DEFAULT false;

DEFINE INDEX fn_name_file_idx ON function FIELDS name, file UNIQUE;

-- CLASS: a class definition
DEFINE TABLE class SCHEMAFULL;
  DEFINE FIELD name   ON class TYPE string;
  DEFINE FIELD file   ON class TYPE record<file>;
  DEFINE FIELD lineno ON class TYPE int;
  DEFINE FIELD bases  ON class TYPE array DEFAULT [];  -- base class names (strings)

DEFINE INDEX class_name_file_idx ON class FIELDS name, file UNIQUE;
```

### Edge tables (relationships)

```sql
-- SCHEMALESS edges: flexible, no required fields beyond the relation itself
DEFINE TABLE imports SCHEMALESS;   -- file -> file (or external dep string)
DEFINE TABLE contains SCHEMALESS;  -- file -> function | class
DEFINE TABLE calls SCHEMALESS;     -- function -> function (stretch goal)
DEFINE TABLE inherits SCHEMALESS;  -- class -> class (stretch goal)
```

### Deterministic record IDs

Use a stable hash so re-ingestion updates rather than duplicates:

```python
import hashlib

def file_id(path: str) -> str:
    h = hashlib.md5(path.encode()).hexdigest()[:12]
    return f"file:`{h}`"

def function_id(file_path: str, fn_name: str) -> str:
    key = f"{file_path}::{fn_name}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return f"function:`{h}`"

def class_id(file_path: str, class_name: str) -> str:
    key = f"{file_path}::{class_name}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return f"class:`{h}`"
```

### Key SurrealQL patterns

```sql
-- Create or update (upsert) — idempotent
INSERT INTO file { id: file:`abc123`, path: "auth.py", language: "python", line_count: 120 }
  ON DUPLICATE KEY UPDATE line_count = line_count;

-- Create edge
RELATE file:`abc123` -> imports -> file:`def456`;

-- Graph traversal: what does auth.py import?
SELECT ->imports->file.path AS imports FROM file:`abc123`;

-- Reverse traversal: what imports utils.py?
SELECT <-imports<-file.path AS imported_by FROM file:`def456`;

-- 2-hop: transitive imports
SELECT ->imports->file->imports->file.path AS transitive FROM file:`abc123`;

-- Hybrid: vector search then graph expand
SELECT *, vector::similarity::cosine(embedding, $query_vec) AS score
FROM function
WHERE vector::similarity::cosine(embedding, $query_vec) > 0.75
ORDER BY score DESC LIMIT 5
FETCH file;
```

---

## Agent design

### State

```python
# agent/state.py
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]  # conversation history
    repo_path: str                            # which repo we're querying
```

### Tools (agent/tools.py)

| Tool | Input | SurrealQL used | Returns |
|---|---|---|---|
| `get_dependencies(module)` | filename (partial ok) | `->imports->file` traversal | list of file paths |
| `find_callers(function)` | function name | `<-calls<-function` reverse | list of function names |
| `semantic_search(query)` | natural language string | vector NEAR on embeddings | list of functions + snippets |
| `explain_module(module)` | filename | get all functions + docstrings | summary string |

### Graph structure (agent/graph.py)

```python
# LangGraph node flow:
# 
#  [START]
#     │
#     ▼
#  llm_node          ← calls Ollama (gemma3) with tools bound
#     │
#     ├─ tool_calls? → tools_node → back to llm_node
#     │
#     └─ no tool calls → [END]
#
# Compiled with checkpointer:
# agent = graph.compile(checkpointer=SurrealDBSaver(...))
```

### Ollama wiring

```python
import os
from langchain_ollama import ChatOllama, OllamaEmbeddings

# LLM — swap model via env var: gemma3:4b (dev) or gemma3:27b (demo)
llm = ChatOllama(
    model=os.getenv("OLLAMA_MODEL", "gemma3:4b"),
    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
)

# Embeddings — nomic-embed-text, 768 dimensions
embeddings = OllamaEmbeddings(
    model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
)

# Embed a docstring
vector = embeddings.embed_query("Authenticates a user with username and password")
# Returns list[float] of length 768 — store directly in SurrealDB embedding field

# Bind tools to LLM for the agent
llm_with_tools = llm.bind_tools([get_dependencies, find_callers, semantic_search, explain_module])
```

**Model selection:**
- `gemma3:4b` — use for all development and testing. Fast, low RAM, good enough for tool use.
- `gemma3:27b` — switch to this for the live demo. Better reasoning, more coherent answers.
- Change via `OLLAMA_MODEL` env var only — no code changes needed.

**Note:** Ollama must be running (`ollama serve`) before the agent starts. Models must be pulled:
```bash
ollama pull gemma3:4b
ollama pull gemma3:27b
ollama pull nomic-embed-text
```

---

### Checkpointer wiring

```python
from langgraph_checkpoint_surrealdb import SurrealDBSaver
import os

checkpointer = SurrealDBSaver(
    url=os.getenv("SURREALDB_URL"),
    username=os.getenv("SURREALDB_USER"),
    password=os.getenv("SURREALDB_PASS"),
    namespace=os.getenv("SURREALDB_NS"),
    database=os.getenv("SURREALDB_DB"),
)

# NOTE: check langgraph-checkpoint-surrealdb README for exact init signature
# It may differ — read the source before coding against it

# Thread ID ties a session to a repo
config = {"configurable": {"thread_id": f"query-{repo_name}"}}

# Invoke (first call or resume — same pattern)
result = agent.invoke({"messages": [("user", query)]}, config)
```

### Ingestion agent thread IDs

```python
# Ingestion uses a separate thread namespace from query sessions
ingest_config = {"configurable": {"thread_id": f"ingest-{repo_name}"}}

# This means: query and ingest share the same SurrealDB
# but have separate checkpoint histories
```

---

## Integration points

These are the five boundaries where things break. Test each one explicitly before moving on.

---

### INT-1: parser.py → loader.py

**What crosses the boundary:** A Python dict per file:
```python
{
  "path": "auth/login.py",
  "line_count": 120,
  "functions": [{"name": "login", "lineno": 14, "docstring": "Authenticates user"}],
  "classes": [{"name": "AuthService", "lineno": 5, "bases": ["BaseService"]}],
  "imports": ["os", "utils.helpers", "fastapi.Request"]
}
```

**Test:**
```bash
python -c "
from ingestion.parser import parse_file
result = parse_file('tests/fixtures/sample.py')
print(result)
assert 'functions' in result
assert 'imports' in result
print('INT-1 PASS')
"
```

**Success:** Dict printed with at least 1 function and 1 import for any non-trivial .py file.

---

### INT-2: loader.py → SurrealDB

**What crosses the boundary:** SurrealDB records and edges created from the parser output.

**Test:**
```bash
python -c "
from ingestion.loader import load_file
from ingestion.parser import parse_file

parsed = parse_file('tests/fixtures/sample.py')
load_file(parsed)
print('INT-2 write PASS')
"

# Then in SurrealDB cloud console or CLI:
# SELECT * FROM file LIMIT 5;          -- should show records
# SELECT * FROM function LIMIT 5;      -- should show records
# SELECT ->contains->function FROM file LIMIT 1;  -- should show edges
```

**Success:** `SELECT count() FROM function GROUP ALL` returns > 0.

---

### INT-3: tools.py → SurrealDB (standalone)

**What crosses the boundary:** Tool functions called directly (not via agent) return real data.

**Test:**
```bash
python -c "
from agent.tools import get_dependencies, find_callers
# Use a filename you know exists in your seeded DB
deps = get_dependencies.invoke({'module': 'login'})
print('deps:', deps)
assert isinstance(deps, list)
assert len(deps) > 0
print('INT-3 PASS')
"
```

**Success:** Each tool returns a non-empty list when called against a seeded database. Test ALL tools individually before wiring into the agent.

---

### INT-4: graph.py + checkpointer → interrupt/resume

**What crosses the boundary:** Agent state surviving a process kill and reloading correctly.

**Test:**
```bash
# Terminal 1: run ingestion agent, kill after 3 files
python -c "
from agent.graph import build_ingestion_agent
import os

agent = build_ingestion_agent()
config = {'configurable': {'thread_id': 'test-ingest-001'}}
# This will run — hit CTRL+C after you see '3 files processed'
agent.invoke({'repo_path': '/path/to/small/repo'}, config)
" 
# CTRL+C

# Terminal 1: resume — same thread_id
python -c "
from agent.graph import build_ingestion_agent

agent = build_ingestion_agent()
config = {'configurable': {'thread_id': 'test-ingest-001'}}
result = agent.invoke(None, config)  # None = resume
print(result)
"
```

**Success:** Second invocation prints something like "resuming from checkpoint, 3 files already processed" and continues from file 4, not file 1. Verify by checking `nodes_indexed` in the resumed state matches the interrupted state.

---

### INT-5: agent → Streamlit UI

**What crosses the boundary:** Agent invoked from Streamlit, response streamed to chat.

**Test:**
```bash
streamlit run ui/app.py
# In the UI:
# 1. Enter a module name in the chat: "what does login.py import?"
# 2. Verify: response appears within 10 seconds
# 3. Verify: graph panel shows nodes (not empty)
# 4. Open smith.langchain.com — verify trace appeared
```

**Success:** Chat response contains actual file/function names from the seeded repo (not hallucinated). LangSmith trace shows tool calls.

---

## Design decisions

**Why SurrealDB for both knowledge graph AND checkpoints?**
SurrealDB can handle both graph-style data (RELATE, traversal queries) and row-style data (checkpoint tables) in the same instance. This avoids a second database and lets us demo SurrealDB doing two distinct things — which scores highly with the SurrealDB judges.

**Why Python `ast` module over tree-sitter?**
`ast` is built-in (zero install friction), handles all valid Python 3 syntax, and is simpler to walk. tree-sitter adds multi-language support but at the cost of a C dependency and more complex setup. Scope to Python only for the hackathon.

**Why Streamlit over FastAPI + React?**
Solo build. Streamlit with `streamlit-agraph` delivers graph visualisation + chat in ~200 lines. React would take 4–6 hours. The demo needs to look credible, not beautiful.

**Why `httpx` as the demo repo?**
~30 Python files, clean code, interesting graph structure (AsyncClient, connection pooling, auth middleware show up as meaningful relationships), fast to index (~20 seconds), well-known to a technical audience.

**Why separate thread IDs for ingestion vs query agents?**
Ingestion and query are separate LangGraph graphs with different state shapes. Mixing their checkpoints in the same thread would cause state deserialization errors. Namespacing thread IDs (`ingest-*` vs `query-*`) keeps them cleanly separated in the same SurrealDB database.
