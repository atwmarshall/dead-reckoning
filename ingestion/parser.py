import ast
from pathlib import Path


def parse_file(path: str) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)

    functions = []
    classes = []
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "lineno": node.lineno,
                "docstring": ast.get_docstring(node),
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

        elif isinstance(node, ast.Import):
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
        "imports": list(dict.fromkeys(imports)),  # deduplicate, preserve order
    }


def _dotted(node: ast.Attribute) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))
