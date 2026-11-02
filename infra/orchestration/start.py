"""Initialize local Airflow authentication and launch one explicit component."""

import argparse
import json
import os
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["init", "api-server", "scheduler", "dag-processor"])
    args = parser.parse_args()
    executable = shutil.which("airflow")
    if not executable:
        raise RuntimeError("Airflow executable is missing")
    path = Path("/opt/airflow/auth/passwords.json")
    path.write_text(
        json.dumps({"admin": os.environ["RETAILFLOW_AIRFLOW_PASSWORD"]}), encoding="utf-8"
    )
    path.chmod(0o600)
    if args.role == "init":
        import psycopg2
        from psycopg2 import sql
        from psycopg2.extensions import parse_dsn

        parameters = parse_dsn(
            os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"].replace(
                "postgresql+psycopg2://", "postgresql://", 1
            )
        )
        password = parameters["password"]
        parameters.update(
            user="postgres", password=os.environ["RETAILFLOW_POSTGRES_ADMIN_PASSWORD"]
        )
        connection = psycopg2.connect(**parameters)
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = 'airflow'")
                if cursor.fetchone() is None:
                    cursor.execute(
                        sql.SQL("CREATE ROLE airflow LOGIN PASSWORD {}").format(
                            sql.Literal(password)
                        )
                    )
                cursor.execute(
                    "ALTER ROLE airflow NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
                cursor.execute("ALTER DATABASE airflow OWNER TO airflow")
        finally:
            connection.close()
        # Fixed executable and operation; no shell or user-supplied command.
        os.execv(executable, [executable, "db", "migrate"])  # nosec B606
    # The init container completes migrations before these processes become ready.
    import subprocess  # nosec B404

    subprocess.run(  # nosec B603
        [executable, "db", "check-migrations", "--migration-wait-timeout", "120"], check=True
    )
    # Role is restricted by argparse to the enumerated Airflow components.
    os.execv(executable, [executable, args.role])  # nosec B606


if __name__ == "__main__":
    main()
