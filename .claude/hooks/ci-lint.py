import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

payload = json.load(sys.stdin)
file_path = payload.get("tool_input", {}).get("file_path", "")
if not file_path.endswith(".gitlab-ci.yml"):
    sys.exit(0)

content = Path(file_path).read_text()
gitlab_url = os.environ.get("GITLAB_URL", "")
token = os.environ.get("GITLAB_TOKEN", "")

if not token:
    print("[ci-lint] GITLAB_TOKEN not set — lint skipped", file=sys.stderr)
    sys.exit(0)
if not gitlab_url.startswith("https://"):
    print(f"[ci-lint] GITLAB_URL must be https:// — lint skipped", file=sys.stderr)
    sys.exit(0)

api_url = f"{gitlab_url}/api/v4/projects/42/ci/lint"

body = json.dumps({"content": content}).encode()
req = urllib.request.Request(
    api_url,
    data=body,
    headers={"Content-Type": "application/json", "PRIVATE-TOKEN": token},
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.load(resp)
except urllib.error.HTTPError as e:
    print(f"[ci-lint] API error {e.code} — lint skipped", file=sys.stderr)
    sys.exit(0)
except Exception as e:
    print(f"[ci-lint] unreachable ({e}) — lint skipped", file=sys.stderr)
    sys.exit(0)

if not result.get("valid", True):
    errors = result.get("errors", ["unknown error"])
    print(f"[ci-lint] INVALID .gitlab-ci.yml: {'; '.join(errors)}", file=sys.stderr)
    print(
        "Fix YAML before saving. Common cause: multi-line python -c without heredoc.",
        file=sys.stderr,
    )
    sys.exit(2)

jobs = len(result.get("jobs", []))
print(f"[ci-lint] valid — {jobs} jobs detected")
