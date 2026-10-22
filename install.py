#!/usr/bin/env python3
"""Set up this checkout on a new machine: the NSO MCP server and the agent in one .venv.

    python3 install.py

The MCP server repository is private, so it is cloned with git, using the same GitHub
access you used for this repository. Without git, or when the clone fails, it is
downloaded through GitHub's API with a personal access token in GITHUB_TOKEN. Re-running is safe:
an existing .env is kept, and the MCP server changes only when the version asked for does.

This file runs under any python3 long enough to find a 3.12 or newer, so it keeps to
Python 3.6 syntax.
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

MIN_PYTHON = (3, 12)
MCP_OWNER_REPO = "fabric-testbed/fabric-nso-mcp-server"
MCP_REPO = "https://github.com/" + MCP_OWNER_REPO
# The MCP server version this agent was tested with: main after pull request #11.
MCP_REF = "80dfd263c66af2f47d693b279861d0e845cfc280"
MARKER = ".installed-ref"
CANDIDATES = ("python3.14", "python3.13", "python3.12")

PYTHON_HINT = """This needs Python 3.12 or newer. Found {found}, and no python3.12 or newer on PATH.
Install one next to the system Python, then run this again with it:
  macOS:                the installer from https://www.python.org/downloads/
  Ubuntu or Debian:     sudo apt install python3.12 python3.12-venv
  RHEL, Rocky, Alma 9:  sudo dnf install python3.12"""

PRIVATE_HINT = """Could not get the MCP server ({error}). The fabric-testbed repositories are private:
  - with git installed and able to reach GitHub as you, the server is cloned the same way
    you got this repository; or
  - create a GitHub personal access token that can read {repo},
    then run:  GITHUB_TOKEN=<token> python3 install.py"""


def shell_path(path, pwd=None, cwd=None):
    """``path`` named through the shell's current folder ($PWD) when it lies under it.

    On macOS /tmp is a link to /private/tmp, and Python reports the /private form; the
    shell keeps the name that was typed. A $PWD that is not the current folder is ignored.
    """
    pwd = os.environ.get("PWD") if pwd is None else pwd
    cwd = os.getcwd() if cwd is None else cwd
    if not pwd or not os.path.isabs(pwd) or os.path.realpath(pwd) != os.path.realpath(cwd):
        return Path(path)
    try:
        inside = Path(os.path.realpath(str(path))).relative_to(os.path.realpath(pwd))
    except ValueError:
        return Path(path)
    return Path(pwd) / inside if inside.parts else Path(pwd)


REPO = shell_path(Path(os.path.abspath(__file__)).parent)


def _version_of(path):
    """(major, minor) of the Python at ``path``, or None when it cannot be run."""
    try:
        out = subprocess.run(
            [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            stdout=subprocess.PIPE, universal_newlines=True, timeout=30, check=True,
        ).stdout
        major, minor = out.strip().split(".")
        return int(major), int(minor)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def find_python(current=None, executable=None, which=shutil.which, version_of=_version_of):
    """This Python when it is new enough, else the newest python3.1x on PATH, else None."""
    current = tuple(sys.version_info[:2]) if current is None else tuple(current)
    if current >= MIN_PYTHON:
        return executable or sys.executable
    for name in CANDIDATES:
        path = which(name)
        version = version_of(path) if path else None
        if version and tuple(version) >= MIN_PYTHON:
            return path
    return None


def archive_url(ref):
    return "{}/archive/{}.zip".format(MCP_REPO, ref)


def mcp_clone_url(origin):
    """Clone the MCP server the way this checkout was cloned: SSH stays SSH, anything else HTTPS."""
    if origin and (origin.startswith("git@github.com:") or origin.startswith("ssh://git@github.com/")):
        return "git@github.com:{}.git".format(MCP_OWNER_REPO)
    return MCP_REPO + ".git"


def _git(args, quiet=False):
    return subprocess.run(["git"] + list(args), stderr=subprocess.DEVNULL if quiet else None).returncode


def _git_works():
    """True when git runs; macOS has a /usr/bin/git that only offers to install the tools."""
    try:
        return subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def _origin_url(repo):
    try:
        result = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _download(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
        return response.read()


def _checkout(git, dest, ref):
    # A branch is checked out as the remote's copy, so a re-run picks up its latest commit.
    # For a commit or tag that guess fails, so its error is not shown.
    if git(["-C", str(dest), "checkout", "--quiet", "--detach", "origin/" + ref], quiet=True) == 0:
        return
    if git(["-C", str(dest), "checkout", "--quiet", "--detach", ref]) == 0:
        return
    sys.exit("The MCP server has no version {!r}; check --mcp-ref.".format(ref))


def fetch_mcp_server(ref, dest, download=_download, git=None, clone_url=None, environ=None):
    """Put the MCP server at ``ref`` into ``dest``, unless that version is already there.

    With ``git``, clone (or fetch) and check out ``ref``; without, unpack its ZIP archive.
    Only a folder this script created (it holds the marker file) is ever changed.
    """
    dest = Path(dest)
    marker = dest / MARKER
    environ = os.environ if environ is None else environ
    if dest.exists():
        if not marker.exists():
            sys.exit("{} exists but was not created by install.py. Move it away, or set "
                     "MCP_SERVER_CMD in .env to use that installation instead.".format(dest))
        if marker.read_text().strip() == ref:
            print("MCP server {} is already in {}".format(ref[:12], dest))
            return
    if git is not None:
        if (dest / ".git").exists():
            print("Updating the MCP server in {} to {} ...".format(dest, ref[:12]))
            git(["-C", str(dest), "fetch", "--quiet", "--tags", "origin"])
        else:
            if dest.exists():
                shutil.rmtree(str(dest))        # an earlier archive install of ours
            url = clone_url or mcp_clone_url(None)
            print("Cloning the MCP server from {} ...".format(url))
            if git(["clone", "--quiet", url, str(dest)]) != 0:
                if not environ.get("GITHUB_TOKEN"):
                    sys.exit(PRIVATE_HINT.format(error="git could not clone " + url, repo=MCP_OWNER_REPO))
                print("git could not clone it; using GITHUB_TOKEN instead.")
                _fetch_archive(ref, dest, download, environ)
                return
        _checkout(git, dest, ref)
        marker.write_text(ref + "\n")
        return
    _fetch_archive(ref, dest, download, environ)


def _fetch_archive(ref, dest, download, environ):
    token = environ.get("GITHUB_TOKEN")
    if token:
        url = "https://api.github.com/repos/{}/zipball/{}".format(MCP_OWNER_REPO, ref)
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    else:
        url, headers = archive_url(ref), {}
    print("Downloading the MCP server {} ...".format(ref[:12]))
    try:
        data = download(url, headers)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            sys.exit(PRIVATE_HINT.format(error="HTTP {} from GitHub".format(exc.code), repo=MCP_OWNER_REPO))
        raise
    with tempfile.TemporaryDirectory(dir=str(dest.parent)) as tmp:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(tmp)
        tops = [path for path in Path(tmp).iterdir() if path.is_dir()]
        if len(tops) != 1:
            sys.exit("Unexpected archive layout from {}".format(url))
        (tops[0] / MARKER).write_text(ref + "\n")
        if dest.exists():
            shutil.rmtree(str(dest))
        shutil.move(str(tops[0]), str(dest))


def venv_python(venv):
    venv = Path(venv)
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def install_commands(python, venv, mcp_dir, repo):
    """The commands that create the environment and install both projects into it."""
    target = venv_python(venv)
    steps = []
    if not target.exists():
        steps.append([python, "-m", "venv", str(venv)])
    steps.append([str(target), "-m", "pip", "install", "--upgrade", str(mcp_dir)])
    steps.append([str(target), "-m", "pip", "install", "-e", str(repo)])
    return steps


def ensure_env_file(repo):
    """Copy .env.example to .env, readable by its owner only; True when it was created."""
    env = Path(repo) / ".env"
    if env.exists():
        return False
    shutil.copyfile(str(Path(repo) / ".env.example"), str(env))
    os.chmod(str(env), 0o600)
    return True


def monitoring_note(repo):
    """How to start the optional Grafana stack, when this checkout has it; this script does not."""
    if not (Path(repo) / "deploy" / "monitoring" / "README.md").exists():
        return ""
    return """
Grafana dashboards (optional): this script does not start Pushgateway, Prometheus or
Grafana. They run in Docker; with Docker installed, start them with
  cd deploy/monitoring && docker compose up -d
as described in deploy/monitoring/README.md, then set PROMETHEUS_PUSHGATEWAY_URL
(the Pushgateway question in --configure). Without them, scans run and report as usual."""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mcp-ref", default=MCP_REF,
                        help="MCP server commit, tag or branch to install "
                             "(default: the version this agent was tested with)")
    args = parser.parse_args(argv)

    python = find_python()
    if python is None:
        sys.exit(PYTHON_HINT.format(found="Python %d.%d" % sys.version_info[:2]))
    if python != sys.executable:
        print("Running again with {} ...".format(python))
        os.execv(python, [python, str(REPO / Path(__file__).name)] +
                 list(sys.argv[1:] if argv is None else argv))

    mcp_dir = REPO / "mcp-server"
    venv = REPO / ".venv"
    use_git = _git_works()
    fetch_mcp_server(args.mcp_ref, mcp_dir, git=_git if use_git else None,
                     clone_url=mcp_clone_url(_origin_url(REPO) if use_git else None))
    for step in install_commands(python, venv, mcp_dir, REPO):
        print("+ " + " ".join(step))
        try:
            subprocess.run(step, check=True)
        except subprocess.CalledProcessError:
            hint = (" On Ubuntu or Debian, install the venv package first, "
                    "for example: sudo apt install python3.12-venv") if step[1:3] == ["-m", "venv"] else ""
            sys.exit("That step failed; see the output above." + hint)
    ensure_env_file(REPO)

    command = os.path.relpath(str(venv_python(venv).parent / "nso-diagnostic-run"), str(REPO))
    if sys.stdin.isatty():
        print("\nInstalled. Now the settings, written into .env:")
        subprocess.run([str(venv_python(venv)), "-m", "diagnostic_mas.run", "--configure"],
                       cwd=str(REPO), env=dict(os.environ, PWD=str(REPO)))
        print("""
To change settings later:  {cmd} --configure
A first scan without the LLM; nothing is saved or sent:
  {cmd} --skip-llm --dry-run
Run these from {repo}.""".format(cmd=command, repo=REPO))
    else:
        print("""
Installed. Next, from {repo}:
  1. Fill in the settings, answering a few questions:
       {cmd} --configure
     (or edit .env directly: NSO_ADDRESS, NSO_USERNAME, NSO_PASSWORD at least)
  2. Check the connection:
       {cmd} --check-connection""".format(cmd=command, repo=REPO))
    note = monitoring_note(REPO)
    if note:
        print(note)


if __name__ == "__main__":
    main()
