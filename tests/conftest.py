import os
import sys
from pathlib import Path

# Never let LiteLLM fetch its remote price map during tests.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
