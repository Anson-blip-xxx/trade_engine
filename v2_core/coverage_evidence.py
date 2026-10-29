"""Content-addressed, account-bound coverage facts. No evidence is discarded.

Records, ordered 64-record chunks, and manifests are immutable deduplicated PG
objects. An observation keeps its inventory, result and two manifest references.
The original before/after objects can be reconstructed exactly, including order.
"""

import json
from dataclasses import asdict

from v2_core.evidence import canonical, digest

FORMAT = "coverage-fact-refs-v1"
GROUPS = {"episodes": "EPISODE", "orders": "ORDER", "protections": "PROTECTION"}


def pack_coverage(conn, scope, payload):
    account = {
        k: asdict(scope)[k]
        for k in ("exchange", "account_id", "environment", "product")
    }
    blobs = {}

    def put(kind, value):
        content = canonical({"account_scope": account, "kind": kind, "value": value})
        identity = digest(content)
        blobs[identity] = content
        return identity

    def pack_facts(facts):
        if not isinstance(facts, dict) or set(facts) != set(GROUPS):
            raise ValueError("COVERAGE_FACT_FIELDS")
        manifest = {}
        for name, kind in GROUPS.items():
            records = facts[name]
            if not isinstance(records, list) or any(
                not isinstance(r, dict) for r in records
            ):
                raise ValueError("COVERAGE_FACT_RECORDS")
            refs = [put(kind, r) for r in records]
            manifest[name] = [
                put("CHUNK", {"kind": kind, "refs": refs[i : i + 64]})
                for i in range(0, len(refs), 64)
            ]
        return put("MANIFEST", manifest)

    before = pack_facts(payload["before"])
    after = pack_facts(payload["after"])
    # Pipelined bounded batches, not one query/round trip per old order.
    entries = list(blobs.items())
    with conn.cursor() as cursor:
        for index in range(0, len(entries), 256):
            cursor.executemany(
                "INSERT INTO v2_coverage_blobs(blob_id,content_json) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                entries[index : index + 256],
            )
    return {
        "_v2_coverage_facts": FORMAT,
        "before_ref": before,
        "after_ref": after,
        "inventory": payload["inventory"],
        "result": payload["result"],
    }


def unpack_coverage(conn, scope, payload):
    account = {
        k: asdict(scope)[k]
        for k in ("exchange", "account_id", "environment", "product")
    }
    if (
        set(payload)
        != {"_v2_coverage_facts", "before_ref", "after_ref", "inventory", "result"}
        or payload["_v2_coverage_facts"] != FORMAT
    ):
        raise ValueError("COVERAGE_STORAGE_FORMAT")
    cache = {}

    def fetch(refs, kind):
        if not isinstance(refs, list) or any(
            not isinstance(r, str) or len(r) != 64 for r in refs
        ):
            raise ValueError("COVERAGE_BLOB_REFS")
        missing = sorted(set(refs) - cache.keys())
        for index in range(0, len(missing), 256):
            batch = missing[index : index + 256]
            rows = conn.execute(
                "SELECT blob_id,content_json FROM v2_coverage_blobs WHERE blob_id=ANY(%s)",
                (batch,),
            ).fetchall()
            if len(rows) != len(batch):
                raise ValueError("COVERAGE_BLOB_MISSING")
            for identity, content in rows:
                if digest(content) != identity:
                    raise ValueError("COVERAGE_BLOB_DIGEST")
                node = json.loads(content)
                if node.get("account_scope") != account:
                    raise ValueError("COVERAGE_BLOB_SCOPE")
                cache[identity] = node
        if any(cache[r].get("kind") != kind for r in refs):
            raise ValueError("COVERAGE_BLOB_KIND")
        return [cache[r]["value"] for r in refs]

    def facts(ref):
        manifest = fetch([ref], "MANIFEST")[0]
        if not isinstance(manifest, dict) or set(manifest) != set(GROUPS):
            raise ValueError("COVERAGE_MANIFEST_FIELDS")
        result = {}
        for name, kind in GROUPS.items():
            chunks = fetch(manifest[name], "CHUNK")
            refs = []
            for chunk in chunks:
                if (
                    set(chunk) != {"kind", "refs"}
                    or chunk["kind"] != kind
                    or not 1 <= len(chunk["refs"]) <= 64
                ):
                    raise ValueError("COVERAGE_CHUNK_INVALID")
                refs.extend(chunk["refs"])
            result[name] = fetch(refs, kind)
        return result

    return {
        "inventory": payload["inventory"],
        "result": payload["result"],
        "before": facts(payload["before_ref"]),
        "after": facts(payload["after_ref"]),
    }
