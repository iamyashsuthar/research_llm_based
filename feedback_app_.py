"""LLM Feedback Viewer: a desktop app (tkinter, standard library only) for the A/B feedback pipeline.

Tab 1, Browse results: open the feedback.db written by the notebook and inspect every logged run:
    the student code, the verification signal, the exact prompt sent to the LLM, and the output it returned.
    "Compare A vs B" shows the two conditions side by side for one submission.
Tab 2, Try it live: paste an assignment and student code, optionally add test cases, and send it through the
    verification harness and the LLM to see the prompt and the output for Condition A and/or B.

Run:   python feedback_app.py [path\\to\\feedback.db]        (Windows: pythonw feedback_app.py hides the console)
Keys:  put OPENAI_API_KEY / ANTHROPIC_API_KEY in the environment or in a .env file next to this script.
"""
import os, sys, re, json, ast, time, math, random, sqlite3, pathlib, tempfile, subprocess, hashlib, glob
import threading, queue
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

TIMEOUT_S = 5
MEM_MB = 512
INCLUDE_SCORE_IN_SIGNAL = True
LLM_PROVIDER = "mock"
OPENAI_MODEL = "gpt-4o"
ANTHROPIC_MODEL = "claude-sonnet-5-5"
TEMPERATURE = 0.0
SEED = 42
TESTS = {}

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def get_secret(name, ask=False):
    try:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret(name)
    except Exception:
        pass
    value = os.environ.get(name)
    if not value and ask:
        import getpass
        try:
            value = getpass.getpass(f"Enter {name} (hidden, kept only for this session): ").strip()
        except (EOFError, OSError):
            value = ""
        if value:
            os.environ[name] = value
    return value

def ensure(pkg, import_name=None):
    try:
        __import__(import_name or pkg)
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg], check=True)

if os.name != "posix":
    print("Windows detected: CPU and memory limits are unavailable (timeout only). Use WSL for the full sandbox.")

BLOCKED_IMPORTS = {"socket", "subprocess", "requests", "urllib", "http", "ftplib", "smtplib", "shutil",
                   "ctypes", "multiprocessing", "pty", "telnetlib", "webbrowser"}
BLOCKED_ATTRS = {"system", "popen", "remove", "unlink", "rmdir", "rmtree", "kill", "fork", "execv", "execl"}

def static_check(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError: {e.msg} (line {e.lineno})", None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BLOCKED_IMPORTS:
                    return None, f"imports {a.name}"
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in BLOCKED_IMPORTS:
            return None, f"imports {node.module}"
        elif isinstance(node, ast.Attribute) and node.attr in BLOCKED_ATTRS:
            return None, f"calls .{node.attr}"
    return None, None

LAUNCHER = """import sys, runpy
try:
    import resource
    for name, value in (("RLIMIT_CPU", {cpu}), ("RLIMIT_AS", {mem}), ("RLIMIT_FSIZE", {fsz})):
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
        except (ValueError, OSError):
            pass
except ImportError:
    pass
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""

def run_python(code, stdin_text=""):
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "submission.py"
        f.write_text(code)
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC") if k in os.environ}
        env.update(PYTHONHASHSEED="0", MPLBACKEND="Agg")
        launcher = LAUNCHER.format(cpu=TIMEOUT_S, mem=MEM_MB * 2**20, fsz=5 * 2**20)
        try:
            r = subprocess.run([sys.executable, "-c", launcher, str(f)], input=stdin_text, capture_output=True, text=True,
                               timeout=TIMEOUT_S + 2, cwd=d, env=env)
            killed = r.returncode in (-24, -9) and not r.stderr.strip()
            return dict(rc=r.returncode, out=r.stdout[-1500:], err="TimeoutExpired" if killed else r.stderr[-1500:], timeout=killed)
        except subprocess.TimeoutExpired:
            return dict(rc=-9, out="", err="TimeoutExpired", timeout=True)

def last_line(s):
    lines = [l for l in s.strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""

def run_expr_test(code, t):
    if "script" in t:
        r = run_python(code + "\n" + t["script"] + "\n")
        return dict(name=t["name"], ok=r["rc"] == 0, expected="", got="", err=last_line(r["err"]) if r["rc"] != 0 else "", raw=True)
    if "left" in t:
        tail = (f"\n__l = ({t['left']})\n__r = ({t['right']})\nprint('@@', __l == __r)\n"
                "print('@@', repr(__l)[:120])\nprint('@@', repr(__r)[:120])\n")
    else:
        tail = f"\nprint('@@', bool({t['expr']}))\n"
    r = run_python(code + "\n" + tail)
    marks = [l[3:] for l in r["out"].splitlines() if l.startswith("@@ ")]
    ok = r["rc"] == 0 and bool(marks) and marks[0] == "True"
    return dict(name=t["name"], ok=ok, expected=marks[2] if len(marks) > 2 else "", got=marks[1] if len(marks) > 1 else "",
                err=last_line(r["err"]) if r["rc"] != 0 else "", raw=True)

def verify(code, score, problem_id):
    syn, blocked = static_check(code)
    sig = dict(syntax=syn, blocked=blocked, run=None, tests=[], score=score)
    if syn or blocked:
        return sig
    tests = TESTS.get(problem_id, [])
    if tests:
        for t in tests:
            if "stdin" not in t:
                sig["tests"].append(run_expr_test(code, t))
                continue
            r = run_python(code, t["stdin"] + "\n")
            got = r["out"].strip()
            ok = r["rc"] == 0 and got == str(t["expected"]).strip()
            sig["tests"].append(dict(name=t["name"], ok=ok, expected=str(t["expected"]).strip(), got=got[:80],
                                     err=last_line(r["err"]) if r["rc"] != 0 else ""))
    else:
        r = run_python(code, "")
        if "EOFError" in r["err"]:
            sig["run"] = "program waits for keyboard input; no test input available"
        elif r["timeout"]:
            sig["run"] = "timed out"
        elif r["rc"] != 0:
            sig["run"] = last_line(r["err"])
        else:
            sig["run"] = "ran without errors"
    return sig

def signal_text(sig):
    first = sig["syntax"] or (f"not run ({sig['blocked']})" if sig["blocked"] else "no syntax errors")
    L = ["Interpreter check: " + first]
    if sig["run"]:
        L.append("Run result: " + sig["run"])
    if sig["tests"]:
        L.append("Unit tests:")
        shown = sig["tests"]
        if len(shown) > 12:
            fails = [t for t in shown if not t["ok"]]
            L.append(f"  {len(shown) - len(fails)} of {len(shown)} tests pass")
            shown = fails[:8]
        for t in shown:
            if t["ok"]:
                L.append(f"  {t['name']}: PASS")
            else:
                exp = (t["expected"], t["got"]) if t.get("raw") else (repr(t["expected"]), repr(t["got"]))
                detail = t["err"] or (f"expected {exp[0]}, got {exp[1]}" if exp[0] or exp[1] else "assertion is false")
                L.append(f"  {t['name']}: FAIL ({detail})")
    if INCLUDE_SCORE_IN_SIGNAL and sig["score"] is not None and not math.isnan(sig["score"]):
        L.append(f"Course autograder score: {sig['score']:.2f} (fraction of the course's test cases passed)")
    return "\n".join(L)

SYSTEM = ("You are a programming tutor for an introductory Python course. Judge whether the student's code solves the "
          "assignment and give short hints. Never reveal the full corrected solution and never write corrected code.")

SCHEMA = ('Return ONLY a JSON object, no other text:\n'
          '{"is_correct": true or false,\n'
          ' "hints": [{"line": <int>, "code_line": "<exact text of that line>", "explanation": "<short hint, no corrected code>"}]}\n'
          'If the code is correct return is_correct true and an empty hints list. Give at most 2 hints.')

def numbered(code):
    return "\n".join(f"{i+1}: {l}" for i, l in enumerate(code.splitlines()))

def build_prompt(exercise, code, condition, sig_text):
    p = f"Assignment:\n{exercise}\n\nStudent code (line numbers shown):\n{numbered(code)}\n"
    if condition == "B":
        p += ("\nVerification results for this code:\n" + sig_text +
              "\n\nBase your hints on what these results show. Do not claim problems the results do not support.\n")
    return p + "\n" + SCHEMA

def _mock(prompt):
    rng = random.Random(int(hashlib.md5(prompt.encode()).hexdigest(), 16))
    lines = re.findall(r"^(\d+): (.*)$", prompt, re.M)
    if "Verification results" in prompt:
        correct = not (("FAIL" in prompt) or ("Error" in prompt) or ("score:" in prompt and "score: 1.00" not in prompt))
    else:
        correct = rng.random() < 0.45
    if correct:
        return json.dumps(dict(is_correct=True, hints=[]))
    ln, txt = lines[rng.randrange(len(lines))] if lines else ("1", "")
    return json.dumps(dict(is_correct=False, hints=[dict(line=int(ln), code_line=txt, explanation="Check the logic on this line.")]))

_clients = {}

def call_llm(prompt):
    if LLM_PROVIDER == "mock":
        return _mock(prompt)
    if LLM_PROVIDER == "openai":
        if "openai" not in _clients:
            ensure("openai")
            from openai import OpenAI
            _clients["openai"] = OpenAI(api_key=get_secret("OPENAI_API_KEY"))
        r = _clients["openai"].chat.completions.create(
            model=OPENAI_MODEL, temperature=TEMPERATURE, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
        return r.choices[0].message.content
    if LLM_PROVIDER == "anthropic":
        if "anthropic" not in _clients:
            ensure("anthropic")
            import anthropic
            _clients["anthropic"] = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        r = _clients["anthropic"].messages.create(
            model=ANTHROPIC_MODEL, max_tokens=700, temperature=TEMPERATURE, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        return r.content[0].text
    raise ValueError(f"LLM_PROVIDER must be 'mock', 'openai' or 'anthropic', got {LLM_PROVIDER!r}")

def parse_hint(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj.get("is_correct"), bool) or not isinstance(obj.get("hints", []), list):
        return None
    hints = [dict(line=h.get("line"), code_line=str(h.get("code_line", "")), explanation=str(h.get("explanation", "")))
             for h in obj.get("hints", [])[:2] if isinstance(h, dict)]
    return dict(is_correct=obj["is_correct"], hints=hints)

def render_hint(p):
    head = "Verdict: code is " + ("CORRECT" if p["is_correct"] else "INCORRECT")
    return head + "".join(f"\n  Line {h['line']}: `{h['code_line']}` - {h['explanation']}" for h in p["hints"])

def one_call(prompt):
    last = None
    for attempt in range(4):
        try:
            text = call_llm(prompt)
            parsed = parse_hint(text)
            if parsed is None:
                text = call_llm(prompt)
                parsed = parse_hint(text)
            return text, parsed
        except Exception as e:
            last = e
            time.sleep(2 ** attempt)
    raise last

DEFAULT_ASSIGNMENT = "Read an integer n and print True if n is prime, otherwise print False."
DEFAULT_CODE = "n = int(input())\nprint(all(n % i for i in range(2, n)))\n"
DEFAULT_TESTS = "2 => True\n9 => False\n1 => False\n13 => True"
DEFAULT_MODELS = {"mock": "mock", "openai": "gpt-4o", "anthropic": "claude-sonnet-5-5"}
TAG_STYLES = {"diff": dict(background="#fff1d6"), "bad": dict(foreground="#b42318"),
              "good": dict(foreground="#067647"), "dim": dict(foreground="#667085")}


def load_run_rows(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(feedback_runs)")}
    prompt_col = "r.prompt" if "prompt" in cols else "NULL"
    return con.execute(f"""
        SELECT r.id run_id, r.submission_id sid, r.condition, r.run_k, r.model, r.temperature, r.raw_json, r.parsed_ok,
               {prompt_col} prompt, s.problem_id, s.exercise, s.code, s.signal_text, s.ground_truth_correct gt, s.score
        FROM feedback_runs r JOIN submissions s ON s.id = r.submission_id
        ORDER BY r.submission_id, r.condition, r.run_k""").fetchall()


def run_prompt(row):
    return row["prompt"] or build_prompt(row["exercise"], row["code"], row["condition"], row["signal_text"])


def run_parsed(row):
    if row["parsed_ok"]:
        try:
            return json.loads(row["raw_json"])
        except json.JSONDecodeError:
            return None
    return None


def output_segments(parsed, raw, meta, verdict_tag=None):
    if parsed is None:
        return [(meta + "\n\n", "dim"), ("Could not parse the model output as the expected JSON:\n\n", "bad"), (raw or "", None)]
    return [(meta + "\n\n", "dim"), (render_hint(parsed) + "\n\n", verdict_tag), ("raw JSON\n", "dim"), (json.dumps(parsed, indent=2), None)]


def parse_tests(raw):
    tests = []
    for i, line in enumerate([l for l in raw.splitlines() if l.strip()], 1):
        if "=>" not in line:
            raise ValueError(f"Test line {i} must look like:  input => expected output")
        a, b = line.split("=>", 1)
        tests.append(dict(name=f"test_{i}", stdin=a.strip().replace("\\n", "\n"), expected=b.strip().replace("\\n", "\n")))
    return tests


def key_status():
    if LLM_PROVIDER == "mock":
        return "mock model: no API call is made"
    name = "OPENAI_API_KEY" if LLM_PROVIDER == "openai" else "ANTHROPIC_API_KEY"
    return f"{name} found" if get_secret(name) else f"{name} MISSING"


class Pane(ttk.Frame):
    def __init__(self, master, title, mono, height=8, wrap="word", hscroll=False):
        super().__init__(master)
        head = ttk.Frame(self)
        head.pack(fill="x")
        bold = tkfont.Font(font="TkDefaultFont")
        bold.configure(weight="bold")
        self.title_var = tk.StringVar(value=title)
        ttk.Label(head, textvariable=self.title_var, font=bold).pack(side="left")
        ttk.Button(head, text="Copy", width=6, command=self.copy).pack(side="right")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, wrap="none" if hscroll else wrap, height=height, font=mono, state="disabled",
                            relief="solid", borderwidth=1, padx=6, pady=4)
        ys = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ys.set)
        if hscroll:
            xs = ttk.Scrollbar(body, orient="horizontal", command=self.text.xview)
            self.text.configure(xscrollcommand=xs.set)
            xs.pack(side="bottom", fill="x")
        ys.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for name, opts in TAG_STYLES.items():
            self.text.tag_configure(name, **opts)

    def set(self, content):
        segments = [(content, None)] if isinstance(content, str) else content
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for chunk, tag in segments:
            self.text.insert("end", chunk, (tag,) if tag else ())
        self.text.configure(state="disabled")

    def set_prompt(self, prompt):
        self.set(prompt)
        start = self.text.search("Verification results for this code:", "1.0", "end")
        if start:
            end = self.text.search("Base your hints", start, "end")
            self.text.tag_add("diff", start, f"{end} lineend" if end else "end")

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.text.get("1.0", "end-1c"))


def make_text(parent, mono, height, wrap="word", expand=True):
    frame = ttk.Frame(parent)
    frame.pack(fill="both" if expand else "x", expand=expand, pady=(0, 6))
    text = tk.Text(frame, wrap=wrap, height=height, font=mono, relief="solid", borderwidth=1, padx=6, pady=4, undo=True)
    ys = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=ys.set)
    ys.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    return text


class CompareWindow(tk.Toplevel):
    def __init__(self, app, sid):
        super().__init__(app.root)
        self.app, self.sid = app, sid
        self.title(f"Compare Condition A vs B: {sid}")
        self.geometry("1280x780")
        self.runs = {(r["condition"], r["run_k"]): r for r in app.rows.values() if r["sid"] == sid}
        ks = sorted({k for _, k in self.runs})
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        ttk.Label(bar, text="Sample k:").pack(side="left")
        self.k_box = ttk.Combobox(bar, values=ks, width=4, state="readonly")
        self.k_box.set(ks[0] if ks else 0)
        self.k_box.pack(side="left", padx=6)
        self.k_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        any_row = next(iter(self.runs.values()))
        truth = "correct" if any_row["gt"] else "incorrect"
        ttk.Label(bar, text=f"Ground truth: {truth}     autograder score: {any_row['score']}").pack(side="left", padx=16)
        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.panes = {}
        for cond, label in (("A", "Condition A: code only"), ("B", "Condition B: code + verification")):
            col = ttk.PanedWindow(main, orient="vertical")
            main.add(col, weight=1)
            prompt = Pane(col, f"{label}: prompt sent", app.mono)
            out = Pane(col, f"{label}: output", app.mono)
            col.add(prompt, weight=3)
            col.add(out, weight=2)
            self.panes[cond] = (prompt, out)
        self.refresh()

    def refresh(self):
        k = int(self.k_box.get())
        for cond, (prompt_pane, out_pane) in self.panes.items():
            row = self.runs.get((cond, k))
            if row is None:
                prompt_pane.set("No run logged.")
                out_pane.set("")
                continue
            prompt_pane.set_prompt(run_prompt(row))
            parsed = run_parsed(row)
            ok = parsed is not None and parsed["is_correct"] == bool(row["gt"])
            out_pane.set(output_segments(parsed, row["raw_json"], f"model {row['model']}   temperature {row['temperature']}   k={k}",
                                         "good" if ok else "bad"))


class App:
    def __init__(self, root, db_path=None):
        self.root = root
        root.title("LLM Feedback Viewer")
        root.geometry("1400x880")
        root.minsize(1000, 640)
        self.mono = tkfont.nametofont("TkFixedFont").copy()
        self.mono.configure(size=10)
        self.con = None
        self.rows = {}
        self.db_path = None
        self.results_queue = queue.Queue()
        self._style()
        self._menu()
        self._toolbar()
        tabs = ttk.Notebook(root)
        tabs.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        browse = ttk.Frame(tabs, padding=6)
        live = ttk.Frame(tabs, padding=6)
        tabs.add(browse, text="  Browse results  ")
        tabs.add(live, text="  Try it live  ")
        self._build_browse(browse)
        self._build_live(live)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w", padding=(6, 2)).pack(fill="x", side="bottom")
        root.bind("<Control-Return>", lambda e: self.run_live())
        start = db_path or next((p for p in ("feedback.db", os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback.db"))
                                 if os.path.exists(p)), None)
        if start:
            self.open_db(start)
        else:
            self.status.set("Open a feedback.db written by the notebook (File > Open DB), or use the live tab.")

    def _style(self):
        style = ttk.Style()
        names = style.theme_names()
        style.theme_use("vista" if "vista" in names else "clam" if "clam" in names else names[0])
        style.configure("Treeview", rowheight=22)
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        style.configure("Run.TButton", font=self.bold, padding=6)

    def _menu(self):
        bar = tk.Menu(self.root)
        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label="Open DB...", command=self.choose_db)
        file_menu.add_command(label="Reload DB", command=lambda: self.db_path and self.open_db(self.db_path))
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.root.destroy)
        bar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label="About", command=lambda: messagebox.showinfo(
            "About", "Shows the exact prompts sent to the LLM and the outputs returned, for Condition A (code only) "
                     "and Condition B (code + verification results)."))
        bar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=bar)

    def _toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="Provider").pack(side="left")
        self.provider = ttk.Combobox(bar, values=list(DEFAULT_MODELS), width=10, state="readonly")
        self.provider.set(LLM_PROVIDER)
        self.provider.pack(side="left", padx=(4, 12))
        self.provider.bind("<<ComboboxSelected>>", self.on_provider)
        ttk.Label(bar, text="Model").pack(side="left")
        self.model = ttk.Entry(bar, width=24)
        self.model.insert(0, DEFAULT_MODELS[LLM_PROVIDER])
        self.model.pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="Temperature").pack(side="left")
        self.temp = ttk.Spinbox(bar, from_=0.0, to=2.0, increment=0.1, width=5)
        self.temp.set(TEMPERATURE)
        self.temp.pack(side="left", padx=(4, 12))
        self.key_label = ttk.Label(bar, text=key_status())
        self.key_label.pack(side="left")
        ttk.Button(bar, text="Open DB...", command=self.choose_db).pack(side="right")
        self.db_label = ttk.Label(bar, text="no database loaded")
        self.db_label.pack(side="right", padx=8)

    def on_provider(self, _event=None):
        self.model.delete(0, "end")
        self.model.insert(0, DEFAULT_MODELS[self.provider.get()])
        self.apply_settings()

    def apply_settings(self):
        global LLM_PROVIDER, OPENAI_MODEL, ANTHROPIC_MODEL, TEMPERATURE
        LLM_PROVIDER = self.provider.get()
        model = self.model.get().strip() or DEFAULT_MODELS[LLM_PROVIDER]
        if LLM_PROVIDER == "openai":
            OPENAI_MODEL = model
        elif LLM_PROVIDER == "anthropic":
            ANTHROPIC_MODEL = model
        try:
            TEMPERATURE = float(self.temp.get())
        except ValueError:
            TEMPERATURE = 0.0
        self.key_label.configure(text=key_status())

    def _build_browse(self, parent):
        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0, 0, 6, 0))
        paned.add(left, weight=1)
        self.root.after(150, lambda: paned.sashpos(0, 460))
        filters = ttk.Frame(left)
        filters.pack(fill="x", pady=(0, 4))
        self.f_cond = ttk.Combobox(filters, values=["All", "A", "B"], width=5, state="readonly")
        self.f_cond.set("All")
        self.f_cond.pack(side="left")
        self.f_prob = ttk.Combobox(filters, values=["All problems"], width=16, state="readonly")
        self.f_prob.set("All problems")
        self.f_prob.pack(side="left", padx=6)
        self.f_wrong = tk.BooleanVar(value=False)
        ttk.Checkbutton(filters, text="Only wrong verdicts", variable=self.f_wrong, command=self.refresh_list).pack(side="left")
        for box in (self.f_cond, self.f_prob):
            box.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())
        cols = (("sub", "Submission", 96), ("cond", "Cond", 46), ("k", "k", 30), ("says", "Model says", 94), ("truth", "Truth", 80), ("ok", "Match", 54))
        wrap = ttk.Frame(left)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for key, title, width in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=width, anchor="w", stretch=False)
        ys = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("wrong", foreground="#b42318")
        self.tree.tag_configure("unparsed", foreground="#667085")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.count_label = ttk.Label(left, text="")
        self.count_label.pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(paned)
        paned.add(right, weight=4)
        ttk.Button(right, text="Compare A vs B for this submission", command=self.compare).pack(anchor="e", pady=(0, 4))
        vertical = ttk.PanedWindow(right, orient="vertical")
        vertical.pack(fill="both", expand=True)
        top = ttk.PanedWindow(vertical, orient="horizontal")
        vertical.add(top, weight=2)
        self.b_code = Pane(top, "Student code (line numbers shown)", self.mono, hscroll=True)
        self.b_sig = Pane(top, "Verification signal", self.mono)
        top.add(self.b_code, weight=1)
        top.add(self.b_sig, weight=1)
        bottom = ttk.PanedWindow(vertical, orient="horizontal")
        vertical.add(bottom, weight=3)
        self.b_prompt = Pane(bottom, "Prompt sent to the LLM  (shaded = added in B)", self.mono)
        self.b_out = Pane(bottom, "Output from the LLM", self.mono)
        bottom.add(self.b_prompt, weight=1)
        bottom.add(self.b_out, weight=1)

    def choose_db(self):
        path = filedialog.askopenfilename(title="Open feedback database", filetypes=[("SQLite database", "*.db *.sqlite"), ("All files", "*.*")])
        if path:
            self.open_db(path)

    def open_db(self, path):
        try:
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            rows = load_run_rows(con)
        except sqlite3.Error as e:
            messagebox.showerror("Could not open database", f"{path}\n\n{e}\n\nExpected the feedback.db written by the notebook.")
            return
        if self.con:
            self.con.close()
        self.con, self.db_path = con, path
        self.rows = {r["run_id"]: r for r in rows}
        problems = sorted({r["problem_id"] for r in rows})
        self.f_prob.configure(values=["All problems"] + problems)
        self.f_prob.set("All problems")
        self.db_label.configure(text=os.path.basename(path))
        self.refresh_list()
        self.status.set(f"Loaded {len(rows)} runs from {path}")

    def refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        cond, prob = self.f_cond.get(), self.f_prob.get()
        shown = 0
        for row in self.rows.values():
            if cond != "All" and row["condition"] != cond:
                continue
            if prob != "All problems" and row["problem_id"] != prob:
                continue
            parsed = run_parsed(row)
            truth = "correct" if row["gt"] else "incorrect"
            if parsed is None:
                says, match, tag = "unparsed", "-", "unparsed"
            else:
                says = "correct" if parsed["is_correct"] else "incorrect"
                good = parsed["is_correct"] == bool(row["gt"])
                match, tag = ("yes" if good else "NO"), ("" if good else "wrong")
            if self.f_wrong.get() and tag != "wrong":
                continue
            self.tree.insert("", "end", iid=str(row["run_id"]), values=(row["sid"], row["condition"], row["run_k"], says, truth, match),
                             tags=(tag,) if tag else ())
            shown += 1
        self.count_label.configure(text=f"{shown} of {len(self.rows)} runs")

    def selected_row(self):
        sel = self.tree.selection()
        return self.rows.get(int(sel[0])) if sel else None

    def on_select(self, _event=None):
        row = self.selected_row()
        if row is None:
            return
        self.b_code.set(numbered(row["code"]))
        sig = row["signal_text"] or ""
        if row["condition"] == "A":
            sig = "(Condition A does not receive this block; shown for reference)\n\n" + sig
        self.b_sig.set(sig)
        self.b_prompt.set_prompt(run_prompt(row))
        parsed = run_parsed(row)
        ok = parsed is not None and parsed["is_correct"] == bool(row["gt"])
        meta = f"condition {row['condition']}   model {row['model']}   temperature {row['temperature']}   k={row['run_k']}"
        self.b_out.set(output_segments(parsed, row["raw_json"], meta, "good" if ok else "bad"))

    def compare(self):
        row = self.selected_row()
        if row is None:
            messagebox.showinfo("Compare", "Select a run in the list first.")
            return
        CompareWindow(self, row["sid"])

    def _build_live(self, parent):
        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        paned.add(left, weight=1)
        ttk.Label(left, text="Assignment").pack(anchor="w")
        self.in_assignment = make_text(left, self.mono, 4, expand=False)
        self.in_assignment.insert("1.0", DEFAULT_ASSIGNMENT)
        ttk.Label(left, text="Student code").pack(anchor="w")
        self.in_code = make_text(left, self.mono, 12, wrap="none")
        self.in_code.insert("1.0", DEFAULT_CODE)
        ttk.Label(left, text="Test cases:  input => expected output").pack(anchor="w")
        self.in_tests = make_text(left, self.mono, 4, wrap="none", expand=False)
        self.in_tests.insert("1.0", DEFAULT_TESTS)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text="Autograder score (optional, 0-1):").pack(side="left")
        self.in_score = ttk.Entry(row, width=6)
        self.in_score.pack(side="left", padx=6)
        self.use_a = tk.BooleanVar(value=True)
        self.use_b = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="Condition A: code only", variable=self.use_a).pack(anchor="w")
        ttk.Checkbutton(left, text="Condition B: code + verification results", variable=self.use_b).pack(anchor="w")
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=8)
        self.run_btn = ttk.Button(buttons, text="Run  (Ctrl+Enter)", style="Run.TButton", command=self.run_live)
        self.run_btn.pack(side="left")
        ttk.Button(buttons, text="Reset example", command=self.reset_live).pack(side="left", padx=8)

        right = ttk.PanedWindow(paned, orient="vertical")
        paned.add(right, weight=3)
        self.l_sig = Pane(right, "Verification result (this is what Condition B adds to the prompt)", self.mono, height=5)
        right.add(self.l_sig, weight=1)
        columns = ttk.PanedWindow(right, orient="horizontal")
        right.add(columns, weight=5)
        self.live_panes = {}
        for cond, label in (("A", "Condition A: code only"), ("B", "Condition B: code + verification")):
            col = ttk.PanedWindow(columns, orient="vertical")
            columns.add(col, weight=1)
            prompt = Pane(col, f"{label}: prompt sent", self.mono)
            out = Pane(col, f"{label}: output", self.mono)
            col.add(prompt, weight=3)
            col.add(out, weight=2)
            self.live_panes[cond] = (prompt, out)

    def reset_live(self):
        for widget, value in ((self.in_assignment, DEFAULT_ASSIGNMENT), (self.in_code, DEFAULT_CODE), (self.in_tests, DEFAULT_TESTS)):
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
        self.in_score.delete(0, "end")

    def run_live(self):
        conditions = [c for c, var in (("A", self.use_a), ("B", self.use_b)) if var.get()]
        if not conditions:
            messagebox.showinfo("Run", "Tick at least one condition.")
            return
        assignment = self.in_assignment.get("1.0", "end-1c").strip()
        code = self.in_code.get("1.0", "end-1c")
        if not assignment or not code.strip():
            messagebox.showinfo("Run", "Enter an assignment and the student code.")
            return
        try:
            tests = parse_tests(self.in_tests.get("1.0", "end-1c"))
            score_text = self.in_score.get().strip()
            score = float(score_text) if score_text else None
        except ValueError as e:
            messagebox.showerror("Check your input", str(e))
            return
        self.apply_settings()
        if LLM_PROVIDER != "mock" and "MISSING" in key_status():
            messagebox.showerror("API key missing", key_status() + ".\n\nAdd it to your environment or a .env file next to this app, "
                                 "or choose the mock provider to try the interface without an API call.")
            return
        TESTS["live"] = tests
        self.run_btn.state(["disabled"])
        self.status.set(f"Running verification and {LLM_PROVIDER} model ...")

        def worker():
            try:
                signal = signal_text(verify(code, score, "live"))
                results = {}
                for cond in conditions:
                    prompt = build_prompt(assignment, code, cond, signal)
                    started = time.time()
                    text, parsed = one_call(prompt)
                    results[cond] = (prompt, text, parsed, time.time() - started)
                self.results_queue.put(("ok", signal, results))
            except Exception as e:
                self.results_queue.put(("error", f"{type(e).__name__}: {e}", None))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self.poll_live)

    def poll_live(self):
        try:
            kind, payload, results = self.results_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll_live)
            return
        self.run_btn.state(["!disabled"])
        if kind == "error":
            self.status.set("Run failed.")
            messagebox.showerror("Run failed", payload)
            return
        self.l_sig.set(payload)
        for cond, (prompt_pane, out_pane) in self.live_panes.items():
            if cond not in results:
                prompt_pane.set("(condition not run)")
                out_pane.set("")
                continue
            prompt, text, parsed, seconds = results[cond]
            prompt_pane.set_prompt(prompt)
            meta = f"model {self.model.get()}   temperature {TEMPERATURE}   {seconds:.1f}s"
            out_pane.set(output_segments(parsed, text, meta, None))
        self.status.set("Done. " + key_status())


def _dpi_aware():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def main():
    _dpi_aware()
    root = tk.Tk()
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
