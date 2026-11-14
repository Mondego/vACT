"""
Unit tests for Spec-Harness (no OpenJML needed).

Run from the repository root:
    python -m pytest tests/test_harness.py -q
or without pytest:
    python tests/test_harness.py
"""
import json
import os
import signal as signal_module
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the repository root

from spec_harness.harness import (
    MethodSpec, StubBuilder, JavaMethodParser, TestPair, InputCase, OutputMutator,
    JType, VerifyResult, classify_pre_verdict, parse_openjml_output, check_integrity,
)
import spec_harness.eval_llm_response as ev

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lines(block):
    return [l.strip() for l in block.splitlines() if l.strip()]


# ---- clause parsing: behaviour headers ------------------------------------

NB = """    /*@ public normal_behavior
      @   requires n >= 1;
      @   ensures \\result == (n % 2 == 0);
      @   assignable \\nothing;
      @*/"""


def test_normal_behavior_requires_detected():
    s = MethodSpec(NB)
    assert s.has_requires and s.has_ensures
    assert _lines(s.requires_only_block()) == ["//@ requires n >= 1;"]


def test_normal_behavior_post_block_is_guarded():
    post = MethodSpec(NB).post_only_block()
    assert "requires" not in post
    assert "//@ ensures ((n >= 1)) ==> (\\result == (n % 2 == 0));" in _lines(post)
    assert "//@ assignable \\nothing;" in _lines(post)


def test_post_stub_for_normal_behavior_has_no_requires():
    src = "public class Solution {\n" + NB + "\n public static boolean solve(int n) { return n % 2 == 0; }\n}"
    parsed = JavaMethodParser().parse(src)
    stub = StubBuilder().post_completeness_stub(parsed, MethodSpec(NB), TestPair({"n": -5}, False, "c0"), True)
    assert "requires" not in stub and "assume n == -5" in stub


# ---- clause parsing: quantifiers and literals -----------------------------

def test_quantified_requires_kept_whole():
    s = MethodSpec("//@ requires (\\forall int i; 0 <= i && i < a.length; a[i] >= 0);\n"
                   "//@ ensures \\result >= 0;")
    assert _lines(s.requires_only_block()) == [
        "//@ requires (\\forall int i; 0 <= i && i < a.length; a[i] >= 0);"]
    assert _lines(s.post_only_block()) == [
        "//@ ensures (((\\forall int i; 0 <= i && i < a.length; a[i] >= 0))) ==> (\\result >= 0);"]


def test_quantified_ensures_kept_whole():
    s = MethodSpec("//@ requires arr != null && arr.length > 0;\n"
                   "//@ ensures (\\forall int k; 0 <= k && k < arr.length; \\result <= arr[k]);")
    assert _lines(s.post_only_block()) == [
        "//@ ensures ((arr != null && arr.length > 0)) ==> "
        "((\\forall int k; 0 <= k && k < arr.length; \\result <= arr[k]));"]


def test_semicolon_inside_string_literal():
    s = MethodSpec('//@ ensures \\result.equals("a;b");')
    assert _lines(s.post_only_block()) == ['//@ ensures \\result.equals("a;b");']


def test_pure_modifier_in_block_is_ignored():
    s = MethodSpec("/*@ pure @*/\n//@ requires x > 0;\n//@ ensures \\result == x;")
    assert s.has_requires
    assert _lines(s.post_only_block()) == ["//@ ensures ((x > 0)) ==> (\\result == x);"]


# ---- multi-case specs (also) ---------------------------------------------

ALSO = """    /*@ public normal_behavior
      @   requires arr != null && arr.length > 0;
      @   ensures (\\forall int j; 0 <= j && j < arr.length; arr[j] >= \\result);
      @ also
      @ public normal_behavior
      @   requires arr != null && arr.length == 0;
      @   ensures \\result == -1;
      @*/"""


def test_also_cases_parsed():
    assert len(MethodSpec(ALSO).cases()) == 2


def test_also_post_block_guards_each_case():
    post = _lines(MethodSpec(ALSO).post_only_block())
    assert post == [
        "//@ ensures ((arr != null && arr.length > 0)) ==> "
        "((\\forall int j; 0 <= j && j < arr.length; arr[j] >= \\result));",
        "//@ ensures ((arr != null && arr.length == 0)) ==> (\\result == -1);",
    ]


def test_also_requires_is_disjunction():
    assert _lines(MethodSpec(ALSO).requires_only_block()) == [
        "//@ requires ((arr != null && arr.length > 0)) || ((arr != null && arr.length == 0));"]


def test_also_case_without_requires_makes_precondition_true():
    s = MethodSpec("//@ requires x > 0;\n//@ ensures \\result == 1;\n//@ also\n//@ ensures \\result >= 0;")
    assert s.requires_only_block() == ""


def test_single_case_unchanged_behaviour():
    s = MethodSpec("//@ requires a != null;\n//@ requires a.length > 1;\n//@ ensures \\result > 0;")
    assert _lines(s.requires_only_block()) == ["//@ requires a != null;", "//@ requires a.length > 1;"]


# ---- verdicts ---------------------------------------------------------------

def test_nullformal_counts_as_rejection():
    out = ("S.java:12: verify: The prover cannot establish an assertion (NullFormal: S.java:4:) "
           "in method check: a in solve(int[])\n1 verification failure")
    assert classify_pre_verdict(VerifyResult.FAIL, out) == VerifyResult.FAIL


def test_body_warning_is_not_a_rejection():
    out = "S.java:9: verify: The prover cannot establish an assertion (ArithmeticOperationRange)\n1 verification failure"
    assert classify_pre_verdict(VerifyResult.FAIL, out) == VerifyResult.ERROR


def test_compile_error_is_error_not_fail():
    assert parse_openjml_output("S.java:3: error: illegal start of type\n1 error", 1) == VerifyResult.ERROR


# ---- timeout path (signal import) ------------------------------------------

def test_signal_module_imported():
    assert ev.signal is signal_module and hasattr(ev.signal, "SIGKILL")


# ---- integrity (F10) --------------------------------------------------------

BENCH = """public class Solution {
    public static int solve(int x) {
        int y=x+1;
        if (y>0) { return y; }
        return 0;
    }
}"""


def _parsed(src):
    return JavaMethodParser().parse(src)


def test_reformatting_is_not_a_body_change():
    llm = """public class Solution {
    //@ ensures \\result >= 0;
    public static int solve(int x)
    {
        int y = x + 1;   // comment
        if (y > 0) {
            return y;
        }
        return 0;
    }
}"""
    r = check_integrity(BENCH, llm, _parsed(BENCH), _parsed(llm))
    assert not r["body_modified"], r


def test_real_body_change_detected():
    llm = BENCH.replace("return 0;", "return 1;")
    r = check_integrity(BENCH, llm, _parsed(BENCH), _parsed(llm))
    assert r["body_modified"]


def test_integrity_failure_counts_as_failure_not_exclusion():
    tasks = json.load(open(os.path.join(ROOT, "benchmarks", "specgenbench", "sgb.json")))
    t = next(x for x in tasks if x["task_id"] == "SGB_Abs")
    task = ev.Task.from_dict(t)
    modified = t["code"].replace("return", "return 0 +", 1)
    res = ev.evaluate_problem(task, modified, output_dir="/tmp/_spec_harness_test", max_pairs=1)
    assert res.get("integrity_failed") is True and res.get("excluded") is False
    assert res["post_correctness"]["score"] == 0.0


def test_added_braces_are_not_a_body_change():
    bench = "public class Solution { public static int solve(int n) { int s = 0; for (int i = 0; i < n; i++) s += i; return s; } }"
    llm = ("public class Solution { public static int solve(int n) { int s = 0;\n"
           "  //@ maintaining 0 <= i && i <= n;\n"
           "  for (int i = 0; i < n; i++) { s += i; } return s; } }")
    r = check_integrity(bench, llm, _parsed(bench), _parsed(llm))
    assert not r["body_modified"] and r.get("benign_brace_change"), r


def test_regrouping_with_braces_is_a_body_change():
    bench = "public class Solution { public static int solve(int a) { int x = 0; if (a > 0) x = 1; x = x + 2; return x; } }"
    llm = "public class Solution { public static int solve(int a) { int x = 0; if (a > 0) { x = 1; x = x + 2; } return x; } }"
    r = check_integrity(bench, llm, _parsed(bench), _parsed(llm))
    assert r["body_modified"], r


# ---- target-method selection and helpers (bug #5) -------------------------

HELPER_SRC = """public class Solution {
    /*@ ensures \\result == (x * 2);
      @*/
    public static /*@ pure @*/ int twice(int x) {
        return x * 2;
    }

    //@ requires x >= 0;
    //@ ensures \\result == twice(x) + 1;
    public static int solve(int x) {
        return x * 2 + 1;
    }
}"""


def test_parser_selects_solve_not_first_method():
    assert JavaMethodParser().parse(HELPER_SRC)["method_name"] == "solve"


def test_spec_extracted_for_solve_with_helpers():
    from spec_harness.eval_spec import extract_jml_spec
    s = extract_jml_spec(HELPER_SRC, "solve")
    assert "twice(x) + 1" in s.jml_block and "x * 2)" not in s.jml_block
    assert "int twice(int x)" in s.helpers and "int solve" not in s.helpers


def test_post_stub_contains_helper():
    from spec_harness.eval_spec import extract_jml_spec
    s = extract_jml_spec(HELPER_SRC, "solve")
    parsed = JavaMethodParser().parse(HELPER_SRC, "solve")
    stub = StubBuilder().post_correctness_stub(parsed, s, TestPair({"x": 3}, 7, "c0"))
    assert "int twice(int x)" in stub and "public static int solve(int x)" in stub
    assert stub.count("public class") == 1


# ---- D1: postcondition stub modes -------------------------------------------

def test_no_requires_ensures_unguarded():
    assert _lines(MethodSpec("//@ ensures \\result >= 0;").post_only_block()) == ["//@ ensures \\result >= 0;"]


def test_strip_mode_reproduces_old_behaviour():
    post = MethodSpec(NB).post_only_block(guard_single_case=False)
    assert "//@ ensures \\result == (n % 2 == 0);" in _lines(post)


def test_post_stub_modes():
    import spec_harness.harness as H
    src = "public class Solution {\n" + NB + "\n public static boolean solve(int n) { return n % 2 == 0; }\n}"
    parsed = JavaMethodParser().parse(src)
    pair = TestPair({"n": -5}, False, "c0")
    old = H.POST_STUB_MODE
    try:
        H.POST_STUB_MODE = "guard"
        g = StubBuilder().post_correctness_stub(parsed, MethodSpec(NB), pair)
        assert "==> (\\result == (n % 2 == 0))" in g and "requires" not in g
        H.POST_STUB_MODE = "strip"
        st = StubBuilder().post_correctness_stub(parsed, MethodSpec(NB), pair)
        assert "==>" not in st and "requires" not in st
        H.POST_STUB_MODE = "raw"
        r = StubBuilder().post_correctness_stub(parsed, MethodSpec(NB), pair)
        assert "requires n >= 1" in r
    finally:
        H.POST_STUB_MODE = old


# ---- D8: dual accept / refute stubs ------------------------------------------

def test_post_terms_and_pre_expr():
    s = MethodSpec(NB)
    assert s.post_terms() == ["((n >= 1)) ==> (\\result == (n % 2 == 0))"]
    assert s.pre_expr() == "(n >= 1)"
    assert MethodSpec(ALSO).pre_expr() == "((arr != null && arr.length > 0)) || ((arr != null && arr.length == 0))"
    assert MethodSpec("//@ ensures \\result > 0;").pre_expr() is None


def test_dual_post_stubs_accept_and_refute():
    src = "import java.util.*;\npublic class Solution {\n" + NB + "\n public static boolean solve(int n) { return n % 2 == 0; }\n}"
    from spec_harness.eval_spec import extract_jml_spec
    spec = extract_jml_spec(src, "solve")
    acc, ref = StubBuilder().dual_post_stubs(JavaMethodParser().parse(src), spec, {"n": 4}, True)
    for st in (acc, ref):
        assert st.startswith("import java.util.*;") and "private static boolean solve(int n)" in st
        assert "requires" not in st and "assume n == 4;" in st and "return true;" in st
    assert "//@ ensures ((n >= 1)) ==> (\\result == (n % 2 == 0));" in acc
    assert "//@ ensures !((((n >= 1)) ==> (\\result == (n % 2 == 0))));" in ref


def test_dual_post_stubs_void_mutated_arg():
    src = "public class Solution {\n //@ ensures (\\forall int i; 0 <= i && i < a.length - 1; a[i] <= a[i+1]);\n public static void solve(int[] a) { java.util.Arrays.sort(a); }\n}"
    from spec_harness.eval_spec import extract_jml_spec
    p = JavaMethodParser().parse(src)
    acc, _ = StubBuilder().dual_post_stubs(p, extract_jml_spec(src, "solve"), {"a": [3, 1]}, [1, 3],
                                           mutated_arg=("a", p["params"][0]["jtype"]))
    assert "assume a[0] == 3;" in acc and "a[0] = 1;" in acc and "a[1] = 3;" in acc and "return;" in acc


def test_effective_pre_and_pre_stubs():
    src = "public class Solution {\n //@ requires n > 0;\n public static int solve(int[] a, int n) { return n; }\n}"
    from spec_harness.eval_spec import extract_jml_spec
    p, spec, b = JavaMethodParser().parse(src), extract_jml_spec(src, "solve"), StubBuilder()
    assert b.effective_pre(p, spec, implicit_nonnull=True) == "((n > 0)) && a != null"
    assert b.effective_pre(p, spec, implicit_nonnull=False) == "((n > 0))"
    nospec = MethodSpec("//@ ensures \\result > 0;")
    assert b.effective_pre(p, nospec, implicit_nonnull=False) is None
    acc, ref = b.dual_pre_stubs(p, spec, {"a": None, "n": 2}, "(n > 0) && a != null")
    assert "assume a == null;" in acc and "//@ assert (n > 0) && a != null;" in acc
    assert "//@ assert !((n > 0) && a != null);" in ref


def test_helpers_marked_skipesc():
    from spec_harness.harness import mark_helpers_skipesc
    helpers = "    //@ ensures \\result == x * 2;\n    public static /*@ pure @*/ int twice(int x) { return x * 2; }\n    private static int[] cache;"
    out = mark_helpers_skipesc(helpers).splitlines()
    i = [k for k, l in enumerate(out) if "int twice" in l][0]
    assert out[i - 1].strip() == "/*@ skipesc @*/" and "ensures" in out[i - 2]


def test_same_length_mutants():
    ms = OutputMutator().mutate_same_length([1, 2, 3])
    assert ms and all(len(m) == 3 for m in ms) and [1, 2, 3] not in ms


def test_char_array_parsing():
    from spec_harness.eval_spec import detect_input_format, parse_input, parse_output
    p = JavaMethodParser().parse("public class Solution { public static void solve(char[] s) { } }")
    ops = detect_input_format("char[] p0 = cs0.toCharArray();", p["params"])
    assert parse_input("a b", ops)["s"] == ["a", " ", "b"]
    assert parse_output("olleh", p["params"][0]["jtype"]) == list("olleh")


def test_refute_wraps_each_term():
    # `==>` binds more loosely than `&&`: every term must be parenthesised
    spec = MethodSpec("//@ requires x > 0;\n//@ ensures \\result >= 0;\n//@ ensures \\result == x;")
    p = JavaMethodParser().parse("public class Solution { public static int solve(int x) { return x; } }")
    _, ref = StubBuilder().dual_post_stubs(p, spec, {"x": 1}, 1)
    line = [l for l in ref.splitlines() if "ensures" in l][0]
    assert line.strip() == "//@ ensures !((((x > 0)) ==> (\\result >= 0)) && (((x > 0)) ==> (\\result == x)));"


def test_map_return_built_with_puts():
    p = JavaMethodParser().parse("import java.util.*;\npublic class Solution { public static HashMap<String, Integer> solve(String s) { return null; } }")
    acc, _ = StubBuilder().dual_post_stubs(p, MethodSpec("//@ ensures \\result != null;"), {"s": "ab"}, {"a": 1, "b": 1})
    assert 'HashMap<String, Integer> _r = new java.util.HashMap<>();' in acc and '_r.put("a", 1);' in acc and "return _r;" in acc


# ---- mutator (F1) -----------------------------------------------------------

def test_no_equivalent_zero_mutant():
    assert 0 not in OutputMutator().mutate(JType("int"), 0)


def test_no_equivalent_string_mutants():
    for v in ["ABC", "aba", ""]:
        assert v not in OutputMutator().mutate(JType("String"), v)


# ---- literal rendering (A5: T6 invalid inputs, char / NaN / null) ------------

_RENDER_CASES = [
    ("char", "'"), ("char", "\\"), ("char", " "), ("char", "\n"),
    ("String", 'a"b\\c\n\u00e9'), ("double", float("nan")), ("double", float("-inf")), ("double", 0.1),
    ("double", -0.0), ("float", float("inf")), ("long", -2**63), ("int", -2**31),
    ("String[]", ["a", None]), ("Integer[]", [1, None]), ("int[][]", [[1, 2], None]), ("int[]", None),
    ("List<String>", ["x", None]), ("HashMap<String, Integer>", {"k": None, "q\"": 2}), ("char[]", ["a", "'"]),
    ("String[][]", [["a"], None, [None]]), ("short", -3), ("byte", 7),
]


def test_render_escapes_nan_null():
    from spec_harness.harness import JavaLiteralRenderer, _parse_raw_jtype
    r = JavaLiteralRenderer()
    assert r.render(_parse_raw_jtype("char"), "'") == "'\\''"
    assert r.render(_parse_raw_jtype("double"), float("nan")) == "Double.NaN"
    assert r.render(_parse_raw_jtype("String[]"), ["a", None]) == 'new String[]{"a", null}'
    assert r.render(_parse_raw_jtype("int[][]"), [[1], None]) == "new int[][]{new int[]{1}, null}"


def test_rendered_literals_compile():
    """Every rendered literal must compile as an initialiser of its declared type (javac 21)."""
    import os, subprocess, tempfile
    from spec_harness.harness import JavaLiteralRenderer, _parse_raw_jtype
    r = JavaLiteralRenderer()
    lines = [f"        {_parse_raw_jtype(t).java_decl()} v{i} = {r.render(_parse_raw_jtype(t), v)};"
             for i, (t, v) in enumerate(_RENDER_CASES)]
    src = "import java.util.*;\npublic class R {\n    static void f() {\n" + "\n".join(lines) + "\n    }\n}\n"
    d = tempfile.mkdtemp()
    open(os.path.join(d, "R.java"), "w").write(src)
    javac = "/pkg/openjdk/21.0.4/bin/javac"
    res = subprocess.run([javac, "-d", d, os.path.join(d, "R.java")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr + "\n" + src


# ---- A3/A4: char reader, validated T-, evaluable flags --------------------------

def test_char_reader_matches_scanner_next():
    from spec_harness.eval_spec import _parse_scalar
    assert _parse_scalar("char", "ab") == "a"          # Test.java: first char of the first token
    assert _parse_scalar("char", "") == " "            # Test.java: ' ' when there is no token
    assert _parse_scalar("char", "  z ") == "z"


def test_decode_tagged_values():
    from spec_harness.eval_spec import decode_tagged
    import math
    v = decode_tagged({"a": None, "m": {"__map__": [["k", 1], [2, None]]}, "f": {"__float__": "nan"},
                       "l": [{"__float__": "-inf"}, 1]})
    assert v["a"] is None and v["m"] == {"k": 1, 2: None}
    assert math.isnan(v["f"]) and v["l"][0] == float("-inf") and v["l"][1] == 1


def test_task_invalid_inputs_none_vs_empty():
    from spec_harness.eval_llm_response import Task
    base = {"task_id": "T", "code": "", "class_name": "Solution", "test_name": "Test"}
    assert Task.from_dict(base).invalid_inputs is None                       # old benchmarks: heuristic T-
    assert Task.from_dict({**base, "invalid_inputs": []}).invalid_inputs == []   # validated: no invalid input


def test_jvm_slots_cap_and_release():
    import threading, time
    from spec_harness.eval_llm_response import JvmSlots
    slots, peak, lock = JvmSlots(4), [0], threading.Lock()

    def job():
        slots.acquire(2)
        with lock:
            peak[0] = max(peak[0], slots.used)
        time.sleep(0.05)
        slots.release(2)
    ts = [threading.Thread(target=job) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] <= 4 and slots.used == 0


# ---- bare (unparenthesized) quantifiers: `requires \\forall int i; r; b;` is valid JML ---------

_BARE_Q = """public class Solution {
    //@ requires nums != null && indices != null;
    //@ requires \\forall int i; 0 <= i < indices.length; 0 <= indices[i] < nums.length;
    //@ ensures \\result.length == indices.length;
    //@ ensures \\forall int i; 0 <= i < indices.length; \\result[i] == nums[indices[i]];
    public static int[] solve(int[] nums, int[] indices) {
        int[] result = new int[indices.length];
        for (int i = 0; i < indices.length; i++) { result[i] = nums[indices[i]]; }
        return result;
    }
}
"""


def test_bare_quantifier_kept_whole():
    from spec_harness.harness import _jml_cases
    blk = "\n".join(l for l in _BARE_Q.splitlines() if "//@" in l)
    (case,) = _jml_cases(blk)
    assert case == ["requires nums != null && indices != null",
                    "requires \\forall int i; 0 <= i < indices.length; 0 <= indices[i] < nums.length",
                    "ensures \\result.length == indices.length",
                    "ensures \\forall int i; 0 <= i < indices.length; \\result[i] == nums[indices[i]]"], case


def test_bare_quantifier_stub_is_wellformed():
    from spec_harness.eval_spec import extract_jml_spec
    from spec_harness.harness import JavaMethodParser, StubBuilder
    parsed = JavaMethodParser().parse(_BARE_Q, "solve")
    spec = extract_jml_spec(_BARE_Q, "solve")
    acc, ref = StubBuilder().dual_post_stubs(parsed, spec, {"nums": [5, 6], "indices": [1]}, [6])
    for stub in (acc, ref):
        assert "(\\forall int i)" not in stub, stub
        assert "\\forall int i; 0 <= i < indices.length; \\result[i] == nums[indices[i]]" in stub, stub


def test_output_mutants_stay_in_type_range():
    from spec_harness.harness import OutputMutator, _parse_raw_jtype as J
    m = OutputMutator(k=5)
    for t, v, lo, hi in (("int", 2**31 - 1, -2**31, 2**31 - 1), ("int", -2**31, -2**31, 2**31 - 1),
                         ("long", 2**63 - 1, -2**63, 2**63 - 1)):
        muts = m.mutate(J(t), v)
        assert len(muts) == 5 and all(lo <= x <= hi for x in muts), (t, v, muts)
    arr = m.mutate(J("int[]"), [1290, 2**31 - 1])
    assert arr and all(-2**31 <= x <= 2**31 - 1 for a in arr for x in a), arr
    assert m.mutate(J("char"), "\0") == ["\x01", "\x02"]
    assert m.mutate(J("int"), 7) == [8, 6, 9, 5, 0]                 # ordinary values: unchanged
    assert m.mutate(J("int[]"), [1, 2]) == [[1], [1, 2, 3], [2, 3], []]


def test_spec_without_ensures_scores_zero_everywhere():
    """D13: no ensures on the method (no JML at all, or requires only) = all four metrics 0, evaluable,
    not excluded, and no OpenJML run (the openjml path does not exist)."""
    import json, re, tempfile
    from spec_harness.eval_llm_response import Task, evaluate_problem
    t = next(x for x in json.load(open("benchmarks/specgenbench/sgb_v2_eval.json")) if x["task_id"] == "SGB_Abs")
    requires_only = re.sub(r"(\n(\s*)(public|static)[^\n]*\bsolve\s*\()", r"\n\2//@ requires true;\1",
                           t["code"], count=1)
    assert "//@ requires true;" in requires_only
    for code in (t["code"], requires_only):
        res = evaluate_problem(Task.from_dict(t), code, openjml_path="/nonexistent/openjml",
                               output_dir=tempfile.mkdtemp(), max_pairs=0, exclude_collection_tasks=False)
        assert res["no_postcondition"] is True and res["excluded"] is False, res
        for m in ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness"):
            assert res[m]["evaluable"] is True and res[m]["score"] == 0.0, (m, res[m])


def test_missing_or_unparseable_method_is_integrity_failure():
    """F10: `solve` renamed, or a bare JML clause outside a comment (not Java): scored 0, not excluded."""
    import json, tempfile
    from spec_harness.eval_llm_response import Task, evaluate_problem
    t = next(x for x in json.load(open("benchmarks/specgenbench/sgb_v2_eval.json")) if x["task_id"] == "SGB_Abs")
    renamed = t["code"].replace("solve(", "absValue(", 1)
    bare = t["code"].replace("{", "{\n    requires true;", 1)
    for code in (renamed, bare):
        res = evaluate_problem(Task.from_dict(t), code, openjml_path="/nonexistent/openjml",
                               output_dir=tempfile.mkdtemp(), max_pairs=0, exclude_collection_tasks=False)
        assert res["integrity_failed"] is True and res["excluded"] is False, res
        assert all(res[m]["score"] == 0.0 for m in ("post_correctness", "post_completeness",
                                                    "pre_correctness", "pre_completeness"))



# ---------------------------------------------------------------- deep review fixes (2026-09-28)

def test_jml_extraction_handles_comments_and_gaps():
    """Trailing // comments, blank lines, plain comments and Javadoc between/inside JML annotations."""
    from spec_harness.eval_spec import extract_jml_spec
    cls = "public class Solution {{\n{}\n    public static int solve(int r) {{ return 2*r; }}\n}}"
    sp = extract_jml_spec(cls.format("    //@ requires r >= 0; // radius\n    //@ ensures \\result == 2*r;"), "solve")
    assert sp.has_requires and sp.has_ensures and sp.post_terms() and "//" not in " ".join(sp.post_terms()), sp.clauses()
    sp = extract_jml_spec(cls.format("    //@ ensures \\result == 2*r; // twice"), "solve")
    assert sp.clauses() == ["ensures \\result == 2*r"], sp.clauses()
    sp = extract_jml_spec(cls.format("    //@ requires r >= 0;\n\n    // >>>INFILL<<<\n    //@ ensures \\result >= 0;"), "solve")
    assert sp.has_requires and sp.has_ensures, sp.clauses()
    assert "requires" not in sp.helpers                    # the JML region never leaks into the helpers
    sp = extract_jml_spec(cls.format("    /**\n     * @requires r >= 0\n     * @ensures \\result == 2*r\n     */"), "solve")
    assert not sp.has_ensures and not sp.has_requires       # Javadoc tags are not JML
    sp = extract_jml_spec(cls.format("    /*@ requires r >= 0; // why\n      @ ensures \\result == 2*r;\n      @*/"), "solve")
    assert sp.clauses() == ["requires r >= 0", "ensures \\result == 2*r"], sp.clauses()
    sp = extract_jml_spec(cls.format('    //@ ensures \\result != 0 || "a//b".length() == 3;'), "solve")
    assert '"a//b"' in sp.clauses()[0]                     # // inside a string literal is not a comment


def test_class_name_from_syntax_tree_not_comment():
    from spec_harness.harness import JavaMethodParser
    src = "/** This class contains helpers. */\npublic class Solution {\n  public static int solve(int x) { return x; }\n}"
    assert JavaMethodParser().parse(src, "solve")["class_name"] == "Solution"


def test_integrity_covers_other_members_and_agent_rule_rejects_assume():
    from spec_harness.harness import agent_code_violation
    orig = ("public class Solution {\n  static void swap(int[] a, int i, int j) { int t = a[i]; a[i] = a[j]; a[j] = t; }\n"
            "  public static int solve(int x) { if (x < 0) return -x; return x; }\n}")
    spec = orig.replace("  public static int solve", "  //@ ensures \\result >= 0;\n  public static int solve")
    assert agent_code_violation(orig, spec) is None
    assert agent_code_violation(orig, spec.replace("a[i] = a[j];", "a[i] = 0;"))
    assert agent_code_violation(orig, spec.replace("if (x < 0)", "//@ assume false;\n    if (x < 0)"))
    assert agent_code_violation(orig, spec.replace("if (x < 0) return -x;", "if (x < 0) { return -x; }")) is None


def test_output_reader_follows_test_java_layout():
    """Size header lines, explicit separators, exact String results, null String."""
    from spec_harness.harness import JType
    from spec_harness.eval_spec import parse_output
    hdr, plain = {"header": True, "sep": " "}, {"header": False, "sep": " "}
    J = lambda b, d=0, g=None: JType(base=b, array_dims=d, generic_args=g or [])
    assert parse_output("2 3\n1 2 3\n4 5 6", J("int", 2), hdr) == [[1, 2, 3], [4, 5, 6]]
    assert parse_output("2 0\n\n", J("int", 2), hdr) == [[], []]
    assert parse_output("1 2 3\n4 5 6", J("int", 2), plain) == [[1, 2, 3], [4, 5, 6]]
    assert parse_output("a, b\nc", J("String", 2), {"header": False, "sep": ", "}) == [["a", "b"], ["c"]]
    assert parse_output("2 2\n1 2\n3 4", J("List", 0, ["List<Integer>"]), hdr) == [[1, 2], [3, 4]]
    assert parse_output("2\nx yo", J("HashSet", 0, ["String"]), hdr) == ["x", "yo"]
    assert parse_output("  two  spaces ", J("String")) == "  two  spaces "
    assert parse_output("null", J("String")) is None


def test_input_reader_truncates_rows_like_test_java():
    """r c header: Test.java reads exactly c tokens per row; an empty String[] line is [""] in Java."""
    from spec_harness.harness import JType
    from spec_harness.eval_spec import ReadOp, parse_input
    op = ReadOp("m", JType(base="int", array_dims=2), "array_2d_rc")
    assert parse_input("2 2\n1 2 9 9\n3 4\n", [op])["m"] == [[1, 2], [3, 4]]
    op_sgb = ReadOp("m", JType(base="int", array_dims=2), "array_2d_rc", rows_when_no_cols=False)
    op_fb = ReadOp("m", JType(base="int", array_dims=2), "array_2d_rc", rows_when_no_cols=True)
    x = ReadOp("x", JType(base="int"), "scalar")
    assert parse_input("2 0\n7\n", [op_sgb, x]) == {"m": [[], []], "x": 7}
    assert parse_input("2 0\n\n\n7\n", [op_fb, x]) == {"m": [[], []], "x": 7}
    s_op = ReadOp("s", JType(base="String", array_dims=1), "array_direct")
    assert parse_input("\n", [s_op])["s"] == [""]


def test_mutants_nan_and_empty_2d():
    from spec_harness.harness import OutputMutator, JType
    m = OutputMutator(k=5)
    nan_muts = m.mutate(JType(base="double"), float("nan"))
    assert nan_muts and all(x == x for x in nan_muts), nan_muts           # never NaN again
    assert m.mutate(JType(base="int", array_dims=2), []) == [[[]]]
    assert OutputMutator._eq([float("nan")], [float("nan")])


def test_nan_and_null_row_assumptions():
    from spec_harness.harness import StubBuilder, JType
    b = StubBuilder()
    assert b._scalar_eq("x", "double", float("nan")) == "Double.isNaN(x)"
    lines = b._assume_array("m", JType(base="int", array_dims=2), [[1], None])
    assert any("m[1] == null" in l for l in lines)


def test_openjml_ok_needs_exit_zero():
    from spec_harness.harness import parse_openjml_output, VerifyResult
    assert parse_openjml_output("", 0) == VerifyResult.OK
    assert parse_openjml_output("", -9) == VerifyResult.UNKNOWN          # killed: never a proof
    assert parse_openjml_output("1 warning", 0) == VerifyResult.FAIL


def test_check_errors_do_not_drop_pairs():
    """A check that raises is an ERROR verdict (counted, excluded), the rest of the pair still runs."""
    import json, tempfile
    import spec_harness.eval_llm_response as ev
    t = next(x for x in json.load(open("benchmarks/specgenbench/sgb_v2_eval.json")) if x["task_id"] == "SGB_Abs")
    code = t["code"].replace("public static int solve", "//@ ensures \\result >= 0;\n\tpublic static int solve", 1)
    calls = {"n": 0}

    class R:
        def __init__(self, *a, **k): pass
        def decide(self, a, r, cname, lbl, wait_both=False):
            calls["n"] += 1
            if "post_correct" in lbl:
                raise RuntimeError("boom")
            return "reject", {"seconds": 0}
    orig = ev.OpenJMLRunnerPersistent
    ev.OpenJMLRunnerPersistent = R
    try:
        res = ev.evaluate_problem(ev.Task.from_dict(t), code, output_dir=tempfile.mkdtemp(), max_pairs=3,
                                  exclude_collection_tasks=False)
    finally:
        ev.OpenJMLRunnerPersistent = orig
    assert res["post_correctness"]["errors"] == 3 and res["check_exceptions"] >= 3, res["post_correctness"]
    assert res["post_completeness"]["total"] > 0 and not res["pair_errors"]


def test_vacuity_guard_only_for_helper_contracts():
    from spec_harness.harness import MethodSpec
    from spec_harness.eval_llm_response import helpers_have_contracts
    assert not helpers_have_contracts(MethodSpec(jml_block="", helpers="static int f(int x) { return x; }"))
    assert helpers_have_contracts(MethodSpec(jml_block="", helpers="//@ ensures false;\nstatic int f(int x) { return x; }"))


def test_slots_never_below_two():
    import spec_harness.eval_llm_response as ev
    old = ev.JVM_SLOTS.n
    ev.set_jvm_slots(1)
    assert ev.JVM_SLOTS.n == 2
    ev.set_jvm_slots(old)


def test_array_inputs_pinned_elementwise():
    """2-D (and deeper) arrays pin every element; String / boxed elements are pinned by value, not identity."""
    from spec_harness.harness import StubBuilder, JType
    sb = StubBuilder()
    two = "\n".join(sb._assume_array("a", JType("int", 2), [[1, 2], [3]]))
    assert "a[0][1] == 2;" in two and "a[1][0] == 3;" in two
    three = "\n".join(sb._assume_array("c", JType("int", 3), [[[7]]]))
    assert "c[0][0][0] == 7;" in three
    strs = "\n".join(sb._assume_array("s", JType("String", 1), ["ab", None]))
    assert 's[0] != null && s[0].equals("ab");' in strs and "s[1] == null;" in strs and 's[0] == "ab"' not in strs
    rows = "\n".join(sb._assume_array("t", JType("String", 2), [["x"], None]))
    assert 't[0][0] != null && t[0][0].equals("x");' in rows and "t[1] == null;" in rows
    boxed = "\n".join(sb._assume_array("b", JType("Integer", 1), [5]))
    assert "b[0] != null && b[0] == 5;" in boxed


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{fails} failed")
    sys.exit(1 if fails else 0)
