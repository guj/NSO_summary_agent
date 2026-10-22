"""install.py: the parts that run without a network."""
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _install():
    spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_running_python_is_used_when_new_enough():
    install = _install()
    assert install.find_python(current=(3, 12), executable="/usr/bin/python3") == "/usr/bin/python3"


def test_an_older_python_looks_for_a_newer_one_on_the_path():
    install = _install()
    found = {"python3.13": "/opt/bin/python3.13", "python3.12": "/usr/bin/python3.12"}
    versions = {"/opt/bin/python3.13": (3, 13), "/usr/bin/python3.12": (3, 12)}

    assert install.find_python(current=(3, 9), executable="/usr/bin/python3",
                               which=found.get, version_of=versions.get) == "/opt/bin/python3.13"
    # A command whose version cannot be read, or none at all: nothing to use.
    assert install.find_python(current=(3, 9), executable="/usr/bin/python3",
                               which=found.get, version_of=lambda path: None) is None


def _archive(ref: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"fabric-nso-mcp-server-{ref}/pyproject.toml", "[project]\nname = 'x'\n")
    return buffer.getvalue()


def test_without_git_the_mcp_server_archive_is_fetched_once_per_ref(tmp_path):
    install = _install()
    fetched = []

    def download(url, headers):
        fetched.append(url)
        return _archive(url.rsplit("/", 1)[1].removesuffix(".zip"))

    dest = tmp_path / "mcp-server"
    install.fetch_mcp_server("abc123", dest, download=download, git=None, environ={})
    assert (dest / "pyproject.toml").exists()
    assert fetched == ["https://github.com/fabric-testbed/fabric-nso-mcp-server/archive/abc123.zip"]

    install.fetch_mcp_server("abc123", dest, download=download, git=None, environ={})
    assert len(fetched) == 1   # already there

    install.fetch_mcp_server("def456", dest, download=download, git=None, environ={})
    assert len(fetched) == 2 and (dest / install.MARKER).read_text().strip() == "def456"


def test_a_token_downloads_the_private_repository_through_the_api(tmp_path):
    install = _install()
    seen = []

    def download(url, headers):
        seen.append((url, headers.get("Authorization")))
        return _archive("abc123")

    install.fetch_mcp_server("abc123", tmp_path / "mcp-server", download=download, git=None,
                             environ={"GITHUB_TOKEN": "ghp_x"})
    assert seen == [("https://api.github.com/repos/fabric-testbed/fabric-nso-mcp-server/zipball/abc123",
                     "Bearer ghp_x")]


def test_a_private_repository_without_git_or_token_says_what_to_do(tmp_path):
    import urllib.error

    install = _install()

    def refused(url, headers):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(SystemExit) as stopped:
        install.fetch_mcp_server("abc123", tmp_path / "mcp-server", download=refused, git=None, environ={})
    assert "private" in str(stopped.value) and "GITHUB_TOKEN" in str(stopped.value)


class FakeGit:
    """Records git commands; a clone creates its folder, and only the listed checkouts succeed."""

    def __init__(self, good_checkouts, clone_ok=True):
        self.commands, self.good, self.clone_ok = [], set(good_checkouts), clone_ok
        self.quiet = []

    def __call__(self, args, quiet=False):
        self.commands.append(args)
        if quiet:
            self.quiet.append(args)
        if args[0] == "clone":
            if not self.clone_ok:
                return 128
            Path(args[-1]).mkdir(parents=True)
            (Path(args[-1]) / ".git").mkdir()
        if "checkout" in args:
            return 0 if args[-1] in self.good else 1
        return 0


def test_git_clones_with_the_checkouts_own_access_and_pins_the_version(tmp_path):
    install = _install()
    dest = tmp_path / "mcp-server"
    git = FakeGit({"abc123", "origin/main"})
    url = "git@github.com:fabric-testbed/fabric-nso-mcp-server.git"

    install.fetch_mcp_server("abc123", dest, git=git, clone_url=url)
    assert git.commands == [
        ["clone", "--quiet", url, str(dest)],
        ["-C", str(dest), "checkout", "--quiet", "--detach", "origin/abc123"],
        ["-C", str(dest), "checkout", "--quiet", "--detach", "abc123"],
    ]
    assert (dest / install.MARKER).read_text().strip() == "abc123"
    # The branch guess that fails for a commit is not shown as an error.
    assert git.quiet == [git.commands[1]]

    git.commands.clear()
    install.fetch_mcp_server("abc123", dest, git=git, clone_url=url)
    assert git.commands == []          # that version is already checked out

    install.fetch_mcp_server("main", dest, git=git, clone_url=url)
    assert git.commands == [["-C", str(dest), "fetch", "--quiet", "--tags", "origin"],
                            ["-C", str(dest), "checkout", "--quiet", "--detach", "origin/main"]]


def test_a_version_git_cannot_find_stops_the_install(tmp_path):
    install = _install()
    with pytest.raises(SystemExit, match="nosuchref"):
        install.fetch_mcp_server("nosuchref", tmp_path / "mcp-server", git=FakeGit(set()),
                                 clone_url="https://github.com/fabric-testbed/fabric-nso-mcp-server.git")


def test_a_failed_clone_falls_back_to_a_token_or_says_what_to_do(tmp_path):
    install = _install()
    url = "https://github.com/fabric-testbed/fabric-nso-mcp-server.git"
    seen = []

    def download(url, headers):
        seen.append(url)
        return _archive("abc123")

    install.fetch_mcp_server("abc123", tmp_path / "a", download=download, git=FakeGit(set(), clone_ok=False),
                             clone_url=url, environ={"GITHUB_TOKEN": "ghp_x"})
    assert seen == ["https://api.github.com/repos/fabric-testbed/fabric-nso-mcp-server/zipball/abc123"]
    assert (tmp_path / "a" / "pyproject.toml").exists()

    with pytest.raises(SystemExit) as stopped:
        install.fetch_mcp_server("abc123", tmp_path / "b", download=download, git=FakeGit(set(), clone_ok=False),
                                 clone_url=url, environ={})
    assert "private" in str(stopped.value) and "GITHUB_TOKEN" in str(stopped.value)


def test_the_clone_url_follows_how_this_checkout_was_cloned():
    install = _install()
    ssh, https = ("git@github.com:fabric-testbed/fabric-nso-mcp-server.git",
                  "https://github.com/fabric-testbed/fabric-nso-mcp-server.git")
    assert install.mcp_clone_url("git@github.com:fabric-testbed/fabric-nso-ai-agent.git") == ssh
    assert install.mcp_clone_url("ssh://git@github.com/fabric-testbed/fabric-nso-ai-agent.git") == ssh
    assert install.mcp_clone_url("https://github.com/guj/NSO_summary_agent") == https
    assert install.mcp_clone_url(None) == https


def test_the_checkout_is_named_as_the_shell_shows_it(tmp_path):
    install = _install()
    real = tmp_path / "private" / "tmp" / "agent"
    real.mkdir(parents=True)
    (tmp_path / "tmp").symlink_to(tmp_path / "private" / "tmp")
    typed = tmp_path / "tmp" / "agent"

    assert install.shell_path(real, pwd=str(typed), cwd=str(real)) == typed
    assert install.shell_path(real, pwd=str(tmp_path), cwd=str(real)) == real


def test_starting_grafana_is_left_to_the_user_with_a_pointer(tmp_path):
    install = _install()
    assert install.monitoring_note(tmp_path) == ""          # a checkout without the stack
    (tmp_path / "deploy" / "monitoring").mkdir(parents=True)
    (tmp_path / "deploy" / "monitoring" / "README.md").write_text("")
    note = install.monitoring_note(tmp_path)
    assert "deploy/monitoring/README.md" in note and "docker compose up -d" in note


def test_env_is_created_from_the_example_only_once(tmp_path):
    install = _install()
    (tmp_path / ".env.example").write_text("NSO_ADDRESS=0.0.0.0\n")

    assert install.ensure_env_file(tmp_path) is True
    (tmp_path / ".env").write_text("NSO_ADDRESS=nso.example.org\n")
    assert install.ensure_env_file(tmp_path) is False
    assert (tmp_path / ".env").read_text() == "NSO_ADDRESS=nso.example.org\n"


def test_both_projects_go_into_one_environment(tmp_path):
    install = _install()
    venv = tmp_path / ".venv"
    python = str(venv / "bin" / "python")

    steps = install.install_commands("/usr/bin/python3.12", venv, tmp_path / "mcp-server", tmp_path)

    assert steps == [
        ["/usr/bin/python3.12", "-m", "venv", str(venv)],
        [python, "-m", "pip", "install", "--upgrade", str(tmp_path / "mcp-server")],
        [python, "-m", "pip", "install", "-e", str(tmp_path)],
    ]
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    assert install.install_commands("/usr/bin/python3.12", venv, tmp_path / "mcp-server", tmp_path)[0][0] == python


def test_the_agent_uses_the_mcp_server_next_to_its_own_python(tmp_path):
    from agent.config import _default_mcp_server_cmd

    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("")
    assert _default_mcp_server_cmd(str(python)) == "cisco-nso-mcp-server"   # not installed there: PATH
    (bin_dir / "cisco-nso-mcp-server").write_text("")
    assert _default_mcp_server_cmd(str(python)) == str(bin_dir / "cisco-nso-mcp-server")
