from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
import javalang
import javalang.tree as jtree

from spec_harness.harness import (
    MethodSpec,
    InputCase,
    JType,
    java_comments,
    mask_comments_and_literals,
    strip_jml_inner_comments,
)

# JML extraction from LLM-generated Java source


def _find_method_line(source: str, method_name: str | None = None) -> int | None:
    """
    Use javalang to parse *source* and return the 1-based line number of the
    target MethodDeclaration, relative to the original *source*: `method_name`
    if given, else `solve` if present, else the first method (helper methods
    may precede the target).
    """
    lines = source.splitlines()
    # Count leading blank lines that .strip() would remove
    leading_blanks = 0
    for line in lines:
        if line.strip():
            break
        leading_blanks += 1

    stripped = source.strip()
    has_class = bool(re.search(r"\bclass\s+\w+", stripped))
    if has_class:
        to_parse = stripped
        offset = leading_blanks
    else:
        to_parse = f"class _Wrapper {{\n{stripped}\n}}"
        offset = leading_blanks - 1  # wrapper adds 1 line

    try:
        tree = javalang.parse.parse(to_parse)
    except javalang.parser.JavaSyntaxError:
        return None

    methods = [n for _, n in tree if isinstance(n, jtree.MethodDeclaration) and n.position]
    wanted = method_name or ("solve" if any(m.name == "solve" for m in methods) else None)
    for node in methods:
        if wanted is None or node.name == wanted:
            return node.position.line + offset
    return None


def extract_jml_spec(source: str, method_name: str | None = None) -> MethodSpec:
    """
    Extract the method's JML specification.

    The specification is every JML annotation comment between the end of the previous class member (or
    the class's `{`) and the method's name: `//@ ..` line annotations and `/*@ .. @*/` block annotations,
    whatever blank lines, Java annotations or ordinary comments lie in between. Ordinary comments (`//`,
    `/* */`, Javadoc `/** */`, including a Javadoc `@requires` tag) are not JML and are ignored, as
    OpenJML ignores them; comments nested inside a JML annotation (`//@ requires x > 0; // note`) are
    removed. The result is returned as normalized `//@ ..` lines (one per annotation line) in a
    ``MethodSpec``; `helpers` is the source of all other class members (the JML region and the method
    excluded).
    """
    lines = source.splitlines()
    region = _jml_region(source, method_name)
    if region is not None:
        r_start, r_end, body_open, meth_close, body_close = region
        anns = [(s, e, k) for s, e, k in java_comments(source) if r_start <= s < r_end]
    else:                                           # method not found: every JML annotation of the file
        anns = java_comments(source)
    jml_block = "\n".join(_jml_lines(source, anns))
    helpers = ""
    if region is not None:
        members = (source[body_open + 1: r_start] + "\n" + source[meth_close + 1: body_close]).strip("\n")
        helpers = members if members.strip() else ""
    imports = "\n".join(l.strip() for l in lines
                        if re.match(r"^\s*import\s+(static\s+)?[\w.]+(\.\*)?\s*;", l))
    return MethodSpec(jml_block=jml_block, helpers=helpers, imports=imports)


def _jml_lines(source: str, comments) -> list[str]:
    """Normalized `//@ ..` lines of the JML annotations among `comments` (ordinary comments skipped)."""
    out = []
    for s, e, kind in comments:
        if kind == "line":
            content = source[s + 2:e]
            if not content.startswith("@"):
                continue                               # `// ..`: an ordinary comment
            inner = [content.lstrip("@")]
        else:
            content = source[s + 2:e - 2] if source[s:e].endswith("*/") else source[s + 2:e]
            if not content.startswith("@"):
                continue                               # `/* ..*/` or Javadoc `/** ..*/`
            inner = content.lstrip("@").rstrip("@").splitlines()
        for line in strip_jml_inner_comments("\n".join(inner)).splitlines():
            line = re.sub(r"^\s*@+", "", line).strip()
            if line:
                out.append("//@ " + line)
    return out


def _jml_region(source: str, method_name: str | None):
    """
    Offsets for the target method: (region start, region end, class-body `{`, method-body `}`,
    class-body `}`). The region runs from just after the previous member (the last `;`, `{` or `}` of code
    before the method) to the method's name; comments and literals are ignored when looking for code.
    None if the method or its class cannot be located.
    """
    line = _find_method_line(source, method_name)
    if line is None:
        return None
    code = mask_comments_and_literals(source)
    offs = [0]
    for l in source.splitlines(keepends=True):
        offs.append(offs[-1] + len(l))
    if line - 1 >= len(offs):
        return None
    name = method_name or "solve"
    m = re.compile(r"\b" + re.escape(name) + r"\s*\(").search(code, offs[line - 1])
    if not m:
        m = re.compile(r"\b\w+\s*\(").search(code, offs[line - 1])
        if not m:
            return None
    name_at = m.start()
    prev = max(code.rfind(ch, 0, name_at) for ch in ";{}")
    classes = list(re.finditer(r"\bclass\s+\w+", code[:name_at]))
    if prev < 0 or not classes:
        return None
    body_open = _first_code_brace(code, classes[-1].end())
    body_close = _match_brace(code, body_open) if body_open >= 0 else -1
    meth_open = _first_code_brace(code, m.end())
    meth_close = _match_brace(code, meth_open) if meth_open >= 0 else -1
    if min(body_open, body_close, meth_open, meth_close) < 0:
        return None
    return prev + 1, name_at, body_open, meth_close, body_close


def _match_brace(text: str, open_idx: int) -> int:
    """Index of the brace closing text[open_idx] == '{', skipping comments and
    string/char literals; -1 if unbalanced."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        ch = text[i]
        if ch in ('"', "'"):
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _first_code_brace(text: str, start: int) -> int:
    """First '{' at or after `start` that is not inside a comment or literal."""
    i, n = start, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if text[i] in ('"', "'"):
            j = i + 1
            while j < n and text[j] != text[i]:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if text[i] == "{":
            return i
        i += 1
    return -1


def _other_members(lines: list[str], spec_start: int, method_idx: int) -> str:
    """
    Source text of every class member except the target method and its JML
    block (lines[spec_start] .. end of the method body). Helper methods that the
    spec calls are needed inside the stub classes, or the stub will not compile.
    """
    text = "\n".join(lines)
    offs = [0]
    for l in lines:
        offs.append(offs[-1] + len(l) + 1)
    m = re.search(r"\bclass\s+\w+[^{]*", text)
    if not m:
        return ""
    body_open = _first_code_brace(text, m.end())
    body_close = _match_brace(text, body_open) if body_open >= 0 else -1
    meth_open = _first_code_brace(text, offs[method_idx])
    meth_close = _match_brace(text, meth_open) if meth_open >= 0 else -1
    if min(body_open, body_close, meth_open, meth_close) < 0:
        return ""
    before = text[body_open + 1: offs[spec_start]]
    after = text[meth_close + 1: body_close]
    members = (before + "\n" + after).strip("\n")
    return members if members.strip() else ""


# Test.java input-format detection
@dataclass
class ReadOp:
    """One input-reading operation derived from Test.java."""

    name: str  # param name (from benchmark Solution.java)
    jtype: JType
    mode: str  # scalar | grouped_scalar | string |
    # array_direct | array_sized |
    # array_2d | array_2d_rc |
    # map | set | list_direct
    group_idx: int = -1  # index within the grouped-scalars line
    # array_2d_rc: whether Test.java reads the r row lines when c == 0 (FB: always; SGB: only if c > 0)
    rows_when_no_cols: bool = True


# javalang AST helpers for Test.java analysis


def _walk_ast(node):
    """Yield all javalang AST nodes in the subtree rooted at *node*."""
    if node is None or not isinstance(node, jtree.Node):
        return
    yield node
    for attr_name in node.attrs:
        child = getattr(node, attr_name, None)
        if child is None:
            continue
        if isinstance(child, jtree.Node):
            yield from _walk_ast(child)
        elif isinstance(child, (list, tuple)):
            for item in child:
                if isinstance(item, jtree.Node):
                    yield from _walk_ast(item)


def _is_string_array_decl(type_node, declarator) -> bool:
    """Check whether a LocalVariableDeclaration declares a String[]."""
    if not isinstance(type_node, jtree.ReferenceType):
        return False
    if type_node.name != "String":
        return False
    # Dimensions can live on the type or on the declarator
    if type_node.dimensions and len(type_node.dimensions) > 0:
        return True
    if declarator.dimensions and len(declarator.dimensions) > 0:
        return True
    return False


def _ast_has_method_call(node, method_name: str) -> bool:
    """True if the AST subtree contains a MethodInvocation of *method_name*."""
    for n in _walk_ast(node):
        if isinstance(n, jtree.MethodInvocation) and n.member == method_name:
            return True
    return False


def _ast_find_member_ref(node) -> str | None:
    """
    If *node* is (or directly wraps) a simple MemberReference with no
    selectors, return the member name.  Used to detect ``solve(p1, p0)``
    style argument reordering.
    """
    for n in _walk_ast(node):
        if isinstance(n, jtree.MemberReference) and not n.selectors:
            return n.member
    return None


def _ast_find_array_access(node) -> tuple[str, int] | None:
    """
    Search the AST subtree for an array-index pattern like ``arr[idx]``
    and return ``(array_variable_name, integer_index)``, or ``None``.

    Handles:
      - ``Integer.parseInt(input[0])``   → ("input", 0)
      - ``input[2].charAt(0)``           → ("input", 2)
    """
    for n in _walk_ast(node):
        if isinstance(n, jtree.MemberReference) and n.selectors:
            for sel in n.selectors:
                if isinstance(sel, jtree.ArraySelector):
                    idx_node = sel.index
                    if isinstance(idx_node, jtree.Literal):
                        try:
                            return (n.member, int(idx_node.value))
                        except (ValueError, TypeError):
                            pass
    return None


def _javalang_detect(
    test_src: str, all_param_names: set[str], params: list[dict]
) -> tuple[dict[str, int], bool, bool, dict[str, str]]:
    """
    Parse *test_src* with javalang and return
    ``(grouped_indices, has_2d_rc, has_sized_1d, input_order_map)``.

    ``input_order_map`` maps Solution.java param names to the Test.java
    pN variable names that hold their values.  This is needed when
    Test.java reads inputs in a different order than Solution.java
    declares its parameters (e.g. ``solve(p1, p0)``).

    Walks the ``main()`` body's top-level ``LocalVariableDeclaration``
    nodes to find:
      1. ``String[]`` split-variables initialised from
         ``scanner.nextLine().split(…)``
      2. Parameter variables whose initialiser indexes into one of those
         split-variables (→ grouped-scalar pattern).
      3. 2-D array header splits (variable names starting with ``rc`` /
         ``_rc``).
      4. Inline array-indexing inside the ``solve()`` call (e.g.
         ``solve(Long.parseLong(tok[0]), …)``).
    """
    grouped_indices: dict[str, int] = {}
    has_2d_rc = False
    has_sized_1d = False
    input_order_map: dict[str, str] = {}  # Solution param name → pN name

    # javalang targets Java 8 and may choke on `var`; replace with Object.
    parseable = re.sub(r"\bvar\s+(\w+)\s*=", r"Object \1 =", test_src)
    tree = javalang.parse.parse(parseable)

    # Locate main() body
    main_body = None
    for _, node in tree:
        if isinstance(node, jtree.MethodDeclaration) and node.name == "main":
            main_body = node.body
            break
    if not main_body:
        return grouped_indices, has_2d_rc, has_sized_1d, input_order_map

    split_vars: set[str] = set()  # String[] vars from scanner split
    rc_vars: set[str] = set()  # subset that are 2-D headers

    for stmt in main_body:
        if not isinstance(stmt, jtree.LocalVariableDeclaration):
            continue
        for decl in stmt.declarators:
            vname = decl.name
            init = decl.initializer

            # ---- String[] from scanner.nextLine().…split(…) ----
            if (
                _is_string_array_decl(stmt.type, decl)
                and init is not None
                and _ast_has_method_call(init, "split")
            ):
                split_vars.add(vname)
                if vname.startswith(("_rc", "rc")):
                    rc_vars.add(vname)
                    has_2d_rc = True
                continue

            # ---- param variable indexing into a split array ----
            if vname in all_param_names and init is not None:
                arr_access = _ast_find_array_access(init)
                if arr_access is not None:
                    arr_name, idx = arr_access
                    if arr_name in split_vars and arr_name not in rc_vars:
                        grouped_indices[vname] = idx

    # ---- analyse solve() call for inline parsing & param reorder ----
    non_rc_splits = split_vars - rc_vars
    # Only check inline indexing if no grouped params were found via
    # variable declarations (avoids double-counting).
    check_inline = bool(non_rc_splits) and not grouped_indices
    for stmt in main_body:
        for n in _walk_ast(stmt):
            if not (
                isinstance(n, jtree.MethodInvocation)
                and n.member == "solve"
                and n.arguments
            ):
                continue
            for arg_idx, arg in enumerate(n.arguments):
                if arg_idx >= len(params):
                    break
                sol_name = params[arg_idx]["name"]

                # Inline array indexing: solve(Long.parseLong(tok[0]), …)
                if check_inline:
                    arr_access = _ast_find_array_access(arg)
                    if arr_access is not None and arr_access[0] in non_rc_splits:
                        grouped_indices[sol_name] = arr_access[1]

                # Detect param reorder: solve(p1, p0)
                ref = _ast_find_member_ref(arg)
                if ref is not None and ref in all_param_names:
                    input_order_map[sol_name] = ref

    return grouped_indices, has_2d_rc, has_sized_1d, input_order_map


def _is_nested_collection(inner_type_str: str) -> bool:
    """Check if a generic argument string represents a List or array."""
    return inner_type_str.startswith(
        ("List", "ArrayList", "LinkedList")
    ) or inner_type_str.endswith("[]")


def _leaf_element_type(jtype: JType) -> str:
    """
    Return the innermost scalar type name for array or nested-collection
    types so that ``_parse_scalar`` receives a usable type.

    Examples:
      int[][]            → "int"
      List<Integer>      → "Integer"
      List<List<Integer>> → "Integer"
      List<int[]>        → "int"
    """
    if jtype.is_array:
        return jtype.base
    if jtype.is_collection and jtype.generic_args:
        inner = jtype.generic_args[0]
        # Nested generic: List<Integer> → extract "Integer"
        m = re.match(r"^(\w+)<(.+)>$", inner)
        if m:
            inner_base = m.group(1)
            if inner_base in (
                "List",
                "ArrayList",
                "LinkedList",
                "Set",
                "HashSet",
                "TreeSet",
            ):
                return m.group(2).strip()
            return inner
        # Array element: int[] → "int"
        if inner.endswith("[]"):
            return inner[:-2]
        return inner
    return jtype.base


def _regex_fallback(
    test_src: str, all_param_names: set[str]
) -> tuple[dict[str, int], bool, bool]:
    """
    Regex-based detection as a fallback when javalang cannot parse
    the Test.java source.
    """
    grouped_indices: dict[str, int] = {}

    split_var_names = re.findall(
        r"String\[\]\s+(\w+)\s*=\s*scanner\."
        r"(?:hasNextLine\s*\(\)\s*\?\s*)?nextLine\(\).*?split\s*\(",
        test_src,
    )
    for sv in split_var_names:
        # Skip 2-D array headers, row-parsers, boolean-array raw vars
        if re.match(r"_?rc|_?pr|_raw_", sv):
            continue
        # Type.parseX(splitVar[idx])
        for m in re.finditer(
            rf"(\w+)\s*=\s*\w+\.parse\w+\(\s*{re.escape(sv)}"
            rf"\s*\[\s*(\d+)\s*\]\s*\)",
            test_src,
        ):
            grouped_indices[m.group(1)] = int(m.group(2))
        # splitVar[idx].charAt(0)  (char params)
        for m in re.finditer(
            rf"(\w+)\s*=\s*{re.escape(sv)}"
            rf"\s*\[\s*(\d+)\s*\]\.charAt\s*\(\s*0\s*\)",
            test_src,
        ):
            grouped_indices[m.group(1)] = int(m.group(2))
        # splitVar[idx]  (String params in a grouped line)
        for m in re.finditer(
            rf"(?:String\s+)?(\w+)\s*=\s*{re.escape(sv)}" rf"\s*\[\s*(\d+)\s*\]\s*;",
            test_src,
        ):
            if m.group(1) in all_param_names:
                grouped_indices[m.group(1)] = int(m.group(2))

    has_2d_rc = bool(
        re.search(
            r"_?rc[\w]*\s*=\s*scanner\." r"(?:hasNextLine\s*\(\)\s*\?\s*)?nextLine",
            test_src,
        )
    )
    has_sized_1d = bool(
        re.search(r"int\s+n\d+\s*=\s*Integer\.parseInt\(scanner\.nextLine", test_src)
    )

    return grouped_indices, has_2d_rc, has_sized_1d


def detect_input_format(test_src: str, params: list[dict]) -> list[ReadOp]:
    """
    Analyse machine-generated Test.java to decide how each parameter is
    read from stdin.  Returns one ``ReadOp`` per method parameter, in the
    order the inputs are actually read (which may differ from the
    Solution.java parameter order when Test.java reorders arguments
    in the ``solve()`` call).

    Primary detection uses javalang to parse the AST of Test.java; falls
    back to regex when javalang cannot parse the source.
    """
    ops: list[ReadOp] = []

    param_names = {p["name"] for p in params}
    positional_names = {f"p{i}" for i in range(len(params))}
    all_param_names = param_names | positional_names

    # --- detect grouped-scalars, 2-D headers, sized-1-D via javalang ------
    input_order_map: dict[str, str] = {}
    try:
        grouped_indices, has_2d_rc, has_sized_1d, input_order_map = _javalang_detect(
            test_src, all_param_names, params
        )
    except Exception:
        grouped_indices, has_2d_rc, has_sized_1d = _regex_fallback(
            test_src, all_param_names
        )

    # --- determine reading order ------------------------------------------
    # If Test.java calls solve(p1, p0) instead of solve(p0, p1), we need
    # to emit ReadOps in the Test.java input order (p0, p1, …), not the
    # Solution.java parameter order.
    ordered_params = list(params)
    if input_order_map:
        # Build pN → Solution param mapping
        pn_to_sol: dict[str, dict] = {}
        for sol_name, pn_name in input_order_map.items():
            for p in params:
                if p["name"] == sol_name:
                    pn_to_sol[pn_name] = p
                    break
        if len(pn_to_sol) == len(params):
            # Sort by pN index to get input order
            ordered_params = [
                pn_to_sol[f"p{i}"] for i in range(len(params)) if f"p{i}" in pn_to_sol
            ]
            if len(ordered_params) != len(params):
                ordered_params = list(params)  # fallback

    # SGB tests read the rows of an r x c matrix in `for (i < r && c > 0)`: no row lines when c == 0
    rows_when_no_cols = not re.search(r"<\s*\w+\s*&&\s*\w+\s*>\s*0", test_src or "")

    # --- build ReadOps in input order -------------------------------------
    for pi, p in enumerate(ordered_params):
        jtype = p["jtype"]
        name = p["name"]
        tvar = f"p{pi}"  # specbench / some formal use p0, p1 …

        # --- HashMap / TreeMap ---
        if jtype.base in ("HashMap", "Map", "TreeMap"):
            ops.append(ReadOp(name, jtype, "map"))
            continue

        # --- HashSet / TreeSet ---
        if jtype.base in ("HashSet", "Set", "TreeSet"):
            ops.append(ReadOp(name, jtype, "set"))
            continue

        # --- List / ArrayList / LinkedList ---
        if jtype.base in ("List", "ArrayList", "LinkedList"):
            # Nested list (e.g. List<List<Integer>>) → 2D-style input
            if jtype.generic_args and _is_nested_collection(jtype.generic_args[0]):
                mode = "array_2d_rc" if has_2d_rc else "array_2d"
                ops.append(ReadOp(name, jtype, mode, rows_when_no_cols=rows_when_no_cols))
            else:
                ops.append(ReadOp(name, jtype, "list_direct"))
            continue

        # --- 2-D+ array ---
        if jtype.is_array and jtype.array_dims >= 2:
            mode = "array_2d_rc" if has_2d_rc else "array_2d"
            ops.append(ReadOp(name, jtype, mode, rows_when_no_cols=rows_when_no_cols))
            continue

        # --- char[] read as one line (Test.java: line.toCharArray()) ---
        if jtype.is_array and jtype.array_dims == 1 and jtype.base in ("char", "Character") \
                and re.search(r"toCharArray\s*\(", test_src or ""):
            ops.append(ReadOp(name, jtype, "char_array"))
            continue

        # --- 1-D array ---
        if jtype.is_array:
            mode = "array_sized" if has_sized_1d else "array_direct"
            ops.append(ReadOp(name, jtype, mode))
            continue

        # --- String ---
        if jtype.base == "String":
            ops.append(ReadOp(name, jtype, "string"))
            continue

        # --- scalar (possibly grouped) ---
        gidx = grouped_indices.get(name, grouped_indices.get(tvar, -1))
        if gidx >= 0:
            ops.append(ReadOp(name, jtype, "grouped_scalar", group_idx=gidx))
        else:
            ops.append(ReadOp(name, jtype, "scalar"))

    return ops


# Scalar value parser


def _java_split(line: str) -> list[str]:
    """Java's `line.trim().split("\\s+")`: an empty (or blank) line gives [""], not []."""
    t = line.strip()
    return t.split() if t else [""]


def _parse_scalar(type_name: str, s: str, raw: bool = False) -> Any:
    """Convert a string token to a typed Python value (raw: a String token is kept exactly)."""
    if raw and type_name == "String":
        return s
    s = s.strip()
    if type_name in ("int", "Integer", "short", "Short", "byte", "Byte"):
        return int(s)
    if type_name in ("long", "Long"):
        return int(s)
    if type_name in ("double", "Double"):
        return float(s)
    if type_name in ("float", "Float"):
        return float(s)
    if type_name in ("boolean", "Boolean"):
        return s.lower() == "true"
    if type_name in ("char", "Character"):
        # Test.java reads a char as the first character of the first token (scanner.next() /
        # split("\\s+")), and ' ' when there is no token
        return s[0] if s else " "
    # String and everything else
    return s


# Validated invalid inputs (testset T6): JSON-tagged values -> harness values

def decode_tagged(value: Any) -> Any:
    """Decode the JSON tags used for T6 invalid inputs: {"__map__": [[k, v], ...]} -> dict,
    {"__float__": "nan" | "inf" | "-inf"} -> float; lists recursively; everything else as is."""
    if isinstance(value, dict):
        if "__map__" in value:
            return {decode_tagged(k): decode_tagged(v) for k, v in value["__map__"]}
        if "__float__" in value:
            return float(value["__float__"])
        return {k: decode_tagged(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_tagged(v) for v in value]
    return value


# Input string → parameter dict


def parse_input(input_str: str, ops: list[ReadOp]) -> dict[str, Any]:
    """
    Parse a competitive-programming-style input string into a dict
    keyed by parameter name, guided by the ReadOps from Test.java.
    """
    lines = input_str.split("\n")
    pos = 0
    result: dict[str, Any] = {}

    i = 0
    while i < len(ops):
        op = ops[i]

        # ---- grouped scalars (consume one line for all) ------------------
        if op.mode == "grouped_scalar":
            parts = lines[pos].strip().split()
            pos += 1
            j = i
            while j < len(ops) and ops[j].mode == "grouped_scalar":
                g = ops[j]
                result[g.name] = _parse_scalar(g.jtype.base, parts[g.group_idx])
                j += 1
            i = j
            continue

        # ---- single scalar -----------------------------------------------
        if op.mode == "scalar":
            result[op.name] = _parse_scalar(op.jtype.base, lines[pos].strip())
            pos += 1

        # ---- char[] from one line ----------------------------------------
        elif op.mode == "char_array":
            result[op.name] = list(lines[pos]) if pos < len(lines) else []
            pos += 1

        # ---- string -------------------------------------------------------
        elif op.mode == "string":
            result[op.name] = lines[pos]
            pos += 1

        # ---- 1-D array, no size prefix ------------------------------------
        elif op.mode == "array_direct":
            parts = _java_split(lines[pos])
            result[op.name] = [_parse_scalar(op.jtype.base, p, raw=True) for p in parts] \
                if op.jtype.base == "String" else [_parse_scalar(op.jtype.base, p) for p in parts if p]
            pos += 1

        # ---- 1-D array, size on previous line -----------------------------
        elif op.mode == "array_sized":
            n = int(lines[pos].strip())
            pos += 1
            if n > 0:
                parts = lines[pos].strip().split()
                result[op.name] = [_parse_scalar(op.jtype.base, p) for p in parts]
                pos += 1
            else:
                result[op.name] = []

        # ---- 2-D array (formal): n_rows then rows (every row line is consumed) -------------------
        elif op.mode == "array_2d":
            elem = _leaf_element_type(op.jtype)
            n_rows = int(lines[pos].strip())
            pos += 1
            mat = []
            for _ in range(n_rows):
                line = lines[pos] if pos < len(lines) else ""
                pos += 1
                mat.append([_parse_scalar(elem, x) for x in line.strip().split()])
            result[op.name] = mat

        # ---- 2-D array (specbench): rows cols then rows --------------------
        elif op.mode == "array_2d_rc":
            elem = _leaf_element_type(op.jtype)
            rc = lines[pos].strip().split()
            n_rows, n_cols = int(rc[0]), int(rc[1])
            pos += 1
            mat = []
            if n_cols == 0:
                # r empty rows; the FB tests still read (and ignore) r row lines, the SGB tests read none
                mat = [[] for _ in range(n_rows)]
                if op.rows_when_no_cols:
                    pos += n_rows
            else:
                for _ in range(n_rows):
                    line = lines[pos] if pos < len(lines) else ""
                    pos += 1
                    # Test.java reads exactly c tokens of each row line (extra tokens are ignored)
                    mat.append([_parse_scalar(elem, x) for x in line.strip().split()[:n_cols]])
            result[op.name] = mat

        # ---- HashMap -------------------------------------------------------
        elif op.mode == "map":
            n = int(lines[pos].strip())
            pos += 1
            d: dict = {}
            ktype = op.jtype.generic_args[0] if op.jtype.generic_args else "String"
            vtype = (
                op.jtype.generic_args[1]
                if len(op.jtype.generic_args) > 1
                else "Integer"
            )
            for _ in range(n):
                parts = lines[pos].split("\t")
                d[_parse_scalar(ktype, parts[0])] = _parse_scalar(
                    vtype, parts[1].strip()
                )
                pos += 1
            result[op.name] = d

        # ---- HashSet -------------------------------------------------------
        elif op.mode == "set":
            n = int(lines[pos].strip())
            pos += 1
            etype = op.jtype.generic_args[0] if op.jtype.generic_args else "Integer"
            parts = lines[pos].strip().split()
            result[op.name] = [_parse_scalar(etype, p) for p in parts]
            pos += 1

        # ---- List / ArrayList / LinkedList (space-separated, no size) -----
        elif op.mode == "list_direct":
            etype = op.jtype.generic_args[0] if op.jtype.generic_args else "Integer"
            if etype == "String":                    # Java: line.trim().split("\\s+") ("" -> [""])
                result[op.name] = [_parse_scalar(etype, p, raw=True) for p in _java_split(lines[pos])]
            else:
                line = lines[pos].strip()
                result[op.name] = [_parse_scalar(etype, p) for p in line.split()] if line else []
            pos += 1

        i += 1

    return result


# Output string → typed Python value


def detect_output_layout(test_src: str) -> dict:
    """
    How Test.java prints the result, from its source: `header` = the first print after the call prints
    only the result's size (`result.length`, `result.size()`, `r + " " + c` with r/c taken from the
    result) before the rows / elements; `sep` = the row's String.join separator if not blank (e.g. ", "), else " ".
    """
    src = test_src or ""
    i = src.find("Solution.solve(")
    tail = src[src.find(";", i) + 1:] if i >= 0 else ""
    prints = re.findall(r"System\.out\.print(?:ln)?\s*\((.*?)\);", tail, re.S)
    header = False
    if len(prints) >= 2:
        first = prints[0]
        size_vars = set(re.findall(r"\b(\w+)\s*=\s*[^;,]*?\.(?:length|size\(\))", tail))
        header = (bool(re.search(r"\.length\b|\.size\(\)", first)) and "stream" not in first
                  and "join" not in first) or \
                 (bool(size_vars) and set(re.findall(r"\b[A-Za-z_]\w*\b", first)) <= size_vars)
    m = re.search(r'String\.join\(\s*"((?:[^"\\]|\\.)*)"', tail)
    sep = m.group(1) if m and m.group(1).strip() else " "          # e.g. ", " (FB_ListifyList)
    return {"header": header, "sep": sep}


def parse_output(output_str: str, return_type: JType, layout: dict | None = None) -> Any:
    """
    Parse the text Test.java printed for the result into a typed Python value, guided by the return type
    and by the print layout of Test.java (detect_output_layout): a size header line is skipped, rows are
    split on the row separator, a String result is kept exactly (only the final newline removed) and
    "null" is a null String. Ambiguous layouts (3-D arrays printed without shape) raise ValueError.
    """
    layout = layout or {"header": False, "sep": " "}
    output_str = output_str[:-1] if output_str.endswith("\n") else output_str

    if return_type.base == "void":
        return None

    # ---- HashMap / TreeMap -------------------------------------------------
    if return_type.base in ("HashMap", "Map", "TreeMap"):
        lines = output_str.split("\n")
        count = int(lines[0].strip())
        ktype = return_type.generic_args[0] if return_type.generic_args else "String"
        vtype = (
            return_type.generic_args[1]
            if len(return_type.generic_args) > 1
            else "Integer"
        )
        d: dict = {}
        for j in range(1, count + 1):
            parts = lines[j].split("\t")
            d[_parse_scalar(ktype, parts[0])] = _parse_scalar(vtype, parts[1].strip())
        return d

    def split_row(line: str, etype: str) -> list:
        if layout["sep"].strip():                    # an explicit separator such as "," or ", "
            return [] if line == "" else [_parse_scalar(etype, x, raw=True) for x in line.split(layout["sep"])]
        return [_parse_scalar(etype, x) for x in line.split()]

    def rows_of(etype: str) -> list:
        lines = output_str.split("\n") if output_str != "" else []
        if layout["header"]:
            if not lines:
                raise ValueError("missing size header")
            n = int(lines[0].split()[0])
            body = lines[1:]
            if len(body) < n:
                body += [""] * (n - len(body))       # empty last rows print as empty lines
            return [split_row(l, etype) for l in body[:n]]
        return [split_row(l, etype) for l in lines]

    # ---- 2-D array ---------------------------------------------------------
    if return_type.is_array and return_type.array_dims >= 3:
        raise ValueError("3-D array output is printed without its shape: ambiguous")
    if return_type.is_array and return_type.array_dims == 2:
        return rows_of(return_type.base)

    # ---- nested list (List<List<T>>) ----------------------------------------
    if return_type.base in ("List", "ArrayList", "LinkedList") and return_type.generic_args \
            and _is_nested_collection(return_type.generic_args[0]):
        inner = re.sub(r"^\w+<\s*|\s*>$", "", return_type.generic_args[0])
        return rows_of(inner or "Integer")

    # ---- char[] (one line of characters) ----------------------------------
    if return_type.is_array and return_type.array_dims == 1 and return_type.base in ("char", "Character"):
        return list(output_str)

    # ---- 1-D array ---------------------------------------------------------
    if return_type.is_array:
        tokens = output_str.strip().split()
        if not tokens or tokens == [""]:
            return []
        return [_parse_scalar(return_type.base, t) for t in tokens]

    # ---- List / ArrayList / LinkedList -------------------------------------
    if return_type.base in ("List", "ArrayList", "LinkedList"):
        tokens = output_str.strip().split()
        etype = return_type.generic_args[0] if return_type.generic_args else "Integer"
        if not tokens or tokens == [""]:
            return []
        return [_parse_scalar(etype, t) for t in tokens]

    # ---- Set / HashSet / TreeSet -------------------------------------------
    if return_type.base in ("Set", "HashSet", "TreeSet"):
        if layout["header"]:
            lines = output_str.split("\n")
            output_str = lines[1] if len(lines) > 1 else ""
        tokens = output_str.strip().split()
        etype = return_type.generic_args[0] if return_type.generic_args else "Integer"
        if not tokens or tokens == [""]:
            return []
        return [_parse_scalar(etype, t) for t in tokens]

    # ---- scalar ------------------------------------------------------------
    if return_type.base == "String":
        return None if output_str == "null" else output_str   # exactly what println printed
    return _parse_scalar(return_type.base, output_str.strip())


# Invalid-input generator for PreCompleteness
def generate_invalid_inputs(
    params: list[dict], valid_inputs: list[dict[str, Any]]
) -> list[InputCase]:
    """
    Auto-generate boundary / invalid input cases (T-) for PreCompleteness.

    Strategies per type:
    - Reference types  → null
    - Arrays / colls   → empty
    - Numeric          → -1, 0, MAX boundary
    """
    if not valid_inputs:
        return []

    base = dict(valid_inputs[0])
    invalid: list[InputCase] = []

    for p in params:
        jtype = p["jtype"]
        name = p["name"]

        # null for any reference type (arrays are reference types even
        # when the element is a primitive, e.g. int[])
        is_ref = not jtype.is_primitive or jtype.is_array or jtype.is_collection
        if is_ref:
            case = dict(base)
            case[name] = None
            invalid.append(InputCase(case, False, f"null_{name}"))

        # empty collection / array
        if jtype.is_array or jtype.is_collection:
            case = dict(base)
            case[name] = {} if jtype.base in ("HashMap", "Map", "TreeMap") else []
            invalid.append(InputCase(case, False, f"empty_{name}"))

        # single-element array (when original has >1)
        if (
            jtype.is_array
            and isinstance(base.get(name), list)
            and len(base.get(name, [])) > 1
        ):
            case = dict(base)
            case[name] = [base[name][0]]
            invalid.append(InputCase(case, False, f"single_{name}"))

        # negative for numeric scalars (not arrays/collections)
        if (
            not jtype.is_array
            and not jtype.is_collection
            and jtype.base
            in ("int", "Integer", "long", "Long", "short", "Short", "byte", "Byte")
        ):
            case = dict(base)
            case[name] = -1
            invalid.append(InputCase(case, False, f"neg_{name}"))

    return invalid
