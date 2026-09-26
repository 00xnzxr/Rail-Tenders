"""soffice invocation: isolation, timeout, and failure surfacing.

The 4 worker replicas each run conversions, so they must not share a LibreOffice
user profile -- a shared profile takes an exclusive lock and the second process
silently exits. Each job therefore gets its own -env:UserInstallation.
"""
import subprocess

import pytest

from app.services import xlsx_preview_service as svc


def test_command_isolates_the_user_profile(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["timeout"] = kw.get("timeout")
        (tmp_path / "book.pdf").write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, 0, "converted", "")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    out = svc.convert_to_pdf(str(src), str(tmp_path), timeout_s=90)

    assert out == str(tmp_path / "book.pdf")
    assert captured["timeout"] == 90
    assert any(a.startswith("-env:UserInstallation=file://") for a in captured["cmd"])
    assert "--headless" in captured["cmd"]
    assert "--convert-to" in captured["cmd"]


def test_two_calls_use_different_profiles(monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(next(a for a in cmd if a.startswith("-env:UserInstallation=")))
        (tmp_path / "book.pdf").write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    svc.convert_to_pdf(str(src), str(tmp_path))
    svc.convert_to_pdf(str(src), str(tmp_path))

    assert seen[0] != seen[1]


def test_nonzero_exit_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        svc.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError) as exc:
        svc.convert_to_pdf(str(src), str(tmp_path))
    assert "boom" in str(exc.value)


def test_timeout_raises_rather_than_hanging_the_worker(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 180))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError) as exc:
        svc.convert_to_pdf(str(src), str(tmp_path))
    assert "timed out" in str(exc.value).lower()


def test_missing_output_raises(monkeypatch, tmp_path):
    """rc=0 but no file is still a failure -- do not return a phantom path."""
    monkeypatch.setattr(
        svc.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError):
        svc.convert_to_pdf(str(src), str(tmp_path))


def _profile_from_cmd(cmd):
    """Pull the profile dir out of the -env:UserInstallation=file://<path> arg."""
    arg = next(a for a in cmd if a.startswith("-env:UserInstallation=file://"))
    return arg[len("-env:UserInstallation=file://"):]


def _patch_rmtree_recorder(monkeypatch):
    removed = []
    monkeypatch.setattr(
        svc.shutil, "rmtree",
        lambda path, ignore_errors=False: removed.append(path),
    )
    return removed


def test_profile_cleaned_up_on_success(monkeypatch, tmp_path):
    captured = {}
    removed = _patch_rmtree_recorder(monkeypatch)

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        (tmp_path / "book.pdf").write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    svc.convert_to_pdf(str(src), str(tmp_path))

    assert removed == [_profile_from_cmd(captured["cmd"])]


def test_profile_cleaned_up_on_nonzero_exit(monkeypatch, tmp_path):
    captured = {}
    removed = _patch_rmtree_recorder(monkeypatch)

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError):
        svc.convert_to_pdf(str(src), str(tmp_path))

    assert removed == [_profile_from_cmd(captured["cmd"])]


def test_profile_cleaned_up_on_timeout(monkeypatch, tmp_path):
    captured = {}
    removed = _patch_rmtree_recorder(monkeypatch)

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 180))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError):
        svc.convert_to_pdf(str(src), str(tmp_path))

    assert removed == [_profile_from_cmd(captured["cmd"])]


def test_profile_cleaned_up_on_missing_binary(monkeypatch, tmp_path):
    captured = {}
    removed = _patch_rmtree_recorder(monkeypatch)

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        raise FileNotFoundError("soffice not found")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError):
        svc.convert_to_pdf(str(src), str(tmp_path))

    assert removed == [_profile_from_cmd(captured["cmd"])]


def test_profile_cleaned_up_on_missing_output(monkeypatch, tmp_path):
    captured = {}
    removed = _patch_rmtree_recorder(monkeypatch)

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError):
        svc.convert_to_pdf(str(src), str(tmp_path))

    assert removed == [_profile_from_cmd(captured["cmd"])]
