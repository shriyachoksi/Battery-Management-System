"""Compile the firmware's ML engine on this PC and check it against Python.

    python ml/tests/run_parity_test.py

Run train_models.py first (it writes ml/build/parity_*.csv and ml_model.h).
Uses $CXX, g++ or clang++ if present, otherwise the `ziglang` pip package
(pip install ziglang) as a self-contained C++ compiler.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "ml" / "build"


def compiler():
    for cxx in (os.environ.get("CXX"), "g++", "clang++"):
        if cxx and shutil.which(cxx):
            return [cxx]
    try:
        import ziglang  # noqa: F401
        return [sys.executable, "-m", "ziglang", "c++"]
    except ImportError:
        sys.exit("No C++ compiler found: install g++/clang++, or `pip install ziglang`.")


def main():
    for name in ("parity_input.csv", "parity_expected.csv"):
        if not (BUILD / name).exists():
            sys.exit(f"{BUILD / name} missing - run ml/train_models.py first.")
    exe = BUILD / ("parity_test.exe" if os.name == "nt" else "parity_test")
    cmd = compiler() + ["-O2", "-std=c++17", "-Wall", "-Wextra", "-Wno-unused-result",
                        "-I", str(ROOT / "code" / "smart_bms_ml"),
                        str(ROOT / "ml" / "tests" / "parity_test.cpp"), "-o", str(exe)]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    result = subprocess.run([str(exe), str(BUILD / "parity_input.csv"), str(BUILD / "parity_expected.csv")])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
