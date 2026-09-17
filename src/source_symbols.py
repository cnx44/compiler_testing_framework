from dataclasses import dataclass
from importlib import import_module
from pathlib import Path


class SourceAnalysisError(ValueError):
    pass


@dataclass(frozen=True)
class FunctionSymbols:
    name: str
    line: int
    variables: tuple[str, ...]


@dataclass(frozen=True)
class SourceSymbols:
    functions: tuple[FunctionSymbols, ...]
    globals: tuple[str, ...]
    warnings: tuple[str, ...]


def _walk(node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _name(node):
    if node is None:
        return None
    if node.type == "identifier":
        return node.text.decode("utf-8")
    return _name(node.child_by_field_name("declarator"))


def _bindings(node, rust):
    if rust:
        if node.type not in {"let_declaration", "parameter", "static_item", "const_item"}:
            return []
        pattern = node.child_by_field_name("pattern") or node.child_by_field_name("name")
        # Complex destructuring is deliberately left to manual input.
        return [pattern.text.decode()] if pattern and pattern.type == "identifier" else []
    if node.type not in {"declaration", "parameter_declaration"}:
        return []
    names = []
    for declarator in node.children_by_field_name("declarator"):
        if any(n.type == "function_declarator" for n in _walk(declarator)):
            continue
        name = _name(declarator)
        if name:
            names.append(name)
    return names


def analyze_source(path: str | Path) -> SourceSymbols:
    path = Path(path)
    language = {".c": "c", ".cpp": "cpp", ".rs": "rust"}.get(path.suffix.lower())
    if language is None:
        raise SourceAnalysisError(f"Unsupported source: {path}")
    try:
        from tree_sitter import Language, Parser
        grammar = import_module(f"tree_sitter_{language}")
        tree = Parser(Language(grammar.language())).parse(path.read_bytes())
    except (ImportError, OSError, ValueError, TypeError) as error:
        raise SourceAnalysisError(f"Cannot analyze {path}: {error}. Install requirements.txt or use manual input.") from error
    if tree.root_node.has_error:
        raise SourceAnalysisError(f"Syntax errors or unsupported syntax in {path}; use manual input.")
    rust = language == "rust"
    functions, globals_, warnings = [], [], []

    def visit(node):
        if node.type in {"function_definition", "function_item"}:
            name = _name(node.child_by_field_name("name") if rust else node.child_by_field_name("declarator"))
            if name:
                variables = []
                def locals_in(part):
                    if part != node and part.type in {"function_definition", "function_item", "lambda_expression", "closure_expression"}:
                        return
                    variables.extend(_bindings(part, rust))
                    for child in part.named_children:
                        locals_in(child)
                locals_in(node)
                functions.append(FunctionSymbols(name, node.start_point.row + 1, tuple(dict.fromkeys(variables))))
            else:
                warnings.append(f"Qualified/complex function at line {node.start_point.row + 1}: manual input required.")
            return
        if node.type in {"namespace_definition", "class_specifier", "struct_specifier", "impl_item", "trait_item", "mod_item", "template_declaration"}:
            warnings.append(f"Scoped or generic symbols at line {node.start_point.row + 1} omitted; use manual input.")
            return
        globals_.extend(_bindings(node, rust))
        for child in node.named_children:
            visit(child)

    visit(tree.root_node)
    counts = {f.name: sum(other.name == f.name for other in functions) for f in functions}
    if any(count > 1 for count in counts.values()):
        warnings.append("Overloaded function names omitted: debugger signatures require manual configuration.")
    return SourceSymbols(tuple(f for f in functions if counts[f.name] == 1),
                         tuple(dict.fromkeys(globals_)), tuple(dict.fromkeys(warnings)))
