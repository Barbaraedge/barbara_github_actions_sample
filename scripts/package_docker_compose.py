#!/usr/bin/env python3
"""
Transforms docker-compose.yaml for deployment release:
  - Removes the build: block
  - Replaces the image: field with the provided image reference

Usage: package_docker_compose.py <image_ref> <input_path> <output_path>
"""
import pathlib
import sys


def transform(source: str, image_ref: str) -> str:
    lines = source.splitlines(keepends=True)
    result = []
    in_build_block = False
    build_indent = 0

    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if in_build_block:
            if stripped and indent <= build_indent:
                in_build_block = False
            else:
                continue

        if stripped.startswith("build:"):
            in_build_block = True
            build_indent = indent
            continue

        if stripped.startswith("image:"):
            result.append(" " * indent + "image: " + image_ref + "\n")
            continue

        result.append(line)

    return "".join(result)


if __name__ == "__main__":
    image_ref = sys.argv[1]
    input_path = pathlib.Path(sys.argv[2])
    output_path = pathlib.Path(sys.argv[3])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        transform(input_path.read_text(encoding="utf-8"), image_ref),
        encoding="utf-8",
    )
