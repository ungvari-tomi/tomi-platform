"""Synthetic product contracts and least-privilege operational catalog examples."""

import argparse
import copy
import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

MARKETS = ["HU", "DE", "FR", "PL", "CZ", "RO", "GB", "CH", "SE", "NO", "DK"]
IDENTITIES = {
    "admin": ("catalog_admin", "admin"),
    "reader": ("catalog_reader", "retailflow"),
    "writer": ("catalog_writer", "retailflow"),
}


def documents() -> list[dict]:
    result = []
    for number in range(1, 21):
        category = ["electronics", "apparel", "homeware"][(number - 1) % 3]
        attributes = {
            "electronics": {"power_watts": 10 + number, "warranty_months": 24},
            "apparel": {"fabric": "cotton", "care_temperature_c": 30},
            "homeware": {"material": "ceramic", "dishwasher_safe": True},
        }[category]
        sku = f"RF-{number:04d}"
        created = datetime(2024, 12, 1, tzinfo=UTC) + timedelta(minutes=number)
        result.append(
            {
                "_id": f"product-{number:03d}",
                "sku": sku,
                "name": f"Synthetic {category} item {number:03d}",
                "category": category,
                "attributes": attributes,
                "variants": [
                    {"sku": sku + suffix, "option": option}
                    for suffix, option in zip(
                        ["-A", "-B"],
                        ["M", "L"] if category == "apparel" else ["black", "white"],
                        strict=True,
                    )
                ],
                "markets": list(MARKETS),
                "schema_version": 1,
                "revision": 1,
                "synthetic": True,
                "classification": "synthetic_product_metadata",
                "status": "active",
                "created_at": created,
                "updated_at": created,
            }
        )
    return result


def contract() -> dict:
    def obj(properties):
        return {
            "bsonType": "object",
            "required": list(properties),
            "additionalProperties": False,
            "properties": properties,
        }

    string = {"bsonType": "string", "minLength": 1, "maxLength": 120}
    schema = obj(
        {
            "_id": {"bsonType": "string", "pattern": "^product-(00[1-9]|01[0-9]|020)$"},
            "sku": {"bsonType": "string", "pattern": "^RF-00(0[1-9]|1[0-9]|20)$"},
            "name": string,
            "category": {"enum": ["electronics", "apparel", "homeware"]},
            "attributes": {"bsonType": "object"},
            "variants": {
                "bsonType": "array",
                "minItems": 1,
                "maxItems": 2,
                "uniqueItems": True,
                "items": obj(
                    {
                        "sku": {"bsonType": "string", "pattern": "^RF-00(0[1-9]|1[0-9]|20)-(A|B)$"},
                        "option": string,
                    }
                ),
            },
            "markets": {
                "bsonType": "array",
                "minItems": 1,
                "maxItems": 11,
                "uniqueItems": True,
                "items": {"enum": MARKETS},
            },
            "schema_version": {"bsonType": "int", "enum": [1]},
            "revision": {"bsonType": "int", "minimum": 1},
            "synthetic": {"enum": [True]},
            "classification": {"enum": ["synthetic_product_metadata"]},
            "status": {"enum": ["active", "retired"]},
            "created_at": {"bsonType": "date"},
            "updated_at": {"bsonType": "date"},
        }
    )
    shapes = {
        "electronics": obj(
            {
                "power_watts": {"bsonType": "int", "minimum": 1, "maximum": 500},
                "warranty_months": {"bsonType": "int", "enum": [12, 24, 36]},
            }
        ),
        "apparel": obj(
            {
                "fabric": {"enum": ["cotton", "linen"]},
                "care_temperature_c": {"bsonType": "int", "enum": [30, 40]},
            }
        ),
        "homeware": obj(
            {"material": {"enum": ["ceramic", "glass"]}, "dishwasher_safe": {"bsonType": "bool"}}
        ),
    }
    schema["oneOf"] = [
        {
            "properties": {
                "category": {"enum": [name]},
                "attributes": shape,
                "variants": {
                    "items": {
                        "properties": {
                            "option": {
                                "enum": ["M", "L"] if name == "apparel" else ["black", "white"]
                            }
                        }
                    }
                },
            }
        }
        for name, shape in shapes.items()
    ]
    variants = {"$cond": [{"$isArray": "$variants"}, "$variants", []]}
    variant_skus = {"$map": {"input": variants, "as": "variant", "in": "$$variant.sku"}}
    return {
        "validator": {
            "$and": [
                {"$jsonSchema": schema},
                {
                    "$expr": {
                        "$and": [
                            {"$gte": ["$updated_at", "$created_at"]},
                            {
                                "$eq": [
                                    {"$size": variants},
                                    {"$size": {"$setUnion": [variant_skus, []]}},
                                ]
                            },
                        ]
                    }
                },
            ]
        },
        "validationLevel": "strict",
        "validationAction": "error",
        "indexes": [
            {"key": {"sku": 1}, "name": "sku_unique", "unique": True},
            {"key": {"category": 1, "markets": 1, "status": 1}, "name": "category_market_status"},
            {"key": {"updated_at": 1, "_id": 1}, "name": "incremental_cursor"},
            {"key": {"variants.sku": 1}, "name": "variant_sku_unique", "unique": True},
        ],
    }


def init_secrets(directory: Path) -> None:
    if directory.exists():
        raise ValueError("Secret directory already exists; refusing credential replacement")
    directory.mkdir(parents=True, mode=0o700)
    for role, (username, source) in IDENTITIES.items():
        record = {
            "username": username,
            "password": secrets.token_urlsafe(36),
            "auth_source": source,
        }
        path = directory / (role + ".json")
        with path.open("x", encoding="utf-8") as output:
            json.dump(record, output)
        path.chmod(0o600)
        if role == "admin":
            for name, value in [("admin-user", username), ("admin-password", record["password"])]:
                target = directory / name
                target.write_text(value, encoding="utf-8")
                target.chmod(0o600)


def connect(role: str, path: Path, host: str = "retailflow-mongodb"):
    if host not in {"retailflow-mongodb", "127.0.0.1"}:
        raise ValueError("Only the approved local MongoDB endpoint is allowed")
    secret = json.loads(path.read_text(encoding="utf-8"))
    username, source = IDENTITIES[role]
    if set(secret) != {"username", "password", "auth_source"} or (
        secret["username"],
        secret["auth_source"],
    ) != (username, source):
        raise ValueError("Unexpected catalog credential identity")
    from pymongo import MongoClient

    return MongoClient(
        host,
        27017,
        username=username,
        password=secret["password"],
        authSource=source,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
        directConnection=True,
        tz_aware=True,
        retryWrites=False,
        w="majority",
        journal=True,
    )


class Catalog:
    def __init__(self, client):
        self.products = client.retailflow.products

    def seed(self) -> int:
        inserted = 0
        for doc in documents():
            inserted += int(
                self.products.update_one(
                    {"_id": doc["_id"]}, {"$setOnInsert": doc}, upsert=True
                ).upserted_id
                is not None
            )
        return inserted

    def get(self, identifier: str) -> dict | None:
        self.validate_identifier(identifier)
        return self.products.find_one({"_id": identifier})

    @staticmethod
    def validate_identifier(identifier: str) -> None:
        if not isinstance(identifier, str) or not re.fullmatch(
            r"product-(00[1-9]|01[0-9]|020)", identifier
        ):
            raise ValueError("Unsupported product identifier")

    def revise(self, identifier: str, revision: int, patch: dict, *, now: datetime) -> bool:
        self.validate_identifier(identifier)
        if type(revision) is not int or revision < 1:
            raise ValueError("A positive integer revision is required")
        if (
            not isinstance(patch, dict)
            or not patch
            or set(patch) - {"name", "attributes", "variants", "markets", "status"}
        ):
            raise ValueError("Only mutable catalog fields may be revised")
        if now.tzinfo is None:
            raise ValueError("A UTC-aware revision time is required")
        changes = copy.deepcopy(patch)
        changes["updated_at"] = now.astimezone(UTC)
        result = self.products.update_one(
            {"_id": identifier, "revision": revision, "updated_at": {"$lt": changes["updated_at"]}},
            {"$set": changes, "$inc": {"revision": 1}},
        )
        return result.modified_count == 1

    def retire(self, identifier: str, revision: int, *, now: datetime) -> bool:
        return self.revise(identifier, revision, {"status": "retired"}, now=now)

    def market_products(self, category: str, country: str) -> list[dict]:
        if category not in {"electronics", "apparel", "homeware"} or country not in MARKETS:
            raise ValueError("Unsupported category or country")
        return list(
            self.products.find({"category": category, "markets": country, "status": "active"}).sort(
                "_id", 1
            )
        )

    def summary(self) -> list[dict]:
        return list(
            self.products.aggregate(
                [
                    {"$match": {"status": "active"}},
                    {
                        "$group": {
                            "_id": "$category",
                            "products": {"$sum": 1},
                            "variants": {"$sum": {"$size": "$variants"}},
                        }
                    },
                    {"$sort": {"_id": 1}},
                ]
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init-secrets", "schema", "seed", "verify", "explain"])
    parser.add_argument("--directory", type=Path, default=Path(".local-secrets/mongodb"))
    parser.add_argument("--credential-file", type=Path, default=Path("/run/secrets/catalog.json"))
    args = parser.parse_args()
    if args.action == "init-secrets":
        init_secrets(args.directory)
        print("Local catalog credentials created; values are not displayed.")
        return
    if args.action == "schema":
        print(json.dumps(contract(), sort_keys=True, indent=2))
        return
    try:
        role = "writer" if args.action == "seed" else "reader"
        with connect(
            role, args.credential_file, os.environ.get("CATALOG_HOST", "retailflow-mongodb")
        ) as client:
            catalog = Catalog(client)
            if args.action == "seed":
                print(
                    json.dumps(
                        {"inserted": catalog.seed(), "total": catalog.products.count_documents({})}
                    )
                )
            elif args.action == "verify":
                if catalog.products.count_documents({}) != 20:
                    raise ValueError("Catalog does not cover all twenty product identifiers")
                print(
                    json.dumps(
                        {
                            "total": catalog.products.count_documents({}),
                            "categories": catalog.summary(),
                        }
                    )
                )
            else:
                command = {
                    "find": "products",
                    "filter": {"category": "electronics", "markets": "HU", "status": "active"},
                }
                report = client.retailflow.command("explain", command, verbosity="executionStats")
                hinted = client.retailflow.command(
                    "explain",
                    {**command, "hint": "category_market_status"},
                    verbosity="executionStats",
                )
                print(
                    json.dumps(
                        {"unhinted": report["executionStats"], "hinted": hinted["executionStats"]},
                        default=str,
                    )
                )
    except Exception as error:
        raise SystemExit(
            f"Catalog operation failed ({type(error).__name__}); credential values withheld"
        ) from None


if __name__ == "__main__":
    main()
