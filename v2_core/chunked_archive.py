"""UTC-hour content-addressed blocks plus immutable full-batch manifests."""

import json
import re

from v2_core.evidence import canonical, digest
from v2_core.market_archive import ArchiveIntegrityError, ClickHouseCandleArchive


class ClickHouseChunkedArchive(ClickHouseCandleArchive):
    def _blocks(self, keys):
        if not keys:
            return {}
        if len(keys) > 2500 or any(
            not isinstance(key, str) or not re.fullmatch("[0-9a-f]{64}", key)
            for key in keys
        ):
            raise ArchiveIntegrityError("invalid block references")
        rows = self.client.query(
            "SELECT content_digest,any(payload) FROM v2_candle_archive WHERE content_digest IN {keys:Array(String)} GROUP BY content_digest",
            parameters={"keys": list(keys)},
        ).result_rows
        result = {}
        for key, encoded in rows:
            if (
                key not in keys
                or not isinstance(encoded, str)
                or len(encoded.encode()) > 100000
                or digest(encoded) != key
            ):
                raise ArchiveIntegrityError("block digest mismatch")
            result[key] = encoded
        return result

    def put(self, content_digest, encoded):
        if (
            not isinstance(encoded, str)
            or len(encoded.encode()) > 8_000_000
            or digest(encoded) != content_digest
        ):
            raise ValueError("archive input digest mismatch")
        batch = json.loads(encoded)
        if (
            canonical(batch) != encoded
            or not isinstance(batch.get("candles"), dict)
            or not 1 <= len(batch["candles"]) <= 100
        ):
            raise ValueError("canonical bounded candle batch required")
        blocks, references = {}, {}
        header = {key: value for key, value in batch.items() if key != "candles"}
        for target, bars in sorted(batch["candles"].items()):
            if not isinstance(bars, list) or len(bars) != 1440:
                raise ValueError("full minute history required")
            hours = {}
            for bar in bars:
                if (
                    not isinstance(bar, dict)
                    or type(bar.get("t")) is not int
                    or bar["t"] < 0
                ):
                    raise ValueError("explicit candle time required")
                hours.setdefault(bar["t"] // 3600000, []).append(bar)
            if len(hours) > 25 or any(len(values) > 60 for values in hours.values()):
                raise ValueError("bounded hourly blocks required")
            references[target] = []
            for hour, values in hours.items():
                payload = canonical(
                    {
                        "source": header.get("source"),
                        "environment": header.get("environment"),
                        "symbol": target,
                        "hour": hour,
                        "bars": values,
                    }
                )
                if len(payload.encode()) > 100000:
                    raise ValueError("hour block too large")
                key = digest(payload)
                blocks[key] = payload
                references[target].append(key)
        existing = self._blocks(set(blocks))
        missing = [
            [key, payload] for key, payload in blocks.items() if key not in existing
        ]
        if missing:
            self.client.insert(
                "v2_candle_archive", missing, column_names=["content_digest", "payload"]
            )
        if self._blocks(set(blocks)) != blocks:
            return False
        manifest = canonical({"version": 1, "header": header, "symbols": references})
        self.client.insert(
            "v2_candle_manifests",
            [[content_digest, manifest, digest(manifest)]],
            column_names=["batch_digest", "payload", "manifest_digest"],
        )
        return self.get(content_digest) == encoded

    def get(self, content_digest):
        if not isinstance(content_digest, str) or not re.fullmatch(
            "[0-9a-f]{64}", content_digest
        ):
            raise ValueError("invalid batch digest")
        rows = self.client.query(
            "SELECT payload,manifest_digest FROM v2_candle_manifests WHERE batch_digest={digest:String} LIMIT 1",
            parameters={"digest": content_digest},
        ).result_rows
        if not rows:
            return super().get(
                content_digest
            )  # Existing unchunked receipts remain replayable.
        encoded, expected = rows[0]
        if (
            not isinstance(encoded, str)
            or len(encoded.encode()) > 250000
            or digest(encoded) != expected
        ):
            raise ArchiveIntegrityError("manifest digest mismatch")
        try:
            manifest = json.loads(encoded)
            if (
                set(manifest) != {"version", "header", "symbols"}
                or manifest["version"] != 1
                or not isinstance(manifest["header"], dict)
                or "candles" in manifest["header"]
                or not isinstance(manifest["symbols"], dict)
                or not 1 <= len(manifest["symbols"]) <= 100
            ):
                raise ValueError("invalid manifest")
            keys = set()
            for refs in manifest["symbols"].values():
                if not isinstance(refs, list) or not 1 <= len(refs) <= 25:
                    raise ValueError("invalid block list")
                keys.update(refs)
        except (ValueError, TypeError, KeyError) as exc:
            raise ArchiveIntegrityError("invalid archived manifest") from exc
        # Transport/decode failures from the client are not corruption proof.
        blocks = self._blocks(keys)
        if blocks.keys() != keys:
            return None  # Incomplete replica/read visibility is retryable.
        try:
            candles = {}
            for target, refs in manifest["symbols"].items():
                values = []
                for key in refs:
                    block = json.loads(blocks[key])
                    if (
                        block["symbol"] != target
                        or block["environment"] != manifest["header"].get("environment")
                        or block["source"] != manifest["header"].get("source")
                        or not isinstance(block["bars"], list)
                    ):
                        raise ValueError("block scope mismatch")
                    values.extend(block["bars"])
                if len(values) != 1440:
                    raise ValueError("incomplete reconstructed history")
                candles[target] = values
            result = canonical({**manifest["header"], "candles": candles})
            if len(result.encode()) > 8_000_000 or digest(result) != content_digest:
                raise ValueError("reconstructed batch digest mismatch")
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise ArchiveIntegrityError("invalid archived manifest or blocks") from exc
