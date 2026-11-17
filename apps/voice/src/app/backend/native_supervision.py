"""Opt-in Dataverse-authorized, one-way native AI pause commands."""

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from backend.dataverse import DataverseError

logger = logging.getLogger("native_supervision")
CONTRACT = 1
TABLE = "moacc_nativeaicommands"
ENTITY = "moacc_nativeaicommand"
PRIMARY = ENTITY + "id"
RESULT_FIELDS = {
    "moacc_outcome", "moacc_reason", "moacc_ackrevision", "moacc_ackon", "moacc_inflight",
}
REQUEST_FIELDS = {"moacc_conversationid", "moacc_action"}
SELECT = ",".join((
    PRIMARY, "moacc_conversationid", "moacc_action", "moacc_outcome", "createdon",
    "modifiedon", "_createdby_value", "_createdonbehalfby_value", "overriddencreatedon",
))
FRESH_SECONDS = 30
BATCH_SIZE = 8
POLL_SECONDS = 1
IDLE_SECONDS = 5


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ValueError("A nonempty UUID is required.")
    parsed = UUID(value)
    if not parsed.int:
        raise ValueError("A nonempty UUID is required.")
    return str(parsed)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("A platform timestamp is required.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A timezone is required.")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SupervisionConfig:
    enabled: bool = False
    actors: tuple[str, ...] = ()
    controllers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Native supervision requires a boolean opt-in.")
        if not isinstance(self.actors, tuple) or len(self.actors) > 16:
            raise ValueError("Approve at most sixteen native supervision actors.")
        normalized = tuple(identifier(actor) for actor in self.actors)
        if normalized != self.actors or len(set(normalized)) != len(normalized):
            raise ValueError("Approved native supervision actors must be distinct canonical UUIDs.")
        if not isinstance(self.controllers, tuple) or len(self.controllers) > 4:
            raise ValueError("Pin at most four published native controllers.")
        for component, digest in self.controllers:
            identifier(component)
            if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
                raise ValueError("Pin the exact published controller SHA-256.")
        if len({item[0] for item in self.controllers}) != len(self.controllers):
            raise ValueError("Native controller pins must be distinct.")
        if self.enabled and (not self.actors or not self.controllers):
            raise ValueError("Native supervision needs approved actors and verified published controller pins.")

    @classmethod
    def from_env(cls):
        enabled = os.environ.get("DYNAMICS_NATIVE_SUPERVISION_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("DYNAMICS_NATIVE_SUPERVISION_ENABLED must be true or false.")
        actors = os.environ.get("DYNAMICS_NATIVE_SUPERVISION_ACTOR_IDS", "").strip()
        raw = json.loads(os.environ.get("DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS", "[]"))
        if not isinstance(raw, list) or any(
            not isinstance(item, dict) or set(item) != {"id", "sha256"} for item in raw
        ):
            raise ValueError("Native controller pins require id and sha256 only.")
        return cls(
            enabled == "true",
            tuple(identifier(item.strip()) for item in actors.split(",")) if actors else (),
            tuple((identifier(item["id"]), item["sha256"]) for item in raw),
        )


class NativeCommandConsumer:
    def __init__(self, config, dataverse, apply_pause, sessions):
        self.config = config
        self.dataverse = dataverse
        self.apply_pause = apply_pause
        self.sessions = sessions
        self.ready = False
        self.task = None
        self.stopping = asyncio.Event()

    async def verify(self):
        self.ready = False
        definition = await self.dataverse.get(
            f"EntityDefinitions(LogicalName='{ENTITY}')",
            params={"$select": "EntitySetName,OwnershipType,IsOptimisticConcurrencyEnabled", "$expand": "Attributes($select=LogicalName,IsSecured)"},
        )
        columns = {item["LogicalName"]: item for item in definition.get("Attributes", [])}
        if (
            definition.get("EntitySetName") != TABLE or definition.get("OwnershipType") != "UserOwned"
            or definition.get("IsOptimisticConcurrencyEnabled") is not True
            or not all(columns.get(name, {}).get("IsSecured") is True for name in RESULT_FIELDS | REQUEST_FIELDS)
            or not {"moacc_action", "moacc_conversationid", PRIMARY}.issubset(columns)
        ):
            raise ValueError("Native supervision command schema or secured outcomes are not ready.")
        await self.dataverse.get(TABLE, params={"$select": PRIMARY, "$top": "1"})
        for component, expected in self.config.controllers:
            topic = await self.dataverse.get(
                f"botcomponents({component})", params={"$select": "data,modifiedon,_parentbotid_value"},
            )
            data = topic.get("data", "")
            if (
                not isinstance(data, str) or hashlib.sha256(data.encode()).hexdigest() != expected
                or "supervision_contract: 1" not in data
                or "native_supervision_hold" not in data or "native_supervision_quiet_end" not in data
            ):
                raise ValueError("The native supervision controller differs from its reviewed pin.")
            bot = await self.dataverse.get(
                f"bots({identifier(topic.get('_parentbotid_value'))})", params={"$select": "publishedon"},
            )
            if not bot.get("publishedon") or timestamp(topic["modifiedon"]) > timestamp(bot["publishedon"]):
                raise ValueError("Publish the compatible native supervision controller before enabling the feature.")
        self.ready = True

    async def start(self):
        if not self.config.enabled or self.task is not None:
            return
        await self.verify()
        self.task = asyncio.create_task(self._run())

    async def close(self):
        self.stopping.set()
        if self.task is not None:
            # Do not cancel a pause after it has latched or an outcome write in flight.
            await self.task

    async def _update(self, row, values):
        values = {key: None if key == "moacc_reason" and value == "" else value for key, value in values.items()}
        saved = await self.dataverse.update_native_command(row[PRIMARY], values, row.get("@odata.etag", ""))
        if not saved or any(key not in saved for key in values):
            saved = await self.dataverse.get(
                f"{TABLE}({row[PRIMARY]})", params={"$select": ",".join((PRIMARY, *values))},
            )
        if saved.get(PRIMARY) != row[PRIMARY] or any(key not in saved or saved[key] != value for key, value in values.items()):
            raise DataverseError("supervision_outcome_unconfirmed", "The command outcome could not be verified.", 502)
        return saved

    async def process(self, row):
        try:
            command = identifier(row.get(PRIMARY))
            if not re.fullmatch(r'W/"\d+"', row.get("@odata.etag", "")):
                raise ValueError("A command ETag is required.")
        except (ValueError, TypeError):
            logger.warning("Native supervision ignored an invalid command envelope")
            return
        outcome = row.get("moacc_outcome")
        if outcome not in {None, "", "pending", "processing"}:
            return
        if outcome == "processing":
            # A previous worker may have latched pause without persisting its receipt.
            if timestamp(row["modifiedon"]) > datetime.now(timezone.utc) - timedelta(seconds=45):
                return
            await self._update(row, {"moacc_outcome": "failed", "moacc_reason": "outcome_unconfirmed"})
            return
        reason = None
        try:
            conversation = identifier(row.get("moacc_conversationid"))
            actor_id = identifier(row.get("_createdby_value"))
            created = timestamp(row["createdon"])
            age = datetime.now(timezone.utc) - created
            if row.get("moacc_action") != "pause":
                reason = "invalid_action"
            elif row.get("_createdonbehalfby_value") or row.get("overriddencreatedon"):
                reason = "indirect_creator"
            elif actor_id not in self.config.actors:
                reason = "actor_not_approved"
            elif not timedelta(seconds=-5) <= age <= timedelta(seconds=FRESH_SECONDS):
                reason = "expired"
            else:
                actor = await self.dataverse.get(
                    f"systemusers({actor_id})",
                    params={"$select": "systemuserid,isdisabled,applicationid,accessmode,islicensed"},
                )
                if (
                    actor.get("systemuserid") != actor_id or actor.get("isdisabled") is not False
                    or actor.get("applicationid") or actor.get("accessmode") != 0
                    or actor.get("islicensed") is not True
                ):
                    reason = "actor_not_interactive"
        except (ValueError, TypeError, KeyError):
            reason = "invalid_request"
        if reason:
            await self._update(row, {"moacc_outcome": "rejected", "moacc_reason": reason})
            return
        claimed = await self._update(row, {"moacc_outcome": "processing", "moacc_reason": ""})
        result = await self.apply_pause(conversation, command, created)
        allowed = {"paused", "rejected", "failed"}
        if result.get("outcome") not in allowed:
            raise ValueError("Invalid native pause outcome.")
        await self._update(claimed, {
            "moacc_outcome": result["outcome"], "moacc_reason": result.get("reason", ""),
            "moacc_ackrevision": result.get("revision", 0), "moacc_inflight": result.get("inflight", 0),
            **({"moacc_ackon": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")} if result["outcome"] == "paused" else {}),
        })

    async def sweep(self):
        rows = await self.dataverse.get(TABLE, params={
            "$select": SELECT, "$orderby": "createdon asc", "$top": str(BATCH_SIZE),
            "$filter": "(moacc_outcome eq null or moacc_outcome eq '' or moacc_outcome eq 'pending' "
                       "or (moacc_outcome eq 'processing' and modifiedon lt "
                       + (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat() + "))",
        })
        for row in rows.get("value", [])[:BATCH_SIZE]:
            if self.stopping.is_set():
                break
            try:
                await self.process(row)
            except DataverseError as error:
                if error.code == "dynamics_conflict":
                    continue
                raise
        # Bounded idle reconciliation also makes closed/no-provider requests explicit.
        completed = await self.dataverse.get(TABLE, params={
            "$select": PRIMARY + ",moacc_conversationid,moacc_inflight",
            "$filter": "moacc_outcome eq 'paused'", "$orderby": "modifiedon asc", "$top": str(BATCH_SIZE),
        })
        for row in completed.get("value", [])[:BATCH_SIZE]:
            session = self.sessions().get(row.get("moacc_conversationid"))
            if session is None or session.done.is_set():
                await self._update(row, {"moacc_outcome": "ended", "moacc_reason": "native_session_ended"})
            elif row.get("moacc_inflight") and session.voice is not None and not session.voice.inflight_tools:
                await self._update(row, {"moacc_inflight": 0})

    async def _run(self):
        failures = 0
        while not self.stopping.is_set():
            try:
                await self.sweep()
                failures = 0
            except (DataverseError, ValueError, TypeError, KeyError):
                failures = min(failures + 1, 5)
                logger.warning("Native supervision command sweep unavailable; no unverified command executed")
            delay = min(30, 2 ** failures) if failures else (
                POLL_SECONDS if any(not value.done.is_set() for value in self.sessions().values()) else IDLE_SECONDS
            )
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=delay)
            except TimeoutError:
                pass
