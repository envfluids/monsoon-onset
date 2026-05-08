import importlib.util
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _module_context(module_path):
    module_dir = str(module_path.parent)
    original_cwd = os.getcwd()
    original_path = list(sys.path)
    try:
        os.chdir(module_dir)
        sys.path.insert(0, module_dir)
        yield
    finally:
        os.chdir(original_cwd)
        sys.path[:] = original_path


def call_function(module_path, function_name, *args, **kwargs):
    """Call a function from a script while preserving its local path assumptions."""
    module_path = Path(module_path).resolve()
    if not module_path.exists():
        raise FileNotFoundError(f"Downloader module does not exist: {module_path}")

    module_name = f"_hpc_listener_{module_path.stem}_{abs(hash(module_path))}"
    logging.info("Calling %s:%s", module_path, function_name)

    with _module_context(module_path):
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load module spec for {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        function = getattr(module, function_name)
        return function(*args, **kwargs)
