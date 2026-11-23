"""scripts/set_domain.py rewrites every reference to the GitHub Pages URL and the licence contact address
when an operator points NebuLens at their own domain. ROOT and FILES are module globals looked up fresh on
every call, so pointing them at a temp directory (instead of monkeypatching cwd) safely redirects every file
read/write the script does without it ever touching this repo's own files."""
import os

import pytest

import scripts.set_domain as sd

OLD_SITE = sd.OLD_SITE


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A miniature repo tree under tmp_path, with set_domain.py's ROOT/FILES redirected to it."""
    root = str(tmp_path)
    _write(os.path.join(root, "server.py"), f"SITE = '{OLD_SITE}'\n")
    _write(os.path.join(root, "static", "core.js"), f"const SITE = '{OLD_SITE}';\n")
    _write(os.path.join(root, "README.md"), f"See {OLD_SITE} and email licensing@old-example.com.\n")
    _write(os.path.join(root, "GETTING-STARTED.md"), "Nothing to rewrite here.\n")
    _write(os.path.join(root, "UPGRADE.md"), f"{OLD_SITE}\n")
    _write(os.path.join(root, "LICENSE"), "No mentions here.\n")
    _write(os.path.join(root, "docs", "index.html"), f"<a href='{OLD_SITE}'>home</a>\n")
    _write(os.path.join(root, "docs", "pricing.html"), "licensing@old-example.com\n")
    monkeypatch.setattr(sd, "ROOT", root)
    monkeypatch.setattr(sd, "FILES", ["server.py", "static/core.js", "README.md", "GETTING-STARTED.md",
                                       "UPGRADE.md", "LICENSE", "docs/index.html", "docs/pricing.html",
                                       "docs/does-not-exist.html"])
    return root


def _read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def test_rewrites_site_and_licensing_address(repo, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["set_domain.py", "nebulens.io"])
    sd.main()
    assert "https://nebulens.io/" in _read(repo, "server.py")
    assert "https://nebulens.io/" in _read(repo, "static/core.js")
    assert "licensing@old-example.com" not in _read(repo, "README.md")
    assert "licensing@nebulens.io" in _read(repo, "README.md")
    assert "https://nebulens.io/" in _read(repo, "docs/index.html")
    assert "licensing@nebulens.io" in _read(repo, "docs/pricing.html")

    out = capsys.readouterr().out
    assert "server.py" in out and "docs/CNAME" in out
    assert "185.199.108.153" in out and "CNAME  www   syedammarsuhail.github.io" in out
    assert "nebulens.io" in out


def test_www_subdomain_uses_apex_for_licensing_address(repo, monkeypatch):
    monkeypatch.setattr("sys.argv", ["set_domain.py", "www.nebulens.io"])
    sd.main()
    assert "https://www.nebulens.io/" in _read(repo, "server.py")          # full host for the site
    assert "licensing@nebulens.io" in _read(repo, "README.md")            # apex (no "www.") for the address
    assert _read(repo, os.path.join("docs", "CNAME")).strip() == "www.nebulens.io"


def test_untouched_files_are_not_reported_changed(repo, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["set_domain.py", "nebulens.io"])
    sd.main()
    out = capsys.readouterr().out
    updated_line = next(ln for ln in out.splitlines() if ln.startswith("updated:"))
    assert "GETTING-STARTED.md" not in updated_line          # had no OLD_SITE / licensing@ to rewrite
    assert "LICENSE" not in updated_line
    assert "does-not-exist.html" not in updated_line         # listed in FILES but missing on disk: skipped, no crash


def test_missing_file_in_the_list_is_skipped_without_error(repo, monkeypatch):
    monkeypatch.setattr("sys.argv", ["set_domain.py", "nebulens.io"])
    sd.main()  # docs/does-not-exist.html is in FILES but was never written; must not raise


@pytest.mark.parametrize("bad", ["", "not a domain", "nebulens", "a" * 260 + ".com"])
def test_invalid_domain_exits_with_usage(repo, monkeypatch, bad):
    # Note: the domain regex accepts a few shapes that aren't strictly valid DNS labels (e.g. a leading
    # hyphen); this only checks the cases it does reject, not a full RFC validation audit.
    monkeypatch.setattr("sys.argv", ["set_domain.py", bad])
    with pytest.raises(SystemExit) as exc:
        sd.main()
    assert "Point NebuLens" in str(exc.value)                 # the module docstring is the usage message


def test_wrong_argument_count_exits_with_usage(repo, monkeypatch):
    monkeypatch.setattr("sys.argv", ["set_domain.py"])
    with pytest.raises(SystemExit):
        sd.main()
    monkeypatch.setattr("sys.argv", ["set_domain.py", "a.com", "b.com"])
    with pytest.raises(SystemExit):
        sd.main()


def test_domain_is_lowercased(repo, monkeypatch):
    monkeypatch.setattr("sys.argv", ["set_domain.py", "NebuLens.IO"])
    sd.main()
    assert "https://nebulens.io/" in _read(repo, "server.py")
    assert _read(repo, os.path.join("docs", "CNAME")).strip() == "nebulens.io"
