import ast
from pathlib import Path


def parse_file(path: str) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)

    functions = []
    classes = []
    imports = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "lineno": node.lineno,
                "docstring": ast.get_docstring(node),
                "class_name": None,
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


def _dotted(node: ast.Attribute) -> str:
    parts = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


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
