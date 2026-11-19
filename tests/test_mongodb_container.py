"""Real Community MongoDB security, validation and recovery in disposable storage."""

import json
import os
import subprocess
import time
from uuid import uuid4

import pytest

from infra.mongodb.catalog import init_secrets

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_MONGODB_IMAGE") != "1",
    reason="Opt in to isolated MongoDB Community/catalog images",
)


def test_real_catalog_security_model_restart_and_backup_restore(tmp_path):
    prefix = "mongo-fixture-" + uuid4().hex
    server = prefix + "-server"
    worker = prefix + "-worker"
    secrets = tmp_path / "secrets"
    init_secrets(secrets)
    secret_owners = []
    secret_volumes = {}

    def docker(*args, check=True, timeout=180):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode and "/opt/catalog/backup.js" in args:
            diagnostic = subprocess.run(
                ["docker", "exec", server, "cat", "/tmp/catalog-tool-error.log"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            result.stderr += diagnostic.stdout
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def client(role, script):
        return docker(
            "run",
            "--rm",
            "--name",
            worker,
            "--network",
            prefix,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            f"type=volume,source={secret_volumes[role]},target=/run/secrets,readonly",
            "--entrypoint",
            "python",
            "retailflow-catalog:validation",
            "-c",
            script,
        )

    def ready():
        for _ in range(60):
            r = docker(
                "exec",
                server,
                "mongosh",
                "--quiet",
                "--nodb",
                "--file",
                "/opt/catalog/health.js",
                check=False,
                timeout=20,
            )
            if r.returncode == 0:
                return
            time.sleep(1)
        pytest.fail("Disposable MongoDB authenticated health did not become ready")

    docker("network", "create", "--internal", prefix)
    try:
        # Only this network-disabled fixture initializer is root. Workloads stay non-root.
        for role in ["server", "admin", "reader", "writer"]:
            owner = prefix + "-" + role + "-secrets"
            secret_owners.append(owner)
            files = (
                ["admin-user", "admin-password", "reader.json", "writer.json"]
                if role == "server"
                else [role + ".json"]
            )
            uid = 999 if role == "server" else 10001
            initialize = f"""
import os,shutil
from pathlib import Path
target=Path('/run/secrets');target.chmod(0o700)
for name in {files!r}:
 destination=target/(name if {role!r}=='server' else 'catalog.json')
 shutil.copyfile(Path('/input')/name,destination)
 destination.chmod(0o400);os.chown(destination,{uid},{uid})
os.chown(target,{uid},{uid})
"""
            docker(
                "run",
                "--name",
                owner,
                "--network",
                "none",
                "--user",
                "0:0",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges:true",
                "--mount",
                f"type=bind,source={secrets},target=/input,readonly",
                "--mount",
                "type=volume,target=/run/secrets",
                "--entrypoint",
                "python",
                "retailflow-catalog:validation",
                "-c",
                initialize,
            )
            mounts = json.loads(docker("inspect", owner, "--format", "{{json .Mounts}}").stdout)
            secret_volumes[role] = next(
                m["Name"] for m in mounts if m["Destination"] == "/run/secrets"
            )
        command = [
            "run",
            "-d",
            "--name",
            server,
            "--network",
            prefix,
            "--network-alias",
            "retailflow-mongodb",
            "--read-only",
            "--user",
            "999:999",
            "--memory",
            "1g",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m,uid=999,gid=999",
            "--tmpfs",
            "/data/configdb:rw,nosuid,size=16m,uid=999,gid=999",
            "--mount",
            "type=volume,target=/data/db",
            "--mount",
            "type=volume,target=/backups",
            "-e",
            # Public file path only; not credential content.
            # pragma: allowlist nextline secret
            "MONGO_INITDB_ROOT_USERNAME_FILE=/run/secrets/admin-user",
            "-e",
            # Public file path only; not credential content.
            # pragma: allowlist nextline secret
            "MONGO_INITDB_ROOT_PASSWORD_FILE=/run/secrets/admin-password",
        ]
        command += [
            "--mount",
            f"type=volume,source={secret_volumes['server']},target=/run/secrets,readonly",
        ]
        docker(*command, "retailflow-mongodb:validation")
        ready()
        state = json.loads(docker("inspect", server).stdout)[0]
        assert state["Config"]["User"] == "999:999" and state["HostConfig"]["ReadonlyRootfs"]
        assert not state["HostConfig"].get("PortBindings")
        credential_values = [
            json.loads((secrets / (role + ".json")).read_text())["password"]
            for role in ["admin", "writer", "reader"]
        ]
        config_leaked = any(value in json.dumps(state) for value in credential_values)
        assert not config_leaked, "Credential leaked into Docker configuration"
        empty_verify = """
import subprocess
r=subprocess.run(['python','/app/catalog.py','verify'],capture_output=True,text=True)
assert r.returncode != 0
"""
        client("reader", empty_verify)

        writer_script = """
import copy,os,secrets
from pathlib import Path
from datetime import UTC,datetime
from pymongo.errors import OperationFailure,WriteError,DuplicateKeyError
from pymongo import MongoClient
from catalog import Catalog,connect,documents
assert os.getuid()==10001
with connect('writer',Path('/run/secrets/catalog.json')) as c:
 p=Catalog(c)
 assert p.seed()==20 and p.seed()==0
 assert p.products.count_documents({})==20
 invalid=copy.deepcopy(documents()[0]);invalid['attributes']={'fabric':'cotton'}
 duplicate_variant=copy.deepcopy(documents()[0])
 duplicate_variant['variants'][1]['sku']=duplicate_variant['variants'][0]['sku']
 for change in [invalid,duplicate_variant,{**documents()[0],'schema_version':2},
                {**documents()[0],'email':'forbidden@example.invalid'},
                {**documents()[0],'created_at':'not-a-date'}]:
  try:p.products.replace_one({'_id':'product-001'},change)
  except WriteError as error:assert error.code==121
  else:raise AssertionError('Invalid document was accepted')
 try:p.products.update_one({'_id':'product-002'},{'$set':{'sku':'RF-0001'}})
 except DuplicateKeyError:pass
 else:raise AssertionError('Unique SKU was not enforced')
 for command in [{'collMod':'products','validationAction':'warn'},{'drop':'products'},
                 {'createUser':'forbidden','pwd':secrets.token_urlsafe(16),'roles':[]}]:
  try:c.retailflow.command(command)
  except OperationFailure as error:assert error.code==13
  else:raise AssertionError('Writer can administer catalog')
 try:p.products.delete_one({'_id':'product-001'})
 except OperationFailure as error:assert error.code==13
 else:raise AssertionError('Writer can physically delete')
 assert p.retire('product-001',1,now=datetime(2026,11,19,tzinfo=UTC))
 assert not p.retire('product-001',1,now=datetime(2026,11,19,tzinfo=UTC))
 assert p.seed()==0 and p.get('product-001')['revision']==2
 assert p.get('product-001')['status']=='retired'
 found=p.products.find_one({'variants':{'$elemMatch':{'sku':'RF-0002-A','option':'M'}}})
 assert found['_id']=='product-002'
print('PASS writer, deterministic seed, validation, CAS, soft delete and nested queries')
"""
        assert "PASS writer" in client("writer", writer_script).stdout
        reader_script = """
from pathlib import Path
from pymongo import MongoClient
from pymongo.errors import OperationFailure
from catalog import Catalog,connect
import json,subprocess
r=subprocess.run(['python','/app/catalog.py','verify'],capture_output=True,text=True)
assert r.returncode==0 and json.loads(r.stdout)['total']==20
with connect('reader',Path('/run/secrets/catalog.json')) as c:
 p=Catalog(c)
 assert p.products.count_documents({})==20
 assert sum(row['products'] for row in p.summary())==19
 assert len(p.market_products('electronics','HU'))==6
 for action in [lambda:p.products.insert_one({'_id':'product-020'}),
                lambda:p.products.update_one({'_id':'product-002'},{'$set':{'status':'retired'}}),
                lambda:p.products.delete_one({'_id':'product-002'}),
                lambda:c.retailflow.command({'collMod':'products','validationAction':'warn'})]:
  try:action()
  except OperationFailure as error:assert error.code==13
  else:raise AssertionError('Reader can mutate/administer')
 query={'find':'products','filter':{'category':'electronics','markets':'HU','status':'active'}}
 hinted=c.retailflow.command('explain',{**query,'hint':'category_market_status'},verbosity='executionStats')
 scan=c.retailflow.command('explain',{**query,'hint':{'$natural':1}},verbosity='executionStats')
 assert hinted['executionStats']['nReturned']==scan['executionStats']['nReturned']==6
 assert hinted['executionStats']['totalKeysExamined']>0
 assert hinted['executionStats']['totalDocsExamined']<=scan['executionStats']['totalDocsExamined']
 assert 'IXSCAN' in str(hinted)
with MongoClient('retailflow-mongodb',27017,serverSelectionTimeoutMS=5000) as unauth:
 try:unauth.retailflow.products.find_one({})
 except OperationFailure as error:assert error.code==13
 else:raise AssertionError('Unauthenticated read accepted')
print('PASS reader, unauthenticated denial, aggregate and indexed explain')
"""
        assert "PASS reader" in client("reader", reader_script).stdout
        docker("restart", "--timeout", "60", server)
        ready()
        assert "PASS reader" in client("reader", reader_script).stdout
        refused = docker(
            "exec",
            server,
            "mongosh",
            "--quiet",
            "--nodb",
            "--file",
            "/opt/catalog/backup.js",
            check=False,
        )
        assert (
            refused.returncode != 0
        )  # Backups require explicit writer-quiescence acknowledgement.
        backup = json.loads(
            docker(
                "exec",
                "-e",
                "CATALOG_WRITERS_QUIESCED=true",
                server,
                "mongosh",
                "--quiet",
                "--nodb",
                "--file",
                "/opt/catalog/backup.js",
            ).stdout.strip()
        )
        restored = json.loads(
            docker(
                "exec",
                "-e",
                "CATALOG_BACKUP_ACTION=restore",
                "-e",
                "CATALOG_BACKUP_BUNDLE=" + backup["bundle"],
                server,
                "mongosh",
                "--quiet",
                "--nodb",
                "--file",
                "/opt/catalog/backup.js",
            ).stdout.strip()
        )
        target = restored["database"]
        assert target.startswith("catalog_restore_")
        verify = f"""
from pathlib import Path
from catalog import connect
with connect('admin',Path('/run/secrets/catalog.json')) as c:
 db=c[{target!r}]
 assert db.products.count_documents({{}})==20
 assert db.products.find_one({{'_id':'product-001'}})['status']=='retired'
 options=db.command('listCollections',filter={{'name':'products'}})['cursor']['firstBatch'][0]['options']
 assert options['validationLevel']=='strict' and options['validationAction']=='error'
 assert 'sku_unique' in db.products.index_information()
 assert c.retailflow.products.count_documents({{}})==20
print('PASS new-target restore preserves documents, validation and indexes')
"""
        assert "PASS new-target restore" in client("admin", verify).stdout
        corrupt = (
            "require('fs').appendFileSync('/backups/"
            + backup["bundle"]
            + "/catalog.archive.gz', 'corrupt')"
        )
        docker("exec", server, "mongosh", "--quiet", "--nodb", "--eval", corrupt)
        rejected = docker(
            "exec",
            "-e",
            "CATALOG_BACKUP_ACTION=restore",
            "-e",
            "CATALOG_BACKUP_BUNDLE=" + backup["bundle"],
            server,
            "mongosh",
            "--quiet",
            "--nodb",
            "--file",
            "/opt/catalog/backup.js",
            check=False,
        )
        assert rejected.returncode != 0
        assert "PASS new-target restore" in client("admin", verify).stdout
        logs = docker("logs", server).stdout
        log_leaked = any(value in logs for value in credential_values)
        assert not log_leaked, "Credential leaked into MongoDB logs"
    finally:
        cleanup_errors = []
        for name in [worker, server, *secret_owners]:
            result = docker("rm", "--force", "--volumes", name, check=False)
            if result.returncode and "No such container" not in result.stderr:
                cleanup_errors.append(name)
        result = docker("network", "rm", prefix, check=False)
        if result.returncode and "not found" not in result.stderr:
            cleanup_errors.append(prefix)
        assert not cleanup_errors, "Disposable catalog cleanup did not complete"
