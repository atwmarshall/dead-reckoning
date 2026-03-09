# Demo Cheat Sheet

## Setup (before judges arrive)

```bash
ollama list
ollama pull llama3.2:3b
ollama pull nomic-embed-text

uv run python demo/seed_demo.py

uv run streamlit run ui/app.py
```

---

## Paste into "Ask the Codebase" chat

```
find the slugify function and what depends on it
```

```
What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.
```

---

## Backup queries

```
what would break if I changed slugify?
```

```
what repos have been indexed and how many versions?
```

```
what changed between versions and what might be affected?
```

---

## SurrealDB Cloud UI queries

Query 1 — BM25 keyword search:
> "SurrealDB has native full-text search with BM25 scoring — this finds functions matching 'auth' across names and docstrings in a single query."

```sql
SELECT name, file.path AS path,
       search::score(0) + search::score(1) AS score
FROM `function`
WHERE name @0@ 'auth' OR docstring @1@ 'auth'
ORDER BY score DESC LIMIT 10;
```

Query 2 — Blast radius (graph traversal):
> "Multi-hop graph traversal — one SurrealQL statement walks two hops through the call graph to find direct callers and their callers. This is structural reasoning that context windows can't do."

```sql
SELECT name, file.path AS path,
       <-calls<-`function`.name AS direct_callers,
       <-calls<-`function`<-calls<-`function`.name AS transitive_callers
FROM `function`
WHERE name CONTAINS "_send";
```

Query 3 — Diff status:
> "The knowledge graph is version-aware — every file and function has a diff status. This query traverses the contains edge to show which functions live inside each changed file."

```sql
SELECT path, diff_status,
       ->contains->function.name AS functions
FROM file
WHERE diff_status IS NOT NONE
ORDER BY diff_status;
```

---

## Emergency fallback (if Streamlit crashes)

```bash
uv run python -c "
from agent.graph import build_query_agent
agent = build_query_agent()
config = {'configurable': {'thread_id': 'demo-fallback'}}
r = agent.invoke({
    'messages': [('user', 'What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.')],
}, config)
print(r['messages'][-1].content)
"
```
