"""Exercise the configured linter's style, parser and offline Jinja boundary."""

from pathlib import Path

from sqlfluff.core import FluffConfig, Linter

ROOT = Path(__file__).resolve().parents[1]


def linter() -> Linter:
    return Linter(config=FluffConfig.from_root(extra_config_path=str(ROOT / ".sqlfluff")))


def test_sql_capitalization_preserves_identifiers_and_quoted_values():
    query = (
        'select customer_id, count(*) as order_count, "select", '
        "'from' as example, cast(total_minor as bigint) as amount "
        "from customer_orders where active = true and deleted_at is null "
        'group by customer_id, total_minor, "select"'
    )
    result = linter().lint_string(query, fix=True)
    fixed, _ = result.fix_string()
    assert fixed == (
        'SELECT customer_id, COUNT(*) AS order_count, "select", '
        "'from' AS example, CAST(total_minor AS BIGINT) AS amount "
        "FROM customer_orders WHERE active = TRUE AND deleted_at IS NULL "
        'GROUP BY customer_id, total_minor, "select"'
    )
    assert not linter().lint_string(fixed).violations


def test_lowercase_keywords_and_invalid_sql_are_rejected():
    assert any(
        v.rule_code() == "CP01"
        for v in linter().lint_string("select order_id from orders").violations
    )
    assert any(v.rule_code() == "PRS" for v in linter().lint_string("SELECT FROM").violations)


def test_jinja_uses_defaults_without_reading_source_credentials(monkeypatch):
    monkeypatch.setenv("RETAILFLOW_SEED", "must-not-be-read")
    query = "SELECT {{ env_var('RETAILFLOW_SEED', '42') }} AS seed FROM {{ ref('stg_orders') }}"
    result = linter().lint_string(query)
    assert not result.violations
    assert "42" in result.templated_file.templated_str
    unknown = linter().lint_string("SELECT {{ undefined_function() }}")
    assert any(v.rule_code() == "TMP" for v in unknown.violations)
