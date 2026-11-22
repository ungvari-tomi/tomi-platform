"""Explicit optional real-model evaluation; no download or LLM is required by CI."""

import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from test_ai_tools import tools as tools
from test_catalog_reporting import warehouse as warehouse
from test_reporting import sales_warehouse as sales_warehouse

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_LOCAL_MODEL") != "1",
    reason="Explicitly opt in to downloading/evaluating the pinned 1.4 GB local model",
)


def test_real_local_model_understands_paraphrases_and_followups(tools, tmp_path):
    name = "ollama-eval-" + uuid4().hex
    owner, runtime, network = name + "-owner", name + "-runtime", name + "-net"
    cache = os.environ.get("RETAILFLOW_TEST_MODEL_VOLUME")
    if cache:
        assert re.fullmatch(r"ollama-eval-cache-[a-f0-9]{32}", cache)

    def docker(*args, timeout=240, check=True):
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
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
            f"type=volume,source={cache},target=/models" if cache else "type=volume,target=/models",
            "--mount",
            "type=volume,target=/reports",
            "retailflow-ollama:validation",
        )
        docker("cp", str(tools.reports.root) + "/.", owner + ":/reports")
        mounts = json.loads(docker("inspect", "--format", "{{json .Mounts}}", owner).stdout)
        model_volume = next(m["Name"] for m in mounts if m["Destination"] == "/models")
        report_volume = next(m["Name"] for m in mounts if m["Destination"] == "/reports")
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--mount",
            f"type=volume,source={report_volume},target=/reports",
            "--entrypoint",
            "/bin/sh",
            "retailflow-ollama:validation",
            "-c",
            "chown -R 10001:10001 /reports; chmod -R u+rX /reports",
        )
        seed = docker(
            "run",
            "--name",
            name + "-seed",
            "--read-only",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--mount",
            f"type=volume,source={model_volume},target=/models",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m,uid=10001,gid=10001",
            "--entrypoint",
            "/bin/sh",
            "retailflow-ollama:validation",
            "/app/seed.sh",
            timeout=1200,
            check=False,
        )
        if seed.returncode:
            root = Path(__file__).resolve().parents[1] / "artifacts/ai-evaluation"
            root.mkdir(parents=True, exist_ok=True)
            (root / "seed-failure.log").write_text(seed.stdout + seed.stderr, encoding="utf-8")
        assert seed.returncode == 0, (seed.stdout + seed.stderr)[-1200:]
        assert "OK" in seed.stdout
        # Runtime gets a read-only model mount and no internet connection.
        docker(
            "run",
            "--detach",
            "--name",
            runtime,
            "--network",
            network,
            "--network-alias",
            "retailflow-ollama",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "3g",
            "--memory-swap",
            "3g",
            "--cpus",
            "2",
            "--mount",
            f"type=volume,source={model_volume},target=/models,readonly",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m,uid=10001,gid=10001",
            "retailflow-ollama:validation",
        )
        info = json.loads(docker("inspect", runtime).stdout)[0]
        env = dict(value.split("=", 1) for value in info["Config"]["Env"])
        assert env["OLLAMA_NO_CLOUD"] == "1"
        assert set(info["NetworkSettings"]["Networks"]) == {network}
        assert not info["HostConfig"].get("PortBindings")
        assert all(not mount["RW"] for mount in info["Mounts"])
        script = """
import json,time
from datetime import date
from pathlib import Path
import httpx
from retailflow_ai.agent import Agent,Question,HistoryItem
from retailflow_ai.contracts import AnalysisContext
from retailflow_ai.providers import JsonProvider
from retailflow_ai.snapshots import Reports
endpoint='http://retailflow-ollama:11434'
with httpx.Client(trust_env=False,timeout=5) as client:
    for _ in range(60):
        try:
            r=client.get(endpoint+'/api/tags'); r.raise_for_status(); break
        except httpx.HTTPError: time.sleep(0.5)
    else: raise AssertionError('Ollama unavailable')
    models=r.json()['models']
    expected={
      '8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7', # pragma: allowlist secret
      '0f45bae8cd2fec097cf09b400b078cf42388a1f5702f4058d2d6d641a09db46d', # pragma: allowlist secret
    }
    assert models[0]['digest'] in expected
context=AnalysisContext(source='catalog',dataset_id='profile-a',start_date=date(2026,11,19),end_date=date(2026,11,19),inventory_seed=42,inventory_interval_seconds=600)
class Recording(JsonProvider):
    def decide(self,*args):
        result=super().decide(*args)
        self.last=result.model_dump()
        return result
provider=Recording('ollama','qwen3:1.7b',endpoint=endpoint+'/api/chat')
agent=Agent(provider,total_seconds=180,decision_seconds=60)
cases=[
 ('markets','Rank the markets by their euro takings.','country_performance'),
 ('items','Which items were ordered most frequently?','product_performance'),
 ('coverage','How much of this turnover can we actually value in EUR?',
  ('fx_coverage','sales_summary')),
]
results=[]
previous=None
for name,text,expected in cases:
    answer=agent.run(Question(question=text,context=context),Reports(Path('/reports')))
    selected=[t.tool for t in answer.trace]
    results.append({'case':name,'status':answer.status,'tools':selected,
      'latency_ms':answer.latency_ms,'text':answer.text,
      'trace':[t.model_dump() for t in answer.trace], 'last_decision':provider.last})
    assert answer.status=='answered', results
    accepted=(expected,) if isinstance(expected,str) else expected
    assert any(tool in selected for tool in accepted), results
    assert answer.evidence and '[e1]' in answer.text, results
    metric_facts=[r.facts.get('totals',r.facts) for r in answer.evidence.values()
                  if r.tool in accepted]
    assert metric_facts and all(f.get('orders')==3 for f in metric_facts), results
    assert all(f.get('fx_covered_orders')==2 for f in metric_facts), results
    assert all(f.get('fx_uncovered_orders')==1 for f in metric_facts), results
    if name=='markets': previous=(text,answer)
text,first=previous
follow=agent.run(Question(question='Just Hungary please.',context=context,history=[
    HistoryItem(role='user',content=text),
    HistoryItem(role='assistant',content=first.text[:4000])]),Reports(Path('/reports')))
results.append({'case':'followup','status':follow.status,'tools':[t.tool for t in follow.trace],
                'latency_ms':follow.latency_ms,'text':follow.text,
                'trace':[t.model_dump() for t in follow.trace]})
assert follow.status=='answered', results
assert any(t.arguments.get('countries')==['HU'] for t in follow.trace), results
facts=[r.facts.get('totals',r.facts) for r in follow.evidence.values()]
assert any(f.get('fx_uncovered_orders')==1 for f in facts),results
assert any(f.get('covered_revenue_eur_minor') is None for f in facts),results
assert 'DE:' not in follow.text and '1 observed orders' in follow.text,results
print('RESULT:'+json.dumps({'model':'qwen3:1.7b','cases':results,'passed':len(results)}))
"""
        result = docker(
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--mount",
            f"type=volume,source={report_volume},target=/reports,readonly",
            "--entrypoint",
            "python",
            "retailflow-analyst:validation",
            "-c",
            script,
            timeout=900,
        )
        report = json.loads(
            next(line[7:] for line in result.stdout.splitlines() if line.startswith("RESULT:"))
        )
        root = Path(__file__).resolve().parents[1] / "artifacts/ai-evaluation"
        root.mkdir(parents=True, exist_ok=True)
        (root / "ollama-local.json").write_text(json.dumps(report, indent=2))
        assert report["passed"] == 4
    finally:
        docker("rm", "--force", name + "-seed", check=False)
        docker("rm", "--force", runtime, check=False)
        docker("rm", "--force", "--volumes", owner, check=False)
        docker("network", "rm", network, check=False)
