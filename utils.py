from pathlib import Path
import sys
import tempfile

def resource_path(relative_path: str, writable: bool = False) -> Path:
    """
    Returns the absolute path to a resource.
    
    - writable=True → returns a folder on disk that can be written to (for CSVs, logs, etc.)
    - writable=False → returns the folder for reading embedded resources
    """
    if getattr(sys, "frozen", False):
        base_path = Path(sys.executable).parent
        if writable:
            # Use a folder next to the EXE or in temp for writing
            return Path(tempfile.gettempdir()) / relative_path
    else:
        base_path = Path(__file__).parent

    return base_path / relative_path