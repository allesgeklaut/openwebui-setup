"""Render environment placeholders inside JSON strings without corrupting JSON."""

import json
import os
from pathlib import Path
import string
import sys


def render(value, environment):
    if isinstance(value, dict):
        return {key: render(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, environment) for item in value]
    if isinstance(value, str):
        return string.Template(value).substitute(environment)
    return value


if __name__ == "__main__":
    try:
        template = json.loads(Path(sys.argv[1]).read_text())
        result = render(template, os.environ)
    except KeyError as error:
        sys.exit(f"Missing template environment variable: {error.args[0]}")
    except (OSError, ValueError) as error:
        sys.exit(f"Cannot render MCP config: {error}")
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
