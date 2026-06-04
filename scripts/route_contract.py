#!/usr/bin/env python3
"""Emit a static route contract for backend APIRouter modules.

This intentionally uses AST parsing instead of importing backend.main. Importing
the app initializes schema and queues, which is too side-effectful for a
contract baseline.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any


def _literal_str(node: ast.AST | None, default: str = "") -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return default


def _list_literal(node: ast.AST | None) -> list[str]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return [
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    return []


def _router_metadata(module: ast.Module) -> tuple[str, list[str]]:
    prefix = ""
    tags: list[str] = []
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "router" for target in node.targets):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Name) and func.id == "APIRouter"):
            continue
        for kw in node.value.keywords:
            if kw.arg == "prefix":
                prefix = _literal_str(kw.value, prefix)
            elif kw.arg == "tags":
                tags = _list_literal(kw.value)
    return prefix, tags


def _join_path(prefix: str, path: str) -> str:
    if not prefix:
        return path or "/"
    if not path:
        return prefix
    return f"{prefix.rstrip('/')}/{path.lstrip('/')}"


def _decorator_route(decorator: ast.AST) -> tuple[str, str] | None:
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "router"
    ):
        return None
    method = func.attr.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        return None
    path = _literal_str(decorator.args[0], "") if decorator.args else ""
    return method, path


def _annotation(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return ast.unparse(node)


def _default(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return ast.unparse(node)


def _argument_contract(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, Any]]:
    positional = [*node.args.posonlyargs, *node.args.args]
    defaults: list[ast.AST | None] = [None] * (len(positional) - len(node.args.defaults))
    defaults.extend(node.args.defaults)
    args: list[dict[str, Any]] = []
    for arg, default in zip(positional, defaults):
        args.append(
            {
                "name": arg.arg,
                "kind": "positional",
                "annotation": _annotation(arg.annotation),
                "default": _default(default),
            }
        )
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        args.append(
            {
                "name": arg.arg,
                "kind": "keyword_only",
                "annotation": _annotation(arg.annotation),
                "default": _default(default),
            }
        )
    if node.args.vararg:
        args.append(
            {
                "name": node.args.vararg.arg,
                "kind": "vararg",
                "annotation": _annotation(node.args.vararg.annotation),
                "default": None,
            }
        )
    if node.args.kwarg:
        args.append(
            {
                "name": node.args.kwarg.arg,
                "kind": "kwarg",
                "annotation": _annotation(node.args.kwarg.annotation),
                "default": None,
            }
        )
    return args


def _decorator_kwargs(decorator: ast.AST) -> dict[str, str]:
    if not isinstance(decorator, ast.Call):
        return {}
    return {
        str(keyword.arg): ast.unparse(keyword.value)
        for keyword in decorator.keywords
        if keyword.arg is not None
    }


def _routes_for_file(path: Path, root: Path) -> list[dict[str, Any]]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    prefix, tags = _router_metadata(module)
    routes: list[dict[str, Any]] = []
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            route = _decorator_route(decorator)
            if route is None:
                continue
            method, route_path = route
            routes.append(
                {
                    "method": method,
                    "path": _join_path(prefix, route_path),
                    "router_prefix": prefix,
                    "route_path": route_path,
                    "route_kwargs": _decorator_kwargs(decorator),
                    "endpoint": node.name,
                    "arguments": _argument_contract(node),
                    "returns": _annotation(node.returns),
                    "source": str(path.relative_to(root)),
                    "line": node.lineno,
                    "tags": tags,
                }
            )
    return routes


def build_contract(root: Path) -> dict[str, Any]:
    route_dir = root / "backend" / "routes"
    routes: list[dict[str, Any]] = []
    for path in sorted(route_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        routes.extend(_routes_for_file(path, root))
    routes.sort(key=lambda item: (item["path"], item["method"], item["endpoint"]))
    return {"route_count": len(routes), "routes": routes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    contract = build_contract(root)
    rendered = json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = args.output
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
