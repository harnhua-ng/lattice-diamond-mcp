"""Tests for scripts/build_embedded.py (Windows package with python.org's Python), the embedded mode of
install.py, and combining it with the Linux standalone program."""

import hashlib
import importlib.util
import json
import os
import stat
import struct
import subprocess
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
            archive.writestr("python312.dll", fake_pe())
        archive.writestr("python312._pth", "python312.zip\n.\n# import site\n")
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


@pytest.mark.parametrize("path, posix, expected", [
    ("C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\tmpabc", True, "/c/Users/RUNNER~1/AppData/Local/Temp/tmpabc"),
    ("D:\\a\\repo\\scripts\\keys\\k.asc", True, "/d/a/repo/scripts/keys/k.asc"),
    ("C:/Users/x/file.zip", True, "/c/Users/x/file.zip"),
    ("C:\\Users\\x", False, "C:\\Users\\x"),                    # a native Windows gpg wants the path as it is
    ("/tmp/already/posix", True, "/tmp/already/posix"),
    ("relative\\path", True, "relative\\path"),
])
def test_paths_are_given_to_an_msys_gpg_the_unix_way_and_to_other_gpgs_unchanged(path, posix, expected):
    assert embedded.to_gpg_path(path, posix) == expected


def test_the_kind_of_gpg_is_read_from_the_home_line_of_its_version_output():
    msys = "gpg (GnuPG) 2.4.7\nlibgcrypt 1.11.0\nHome: /c/Users/runneradmin/.gnupg\nSupported algorithms:\n"
    native = "gpg (GnuPG) 2.4.5\nHome: C:\\Users\\runneradmin\\AppData\\Roaming\\gnupg\nSupported algorithms:\n"
    assert embedded.gpg_home_is_posix(msys) is True
    assert embedded.gpg_home_is_posix(native) is False
    assert embedded.gpg_home_is_posix("no home line here") is False


@posix_only
def test_gpg_is_asked_which_kind_it_is_without_the_callers_gnupghome(tmp_path, monkeypatch):
    fake = tmp_path / "gpg"
    fake.write_text('#!/bin/sh\nif [ -n "$GNUPGHOME" ]; then echo "Home: C:\\\\leaked"; else echo "Home: /c/Users/x/.gnupg"; fi\n')
    fake.chmod(0o755)
    monkeypatch.setenv("GNUPGHOME", "C:\\Users\\x")
    assert embedded.gpg_wants_posix_paths(str(fake)) is True


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


@pytest.mark.parametrize("member, message", [("pkg-1.data/headers/x.h", "does not unpack"), ("pkg-1.data/data/share/x", "does not unpack"),
                                             ("../escape.py", "unsafe path")])
def test_a_wheel_with_headers_data_or_an_unsafe_path_is_refused(tmp_path, member, message):
    wheels = tmp_path / "w"
    wheels.mkdir()
    make_wheel(wheels, "pkg-1-py3-none-any.whl", {member: "x"})
    with pytest.raises(embedded.BuildError, match=message):
        embedded.unpack_wheels(wheels, tmp_path / "site")


def test_scripts_are_left_out_and_reported_and_purelib_goes_into_site_packages(tmp_path):
    wheels = tmp_path / "w"
    wheels.mkdir()
    make_wheel(wheels, "pkg-1-py3-none-any.whl", {"pkg/__init__.py": "x", "pkg-1.data/scripts/post_install.py": "run",
                                                  "pkg-1.data/purelib/extra/mod.py": "y", "pkg-1.dist-info/METADATA": "m"})
    site = tmp_path / "python" / "Lib" / "site-packages"
    skipped = embedded.unpack_wheels(wheels, site)
    assert skipped == ["pkg: post_install.py"]
    assert (site / "pkg" / "__init__.py").exists() and (site / "extra" / "mod.py").read_text() == "y"
    assert not list(site.rglob("post_install.py")) and not any(p.name.endswith(".data") for p in site.iterdir())


def test_programs_from_wheels_are_removed_and_reported(tmp_path):
    site = tmp_path / "site-packages"
    (site / "pythonwin").mkdir(parents=True)
    (site / "win32").mkdir()
    (site / "pythonwin" / "Pythonwin.exe").write_bytes(b"MZ")
    (site / "win32" / "pythonservice.exe").write_bytes(b"MZ")
    (site / "win32" / "win32api.pyd").write_bytes(b"MZ")
    assert embedded.remove_programs(site) == ["pythonwin/Pythonwin.exe", "win32/pythonservice.exe"]
    assert not list(site.rglob("*.exe")) and (site / "win32" / "win32api.pyd").exists()
    assert embedded.remove_programs(site) == []


def test_the_pth_file_lists_the_library_zip_the_folder_site_packages_and_enables_site(tmp_path):
    (tmp_path / "python312._pth").write_text("python312.zip\n.\n# Uncomment to run site.main() automatically\n#import site\n")
    pth = embedded.write_pth(tmp_path)
    assert pth.read_text().splitlines() == ["python312.zip", ".", "Lib\\site-packages", "import site"]


# --------------------------------------------------------------------- the checks

def make_wheel_with_requires(folder, name, requires):
    metadata = "Metadata-Version: 2.1\nName: %s\n%s" % (name.split("-")[0], "".join("Requires-Dist: %s\n" % r for r in requires))
    return make_wheel(folder, name + ".whl", {"%s.dist-info/METADATA" % "-".join(name.split("-")[:2]): metadata})


def test_windows_markers_are_those_of_the_target_not_of_the_build_machine():
    environment = embedded.windows_marker_environment()
    assert environment["sys_platform"] == "win32" and environment["platform_system"] == "Windows" and environment["os_name"] == "nt"
    assert environment["python_version"] == "3.12" and environment["python_full_version"] == embedded.EMBED_VERSION


def test_a_missing_windows_only_dependency_is_caught(tmp_path):
    wheels = tmp_path / "w"
    wheels.mkdir()
    make_wheel_with_requires(wheels, "mcp-1.0-py3-none-any", ["anyio>=4", "pywin32>=310; sys_platform == 'win32'",
                                                              "uvloop; sys_platform != 'win32'", "extra-only; extra == 'cli'"])
    make_wheel_with_requires(wheels, "anyio-4.0-py3-none-any", [])
    with pytest.raises(embedded.BuildError, match=r"pywin32 \(required by mcp\)") as caught:
        embedded.check_closure(wheels, embedded.windows_marker_environment())
    assert "uvloop" not in str(caught.value) and "extra-only" not in str(caught.value)   # not needed on Windows, or only for an extra
    make_wheel_with_requires(wheels, "pywin32-312-cp312-cp312-win_amd64", [])
    embedded.check_closure(wheels, embedded.windows_marker_environment())               # now complete


def test_pip_is_started_with_the_markers_of_the_target():
    assert "markers.default_environment" in embedded.PIP_AS_TARGET and "sys.exit(3)" in embedded.PIP_AS_TARGET


def make_stage(tmp_path):
    stage = tmp_path / "stage"
    embedded.unpack_interpreter(make_embed_zip(tmp_path / "e.zip"), stage / "python")
    site = stage / "python" / "Lib" / "site-packages"
    (site / "mcp").mkdir(parents=True)
    (site / "lattice_diamond_mcp-0.1.0.dist-info").mkdir()
    embedded.write_pth(stage / "python")
    return stage


def test_a_layout_that_does_not_enable_site_or_keeps_a_comment_fails(tmp_path):
    stage = make_stage(tmp_path)
    pth = stage / "python" / "python312._pth"
    pth.write_text("python312.zip\n.\nLib\\site-packages\n")
    with pytest.raises(embedded.BuildError, match="does not look as expected"):
        embedded.verify_layout(stage, ["mcp"])
    pth.write_text("python312.zip\n.\nLib\\site-packages\nimport site\n#import site\n")
    with pytest.raises(embedded.BuildError, match="does not look as expected"):
        embedded.verify_layout(stage, ["mcp"])


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
    assert "openssl-3." in (tmp_path / "po" / "LIBRARIES.txt").read_text()
    text = embedded.legal_review_text(version="0.1.0", python_sha256="abc", embedded_notices=names,
                                      unsigned_modules=["rpds.pyd"], vcruntime=["vcruntime140.dll"], python_org_notices=python_org,
                                      removed_programs=["win32/pythonservice.exe"])
    assert "win32/pythonservice.exe" in text and "differ from those wheels in exactly these" in text
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
    assert len(embedded.EMBED_SHA256) == 64 and embedded.PYTHON_TAG == "cp312"


# ------------------------------------------------------------------------ installer

def embedded_source(folder, script="#!/bin/sh\necho diamond-mcp usage\n"):
    source = folder / "python"
    source.mkdir(parents=True)
    exe = source / "python.exe"
    exe.write_text(script)
    exe.chmod(0o755)
    (source / "python312._pth").write_text("x")
    return source


@posix_only
def test_embedded_install_copies_checks_and_marks_the_folder(tmp_path):
    embedded_source(tmp_path / "zip")
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    python = install.install_embedded_bundle(args, {"version": "0.1.0", "python": "3.12.10"}, str(tmp_path / "zip"))
    assert python == str(tmp_path / "target" / "python.exe")
    assert (tmp_path / "target" / install.MARKER).exists() and (tmp_path / "target" / "python312._pth").exists()


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
    (tmp_path / "windows-x86_64" / "BUILDINFO").write_text(json.dumps({"kind": "embedded", "platform": "windows-x86_64", "python": "3.12.10"}))
    monkeypatch.setattr(install, "HERE", str(tmp_path))
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    folder, info = install.select_bundle()
    assert Path(folder).name == "windows-x86_64" and info["kind"] == "embedded"


# ------------------------------------------------------------------------ combining

def make_zip(folder, platform, kind, version="0.1.0", git="abc"):
    info = {"name": "lattice-diamond-mcp", "version": version, "kind": kind, "platform": platform, "git": git}
    if kind == "embedded":
        info.update(python="3.12.10", python_tag="cp312")
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


# ------------------------------------------------------------ trimming what the server does not use

def pe_with_imports(names):
    """A minimal PE32+ file with one section that holds an import table naming `names`."""
    data = bytearray(0x600)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x80 + 6, 1)                       # one section
    struct.pack_into("<H", data, 0x80 + 20, 240)                    # size of the optional header
    struct.pack_into("<H", data, 0x80 + 24, 0x20B)
    section_header = 0x80 + 24 + 240
    virtual_address, raw_pointer = 0x1000, 0x200
    struct.pack_into("<IIII", data, section_header + 8, 0x400, virtual_address, 0x400, raw_pointer)
    table_rva = virtual_address
    struct.pack_into("<II", data, 0x80 + 24 + 112 + 8, table_rva, 20 * (len(names) + 1))
    strings = raw_pointer + 20 * (len(names) + 1)
    for index, name in enumerate(names):
        name_rva = virtual_address + (strings - raw_pointer)
        struct.pack_into("<I", data, raw_pointer + 20 * index + 12, name_rva)
        data[strings:strings + len(name) + 1] = name.encode() + b"\0"
        strings += len(name) + 1
    return bytes(data)


def test_the_import_table_of_a_windows_module_is_read(tmp_path):
    path = tmp_path / "m.pyd"
    path.write_bytes(pe_with_imports(["python312.dll", "pywintypes312.dll", "KERNEL32.dll"]))
    assert embedded.pe_imports(path) == ["python312.dll", "pywintypes312.dll", "KERNEL32.dll"]
    other = tmp_path / "t.txt"
    other.write_text("not a program")
    assert embedded.pe_imports(other) == []


def test_a_kept_file_that_needs_a_removed_dll_fails_the_build(tmp_path):
    (tmp_path / "win32").mkdir()
    (tmp_path / "win32" / "win32api.pyd").write_bytes(pe_with_imports(["python312.dll", "pywintypes312.dll", "kernel32.dll"]))
    with pytest.raises(embedded.BuildError, match="win32api.pyd needs pywintypes312.dll, which was removed"):
        embedded.check_native_dependencies(tmp_path, {"pywintypes312.dll", "pythoncom312.dll"})
    embedded.check_native_dependencies(tmp_path, {"pythoncom312.dll"})               # it only needs what stayed
    embedded.check_native_dependencies(tmp_path, set())


def make_pywin32_like_wheel(folder):
    members = {name: "x" for name in ("win32/win32api.pyd", "win32/win32ui.pyd", "pywin32.pth", "pythonwin/Pythonwin.exe",
                                      "win32com/client/__init__.py", "pywin32-312.dist-info/METADATA", "pywin32-312.dist-info/licenses/LICENSE")}
    return make_wheel(folder, "pywin32-312-cp312-cp312-win_amd64.whl", members)


def test_a_wheel_is_trimmed_to_the_listed_files_and_keeps_its_dist_info(tmp_path):
    wheels, site = tmp_path / "w", tmp_path / "site"
    wheels.mkdir()
    make_pywin32_like_wheel(wheels)
    embedded.unpack_wheels(wheels, site)
    pruned = embedded.prune_wheel_files(wheels, site, {"pywin32": ("win32/win32api.pyd", "pywin32.pth")})
    assert pruned == {"pywin32": 3}
    kept = sorted(p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file())
    assert kept == ["pywin32-312.dist-info/METADATA", "pywin32-312.dist-info/licenses/LICENSE", "pywin32.pth", "win32/win32api.pyd"]
    assert not (site / "win32com").exists() and not (site / "pythonwin").exists()           # emptied folders are gone too


def test_trimming_refuses_a_wheel_whose_layout_changed(tmp_path):
    wheels, site = tmp_path / "w", tmp_path / "site"
    wheels.mkdir()
    make_pywin32_like_wheel(wheels)
    embedded.unpack_wheels(wheels, site)
    with pytest.raises(embedded.BuildError, match="no longer holds win32/win32job.pyd"):
        embedded.prune_wheel_files(wheels, site, {"pywin32": ("win32/win32job.pyd",)})


def test_unused_packages_are_dropped_with_their_reasons_and_not_reported_as_missing(tmp_path):
    wheels = tmp_path / "w"
    wheels.mkdir()
    make_wheel_with_requires(wheels, "mcp-1.0-py3-none-any", ["pyjwt[crypto]>=2.10", "anyio"])
    make_wheel_with_requires(wheels, "anyio-4.0-py3-none-any", [])
    make_wheel_with_requires(wheels, "pyjwt-2.0-py3-none-any", ["cryptography; extra == 'crypto'"])
    make_wheel_with_requires(wheels, "cryptography-1.0-cp311-abi3-win_amd64", ["cffi"])
    assert embedded.omit_packages(wheels, embedded.OMITTED_PACKAGES) == ["cryptography", "pyjwt"]
    with pytest.raises(embedded.BuildError, match="pyjwt"):
        embedded.check_closure(wheels, embedded.windows_marker_environment())
    embedded.check_closure(wheels, embedded.windows_marker_environment(), exempt=set(embedded.OMITTED_PACKAGES))


def test_the_pywin32_files_that_are_kept_are_the_four_modules_mcp_imports_and_what_they_load():
    names = {Path(p).name for p in embedded.PYWIN32_KEEP}
    assert {"win32api.pyd", "win32job.pyd", "win32con.py", "pywintypes.py", "pywintypes312.dll", "_win32sysloader.pyd",
            "pywin32.pth", "pywin32_bootstrap.py"} == names
    assert embedded.KEEP_ONLY == {"pywin32": embedded.PYWIN32_KEEP}


def test_the_server_imports_none_of_the_packages_the_package_leaves_out():
    code = ("import sys; sys.argv = ['x']; import diamond_mcp.server; "
            "names = {'jwt': 'pyjwt', 'cryptography': 'cryptography', 'cffi': 'cffi', '_cffi_backend': 'cffi', 'pycparser': 'pycparser'}; "
            "bad = sorted({names[m.split('.')[0]] for m in sys.modules if m.split('.')[0] in names}); "
            "print('IMPORTED:' + ','.join(bad)); sys.exit(1 if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                            env=dict(os.environ, PYTHONPATH=str(SCRIPTS.parent / "src")))
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(embedded.OMITTED_PACKAGES) == {"pyjwt", "cryptography", "cffi", "pycparser"}


# ------------------------------------------------- moving the copied folder into place (Windows refuses while files are open)

def staged(tmp_path, content="new"):
    staging = tmp_path / "python.installing"
    staging.mkdir()
    (staging / "python.exe").write_text(content)
    return staging


def test_on_windows_a_refused_rename_is_retried_until_it_works(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setattr(install, "SWAP_PAUSE", 0)
    real, calls = os.rename, []

    def flaky(source, target):
        calls.append(source)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real(source, target)
    monkeypatch.setattr(install.os, "rename", flaky)
    target = tmp_path / "python"
    install.swap_into_place(str(staged(tmp_path)), str(target))
    assert len(calls) == 3 and (target / "python.exe").read_text() == "new" and not (tmp_path / "python.installing").exists()


def test_when_the_rename_never_works_the_files_are_copied_into_place(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setattr(install, "SWAP_PAUSE", 0)
    monkeypatch.setattr(install.os, "rename", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "Access is denied")))
    target = tmp_path / "python"
    target.mkdir()
    (target / "old.txt").write_text("old")
    install.swap_into_place(str(staged(tmp_path)), str(target))
    assert (target / "python.exe").read_text() == "new" and not (target / "old.txt").exists()
    assert not (tmp_path / "python.installing").exists()


def test_off_windows_the_first_failure_is_raised_without_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    calls = []
    monkeypatch.setattr(install.os, "rename", lambda a, b: calls.append(1) or (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(PermissionError):
        install.swap_into_place(str(staged(tmp_path)), str(tmp_path / "python"))
    assert calls == [1]


def test_if_even_copying_fails_the_first_error_is_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setattr(install, "SWAP_PAUSE", 0)
    monkeypatch.setattr(install, "SWAP_ATTEMPTS", 2)
    monkeypatch.setattr(install.os, "rename", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "first")))
    monkeypatch.setattr(install.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("copy failed")))
    with pytest.raises(PermissionError, match="first"):
        install.swap_into_place(str(staged(tmp_path)), str(tmp_path / "python"))


def failing_swap(monkeypatch, times):
    """swap_into_place that fails `times` times, then really swaps; returns the list of calls."""
    real, calls = install.swap_into_place, []

    def swap(staging, target):
        calls.append(1)
        if len(calls) <= times:
            raise PermissionError(5, "Access is denied")
        real(staging, target)
    monkeypatch.setattr(install, "swap_into_place", swap)
    return calls


def test_with_nothing_running_the_error_blames_antivirus_not_the_ai_client(tmp_path, monkeypatch):
    embedded_source(tmp_path / "zip")
    failing_swap(monkeypatch, 99)
    monkeypatch.setattr(install, "holders_of", lambda *folders: [])
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    with pytest.raises(install.InstallError) as caught:
        install.place_folder(args, str(tmp_path / "zip" / "python"), str(tmp_path / "target"))
    message = str(caught.value)
    assert "No MCP server was found running from it" in message and "antivirus" in message and "Access is denied" in message
    assert not (tmp_path / "target.installing").exists()


def test_a_server_that_appears_before_the_move_gets_the_same_stop_or_wait_question_and_the_copy_is_redone(tmp_path, monkeypatch):
    embedded_source(tmp_path / "zip")
    calls = failing_swap(monkeypatch, 1)
    monkeypatch.setattr(install, "holders_of", lambda *folders: ["4242 python.exe"])
    asked = []
    monkeypatch.setattr(install, "free_directory", lambda args, path: asked.append(path))
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    assert install.place_folder(args, str(tmp_path / "zip" / "python"), str(tmp_path / "target")) is True
    assert asked == [str(tmp_path / "target")] and len(calls) == 2
    assert (tmp_path / "target" / "python.exe").exists() and (tmp_path / "target" / install.MARKER).exists()


def test_a_holder_that_is_still_there_after_the_question_is_named_in_the_error(tmp_path, monkeypatch):
    embedded_source(tmp_path / "zip")
    failing_swap(monkeypatch, 99)
    monkeypatch.setattr(install, "holders_of", lambda *folders: ["4242 python.exe"])
    monkeypatch.setattr(install, "free_directory", lambda args, path: None)
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    with pytest.raises(install.InstallError, match="Still running from it: 4242 python.exe"):
        install.place_folder(args, str(tmp_path / "zip" / "python"), str(tmp_path / "target"))


def test_exiting_at_the_question_stops_the_install(tmp_path, monkeypatch):
    embedded_source(tmp_path / "zip")
    failing_swap(monkeypatch, 99)
    monkeypatch.setattr(install, "holders_of", lambda *folders: ["4242 python.exe"])

    def decline(args, path):
        raise install.InstallError("Nothing was changed.")
    monkeypatch.setattr(install, "free_directory", decline)
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    with pytest.raises(install.InstallError, match="Nothing was changed"):
        install.place_folder(args, str(tmp_path / "zip" / "python"), str(tmp_path / "target"))


# ------------------------------------------------- naming the program that has files open (Windows Restart Manager)

def test_the_restart_manager_is_not_asked_off_windows_and_never_raises_where_it_is_missing(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    assert install.open_file_holders([str(tmp_path)]) == []
    monkeypatch.setattr(install, "IS_WINDOWS", True)          # on this system there is no ctypes.WinDLL or rstrtmgr
    assert install.open_file_holders([str(tmp_path)]) == []
    assert install.open_file_holders([str(tmp_path / "nothing")]) == []


def test_the_files_to_ask_about_are_those_below_the_existing_folders_up_to_a_limit(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("a.txt", "sub/b.txt", "sub/c.txt"):
        (tmp_path / name).write_text("x")
    found = install.files_under([str(tmp_path), str(tmp_path / "missing")])
    assert sorted(Path(f).name for f in found) == ["a.txt", "b.txt", "c.txt"]
    assert len(install.files_under([str(tmp_path)], limit=2)) == 2


def test_programs_with_files_open_are_named_but_not_offered_for_stopping(tmp_path, monkeypatch):
    embedded_source(tmp_path / "zip")
    failing_swap(monkeypatch, 99)
    monkeypatch.setattr(install, "holders_of", lambda *folders: [])
    monkeypatch.setattr(install, "open_file_holders", lambda folders: ["1234 Microsoft Defender Antivirus Service", "5678 Windows Search"])
    asked = []
    monkeypatch.setattr(install, "free_directory", lambda args, path: asked.append(path))
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    with pytest.raises(install.InstallError) as caught:
        install.place_folder(args, str(tmp_path / "zip" / "python"), str(tmp_path / "target"))
    message = str(caught.value)
    assert "1234 Microsoft Defender Antivirus Service" in message and "5678 Windows Search" in message and "Windows Defender is MsMpEng.exe" in message
    assert asked == []                                   # never offered for stopping: it is not ours


def test_a_program_found_both_ways_is_not_listed_twice(tmp_path, monkeypatch):
    embedded_source(tmp_path / "zip")
    failing_swap(monkeypatch, 99)
    monkeypatch.setattr(install, "holders_of", lambda *folders: ["4242 python.exe"])
    monkeypatch.setattr(install, "open_file_holders", lambda folders: ["4242 Python", "9 Other"])
    monkeypatch.setattr(install, "free_directory", lambda args, path: None)
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    with pytest.raises(install.InstallError, match="Still running from it: 4242 python.exe"):
        install.place_folder(args, str(tmp_path / "zip" / "python"), str(tmp_path / "target"))
