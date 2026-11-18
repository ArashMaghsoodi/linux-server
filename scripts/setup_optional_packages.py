#!/usr/bin/env python3
"""Install optional setup packages without blocking server startup."""

import subprocess
import sys
from pathlib import Path


def install_optional_packages(packages: list[str], warnings_path: Path) -> list[str]:
    warnings = []

    for package in packages:
        print(f"==> Installing optional package: {package}", flush=True)
        try:
            result = subprocess.run(
                ["sudo", "apt-get", "install", "-y", package],
                check=False,
            )
        except OSError as error:
            warning = f"Optional package '{package}' installation failed ({error})."
        else:
            if result.returncode == 0:
                continue
            warning = (
                f"Optional package '{package}' installation failed "
                f"(apt-get exit {result.returncode})."
            )

        print(f"WARNING: {warning}", file=sys.stderr, flush=True)
        with warnings_path.open("a", encoding="utf-8") as warnings_file:
            warnings_file.write(f"{warning}\n")
        warnings.append(warning)

    return warnings


if __name__ == "__main__":
    install_optional_packages(
        sys.argv[2:],
        Path(sys.argv[1]),
    )
