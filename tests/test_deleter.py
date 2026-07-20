"""Tests for path guards and dry-run deletion (no real files removed)."""

from __future__ import annotations

import os

from ultimate_sacrifice.cleanup.deleter import delete_path, is_drive_root, is_guarded


def test_drive_root_is_guarded():
    assert is_drive_root("C:\\")
    guarded, _ = is_guarded("C:\\")
    assert guarded


def test_system_paths_guarded():
    for p in (r"C:\Windows", r"C:\Windows\System32", r"C:\Program Files\x", r"C:\ProgramData\y"):
        guarded, reason = is_guarded(p)
        assert guarded, p
        assert reason


def test_system_paths_guarded_on_any_drive():
    # Guards must apply drive-relative, not only on C: (issue #6).
    for p in (
        r"D:\Windows",
        r"D:\Windows\System32",
        r"E:\Program Files\app",
        r"D:\Program Files (x86)\x",
        r"F:\ProgramData\y",
        r"D:\$Recycle.Bin",
        r"G:\System Volume Information",
    ):
        guarded, reason = is_guarded(p)
        assert guarded, p
        assert reason


def test_non_system_dir_on_other_drive_not_guarded(tmp_path):
    # A normal top-level dir on a non-C drive is NOT guarded just for being top-level.
    # (Use a fake path; is_guarded's container check only triggers for existing dirs.)
    guarded, _ = is_guarded(r"D:\Projects\myrepo\node_modules")
    assert not guarded  # node_modules is recognized junk
    guarded, _ = is_guarded(r"D:\Downloads\big.iso")
    assert not guarded  # a loose file is not a system dir


def test_normal_path_not_guarded(tmp_path):
    guarded, _ = is_guarded(str(tmp_path / "junk"))
    assert not guarded


def test_os_swap_and_hibernation_files_guarded():
    # Guarded by basename regardless of drive/location.
    for p in (r"C:\pagefile.sys", r"D:\hiberfil.sys", r"C:\swapfile.sys"):
        guarded, reason = is_guarded(p)
        assert guarded, p
        assert "swap" in reason or "hibernation" in reason


def test_virtual_disk_images_guarded():
    for p in (
        r"C:\Users\me\AppData\Local\wsl\{abc}\ext4.vhdx",
        r"D:\VMs\win11.vmdk",
        r"E:\vbox\disk.vdi",
        r"C:\hyperv\snap.avhdx",
    ):
        guarded, reason = is_guarded(p)
        assert guarded, p
        assert "virtual-disk" in reason


def test_normal_file_extension_not_guarded(tmp_path):
    # A regular large file (not a VM image / swap file) is still deletable.
    guarded, _ = is_guarded(str(tmp_path / "movie.iso"))
    assert not guarded
    guarded, _ = is_guarded(str(tmp_path / "installer.exe"))
    assert not guarded


def test_container_directory_is_guarded(tmp_path, monkeypatch):
    # A real directory that isn't recognized junk is a container -> guarded.
    # pytest's tmp_path lives under %TEMP%, so neutralize the temp-path signal
    # (which would otherwise correctly classify it as deletable temp, not a container).
    import ultimate_sacrifice.scanner.heuristics as h

    monkeypatch.setattr(h, "is_temp_path", lambda p: False)
    d = tmp_path / "MyStuff"
    (d / "sub").mkdir(parents=True)
    guarded, reason = is_guarded(str(d))
    assert guarded
    assert "container" in reason


def test_recognized_junk_dirs_not_guarded(tmp_path):
    # node_modules and Temp are recognized-disposable, so they stay deletable.
    nm = tmp_path / "proj" / "node_modules"
    nm.mkdir(parents=True)
    guarded, _ = is_guarded(str(nm))
    assert not guarded

    tmp = tmp_path / "Temp"
    tmp.mkdir()
    guarded, _ = is_guarded(str(tmp))
    assert not guarded


def test_junk_file_not_guarded(tmp_path):
    # Loose files are never container-guarded regardless of extension.
    f = tmp_path / "movie.iso"
    f.write_bytes(b"\0" * 16)
    guarded, _ = is_guarded(str(f))
    assert not guarded


def test_guarded_path_refused_by_delete():
    res = delete_path(r"C:\Windows", use_recycle_bin=True, dry_run=True)
    assert not res.ok
    assert res.method == "skipped"


def test_dry_run_does_not_delete(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"\0" * 2048)
    res = delete_path(str(f), use_recycle_bin=True, dry_run=True)
    assert res.ok
    assert res.method == "dry-run"
    assert res.freed_bytes == 2048
    assert f.exists()  # still there


def test_permanent_delete_removes_file(tmp_path):
    f = tmp_path / "b.bin"
    f.write_bytes(b"\0" * 1024)
    res = delete_path(str(f), use_recycle_bin=False, dry_run=False)
    assert res.ok
    assert res.method == "permanent"
    assert not os.path.exists(f)


def test_missing_path_reports_skipped(tmp_path):
    res = delete_path(str(tmp_path / "nope"), dry_run=True)
    assert not res.ok
    assert "no longer exists" in res.error
