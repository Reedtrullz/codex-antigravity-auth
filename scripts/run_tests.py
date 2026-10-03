"""Start pytest after scrubbing ambient state, including before plugin loading."""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "test_support"))
from _test_isolation import install

install()
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]]))
