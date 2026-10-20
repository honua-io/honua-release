"""Exact original producer paths; path aliases are not receipt normalization."""
from pathlib import Path


def canonical_layout(policy):
    binding = policy["executionBinding"]
    filename = binding["implementationPackagingFilename"]
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".whl"):
        raise ValueError("implementation tool filename escapes its canonical input")
    return {"toolWheel": Path("/implementation-tools") / filename,
            "originalsDirectory": Path("/output"), "proposalDirectory": Path("/output/proposal")}
