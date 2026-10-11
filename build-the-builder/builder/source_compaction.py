"""Reduce emitted Python whitespace without rewriting runtime behavior.

This runs only in the compiler. Strings, including whole Python 3.12 f-strings,
are copied from their original source spans. Every result must retain the exact
attribute-free Python AST and avoid Freya's template delimiters.
"""
import ast
from collections import Counter
import io
import json
import tokenize

from .inputs import InvalidInput


def _literal_tokens(source):
    """Treat nested 3.12 f-strings as one untouched lexical unit."""
    # Token rows follow physical newlines, not Unicode/form-feed separators
    # which splitlines() would also treat as line boundaries inside literals.
    offsets = [0] + [index + 1 for index, char in enumerate(source) if char == "\n"]
    stream = iter(tokenize.generate_tokens(io.StringIO(source).readline))
    for token in stream:
        if token.type != getattr(tokenize, "FSTRING_START", None):
            yield token
            continue
        depth = 1
        start = token.start
        for end in stream:
            if end.type == getattr(tokenize, "FSTRING_START", None):
                depth += 1
            elif end.type == getattr(tokenize, "FSTRING_END", None):
                depth -= 1
            if not depth:
                break
        else:
            raise InvalidInput("Unterminated f-string during runtime source compaction")
        first = offsets[start[0] - 1] + start[1]
        last = offsets[end.end[0] - 1] + end.end[1]
        yield tokenize.TokenInfo(tokenize.STRING, source[first:last], start, end.end, token.line)


def _needs_space(previous, current):
    if previous is None:
        return False
    words = {tokenize.NAME, tokenize.NUMBER, tokenize.STRING}
    if previous.type in words and current.type in words:
        if tokenize.STRING in {previous.type, current.type}:
            # Quotes usually separate a keyword from a string already. Verify
            # the exact two-token boundary, including prefixes/triple quotes.
            try:
                joined = [(token.type, token.string)
                          for token in _literal_tokens(previous.string + current.string)
                          if token.type not in {tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER}]
            except (SyntaxError, tokenize.TokenError, InvalidInput):
                return True
            return joined != [(previous.type, previous.string), (current.type, current.string)]
        return True
    if previous.type == tokenize.NUMBER and current.string.startswith("."):
        return True
    if previous.string.endswith(".") and current.type == tokenize.NUMBER:
        return True
    if previous.type == current.type == tokenize.OP:
        joined = previous.string + current.string
        return (joined in tokenize.EXACT_TOKEN_TYPES or joined in {"..", "{{", "}}"})
    return False


def _join_simple_lines(source):
    """Join only simple statements and suites containing only simple statements.

    The AST identifies simple-statement start rows; tokens identify logical
    lines. Physical newlines inside literals are part of a copied source span.
    Compound statements, decorators and clause boundaries remain separate.
    """
    tree = ast.parse(source)
    compound = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If,
                ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
                ast.Match, ast.Try) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
    simple = {node.lineno for node in ast.walk(tree)
              if isinstance(node, ast.stmt) and not isinstance(node, compound)}
    simple -= {node.lineno for node in ast.walk(tree) if isinstance(node, compound)}
    offsets = [0] + [index + 1 for index, char in enumerate(source) if char == "\n"]
    records, depth, start, previous, first_value = [], 0, None, None, None
    for token in _literal_tokens(source):
        if token.type == tokenize.INDENT:
            depth += 1
        elif token.type == tokenize.DEDENT:
            depth -= 1
        elif token.type in {tokenize.NL, tokenize.COMMENT, tokenize.ENDMARKER}:
            continue
        elif token.type == tokenize.NEWLINE:
            if start:
                first = offsets[start[0] - 1] + start[1]
                last = offsets[token.start[0] - 1] + token.start[1]
                is_simple = (start[0] in simple and
                             first_value not in {"elif", "else", "except", "finally", "case"})
                records.append(dict(depth=depth, text=source[first:last], simple=is_simple,
                                    header=previous.type == tokenize.OP and previous.string == ":"))
            start, previous, first_value = None, None, None
        else:
            if start is None:
                start = token.start
                first_value = token.string
            previous = token
    output, index = [], 0
    while index < len(records):
        item = records[index]
        end = index + 1
        if item["header"]:
            while end < len(records) and records[end]["depth"] > item["depth"]:
                end += 1
            body = records[index + 1:end]
            if body and all(row["simple"] and row["depth"] == item["depth"] + 1 for row in body):
                output.append(" " * item["depth"] + item["text"] +
                              ";".join(row["text"].removesuffix(";") for row in body))
                index = end
                continue
        end = index + 1
        if item["simple"]:
            while end < len(records) and records[end]["simple"] and records[end]["depth"] == item["depth"]:
                end += 1
        output.append(" " * item["depth"] +
                      ";".join(row["text"].removesuffix(";") for row in records[index:end]))
        index = end
    return "\n".join(output) + ("\n" if output else "")


def _pool_repeated_literals(source):
    """Factor repeated string values, then prove exact AST restoration.

    Only the compiler's closed runtime source is pooled. Strings used by
    annotations, f-strings, docstrings or literal patterns are left in place.
    The pool is an immutable tuple: constructing and reading it needs no calls.
    """
    tree = ast.parse(source)
    all_nodes = list(ast.walk(tree))
    # Adding a binding is observable to dynamic namespace inspection. Keep the
    # strict source unchanged if it ever gains such behavior or wildcard imports.
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
           node.func.id in {"globals", "locals", "vars", "eval", "exec", "dir"}
           for node in all_nodes):
        return source
    if any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
           for node in all_nodes):
        return source
    names = set()
    for node in all_nodes:
        for field in ("id", "arg", "name", "asname"):
            value = getattr(node, field, None)
            if isinstance(value, str):
                names.add(value.split(".", 1)[0])
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    name, suffix = "C", 0
    while name in names:
        name = "C" + str(suffix)
        suffix += 1

    def eligible(node):
        if isinstance(node, (ast.JoinedStr, ast.pattern)):
            return
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        if hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node
        for field, value in ast.iter_fields(node):
            if field in {"annotation", "returns"}:
                continue
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, ast.AST):
                    yield from eligible(child)

    nodes = list(eligible(tree))
    counts = Counter(node.value for node in nodes)
    raw = source.encode("utf-8")
    offsets = [0] + [index + 1 for index, value in enumerate(raw) if value == 10]
    literals, spans = {}, []
    for node in nodes:
        first = offsets[node.lineno - 1] + node.col_offset
        last = offsets[node.end_lineno - 1] + node.end_col_offset
        # AST columns are UTF-8 byte offsets, unlike tokenize's character columns.
        literals.setdefault(node.value, raw[first:last].decode("utf-8"))
        spans.append((first, last, node.value))
    cost = {value: len(json.dumps(literal, ensure_ascii=True).encode("utf-8")) - 2
            for value, literal in literals.items()}
    ordered = sorted((value for value, count in counts.items() if count > 1),
                     key=lambda value: (-(counts[value] * (cost[value] - len(name) - 3) - cost[value] - 1), value))
    values = []
    for value in ordered:
        reference_cost = len(name) + 2 + len(str(len(values)))
        if counts[value] * (cost[value] - reference_cost) > cost[value] + 1:
            values.append(value)
    if not values:
        return source
    indexes = {value: index for index, value in enumerate(values)}
    # Preserve a module docstring and any future imports in their mandated place.
    insertion = 0
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str):
        insertion = 1
    while insertion < len(tree.body) and isinstance(tree.body[insertion], ast.ImportFrom) and tree.body[insertion].module == "__future__":
        insertion += 1
    if insertion < len(tree.body):
        next_node = tree.body[insertion]
        decorators = getattr(next_node, "decorator_list", [])
        insert_at = (offsets[decorators[0].lineno - 1] if decorators else
                     offsets[next_node.lineno - 1] + next_node.col_offset)
    else:
        insert_at = len(raw)
    declaration = name + "=(" + ",".join(literals[value] for value in values) + ",)\n"
    edits = [(insert_at, insert_at, declaration.encode("utf-8"))]
    edits += [(first, last, (" " + name + "[" + str(indexes[value]) + "] ").encode("ascii"))
              for first, last, value in spans if value in indexes]
    chunks, previous = [], 0
    for first, last, replacement in sorted(edits, key=lambda item: (item[0], item[1])):
        if first < previous:
            raise InvalidInput("Overlapping literal-pool source ranges")
        chunks.extend((raw[previous:first], replacement))
        previous = last
    chunks.append(raw[previous:])
    pooled = compact_source(b"".join(chunks).decode("utf-8"))
    expanded = ast.parse(pooled)
    introduced = expanded.body.pop(insertion)
    if (not isinstance(introduced, ast.Assign) or len(introduced.targets) != 1 or
            not isinstance(introduced.targets[0], ast.Name) or introduced.targets[0].id != name or
            ast.literal_eval(introduced.value) != tuple(values)):
        raise InvalidInput("Runtime literal pool declaration changed")

    class ExpandPool(ast.NodeTransformer):
        def visit_Subscript(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == name:
                if not isinstance(node.slice, ast.Constant) or type(node.slice.value) is not int or not 0 <= node.slice.value < len(values):
                    raise InvalidInput("Runtime literal pool index is invalid")
                return ast.Constant(values[node.slice.value])
            return self.generic_visit(node)

    expanded = ExpandPool().visit(expanded)
    if any(isinstance(node, ast.Name) and node.id == name for node in ast.walk(expanded)):
        raise InvalidInput("Unexpanded runtime literal pool reference")
    if ast.dump(expanded, include_attributes=False) != ast.dump(tree, include_attributes=False):
        raise InvalidInput("Runtime literal pooling changed the restored Python AST")
    if len(json.dumps(pooled, ensure_ascii=True)) >= len(json.dumps(source, ensure_ascii=True)):
        return source
    return pooled


def compact_source(source, *, pool_literals=False):
    """Remove insignificant whitespace, with syntax/AST equality as a hard gate."""
    original = ast.dump(ast.parse(source), include_attributes=False)
    chunks, previous, depth, line_start = [], None, 0, True
    for token in _literal_tokens(source):
        if token.type in {tokenize.COMMENT, tokenize.NL, tokenize.ENDMARKER}:
            continue
        if token.type == tokenize.INDENT:
            depth += 1
            continue
        if token.type == tokenize.DEDENT:
            depth -= 1
            continue
        if token.type == tokenize.NEWLINE:
            if not line_start:
                chunks.append("\n")
            line_start, previous = True, None
            continue
        if line_start:
            chunks.append(" " * depth)
            line_start = False
        elif _needs_space(previous, token):
            chunks.append(" ")
        chunks.append(token.string)
        previous = token
    try:
        result = _join_simple_lines("".join(chunks))
        equivalent = ast.dump(ast.parse(result), include_attributes=False) == original
    except SyntaxError as exc:
        raise InvalidInput("Runtime compaction changed Python syntax") from exc
    if not equivalent:
        raise InvalidInput("Runtime compaction changed the Python AST")
    if "{{" in result or "}}" in result:
        raise InvalidInput("Runtime code contains unsafe template delimiters")
    return _pool_repeated_literals(result) if pool_literals else result
