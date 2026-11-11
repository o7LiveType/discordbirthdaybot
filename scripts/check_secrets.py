"""Check the Git index before publishing; never print a matched secret."""

import re
import subprocess


def main():
    filenames = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")  # noqa: S603,S607
    failed = False
    patterns = [
        rb"(?:mfa\.[\w-]{20,}|[A-Za-z\d_-]{23,28}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,})",
        rb"(?:ghp|github_pat)_[A-Za-z0-9_]{20,}",
        rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    ]
    for name in filter(None, filenames):
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            print(f"ERROR: forbidden credential file tracked: {name}")
            failed = True
        content = subprocess.check_output(["git", "show", f":{name}"])  # noqa: S603,S607
        if any(re.search(pattern, content) for pattern in patterns):
            print(f"ERROR: possible credential in tracked file: {name}")
            failed = True
    if failed:
        return 1
    print("Tracked files passed credential-pattern checks (not a complete secret detector).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
