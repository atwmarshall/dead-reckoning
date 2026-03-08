# Demo Runbook — Pass/Fail Results & Step-by-Step Commands

Last tested: 2026-03-08

---

## Pass/Fail Summary

| # | Step | Status | Notes |
|---|------|--------|-------|
| 1 | Ollama models pulled | PASS | `llama3.2:3b` + `nomic-embed-text` present |
| 2 | SurrealDB Cloud connection | PASS | Returns count array, no errors |
| 3 | Seed: v1 fixtures | PASS | 4 files, ~10 functions, 1 ingestion |
| 4 | Streamlit launches | PASS | `localhost:8501` loads, graph renders |
| 5 | Q1 — RRF hybrid search (scratch) | PASS | Returns ranked functions (after fix) |
| 5 | Q2 — Blast radius (scratch) | PASS | Returns callers + transitive callers (after fix) |
| 5 | Q3 — Diff status (scratch) | PASS | Returns diff_status rows after v2 ingestion |
| 6 | v1 graph rendering | PASS | Nodes, edges, pan/zoom works |
| 7 | Agent query ("find slugify") | PASS | hybrid_search + trace_impact chain fires |
| 8 | Ingest v2 | PASS | Conflict dialog appears, "Add new version" works |
| 9 | Interrupt at diff review | PASS | Pipeline pauses, "Resume" button appears (after fix) |
| 10 | Resume → diff colouring | PASS | Green/yellow/red/blue nodes render |
| 11 | 3-tool agent chain | PASS | version_diff → generate_docstring → raise_issue |
| 12 | LangSmith traces | PASS | Tool calls visible as nested spans |
| 13 | All unit tests | PASS | 24/24 pass (`uv run pytest tests/test_tools.py -v`) |

### Fixes applied during testing

| Issue | Root cause | Fix |
|-------|-----------|-----|
| Q1 — `Unexpected token 'WHERE'` | `function` is a SurrealDB reserved word, needs backticks | Added `` `function` `` quoting |
| Q1 — `vector::similarity::cosine` wrong type | `<\|5,100\|>` returns functions without embeddings | Added `WHERE embedding IS NOT NONE` filter |
| Q1 — `search::rrf()` empty in LET context | Multi-statement `query_raw` failed silently | Switched to separate `db.query()` calls + Python-side RRF |
| Q2 — `Unexpected token 'WHERE'` | Same reserved word issue | Added backtick quoting |
| 3c — Auto-resume skipping interrupt | Background thread sent `Command(resume=True)` immediately | Added `resume_event.wait()` to block until user clicks Resume |
| Enrich hung on 1000+ Ollama calls | `enrich_summaries` ran for ALL functions during ingestion | Removed from ingestion graph; now UI-only via "Suggest Docstrings" button |

---

## Step-by-Step Commands

### Setup (before judges arrive)

#### 1. Verify Ollama

```bash
ollama list
# Must show: llama3.2:3b + nomic-embed-text
# If missing:
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

#### 2. Verify SurrealDB connection

```bash
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
load_dotenv()
async def check():
    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])
    print(await db.query('SELECT count() FROM file GROUP ALL'))
    await db.close()
asyncio.run(check())
"
```

Expected: `[{'count': ...}]` or `[]` — no error.

#### 3. Seed demo data

```bash
# One command: reset DB, ingest v1 fixtures only. Fast, clean.
uv run python demo/seed_demo.py

# Verify counts (should see 4 files, ~10 functions, 1 ingestion)
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
load_dotenv()
async def counts():
    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])
    for t in ['file', 'function', 'class', 'calls', 'imports', 'ingestion']:
        r = await db.query(f'SELECT count() FROM \`{t}\` GROUP ALL')
        print(f'{t:>12}: {r}')
    await db.close()
asyncio.run(counts())
"
```

Expected: `file` 4, `function` ~10, `ingestion` 1.

#### 4. Start the app

```bash
uv run streamlit run ui/app.py
```

#### 5. Run tests

```bash
uv run pytest tests/test_tools.py -v
# Expected: 24/24 pass
```

---

### Scratch Queries (have ready in a second terminal)

#### Q1 — RRF Hybrid Search

```bash
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
from langchain_ollama import OllamaEmbeddings
load_dotenv()

async def rrf_demo():
    embedder = OllamaEmbeddings(model='nomic-embed-text')
    vec = embedder.embed_query('text processing')

    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])

    # Vector search (top-5 nearest neighbours among embedded functions)
    vs_rows = await db.query('''
        SELECT name, file.path AS path,
               vector::similarity::cosine(embedding, \$vec) AS score
        FROM \`function\`
        WHERE embedding IS NOT NONE AND embedding <|5,100|> \$vec
    ''', {'vec': vec})

    # BM25 keyword search
    ft_rows = await db.query('''
        SELECT name, file.path AS path,
               search::score(0) + search::score(1) AS score
        FROM \`function\`
        WHERE name @0@ \$keyword OR docstring @1@ \$keyword
        ORDER BY score DESC LIMIT 10
    ''', {'keyword': 'slug'})

    # RRF fusion (same algorithm as SurrealDB's search::rrf)
    scores, data_map = {}, {}
    k = 60
    for rank, r in enumerate((vs_rows or [])[:5]):
        name = r.get('name', '?')
        scores[name] = scores.get(name, 0) + 1 / (k + rank + 1)
        data_map[name] = r
    for rank, r in enumerate((ft_rows or [])[:10]):
        name = r.get('name', '?')
        scores[name] = scores.get(name, 0) + 1 / (k + rank + 1)
        if name not in data_map:
            data_map[name] = r

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    print('  RRF hybrid results (vector + BM25):')
    for name, score in ranked[:5]:
        path = data_map.get(name, {}).get('path', '?')
        print(f\"  {name:30s}  {path}\")
    await db.close()

asyncio.run(rrf_demo())
"
```

Expected: Ranked functions from the fixture repo related to text processing / slugify.

**What to say:** "Two searches — vector similarity for semantic meaning, BM25 for keyword matching — fused with Reciprocal Rank Fusion. The database does the heavy lifting."

#### Q2 — Blast Radius / Multi-hop Graph Traversal

```bash
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
load_dotenv()

async def impact():
    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])

    rows = await db.query('''
        SELECT name, file.path AS path,
               <-calls<-\`function\`.name AS direct_callers,
               <-calls<-\`function\`<-calls<-\`function\`.name AS transitive_callers
        FROM \`function\`
        WHERE name CONTAINS \"slugify\"
    ''')
    print('--- Blast radius for slugify ---')
    for r in (rows if isinstance(rows, list) else []):
        dc = r.get('direct_callers') or []
        tc = r.get('transitive_callers') or []
        print(f\"  {r.get('name')}: {len(dc)} direct callers, {len(tc)} transitive\")
    await db.close()

asyncio.run(impact())
"
```

Expected: `slugify` with its direct + transitive caller counts.

**What to say:** "One SurrealQL query, two hops through the call graph. 'What calls X, and what calls that?' — structural reasoning that context windows can't do."

#### Q3 — Diff Status (after v2 ingestion)

```bash
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from surrealdb import AsyncSurreal
load_dotenv()

async def diff():
    db = AsyncSurreal(os.environ['SURREALDB_URL'])
    await db.connect()
    await db.signin({'username': os.environ['SURREALDB_USER'], 'password': os.environ['SURREALDB_PASS']})
    await db.use(os.environ['SURREALDB_NS'], os.environ['SURREALDB_DB'])

    rows = await db.query('''
        SELECT path, diff_status,
               ->contains->function.name AS functions
        FROM file
        WHERE diff_status IS NOT NONE
        ORDER BY diff_status
    ''')
    for r in (rows if isinstance(rows, list) else []):
        status = r.get('diff_status', '?').upper()
        print(f\"  [{status:>8}] {r.get('path')} -> functions: {r.get('functions')}\")
    await db.close()

asyncio.run(diff())
"
```

Expected: Files with `ADDED`, `MODIFIED`, `DELETED`, `UNCHANGED` statuses and their functions.

**What to say:** "The knowledge graph is version-aware. Not just 'what exists' but 'what changed' — at file AND function granularity."

---

### Live Demo Flow

| Time | Action | What to do |
|------|--------|------------|
| 0:00 | Show v1 graph | Knowledge Graph tab, v1 fixture selected. Point at nodes/edges. |
| 0:10 | Agent query | Ask the Codebase tab → type: `find the slugify function and what depends on it` |
| 0:30 | Ingest v2 | Quick-select v2 → click Ingest. Click "Add new version" on conflict dialog. |
| 0:40 | Interrupt fires | Pipeline pauses. Say "LangGraph interrupt — checkpointed to SurrealDB, resumable." |
| 0:45 | Resume | Click Resume button. Watch green/yellow/red/blue colouring. |
| 0:55 | 3-tool agent chain | Ask the Codebase tab → type: `What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.` |
| 1:20 | LangSmith | Switch to LangSmith tab. Show the 3-tool chain trace. |
| 1:40 | Close | Switch back to graph. Summary line. |

---

### Key gotchas

1. **`function` and `class` are reserved words in SurrealDB.** Always backtick-quote them: `` `function` ``, `` `class` ``.
2. **The interrupt/resume requires a user click.** The pipeline blocks on `resume_event.wait()` — the UI will show "Diff ready — review the graph, then click Resume."
3. **SurrealDB Cloud can have transient connection issues.** If you get `CancelledError` or `TimeoutError`, wait 30s and retry.
4. **If the UI "Resume" button doesn't appear immediately**, click anywhere in the sidebar to trigger a Streamlit rerun — the button will appear.
5. **Docstring enrichment is UI-only.** It does NOT run during ingestion. Use the "Suggest Docstrings" button in the sidebar to trigger it manually.
