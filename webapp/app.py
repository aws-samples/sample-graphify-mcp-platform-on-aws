"""Local setup console for aws-graphify-mcp-platform.

A localhost-only web app that drives the whole initial setup with the AWS
credentials already configured on this machine (profiles / env chain):

  environment check -> runtime packaging -> CDK bootstrap/deploy ->
  repo registration -> registry status / smoke test -> MCP client config.

Run:  uv run python webapp/app.py        (opens http://127.0.0.1:8787)

Security model: binds 127.0.0.1 only and rejects any request whose Host
header is not localhost (DNS-rebinding guard). It intentionally has no auth —
it wields the same local credentials the CLI already would.
"""

from __future__ import annotations

import itertools
import os
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import boto3
import botocore.exceptions
import botocore.session
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(ROOT / "scripts"))

from common import STACK_NAME, mcp_server_entry  # noqa: E402

PORT = int(os.environ.get("SETUP_PORT", "8787"))
DEFAULT_REGION = "ap-northeast-2"
RUNTIME_NAME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,47}")
CDK = ["npx", "-y", "aws-cdk@latest"]

app = FastAPI()


_ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


@app.middleware("http")
async def host_guard(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost", "[::1]"):
        return JSONResponse({"error": "forbidden host"}, status_code=403)
    if request.method == "POST":
        # CSRF guard: a malicious web page can fire a no-preflight POST at
        # 127.0.0.1. Browser requests carry Origin — reject foreign ones —
        # and the console's own JS sends a custom header that plain HTML
        # forms cannot. curl/scripts (no Origin) stay unaffected.
        origin = request.headers.get("origin", "")
        if origin and origin not in _ALLOWED_ORIGINS:
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        if origin and request.headers.get("x-setup-console") != "1":
            return JSONResponse({"error": "missing console header"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Job runner: one mutating subprocess at a time, log lines polled by the UI.
# ---------------------------------------------------------------------------
_job_seq = itertools.count(1)
_jobs: dict[int, dict] = {}
_job_lock = threading.Lock()


def _reader(job: dict, proc: subprocess.Popen) -> None:
    # Every exit path must leave a terminal status, or the single-job lock
    # blocks all future jobs until the server restarts.
    try:
        for line in proc.stdout:
            job["lines"].append(line.rstrip("\n"))
        job["rc"] = proc.wait()
    except Exception as exc:
        job["lines"].append(f"[reader error] {exc}")
        try:
            job["rc"] = proc.wait(timeout=5)
        except Exception:
            proc.kill()
            job["rc"] = -1
    finally:
        job["status"] = "succeeded" if job["rc"] == 0 else "failed"


def start_job(kind: str, cmd: list[str], env_extra: dict[str, str]) -> dict:
    with _job_lock:
        if any(j["status"] == "running" for j in _jobs.values()):
            raise RuntimeError("다른 작업이 실행 중입니다. 완료 후 다시 시도하세요.")
        job_id = next(_job_seq)
        job = {"id": job_id, "kind": kind, "status": "running", "rc": None,
               "lines": [f"$ {' '.join(cmd)}"]}
        _jobs[job_id] = job
    env = {**os.environ, **{k: v for k, v in env_extra.items() if v}}
    # A chosen profile must fully own credential resolution.
    if "AWS_PROFILE" in env_extra and env_extra["AWS_PROFILE"]:
        for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            env.pop(k, None)
    try:
        proc = subprocess.Popen(
            cmd, cwd=ROOT, env=env, text=True, errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        # e.g. npx missing — must not leave the job stuck in "running".
        job["lines"].append(f"failed to start: {exc}")
        job["rc"] = -1
        job["status"] = "failed"
        return job
    threading.Thread(target=_reader, args=(job, proc), daemon=True).start()
    return job


def aws_env(profile: str, region: str) -> dict[str, str]:
    region = region or DEFAULT_REGION
    return {
        "AWS_PROFILE": profile,
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "CDK_DEFAULT_REGION": region,
    }


def session(profile: str, region: str) -> boto3.Session:
    return boto3.Session(profile_name=profile or None, region_name=region or DEFAULT_REGION)


def _tool_version(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip().splitlines()[0]
    except Exception:
        return ""


def stack_info(sess: boto3.Session) -> dict:
    cfn = sess.client("cloudformation")
    try:
        st = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
        return {
            "status": st["StackStatus"],
            "outputs": {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])},
        }
    except botocore.exceptions.ClientError:
        return {"status": "NOT_DEPLOYED", "outputs": {}}


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/profiles")
def profiles():
    return {"profiles": botocore.session.Session().available_profiles,
            "env_profile": os.environ.get("AWS_PROFILE", ""),
            "env_credentials": bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"))}


@app.get("/api/env")
def env_check(profile: str = "", region: str = ""):
    region = region or DEFAULT_REGION
    out: dict = {"region": region, "profile": profile}
    try:
        sess = session(profile, region)
        ident = sess.client("sts").get_caller_identity()
        out["identity"] = {"account": ident["Account"], "arn": ident["Arn"]}
    except Exception as exc:
        out["identity_error"] = str(exc)
        return out
    cfn = sess.client("cloudformation")
    try:
        cfn.describe_stacks(StackName="CDKToolkit")
        out["bootstrapped"] = True
    except botocore.exceptions.ClientError:
        out["bootstrapped"] = False
    out["stack"] = stack_info(sess)
    out["tools"] = {
        "node": _tool_version(["node", "--version"]),
        "uv": _tool_version(["uv", "--version"]),
    }
    out["runtime_pkg"] = (ROOT / "dist" / "runtime_pkg" / "entrypoint.py").exists()
    return out


@app.get("/api/repos")
def repos(profile: str = "", region: str = ""):
    try:
        return _repos(profile, region)
    except Exception as exc:
        return JSONResponse({"deployed": False, "repos": [], "error": str(exc)}, status_code=200)


def _repos(profile: str, region: str):
    sess = session(profile, region)
    info = stack_info(sess)
    table_name = info["outputs"].get("RepoRegistryTable")
    if not table_name:
        return {"deployed": False, "repos": []}
    table = sess.resource("dynamodb").Table(table_name)
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    rows = [
        {
            "repo_id": i.get("repo_id"),
            "git_url": i.get("git_url"),
            "provider": i.get("provider"),
            "ref": i.get("ref"),
            "status": i.get("status"),
            "last_built_sha": str(i.get("last_built_sha", ""))[:12],
            "graph_bytes": int(i.get("graph_bytes", 0) or 0),
            "updated_at": i.get("updated_at", ""),
            "last_error": str(i.get("last_error", ""))[:200],
            "runtime_id": i.get("runtime_id", ""),
        }
        for i in sorted(items, key=lambda x: x.get("repo_id", ""))
    ]
    return {"deployed": True, "repos": rows, "outputs": info["outputs"]}


@app.get("/api/webhook-info")
def webhook_info(profile: str = "", region: str = ""):
    """Webhook endpoint + HMAC secret for owned-repo push triggers.

    Local console only — the operator needs the secret to paste into the
    GitHub webhook form; it never leaves this machine otherwise.
    """
    region = region or DEFAULT_REGION
    try:
        sess = session(profile, region)
        outputs = stack_info(sess)["outputs"]
        url, secret_arn = outputs.get("WebhookUrl"), outputs.get("WebhookSecretArn")
        if not url or not secret_arn:
            return JSONResponse({"error": "스택에 Webhook 출력이 없습니다 — 먼저 배포하세요."}, status_code=404)
        secret = sess.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
        return {"url": url, "secret": secret}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/mcp-config")
def mcp_config(profile: str = "", region: str = ""):
    """Combined config: hub (merged all-repos graph) + every dedicated runtime."""
    region = region or DEFAULT_REGION
    try:
        sess = session(profile, region)
        info = stack_info(sess)
        arn = info["outputs"].get("RuntimeArn")
        if not arn:
            return JSONResponse({"error": "스택이 아직 배포되지 않았습니다."}, status_code=404)
        servers = {"graphify-all": mcp_server_entry(arn, region, profile)}
        table_name = info["outputs"].get("RepoRegistryTable")
        if table_name:
            table = sess.resource("dynamodb").Table(table_name)
            kwargs: dict = {}
            while True:
                page = table.scan(**kwargs)
                for item in page.get("Items", []):
                    if item.get("enabled") == "1" and item.get("runtime_arn"):
                        servers[item["repo_id"]] = mcp_server_entry(item["runtime_arn"], region, profile)
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return {"runtime_arn": arn, "config": {"mcpServers": servers}}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(request: Request):
    body = await request.json()
    kind = body.get("kind", "")
    profile = body.get("profile", "")
    region = body.get("region", "") or DEFAULT_REGION
    opts = body.get("options", {}) or {}
    env = aws_env(profile, region)

    if kind == "package":
        cmd = [sys.executable, "scripts/package_runtime.py"]
    elif kind == "bootstrap":
        try:
            account = session(profile, region).client("sts").get_caller_identity()["Account"]
        except Exception as exc:
            return JSONResponse({"error": f"자격증명 확인 실패 / credential check failed: {exc}"}, status_code=400)
        cmd = [*CDK, "bootstrap", f"aws://{account}/{region}"]
    elif kind == "deploy":
        cmd = [*CDK, "deploy", "--require-approval", "never", "--progress", "events"]
        runtime_name = (opts.get("runtime_name") or "").strip()
        if runtime_name and not RUNTIME_NAME_RE.fullmatch(runtime_name):
            return JSONResponse({"error": "runtime_name은 [a-zA-Z][a-zA-Z0-9_]{0,47} 이어야 합니다 (하이픈 불가)."}, status_code=400)
        for key in ("runtime_name", "default_repo_id", "github_token_secret_arn"):
            if (opts.get(key) or "").strip():
                cmd += ["-c", f"{key}={opts[key].strip()}"]
    elif kind == "register":
        url = (opts.get("url") or "").strip()
        if not url.startswith("https://"):
            return JSONResponse({"error": "https:// git URL이 필요합니다."}, status_code=400)
        cmd = [sys.executable, "scripts/register_repo.py", "--url", url, "--region", region]
        if (opts.get("ref") or "").strip():
            cmd += ["--ref", opts["ref"].strip()]
        if (opts.get("provider") or "auto") != "auto":
            cmd += ["--provider", opts["provider"]]
        if opts.get("trigger") == "webhook":
            cmd += ["--trigger", "webhook"]
        if (opts.get("auth_secret") or "").strip():
            cmd += ["--auth-secret", opts["auth_secret"].strip()]
        if opts.get("poll_interval"):
            try:
                interval = max(60, int(float(str(opts["poll_interval"]))))
            except (TypeError, ValueError):
                return JSONResponse({"error": "poll_interval must be a number of seconds"}, status_code=400)
            cmd += ["--poll-interval", str(interval)]
        if opts.get("force"):
            cmd += ["--force"]
    elif kind == "smoke":
        cmd = [sys.executable, "scripts/smoke_test.py", "--region", region]
        if (opts.get("repo_id") or "").strip():
            cmd += ["--repo-id", opts["repo_id"].strip()]
    elif kind == "destroy":
        if opts.get("confirm") != STACK_NAME:
            return JSONResponse({"error": f"확인 문자열이 스택 이름({STACK_NAME})과 일치해야 합니다."}, status_code=400)
        cmd = [*CDK, "destroy", "--force"]
    else:
        return JSONResponse({"error": f"unknown job kind: {kind}"}, status_code=400)

    try:
        job = start_job(kind, cmd, env)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return {"id": job["id"], "kind": kind}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: int, after: int = 0):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    lines = job["lines"]
    return {"id": job["id"], "kind": job["kind"], "status": job["status"],
            "rc": job["rc"], "next": len(lines), "lines": lines[after:]}


def main() -> None:
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"aws-graphify-mcp-platform setup console: http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
