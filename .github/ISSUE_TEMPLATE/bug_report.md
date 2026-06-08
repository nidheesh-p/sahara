---
name: Bug report
about: Something is broken or behaving unexpectedly
labels: bug
---

## Description

A clear and concise description of the bug.

## Steps to reproduce

1. 
2. 
3. 

## Expected behaviour

What you expected to happen.

## Actual behaviour

What actually happened. Include the full error message and traceback if there is one.

```
paste output here
```

## Environment

- OS: (e.g. macOS 14.5, Ubuntu 24.04, Windows 11)
- Python version: (`python3 --version` or `py -3.11 --version` on Windows)
- Sahara version: (`sahara --version`)
- Install method: (`pipx install sahara-memory` / virtual environment / editable install / other)
- Storage backend: (local / minio / s3)

## Configuration (sanitised)

Paste the relevant parts of `~/.sahara/config.toml`. **Remove any credentials, bucket names, or personal paths before posting.**

```toml
storage_mode = "..."
# ...
```

## Additional context

Any other context that might help — e.g. file types involved, approximate number of files, whether encryption is enabled.
