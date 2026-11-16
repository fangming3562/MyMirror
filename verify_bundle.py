"""Compare an installed mirror or private overlay archive to the reference manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile

MAX_OVERLAY_FILE = 2 * 1024 * 1024


def matches(file, expected):
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(128 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest() == expected


def verify_directory(directory, files):
    for name, expected in sorted(files.items()):
        candidate = directory / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("缺少普通文件: " + name)
        with candidate.open("rb") as file:
            if not matches(file, expected):
                raise ValueError("校验值不同: " + name)
        print("OK", name)


def verify_archive(archive, files):
    seen = set()
    with tarfile.open(archive, mode="r|gz") as bundle:
        for member in bundle:
            name = member.name
            if name not in files or name in seen or not member.isreg() or member.size > MAX_OVERLAY_FILE:
                raise ValueError("压缩包包含非预期条目。")
            file = bundle.extractfile(member)
            with file:
                if not matches(file, files[name]):
                    raise ValueError("校验值不同: " + name)
            seen.add(name)
            print("OK", name)
    if seen != set(files):
        raise ValueError("压缩包缺少文件。")


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    group = arguments.add_mutually_exclusive_group(required=True)
    group.add_argument("--directory", type=Path)
    group.add_argument("--archive", type=Path)
    arguments.add_argument("--manifest", type=Path, default=Path(__file__).with_name("manifest.json"))
    options = arguments.parse_args()
    try:
        manifest = json.loads(options.manifest.read_text(encoding="utf-8"))
        if options.directory:
            verify_directory(options.directory, {**manifest["binary"], **manifest["overlay"]})
        else:
            verify_archive(options.archive, manifest["overlay"])
    except (ValueError, TypeError, KeyError, OSError, tarfile.TarError) as error:
        arguments.exit(1, "核对失败: " + str(error) + "\n")
    print("与参考版本一致。")


if __name__ == "__main__":
    main()
