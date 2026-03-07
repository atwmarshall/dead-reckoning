# Demo Script

Live judging — Sunday ~14:00. You have **2 minutes**. This is the exact script.

Rehearse it 3 times Saturday evening. Know the queries cold. Have both tabs open and ready before the judges arrive at your station.

---

## Setup (before judges arrive)

```
Browser tab 1: Streamlit app (localhost:8501) — on the Knowledge Graph tab
Browser tab 2: LangSmith (smith.langchain.com) — on the dead-reckoning project
Terminal: ready but minimised
```

**State of the demo repo before judges arrive:**
- `httpx` repo fully indexed in SurrealDB (run `demo/seed_demo.py` Sunday morning)
- Ingestion agent interrupted at ~50% — checkpointed state saved
  - To set this up: run ingestion, kill it at ~17 files, don't resume yet
- Graph tab should show ~17 files' worth of nodes when you open it

---

## The script

---

**[0:00 — OPEN with the problem]**

*Click to graph tab — show partial graph with ~17 nodes*

Say:
> "Every developer knows this moment. You've joined a new codebase and you have no idea what talks to what. You're reading files trying to build a mental model that already exists — it's just locked in the code."

---

**[0:15 — SHOW the partial state]**

*Point at the graph — nodes visible but incomplete*

Say:
> "This is `httpx` — a popular Python HTTP library. Our agent started mapping it into a knowledge graph in SurrealDB. Files, functions, imports — all becoming nodes and edges. Then I killed it."

---

**[0:25 — THE WOW MOMENT — interrupt/resume]**

*Switch to terminal. Type:*
```bash
python -c "
from agent.ingest_graph import build_ingestion_agent
agent = build_ingestion_agent()
config = {'configurable': {'thread_id': 'ingest-httpx'}}
result = agent.invoke(None, config)
"
```

Say (while it runs):
> "Same thread ID. Watch."

*Point at terminal output — should print "resuming from checkpoint, 17 files already processed"*

Say:
> "It resumed exactly where it stopped. The 17 files it already processed? Still in the graph. It didn't start over. That's the SurrealDB checkpointer — the agent's state survived the kill."

*Graph updates live as new nodes appear*

---

**[0:50 — QUERY the graph]**

*Switch to Streamlit chat tab. Click on "Ask the Codebase" tab. Type in chat:*
```
what does _auth.py depend on?
```

Say (while agent responds):
> "Now it's queryable. Plain English. The agent is traversing the knowledge graph — not searching text, following typed relationships."

*Response appears — should list file dependencies*

---

**[1:10 — SECOND QUERY — show the graph reasoning]**

*Type in chat:*
```
which functions are involved in making an HTTP request?
```

Say:
> "This one uses semantic search — embeddings on function docstrings — then expands the context with graph traversal. Vector search finds candidates, graph traversal finds what they connect to."

---

**[1:25 — LANGSMITH — show the reasoning]**

*Switch to LangSmith tab — trace for the last query should be visible*

Say:
> "Every step is observable. You can see the agent decided to call `semantic_search`, got 5 candidates, then called `get_dependencies` to expand context. Fully auditable."

*Point at the tool calls in the trace*

---

**[1:40 — CLOSE]**

*Switch back to graph tab — full graph now visible*

Say:
> "One weekend. A codebase that can answer questions about itself — with a knowledge graph that survives crashes, and a reasoning agent you can watch think. Both powered by SurrealDB: the graph is the knowledge store, and the checkpoints are the agent's memory. Same database, two jobs."

---

**[2:00 — DONE]**

---

## Anticipated judge questions

**"How is this different from just using an LLM with the code as context?"**
> "Context windows can't do multi-hop graph traversal. 'What imports auth.py, and what does that file import?' is a structural question — you need the graph. LLMs also forget between sessions. Our agent accumulates knowledge across runs."

**"What happens if you change the code?"**
> "Re-run the ingestion on the changed files — same deterministic IDs, so it updates the existing nodes rather than duplicating. The graph evolves with the codebase."

**"Why SurrealDB specifically?"**
> "It's the only database doing graph traversal AND vector search in a single query, in the same instance. We didn't need a separate vector store — the hybrid retrieval query is a single SurrealQL statement."

**"What's the LangGraph checkpointer doing exactly?"**
> "LangGraph saves the agent's full state — every variable, every message, the position in the graph — after each step. We're using `langgraph-checkpoint-surrealdb` which stores that state in SurrealDB. Same database as the knowledge graph, different tables. When we resume, LangGraph rehydrates the state and continues the loop."

**"Does it work on non-Python repos?"**
> "Python only right now — we used the built-in `ast` module for reliable parsing. tree-sitter would add multi-language support; that's the obvious next step."

---

## What to have queued up (pre-typed, ready to paste)

Have these in a scratch file to copy-paste fast during the demo:

```
what does _auth.py depend on?
which functions are involved in making an HTTP request?
what would break if I changed the _client.py connection logic?
explain what _models.py contains
```

---

## Timing failsafes

**If the agent is slow (> 15s):** Keep talking. "The agent is doing a multi-hop graph traversal — you can see the tool calls firing in LangSmith..." — switch to LangSmith while it runs.

**If the resume fails:** Don't panic. Say "let me show you the SurrealDB query directly" — open the SurrealDB cloud console and run `SELECT ->imports->file FROM file LIMIT 1` live. Shows the graph is real.

**If Streamlit crashes:** Have a terminal fallback:
```bash
python -c "
from agent.graph import build_query_agent
agent = build_query_agent()
config = {'configurable': {'thread_id': 'demo-fallback'}}
r = agent.invoke({'messages': [('user', 'what does _auth.py depend on?')], 'repo_path': '/tmp/demo-repo'}, config)
print(r['messages'][-1].content)
"
```

**If you have less than 2 minutes:** Cut to the interrupt/resume moment immediately, then one agent query, then close. Skip everything else. The wow moment > everything.
