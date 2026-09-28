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

from __future__ import annotations

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
#                       multi-case specs are still guarded (earlier phase-0 behaviour)
#   "raw"             - the spec block as written (the submitted harness: `requires`
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
#                        rejection (the submitted harness)
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

    def _assume_array(self, name: str, jtype: JType,
                      value: list) -> list[str]:
        """Element-wise assumes for 1-D / 2-D arrays (a null row of a 2-D array is pinned as null)."""
        I = "        "
        lines = [f"{I}//@ assume {name} != null;",
                 f"{I}//@ assume {name}.length == {len(value)};"]

        if jtype.array_dims == 1:
            for i, v in enumerate(value):
                lines.append(f"{I}//@ assume {self._scalar_eq(f'{name}[{i}]', jtype.base, v)};")
        elif jtype.array_dims >= 2:
            elem = jtype.element_type()
            for i, row in enumerate(value):
                if row is None:
                    lines.append(f"{I}//@ assume {name}[{i}] == null;")
                    continue
                lines.append(f"{I}//@ assume {name}[{i}] != null;")
                lines.append(f"{I}//@ assume {name}[{i}].length == {len(row)};")
                if elem.array_dims == 0:
                    for j, v in enumerate(row):
                        lines.append(f"{I}//@ assume {self._scalar_eq(f'{name}[{i}][{j}]', jtype.base, v)};")
                # deeper arrays — skip per-element (would need recursion)
        return lines

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

if __name__ == "__main__":

    # ------------------------------------------------------------------
    # Example 1: twoSum — primitive array I/O
    # ------------------------------------------------------------------
    java_src_1 = """
    public class TwoSum {
        public static int[] twoSum(int[] nums, int target) { return null; }
    }
    """
    spec_1 = MethodSpec(
        jml_block = ("//@ requires nums != null && nums.length >= 2;\n"
                     "//@ ensures \\result != null && \\result.length == 2 && "
                     "nums[\\result[0]] + nums[\\result[1]] == target;"),
    )
    test_pairs_1 = [
        TestPair({"nums": [2,7,11,15], "target": 9},  [0,1],  "basic"),
        TestPair({"nums": [3,2,4],     "target": 6},  [1,2],  "mid"),
        TestPair({"nums": [3,3],        "target": 6},  [0,1],  "dup"),
    ]
    input_cases_1 = [
        InputCase({"nums": [2,7,11,15], "target": 9},  True,  "valid_basic"),
        InputCase({"nums": [3,2,4],     "target": 6},  True,  "valid_mid"),
        InputCase({"nums": None,          "target": 9},  False, "null_array"),
        InputCase({"nums": [1],            "target": 1},  False, "single_elem"),
        InputCase({"nums": [],             "target": 0},  False, "empty_array"),
    ]

    h1 = SpecHarness(java_src_1, spec_1, openjml_path="openjml", verbose=True)
    r1 = h1.evaluate(test_pairs_1, input_cases_1)
    h1.save_results(r1, "twosum_results.json")

    # ------------------------------------------------------------------
    # Example 2: matrix multiply — 2D array I/O
    # ------------------------------------------------------------------
    java_src_2 = """
    public class MatMul {
        public static int[][] multiply(int[][] a, int[][] b) { return null; }
    }
    """
    spec_2 = MethodSpec(
        jml_block = ("//@ requires a != null && b != null && a[0].length == b.length;\n"
                     "//@ ensures \\result != null && \\result.length == a.length;"),
    )
    test_pairs_2 = [
        TestPair(
            {"a": [[1,2],[3,4]], "b": [[5,6],[7,8]]},
            [[19,22],[43,50]], "2x2"
        ),
    ]
    input_cases_2 = [
        InputCase({"a": [[1,2],[3,4]], "b": [[5,6],[7,8]]}, True,  "valid_2x2"),
        InputCase({"a": None,           "b": [[1,2],[3,4]]}, False, "null_a"),
        InputCase({"a": [[1,2,3]],      "b": [[1,2],[3,4]]}, False, "dim_mismatch"),
    ]

    h2 = SpecHarness(java_src_2, spec_2, openjml_path="openjml", verbose=True)
    h2.evaluate(test_pairs_2, input_cases_2)

    # ------------------------------------------------------------------
    # Example 3: Map<String, List<Integer>> — nested collection I/O
    # ------------------------------------------------------------------
    java_src_3 = """
    import java.util.*;
    public class GroupBy {
        public static Map<String, List<Integer>> groupByLength(List<String> words) {
            return null;
        }
    }
    """
    spec_3 = MethodSpec(
        jml_block = ("//@ requires words != null;\n"
                     "//@ ensures \\result != null;"),
    )
    test_pairs_3 = [
        TestPair(
            {"words": ["hi","hey","yo"]},
            {"2": [0,2], "3": [1]},
            "basic_map"
        ),
    ]
    input_cases_3 = [
        InputCase({"words": ["hi","ho"]}, True,  "valid"),
        InputCase({"words": None},         False, "null_list"),
    ]

    h3 = SpecHarness(java_src_3, spec_3, openjml_path="openjml", verbose=True)
    h3.evaluate(test_pairs_3, input_cases_3)