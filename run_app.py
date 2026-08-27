from pathlib import Path
import sys


source_dir = Path(__file__).resolve().parent / "src"
if source_dir.is_dir():
    sys.path.insert(0, str(source_dir))

from local_model_chat.main import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
