# Demo Script — DeadReckoning

Live judging — Sunday ~14:00. You have **2 minutes**. This is the exact script.

Rehearse it 3 times Saturday evening. Know every click cold. Have all tabs open and ready before the judges arrive at your station.

---

## Setup (before judges arrive)

```
Browser tab 1: Streamlit app (localhost:8501) — Knowledge Graph tab, v1 already ingested
Browser tab 2: LangSmith (smith.langchain.com) — dead-reckoning project
Terminal: open, minimised, ready
```

**State before judges arrive:**
- v1 sample repo fully ingested — graph shows all green nodes
- v1 ingestion was interrupted mid-run and resumed — both runs visible in LangSmith
- v2 sample repo NOT yet ingested — waiting to trigger live
- Sidebar shows: `Snapshot: 48 KB · abc12345.tar`

**Run Sunday morning to set this up:**
```bash
python demo/seed_demo.py --version v1   # ingest v1, interrupt at ~50%, resume
# Leave the app open on the graph tab
```

---

## The script

---

**[0:00 — OPEN with the problem]**

*Show graph tab — v1 fully indexed, all nodes green*

Say:
> "Every developer knows this moment. New codebase, no idea what talks to what. You're reading files trying to reconstruct a map that already exists — it's just locked in the code."

---

**[0:12 — FIRST WOW — interrupt and resume]**

*Click to LangSmith tab — show the interrupted run, then the resumed run below it*

Say:
> "Our ingestion agent was mapping this repo into a SurrealDB knowledge graph. Halfway through — I killed it. Here's the LangSmith trace."

*Point at the two runs — interrupted, then resumed*

Say:
> "Same thread ID. It resumed from checkpoint — exactly where it stopped. The nodes already built stayed in the graph. That's the SurrealDB checkpointer: agent state survives the kill."

*Switch back to graph tab*

---

**[0:35 — SECOND WOW — versioned diff]**

*In the sidebar, click "v2 — sample repo (with changes)" quick-select → path fills*

Say:
> "Now the code has changed. New version."

*Click Ingest → conflict dialog: "A previous version exists"*

Say:
> "It detects the previous version and asks what to do."

*Click "Add new version" → graph animates → nodes turn green, yellow, red*

Say:
> "Green: unchanged. Yellow: modified. Red: deleted. The knowledge graph is now a diff."

*Open terminal. Type:*
```bash
tar tf ~/.dead-reckoning/snapshots/$(ls -t ~/.dead-reckoning/snapshots/ | head -1)
```

Say:
> "This is a real tar file — OCI format, same content-addressing and whiteout semantics Docker uses. We implemented it in pure Python. No Docker daemon needed. You could docker import this."

---

**[1:05 — QUERY across the diff]**

*Switch to "Ask the Codebase" tab. Type:*
```
what changed between versions and what might be affected?
```

Say (while agent responds):
> "The agent reasons across the diff — it knows which nodes are yellow and traverses their dependencies to surface impact."

---

**[1:25 — LANGSMITH — show the reasoning]**

*Switch to LangSmith — trace for the query visible*

Say:
> "Every step observable. It called get_dependencies on the modified files to find downstream impact. Tool calls, graph traversal, fully auditable."

*Point at the tool call chain*

---

**[1:45 — CLOSE]**

*Switch back to graph — coloured nodes*

Say:
> "A knowledge graph that survives crashes, versions itself like a container layer, and lets you query across the diff. All in SurrealDB — the graph, the checkpoints, the version history. Same database. Three jobs."

---

**[2:00 — DONE]**

---

## Anticipated judge questions

**"How is this different from just using an LLM with the code as context?"**
> "Context windows can't do multi-hop graph traversal. 'What imports auth.py, and what does that import?' is structural — you need the graph. LLMs also forget between sessions. Our agent accumulates knowledge across runs and versions."

**"Why OCI tar format?"**
> "Same content-addressing as Docker — SHA-256 per file, whiteout entries for deletions. Pure Python stdlib, zero extra dependencies. The snapshots are format-compatible: you can docker import them. We understood the spec well enough to implement it ourselves."

**"What happens to the graph on a new version?"**
> "We diff the two tar snapshots — old SHA-256 vs new. Same hash goes green, changed goes yellow, absent goes red. SurrealDB nodes get a diff_status field updated in place. No re-ingestion needed for the diff — it's pure snapshot comparison."

**"Why SurrealDB specifically?"**
> "One instance doing three things: knowledge graph with typed edges, LangGraph checkpoint state, and version history with snapshot paths. Graph traversal AND vector search in a single SurrealQL query. No second database anywhere."

**"What's the LangGraph checkpointer doing?"**
> "After every node, LangGraph serialises the full agent state and writes it to SurrealDB via langgraph-checkpoint-surrealdb. Same thread ID on resume = rehydrate that state and continue the loop from where it stopped."

**"Does it work on non-Python repos?"**
> "Python only for the AST parsing — we used the built-in ast module. tree-sitter adds multi-language support; that's the obvious next step. The snapshot and diff layer works on any file type already."

---

## Pre-typed queries — scratch file, copy-paste during demo

```
what changed between versions and what might be affected?
what does _auth.py depend on?
which functions handle the core HTTP request logic?
what would break if I removed utils.py?
```

---

## Timing failsafes

**If diff colouring is slow (> 5s):** Keep talking — "computing SHA-256 across both tar snapshots, same as Docker layer diffing..." — it will arrive.

**If the conflict dialog doesn't appear:** v1 ingestion_id not in session state. Refresh, re-ingest v1 quickly (fixture repo is fast), then ingest v2.

**If the agent query is slow (> 15s):** Switch to LangSmith immediately — "you can watch it reasoning right now" — point at tool calls firing. The wait becomes part of the demo.

**If you need to show interrupt/resume live instead of via LangSmith history:**
```bash
python -c "
from agent.ingest_graph import build_ingestion_agent
agent = build_ingestion_agent()
config = {'configurable': {'thread_id': 'ingest-sample-v1'}}
agent.invoke(None, config)
"
```

**If Streamlit crashes entirely:**
```bash
python -c "
from agent.graph import build_query_agent
agent = build_query_agent()
config = {'configurable': {'thread_id': 'demo-fallback'}}
r = agent.invoke({
    'messages': [('user', 'what does _auth.py depend on?')],
    'repo_path': 'tests/fixtures/sample_repo/v1'
}, config)
print(r['messages'][-1].content)
"
```

**If you have under 90 seconds:** Skip the query and LangSmith entirely. Do: problem → interrupt/resume (point at LangSmith history) → diff colouring → close. The two visual moments are enough to win.

**The one thing that must not fail:** Green/yellow/red colouring on the v1→v2 diff. Rehearse this flow until it works perfectly every single time before Sunday morning.