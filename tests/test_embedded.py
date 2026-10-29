"""Tests for scripts/build_embedded.py (Windows package with python.org's Python), the embedded mode of
install.py, and combining it with the Linux standalone program."""

import hashlib
import importlib.util
import json
import os
import stat
import struct
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses shell-script test doubles")


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


embedded = load("build_embedded")
combine = load("combine_installers")
install = load("install")


def fake_pe(signer=b"Python Software Foundation"):
    """A minimal PE32+ file; with `signer`, its certificate table holds that name."""
    data = bytearray(0x400)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x80 + 24, 0x20B)
    if signer:
        blob = b"\0" * 8 + signer
        struct.pack_into("<II", data, 0x80 + 24 + 112 + 4 * 8, 0x200, len(blob))
        data[0x200:0x200 + len(blob)] = blob
    return bytes(data)


def make_embed_zip(path, signed=True, extra=(), with_dll=True):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("python.exe", fake_pe(b"Python Software Foundation" if signed else b""))
        archive.writestr("pythonw.exe", fake_pe())
        if with_dll:
            archive.writestr("python313.dll", fake_pe())
        archive.writestr("python313._pth", "python313.zip\n.\n# import site\n")
        archive.writestr("LICENSE.txt", "PSF licence")
        archive.writestr("vcruntime140.dll", fake_pe(b"Microsoft"))
        for name, data in extra:
            archive.writestr(name, data)
    return path


# ------------------------------------------------------------------- the interpreter

def test_signature_block_finds_the_signer_and_reports_none_for_unsigned_or_other_files(tmp_path):
    signed, unsigned, other = tmp_path / "a.exe", tmp_path / "b.exe", tmp_path / "c.txt"
    signed.write_bytes(fake_pe())
    unsigned.write_bytes(fake_pe(b""))
    other.write_text("not a program")
    assert embedded.SIGNER in embedded.signature_block(signed)
    assert embedded.signature_block(unsigned) == b"" and embedded.signature_block(other) == b""


def test_the_embeddable_zip_must_match_the_pinned_hash(tmp_path, monkeypatch):
    zip_path = make_embed_zip(tmp_path / "e.zip")
    with pytest.raises(embedded.BuildError, match="expected"):
        embedded.fetch_embeddable(tmp_path, zip_path, check_pgp=False)
    monkeypatch.setattr(embedded, "EMBED_SHA256", hashlib.sha256(zip_path.read_bytes()).hexdigest())
    assert embedded.fetch_embeddable(tmp_path, zip_path, check_pgp=False) == (zip_path, None)


# ----------------------------------------------------------------------- OpenPGP signature

FPR = embedded.PGP_FINGERPRINT


def status(*lines):
    return "\n".join("[GNUPG:] " + line for line in lines) + "\n"


def test_gpg_status_with_a_good_signature_by_the_pinned_key_passes():
    good = status("GOODSIG FC624643487034E5 Someone", "VALIDSIG %s 2026-10-21 1 0 4 0 1 8 00 %s" % (FPR, FPR), "TRUST_UNDEFINED 0 pgp")
    assert embedded.check_gpg_status(good) == FPR
    subkey = status("GOODSIG AAAA Someone", "VALIDSIG 1111222233334444555566667777888899990000 2026-10-21 1 0 4 0 1 8 00 " + FPR)
    assert embedded.check_gpg_status(subkey) == FPR          # signed by a subkey of the pinned key


@pytest.mark.parametrize("lines, message", [
    (["BADSIG AAAA Someone"], "BADSIG"),
    (["EXPKEYSIG AAAA Someone", "VALIDSIG %s x 1 0 4 0 1 8 00 %s" % (FPR, FPR)], "EXPKEYSIG"),
    (["REVKEYSIG AAAA Someone"], "REVKEYSIG"),
    (["NO_PUBKEY AAAA", "ERRSIG AAAA 1 8 00 1 9 -"], "ERRSIG|NO_PUBKEY"),
    (["GOODSIG AAAA Someone"], "did not report"),
    (["GOODSIG AAAA Someone", "VALIDSIG " + "A" * 40 + " x 1 0 4 0 1 8 00 " + "A" * 40], "not by the pinned key"),
])
def test_gpg_status_that_is_not_a_good_signature_by_the_pinned_key_fails(lines, message):
    with pytest.raises(embedded.BuildError, match=message):
        embedded.check_gpg_status(status(*lines))


def test_the_checked_in_key_is_the_pinned_one():
    import re
    text = embedded.PGP_KEY.read_text()
    assert "BEGIN PGP PUBLIC KEY BLOCK" in text and "PRIVATE" not in text
    assert re.fullmatch(r"[0-9A-F]{40}", FPR)


gpg_available = pytest.mark.skipif(not (__import__("shutil").which("gpg") or __import__("shutil").which("gpg2")), reason="gpg not installed")


def make_signed_file(tmp_path, monkeypatch):
    """A file signed with a throwaway key; the build's pin and key file are pointed at that key."""
    import shutil
    import subprocess
    gpg = shutil.which("gpg") or shutil.which("gpg2")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    env = dict(os.environ, GNUPGHOME=str(home), LC_ALL="C")
    subprocess.run([gpg, "--batch", "--no-tty", "--passphrase", "", "--quick-generate-key", "Test Signer <t@example.invalid>",
                    "ed25519", "sign", "never"], env=env, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
    listing = subprocess.run([gpg, "--batch", "--with-colons", "--fingerprint", "--list-keys"], env=env, check=True,
                             capture_output=True, text=True).stdout
    fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    key = tmp_path / "key.asc"
    key.write_bytes(subprocess.run([gpg, "--batch", "--armor", "--export", fingerprint], env=env, check=True, capture_output=True).stdout)
    data = tmp_path / "data.zip"
    data.write_bytes(b"the interpreter")
    sig = tmp_path / "data.zip.asc"
    subprocess.run([gpg, "--batch", "--no-tty", "--armor", "--detach-sign", "--output", str(sig), str(data)], env=env, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
    monkeypatch.setattr(embedded, "PGP_FINGERPRINT", fingerprint)
    return data, sig, key


@gpg_available
def test_a_file_signed_by_the_pinned_key_verifies(tmp_path, monkeypatch):
    data, sig, key = make_signed_file(tmp_path, monkeypatch)
    assert embedded.verify_pgp(data, sig, key) == embedded.PGP_FINGERPRINT


@gpg_available
def test_a_changed_file_or_a_different_key_does_not_verify(tmp_path, monkeypatch):
    data, sig, key = make_signed_file(tmp_path, monkeypatch)
    data.write_bytes(b"the interpreter, changed")
    with pytest.raises(embedded.BuildError, match="does not verify"):
        embedded.verify_pgp(data, sig, key)
    data.write_bytes(b"the interpreter")
    monkeypatch.setattr(embedded, "PGP_FINGERPRINT", "A" * 40)             # a different pin than the key that signed
    with pytest.raises(embedded.BuildError, match="does not contain the pinned key"):
        embedded.verify_pgp(data, sig, key)


@gpg_available
def test_a_missing_key_file_and_a_missing_gpg_are_reported(tmp_path, monkeypatch):
    data, sig, key = make_signed_file(tmp_path, monkeypatch)
    with pytest.raises(embedded.BuildError, match="missing from the repository"):
        embedded.verify_pgp(data, sig, tmp_path / "nokey.asc")
    monkeypatch.setattr(embedded.shutil, "which", lambda name: None)
    with pytest.raises(embedded.BuildError, match="gpg is not installed"):
        embedded.verify_pgp(data, sig, key)


def test_pgp_is_checked_by_default_and_skipping_is_recorded_as_unverified(tmp_path, monkeypatch):
    zip_path = make_embed_zip(tmp_path / "e.zip")
    monkeypatch.setattr(embedded, "EMBED_SHA256", hashlib.sha256(zip_path.read_bytes()).hexdigest())
    with pytest.raises(embedded.BuildError, match="gpg is not installed|does not verify|No such file|signature"):
        monkeypatch.setattr(embedded.shutil, "which", lambda name: None)
        embedded.fetch_embeddable(tmp_path, zip_path, signature=tmp_path / "e.zip.asc")
    assert embedded.fetch_embeddable(tmp_path, zip_path, check_pgp=False)[1] is None


def test_unpacking_keeps_only_python_exe_and_returns_the_licence(tmp_path):
    dest = tmp_path / "python"
    licence = embedded.unpack_interpreter(make_embed_zip(tmp_path / "e.zip"), dest)
    assert sorted(p.name for p in dest.rglob("*.exe")) == ["python.exe"]
    assert licence.read_text() == "PSF licence"


def test_an_interpreter_without_the_foundations_signature_is_refused(tmp_path):
    with pytest.raises(embedded.BuildError, match="Python Software Foundation"):
        embedded.unpack_interpreter(make_embed_zip(tmp_path / "e.zip", signed=False), tmp_path / "python")


def test_a_changed_layout_or_unsafe_path_is_refused(tmp_path):
    with pytest.raises(embedded.BuildError, match="layout has changed"):
        embedded.unpack_interpreter(make_embed_zip(tmp_path / "a.zip", with_dll=False), tmp_path / "a")
    with pytest.raises(embedded.BuildError, match="unsafe path"):
        embedded.unpack_interpreter(make_embed_zip(tmp_path / "b.zip", extra=[("../evil.txt", "x")]), tmp_path / "b")


# ------------------------------------------------------------------------ packages

def make_wheel(folder, name, members):
    path = folder / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data)
    return path


def test_wheels_are_unpacked_into_site_packages(tmp_path):
    wheels = tmp_path / "w"
    wheels.mkdir()
    make_wheel(wheels, "pkg-1-py3-none-any.whl", {"pkg/__init__.py": "x = 1", "pkg-1.dist-info/METADATA": "m"})
    site = tmp_path / "python" / "Lib" / "site-packages"
    embedded.unpack_wheels(wheels, site)
    assert (site / "pkg" / "__init__.py").read_text() == "x = 1" and (site / "pkg-1.dist-info" / "METADATA").exists()


@pytest.mark.parametrize("member, message", [("pkg-1.data/scripts/tool", "\\.data folder"), ("../escape.py", "unsafe path")])
def test_a_wheel_with_scripts_or_an_unsafe_path_is_refused(tmp_path, member, message):
    wheels = tmp_path / "w"
    wheels.mkdir()
    make_wheel(wheels, "pkg-1-py3-none-any.whl", {member: "x"})
    with pytest.raises(embedded.BuildError, match=message):
        embedded.unpack_wheels(wheels, tmp_path / "site")


def test_the_pth_file_lists_the_library_zip_the_folder_and_site_packages_only(tmp_path):
    (tmp_path / "python313._pth").write_text("python313.zip\n.\nimport site\n")
    pth = embedded.write_pth(tmp_path)
    assert pth.read_text().split() == ["python313.zip", ".", "Lib\\site-packages"]


# --------------------------------------------------------------------- the checks

def make_stage(tmp_path):
    stage = tmp_path / "stage"
    embedded.unpack_interpreter(make_embed_zip(tmp_path / "e.zip"), stage / "python")
    site = stage / "python" / "Lib" / "site-packages"
    (site / "mcp").mkdir(parents=True)
    (site / "lattice_diamond_mcp-0.1.0.dist-info").mkdir()
    embedded.write_pth(stage / "python")
    return stage


def test_a_correct_layout_passes(tmp_path):
    embedded.verify_layout(make_stage(tmp_path), ["mcp"])


def test_an_extra_program_or_a_missing_package_fails(tmp_path):
    stage = make_stage(tmp_path)
    with pytest.raises(embedded.BuildError, match="Expected nothing_here"):
        embedded.verify_layout(stage, ["nothing_here"])
    (stage / "python" / "Scripts").mkdir()
    (stage / "python" / "Scripts" / "diamond-mcp.exe").write_bytes(b"MZ")
    with pytest.raises(embedded.BuildError, match="no program but python.exe"):
        embedded.verify_layout(stage, ["mcp"])


def test_notices_are_copied_from_the_windows_set_and_listed_for_legal(tmp_path):
    names = embedded.collect_notices(tmp_path / "out")
    assert "LICENSE.openssl-3.txt" in names and "LICENSE.zlib.txt" in names
    assert (tmp_path / "out" / "LICENSE.openssl-3.txt").stat().st_size > 200
    python_org = embedded.collect_python_org_notices(tmp_path / "po")
    assert "license.rst" in python_org and "LIBRARIES.txt" in python_org
    assert "OpenSSL" in (tmp_path / "po" / "license.rst").read_text(encoding="utf-8")
    assert "openssl-3.5.9" in (tmp_path / "po" / "LIBRARIES.txt").read_text()
    text = embedded.legal_review_text(version="0.1.0", python_sha256="abc", embedded_notices=names,
                                      unsigned_modules=["rpds.pyd"], vcruntime=["vcruntime140.dll"], python_org_notices=python_org)
    for needle in ("LICENSE.openssl-3.txt", "vcruntime140.dll", "rpds.pyd", "unmodified", "did\n      not publish them",
                   "No executable of ours", "license.rst", "microsoft-distributable-code.txt", "has NOT been checked"):
        assert needle in text, needle


REAL_CLAUSE = """Additional Conditions for this Windows binary build
This program is linked with and uses Microsoft Distributable Code, copyrighted by Microsoft Corporation.
- alter any copyright, trademark or patent notice in Microsoft's
Distributable Code;
- use Microsoft's trademarks in your programs' names
- distribute Microsoft's Distributable Code to run on a platform other
than Microsoft operating systems, run-time technologies or application
- include Microsoft Distributable Code in malicious, deceptive or
unlawful programs.
"""


def test_the_microsoft_clause_is_found_in_the_real_python_licence_and_in_a_stand_in_but_not_when_changed():
    assert embedded.microsoft_clause_present(REAL_CLAUSE)
    assert not embedded.microsoft_clause_present(REAL_CLAUSE.replace("Microsoft Distributable Code", "something else"))
    assert not embedded.microsoft_clause_present("PSF licence")


def test_the_microsoft_notice_lists_the_restrictions_and_the_files():
    text = embedded.microsoft_notice_text(["python.exe", "vcruntime140.dll"])
    for needle in ("Microsoft Distributable Code", "alter any copyright, trademark or patent notice", "trademarks",
                   "platform other than Microsoft operating systems", "malicious, deceptive or unlawful", "reverse engineer",
                   "python.exe, vcruntime140.dll", "cpython-%s/LICENSE.txt" % embedded.EMBED_VERSION):
        assert needle in text, needle


def test_the_pinned_download_is_a_python_org_https_url_for_the_same_version():
    assert embedded.EMBED_URL.startswith("https://www.python.org/ftp/python/%s/" % embedded.EMBED_VERSION)
    assert len(embedded.EMBED_SHA256) == 64 and embedded.PYTHON_TAG == "cp313"


# ------------------------------------------------------------------------ installer

def embedded_source(folder, script="#!/bin/sh\necho diamond-mcp usage\n"):
    source = folder / "python"
    source.mkdir(parents=True)
    exe = source / "python.exe"
    exe.write_text(script)
    exe.chmod(0o755)
    (source / "python313._pth").write_text("x")
    return source


@posix_only
def test_embedded_install_copies_checks_and_marks_the_folder(tmp_path):
    embedded_source(tmp_path / "zip")
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    python = install.install_embedded_bundle(args, {"version": "0.1.0", "python": "3.13.17"}, str(tmp_path / "zip"))
    assert python == str(tmp_path / "target" / "python.exe")
    assert (tmp_path / "target" / install.MARKER).exists() and (tmp_path / "target" / "python313._pth").exists()


@posix_only
def test_embedded_install_refuses_a_foreign_folder_and_a_server_that_does_not_start(tmp_path):
    embedded_source(tmp_path / "zip")
    (tmp_path / "foreign").mkdir()
    (tmp_path / "foreign" / "mine.txt").write_text("keep")
    args = install.parse_args(["--install-dir", str(tmp_path / "foreign")])
    with pytest.raises(install.InstallError, match="not created by this installer"):
        install.install_embedded_bundle(args, {"version": "0.1.0"}, str(tmp_path / "zip"))
    assert (tmp_path / "foreign" / "mine.txt").read_text() == "keep"
    broken = tmp_path / "broken"
    embedded_source(broken, "#!/bin/sh\necho something else\n")
    with pytest.raises(install.InstallError, match="did not behave"):
        install.install_embedded_bundle(install.parse_args(["--install-dir", str(tmp_path / "t2")]), {"version": "0.1.0"}, str(broken))


@posix_only
def test_embedded_install_runs_the_server_module_in_isolated_mode(tmp_path):
    log = tmp_path / "argv.txt"
    embedded_source(tmp_path / "zip", '#!/bin/sh\necho "$@" > %s\necho diamond-mcp\n' % log)
    install.install_embedded_bundle(install.parse_args(["--install-dir", str(tmp_path / "t")]), {"version": "0.1.0"}, str(tmp_path / "zip"))
    assert log.read_text().split() == ["-I", "-m", "diamond_mcp", "--help"]


def test_a_dry_run_changes_nothing(tmp_path):
    embedded_source(tmp_path / "zip")
    args = install.parse_args(["--dry-run", "--install-dir", str(tmp_path / "target")])
    python = install.install_embedded_bundle(args, {"version": "0.1.0"}, str(tmp_path / "zip"))
    assert python.endswith("python.exe") and not (tmp_path / "target").exists()


def test_uninstall_recognises_an_embedded_folder_given_with_install_dir(tmp_path):
    folder = tmp_path / "t"
    folder.mkdir()
    (folder / "python.exe").write_text("x")
    assert install.uninstall_targets(install.parse_args(["--uninstall", "--install-dir", str(folder)])) == [("embedded", str(folder))]
    (folder / "diamond-mcp").write_text("x") if os.name != "nt" else (folder / "diamond-mcp.exe").write_text("x")
    assert install.uninstall_targets(install.parse_args(["--uninstall", "--install-dir", str(folder)]))[0][0] == "binary"


def test_the_embedded_default_folder_sits_beside_the_environment_folder():
    assert Path(install.default_embedded_dir()).parent == Path(install.default_venv_dir()).parent
    assert Path(install.default_embedded_dir()).name == "python"


def test_install_selects_an_embedded_build_without_asking_for_a_python(tmp_path, monkeypatch):
    (tmp_path / "windows-x86_64").mkdir()
    (tmp_path / "windows-x86_64" / "BUILDINFO").write_text(json.dumps({"kind": "embedded", "platform": "windows-x86_64", "python": "3.13.17"}))
    monkeypatch.setattr(install, "HERE", str(tmp_path))
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    folder, info = install.select_bundle()
    assert Path(folder).name == "windows-x86_64" and info["kind"] == "embedded"


# ------------------------------------------------------------------------ combining

def make_zip(folder, platform, kind, version="0.1.0", git="abc"):
    info = {"name": "lattice-diamond-mcp", "version": version, "kind": kind, "platform": platform, "git": git}
    if kind == "embedded":
        info.update(python="3.13.17", python_tag="cp313")
    path = folder / ("lattice-diamond-mcp-%s-%s.zip" % (kind, platform))
    root = "lattice-diamond-mcp-" + version
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(root + "/BUILDINFO", json.dumps(info))
        archive.writestr(root + "/install.py", b"# installer\n")
        archive.writestr(root + ("/python/python.exe" if kind == "embedded" else "/diamond-mcp/diamond-mcp"), b"x")
    return path


def test_a_windows_embedded_zip_and_a_linux_standalone_zip_become_a_hybrid_package(tmp_path):
    (tmp_path / "in").mkdir()
    zips = [make_zip(tmp_path / "in", "windows-x86_64", "embedded"), make_zip(tmp_path / "in", "linux-x86_64", "binary")]
    assert combine.main(["--kind", "standalone", "--outdir", str(tmp_path / "out")] + [str(z) for z in zips]) == 0
    target = tmp_path / "out" / "lattice-diamond-mcp-0.1.0-hybrid-windows-linux-x86_64.zip"
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        index = archive.read("lattice-diamond-mcp-0.1.0/PACKAGES.txt").decode()
    assert "lattice-diamond-mcp-0.1.0/windows-x86_64/python/python.exe" in names
    assert "lattice-diamond-mcp-0.1.0/linux-x86_64/diamond-mcp/diamond-mcp" in names
    assert "no program of ours" in index


def test_an_embedded_package_for_linux_is_refused(tmp_path):
    (tmp_path / "in").mkdir()
    zips = [make_zip(tmp_path / "in", "windows-x86_64", "binary"), make_zip(tmp_path / "in", "linux-x86_64", "embedded")]
    assert combine.main(["--kind", "standalone", "--outdir", str(tmp_path / "out")] + [str(z) for z in zips]) == 1


def test_a_wheels_zip_still_cannot_be_combined_as_standalone(tmp_path):
    (tmp_path / "in").mkdir()
    zips = [make_zip(tmp_path / "in", "windows-x86_64", "embedded"), make_zip(tmp_path / "in", "linux-x86_64", "wheels")]
    assert combine.main(["--kind", "standalone", "--outdir", str(tmp_path / "out")] + [str(z) for z in zips]) == 1


# ------------------------------------------------------------------ the list of native files

def test_the_inventory_names_every_native_file_and_who_signed_it(tmp_path):
    (tmp_path / "python").mkdir()
    (tmp_path / "python" / "python.exe").write_bytes(fake_pe(b"Python Software Foundation"))
    (tmp_path / "python" / "vcruntime140.dll").write_bytes(fake_pe(b"Microsoft Windows Software Compatibility Publisher"))
    (tmp_path / "python" / "other.dll").write_bytes(fake_pe(b"Some Vendor Ltd"))
    (tmp_path / "python" / "mod.pyd").write_bytes(fake_pe(b""))
    (tmp_path / "python" / "readme.txt").write_text("not native")
    rows = embedded.native_inventory(tmp_path)
    assert {r[0]: r[1] for r in rows} == {"python/mod.pyd": "unsigned", "python/other.dll": "signed (other)",
                                          "python/python.exe": "Python Software Foundation",
                                          "python/vcruntime140.dll": "Microsoft"}
    text = embedded.inventory_text(rows)
    assert "not validated" in text and "Totals:" in text and "1 unsigned" in text
    assert all(len(r[2]) == 64 for r in rows)
