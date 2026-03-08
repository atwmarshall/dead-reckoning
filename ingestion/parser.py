import ast
import hashlib
from pathlib import Path

from langsmith import traceable


@traceable(name="parse_file", run_type="chain")
def parse_file(path: str) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)

    functions = []
    classes = []
    imports = []

    source_lines = source.splitlines()

    def _source_hash(node):
        segment = ast.get_source_segment(source, node)
        if segment is None:
            segment = "\n".join(source_lines[node.lineno - 1:node.end_lineno])
        return hashlib.sha256(segment.encode()).hexdigest()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "lineno": node.lineno,
                "docstring": ast.get_docstring(node),
                "class_name": None,
                "calls": _extract_calls(node),
                "source_hash": _source_hash(node),
            })

        elif isinstance(node, ast.ClassDef):
            bases = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(_dotted(base))
            classes.append({
                "name": node.name,
                "lineno": node.lineno,
                "bases": bases,
            })
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append({
                        "name": child.name,
                        "lineno": child.lineno,
                        "docstring": ast.get_docstring(child),
                        "class_name": node.name,
                        "calls": _extract_calls(child),
                        "source_hash": _source_hash(child),
                    })

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    return {
        "path": path,
        "line_count": len(source.splitlines()),
        "functions": functions,
        "classes": classes,
        "imports": list(dict.fromkeys(imports)),
    }


def _extract_calls(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    calls = []
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                calls.append(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                calls.append(n.func.attr)
    return list(dict.fromkeys(calls))


def _dotted(node: ast.Attribute) -> str:
    parts = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


@traceable(name="parse_repo", run_type="chain")
def parse_repo(repo_path: str) -> list[dict]:
    skip = {"__pycache__", ".git", "venv", ".venv", "node_modules", ".tox"}
    root = Path(repo_path)
    results = []
    for py_file in sorted(root.rglob("*.py")):
        if any(part in skip for part in py_file.parts):
            continue
        try:
            results.append(parse_file(str(py_file)))
        except SyntaxError:
            pass
    return results
