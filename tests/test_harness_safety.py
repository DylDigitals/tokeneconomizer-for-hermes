"""The real-session harness only ever writes inside this checkout's sandbox/ folder."""
import os
import shutil
import subprocess
import sys

HARNESS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness")
FAKE = {"TE_TEST_BASE_URL": "http://127.0.0.1:9/v1", "TE_TEST_MODEL": "m", "TE_TEST_API_KEY": "fake-key-0123456789"}


def checkout(tmp_path, *scripts, name="w"):
    """A copy of the named harness scripts in a throwaway checkout, plus a folder beside it."""
    w = tmp_path / name
    (w / "harness").mkdir(parents=True)
    (w / "sandbox").mkdir()
    for s in scripts:
        shutil.copy2(os.path.join(HARNESS, s), w / "harness" / s)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "marker").write_text("keep")
    return w, victim


def run(cmd, **env):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), **env})


def test_new_root_refuses_a_name_that_leaves_the_sandbox(tmp_path):
    w, victim = checkout(tmp_path, "new-root")
    for name in ("../../victim", "..", "a/b", ""):
        p = run([str(w / "harness" / "new-root"), name, "--no-plugin"], **FAKE)
        assert p.returncode != 0, name
    assert (victim / "marker").read_text() == "keep"
    assert not list((w / "sandbox").iterdir())


def test_new_root_checks_the_test_model_before_deleting_anything(tmp_path):
    w, _ = checkout(tmp_path, "new-root")
    old = w / "sandbox" / "r1"
    old.mkdir()
    (old / "keep").write_text("x")
    p = run([str(w / "harness" / "new-root"), "r1", "--no-plugin"])
    assert p.returncode != 0 and "TE_TEST_BASE_URL" in p.stderr
    assert (old / "keep").exists()


def test_hx_refuses_roots_that_resolve_outside_the_sandbox(tmp_path):
    for script in ("hx", "hxpy"):
        w, victim = checkout(tmp_path / script, script)
        (w / "sandbox" / "link").symlink_to(victim)
        for root in (f"{w}/sandbox/../../victim", f"{w}/sandbox/link/r"):
            p = run([str(w / "harness" / script), "--version"], HX_ROOT=root,
                    HERMES_SRC=str(tmp_path / "no-hermes"))
            assert p.returncode == 2 and "outside sandbox" in p.stderr, (script, root, p.stderr)
        assert sorted(x.name for x in victim.iterdir()) == ["marker"]


def test_acceptance_refuses_a_checkout_path_the_shell_would_split(tmp_path):
    w, _ = checkout(tmp_path, "acceptance.py", name="my checkout")
    p = run([sys.executable, str(w / "harness" / "acceptance.py"), "install"])
    assert p.returncode == 2 and "checkout path" in p.stderr, p.stdout[-300:] + p.stderr[-300:]


def test_repomap_python_setting_reaches_the_shell_as_one_word(monkeypatch, tmp_path):
    import importlib.util
    import shlex
    spec = importlib.util.spec_from_file_location("acceptance_probe", os.path.join(HARNESS, "acceptance.py"))
    acc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acc)
    hostile = f"/opt/my py/bin/python; touch {tmp_path}/marker #"
    monkeypatch.setenv("TE_REPOMAP_PYTHON", hostile)
    seen = []

    class Stop(Exception):
        pass

    def te(r, args, cwd=None):
        seen.append(args)
        raise Stop
    monkeypatch.setattr(acc, "root", lambda name, plugin=True: str(tmp_path))
    monkeypatch.setattr(acc, "te", te)
    try:
        acc.a7_repomap()
    except Stop:
        pass
    assert f"repomap_python={hostile}" in shlex.split(seen[0])


def test_hxpy_runs_the_hermes_python_from_a_source_dir_with_spaces(tmp_path):
    w, _ = checkout(tmp_path, "hxpy")
    src = tmp_path / "my hermes"
    (src / "venv" / "bin").mkdir(parents=True)
    py = src / "venv" / "bin" / "python"
    py.write_text("#!/bin/sh\necho stub-ran \"$@\"\n")
    py.chmod(0o755)
    p = run([str(w / "harness" / "hxpy"), "x"], HX_ROOT=f"{w}/sandbox/r", HERMES_SRC=str(src))
    assert p.returncode == 0 and "stub-ran x" in p.stdout, (p.returncode, p.stderr[-300:])
