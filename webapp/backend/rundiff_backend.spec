# PyInstaller spec for the Run·Diff backend sidecar.
#
# The backend reaches into sibling source trees at runtime via `sys.path.insert` tricks
# (Path(__file__).resolve().parents[2] / "tutor" | "populator" | "eval/src"). Those runtime
# inserts point at repo-relative dirs that do NOT exist inside a PyInstaller bundle, but that's
# harmless: we add the same source dirs to `pathex` here so Analysis discovers every module and
# bundles them as importable TOP-LEVEL modules. The (now-stale) runtime inserts just prepend a
# missing path to sys.path, which Python ignores.
#
# Things that bit us / are handled below:
#   - dynamic imports (grader, harness, model, populate, instructor_flow, edge_coverage,
#     providers, predictor, sim_student_eval, problems.bank) -> hiddenimports
#   - uvicorn's dynamic loop/protocol/logging imports -> collect_submodules("uvicorn")
#   - the student grade path execs generator source embedded in the bundle JSON (load_populate),
#     so no populator generator files are needed at runtime
#   - .env / groq: only the instructor authoring path needs a groq key; the student app never
#     does, so a missing .env in the bundle is fine (load_dotenv just no-ops)

import os
from PyInstaller.utils.hooks import collect_submodules

# this spec is invoked from webapp/backend/, so CWD is the backend dir
BACKEND = os.path.abspath(os.getcwd())
REPO = os.path.abspath(os.path.join(BACKEND, "..", ".."))
TUTOR = os.path.join(REPO, "tutor")
POPULATOR = os.path.join(REPO, "populator")
EVAL_SRC = os.path.join(REPO, "eval", "src")

extra_paths = [BACKEND, TUTOR, POPULATOR, EVAL_SRC]

hidden = [
    # backend-local modules pulled in dynamically
    "static", "setup_ollama", "predictor", "authoring", "publish", "classes",
    "config", "store", "tutor_core", "state_core", "seal",
    # tutor/
    "grader", "harness", "sim_student_eval", "leakage_eval",
    # populator/
    "model", "populate", "instructor_flow", "edge_coverage", "problems", "problems.bank",
    # eval/src/
    "providers",
    # third-party that may be imported lazily
    "sqlglot", "dotenv",
]
hidden += collect_submodules("uvicorn")
hidden += collect_submodules("sqlglot")

a = Analysis(
    ["run_server.py"],
    pathex=extra_paths,
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rundiff-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="rundiff-backend",
)
