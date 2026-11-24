#!/usr/bin/env python3
"""Pack a Git revision and its two locally supplied assets for private deployment."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

from setup import PRIVATE, ROOT, validate_file


SOURCE = (
    ".dockerignore", ".github", ".gitignore", "Dockerfile", "Makefile",
    "README.md", "compose.yaml", "deploy", "deployment-target.md", "docs",
    "manifest.json", "mirror", "requirements.txt", "tests", "verify_bundle.py",
)


def package(destination, assets):
    # Read the manifest from the revision being packed, not from an uncommitted edit.
    manifest = json.loads(subprocess.check_output(
        ["git", "show", "HEAD:manifest.json"], cwd=ROOT,
    ))
    files = {
        "fuclaude": (manifest["binary"]["fuclaude"], 0o755),
        "frame-shell.js": (manifest["overlay"]["frame-shell.js"], 0o644),
    }
    for name, (expected, _) in files.items():
        validate_file(assets / name, expected)
    source = subprocess.check_output(
        ["git", "archive", "--prefix=MyMirror/", "HEAD", "--", *SOURCE], cwd=ROOT,
    )
    with tempfile.NamedTemporaryFile(dir=destination.parent) as result:
        with tarfile.open(fileobj=result, mode="w:gz", compresslevel=5) as output:
            with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as tracked:
                for item in tracked:
                    if not (item.isfile() or item.isdir()):
                        raise ValueError(f"不支持打包此文件类型：{item.name}")
                    output.addfile(item, tracked.extractfile(item) if item.isfile() else None)
            directory = tarfile.TarInfo("MyMirror/private")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o700
            output.addfile(directory)
            for name, (_, mode) in files.items():
                with (assets / name).open("rb") as asset:
                    item = tarfile.TarInfo("MyMirror/private/" + name)
                    item.mode = mode
                    item.size = os.fstat(asset.fileno()).st_size
                    output.addfile(item, asset)
        result.flush()
        os.link(result.name, destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--assets", type=Path, default=PRIVATE)
    args = parser.parse_args()
    try:
        package(args.output, args.assets)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"打包失败：{exc}\n")
    print(f"已生成私有部署包：{args.output}")
