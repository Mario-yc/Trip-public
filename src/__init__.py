from pathlib import Path


backend_src = Path(__file__).resolve().parents[1] / "backend" / "src"
if backend_src.is_dir():
    __path__.append(str(backend_src))
