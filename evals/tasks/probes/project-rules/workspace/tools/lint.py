"""The project's lint: every function in textkit/ has type hints and a docstring. Prints "lint ok" or what is missing."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def problems() -> list[str]:
    found = []
    for file in sorted((ROOT / "textkit").glob("*.py")):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            where = f"{file.relative_to(ROOT).as_posix()}:{node.lineno} {node.name}"
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.returns is None or any(argument.annotation is None for argument in arguments):
                found.append(f"{where}: type hints are missing")
            if ast.get_docstring(node) is None:
                found.append(f"{where}: the docstring is missing")
    return found


if __name__ == "__main__":
    missing = problems()
    print("\n".join(missing) if missing else "lint ok")
    sys.exit(1 if missing else 0)
