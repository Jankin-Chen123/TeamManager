"""Pull a successful main build on the server; no inbound deployment credentials."""

import argparse
import json
import re
import subprocess
import urllib.request
from pathlib import Path

REPOSITORY = "Jankin-Chen123/TeamManager"
ROOT = Path(__file__).resolve().parents[1]


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "TeamManager-deploy"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Check only; do not pull or deploy")
    args = parser.parse_args()
    url = (
        f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/ci.yml/runs"
        "?branch=main&event=push&status=success&per_page=1"
    )
    runs = json.loads(download(url))["workflow_runs"]
    if not runs:
        print("No successful main build yet", flush=True)
        return
    run = runs[0]
    revision = run["head_sha"]
    assert re.fullmatch(r"[a-f0-9]{40}", revision)
    assert run["head_repository"]["full_name"].lower() == REPOSITORY.lower()
    assert run["head_branch"] == "main" and run["conclusion"] == "success"
    state = ROOT / ".deployment"
    deployed = state / "revision"
    current = deployed.read_text().strip() if deployed.exists() else ""
    print(f"Successful build: {revision}; deployed: {current or 'unknown'}", flush=True)
    if args.check or revision == current:
        return
    attempt = state / "attempt"
    if attempt.exists() and attempt.read_text().strip() == revision:
        raise RuntimeError("Previous attempt failed; inspect service logs before removing .deployment/attempt")

    # Fetch deployment files from the same tested commit, before changing the service.
    paths = ("compose.yaml", "scripts/deploy.sh", "scripts/update.sh", "scripts/pull_deploy.py")
    files = {
        path: download(f"https://raw.githubusercontent.com/{REPOSITORY}/{revision}/{path}")
        for path in paths
    }
    image = f"ghcr.io/{REPOSITORY.lower()}:sha-{revision[:7]}"
    subprocess.run(["docker", "pull", image], check=True)
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    assert inspected["Config"]["Labels"]["org.opencontainers.image.revision"] == revision
    digest = next(
        value for value in inspected["RepoDigests"]
        if value.startswith(f"ghcr.io/{REPOSITORY.lower()}@sha256:")
    )
    state.mkdir(mode=0o700, exist_ok=True)
    attempt.write_text(revision + "\n")
    for path, data in files.items():
        target = ROOT / path
        staged = target.with_name(target.name + ".pending")
        staged.write_bytes(data.replace(b"\r\n", b"\n"))
        staged.replace(target)
    subprocess.run(["sh", "scripts/deploy.sh", digest], cwd=ROOT, check=True)
    deployed.write_text(revision + "\n")
    attempt.unlink()
    print(f"Deployment healthy: {revision} ({run['html_url']})", flush=True)


if __name__ == "__main__":
    main()
