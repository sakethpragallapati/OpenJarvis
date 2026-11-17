"""faster-whisper finds cuBLAS/cuDNN shipped with PyTorch or nvidia wheels."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from openjarvis.speech import faster_whisper as fw


def test_finds_torch_and_nvidia_dll_folders(monkeypatch, tmp_path: Path) -> None:
    torch_lib = tmp_path / "torch" / "lib"
    torch_lib.mkdir(parents=True)
    (torch_lib / "cublas64_12.dll").write_bytes(b"")
    cudnn_bin = tmp_path / "nvidia" / "cudnn" / "bin"
    cudnn_bin.mkdir(parents=True)
    (cudnn_bin / "cudnn64_9.dll").write_bytes(b"")
    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)  # empty

    specs = {
        "torch": SimpleNamespace(origin=str(tmp_path / "torch" / "__init__.py")),
        "nvidia": SimpleNamespace(
            submodule_search_locations=[str(tmp_path / "nvidia")]
        ),
    }
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: specs.get(name))
    assert fw._cuda_dll_dirs() == [str(torch_lib), str(cudnn_bin)]


def test_nothing_found_without_cuda_packages(monkeypatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert fw._cuda_dll_dirs() == []
