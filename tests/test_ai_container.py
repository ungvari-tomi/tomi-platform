"""Private analyst + Streamlit, disposable reports only; never use managed storage."""

import os
import subprocess
from uuid import uuid4

import pytest
from test_ai_tools import tools as tools
from test_catalog_reporting import warehouse as warehouse
from test_reporting import sales_warehouse as sales_warehouse

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_ANALYST_IMAGE") != "1",
    reason="Opt in to disposable analyst/dashboard validation images",
)


def test_isolated_agent_and_conversational_dashboard(tools):
    name = "analyst-test-" + uuid4().hex
    network, owner, service = name + "-net", name + "-owner", name + "-service"

    def docker(*args, check=True):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    try:
        docker("network", "create", "--internal", network)
        docker(
            "create",
            "--name",
            owner,
            "--network",
            "none",
            "--mount",
            "type=volume,target=/reports",
            "retailflow-analyst:validation",
        )
        docker("cp", str(tools.reports.root) + "/.", owner + ":/reports")
        # Ownership changes are confined to this disposable anonymous report volume.
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--volumes-from",
            owner,
            "--entrypoint",
            "python",
            "retailflow-analyst:validation",
            "-c",
            "import os; from pathlib import Path; "
            "paths=[Path('/reports'),*Path('/reports').rglob('*')]; "
            "[(os.chown(p,10001,10001),os.chmod(p,0o750 if p.is_dir() else 0o640)) for p in paths]",
        )
        docker(
            "run",
            "--detach",
            "--name",
            service,
            "--network",
            network,
            "--network-alias",
            "retailflow-analyst",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "512m",
            "--cpus",
            "0.5",
            "--volumes-from",
            owner + ":ro",
            "retailflow-analyst:validation",
        )
        probe = """
import json,os,time,urllib.request
assert os.getuid()==10001
for _ in range(40):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=2) as r:
            assert json.load(r)['provider']=='mock'
        break
    except OSError: time.sleep(0.1)
else: raise AssertionError('private analyst not ready')
assert not os.path.exists('/warehouse') and not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/app/retailflow') and not os.path.exists('/app/.env')
try: open('/reports/forbidden-write','w')
except OSError: pass
else: raise AssertionError('report volume writable')
"""
        docker("exec", service, "python", "-c", probe)
        ui = """
import os
from streamlit.testing.v1 import AppTest
app=AppTest.from_file('/app/app.py').run(timeout=30)
assert not app.exception and 'AI business analyst' in app.sidebar.radio[0].options
app.sidebar.radio[0].set_value('AI business analyst').run()
assert not app.exception
assert 'mock' in str([c.value for c in app.caption])
app.chat_input[0].set_value('Which countries generated the most sales?').run(timeout=60)
assert not app.exception and len(app.chat_message)==2
assert '[e1]' in str([t.value for t in app.text])
app.chat_input[0].set_value('What about Hungary?').run(timeout=60)
assert not app.exception and len(app.chat_message)==4
assert 'unavailable' in str([t.value for t in app.text])
app.sidebar.radio[0].set_value('Sales overview').run()
assert not app.exception and app.metric[1].value=='3'
os.environ['RETAILFLOW_AI_ENABLED']='false'
app=AppTest.from_file('/app/app.py').run()
assert not app.exception
assert app.sidebar.radio[0].options==['Sales overview','Catalog and inventory']
print('PASS private analyst, chat follow-up, exact evidence, preserved sales and disabled mode')
"""
        result = docker(
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",
            "--volumes-from",
            owner + ":ro",
            "-e",
            "RETAILFLOW_AI_ENABLED=true",
            "--entrypoint",
            "python",
            "retailflow-dashboard:validation",
            "-c",
            ui,
        )
        assert "PASS private analyst" in result.stdout
        docker("stop", "--time", "3", service)
        unavailable = """
from streamlit.testing.v1 import AppTest
app=AppTest.from_file('/app/app.py').run()
app.sidebar.radio[0].set_value('AI business analyst').run(timeout=30)
assert not app.exception and app.warning
app.sidebar.radio[0].set_value('Sales overview').run()
assert not app.exception and app.metric[1].value=='3'
"""
        docker(
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",
            "--volumes-from",
            owner + ":ro",
            "-e",
            "RETAILFLOW_AI_ENABLED=true",
            "--entrypoint",
            "python",
            "retailflow-dashboard:validation",
            "-c",
            unavailable,
        )
    finally:
        docker("rm", "--force", service, check=False)
        docker("rm", "--force", "--volumes", owner, check=False)
        docker("network", "rm", network, check=False)
