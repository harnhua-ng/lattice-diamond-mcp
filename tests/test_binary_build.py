"""Tests for scripts/build_binary.py and the binary mode of scripts/install.py."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build_binary")
install = load("install")
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses shell-script test doubles")

OBJDUMP = """
0000000000000000      DF *UND*  0000000000000000  GLIBC_2.2.5 memcpy
0000000000000000      DF *UND*  0000000000000000  GLIBC_2.14  memcpy
0000000000000000      DF *UND*  0000000000000000  (GLIBC_2.17) clock_gettime
0000000000000000      DF *UND*  0000000000000000  GLIBC_2.34  pthread_create
"""


# ------------------------------------------------------------------ glibc gate

def test_parse_glibc_requirement_takes_the_highest_version():
    assert build.parse_glibc_requirement(OBJDUMP) == (2, 34)
    assert build.parse_glibc_requirement("GLIBC_2.9 GLIBC_2.10") == (2, 10)  # numeric, not text, order
    assert build.parse_glibc_requirement("no versions here") is None


def test_enforce_glibc_passes_at_or_below_the_limit():
    assert build.enforce_glibc({"a.so": (2, 14), "b.so": (2, 17)}, (2, 17)) == (2, 17)
    assert build.enforce_glibc({}, (2, 17)) is None


def test_enforce_glibc_names_every_offender_worst_first():
    report = {"ok.so": (2, 14), "libgcc_s.so.1": (2, 35), "mid.so": (2, 28)}
    with pytest.raises(build.BuildError) as excinfo:
        build.enforce_glibc(report, (2, 17))
    message = str(excinfo.value)
    assert "glibc 2.35  libgcc_s.so.1" in message and "glibc 2.28  mid.so" in message and "ok.so" not in message
    assert message.index("libgcc_s") < message.index("mid.so")
    assert "HOST_LIBRARIES" in message


def test_the_spec_drops_the_host_libgcc_and_the_build_tools():
    spec = (SCRIPTS / "diamond-mcp.spec").read_text()
    assert 'HOST_LIBRARIES = ("libgcc_s.so",)' in spec and "BUILD_TOOLS" in spec
    for tool in ("setuptools", "pkg_resources", "_distutils_hack", "packaging"):
        assert '"%s"' % tool in spec


# ------------------------------------------------------------ pins stay consistent

def test_locked_pyinstaller_matches_the_pin_in_pyproject():
    import tomllib
    pins = tomllib.loads((REPO / "pyproject.toml").read_text())["dependency-groups"]["build"]
    assert "pyinstaller==%s" % build.locked_version("pyinstaller") in pins


def test_the_lock_names_the_runtime_dependency_and_no_build_tools_in_it_by_accident():
    assert build.locked_version("mcp")
    with pytest.raises(build.BuildError, match="not in uv.lock"):
        build.locked_version("definitely-not-a-package")


def test_embedded_python_pin_is_a_full_version():
    assert build.PYTHON_VERSION.count(".") == 2 and build.UV_VERSION.count(".") == 2


# ---------------------------------------------------------------------- inventory

def test_native_files_lists_libraries_and_the_program_with_hashes(tmp_path):
    dist = tmp_path / "diamond-mcp"
    (dist / "_internal" / "pkg").mkdir(parents=True)
    (dist / build.EXE_NAME).write_bytes(b"exe")
    (dist / "_internal" / "libpython3.11.so.1.0").write_bytes(b"lib")
    (dist / "_internal" / "pkg" / "ext.cpython-311-x86_64-linux-gnu.so").write_bytes(b"ext")
    (dist / "_internal" / "pkg" / "module.py").write_text("x = 1")
    (dist / "_internal" / "data.json").write_text("{}")
    if os.name != "nt":
        (dist / "_internal" / "libpython3.so").symlink_to("libpython3.11.so.1.0")
    found = {f["path"]: f for f in build.native_files(dist)}
    assert set(found) == {build.EXE_NAME, "_internal/libpython3.11.so.1.0", "_internal/pkg/ext.cpython-311-x86_64-linux-gnu.so"}
    import hashlib
    assert found["_internal/libpython3.11.so.1.0"]["sha256"] == hashlib.sha256(b"lib").hexdigest()
    assert found[build.EXE_NAME]["size"] == 3


def test_sha256_file_is_correct(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"abc")
    assert build.sha256_file(f) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# ------------------------------------------------------------------------- SBOM

def uv_style_sbom():
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "metadata": {"tools": [{"name": "uv", "version": "0.13.5"}],
                     "component": {"type": "library", "bom-ref": "proj-1@0.1.0", "name": "lattice-diamond-mcp", "version": "0.1.0"}},
        "components": [
            {"type": "library", "bom-ref": "mcp-2@1.0", "name": "mcp", "version": "1.0", "purl": "pkg:pypi/mcp@1.0"},
            {"type": "library", "bom-ref": "cryptography-3@46", "name": "cryptography", "version": "46", "purl": "pkg:pypi/cryptography@46"},
            {"type": "library", "bom-ref": "cffi-4@2", "name": "cffi", "version": "2", "purl": "pkg:pypi/cffi@2"},
        ],
        "dependencies": [
            {"ref": "proj-1@0.1.0", "dependsOn": ["mcp-2@1.0"]},
            {"ref": "mcp-2@1.0", "dependsOn": ["cryptography-3@46"]},
            {"ref": "cryptography-3@46", "dependsOn": ["cffi-4@2"]},
            {"ref": "cffi-4@2", "dependsOn": []},
        ],
    }


FILES = [{"path": "diamond-mcp", "sha256": "a" * 64, "size": 10}, {"path": "_internal/libpython3.11.so.1.0", "sha256": "b" * 64, "size": 20}]


def test_augment_adds_interpreter_native_files_and_build_tool():
    sbom = build.augment_sbom(uv_style_sbom(), python_version="3.11.17", pyinstaller_version="6.22.3", files=FILES,
                              properties={"platform": "linux-x86_64"})
    assert {"name": "pyinstaller", "version": "6.22.3"} in sbom["metadata"]["tools"]
    assert {"name": "diamond-mcp:platform", "value": "linux-x86_64"} in sbom["metadata"]["properties"]
    python = next(c for c in sbom["components"] if c["name"] == "cpython")
    assert python["version"] == "3.11.17" and python["purl"] == "pkg:generic/cpython@3.11.17"
    files = [c for c in sbom["components"] if c["type"] == "file"]
    assert [(c["name"], c["hashes"][0]["alg"], c["hashes"][0]["content"]) for c in files] == [
        ("diamond-mcp", "SHA-256", "a" * 64), ("_internal/libpython3.11.so.1.0", "SHA-256", "b" * 64)]
    root = next(d for d in sbom["dependencies"] if d["ref"] == "proj-1@0.1.0")
    assert "cpython@3.11.17" in root["dependsOn"] and "file:diamond-mcp" in root["dependsOn"]
    assert build.validate_sbom(sbom) == 3 + 1 + 2


def test_augment_does_not_modify_its_input():
    original = uv_style_sbom()
    build.augment_sbom(original, python_version="3.11.17", pyinstaller_version="6.22.3", files=FILES, properties={})
    assert original == uv_style_sbom()


def test_prune_removes_absent_packages_and_every_edge_to_them():
    pruned = build.prune_sbom(uv_style_sbom(), ["cryptography", "cffi"])
    assert [c["name"] for c in pruned["components"]] == ["mcp"]
    assert pruned["dependencies"] == [{"ref": "proj-1@0.1.0", "dependsOn": ["mcp-2@1.0"]}, {"ref": "mcp-2@1.0", "dependsOn": []}]
    assert {"name": "diamond-mcp:locked-but-not-bundled", "value": "cffi, cryptography"} in pruned["metadata"]["properties"]
    assert build.validate_sbom(pruned) == 1


def test_validate_rejects_duplicates_and_dangling_references():
    sbom = uv_style_sbom()
    sbom["components"].append(dict(sbom["components"][0]))
    with pytest.raises(build.BuildError, match="duplicate"):
        build.validate_sbom(sbom)
    sbom = uv_style_sbom()
    sbom["dependencies"][0]["dependsOn"].append("ghost@1")
    with pytest.raises(build.BuildError, match="ghost@1"):
        build.validate_sbom(sbom)
    with pytest.raises(build.BuildError, match="CycloneDX"):
        build.validate_sbom({"bomFormat": "other"})


def test_runtime_package_names_excludes_the_project_and_the_interpreter():
    sbom = build.augment_sbom(uv_style_sbom(), python_version="3.11.17", pyinstaller_version="6", files=[], properties={})
    assert build.runtime_package_names(sbom) == {"mcp", "cryptography", "cffi"}


# --------------------------------------------- SBOM versus what is really in the program

def make_site_packages(root, dists):
    site = root / "site-packages"
    for name, version, files in dists:
        info = site / ("%s-%s.dist-info" % (name.replace("-", "_"), version))
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Name: %s\nVersion: %s\nLicense-Expression: MIT\n" % (name, version))
        (info / "RECORD").write_text("".join("%s,,\n" % f for f in files) + "%s/RECORD,,\n" % info.name)
        (info / "licenses").mkdir()
        (info / "licenses" / "LICENSE").write_text("licence of " + name)
    return site


def write_pyz_toc(path, modules):
    entries = [(m, "/env/lib/python3.11/site-packages/%s" % src, "PYMODULE") for m, src in modules]
    path.write_text(repr(("/work/PYZ-00.pyz", entries)))


def test_bundled_distributions_come_from_frozen_modules_and_native_files(tmp_path):
    site = make_site_packages(tmp_path, [
        ("anyio", "4", ["anyio/__init__.py", "anyio/_core.py"]),
        ("pydantic-core", "2", ["pydantic_core/__init__.py", "pydantic_core/_pydantic_core.so"]),
        ("cryptography", "46", ["cryptography/__init__.py"]),
        ("setuptools", "80", ["setuptools/__init__.py", "pkg_resources/__init__.py", "../../bin/x"]),
        ("lattice-diamond-mcp", "0.1", ["diamond_mcp/__init__.py"]),
        ("six", "1", ["six.py"]),
    ])
    toc = tmp_path / "PYZ-00.toc"
    write_pyz_toc(toc, [("anyio", "anyio/__init__.py"), ("anyio._core", "anyio/_core.py"), ("diamond_mcp", "diamond_mcp/__init__.py"),
                        ("six", "six.py"), ("os", "../stdlib/os.py")])
    bundled = build.bundled_distributions(site, toc, ["_internal/pydantic_core/_pydantic_core.so", "diamond-mcp"], "lattice-diamond-mcp")
    assert bundled == {"anyio", "pydantic-core", "six"}  # not cryptography or setuptools; never the project itself


def test_check_sbom_matches_bundle_reports_absent_and_rejects_unlisted():
    assert build.check_sbom_matches_bundle({"mcp", "cffi"}, {"mcp"}) == ["cffi"]
    assert build.check_sbom_matches_bundle({"mcp"}, {"mcp"}) == []
    with pytest.raises(build.BuildError, match="setuptools"):
        build.check_sbom_matches_bundle({"mcp"}, {"mcp", "setuptools"})


def test_collect_licences_copies_only_bundled_packages_plus_pyinstaller(tmp_path):
    site = make_site_packages(tmp_path, [("anyio", "4", []), ("cryptography", "46", []), ("pyinstaller", "6", [])])
    python_root = tmp_path / "py"
    (python_root / "lib" / "python3.11").mkdir(parents=True)
    (python_root / "lib" / "python3.11" / "LICENSE.txt").write_text("PSF")
    out = tmp_path / "out"
    index, missing = build.collect_licences(site, {"anyio"}, python_root, out)
    assert (out / "anyio-4" / "licenses" / "LICENSE").read_text() == "licence of anyio"
    assert (out / "pyinstaller-6" / "licenses" / "LICENSE").exists()
    assert not (out / "cryptography-46").exists()
    assert (out / ("cpython-" + build.PYTHON_VERSION) / "LICENSE.txt").read_text() == "PSF"
    assert missing == [] and "anyio 4  [MIT]" in index and "cryptography" not in index


def test_collect_licences_flags_a_package_without_one(tmp_path):
    site = make_site_packages(tmp_path, [("odd", "1", [])])
    for f in (site / "odd-1.dist-info" / "licenses").iterdir():
        f.unlink()
    index, missing = build.collect_licences(site, {"odd"}, tmp_path / "nowhere", tmp_path / "out")
    assert "odd 1" in missing and ("cpython " + build.PYTHON_VERSION) in missing


# ------------------------------------------------------------------------ legal notes

REAL_TERMS = ("In addition to the permissions in the GNU General Public License, the authors give you unlimited permission "
              "to link or embed compiled bootloader and related files into combinations with other programs. Run-time Hooks "
              "are licensed under the Apache License, Version 2.0.")


def legal(**overrides):
    args = dict(version="0.1.0", platform="linux-x86_64", pyinstaller_version="6.22.3",
                licence_relpath="THIRD_PARTY_LICENSES/pyinstaller-6.22.3/licenses/COPYING.txt", licence_text=REAL_TERMS)
    args.update(overrides)
    return build.legal_review_text(**args)


def test_legal_review_states_the_facts_and_names_the_shipped_licence_file():
    text = legal()
    assert "diamond-mcp' IS PyInstaller's compiled bootloader" in text
    assert "pyimod01_archive" in text and "build tool itself is not shipped" in text
    assert "THIRD_PARTY_LICENSES/pyinstaller-6.22.3/licenses/COPYING.txt" in text
    assert "Bootloader Exception" in text and "GPL-2.0-or-later" in text and "Apache-2.0" in text
    assert "wheels zip contains no PyInstaller code" in text
    assert "copied, not written for this product" in text and "byte for byte" in text and "license_sha256" in text
    assert "No product licence text" not in text
    assert "not legal conclusions" in text and "WARNING" not in text


def test_legal_review_still_flags_a_missing_product_licence():
    text = legal(product_licence_included=False)
    assert "No product licence text is in this zip" in text and "copied, not written" not in text


def test_the_repository_licence_is_the_unmodified_agreement_text():
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("AI SOLUTIONS SOFTWARE LICENSE AGREEMENT")
    # Its notice allows free distribution only with this line, and forbids changing the terms.
    assert "(c)2026 Lattice Semiconductor Corporation. You may freely distribute the text of this Agreement" in text
    assert "THIRD_PARTY_LICENSES.txt" in text  # section 4 points at the file both zips ship


def test_legal_review_warns_when_the_licence_wording_it_relies_on_is_gone():
    text = legal(licence_text="Some entirely different licence.")
    assert "WARNING" in text and "NOT found" in text and "Legal must read the file" in text
    assert "GPL-2.0-or-later" not in text and "unlimited" not in text  # no summary of terms the file does not back


def test_legal_review_does_not_claim_apache_hooks_unless_the_file_says_so():
    text = legal(licence_text=REAL_TERMS.replace("Apache License", "Some License"))
    assert "run-time hooks was not found" in text and "Its run-time hooks are Apache-2.0" not in text


def test_legal_review_handles_a_missing_licence_file():
    text = legal(licence_relpath=None, licence_text="")
    assert "NOT FOUND" in text and "WARNING" in text


# ------------------------------------------------------------------ smoke test and zip

@posix_only
def test_smoke_test_passes_against_the_real_server(tmp_path):
    wrapper = tmp_path / "diamond-mcp"
    wrapper.write_text('#!/bin/sh\nPYTHONPATH="%s" exec "%s" -m diamond_mcp "$@"\n' % (REPO / "src", sys.executable))
    wrapper.chmod(0o755)
    names = build.mcp_smoke_test(wrapper)
    assert len(names) >= build.MIN_TOOLS and "compile_project" in names


@posix_only
@pytest.mark.parametrize("script, fragment", [
    # --help is fine, but over stdio it says nothing and exits
    ('if [ "$1" = "--help" ]; then echo "usage: diamond-mcp"; fi', "closed its output"),
    # not the program we expect
    ('echo "something unrelated"', "did not describe"),
    # --help is fine, but over stdio it writes text that is not JSON-RPC
    ('echo "usage: diamond-mcp"', "not JSON-RPC"),
    # answers initialize, then exits before tools/list
    ('if [ "$1" = "--help" ]; then echo "usage: diamond-mcp"; else read l; '
     'echo \'{"jsonrpc":"2.0","id":1,"result":{}}\'; fi', None),
])
def test_smoke_test_rejects_programs_that_do_not_speak_mcp(tmp_path, script, fragment):
    fake = tmp_path / "diamond-mcp"
    fake.write_text("#!/bin/sh\n" + script + "\n")
    fake.chmod(0o755)
    with pytest.raises(build.BuildError) as excinfo:  # always a clean build failure, never a raw traceback
        build.mcp_smoke_test(fake)
    if fragment:
        assert fragment in str(excinfo.value)


@posix_only
def test_smoke_test_command_line_option(tmp_path):
    good = tmp_path / "diamond-mcp"
    good.write_text('#!/bin/sh\nPYTHONPATH="%s" exec "%s" -m diamond_mcp "$@"\n' % (REPO / "src", sys.executable))
    good.chmod(0o755)
    bad = tmp_path / "broken"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    run = lambda program: subprocess.run([sys.executable, str(SCRIPTS / "build_binary.py"), "--smoke-test", str(program)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ok = run(good)
    assert ok.returncode == 0 and "smoke test passed" in ok.stdout
    failed = run(bad)
    assert failed.returncode == 1 and "SMOKE TEST FAILED" in failed.stderr and "Traceback" not in failed.stderr


def windows_text_mode(monkeypatch):
    """Make Path.write_text behave as on Windows, where text mode turns \\n into \\r\\n."""
    original = Path.write_text

    def write_text(self, data, encoding=None, errors=None, newline=None):
        if newline is None:
            data = data.replace("\n", "\r\n")
        return original(self, data, encoding=encoding, errors=errors, newline="")

    monkeypatch.setattr(Path, "write_text", write_text)


@pytest.mark.parametrize("module_name", ["build_binary", "build_installer"])
def test_checksum_file_has_unix_line_endings_even_where_text_mode_adds_carriage_returns(tmp_path, monkeypatch, module_name):
    # Regression: on Windows the .sha256 file was written as "<hash>  <name>\\r\\n", and `sha256sum -c`
    # (run by the CI verify jobs under Git Bash) then looked for a file whose name ends in a carriage return.
    windows_text_mode(monkeypatch)
    module = build if module_name == "build_binary" else load("build_installer")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "BUILDINFO").write_text("{}")
    out = tmp_path / "out"
    out.mkdir()
    info = {"version": "0.1.0", "python": "3.11", "python_tag": "cp311", "platform": "linux-x86_64"}
    if module_name == "build_binary":
        target, digest = module.write_zip(stage, out, "bundle", "root")
        checksum = out / "bundle.zip.sha256"
    else:
        target, digest = module.write_zip(stage, out, info)
        checksum = out / (target.name + ".sha256")
    raw = checksum.read_bytes()
    assert b"\r" not in raw
    assert raw == ("%s  %s\n" % (digest, target.name)).encode()


def test_the_simulation_really_would_have_broken_the_old_behaviour(tmp_path, monkeypatch):
    windows_text_mode(monkeypatch)
    probe = tmp_path / "probe"
    probe.write_text("a\n", encoding="utf-8")
    assert probe.read_bytes() == b"a\r\n"


@posix_only
def test_write_zip_keeps_execute_bits_and_writes_a_checksum(tmp_path):
    stage = tmp_path / "stage"
    (stage / "diamond-mcp").mkdir(parents=True)
    exe = stage / "diamond-mcp" / "diamond-mcp"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (stage / "BUILDINFO").write_text("{}")
    out = tmp_path / "out"
    out.mkdir()
    target, digest = build.write_zip(stage, out, "bundle", "lattice-diamond-mcp-0.1.0")
    with zipfile.ZipFile(target) as archive:
        info = archive.getinfo("lattice-diamond-mcp-0.1.0/diamond-mcp/diamond-mcp")
        assert (info.external_attr >> 16) & stat.S_IXUSR
    assert digest == build.sha256_file(target)
    assert (out / "bundle.zip.sha256").read_text() == "%s  bundle.zip\n" % digest


# -------------------------------------------------------- installer: binary bundles

@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """An unpacked binary installer: BUILDINFO, install.py's folder, and a program that answers --help."""
    root = tmp_path / "bundle"
    (root / "diamond-mcp" / "_internal").mkdir(parents=True)
    program = root / "diamond-mcp" / install.binary_server("").lstrip("/\\")
    program.write_text('#!/bin/sh\necho "usage: diamond-mcp [-h]"\n')
    program.chmod(0o644)  # deliberately lost the execute bit, as some unzip tools do
    (root / "diamond-mcp" / "_internal" / "lib.so").write_text("x")
    info = {"version": "0.1.0", "kind": "binary", "platform": "linux-x86_64", "min_glibc": None}
    (root / "BUILDINFO").write_text(json.dumps(info))
    monkeypatch.setattr(install, "HERE", str(root))
    return root, info


@posix_only
def test_binary_install_copies_checks_marks_and_repairs_the_execute_bit(bundle, tmp_path):
    _, info = bundle
    target = tmp_path / "inst"
    args = install.parse_args(["--no-register", "--install-dir", str(target)])
    server = install.install_binary_bundle(args, info)
    assert server == str(target / "diamond-mcp")
    assert (target / "_internal" / "lib.so").exists() and (target / install.MARKER).exists()
    assert os.access(server, os.X_OK)
    assert not Path(str(target) + ".installing").exists()


@posix_only
def test_binary_reinstall_replaces_the_previous_copy(bundle, tmp_path):
    _, info = bundle
    target = tmp_path / "inst"
    args = install.parse_args(["--no-register", "--install-dir", str(target)])
    install.install_binary_bundle(args, info)
    (target / "stale.txt").write_text("from an older version")
    install.install_binary_bundle(args, info)
    assert not (target / "stale.txt").exists() and (target / "_internal" / "lib.so").exists()


@posix_only
def test_binary_install_refuses_a_folder_it_did_not_create(bundle, tmp_path):
    _, info = bundle
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("keep")
    args = install.parse_args(["--no-register", "--install-dir", str(foreign)])
    with pytest.raises(install.InstallError, match="not created by this installer"):
        install.install_binary_bundle(args, info)
    assert (foreign / "precious.txt").exists()


@posix_only
def test_binary_install_accepts_an_existing_empty_folder(bundle, tmp_path):
    _, info = bundle
    empty = tmp_path / "empty"
    empty.mkdir()
    install.install_binary_bundle(install.parse_args(["--no-register", "--install-dir", str(empty)]), info)
    assert (empty / install.MARKER).exists()


def test_binary_install_dry_run_touches_nothing(bundle, tmp_path):
    _, info = bundle
    target = tmp_path / "inst"
    args = install.parse_args(["--dry-run", "--no-register", "--install-dir", str(target)])
    install.install_binary_bundle(args, info)
    assert not target.exists() and not Path(str(target) + ".installing").exists()


def test_binary_install_reports_a_missing_program_folder(bundle, tmp_path):
    root, info = bundle
    import shutil
    shutil.rmtree(root / "diamond-mcp")
    args = install.parse_args(["--no-register", "--install-dir", str(tmp_path / "inst")])
    with pytest.raises(install.InstallError, match="folder is missing"):
        install.install_binary_bundle(args, info)


@posix_only
def test_binary_install_reports_a_program_that_does_not_start(bundle, tmp_path):
    root, info = bundle
    (root / "diamond-mcp" / "diamond-mcp").write_text('#!/bin/sh\necho "version GLIBC_2.35 not found" >&2; exit 1\n')
    args = install.parse_args(["--no-register", "--install-dir", str(tmp_path / "inst")])
    with pytest.raises(install.InstallError, match="GLIBC_2.35"):
        install.install_binary_bundle(args, info)


@posix_only
def test_do_install_chooses_the_binary_path_from_buildinfo(bundle, tmp_path, capsys):
    _, info = bundle
    target = tmp_path / "inst"
    install.do_install(install.parse_args(["--no-register", "--install-dir", str(target)]), info)
    out = capsys.readouterr().out
    assert "self-contained" in out and "pip" not in out and str(target / "diamond-mcp") in out


@posix_only
def test_binary_uninstall_removes_registration_and_folder(bundle, tmp_path, fake_claude):
    claude, state = fake_claude
    _, info = bundle
    target = tmp_path / "inst"
    install.install_binary_bundle(install.parse_args(["--no-register", "--install-dir", str(target)]), info)
    state.write_text("diamond\t%s\nother\t/x\n" % install.binary_server(str(target)))
    install.do_uninstall(install.parse_args(["--uninstall", "--install-dir", str(target), "--claude-path", claude]))
    assert not target.exists() and state.read_text() == "other\t/x\n"


def test_uninstall_targets_follow_the_kind_of_installation(bundle, tmp_path):
    root, _ = bundle
    args = install.parse_args(["--uninstall"])
    assert install.uninstall_targets(args) == [("binary", install.default_install_dir())]
    (root / "BUILDINFO").write_text(json.dumps({"kind": "wheels"}))
    assert install.uninstall_targets(args) == [("wheels", install.default_venv_dir())]
    (root / "BUILDINFO").unlink()
    assert [k for k, _ in install.uninstall_targets(args)] == ["binary", "wheels"]  # unknown kind: look at both
    explicit = install.parse_args(["--uninstall", "--install-dir", str(tmp_path / "a")])
    assert install.uninstall_targets(explicit) == [("binary", str(tmp_path / "a"))]


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent))
    import test_installer
    script = tmp_path / "claude"
    script.write_text(test_installer.FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "state"
    state.write_text("")
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    return str(script), state


# ----------------------------------------------- end to end (needs uv and a network)

@pytest.mark.skipif(not os.environ.get("DIAMOND_TEST_BINARY"), reason="set DIAMOND_TEST_BINARY=1 (needs uv and network access)")
def test_build_the_real_standalone_installer(tmp_path):
    done = subprocess.run([sys.executable, str(SCRIPTS / "build_binary.py"), "--outdir", str(tmp_path)],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert done.returncode == 0, done.stdout
    zips = list(tmp_path.glob("lattice-diamond-mcp-*.zip"))
    assert len(zips) == 1 and (tmp_path / (zips[0].name + ".sha256")).exists()
    with zipfile.ZipFile(zips[0]) as archive:
        names = archive.namelist()
        legal_name = next(n for n in names if n.endswith("/LEGAL_REVIEW.txt"))
        assert any("/THIRD_PARTY_LICENSES/pyinstaller-" in n and n.endswith("COPYING.txt") for n in names)
        licence_name = next(n for n in names if n.endswith("/LICENSE") and n.count("/") == 1)
        assert archive.read(licence_name) == (REPO / "LICENSE").read_bytes()
        assert json.loads(archive.read(next(n for n in names if n.endswith("/BUILDINFO"))))["license_sha256"] == build.sha256_file(REPO / "LICENSE")
        legal_text = archive.read(legal_name).decode()
        assert "Bootloader Exception" in legal_text and "WARNING" not in legal_text  # the real PyInstaller still says it
        assert archive.read(next(n for n in names if n.endswith("/THIRD_PARTY_LICENSES.txt"))).decode().startswith("See LEGAL_REVIEW.txt")
    sbom = json.loads(next(tmp_path.glob("*.cdx.json")).read_text())
    assert build.validate_sbom(sbom) > 20
    assert any(c["type"] == "file" for c in sbom["components"]) and any(c["name"] == "cpython" for c in sbom["components"])
