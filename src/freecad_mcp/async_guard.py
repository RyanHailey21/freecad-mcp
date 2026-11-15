"""Static guard for ``execute_code_async``: GUI/document work outside ``commit()``.

TechDraw document work run from the async thread hung FreeCAD (light_weight_arm audit, 3.3): pages,
views and their Qt scene live on the GUI thread.  The guard parses the submitted code and looks at
everything that is NOT inside a function handed to ``commit(fn)`` (or a lambda inside the call):

* TechDraw / TechDrawGui / FreeCADGui use there is REFUSED;
* document mutation there (addObject, removeObject, recompute, save/saveAs, newDocument,
  openDocument, closeDocument, setting a property on a document object is not detectable) is reported
  as a warning, prepended to the reply.
"""

from __future__ import annotations

import ast

GUI_MODULES = {"TechDraw", "TechDrawGui", "FreeCADGui"}
MUTATORS = {"addObject", "removeObject", "recompute", "save", "saveAs", "newDocument", "openDocument",
            "closeDocument", "addView", "touch", "exportPageAsPdf", "exportPageAsSvg"}


def _committed(tree: ast.AST) -> tuple[set[str], set[int]]:
    """Names of functions passed to commit(...) and ids of lambda nodes passed to it."""
    names, lambdas = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "commit":
            for a in node.args[:1]:
                if isinstance(a, ast.Name):
                    names.add(a.id)
                elif isinstance(a, ast.Lambda):
                    lambdas.add(id(a))
    return names, lambdas


def check(code: str) -> dict[str, list[str]]:
    """{"refuse": [...], "warn": [...]} for code meant for execute_code_async."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"refuse": [], "warn": ["code does not parse: %s" % exc]}
    names, lambdas = _committed(tree)
    refuse: list[str] = []
    warn: list[str] = []

    def visit(node: ast.AST, inside: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            inside = True
        if isinstance(node, ast.Lambda) and id(node) in lambdas:
            inside = True
        if not inside:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                    [(node.module or "").split(".")[0]]
                for m in mods:
                    if m in GUI_MODULES:
                        refuse.append("line %d: imports %s outside commit()" % (node.lineno, m))
            elif isinstance(node, ast.Name) and node.id in GUI_MODULES:
                refuse.append("line %d: uses %s outside commit()" % (node.lineno, node.id))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("TechDraw::"):
                refuse.append("line %d: creates %s outside commit()" % (node.lineno, node.value))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS:
                warn.append("line %d: .%s() outside commit() mutates the document from the async thread"
                            % (node.lineno, node.func.attr))
        for child in ast.iter_child_nodes(node):
            visit(child, inside)

    visit(tree, False)
    return {"refuse": sorted(set(refuse), key=refuse.index), "warn": sorted(set(warn), key=warn.index)}


def refusal_text(res: dict[str, list[str]]) -> str:
    return ("REFUSED: execute_code_async must not touch TechDraw or the GUI from its background thread "
            "(it can wedge FreeCAD until a restart).\n  " + "\n  ".join(res["refuse"]) +
            "\nRun TechDraw work with execute_code (or the techdraw_* tools), or wrap it in a function "
            "passed to commit(fn). Pass allow_gui=True only if you are sure.")
