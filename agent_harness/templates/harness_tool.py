"""
harness_tool.py  --  GENERATED FILE, DO NOT EDIT.

Built by agent_harness/build_harness_tool.py from spec_harness/harness.py,
spec_harness/eval_spec.py and spec_harness/eval_llm_response.py. Edit those files
and re-run the builder.

Self-contained module exposing ``evaluate_problem`` (float scores, no stdout output)
and ``evaluate_problem_detailed`` (per-check details), plus ``Task`` / ``TestCase``.
"""
from __future__ import annotations

# ==== from spec_harness/harness.py ====
"""
Automated computation of four spec-harness metrics for LLM-generated JML annotations on Java methods.

Metrics
-------
  PostCorrectness  : |{(i,o)  in T   : verify {true} x:=i; y:=o  {phi(x,y)}}| / |T|
  PostCompleteness : |{(i,o') in T1  : ~verify{true} x:=i; y:=o' {phi(x,y)}}| / |T1|
  PreCorrectness   : |{i      in T+  : verify {true} x:=i         {psi(x)}}|   / |T+|
  PreCompleteness  : |{i'     in T-  : ~verify{true} x:=i'        {psi(x)}}|   / |T-|

All four reduce to the same operation:
  1. Parse Java method signature via javalang to extract parameter / return types
  2. Build a Java stub that replaces the method body with concrete assignments
  3. Invoke OpenJML (ESC mode); interpret verify / fail as desired outcome

Supported types
---------------
  Primitives  : int, long, short, byte, double, float, boolean, char
  Boxed       : Integer, Long, Double, Float, Boolean, Character, String
  Arrays      : T[], T[][], T[][][]  for any T above
  Collections : List<T>, ArrayList<T>, LinkedList<T>
                Set<T>,  HashSet<T>,  TreeSet<T>
                Map<K,V>, HashMap<K,V>, TreeMap<K,V>
                Nested generics: Map<String, List<Integer>>, List<int[]>
"""


import os
import math
import re
import json
import textwrap
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import javalang
import javalang.tree as jtree
import javalang.tokenizer


# ============================================================
# Result types
# ============================================================

# ============================================================
# Evaluation policy flags (set from the CLI in eval_llm_response.py)
# ============================================================

# F4 / D1: how a spec's precondition enters the postcondition stubs.
#   "guard" (default) - every specification case contributes
#                       `ensures (psi_k) ==> (phi_k)`, i.e. the checked triple is
#                       {true} x:=i; y:=o {psi => phi}. This is JML's meaning: a spec
#                       promises nothing for inputs its precondition excludes. For
#                       such an input the check passes (no false alarm) but no mutant
#                       can be killed, so PostComp drops; PreCorr reports the narrow
#                       precondition. Same rule for single- and multi-case specs.
#   "strip"           - drop `requires` from single-case specs ({true} .. {phi});
#                       multi-case specs are still guarded (earlier behaviour)
#   "raw"             - the spec block as written (the original harness: `requires`
#                       plus the pinned input make the whole stub vacuous when the
#                       input violates the precondition)
POST_STUB_MODE: str = "guard"

# F8: how to score precondition metrics when the spec has no `requires`.
#   "jml"           - run the call-site check anyway; absent requires means
#                     `requires true` plus the implicit non-null defaults
#                     (recommended: matches JML semantics)
#   "requires_true" - PreCorr = 1.0, PreComp = 0.0 without calling OpenJML
#   "zero"          - legacy behaviour: 0/0 -> 0.0 for both
MISSING_REQUIRES_POLICY: str = "jml"

# D8: how each concrete check is decided (same ESC command as RQ1 either way).
#   "dual"   (default) - two stubs per check run in parallel: an *accept* stub that
#                        proves the formula (psi => phi, or psi) and a *refute* stub
#                        that proves its negation. The first to verify decides; if
#                        neither verifies the check is "unknown" (reported, excluded
#                        from the denominator). A kill / rejection is a proof.
#   "single"           - one stub per check; a failure to prove counts as a kill or
#                        rejection (the original harness)
DECISION_MODE: str = "dual"

# Call-site assertion labels that count as "the precondition rejected the input".
# OpenJML reports a null argument against an (implicitly) non-null parameter as
# "NullFormal", so that label counts as a rejection too.
PRECONDITION_LABELS: tuple[str, ...] = ("precondition", "nullformal", "nonnull", "non_null", "nullcheck")


class VerifyResult(Enum):
    OK      = "ok"        # obligation discharged
    FAIL    = "fail"      # explicit verification failure
    UNKNOWN = "unknown"   # timeout / prover gave up
    ERROR   = "error"     # stub did not compile or the tool failed (F3):
                          # never a pass, never a kill, never a rejection


@dataclass
class HarnessResult:
    metric:  str
    total:   int                      # checks that produced a definite verdict (ERROR excluded)
    passed:  int
    details: list = field(default_factory=list)
    errors:  int = 0                  # F3: checks that could not run (compile/tool error)
    unknowns: int = 0                 # D8: checks the prover could not decide either way

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0

    def __str__(self) -> str:
        err = f"  errors={self.errors}" if self.errors else ""
        return (f"{self.metric:25s}  score={self.score:.3f}"
                f"  ({self.passed}/{self.total}){err}")


# ============================================================
# Test data structures
# ============================================================

@dataclass
class TestPair:
    """One concrete input-output pair for postcondition evaluation."""
    inputs: dict[str, Any]   # {"paramName": pythonValue, …}
    output: Any              # expected return value
    label:  str = ""


@dataclass
class InputCase:
    """One input case for precondition evaluation."""
    inputs: dict[str, Any]   # {"paramName": pythonValue, …}
    valid:  bool             # True  → T+  (must be admitted by precondition)
                             # False → T-  (must be rejected by precondition)
    label:  str = ""


_PRE_KEYWORDS  = ("requires", "requires_redundantly", "pre", "pre_redundantly")
_POST_KEYWORDS = ("ensures", "ensures_redundantly", "post", "post_redundantly")


# Words that may precede a clause keyword: visibility / behaviour headers and
# method modifiers that LLMs put inside the JML block.
_HEADER_WORDS = {
    "public", "private", "protected",
    "normal_behavior", "normal_behaviour", "exceptional_behavior", "exceptional_behaviour",
    "behavior", "behaviour",
    "pure", "helper", "spec_public", "spec_protected", "model", "instance", "static",
    "non_null", "nullable", "code", "strictly_pure", "function",
}


def java_comments(text: str) -> list[tuple[int, int, str]]:
    """
    (start, end, kind) of every Java comment, kind "line" (// .. end of line, the newline excluded) or
    "block" (/* .. */), found by a lexer that skips string, char and text-block literals, so `//` or `/*`
    inside "..." is not a comment. An unterminated block comment runs to the end of the text.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith('"""', i):                       # text block
            j = text.find('"""', i + 3)
            i = n if j < 0 else j + 3
        elif c in ('"', "'"):
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append((i, j, "line"))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append((i, j, "block"))
            i = j
        else:
            i += 1
    return out


def mask_comments_and_literals(text: str) -> str:
    """`text` with every comment and string/char literal replaced by spaces (newlines kept), so offsets and
    line numbers are unchanged and regexes / brace matching see only code."""
    buf = list(text)
    for s, e, _ in java_comments(text):
        for k in range(s, e):
            if buf[k] != "\n":
                buf[k] = " "
    i, n = 0, len(text)
    comments = {s: e for s, e, _ in java_comments(text)}
    while i < n:
        if i in comments:
            i = comments[i]
            continue
        c = text[i]
        if c in ('"', "'"):
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if buf[k] != "\n":
                    buf[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(buf)


def strip_jml_inner_comments(text: str) -> str:
    """Inside a JML annotation: drop nested `// ..` (to end of line) and `/* .. */` comments that are not
    inside string/char literals (OpenJML treats them as comments)."""
    parts, last = [], 0
    for s, e, _ in java_comments(text):
        parts.append(text[last:s])
        last = e
    parts.append(text[last:])
    return "".join(parts)


def _strip_markers(jml_block: str) -> str:
    """Remove JML comment markers (//@, /*@, @*/, */, leading @) and join lines."""
    text = jml_block
    text = re.sub(r'/\*@', ' ', text)
    text = re.sub(r'@\*/', ' ', text)
    text = re.sub(r'\*/', ' ', text)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r'^//@', '', line)
        line = re.sub(r'^@+', '', line)
        lines.append(line.strip())
    return " ".join(l for l in lines if l)


def _split_top_level(text: str) -> list[str]:
    """
    Split on ';' only at bracket depth 0 and outside string/char literals, so the
    ';' separators inside quantifiers such as (\\forall int i; 0 <= i; a[i] > 0)
    stay inside their clause.
    """
    parts, buf, depth, quote, i = [], [], 0, None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == ";" and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


# words that can begin a JML method-specification clause (besides also / the header words)
_CLAUSE_WORDS = {
    "requires", "ensures", "pre", "post", "requires_redundantly", "ensures_redundantly",
    "pre_redundantly", "post_redundantly", "assignable", "modifies", "modifiable",
    "assignable_redundantly", "modifies_redundantly", "signals", "signals_only", "exsures",
    "signals_redundantly", "diverges", "when", "measured_by", "working_space", "duration",
    "accessible", "callable", "captures", "forall", "old", "also",
}


def _merge_continuations(parts: list[str]) -> list[str]:
    """
    Re-join pieces that _split_top_level cut out of one clause. A bare (unparenthesized) quantifier
    such as `requires \\forall int i; 0 <= i < n; a[i] > 0;` is valid JML, and its inner ';' are at
    depth 0. A piece that does not begin with a clause keyword, `also` or a header word continues
    the previous clause.
    """
    out: list[str] = []
    for p in parts:
        m = re.match(r'([A-Za-z_]+)\b', p)
        starts = bool(m) and (m.group(1) in _CLAUSE_WORDS or m.group(1) in _HEADER_WORDS)
        if out and not starts:
            out[-1] = out[-1] + "; " + p
        else:
            out.append(p)
    return out


def _strip_header(clause: str) -> tuple[bool, str]:
    """
    Strip leading `also` and header words. Returns (starts_new_case, clause).
    E.g. 'also public normal_behavior requires x > 0' -> (True, 'requires x > 0').
    """
    new_case = False
    while True:
        m = re.match(r'([A-Za-z_]+)\b\s*', clause)
        if not m:
            return new_case, clause
        word = m.group(1)
        if word == "also":
            new_case = True
        elif word not in _HEADER_WORDS:
            return new_case, clause
        clause = clause[m.end():]


def _jml_cases(jml_block: str) -> list[list[str]]:
    """
    Parse a JML block into specification cases (separated by `also`), each a list
    of clauses with headers removed, e.g.
      [['requires n >= 1', 'ensures \\result == n', 'assignable \\nothing']].
    """
    cases: list[list[str]] = [[]]
    for raw in _merge_continuations(_split_top_level(_strip_markers(jml_block))):
        new_case, clause = _strip_header(raw)
        if new_case and cases[-1]:
            cases.append([])
        if clause:
            cases[-1].append(clause)
    return [c for c in cases if c] or [[]]


def _jml_clauses(jml_block: str) -> list[str]:
    """All clauses of all specification cases, headers removed (flattened)."""
    return [c for case in _jml_cases(jml_block) for c in case]


def _clause_keyword(clause: str) -> str:
    m = re.match(r'([A-Za-z_]+)', clause)
    return m.group(1) if m else ""


def _clause_body(clause: str) -> str:
    """Expression part of a clause: 'requires x > 0' -> 'x > 0'."""
    return clause[len(_clause_keyword(clause)):].strip()


@dataclass
class MethodSpec:
    """Holds the raw LLM-generated JML comment block, preserved as-is."""
    jml_block:     str             # raw JML comment block (//@ or /*@ ... @*/) from LLM code
    auxiliaries:   str = ""        # helper predicates / model methods (prepended to stub)
    imports:       str = ""        # import lines of the source (stubs need them)
    helpers:       str = ""        # other members of the LLM class (e.g. pure helper
                                   # methods the spec calls), inserted inside stub classes

    # ---- clause access -------------------------------------------------

    def cases(self) -> list[list[str]]:
        """Specification cases (split on `also`), headers removed."""
        return _jml_cases(self.jml_block)

    def clauses(self) -> list[str]:
        return _jml_clauses(self.jml_block)

    @property
    def has_requires(self) -> bool:
        # F9: keyword match on parsed clauses (headers such as
        # `public normal_behavior` are stripped first), not a substring search
        return any(_clause_keyword(c) in _PRE_KEYWORDS for c in self.clauses())

    @property
    def has_ensures(self) -> bool:
        return any(_clause_keyword(c) in _POST_KEYWORDS for c in self.clauses())

    @staticmethod
    def _emit(clauses: list[str]) -> str:
        return "\n".join(f"//@ {c};" for c in clauses)

    @staticmethod
    def _case_pre(case: list[str]) -> Optional[str]:
        pres = [_clause_body(c) for c in case if _clause_keyword(c) in _PRE_KEYWORDS]
        return " && ".join(f"({p})" for p in pres) if pres else None

    def post_terms(self, guard_single_case: bool = True) -> list[str]:
        """
        Boolean expressions whose conjunction is the postcondition checked on a
        stub: `(pre_k) ==> (post)` per ensures clause of case k (D1 guard), or
        `(post)` for a case without requires.
        """
        cases = self.cases()
        terms = []
        for case in cases:
            pre = self._case_pre(case)
            guard = pre is not None and (len(cases) > 1 or guard_single_case)
            for c in case:
                if _clause_keyword(c) in _POST_KEYWORDS:
                    body = _clause_body(c)
                    terms.append(f"({pre}) ==> ({body})" if guard else f"({body})")
        return terms

    def pre_expr(self) -> Optional[str]:
        """
        The method's explicit precondition as one expression, or None if it is
        `true` (no requires, or some specification case has no requires).
        Single case: conjunction of its requires; several cases: disjunction.
        """
        pres = [self._case_pre(c) for c in self.cases()]
        if not pres or any(p is None for p in pres):
            return None
        return pres[0] if len(pres) == 1 else " || ".join(f"({p})" for p in pres)

    def requires_only_block(self) -> str:
        """
        The method's precondition, re-emitted as //@ lines (may be empty).
        Single case: the requires clauses as written (conjunction).
        Several cases (`also`): JML semantics is the disjunction of the cases'
        preconditions; a case without requires contributes `true`, making the
        whole precondition `true` (empty block).
        """
        cases = self.cases()
        if len(cases) <= 1:
            return self._emit([c for c in self.clauses() if _clause_keyword(c) in _PRE_KEYWORDS])
        pres = [self._case_pre(c) for c in cases]
        if any(p is None for p in pres):
            return ""
        return self._emit(["requires " + " || ".join(f"({p})" for p in pres)])

    def post_only_block(self, guard_single_case: bool = True) -> str:
        """
        The postcondition side of the spec for PostCorr / PostComp stubs, with no
        `requires` clause on the stub (D1).

        Every specification case contributes its ensures clauses guarded by that
        case's precondition, `ensures (pre_k) ==> (post)`: the checked triple is
        {true} x:=i; y:=o {psi => phi}, which is JML's meaning of a spec (and of
        `also`). A case without requires contributes its ensures unguarded. Other
        clauses (assignable, signals, ...) are kept as written.

        guard_single_case=False reproduces the earlier "strip" variant: a
        single-case spec drops its requires ({true} .. {phi}); multi-case specs
        are guarded either way (flattening would conjoin different cases).
        """
        cases = self.cases()
        if len(cases) <= 1 and not guard_single_case:
            return self._emit([c for c in self.clauses() if _clause_keyword(c) not in _PRE_KEYWORDS])
        out = []
        for case in cases:
            pre = self._case_pre(case)
            for c in case:
                kw = _clause_keyword(c)
                if kw in _PRE_KEYWORDS:
                    continue
                if kw in _POST_KEYWORDS and pre is not None:
                    out.append(f"{kw} ({pre}) ==> ({_clause_body(c)})")
                else:
                    out.append(c)
        return self._emit(out)

    # Back-compat aliases (deprecated)
    @property
    def precondition(self) -> Optional[str]:
        return self.jml_block if self.has_requires else None

    @property
    def postcondition(self) -> Optional[str]:
        return self.jml_block if self.has_ensures else None


# ============================================================
# javalang-based type descriptor
# ============================================================

@dataclass
class JType:
    """
    Normalised Java type descriptor extracted from a javalang AST node.

    Examples
    --------
    int            → JType("int",     array_dims=0)
    int[]          → JType("int",     array_dims=1)
    int[][]        → JType("int",     array_dims=2)
    String         → JType("String",  array_dims=0)
    List<Integer>  → JType("List",    generic_args=["Integer"])
    Map<String,
        List<int>> → JType("Map",     generic_args=["String","List<int>"])
    """
    base:         str
    array_dims:   int  = 0
    generic_args: list = field(default_factory=list)

    # ---- convenience predicates ----------------------------------------

    PRIMITIVES = frozenset({
        "int","long","short","byte","double","float","boolean","char"
    })
    COLLECTIONS = frozenset({
        "List","ArrayList","LinkedList",
        "Set","HashSet","TreeSet",
        "Map","HashMap","TreeMap",
    })

    @property
    def is_primitive(self) -> bool:
        return self.base in self.PRIMITIVES

    @property
    def is_array(self) -> bool:
        return self.array_dims > 0

    @property
    def is_collection(self) -> bool:
        return self.base in self.COLLECTIONS

    def java_decl(self) -> str:
        """Full Java type declaration string, e.g. 'List<Integer>' or 'int[][]'."""
        if self.generic_args:
            args = ", ".join(self.generic_args)
            base = f"{self.base}<{args}>"
        else:
            base = self.base
        return base + "[]" * self.array_dims

    def element_type(self) -> "JType":
        """Return the element type for array types (strip one dimension)."""
        assert self.is_array, "element_type() called on non-array type"
        return JType(self.base, self.array_dims - 1, self.generic_args)


# ============================================================
# javalang-based method signature parser
# ============================================================

class JavaMethodParser:
    """
    Parses a Java source string (class or bare method) using javalang
    and returns structured parameter / return type information.

    Returns
    -------
    {
      "class_name":   str,
      "method_name":  str,
      "return_type":  JType,
      "params":       list[dict{"name": str, "jtype": JType}],
    }
    """

    def parse(self, source: str, method_name: Optional[str] = None) -> dict:
        """Parse the target method: `method_name` if given, else `solve` if the
        class has one, else the first method (helpers may precede the target)."""
        wrapped, class_name = self._ensure_class(source)
        try:
            tree = javalang.parse.parse(wrapped)
        except javalang.parser.JavaSyntaxError as e:
            raise ValueError(f"javalang parse error: {e}") from e

        methods = [(p, n) for p, n in tree if isinstance(n, jtree.MethodDeclaration)]
        wanted = method_name or ("solve" if any(m.name == "solve" for _, m in methods) else None)
        for path, node in methods:
            if wanted is None or node.name == wanted:
                # the class that declares the method (from the syntax tree, so the word "class" in a
                # comment cannot be mistaken for the class name)
                owners = [x for x in path if isinstance(x, jtree.ClassDeclaration)]
                return {
                    "class_name":  owners[-1].name if owners else class_name,
                    "method_name": node.name,
                    "return_type": self._jtype(node.return_type),
                    "params": [
                        {"name": p.name, "jtype": self._jtype(p.type)}
                        for p in (node.parameters or [])
                    ],
                }
        raise ValueError("No MethodDeclaration found in the provided source.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_class(source: str) -> tuple[str, str]:
        source = source.strip()
        m = re.search(r'\bclass\s+(\w+)', mask_comments_and_literals(source))
        if m:
            return source, m.group(1)
        # Bare method — wrap in a synthetic class
        return f"class _Harness {{ {source} }}", "_Harness"

    def _jtype(self, node) -> JType:
        """Convert a javalang type AST node to a JType."""
        if node is None:
            return JType(base="void")

        # ---- BasicType: int, boolean, double … -------------------------
        if isinstance(node, jtree.BasicType):
            dims = len(node.dimensions) if node.dimensions else 0
            return JType(base=node.name, array_dims=dims)

        # ---- ReferenceType: String, int[], List<Integer>, Map<K,V> … ---
        if isinstance(node, jtree.ReferenceType):
            dims         = len(node.dimensions) if node.dimensions else 0
            generic_args = self._generic_args(node)
            return JType(base=node.name, array_dims=dims,
                         generic_args=generic_args)

        return JType(base=str(node))

    def _generic_args(self, node: jtree.ReferenceType) -> list[str]:
        """Recursively flatten type arguments to string representations."""
        if not getattr(node, "arguments", None):
            return []
        result = []
        for arg in node.arguments:
            t = getattr(arg, "type", None)
            if t is not None:
                result.append(self._jtype(t).java_decl())
            elif hasattr(arg, "name"):
                result.append(arg.name)
        return result


# ============================================================
# Type-aware Java literal renderer
# ============================================================

def _java_escape(text: str, quote: str) -> str:
    """Escape text for a Java char/String literal: quote, backslash, control and non-ASCII characters."""
    out = []
    for ch in text:
        if ch == quote or ch == "\\":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) > 0x7E:
            out.extend(f"\\u{b:04x}" for b in _utf16_units(ch))
        else:
            out.append(ch)
    return "".join(out)


def _utf16_units(ch: str) -> list[int]:
    b = ch.encode("utf-16-be")
    return [int.from_bytes(b[i:i + 2], "big") for i in range(0, len(b), 2)]


class JavaLiteralRenderer:
    """
    Converts a Python value to a syntactically valid Java literal /
    initialiser expression, guided by a JType descriptor.

    Handles
    -------
    - All primitives and their boxed counterparts
    - 1-D, 2-D, 3-D (and deeper) arrays
    - List / ArrayList / LinkedList
    - Set / HashSet / TreeSet
    - Map / HashMap / TreeMap
    - Nested generics: Map<String, List<Integer>>, List<int[]>, …
    """

    def render(self, jtype: JType, value: Any) -> str:
        if value is None:
            return "null"
        if jtype.is_array:
            return self._array(jtype, value)
        if jtype.is_collection:
            return self._collection(jtype, value)
        return self._scalar(jtype.base, value)

    # ---- arrays --------------------------------------------------------

    def _array(self, jtype: JType, value: Any) -> str:
        """Recursively render N-dimensional arrays."""
        elem_type = jtype.element_type()
        if jtype.array_dims == 1:
            elems = ", ".join(self._scalar(jtype.base, v) for v in value)
            return f"new {jtype.base}[]{{{elems}}}"
        # 2D / 3D / … — recurse on each row
        rows = ", ".join(self.render(elem_type, row) for row in value)   # render(): a null row stays null
        inner = jtype.base + "[]" * (jtype.array_dims - 1)
        return f"new {inner}[]{{{rows}}}"

    # ---- collections ---------------------------------------------------

    def _collection(self, jtype: JType, value: Any) -> str:
        base = jtype.base
        if base in ("Map", "HashMap", "TreeMap"):
            return self._map(jtype, value)
        if base in ("Set", "HashSet", "TreeSet"):
            return self._set(jtype, value)
        return self._list(jtype, value)  # List / ArrayList / LinkedList

    def _list(self, jtype: JType, value: list) -> str:
        elem = self._arg_type(jtype, 0)
        elems = ", ".join(self.render(elem, v) for v in value)
        concrete = ("LinkedList" if jtype.base == "LinkedList"
                    else "ArrayList")
        return f"new java.util.{concrete}<>(java.util.Arrays.asList({elems}))"

    def _set(self, jtype: JType, value) -> str:
        elem = self._arg_type(jtype, 0)
        elems = ", ".join(self.render(elem, v) for v in value)
        concrete = "TreeSet" if jtype.base == "TreeSet" else "HashSet"
        return (f"new java.util.{concrete}<>"
                f"(java.util.Arrays.asList({elems}))")

    def _map(self, jtype: JType, value: dict) -> str:
        k_type = self._arg_type(jtype, 0)
        v_type = self._arg_type(jtype, 1)
        concrete = "TreeMap" if jtype.base == "TreeMap" else "HashMap"
        entries = "; ".join(
            f"_m.put({self.render(k_type, k)}, {self.render(v_type, v)})"
            for k, v in value.items()
        )
        kd, vd = k_type.java_decl(), v_type.java_decl()
        entries = "; ".join(f"put({self.render(k_type, k)}, {self.render(v_type, v)})"
                            for k, v in value.items())
        # anonymous initialiser block
        return (f"new java.util.{concrete}<{kd}, {vd}>() "
                f"{{{{ {entries}; }}}}")

    # ---- scalars -------------------------------------------------------

    def _scalar(self, base: str, value: Any) -> str:
        if value is None:                     # null element of a String[] / Integer[] / List, or a null key/value
            return "null"
        if base in ("int", "Integer", "short", "Short", "byte", "Byte"):
            v = int(value)
            if base in ("short", "Short", "byte", "Byte"):
                return f"(({'short' if base in ('short', 'Short') else 'byte'}) {v})"
            return str(v)
        if base in ("long", "Long"):
            return f"{int(value)}L"
        if base in ("double", "Double", "float", "Float"):
            f = float(value)
            box, suf = ("Double", "d") if base in ("double", "Double") else ("Float", "f")
            if math.isnan(f):
                return f"{box}.NaN"
            if math.isinf(f):
                return f"{box}.{'POSITIVE' if f > 0 else 'NEGATIVE'}_INFINITY"
            return f"{repr(f)}{suf}"
        if base in ("boolean", "Boolean"):
            return "true" if value else "false"
        if base in ("char", "Character"):
            c = value if isinstance(value, str) else chr(int(value))
            if len(c) != 1:
                raise ValueError(f"char literal needs exactly one character: {value!r}")
            return "'" + _java_escape(c, quote="'") + "'"
        if base == "String":
            return '"' + _java_escape(str(value), quote='"') + '"'
        return str(value)

    # ---- helper --------------------------------------------------------

    def _arg_type(self, jtype: JType, idx: int) -> JType:
        """Return the JType for the idx-th generic argument."""
        if jtype.generic_args and idx < len(jtype.generic_args):
            return _parse_raw_jtype(jtype.generic_args[idx])
        return JType(base="Object")


# ============================================================
# Stub builder
# ============================================================

def mark_helpers_skipesc(members: str) -> str:
    """
    Insert `/*@ skipesc @*/` before every method declared in `members` (the other
    class members copied into a stub), so ESC uses the helpers' contracts in the
    spec but does not re-verify their bodies inside the check.
    """
    if not members or not members.strip():
        return members
    try:
        tree = javalang.parse.parse("class _H {\n" + members + "\n}")
    except Exception:
        return members
    lines = members.splitlines()
    starts = sorted({n.position.line - 2 for _, n in tree
                     if isinstance(n, jtree.MethodDeclaration) and n.position}, reverse=True)
    for i in starts:
        if 0 <= i < len(lines):
            indent = re.match(r"\s*", lines[i]).group(0)
            lines.insert(i, f"{indent}/*@ skipesc @*/")
    return "\n".join(lines)


class StubBuilder:
    """
    Constructs Java harness stubs for each of the four Hoare triples.

    Postcondition metrics use a stub whose body is ``//@ assume`` statements
    that pin the inputs plus a ``return`` of the (possibly mutated) output.
    With POST_STUB_MODE == "guard" (default) the stub carries no ``requires``;
    each ensures clause is guarded by its case's precondition, so the checked
    triple is {true} x:=i; y:=o {psi => phi}.

      PostCorrectness  : //@ ensures phi;  body: assume inputs; return o;
      PostCompleteness : //@ ensures phi;  body: assume inputs; return o';

    Precondition metrics use a *call-site* check: a callee carrying only the
    ``requires`` clauses and a trivial body, plus a caller that invokes it
    with concrete literals. OpenJML evaluates the requires clause at the
    call. The original method body is never re-verified (F5).

      PreCorrectness   : check() { Solution.m(valid literals);   }  -> expect OK
      PreCompleteness  : check() { Solution.m(invalid literals); }  -> expect Precondition warning
    """

    def __init__(self):
        self._renderer = JavaLiteralRenderer()

    # ---- public entry points -------------------------------------------

    def post_correctness_stub(self, parsed: dict,
                               spec: MethodSpec,
                               pair: TestPair) -> str:
        assumes = self._input_assumes(parsed, pair.inputs)
        ret     = self._return_stmt(parsed, pair.output)
        return self._wrap_post(parsed, spec, assumes, ret)

    def post_completeness_stub(self, parsed: dict,
                                spec: MethodSpec,
                                pair: TestPair,
                                mutated_output: Any) -> str:
        assumes = self._input_assumes(parsed, pair.inputs)
        ret     = self._return_stmt(parsed, mutated_output)
        return self._wrap_post(parsed, spec, assumes, ret)

    def pre_correctness_stub(self, parsed: dict,
                              spec: MethodSpec,
                              case: InputCase,
                              original_source: str = "") -> str:
        if original_source:
            return self._wrap_caller(parsed, spec, original_source,
                                     case.inputs)
        assumes = self._input_assumes(parsed, case.inputs)
        return self._wrap_pre(parsed, spec, assumes)

    def pre_completeness_stub(self, parsed: dict,
                               spec: MethodSpec,
                               case: InputCase,
                               original_source: str = "") -> str:
        if original_source:
            return self._wrap_caller(parsed, spec, original_source,
                                     case.inputs)
        assumes = self._input_assumes(parsed, case.inputs)
        return self._wrap_pre(parsed, spec, assumes)

    # ---- assume generation ---------------------------------------------

    def _input_assumes(self, parsed: dict,
                       inputs: dict[str, Any]) -> str:
        """Generate ``//@ assume`` statements for all input values."""
        lines: list[str] = []
        for p in parsed["params"]:
            val = inputs[p["name"]]
            lines.extend(self._assume_for(p["name"], p["jtype"], val))
        return "\n".join(lines)

    def _assume_for(self, name: str, jtype: JType,
                    value: Any) -> list[str]:
        """JML assume statement(s) constraining *name* to *value*."""
        I = "        "  # 8-space indent

        if value is None:
            return [f"{I}//@ assume {name} == null;"]

        # ---- arrays ----
        if jtype.is_array:
            return self._assume_array(name, jtype, value)

        # ---- collections ----
        if jtype.is_collection:
            return self._assume_collection(name, jtype, value)

        # ---- String ----
        if jtype.base == "String":
            lit = self._renderer._scalar("String", value)
            return [f"{I}//@ assume {name} != null && {name}.equals({lit});"]

        # ---- other scalars ----
        return [f"{I}//@ assume {self._scalar_eq(name, jtype.base, value)};"]

    def _scalar_eq(self, expr: str, base: str, value) -> str:
        """`expr == literal`; a NaN value is pinned with isNaN (`x == Double.NaN` is always false, which
        would make every stub vacuous)."""
        if isinstance(value, float) and value != value and base in ("double", "Double", "float", "Float"):
            return f"{'Float' if base in ('float', 'Float') else 'Double'}.isNaN({expr})"
        return f"{expr} == {self._renderer._scalar(base, value)}"

    _BOXED = {"Integer", "Long", "Short", "Byte", "Double", "Float", "Boolean", "Character"}

    def _assume_array(self, name: str, jtype: JType,
                      value: list) -> list[str]:
        """Element-wise assumes for arrays of any dimension: the array is non-null with the given length and
        every element is pinned recursively (a null row or element is pinned as null). Elements of a
        reference type are pinned by value: String with equals, boxed numbers by unboxing; `==` would compare
        object identity and leave the content unconstrained."""
        I = "        "
        lines = [f"{I}//@ assume {name} != null;",
                 f"{I}//@ assume {name}.length == {len(value)};"]
        elem = jtype.element_type()
        for i, v in enumerate(value):
            e = f"{name}[{i}]"
            if v is None:
                lines.append(f"{I}//@ assume {e} == null;")
            elif elem.is_array:
                lines.extend(self._assume_array(e, elem, v))
            else:
                lines.append(f"{I}//@ assume {self._element_eq(e, elem.base, v)};")
        return lines

    def _element_eq(self, expr: str, base: str, value) -> str:
        """Pin one non-null array element to *value*, comparing by value for reference types."""
        if base == "String":
            return f"{expr} != null && {expr}.equals({self._renderer._scalar('String', value)})"
        if base in self._BOXED:
            prim = {"Integer": "int", "Long": "long", "Short": "short", "Byte": "byte", "Double": "double",
                    "Float": "float", "Boolean": "boolean", "Character": "char"}[base]
            return f"{expr} != null && {self._scalar_eq(expr, prim, value)}"
        return self._scalar_eq(expr, base, value)

    def _assume_collection(self, name: str, jtype: JType,
                           value: Any) -> list[str]:
        """
        Assume statements for collection inputs (F6). Pins size and, where the
        element type is a scalar, the elements themselves:
          List<T>  : get(i) == v_i
          Set<T>   : contains(v)
          Map<K,V> : containsKey(k) and get(k) == v
        Nested collections are pinned by size only; the caller marks such
        tasks as unsupported (see eval_llm_response.evaluate_problem).
        """
        I = "        "
        if value is None:
            return [f"{I}//@ assume {name} == null;"]
        lines = [f"{I}//@ assume {name} != null;"]
        base = jtype.base

        def lit(t: JType, v: Any) -> Optional[str]:
            if t.is_array or t.is_collection:
                return None
            return self._renderer.render(t, v)

        _BOXED_NUM = {"Integer", "Long", "Short", "Byte", "Double", "Float",
                      "Boolean", "Character"}

        def eq(expr: str, t: JType, v: Any) -> Optional[str]:
            l = lit(t, v)
            if l is None:
                return None
            if t.base == "String":
                return f"{expr} != null && {expr}.equals({l})"
            if t.base in _BOXED_NUM:
                # unboxing comparison; OpenJML reasons about this, not about .equals
                return f"{expr} != null && {expr} == {l}"
            if t.is_primitive:
                return f"{expr} == {l}"
            return f"{expr} != null && {expr}.equals({l})"

        if base in ("Map", "HashMap", "TreeMap") and isinstance(value, dict):
            k_t = self._renderer._arg_type(jtype, 0)
            v_t = self._renderer._arg_type(jtype, 1)
            lines.append(f"{I}//@ assume {name}.size() == {len(value)};")
            for k, v in value.items():
                kl = lit(k_t, k)
                ve = eq(f"{name}.get({kl})", v_t, v) if kl is not None else None
                if kl is not None:
                    lines.append(f"{I}//@ assume {name}.containsKey({kl});")
                if ve is not None:
                    lines.append(f"{I}//@ assume {ve};")
        elif base in ("Set", "HashSet", "TreeSet") and isinstance(value, (list, set, tuple)):
            e_t = self._renderer._arg_type(jtype, 0)
            vals = list(value)
            lines.append(f"{I}//@ assume {name}.size() == {len(vals)};")
            for v in vals:
                l = lit(e_t, v)
                if l is not None:
                    lines.append(f"{I}//@ assume {name}.contains({l});")
        elif isinstance(value, (list, tuple)):
            e_t = self._renderer._arg_type(jtype, 0)
            vals = list(value)
            lines.append(f"{I}//@ assume {name}.size() == {len(vals)};")
            for i, v in enumerate(vals):
                ve = eq(f"{name}.get({i})", e_t, v)
                if ve is not None:
                    lines.append(f"{I}//@ assume {ve};")
        else:
            try:
                lines.append(f"{I}//@ assume {name}.size() == {len(value)};")
            except TypeError:
                pass
        return lines

    # ---- return / assert generation ------------------------------------

    def _return_stmt(self, parsed: dict, value: Any) -> str:
        rt  = parsed["return_type"]
        if rt.base == "void":          # F7: no \result to constrain
            return "        return;"
        lit = self._renderer.render(rt, value)
        return f"        return {lit};"

    # ---- wrapping ------------------------------------------------------

    def _wrap_post(self, parsed: dict, spec: MethodSpec,
                   assumes: str, ret_stmt: str) -> str:
        """Wrap for postcondition checking — raw JML block above method."""
        params_decl = ", ".join(
            f"{p['jtype'].java_decl()} {p['name']}"
            for p in parsed["params"]
        )
        ret   = parsed["return_type"].java_decl()
        mname = parsed["method_name"]
        cname = parsed["class_name"]
        aux   = spec.auxiliaries or ""
        block = (spec.jml_block if POST_STUB_MODE == "raw"
                 else spec.post_only_block(guard_single_case=(POST_STUB_MODE == "guard")))
        jml   = self._indent_jml(block, "    ")

        return (
            f"{aux}\n"
            f"//@ nullable_by_default\n"
            f"public class {cname}Harness {{\n"
            f"{self._indent_members(spec.helpers)}"
            f"{jml}\n"
            f"    public static {ret} {mname}({params_decl}) {{\n"
            f"{assumes}\n"
            f"{ret_stmt}\n"
            f"    }}\n"
            f"}}\n"
        )

    def _wrap_pre(self, parsed: dict, spec: MethodSpec,
                  assumes: str) -> str:
        """
        DEPRECATED (F9): not used by the evaluation. A `requires` clause on a
        method is *assumed* at entry, so a stub with `requires psi` and
        `assume x == i` cannot detect a violation. Kept only for backward
        compatibility; precondition checks go through _wrap_caller.
        """
        params_decl = ", ".join(
            f"{p['jtype'].java_decl()} {p['name']}"
            for p in parsed["params"]
        )
        mname = parsed["method_name"]
        cname = parsed["class_name"]
        aux   = spec.auxiliaries or ""
        jml   = self._indent_jml(spec.jml_block, "    ")

        return (
            f"{aux}\n"
            f"//@ nullable_by_default\n"
            f"public class {cname}Harness {{\n"
            f"{jml}\n"
            f"    public static void {mname}({params_decl}) {{\n"
            f"{assumes}\n"
            f"    }}\n"
            f"}}\n"
        )

    def _wrap_caller(self, parsed: dict, spec: MethodSpec,
                     original_source: str,
                     inputs: dict[str, Any]) -> str:
        """
        Precondition stubs (F5): a *callee* that carries only the spec's
        `requires` clauses on a trivial body, and a *caller* that invokes it
        with concrete argument literals.

        OpenJML ESC evaluates the requires clause at the call site:
          - valid input   -> no warning          (PreCorrectness:  desired OK)
          - invalid input -> Precondition warning (PreCompleteness: desired FAIL)

        The original method body is NOT included, so nothing in the body
        (loop invariants, nullity of locals, arithmetic) can be charged to
        the precondition. `original_source` is accepted for API
        compatibility but only its parsed signature is used (via `parsed`).

        The callee body is `//@ assume false;` followed by a default return,
        which discharges every obligation inside the callee vacuously.

        Generated shape
        ---------------
        class {C} {                        // not public: one public class per file
            //@ requires ...;              // requires-only block (may be empty)
            public static T {m}(...) { //@ assume false; return <default>; }
        }
        public class {C}Harness {
            public static void check() { {C}.{m}(lit1, lit2, ...); }
        }
        """
        mname = parsed["method_name"]
        cname = parsed["class_name"]
        rt    = parsed["return_type"]

        params_decl = ", ".join(
            f"{p['jtype'].java_decl()} {p['name']}" for p in parsed["params"]
        )
        call_args = ", ".join(
            self._renderer.render(p["jtype"], inputs[p["name"]])
            for p in parsed["params"]
        )

        req_block = self._indent_jml(spec.requires_only_block(), "    ")
        aux       = spec.auxiliaries or ""

        if rt.base == "void":
            default_ret = ""
        elif rt.is_array or rt.is_collection or not rt.is_primitive:
            default_ret = "        return null;"
        elif rt.base in ("boolean",):
            default_ret = "        return false;"
        elif rt.base in ("char",):
            default_ret = "        return 'a';"
        elif rt.base in ("double", "float"):
            default_ret = "        return 0.0;"
        elif rt.base == "long":
            default_ret = "        return 0L;"
        else:
            default_ret = "        return 0;"

        return (
            f"{aux}\n"
            f"class {cname} {{\n"
            f"{self._indent_members(spec.helpers)}"
            f"{req_block}\n"
            f"    public static {rt.java_decl()} {mname}({params_decl}) {{\n"
            f"        //@ assume false;\n"
            f"{default_ret}\n"
            f"    }}\n"
            f"}}\n\n"
            f"public class {cname}Harness {{\n"
            f"    public static void check() {{\n"
            f"        {cname}.{mname}({call_args});\n"
            f"    }}\n"
            f"}}\n"
        )



    # ---- D8: dual accept / refute stubs --------------------------------

    def _harness_class(self, parsed: dict, spec: MethodSpec, method_src: str) -> str:
        """Imports + nullable-by-default harness class with helpers (skipesc)."""
        cname = parsed["class_name"]
        helpers = mark_helpers_skipesc(spec.helpers)
        return (
            f"{spec.imports.strip()}\n"
            f"//@ nullable_by_default\n"
            f"public class {cname}Harness {{\n"
            f"{self._indent_members(helpers)}"
            f"{method_src}"
            f"}}\n"
        )

    def _params_decl(self, parsed: dict) -> str:
        return ", ".join(f"{p['jtype'].java_decl()} {p['name']}" for p in parsed["params"])

    def dual_post_stubs(self, parsed: dict, spec: MethodSpec, inputs: dict,
                        output: Any, mutated_arg: Optional[tuple] = None,
                        guard_single_case: bool = True) -> Optional[tuple[str, str]]:
        """
        (accept, refute) stubs for one (input, output) postcondition check, or None
        if the spec has no ensures. The stub method is private (its spec may name
        private members) and carries no requires.
          accept: ensures T1; ensures T2; ...      -> verifies iff spec accepts
          refute: ensures !((T1) && (T2) && ...);  -> verifies iff spec rejects
        mutated_arg=(name, jtype): the method is void and modifies this 1-D array
        argument in place; `output` is its final content, written in the body.
        """
        terms = spec.post_terms(guard_single_case=guard_single_case)
        if not terms:
            return None
        assumes = self._input_assumes(parsed, inputs)
        rt = parsed["return_type"]
        body = [assumes]
        if mutated_arg is not None:
            name, jt = mutated_arg
            for j, v in enumerate(output):
                body.append(f"        {name}[{j}] = {self._renderer._scalar(jt.base, v)};")
            body.append("        return;")
        elif rt.base == "void":
            body.append("        return;")
        elif rt.base in ("Map", "HashMap", "TreeMap") and isinstance(output, dict):
            k_t, v_t = self._renderer._arg_type(rt, 0), self._renderer._arg_type(rt, 1)
            concrete = "TreeMap" if rt.base == "TreeMap" else "HashMap"
            body.append(f"        {rt.java_decl()} _r = new java.util.{concrete}<>();")
            for k, v in output.items():
                body.append(f"        _r.put({self._renderer.render(k_t, k)}, {self._renderer.render(v_t, v)});")
            body.append("        return _r;")
        else:
            body.append(f"        return {self._renderer.render(rt, output)};")
        sig = f"    private static {rt.java_decl()} {parsed['method_name']}({self._params_decl(parsed)}) {{\n"
        tail = "\n".join(body) + "\n    }\n"
        accept = "".join(f"    //@ ensures {t};\n" for t in terms) + sig + tail
        # each term in parentheses: `==>` binds more loosely than `&&` in JML
        refute = f"    //@ ensures !({' && '.join(f'({t})' for t in terms)});\n" + sig + tail
        return (self._harness_class(parsed, spec, accept),
                self._harness_class(parsed, spec, refute))

    def effective_pre(self, parsed: dict, spec: MethodSpec,
                      implicit_nonnull: bool) -> Optional[str]:
        """
        The precondition a caller must meet: the spec's requires (per case) plus,
        when implicit_nonnull (RQ1 verifies with --nonnull-by-default), non-null
        reference-typed parameters. None means `true`.
        """
        parts = []
        pre = spec.pre_expr()
        if pre is not None:
            parts.append(f"({pre})")
        if implicit_nonnull:
            for p in parsed["params"]:
                jt = p["jtype"]
                if jt.is_array or jt.is_collection or not jt.is_primitive:
                    parts.append(f"{p['name']} != null")
        return " && ".join(parts) if parts else None

    def dual_pre_stubs(self, parsed: dict, spec: MethodSpec, inputs: dict,
                       psi: str) -> tuple[str, str]:
        """(accept, refute) stubs asserting psi / !psi on the pinned inputs."""
        assumes = self._input_assumes(parsed, inputs)
        sig = f"    private static void {parsed['method_name']}__pre({self._params_decl(parsed)}) {{\n"
        accept = sig + assumes + f"\n        //@ assert {psi};\n    }}\n"
        refute = sig + assumes + f"\n        //@ assert !({psi});\n    }}\n"
        return (self._harness_class(parsed, spec, accept),
                self._harness_class(parsed, spec, refute))

    @staticmethod
    def _indent_members(members: str) -> str:
        """Class members (helper methods) as-is, followed by a newline; empty if none."""
        return (members.rstrip() + "\n") if members and members.strip() else ""

    @staticmethod
    def _indent_jml(jml_block: str, indent: str) -> str:
        """Re-indent each line of the JML block to the given indent level."""
        return "\n".join(
            indent + line.strip() for line in jml_block.splitlines()
            if line.strip()
        )


# ============================================================
# Output mutator  (type-aware)
# ============================================================

class OutputMutator:
    """
    Generates up to k output mutants for PostCompleteness using type-specific
    perturbations guided by JType.

    F1: a mutant equal to the original output is never emitted (it could not
        be killed by any specification and would only cap PostComp).
    F2: string / char / boolean element arrays are supported; the mutator
        never raises. If no type-specific rule applies, it returns [].
    """

    def __init__(self, k: int = 5):
        self.k = k

    # ---- equality that treats -0.0 == 0.0 and NaN == NaN, element-wise in nested structures ----
    @staticmethod
    def _eq(a: Any, b: Any) -> bool:
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(OutputMutator._eq(x, y) for x, y in zip(a, b))
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(OutputMutator._eq(a[k], b[k]) for k in a)
        if isinstance(a, float) or isinstance(b, float):
            try:
                fa, fb = float(a), float(b)
            except (TypeError, ValueError):
                return a == b
            return fa == fb or (fa != fa and fb != fb)
        return a == b

    _INT_RANGE = {"int": (-2**31, 2**31 - 1), "Integer": (-2**31, 2**31 - 1),
                  "long": (-2**63, 2**63 - 1), "Long": (-2**63, 2**63 - 1),
                  "short": (-2**15, 2**15 - 1), "Short": (-2**15, 2**15 - 1),
                  "byte": (-128, 127), "Byte": (-128, 127)}

    @classmethod
    def _in_range(cls, x: Any, base: str | None) -> bool:
        """A mutant must be a value of the output type: integers within the type's range (an
        out-of-range literal does not compile), chars within 0..0xFFFF. Checked element-wise."""
        if isinstance(x, list):
            return all(cls._in_range(y, base) for y in x)
        if isinstance(x, dict):
            return all(cls._in_range(y, base) for y in x.values())
        if isinstance(x, bool) or base is None:
            return True
        if isinstance(x, int) and base in cls._INT_RANGE:
            lo, hi = cls._INT_RANGE[base]
            return lo <= x <= hi
        return True

    def _finish(self, cands: list, v: Any, base: str | None = None, fallbacks: list = ()) -> list:
        """Up to k distinct mutants != v. `fallbacks` are used only when a primary candidate is outside
        the output type's range, so ordinary values keep exactly the documented mutants."""
        ok = [m for m in cands if self._in_range(m, base)]
        if len(ok) < len(cands):
            ok += [m for m in fallbacks if self._in_range(m, base)]
        out: list = []
        for m in ok:
            if self._eq(m, v):
                continue
            if any(self._eq(m, o) for o in out):
                continue
            out.append(m)
            if len(out) >= self.k:
                break
        return out

    def mutate(self, jtype: JType, value: Any) -> list[Any]:
        try:
            if jtype.is_array:
                return self._mutate_array(list(value) if value is not None else [],
                                          jtype.base, jtype.array_dims)
            if jtype.is_collection:
                return self._mutate_collection(jtype, value)
            return self._mutate_scalar(jtype.base, value)
        except Exception:
            return []

    # ---- scalars ----------------------------------------------------------
    def _mutate_scalar(self, base: str, v: Any) -> list:
        if base in ("boolean", "Boolean"):
            return self._finish([not bool(v)], v)
        if base in ("int", "long", "short", "byte", "Integer", "Long", "Short", "Byte"):
            v = int(v)
            # the first five as always; in-range fallbacks when some leave the type's range
            return self._finish([v + 1, v - 1, v + 2, v - 2, 0], v, base, fallbacks=[v - 3, v + 3, 1, -1, v // 2])
        if base in ("double", "float", "Double", "Float"):
            v = float(v)
            if v != v:                                             # NaN: v + 1 etc. would be NaN again
                return self._finish([0.0, 1.0, -1.0, float("inf"), float("-inf")], v)
            return self._finish([v + 1.0, v - 1.0, v * 2.0, -v, 0.0], v)
        if base == "String":
            v = "" if v is None else str(v)
            return self._finish([v + "_x", (v[:-1] if v else "x"),
                                 v.upper(), "", v[::-1], v.lower()], v)
        if base in ("char", "Character"):
            c = ord(str(v)[0]) if v else ord("a")
            cands = [chr(x) for x in (c + 1, c - 1, c + 2, c - 2) if 0 <= x <= 0xFFFF]
            return self._finish(cands[:2] if len(cands) >= 2 else cands, chr(c))
        return []

    # ---- element-level perturbation used by arrays / lists ---------------
    @staticmethod
    def _perturb_elem(x: Any, d: int = 1) -> Any:
        if isinstance(x, bool):
            return not x
        if isinstance(x, int):
            return x + d
        if isinstance(x, float):
            return x + float(d)
        if isinstance(x, str):
            if len(x) != 1:
                return x + "_x"
            c = ord(x) + d
            return chr(c) if 0 <= c <= 0xFFFF else chr(ord(x) - d)
        if isinstance(x, list):
            return [OutputMutator._perturb_elem(y, d) for y in x]
        return x

    @staticmethod
    def _default_elem(base: str) -> Any:
        if base in ("boolean", "Boolean"):
            return False
        if base in ("double", "float", "Double", "Float"):
            return 0.0
        if base in ("String",):
            return ""
        if base in ("char", "Character"):
            return "a"
        return 0

    # ---- arrays (1-D and nested) -----------------------------------------
    def mutate_same_length(self, value: list, base: str | None = None) -> list:
        """
        Output mutants that keep the array length, for void methods that modify an
        array argument in place (its length cannot change): perturb the last,
        the first, and every element; swap the first two; reverse.
        """
        v = list(value) if value is not None else []
        if not v:
            return []
        cands = [v[:-1] + [self._perturb_elem(v[-1])],
                 [self._perturb_elem(v[0])] + v[1:],
                 [self._perturb_elem(x) for x in v]]
        if len(v) > 1:
            cands.append([v[1], v[0]] + v[2:])
            cands.append(v[::-1])
        # in-range fallbacks (perturb down) for elements at the top of the type's range
        fb = [v[:-1] + [self._perturb_elem(v[-1], -1)], [self._perturb_elem(v[0], -1)] + v[1:],
              [self._perturb_elem(x, -1) for x in v]]
        return self._finish(cands, v, base, fallbacks=fb)

    def _mutate_array(self, v: list, base: str = "int", dims: int = 1) -> list:
        cands: list = []
        if v:
            cands.append(v[:-1])                                   # drop last
            last = v[-1]
            cands.append(v + [self._perturb_elem(last)])           # append perturbed
            cands.append([self._perturb_elem(x) for x in v])       # perturb every element
            if isinstance(last, list) and last:
                cands.append(v[:-1] + [last[:-1]])                 # 2-D: drop last elem of last row
        elif dims >= 2:
            cands.append([[]])                                     # empty 2-D -> one empty row
        else:
            cands.append([self._default_elem(base)])               # empty -> one element
        cands.append([])                                           # empty
        fb = ([v + [self._perturb_elem(v[-1], -1)], [self._perturb_elem(x, -1) for x in v]]
              if v else [])                                        # in-range fallbacks (perturb down)
        return self._finish(cands, v, base, fallbacks=fb)

    # ---- collections -------------------------------------------------------
    def _mutate_collection(self, jtype: JType, v: Any) -> list:
        if jtype.base in ("Map", "HashMap", "TreeMap"):
            d = dict(v) if v else {}
            cands: list = []
            if d:
                k0 = next(iter(d))
                cands.append({k: val for k, val in d.items() if k != k0})   # drop first key
                cands.append({k: self._perturb_elem(val) for k, val in d.items()})  # perturb values
            cands.append({})
            vb = jtype.generic_args[1] if len(jtype.generic_args) > 1 else None
            fb = [{k: self._perturb_elem(val, -1) for k, val in d.items()}] if d else []
            return self._finish(cands, d, vb, fallbacks=fb)
        # List / Set: treat as a sequence
        elem_base = jtype.generic_args[0] if jtype.generic_args else "int"
        return self._mutate_array(list(v) if v else [], elem_base)


# ============================================================
# Shared verdict parsing (used by both runners)
# ============================================================

def parse_openjml_output(output: str, returncode: Optional[int] = None) -> VerifyResult:
    """
    F3: distinguish verification failures from tool/compile errors.
      FAIL    - "N warning(s)" or "N verification failure(s)"
      ERROR   - compile / tool error ("error" present, no verification report)
      OK      - returncode 0 (when a returncode is given); without one: "verified", "0 warnings",
                or empty output
      UNKNOWN - anything else (timeouts are mapped by the caller)
    """
    low = output.strip().lower()
    if re.search(r'[1-9]\d* warning', low):
        return VerifyResult.FAIL
    if re.search(r'[1-9]\d* verification failure', low):
        return VerifyResult.FAIL
    if re.search(r'\berrors?\b', low):
        return VerifyResult.ERROR
    if returncode is not None:
        # a run that did not exit normally (killed, crashed) is never a proof, whatever it printed
        return VerifyResult.OK if returncode == 0 else VerifyResult.UNKNOWN
    if "verified" in low or "0 warnings" in low or low == "":
        return VerifyResult.OK
    return VerifyResult.UNKNOWN


def classify_pre_verdict(verdict: VerifyResult, output: str) -> VerifyResult:
    """
    F5: for precondition checks, only a call-site precondition / non-null
    assertion counts as a rejection. Any other FAIL is not attributable to
    the `requires` clause and is reported as ERROR.
    """
    if verdict != VerifyResult.FAIL:
        return verdict
    low = output.lower()
    if any(lbl in low for lbl in PRECONDITION_LABELS):
        return VerifyResult.FAIL
    return VerifyResult.ERROR


# ============================================================
# Integrity helpers (F10)
# ============================================================

_JML_LINE  = re.compile(r'//@[^\n]*')
_JML_BLOCK = re.compile(r'/\*@.*?\*/', re.S)
_CMT_LINE  = re.compile(r'//[^\n]*')
_CMT_BLOCK = re.compile(r'/\*.*?\*/', re.S)


def strip_comments(src: str) -> str:
    """Remove JML annotations and ordinary Java comments (a lexer that leaves string / char literals
    intact, so "http://x" is not cut)."""
    parts, last = [], 0
    for s_, e_, _ in java_comments(src):
        parts.append(src[last:s_])
        parts.append(" ")
        last = e_
    parts.append(src[last:])
    return "".join(parts)


def extract_method_body(src: str, method_name: str) -> Optional[str]:
    """
    Return the whitespace-normalised body of `method_name` (text between the
    outermost braces), with all comments removed. None if not found.
    """
    clean = strip_comments(src)
    m = re.search(r'\b' + re.escape(method_name) + r'\s*\([^)]*\)\s*(?:throws[^{]*)?\{', clean)
    if not m:
        return None
    i = m.end()          # index just after the opening brace
    depth = 1
    j = i
    while j < len(clean) and depth > 0:
        ch = clean[j]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        j += 1
    if depth != 0:
        return None
    body = clean[i:j - 1]
    return re.sub(r'\s+', ' ', body).strip()


def _method_ast_signature(src: str, method_name: str):
    """
    Structural signature of a method body: the javalang AST with positions and
    comments dropped, and `{ single statement }` blocks collapsed to the statement.
    Adding braces around a single statement (e.g. to place a loop invariant) is
    therefore not a change, while regrouping statements is. None if unparseable.
    """
    wrapped, _ = JavaMethodParser._ensure_class(src)
    try:
        tree = javalang.parse.parse(wrapped)
    except Exception:
        return None
    method = next((n for _, n in tree
                   if isinstance(n, jtree.MethodDeclaration) and n.name == method_name), None)
    if method is None:
        return None

    def norm(x):
        if isinstance(x, jtree.BlockStatement) and x.statements and len(x.statements) == 1:
            return norm(x.statements[0])
        if isinstance(x, jtree.Node):
            return (type(x).__name__,
                    tuple((a, norm(getattr(x, a, None))) for a in x.attrs if a != "documentation"))
        if isinstance(x, (list, tuple)):
            return tuple(norm(y) for y in x)
        if isinstance(x, set):
            return tuple(sorted(str(y) for y in x))
        return x

    return norm(method.body)


def _java_tokens(code: str):
    """Token values of a comment-free Java fragment (javalang); falls back to
    whitespace-normalised text if tokenisation fails."""
    try:
        return [t.value for t in javalang.tokenizer.tokenize(code)]
    except Exception:
        return re.sub(r"\s+", " ", code).strip()


def _norm_node(x):
    """A javalang node as a comparable value: positions and documentation dropped, `{ single statement }`
    blocks collapsed (braces added to place an annotation are not a change)."""
    if isinstance(x, jtree.BlockStatement) and x.statements and len(x.statements) == 1:
        return _norm_node(x.statements[0])
    if isinstance(x, jtree.Node):
        return (type(x).__name__,
                tuple((a, _norm_node(getattr(x, a, None))) for a in x.attrs if a != "documentation"))
    if isinstance(x, (list, tuple)):
        return tuple(_norm_node(y) for y in x)
    if isinstance(x, set):
        return tuple(sorted(str(y) for y in x))
    return x


def _class_members(src: str, method_name: str) -> Optional[dict]:
    """key -> normalized node of every member of the class that declares `method_name` (other than that
    method): methods and constructors by (name, parameter types), fields by their declarator names. None
    if the source cannot be parsed."""
    wrapped, _ = JavaMethodParser._ensure_class(src)
    try:
        tree = javalang.parse.parse(wrapped)
    except Exception:
        return None
    owner = None
    for path, node in tree:
        if isinstance(node, jtree.MethodDeclaration) and node.name == method_name:
            owners = [x for x in path if isinstance(x, jtree.ClassDeclaration)]
            owner = owners[-1] if owners else None
            break
    if owner is None:
        return None
    out = {}
    for m in owner.body:
        if isinstance(m, (jtree.MethodDeclaration, jtree.ConstructorDeclaration)):
            if isinstance(m, jtree.MethodDeclaration) and m.name == method_name:
                continue
            key = ("method", m.name, tuple(str(_norm_node(p.type)) for p in (m.parameters or [])))
        elif isinstance(m, jtree.FieldDeclaration):
            key = ("field", tuple(d.name for d in m.declarators))
        else:
            key = (type(m).__name__, getattr(m, "name", None))
        out[key] = _norm_node(m)
    return out


def check_integrity(bench_src: str, llm_src: str,
                    bench_parsed: dict, llm_parsed: dict) -> dict:
    """
    F10: verify the LLM only added annotations.
    Returns {"class_renamed", "signature_changed", "body_modified", "members_modified", "reason"}.
    The target method must keep its signature and body; every other member of the benchmark class
    (helper methods, fields, constructors) must be present and unchanged (added members are allowed:
    e.g. pure helper methods for the specification).
    """
    res = {"class_renamed": False, "signature_changed": False,
           "body_modified": False, "reason": ""}
    reasons = []

    if bench_parsed["class_name"] != llm_parsed["class_name"]:
        res["class_renamed"] = True
        reasons.append(f"class {bench_parsed['class_name']} -> {llm_parsed['class_name']}")

    b_sig = ([p["jtype"].java_decl() for p in bench_parsed["params"]],
             bench_parsed["return_type"].java_decl(),
             bench_parsed["method_name"])
    l_sig = ([p["jtype"].java_decl() for p in llm_parsed["params"]],
             llm_parsed["return_type"].java_decl(),
             llm_parsed["method_name"])
    if b_sig != l_sig:
        res["signature_changed"] = True
        reasons.append(f"signature {b_sig} -> {l_sig}")

    b_body = extract_method_body(bench_src, bench_parsed["method_name"])
    l_body = extract_method_body(llm_src, llm_parsed["method_name"])
    if b_body is not None and l_body is not None:
        # compare token streams, so pure reformatting (x=0 vs x = 0) is not a change
        b_body, l_body = _java_tokens(b_body), _java_tokens(l_body)
    if b_body is None or l_body is None:
        res["body_modified"] = True
        reasons.append("could not extract method body")
    elif b_body != l_body:
        # tokens differ: accept only if the syntax trees match once
        # single-statement blocks are collapsed (braces added for annotations)
        b_ast = _method_ast_signature(bench_src, bench_parsed["method_name"])
        l_ast = _method_ast_signature(llm_src, llm_parsed["method_name"])
        if b_ast is not None and b_ast == l_ast:
            res["benign_brace_change"] = True
        else:
            res["body_modified"] = True
            reasons.append("method body differs from benchmark")

    b_mem = _class_members(bench_src, bench_parsed["method_name"])
    l_mem = _class_members(llm_src, llm_parsed["method_name"])
    res["members_modified"] = False
    if b_mem is not None and l_mem is not None:
        changed = [k for k in b_mem if l_mem.get(k) != b_mem[k]]
        if changed:
            res["members_modified"] = True
            res["body_modified"] = True          # consumers treat any change of the program alike
            reasons.append("other members changed: " + ", ".join(str(k[1]) for k in changed[:5]))

    res["reason"] = "; ".join(reasons)
    return res


def jml_assume_in_body(src: str, method_name: str) -> bool:
    """True if a JML `assume` annotation appears inside the body of `method_name` (it lets OpenJML verify
    any specification; the RQ4 agent tools reject it)."""
    code = mask_comments_and_literals(src)
    m = re.search(r"\b" + re.escape(method_name) + r"\s*\([^)]*\)\s*(?:throws[^{]*)?\{", code)
    if not m:
        return False
    depth, j = 1, m.end()
    while j < len(code) and depth:
        depth += {"{": 1, "}": -1}.get(code[j], 0)
        j += 1
    for s_, e_, _ in java_comments(src):
        if m.end() <= s_ < j:
            body = src[s_ + 2:e_]
            if body.startswith("@") and re.search(r"\bassume\b", body):
                return True
    return False


def agent_code_violation(original_code: str, code: str) -> Optional[str]:
    """
    The RQ4 agent tools' rule for a tool call (identical for every agent): a reason if `code` changes the
    program (class name, the method's signature or body, any other member of the class) or puts a JML
    `assume` inside the method body; None if the call may run. Code that cannot be parsed is let through
    (OpenJML reports the syntax error).
    """
    if not original_code:
        return None
    try:
        parser = JavaMethodParser()
        orig = parser.parse(original_code)
        new = parser.parse(code, orig["method_name"])
        r = check_integrity(original_code, code, orig, new)
    except Exception:
        return None
    if r["class_renamed"] or r["signature_changed"] or r["body_modified"]:
        return r["reason"] or "code differs from the original program"
    if jml_assume_in_body(code, orig["method_name"]):
        return "JML `assume` inside the method body (it would make any specification verify)"
    return None


# ============================================================
# OpenJML runner
# ============================================================

class OpenJMLRunner:

    def __init__(self, openjml_path: str = "openjml",
                 timeout: int = 300):          # F9: match eval runner
        self.openjml_path = openjml_path
        self.timeout      = timeout

    def verify(self, java_source: str,
               class_name: str) -> tuple[VerifyResult, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, f"{class_name}Harness.java")
            with open(path, "w") as f:
                f.write(java_source)
            try:
                r = subprocess.run(
                    [self.openjml_path, "-esc", path],
                    capture_output=True, text=True,
                    timeout=self.timeout,
                )
                out = r.stdout + r.stderr
                return self._parse(out), out
            except subprocess.TimeoutExpired:
                return VerifyResult.UNKNOWN, "timeout"
            except FileNotFoundError:
                raise RuntimeError(
                    f"OpenJML binary not found at '{self.openjml_path}'. "
                    "Install OpenJML and pass its path via openjml_path=."
                )

    @staticmethod
    def _parse(output: str) -> VerifyResult:
        return parse_openjml_output(output)


# ============================================================
# SpecHarness  —  main API
# ============================================================

class SpecHarness:
    """
    Evaluates a LLM-generated JML spec against all four spec-harness metrics.

    Parameters
    ----------
    java_source   : Java class or method source string (parsed by javalang)
    spec          : MethodSpec holding raw JML precondition / postcondition
    openjml_path  : path to the OpenJML binary (default: "openjml")
    mutant_budget : max output mutants per test pair for PostCompleteness
    verbose       : print per-case verdicts to stdout

    Usage
    -----
    harness = SpecHarness(java_source, spec, openjml_path="/opt/openjml/openjml")
    results = harness.evaluate(test_pairs, input_cases)
    harness.save_results(results, "results.json")
    """

    def __init__(self, java_source: str, spec: MethodSpec,
                 openjml_path:  str  = "openjml",
                 mutant_budget: int  = 5,
                 verbose:       bool = False):

        self.spec    = spec
        self.verbose = verbose

        # Keep the original annotated source for call-site pre-condition stubs
        self._java_source = java_source

        # Parse the method signature once via javalang
        self._parsed = JavaMethodParser().parse(java_source)
        self._cname  = self._parsed["class_name"]
        self._rtype  = self._parsed["return_type"]

        self._builder = StubBuilder()
        self._runner  = OpenJMLRunner(openjml_path)
        self._mutator = OutputMutator(k=mutant_budget)

    # ------------------------------------------------------------------
    # Metric 1 — PostCorrectness
    # ------------------------------------------------------------------

    def post_correctness(self, pairs: list[TestPair]) -> HarnessResult:
        """
        PostCorrectness(phi, T) =
            |{ (i,o) in T : |= {true} x:=i; result:=o {phi(x,result)} }|
            ---------------------------------------------------------------
                                        |T|
        Desired outcome per case: VERIFY  (no false alarm on valid output)
        """
        res = HarnessResult("PostCorrectness", 0, 0)
        for pair in pairs:
            stub = self._builder.post_correctness_stub(
                       self._parsed, self.spec, pair)
            verdict, log = self._runner.verify(stub, self._cname)
            if verdict == VerifyResult.ERROR:          # F3
                res.errors += 1
                res.details.append({"label": pair.label,
                                     "verdict": verdict.value, "pass": None})
                continue
            res.total += 1
            ok = (verdict == VerifyResult.OK)
            if ok:
                res.passed += 1
            res.details.append({"label": pair.label,
                                 "verdict": verdict.value, "pass": ok})
            self._log("PostCorrectness", pair.label, verdict, ok)
        return res

    # ------------------------------------------------------------------
    # Metric 2 — PostCompleteness
    # ------------------------------------------------------------------

    def post_completeness(self, pairs: list[TestPair]) -> HarnessResult:
        """
        PostCompleteness(phi, T) =
            |{ (i,o') in T1 : not |= {true} x:=i; result:=o' {phi} }|
            -------------------------------------------------------------
                                        |T1|
        where T1 = union of output-mutant pairs for each (i,o) in T.
        Desired outcome per case: FAIL  (mutant correctly rejected)
        """
        all_mutants = [
            (pair, m)
            for pair in pairs
            for m in self._mutator.mutate(self._rtype, pair.output)
        ]
        res = HarnessResult("PostCompleteness", 0, 0)
        for pair, mut_out in all_mutants:
            stub = self._builder.post_completeness_stub(
                       self._parsed, self.spec, pair, mut_out)
            verdict, log = self._runner.verify(stub, self._cname)
            if verdict == VerifyResult.ERROR:          # F3
                res.errors += 1
                res.details.append({"label": pair.label, "mutant": str(mut_out),
                                     "verdict": verdict.value, "killed": None})
                continue
            res.total += 1
            killed = (verdict == VerifyResult.FAIL)   # UNKNOWN is not a kill
            if killed:
                res.passed += 1
            res.details.append({"label": pair.label, "mutant": str(mut_out),
                                 "verdict": verdict.value, "killed": killed})
            self._log("PostCompleteness", pair.label, verdict, killed,
                      extra=f"mutant={mut_out}")
        return res

    # ------------------------------------------------------------------
    # Metric 3 — PreCorrectness
    # ------------------------------------------------------------------

    def pre_correctness(self, cases: list[InputCase]) -> HarnessResult:
        """
        PreCorrectness(psi, T+) =
            |{ i in T+ : |= {true} x:=i {psi(x)} }|
            ------------------------------------------
                                |T+|
        Desired outcome per case: VERIFY  (valid input admitted, no false rejection)

        Uses a call-site stub: the harness *calls* the annotated method
        with concrete values so OpenJML evaluates the requires clause.
        """
        valid = [c for c in cases if c.valid]
        if not self.spec.has_requires:                          # F8
            if MISSING_REQUIRES_POLICY == "zero":
                return HarnessResult("PreCorrectness", 0, 0)
            if MISSING_REQUIRES_POLICY == "requires_true":
                return HarnessResult("PreCorrectness", len(valid), len(valid))
            # "jml": fall through and run the call-site check
        res   = HarnessResult("PreCorrectness", 0, 0)
        for case in valid:
            stub = self._builder.pre_correctness_stub(
                       self._parsed, self.spec, case,
                       original_source=self._java_source)
            verdict, log = self._runner.verify(stub, self._cname)
            verdict = classify_pre_verdict(verdict, log)          # F5
            if verdict == VerifyResult.ERROR:
                res.errors += 1
                res.details.append({"label": case.label,
                                     "verdict": verdict.value, "pass": None})
                continue
            res.total += 1
            ok = (verdict == VerifyResult.OK)
            if ok:
                res.passed += 1
            res.details.append({"label": case.label,
                                 "verdict": verdict.value, "pass": ok})
            self._log("PreCorrectness", case.label, verdict, ok)
        return res

    # ------------------------------------------------------------------
    # Metric 4 — PreCompleteness
    # ------------------------------------------------------------------

    def pre_completeness(self, cases: list[InputCase]) -> HarnessResult:
        """
        PreCompleteness(psi, T-) =
            |{ i' in T- : not |= {true} x:=i' {psi(x)} }|
            ------------------------------------------------
                                |T-|
        T- = known boundary/edge inputs that violate the method domain.
        Desired outcome per case: FAIL  (invalid input correctly rejected)

        Uses a call-site stub: the harness *calls* the annotated method
        with concrete values so OpenJML evaluates the requires clause.
        """
        invalid = [c for c in cases if not c.valid]
        if not self.spec.has_requires:                          # F8
            if MISSING_REQUIRES_POLICY == "zero":
                return HarnessResult("PreCompleteness", 0, 0)
            if MISSING_REQUIRES_POLICY == "requires_true":
                return HarnessResult("PreCompleteness", len(invalid), 0)
            # "jml": fall through and run the call-site check
        res     = HarnessResult("PreCompleteness", 0, 0)
        for case in invalid:
            stub = self._builder.pre_completeness_stub(
                       self._parsed, self.spec, case,
                       original_source=self._java_source)
            verdict, log = self._runner.verify(stub, self._cname)
            verdict = classify_pre_verdict(verdict, log)          # F5
            if verdict == VerifyResult.ERROR:
                res.errors += 1
                res.details.append({"label": case.label,
                                     "verdict": verdict.value, "rejected": None})
                continue
            res.total += 1
            rejected = (verdict == VerifyResult.FAIL)
            if rejected:
                res.passed += 1
            res.details.append({"label": case.label,
                                 "verdict": verdict.value, "rejected": rejected})
            self._log("PreCompleteness", case.label, verdict, rejected)
        return res

    # ------------------------------------------------------------------
    # Convenience: run all four
    # ------------------------------------------------------------------

    def evaluate(self, test_pairs:  list[TestPair],
                 input_cases: list[InputCase]) -> dict[str, HarnessResult]:
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  Spec-Harness  |  method: {self._parsed['method_name']}")
        print(sep)
        results = {
            "post_correctness":  self.post_correctness(test_pairs),
            "post_completeness": self.post_completeness(test_pairs),
            "pre_correctness":   self.pre_correctness(input_cases),
            "pre_completeness":  self.pre_completeness(input_cases),
        }
        print(f"\n{sep}")
        for r in results.values():
            print(f"  {r}")
        print(sep)
        return results

    def save_results(self, results: dict[str, HarnessResult],
                     path: str) -> None:
        out = {
            k: {"score": v.score, "passed": v.passed,
                "total": v.total, "errors": v.errors, "details": v.details}
            for k, v in results.items()
        }
        with open(path, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Results saved → {path}")

    def _log(self, metric: str, label: str,
             verdict: VerifyResult, desired: bool,
             extra: str = "") -> None:
        if not self.verbose:
            return
        status = "✓" if desired else "✗"
        ex = f"  [{extra}]" if extra else ""
        print(f"  [{metric:20s}] {label}{ex}  →  {verdict.value}  {status}")


# ============================================================
# Utility: parse a raw generic-arg string back to JType
# ============================================================

def _parse_raw_jtype(raw: str) -> JType:
    """
    Minimal parser for strings like "Integer", "int[]", "List<String>",
    "Map<String, List<Integer>>".  Used when rendering nested generics.
    """
    raw = raw.strip()
    # Count and strip trailing []
    array_dims = 0
    while raw.endswith("[]"):
        array_dims += 1
        raw = raw[:-2]
    # Generic  e.g. List<Integer> or Map<String, List<Integer>>
    m = re.match(r'^(\w+)<(.+)>$', raw)
    if m:
        base  = m.group(1)
        # Split top-level generic args (respecting nested <…>)
        inner = _split_generic_args(m.group(2))
        return JType(base=base, array_dims=array_dims, generic_args=inner)
    return JType(base=raw, array_dims=array_dims)


def _split_generic_args(s: str) -> list[str]:
    """Split 'String, List<Integer>' respecting nested angle brackets."""
    depth, buf, result = 0, [], []
    for ch in s:
        if ch == "<":
            depth += 1; buf.append(ch)
        elif ch == ">":
            depth -= 1; buf.append(ch)
        elif ch == "," and depth == 0:
            result.append("".join(buf).strip()); buf = []
        else:
            buf.append(ch)
    if buf:
        result.append("".join(buf).strip())
    return result


# ============================================================
# Example usage
# ============================================================


# ==== from spec_harness/eval_spec.py ====

import re
from dataclasses import dataclass, field
from typing import Any, Optional
import javalang
import javalang.tree as jtree


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

# ==== from spec_harness/eval_llm_response.py ====
"""
Batch-evaluate LLM-generated JML annotations from a JSONL response file
against a benchmark JSON, with two levels of threading:

  --threads    : outer parallelism — how many tasks to evaluate concurrently
  --max-pairs  : number of test pairs from benchmark test_inputs to use;
                 each test pair gets its own thread to compute all 4 metrics

Only responses with "status": "verified" are evaluated.

Fixes in this version:
  F3  compile/tool errors are a separate ERROR verdict, excluded from
      denominators and counted in `errors`
  F4  postcondition stubs check psi => phi per specification case (flag --post-stub-mode)
  F5  precondition checks use a trivial callee; only call-site Precondition /
      non-null warnings count as rejections
  F6  collection-typed tasks excluded by default (flag --keep-collection-tasks)
  F7  void-returning tasks excluded
  F8  missing `requires` policy (flag --missing-requires-policy)
  F10 class / signature / body integrity check (token-level); a modified submission
      is kept with all scores 0 (integrity_failed=True), i.e. counted as a failure
  D13 no `ensures` on the method: kept with all scores 0 (no_postcondition=True), not verified

Usage
-----
  python eval_spec_with_model_benchmark_response.py \\
      --benchmark_path benchmarks/benchmark.json \\
      --llm_response_path responses.jsonl \\
      --openjml /path/to/openjml \\
      --output results/ \\
      --threads 4 \\
      --max-pairs 10 \\
      [-v]
"""


from asyncio.log import logger
import os
import re
import json
import argparse
import atexit
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any



# Data classes

@dataclass
class TestCase:
    input: str
    output: str

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "TestCase":
        return cls(input=data["input"], output=data["output"])


@dataclass
class Task:
    task_id: str
    code: str
    class_name: str
    test_name: str
    javadoc: str
    category: str
    origin_id: str
    test_code: str = ""
    test_inputs: list[TestCase] = field(default_factory=list)
    generated_test_cases: list[TestCase] = field(default_factory=list)
    # validated invalid inputs (testset T6): None -> the old heuristic generator; [] -> no invalid input
    invalid_inputs: list[dict] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            task_id=data["task_id"],
            code=data["code"],
            class_name=data["class_name"],
            test_name=data["test_name"],
            javadoc=data.get("javadoc", ""),
            category=data.get("category", ""),
            origin_id=data.get("origin_id", ""),
            test_code=data.get("test_code", ""),
            test_inputs=[TestCase.from_dict(tc) for tc in data.get("test_inputs", [])],
            generated_test_cases=[
                TestCase.from_dict(tc) for tc in data.get("generated_test_cases", [])
            ],
            invalid_inputs=data.get("invalid_inputs"),
        )


@dataclass
class LLMResponse:
    """One line from the JSONL response file."""

    task_id: str
    status: str
    final_code: str
    class_name: str
    config: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMResponse":
        return cls(
            task_id=data["task_id"],
            status=data.get("status", ""),
            final_code=data.get("final_code", ""),
            class_name=data.get("class_name", "Solution"),
            config=data.get("config", {}),
        )



# Loaders



def load_benchmark(benchmark_path: str) -> dict[str, Task]:
    """Load benchmark JSON once and return a task_id -> Task lookup dict."""
    with open(benchmark_path) as f:
        benchmark = json.load(f)
    return {entry["task_id"]: Task.from_dict(entry) for entry in benchmark}


def load_verified_responses(jsonl_path: str) -> list[LLMResponse]:
    """Read JSONL, return only responses with status == 'verified'."""
    responses: list[LLMResponse] = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("status") == "verified":
                responses.append(LLMResponse.from_dict(obj))
    return responses



# OpenJML runner — writes stubs to a persistent directory

class JvmSlots:
    """Process-wide cap on concurrently running OpenJML JVMs (0 = no cap).

    A dual decision takes 2 slots at once (accept + refute), a single verification 1. Slots are taken
    before the JVMs start and returned when they end, and every timeout is measured from the launch,
    so waiting for a slot never counts against the 300 s. Set with set_jvm_slots(n) or the environment
    variable SPEC_HARNESS_JVM_SLOTS (runners use this to match the machine's cores)."""

    def __init__(self, n: int = 0):
        self.n, self.used = n, 0
        self.cv = threading.Condition()

    def acquire(self, k: int) -> None:
        with self.cv:
            while self.n and self.used + k > self.n:
                self.cv.wait()
            self.used += k

    def release(self, k: int) -> None:
        with self.cv:
            self.used -= k
            self.cv.notify_all()


def _slots(n: int) -> int:
    """0 = no cap; otherwise at least 2 (a dual decision needs 2 slots at once, 1 would deadlock)."""
    return 0 if n <= 0 else max(2, n)


JVM_SLOTS = JvmSlots(_slots(int(os.environ.get("SPEC_HARNESS_JVM_SLOTS", "0") or 0)))


def set_jvm_slots(n: int) -> None:
    with JVM_SLOTS.cv:
        JVM_SLOTS.n = _slots(n)
        JVM_SLOTS.cv.notify_all()


# Every OpenJML call runs in its own session (setsid) so that a timeout kills the whole tree (wrapper
# script, JVM, cvc4). Such a group does not receive the caller's Ctrl-C or kill, so the live groups are
# tracked and killed when this process exits or is stopped; otherwise they outlive a stopped run.
_LIVE_GROUPS: set[int] = set()
_LIVE_LOCK = threading.Lock()
STOPPING = False                      # set by the stop handler: results computed from now on are not saved


def _launch(cmd: list, **kw) -> subprocess.Popen:
    """Start an OpenJML process in its own session and track its group, atomically with respect to the
    stop handler: once stopping, nothing new is launched."""
    with _LIVE_LOCK:
        if STOPPING:
            raise RuntimeError("stopping: no new OpenJML runs")
        p = subprocess.Popen(cmd, preexec_fn=os.setsid, **kw)
        _LIVE_GROUPS.add(p.pid)       # setsid: the group id is the child's pid
        return p


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    with _LIVE_LOCK:
        _LIVE_GROUPS.discard(p.pid)


def kill_live_groups() -> None:
    with _LIVE_LOCK:
        groups = list(_LIVE_GROUPS)
    for g in groups:
        try:
            os.killpg(g, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


atexit.register(kill_live_groups)


def install_stop_handlers(*also) -> None:
    """For long runs (call from the main thread): on SIGINT / SIGTERM / SIGHUP kill every live OpenJML
    group (and call each function in `also`), then exit at once. Nothing is written after the signal, so
    no half-scored task is saved; the task is redone on resume (its lock belongs to a dead process)."""
    def stop(signum, frame):
        global STOPPING
        with _LIVE_LOCK:
            STOPPING = True           # no launch after this point (_launch checks it under the lock)
        kill_live_groups()
        for f in also:
            try:
                f()
            except Exception:
                pass
        print(f"stopped by signal {signum}: OpenJML processes killed", file=sys.stderr, flush=True)
        kill_live_groups()            # again: a group registered while the first pass ran
        os._exit(128 + signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)


class OpenJMLRunnerPersistent:
    """Runs OpenJML ESC, writing harness stubs to an actual directory."""

    def __init__(self, openjml_path: str, output_dir: str, timeout: int = 300):
        self.openjml_path = openjml_path
        self.output_dir = output_dir
        self.timeout = timeout
        os.makedirs(output_dir, exist_ok=True)

    def verify(
        self, java_source: str, class_name: str, label: str = ""
    ) -> tuple[VerifyResult, str]:
        # Filename MUST match the public class name inside the stub
        # (OpenJML / javac requires this).  Use a per-label subdirectory
        # so that parallel invocations don't overwrite each other.
        harness_class = f"{class_name}Harness"
        fname = f"{harness_class}.java"
        if label:
            safe_label = re.sub(r"[^\w\-]", "_", label)
            sub_dir = os.path.join(self.output_dir, safe_label)
            os.makedirs(sub_dir, exist_ok=True)
            path = os.path.join(sub_dir, fname)
        else:
            path = os.path.join(self.output_dir, fname)
        with open(path, "w") as f:
            f.write(java_source)
        cmd = [
            self.openjml_path,          # F9: honour --openjml (was hard-coded "openjml")
            "--esc",
            "--esc-max-warnings",
            "1",
            "--prover=cvc4",
            "--nonnull-by-default",
            "--arithmetic-failure=quiet",
            "-nowarn",
            path,
        ]
        JVM_SLOTS.acquire(1)
        try:
            proc = _launch(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception:
            JVM_SLOTS.release(1)
            raise
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
            returncode = proc.returncode
            out = stdout + stderr
            return self._parse(returncode, out), out
        except subprocess.TimeoutExpired:
            return VerifyResult.UNKNOWN, "timeout"
        except FileNotFoundError:
            raise RuntimeError(f"OpenJML binary not found at '{self.openjml_path}'.")
        except Exception as e:
            return VerifyResult.UNKNOWN, f"Error: {str(e)}"
        finally:
            _kill_group(proc)
            JVM_SLOTS.release(1)

    @staticmethod
    def _parse(returncode: int, output: str) -> VerifyResult:
        # F3: shared parser; compile/tool errors -> ERROR, not FAIL
        return parse_openjml_output(output, returncode)

    # ---- D8: dual accept / refute decision -----------------------------

    def _cmd(self, path: str) -> list[str]:
        # Exactly the verification command used for RQ1.
        return [self.openjml_path, "--esc", "--esc-max-warnings", "1", "--prover=cvc4",
                "--nonnull-by-default", "--arithmetic-failure=quiet", "-nowarn", path]

    def decide(self, accept_src: str, refute_src: str, class_name: str,
               label: str, wait_both: bool = False) -> tuple[str, dict]:
        """
        Run the accept and refute stubs concurrently. Returns (decision, info):
          "accept"  - the accept stub verified (the spec provably holds)
          "reject"  - the refute stub verified (the spec provably fails)
          "unknown" - neither verified within the timeout
          "error"   - both stubs failed to compile / the tool failed, or (wait_both) both verified
        wait_both (vacuity guard, used when the spec's helper methods carry JML contracts, which could be
        contradictory): do not stop at the first proof; wait for the other side (same timeout) and report
        "error" (vacuous) if it verifies too. Each stub's OpenJML output is kept next to it (openjml.out).
        """
        import time
        base = os.path.join(self.output_dir, re.sub(r"[^\w\-]", "_", label))
        procs = {}
        JVM_SLOTS.acquire(2)                      # both JVMs of this decision; the timer starts after launch
        try:
            for side, src in (("accept", accept_src), ("refute", refute_src)):
                d = os.path.join(base, side)
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, f"{class_name}Harness.java")
                with open(path, "w") as fh:
                    fh.write(src)
                out = open(os.path.join(d, "openjml.out"), "w")
                try:
                    p = _launch(self._cmd(path), stdout=out, stderr=subprocess.STDOUT, text=True)
                except Exception:
                    out.close()
                    raise
                procs[side] = (p, out, os.path.join(d, "openjml.out"))
        except Exception:                         # launch failed: stop what started, free the slots
            for p, out, _ in procs.values():
                _kill_group(p)
                out.close()
            JVM_SLOTS.release(2)
            raise
        start, verdicts = time.monotonic(), {}
        try:
            while len(verdicts) < 2 and time.monotonic() - start < self.timeout:
                for side, (p, out, opath) in procs.items():
                    if side in verdicts or p.poll() is None:
                        continue
                    _kill_group(p)                # the side has ended: stop any solver it left behind
                    out.close()
                    with open(opath, errors="ignore") as fh:
                        verdicts[side] = self._parse(p.returncode, fh.read())
                    if verdicts[side] == VerifyResult.OK and not wait_both:
                        return ("accept" if side == "accept" else "reject"), \
                               {"verdicts": {k: v.value for k, v in verdicts.items()},
                                "seconds": round(time.monotonic() - start, 2)}
                time.sleep(0.2)
            info = {"verdicts": {k: v.value for k, v in verdicts.items()},
                    "seconds": round(time.monotonic() - start, 2),
                    "timed_out": [s for s in procs if s not in verdicts]}
            ok = [side for side, v in verdicts.items() if v == VerifyResult.OK]
            if len(ok) == 2:
                return "error", {**info, "vacuous": True}      # both provable: contradictory context
            if len(ok) == 1:
                info["wait_both"] = True
                return ("accept" if ok[0] == "accept" else "reject"), info
            if len(verdicts) == 2 and all(v == VerifyResult.ERROR for v in verdicts.values()):
                return "error", info
            return "unknown", info
        finally:
            for side, (p, out, _) in procs.items():
                _kill_group(p)
                try:
                    out.close()
                except Exception:
                    pass
            JVM_SLOTS.release(2)



# Per-pair result — returned by each inner thread
@dataclass
class PairResult:
    """All 4 metric results for a single test pair."""

    pair_idx: int
    # PostCorrectness
    post_correct_detail: dict = field(default_factory=dict)
    # PostCompleteness (multiple mutants)
    post_complete_details: list[dict] = field(default_factory=list)
    # PreCorrectness (valid input for this pair)
    pre_correct_detail: dict = field(default_factory=dict)
    # PreCompleteness (invalid inputs assigned to this pair)
    pre_complete_details: list[dict] = field(default_factory=list)



# Inner thread: compute all 4 metrics for one test pair

def _evaluate_one_pair(
    pair_idx: int,
    pair: TestPair,
    valid_input_case: InputCase,
    invalid_cases: list[InputCase],
    parsed: dict,
    spec: MethodSpec,
    rtype: JType,
    builder: StubBuilder,
    mutator: OutputMutator,
    runner: OpenJMLRunnerPersistent,
    cname: str,
    verbose: bool,
    task_id: str,
    java_source: str = "",
    skip_post: bool = False,
) -> PairResult:
    """Compute all 4 metrics for a single test pair in its own thread."""
    result = PairResult(pair_idx=pair_idx)
    run_pre = _should_run_pre(spec)

    # PostCorrectness
    if spec.postcondition is not None and not skip_post:
        stub = builder.post_correctness_stub(parsed, spec, pair)
        verdict, out = runner.verify(stub, cname, f"post_correct_{pair.label}")
        ok = None if verdict == VerifyResult.ERROR else (verdict == VerifyResult.OK)
        result.post_correct_detail = {
            "label": pair.label,
            "verdict": verdict.value,
            "pass": ok,
            "stub": stub,
        }
        _log(verbose, task_id, "PostCorrectness", pair.label, verdict, bool(ok))

    # PostCompleteness
    if spec.postcondition is not None and not skip_post:
        try:
            mutants = mutator.mutate(rtype, pair.output)
        except Exception as e:                      # F2 guard (mutator should not raise)
            mutants = []
            print(f"WARNING [{task_id}] mutator failed on {pair.label}: {e}", file=sys.stderr)
        for mut_out in mutants:
            stub = builder.post_completeness_stub(parsed, spec, pair, mut_out)
            lbl = f"post_complete_{pair.label}_{str(mut_out)[:20]}"
            verdict, out = runner.verify(stub, cname, lbl)
            killed = None if verdict == VerifyResult.ERROR else (verdict == VerifyResult.FAIL)
            result.post_complete_details.append(
                {
                    "label": pair.label,
                    "mutant": str(mut_out),
                    "verdict": verdict.value,
                    "killed": killed,
                    "stub": stub,
                }
            )
            _log(verbose, task_id, "PostCompleteness", pair.label, verdict,
                 bool(killed), f"mutant={mut_out}")

    # PreCorrectness
    if run_pre:
        stub = builder.pre_correctness_stub(
            parsed, spec, valid_input_case, original_source=java_source
        )
        verdict, out = runner.verify(stub, cname, f"pre_correct_{valid_input_case.label}")
        verdict = classify_pre_verdict(verdict, out)          # F5
        ok = None if verdict == VerifyResult.ERROR else (verdict == VerifyResult.OK)
        result.pre_correct_detail = {
            "label": valid_input_case.label,
            "verdict": verdict.value,
            "pass": ok,
            "stub": stub,
        }
        _log(verbose, task_id, "PreCorrectness", valid_input_case.label, verdict, bool(ok))
    elif MISSING_REQUIRES_POLICY == "requires_true":
        result.pre_correct_detail = {
            "label": valid_input_case.label, "verdict": "assumed",
            "pass": True, "stub": "",
        }

    # PreCompleteness
    if run_pre:
        for case in invalid_cases:
            stub = builder.pre_completeness_stub(
                parsed, spec, case, original_source=java_source
            )
            verdict, out = runner.verify(stub, cname, f"pre_complete_{case.label}")
            verdict = classify_pre_verdict(verdict, out)      # F5
            rejected = None if verdict == VerifyResult.ERROR else (verdict == VerifyResult.FAIL)
            result.pre_complete_details.append(
                {
                    "label": case.label,
                    "verdict": verdict.value,
                    "rejected": rejected,
                    "stub": stub,
                }
            )
            _log(verbose, task_id, "PreCompleteness", case.label, verdict, bool(rejected))
    elif MISSING_REQUIRES_POLICY == "requires_true":
        for case in invalid_cases:
            result.pre_complete_details.append(
                {"label": case.label, "verdict": "assumed",
                 "rejected": False, "stub": ""}
            )

    return result


def _evaluate_one_pair_dual(
    pair_idx: int,
    pair: TestPair,
    valid_input_case: InputCase,
    invalid_cases: list[InputCase],
    parsed: dict,
    spec: MethodSpec,
    out_type: JType,
    builder: StubBuilder,
    mutator: OutputMutator,
    runner: OpenJMLRunnerPersistent,
    cname: str,
    verbose: bool,
    task_id: str,
    mutated_arg: tuple | None = None,
) -> PairResult:
    """
    D8: every check is decided by proving the spec accepts (accept stub) or rejects
    (refute stub) the concrete input/output. Detail values: True/False when proved,
    None when unknown or error (then excluded from the denominator and counted).
    """
    result = PairResult(pair_idx=pair_idx)
    guard = POST_STUB_MODE != "strip"
    vacuity_guard = helpers_have_contracts(spec)

    def failed(e):
        # a check that raised (stub construction / rendering): an ERROR verdict with the exception,
        # excluded from the denominator and counted, never a silently dropped check (or pair)
        print(f"CHECK ERROR [{task_id}] {pair.label}: {type(e).__name__}: {e}", file=sys.stderr)
        return "error", {"exception": f"{type(e).__name__}: {e}"}, ""

    def post_check(output, lbl):
        try:
            stubs = builder.dual_post_stubs(parsed, spec, pair.inputs, output,
                                            mutated_arg=mutated_arg, guard_single_case=guard)
            if stubs is None:
                return None, {}, ""
            decision, info = runner.decide(stubs[0], stubs[1], cname, lbl, wait_both=vacuity_guard)
            return decision, info, stubs[0]
        except Exception as e:
            return failed(e)

    if spec.postcondition is not None:         # always true here: D13 returns earlier without ensures
        # PostCorrectness: the spec must accept the true output
        d, info, stub = post_check(pair.output, f"post_correct_{pair.label}")
        if d is not None:
            result.post_correct_detail = {
                "label": pair.label, "verdict": d, "decision_info": info,
                "pass": True if d == "accept" else False if d == "reject" else None, "stub": stub}
            _log(verbose, task_id, "PostCorrectness", pair.label, VerifyResult.OK if d == "accept"
                 else VerifyResult.FAIL, d == "accept")
        # PostCompleteness: the spec must reject every mutated output
        try:
            mutants = (mutator.mutate_same_length(pair.output, out_type.base) if mutated_arg is not None
                       else mutator.mutate(out_type, pair.output))
        except Exception as e:
            d, info, stub = failed(e)
            result.post_complete_details.append({"label": pair.label, "mutant": "", "verdict": d,
                                                 "decision_info": info, "killed": None, "stub": stub})
            mutants = []
        for mut in mutants:
            d, info, stub = post_check(mut, f"post_complete_{pair.label}_{str(mut)[:20]}")
            if d is None:
                continue
            result.post_complete_details.append({
                "label": pair.label, "mutant": str(mut), "verdict": d, "decision_info": info,
                "killed": True if d == "reject" else False if d == "accept" else None, "stub": stub})

    # Preconditions: prove psi (admitted) or !psi (rejected) on the pinned inputs
    policy = MISSING_REQUIRES_POLICY
    if not spec.has_requires and policy == "zero":
        return result
    try:
        psi = builder.effective_pre(parsed, spec, implicit_nonnull=(policy == "jml"))
        psi_error = None
    except Exception as e:
        psi, psi_error = None, e

    def pre_check(case, lbl):
        if psi_error is not None:
            return failed(psi_error)
        if psi is None:                      # precondition is `true`: admitted, no proof needed
            return "accept", {"trivial": True}, ""
        try:
            a, r = builder.dual_pre_stubs(parsed, spec, case.inputs, psi)
            d, info = runner.decide(a, r, cname, lbl, wait_both=vacuity_guard)
            return d, info, a
        except Exception as e:
            return failed(e)

    d, info, stub = pre_check(valid_input_case, f"pre_correct_{valid_input_case.label}")
    result.pre_correct_detail = {
        "label": valid_input_case.label, "verdict": d, "decision_info": info,
        "pass": True if d == "accept" else False if d == "reject" else None, "stub": stub}
    for case in invalid_cases:
        d, info, stub = pre_check(case, f"pre_complete_{case.label}")
        result.pre_complete_details.append({
            "label": case.label, "verdict": d, "decision_info": info,
            "rejected": True if d == "reject" else False if d == "accept" else None, "stub": stub})
    return result


_CONTRACT_WORDS = re.compile(r"\b(requires|ensures|pre|post|axiom|invariant|represents|signals|constraint|"
                             r"initially)(_redundantly)?\b")


def helpers_have_contracts(spec: MethodSpec) -> bool:
    """True if the helper members copied into the stubs carry JML contracts of their own. Contradictory
    helper contracts make both the accept and the refute stub provable, so decisions then wait for both
    sides (decide(wait_both=True)) and report such a check as an error."""
    src = spec.helpers or ""
    for s_, e_, kind in java_comments(src):
        body = src[s_ + 2:e_]
        if body.startswith("@") and _CONTRACT_WORDS.search(body):
            return True
    return False


def _should_run_pre(spec: MethodSpec) -> bool:
    """F8: decide whether to run OpenJML for the precondition metrics."""
    if spec.has_requires:
        return True
    return MISSING_REQUIRES_POLICY == "jml"


_COLLECTION_RE = re.compile(r"\b(List|ArrayList|LinkedList|Set|HashSet|TreeSet|Map|HashMap|TreeMap)\b")


def _has_collection(jtype: JType) -> bool:
    return jtype.is_collection or bool(_COLLECTION_RE.search(jtype.java_decl()))


def _aggregate(details: list[dict], key: str, name: str) -> HarnessResult:
    """F3: ERROR checks (value None) are excluded from the denominator and counted."""
    hr = HarnessResult(name, 0, 0)
    for d in details:
        v = d.get(key)
        if v is None and d.get("verdict") == "unknown":
            hr.unknowns += 1
        elif v is None:
            hr.errors += 1
        else:
            hr.total += 1
            if v:
                hr.passed += 1
    hr.details = details
    return hr


def _empty_result(task_id: str, reason: str, integrity: dict | None = None,
                  extra: dict | None = None) -> dict:
    """Result dict for an excluded task: all four metrics present, score 0, excluded=True."""
    out: dict[str, Any] = {"task_id": task_id, "excluded": True, "exclude_reason": reason}
    for k in ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness"):
        out[k] = {"score": 0.0, "passed": 0, "total": 0, "errors": 0, "evaluable": False, "details": []}
    out["integrity"] = integrity or {}
    if extra:
        out.update(extra)
    return out



# Main evaluation — one thread per test pair

def evaluate_problem_detailed(
    task: Task,
    llm_code: str,
    openjml_path: str = "openjml",
    output_dir: str = ".",
    verbose: bool = False,
    max_pairs: int = 0,
    exclude_collection_tasks: bool = True,
) -> dict:
    """
    Evaluate one task. Each test pair gets its own thread to compute
    all 4 metrics in parallel.

    Integrity failure (F10: class name, signature, or method body changed): returned
    with all scores 0 and integrity_failed=True, NOT excluded (counts as a failure).
    No postcondition (D13: no `ensures` clause on the method): all scores 0, no_postcondition=True,
    NOT excluded (counts as a failure); nothing is verified.
    Unparseable LLM code (target method missing, or not valid Java) is an integrity failure too.

    Exclusions (task is returned with excluded=True and all scores 0):
      - F7 : method returns void (no \result to constrain)
      - F6 : collection-typed parameter or return, when exclude_collection_tasks
    """
    solution_src = task.code
    test_src = task.test_code
    io_pairs = [{"input": tc.input, "output": tc.output} for tc in task.test_inputs]
    io_pairs_gen = [
        {"input": tc.input, "output": tc.output} for tc in task.generated_test_cases
    ]

    if max_pairs > 0:
        io_pairs = io_pairs[:max_pairs]
        remaining = max_pairs - len(io_pairs)
        if remaining > 0:
            io_pairs.extend(io_pairs_gen[:remaining])

    # parse method signature from benchmark Solution.java 
    parser = JavaMethodParser()
    bench_parsed = parser.parse(solution_src)
    bench_params = bench_parsed["params"]
    return_type = bench_parsed["return_type"]

    # extract JML spec from LLM code (F9: single call)
    # target = the benchmark's method (normally `solve`), even if the LLM added
    # helper methods before it
    spec = extract_jml_spec(llm_code, bench_parsed["method_name"])

    # ---- parse LLM method signature ----
    try:
        llm_parsed = parser.parse(llm_code, bench_parsed["method_name"])
        llm_params = llm_parsed["params"]
    except (ValueError, Exception) as e:
        # F10: the target method is missing (e.g. renamed) or the submission is not valid Java (e.g. a bare
        # `requires ...;` outside a comment, which OpenJML accepts): the code was not kept, so this is an
        # integrity failure, scored 0 and kept in the denominators (like a changed body)
        print(f"INTEGRITY FAILED [{task.task_id}]: llm code unparseable: {e}", file=sys.stderr)
        res = _empty_result(task.task_id, f"integrity: llm code unparseable: {e}",
                            {"class_renamed": None, "signature_changed": None, "body_modified": None,
                             "unparseable": True, "reason": f"llm code unparseable: {e}"})
        res["excluded"] = False
        res["integrity_failed"] = True
        return res

    # ---- F10: integrity (class, signature, body unchanged) ----
    integrity = check_integrity(solution_src, llm_code, bench_parsed, llm_parsed)
    if integrity["class_renamed"] or integrity["signature_changed"] or integrity["body_modified"]:
        print(f"INTEGRITY FAILED [{task.task_id}]: {integrity['reason']}", file=sys.stderr)
        # A submission that changed the code is a failed synthesis, not an unscorable
        # one: keep it in the denominators with all scores 0 (excluded=False).
        res = _empty_result(task.task_id, "integrity: " + integrity["reason"], integrity)
        res["excluded"] = False
        res["integrity_failed"] = True
        return res

    # ---- void methods: the modified array argument is the output ----
    out_type, mutated_bench = return_type, None
    if return_type.base == "void":
        arrays = [p for p in bench_params if p["jtype"].is_array and p["jtype"].array_dims == 1]
        if not arrays or DECISION_MODE != "dual":
            return _empty_result(task.task_id, "unsupported_return: void", integrity,
                                 {"unsupported_return": True})
        out_type, mutated_bench = arrays[0]["jtype"], arrays[0]["name"]

    # stubs need the imports of both the benchmark and the submission
    spec.imports = "\n".join(dict.fromkeys(
        [l.strip() for l in (solution_src + "\n" + llm_code).splitlines()
         if re.match(r"^\s*import\s+(static\s+)?[\w.]+(\.\*)?\s*;", l)]))

    # ---- F6: collection-typed parameters / return ----
    has_coll_param = any(_has_collection(p["jtype"]) for p in bench_params)
    has_coll_ret   = _has_collection(return_type)
    if exclude_collection_tasks and (has_coll_param or has_coll_ret):
        why = ("collection parameter" if has_coll_param else "collection return")
        return _empty_result(task.task_id, f"unsupported_type: {why}", integrity,
                             {"has_collection_param": has_coll_param,
                              "has_collection_return": has_coll_ret})

    # ---- D13: no postcondition (no `ensures` on the method) ----
    # A specification without a postcondition says nothing about the result: all four metrics are 0,
    # nothing is verified, and the task stays in the denominators (excluded=False), like an integrity
    # failure. A missing `requires` alone is fine (precondition true).
    if spec.postcondition is None:
        print(f"NOTE [{task.task_id}]: no JML ensures clause: all metrics 0", file=sys.stderr)
        res = _empty_result(task.task_id, "no_postcondition", integrity)
        res["excluded"] = False
        res["no_postcondition"] = True
        for k in ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness"):
            res[k]["evaluable"] = True
        return res

    # detect input format from Test.java
    read_ops = detect_input_format(test_src, bench_params)
    out_layout = detect_output_layout(test_src)          # how Test.java prints the result

    # parse io_pairs into TestPair / InputCase objects
    test_pairs: list[TestPair] = []
    valid_inputs: list[dict] = []

    for idx, pair in enumerate(io_pairs):
        try:
            inputs_bench = parse_input(pair["input"], read_ops)
            output = parse_output(pair["output"], out_type, out_layout)
            if mutated_bench is not None:
                # the in-place output must have the argument's length (an empty array stays empty)
                if not isinstance(output, list) or len(output) != len(inputs_bench.get(mutated_bench) or []):
                    continue
        except Exception as e:
            if verbose:
                print(
                    f"WARNING [{task.task_id}]: skipping case {idx}: {e}",
                    file=sys.stderr,
                )
            continue

        inputs_llm: dict[str, Any] = {}
        for bp, lp in zip(bench_params, llm_params):
            inputs_llm[lp["name"]] = inputs_bench[bp["name"]]

        test_pairs.append(TestPair(inputs_llm, output, f"case_{idx}"))
        valid_inputs.append(inputs_llm)

    if not test_pairs:
        print(f"ERROR [{task.task_id}]: no test pairs could be parsed", file=sys.stderr)
        return {}

    # build InputCases for Pre metrics
    valid_input_cases = [InputCase(tp.inputs, True, tp.label) for tp in test_pairs]
    if task.invalid_inputs is not None:
        # D4: validated T- (the reference throws or does not return within 300 s), all of them,
        # renamed to the submission's parameter names
        invalid_input_cases = []
        for k, inv in enumerate(task.invalid_inputs):
            vals = decode_tagged(inv["inputs"])
            invalid_input_cases.append(InputCase(
                {lp["name"]: vals[bp["name"]] for bp, lp in zip(bench_params, llm_params)}, False, f"inv_{k}"))
    else:
        invalid_input_cases = generate_invalid_inputs(llm_params, valid_inputs)

    # setup
    builder = StubBuilder()
    mutator = OutputMutator(k=5)
    parsed = llm_parsed
    cname = parsed["class_name"]
    rtype = parsed["return_type"]

    safe_task_id = task.task_id.replace("/", "_").replace("\\", "_")
    stubs_dir = os.path.join(output_dir, "stubs", safe_task_id)
    runner = OpenJMLRunnerPersistent(openjml_path, stubs_dir)

    # distribute invalid cases round-robin across test pairs (scheduling only: PreComp always uses all of them)
    invalid_per_pair: list[list[InputCase]] = [[] for _ in test_pairs]
    for i, ic in enumerate(invalid_input_cases):
        invalid_per_pair[i % len(test_pairs)].append(ic)

    # launch one thread per test pair
    n_threads = len(test_pairs)
    pair_results: list[PairResult] = [None] * n_threads  # type: ignore
    pair_errors: list[str] = []          # pairs whose evaluation raised (should not happen: checks catch)

    mutated_arg = None
    if mutated_bench is not None:
        llm_name = dict(zip([p["name"] for p in bench_params],
                            [p["name"] for p in llm_params]))[mutated_bench]
        mutated_arg = (llm_name, out_type)

    if DECISION_MODE == "dual":
        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = {pool.submit(_evaluate_one_pair_dual, idx, pair, valid_input_cases[idx],
                                invalid_per_pair[idx], parsed, spec, out_type, builder, mutator,
                                runner, cname, verbose, task.task_id, mutated_arg): idx
                    for idx, pair in enumerate(test_pairs)}
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    pair_results[idx] = fut.result()
                except Exception as e:
                    pair_errors.append(f"pair {idx}: {type(e).__name__}: {e}")
                    print(f"ERROR [{task.task_id}] pair {idx}: {e}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        future_to_idx = {} if DECISION_MODE == "dual" else {
            pool.submit(
                _evaluate_one_pair,
                idx,
                pair,
                valid_input_cases[idx],
                invalid_per_pair[idx],
                parsed,
                spec,
                rtype,
                builder,
                mutator,
                runner,
                cname,
                verbose,
                task.task_id,
                llm_code,
                False,
            ): idx
            for idx, pair in enumerate(test_pairs)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                pair_results[idx] = future.result()
            except Exception as e:
                print(f"ERROR [{task.task_id}] pair {idx}: {e}", file=sys.stderr)

    # aggregate PairResults into HarnessResults
    pc_details: list[dict] = []
    pcl_details: list[dict] = []
    prc_details: list[dict] = []
    prl_details: list[dict] = []

    for pr in pair_results:
        if pr is None:
            continue
        if pr.post_correct_detail:
            pc_details.append(pr.post_correct_detail)
        pcl_details.extend(pr.post_complete_details)
        if pr.pre_correct_detail:
            prc_details.append(pr.pre_correct_detail)
        prl_details.extend(pr.pre_complete_details)

    results: dict[str, HarnessResult] = {
        "post_correctness":  _aggregate(pc_details,  "pass",     "PostCorrectness"),
        "post_completeness": _aggregate(pcl_details, "killed",   "PostCompleteness"),
        "pre_correctness":   _aggregate(prc_details, "pass",     "PreCorrectness"),
        "pre_completeness":  _aggregate(prl_details, "rejected", "PreCompleteness"),
    }

    # summary
    sep = "=" * 60
    print(f"\n{sep}")
    print(
        f"  Spec-Harness  |  task: {task.task_id}"
        f"  |  method: {parsed['method_name']}"
    )
    print(sep)
    for r in results.values():
        print(f"  {r}")
    print(sep)

    # build output dict 
    output: dict[str, Any] = {
        "task_id": task.task_id,
        "excluded": False,
        "exclude_reason": "",
        "integrity": integrity,
        "unsupported_return": False,
        "has_collection_param": has_coll_param,
        "has_collection_return": has_coll_ret,
        "pairs_used": len(test_pairs),
        "invalid_inputs_used": len(invalid_input_cases),
        "invalid_inputs_source": "validated" if task.invalid_inputs is not None else "heuristic",
        "mutants_total": len(pcl_details),
        "policy": {
            "post_stub_mode": POST_STUB_MODE,
            "missing_requires_policy": MISSING_REQUIRES_POLICY,
            "decision_mode": DECISION_MODE,
        },
        "mutated_arg": mutated_bench,
    }
    for k, v in results.items():
        output[k] = {
            "score": v.score,
            "passed": v.passed,
            "total": v.total,
            "errors": v.errors,          # F3
            "unknowns": v.unknowns,      # D8
            # D3/D4: no decided check (all unknown/error, or an empty T-) -> the metric is not
            # evaluable for this task: exclude it from Harness@tau denominators, do not count it as 0
            "evaluable": v.total > 0,
            "details": v.details,
        }
    output["pair_errors"] = pair_errors
    output["pairs_unparsed"] = [i for i in range(len(io_pairs)) if f"case_{i}" not in
                                {tp.label for tp in test_pairs}]
    output["check_exceptions"] = sum(1 for det in (pc_details, pcl_details, prc_details, prl_details)
                                     for d in det if (d.get("decision_info") or {}).get("exception"))
    output["not_evaluable"] = [k for k, v in results.items() if v.total == 0]
    output["post_all_unknown"] = (results["post_correctness"].total == 0
                                  and results["post_correctness"].unknowns > 0)
    return output


def _log(
    verbose: bool,
    task_id: str,
    metric: str,
    label: str,
    verdict: VerifyResult,
    desired: bool,
    extra: str = "",
) -> None:
    if not verbose:
        return
    status = "✓" if desired else "✗"
    ex = f"  [{extra}]" if extra else ""
    print(f"  [{task_id}] [{metric:20s}] {label}{ex}" f"  →  {verdict.value}  {status}")



# Outer-level batch processing with threading

def process_one(
    task: Task,
    resp: LLMResponse,
    openjml_path: str,
    output_dir: str,
    verbose: bool,
    max_pairs: int,
    exclude_collection_tasks: bool = True,
) -> dict | None:
    """Evaluate a single (task, response) pair. Returns result dict or None."""
    try:
        result = evaluate_problem_detailed(
            task=task,
            llm_code=resp.final_code,
            openjml_path=openjml_path,
            output_dir=output_dir,
            verbose=verbose,
            max_pairs=max_pairs,
            exclude_collection_tasks=exclude_collection_tasks,
        )
        if result:
            safe_id = task.task_id.replace("/", "_").replace("\\", "_")
            out_path = os.path.join(output_dir, f"{safe_id}.json")
            with open(out_path, "w") as f:
                json.dump(result, f, indent=2)
        return result
    except Exception as e:
        print(f"ERROR [{resp.task_id}]: {e}", file=sys.stderr)
        return None


def run_batch(
    benchmark_path: str,
    llm_response_path: str,
    openjml_path: str,
    output_dir: str,
    threads: int = 1,
    verbose: bool = False,
    max_pairs: int = 0,
    exclude_collection_tasks: bool = True,
) -> list[dict]:
    """
    Load benchmark + verified LLM responses, evaluate all.

    Parameters
    ----------
    threads    : outer parallelism — number of tasks evaluated concurrently
    max_pairs  : number of test pairs to use per task (0 = all);
                 also the number of inner threads per task
    """
    result_base = os.path.splitext(os.path.basename(llm_response_path))[0]
    result_file = path = os.path.join(output_dir, f"{result_base}_spec_harness.json")
    output_dir = os.path.join(output_dir, f"{result_base}")
    os.makedirs(output_dir, exist_ok=True)

    task_lookup = load_benchmark(benchmark_path)
    responses = load_verified_responses(llm_response_path)

    print(
        f"Loaded {len(task_lookup)} benchmark tasks, "
        f"{len(responses)} verified responses"
    )

    # Match responses to tasks
    work: list[tuple[Task, LLMResponse]] = []
    for resp in responses:
        task = task_lookup.get(resp.task_id)
        if task is None:
            print(
                f"WARNING: task_id '{resp.task_id}' not in benchmark, " f"skipping",
                file=sys.stderr,
            )
            continue
        work.append((task, resp))

    print(
        f"Evaluating {len(work)} tasks  "
        f"(outer threads={threads}, "
        f"inner threads per task=max_pairs={max_pairs or 'all'})"
    )

    all_results: list[dict] = []

    if threads <= 1:
        for task, resp in work:
            r = process_one(task, resp, openjml_path, output_dir, verbose,
                            max_pairs, exclude_collection_tasks)
            if r:
                all_results.append(r)
    else:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            future_to_tid = {
                pool.submit(
                    process_one,
                    task,
                    resp,
                    openjml_path,
                    output_dir,
                    verbose,
                    max_pairs,
                    exclude_collection_tasks,
                ): resp.task_id
                for task, resp in work
            }
            for future in as_completed(future_to_tid):
                tid = future_to_tid[future]
                try:
                    r = future.result()
                    if r:
                        all_results.append(r)
                except Exception as e:
                    print(f"ERROR [{tid}]: {e}", file=sys.stderr)

    # write summary 
    excluded = [r for r in all_results if r.get("excluded")]
    summary = {
        "total_evaluated": len(all_results),
        "total_scored": len(all_results) - len(excluded),
        "total_excluded": len(excluded),
        "total_integrity_failed": sum(1 for r in all_results if r.get("integrity_failed")),
        "total_no_postcondition": sum(1 for r in all_results if r.get("no_postcondition")),
        "excluded_by_reason": {},
        "total_verified_responses": len(responses),
        "policy": {
            "post_stub_mode": POST_STUB_MODE,
            "missing_requires_policy": MISSING_REQUIRES_POLICY,
            "exclude_collection_tasks": exclude_collection_tasks,
        },
        "error_checks": {"post_correctness": 0, "post_completeness": 0,
                         "pre_correctness": 0, "pre_completeness": 0},
        "tasks": [],
    }
    for r in excluded:
        key = r.get("exclude_reason", "").split(":")[0] or "unknown"
        summary["excluded_by_reason"][key] = summary["excluded_by_reason"].get(key, 0) + 1
    for r in all_results:
        for k in summary["error_checks"]:
            summary["error_checks"][k] += r[k].get("errors", 0)
        summary["tasks"].append(
            {
                "task_id": r["task_id"],
                "excluded": r.get("excluded", False),
                "exclude_reason": r.get("exclude_reason", ""),
                "post_correctness": r["post_correctness"]["score"],
                "post_completeness": r["post_completeness"]["score"],
                "pre_correctness": r["pre_correctness"]["score"],
                "pre_completeness": r["pre_completeness"]["score"],
                "errors": {k: r[k].get("errors", 0) for k in summary["error_checks"]},
                "evaluable": {k: r[k].get("evaluable", r[k].get("total", 0) > 0) for k in summary["error_checks"]},
                "invalid_inputs_source": r.get("invalid_inputs_source", ""),
                "pairs_used": r.get("pairs_used", 0),
                "mutants_total": r.get("mutants_total", 0),
            }
        )

    with open(result_file, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"  Batch complete: {len(all_results)}/{len(work)} tasks evaluated, "
          f"{len(excluded)} excluded {summary['excluded_by_reason']}")
    print(f"  ERROR checks: {summary['error_checks']}")
    print(f"  Summary -> {result_file}")
    print(f"{'=' * 60}")

    return all_results



# CLI


# ============================================================
# Vendored API adapter (generated)
# ============================================================

import contextlib as _contextlib
import sys as _sys


def evaluate_problem(task, llm_code, openjml_path="openjml", output_dir="vact_outputs",
                     verbose=False, max_pairs=10, run_id=""):
    """Scores for the agents' tools: a float per metric, or None when the metric is not evaluable for
    this task (no decided check), explained in `note`. Nothing is written to stdout.
    Collection-typed tasks are scored (D3), exactly as in final scoring."""
    out_dir = os.path.join(output_dir, run_id) if run_id else output_dir
    with _contextlib.redirect_stdout(_sys.stderr):
        res = evaluate_problem_detailed(task, llm_code, openjml_path=openjml_path, output_dir=out_dir,
                                        verbose=verbose, max_pairs=max_pairs, exclude_collection_tasks=False)
    if not res:
        return {}
    out = {"task_id": res.get("task_id", task.task_id)}
    metrics = ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness")
    notes = []
    if res.get("integrity_failed"):
        for k in metrics:
            out[k] = 0.0
        notes.append("The method body or signature differs from the original program; "
                     "restore the original code and change only the JML annotations.")
    elif res.get("no_postcondition"):
        for k in metrics:
            out[k] = 0.0
        notes.append("No JML postcondition: the method has no `ensures` clause, so the specification says "
                     "nothing about the result. Add `ensures` clauses that describe \result.")
    elif res.get("excluded"):
        for k in metrics:
            out[k] = None
        notes.append("Task could not be scored: " + str(res.get("exclude_reason", "")))
    else:
        for k in metrics:
            r = res.get(k, {}) or {}
            evaluable = r.get("evaluable", (r.get("total") or 0) > 0)
            out[k] = float(r.get("score", 0.0)) if evaluable else None
            if evaluable:
                continue
            if k == "pre_completeness" and not res.get("invalid_inputs_used"):
                notes.append("pre_completeness: not applicable (no input makes the method fail)")
            elif r.get("unknowns"):
                notes.append(f"{k}: not evaluable (the verifier could not decide these checks)")
            elif r.get("errors"):
                notes.append(f"{k}: not evaluable (the checks could not be run; see the verifier output)")
            else:
                notes.append(f"{k}: not evaluable (no checks)")
    out["integrity_failed"] = bool(res.get("integrity_failed"))
    out["note"] = "; ".join(notes)
    return out
