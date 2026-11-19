"""NebuLens was called Pulse0, then briefly Oscilyx: the old settings, data files and browser-script URLs must keep working."""
import os
import subprocess
import sys


def test_old_setting_names_are_read():
    code = ("import os; os.environ['PULSE0_RETENTION_HOURS']='7'; os.environ['PULSE0_CACHE_TTL']='3';"
            "os.environ['NEBULENS_CACHE_TTL']='5'; import server;"
            "print(os.environ['NEBULENS_RETENTION_HOURS'], os.environ['NEBULENS_CACHE_TTL'], os.environ['PULSE0_CACHE_TTL'])")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PULSE0_", "NEBULENS_"))}
    env["NEBULENS_DB"] = os.environ["NEBULENS_DB"]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), check=True).stdout.split()
    assert out[-3:] == ["7", "5", "5"]   # old name copied over; the new name wins when both are set


def test_existing_pulse0_db_is_used(srv, tmp_path):
    new = tmp_path / "nebulens.db"
    assert srv._legacy_db(str(new)) == str(new)          # fresh install: the new name
    (tmp_path / "pulse0.db").write_bytes(b"")
    assert srv._legacy_db(str(new)) == str(tmp_path / "pulse0.db")   # upgraded install: keeps its data
    (tmp_path / "oscilyx.db").write_bytes(b"")
    assert srv._legacy_db(str(new)) == str(tmp_path / "oscilyx.db")  # the newer of the old names first
    new.write_bytes(b"")
    assert srv._legacy_db(str(new)) == str(new)          # once nebulens.db exists it wins
    other = tmp_path / "custom.db"
    assert srv._legacy_db(str(other)) == str(other)      # an explicit custom path is never redirected


def test_old_browser_script_url(client):
    new = client.get("/sdk/nebulens-rum.js")
    assert new.status_code == 200
    for old_url in ("/sdk/pulse0-rum.js", "/sdk/oscilyx-rum.js"):
        old = client.get(old_url)
        assert old.status_code == 200 and old.content == new.content
    assert b"window.pulse0 = window.nebulens" in new.content
