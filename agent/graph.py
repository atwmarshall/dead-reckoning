import os

from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph_checkpoint_surrealdb import SurrealSaver
from surrealdb import AsyncSurreal

from agent.state import AgentState
from agent.tools import hybrid_search, trace_impact, version_diff

load_dotenv()

TOOLS = [hybrid_search, trace_impact, version_diff]

SYSTEM_PROMPT = (
    "You are a codebase assistant with three tools:\n"
    "- hybrid_search: find functions by concept or name (semantic + keyword fusion)\n"
    "- trace_impact: find what calls a function and what would break if it changed (graph traversal)\n"
    "- version_diff: see what changed between versions — files and functions (red/yellow/green)\n"
    "Always use tools before answering. Use version_diff first for 'what changed' questions, "
    "then trace_impact on modified items to assess impact. Answer only from tool results."
)


async def _ensure_checkpoint_tables() -> None:
    """Create checkpoint/write tables if they don't exist.
    SurrealSaver.setup() is a no-op in this library version — we do it ourselves."""
    db = AsyncSurreal(os.environ["SURREALDB_URL"])
    await db.connect()
    await db.signin({"username": os.environ["SURREALDB_USER"], "password": os.environ["SURREALDB_PASS"]})
    await db.use(os.environ["SURREALDB_NS"], os.environ["SURREALDB_DB"])
    await db.query("DEFINE TABLE IF NOT EXISTS checkpoint SCHEMALESS")
    await db.query("DEFINE TABLE IF NOT EXISTS `write` SCHEMALESS")
    await db.close()


def _llm_node(state: AgentState) -> dict:
    model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    kwargs = {
        "model": model,
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    }
    # gpt-oss:20b is a thinking model — disable thinking to avoid wasted tokens
    if "gpt-oss" in model:
        kwargs["model_kwargs"] = {"think": False}
    llm = ChatOllama(**kwargs).bind_tools(TOOLS)
    messages = [("system", SYSTEM_PROMPT)] + list(state["messages"])
    response = llm.invoke(messages)
    return {"messages": [response]}


def _should_use_tools(state: AgentState) -> str:
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return END


def build_query_agent():
    import asyncio
    asyncio.run(_ensure_checkpoint_tables())

    checkpointer = SurrealSaver(
        url=os.environ["SURREALDB_URL"],
        namespace=os.environ["SURREALDB_NS"],
        database=os.environ["SURREALDB_DB"],
        user=os.environ["SURREALDB_USER"],
        password=os.environ["SURREALDB_PASS"],
    )
    checkpointer.setup()

    graph = StateGraph(AgentState)
    graph.add_node("llm", _llm_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.set_entry_point("llm")
    graph.add_conditional_edges("llm", _should_use_tools, {"tools": "tools", END: END})
    graph.add_edge("tools", "llm")

    return graph.compile(checkpointer=checkpointer)
