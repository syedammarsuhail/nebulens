"""Oscilyx was called Pulse0: the old settings, data file, headers and browser-script URL must keep working."""
import os
import subprocess
import sys


def test_old_setting_names_are_read():
    code = ("import os; os.environ['PULSE0_RETENTION_HOURS']='7'; os.environ['PULSE0_CACHE_TTL']='3';"
            "os.environ['OSCILYX_CACHE_TTL']='5'; import server;"
            "print(os.environ['OSCILYX_RETENTION_HOURS'], os.environ['OSCILYX_CACHE_TTL'], os.environ['PULSE0_CACHE_TTL'])")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PULSE0_", "OSCILYX_"))}
    env["OSCILYX_DB"] = os.environ["OSCILYX_DB"]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), check=True).stdout.split()
    assert out[-3:] == ["7", "5", "5"]   # old name copied over; the new name wins when both are set


def test_existing_pulse0_db_is_used(srv, tmp_path):
    new = tmp_path / "oscilyx.db"
    assert srv._legacy_db(str(new)) == str(new)          # fresh install: the new name
    (tmp_path / "pulse0.db").write_bytes(b"")
    assert srv._legacy_db(str(new)) == str(tmp_path / "pulse0.db")   # upgraded install: keeps its data
    new.write_bytes(b"")
    assert srv._legacy_db(str(new)) == str(new)          # once oscilyx.db exists it wins
    other = tmp_path / "custom.db"
    assert srv._legacy_db(str(other)) == str(other)      # an explicit custom path is never redirected


def test_old_browser_script_url(client):
    old = client.get("/sdk/pulse0-rum.js")
    new = client.get("/sdk/oscilyx-rum.js")
    assert old.status_code == new.status_code == 200
    assert old.content == new.content
    assert b"window.pulse0 = window.oscilyx" in new.content
