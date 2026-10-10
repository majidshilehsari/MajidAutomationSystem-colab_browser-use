"""The operations library: named, reusable bundles of steps.

An *operation* (عملیات) is the unit the library tab works with: a name, a
description and the steps that carry it out. Four of them ship with the app
because they are the ones the project keeps needing - open LMArena, detect or
solve a captcha, connect to DeepSeek, and prompt DeepSeek and read its answer
back - and each of them is nothing more than an ordinary step list, which is
the point: opening an operation drops its steps into the steps tab, where they
can be edited, re-run, saved as a flow, or deleted.

Built-ins are ordinary rows with a ``builtin`` marker, so they can be edited
like anything else; ``reset_builtin`` puts the factory definition back when an
edit went wrong. Seeding never overwrites a row the operator changed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import schema
from .agent_store import AgentStore

#: Factory definitions. ``id`` is stable so seeding is idempotent across
#: restarts, upgrades and volumes that move between deployments.
BUILTINS: List[Dict[str, Any]] = [
    {
        "id": "op-builtin-lmarena",
        "builtin": "lmarena",
        "name": "اتصال به ایجنت lmarena",
        "description": "سایت lmarena.ai را در مرورگر اتوماسیون باز می‌کند، کمی صبر "
                       "می‌کند تا صفحه بالا بیاید و متن صفحه را برای گزارش اجرا "
                       "می‌گیرد. اگر دیوار لاگین دیدید، مرحلهٔ pause را دستی اضافه کنید.",
        "tags": ["وب", "چت"],
        "steps": [
            {"id": "lm-1", "type": "goto_url", "url": "https://lmarena.ai/",
             "label": "باز کردن lmarena.ai"},
            {"id": "lm-2", "type": "wait", "ms": 3500, "label": "صبر برای بارگذاری"},
            {"id": "lm-3", "type": "capture_text", "label": "خواندن متن صفحه"},
        ],
    },
    {
        "id": "op-builtin-captcha",
        "builtin": "captcha",
        "name": "شناسایی یا حل کپچا",
        "description": "از صفحهٔ فعال اسکرین‌شات می‌گیرد و زنجیرهٔ حل کپچا را اجرا "
                       "می‌کند: اول بینایی (مدل)، بعد انسان (تلگرام)، بعد افزونه. "
                       "خروجی یک *پیشنهاد* است و بدون تأیید شما کلیک نمی‌شود، مگر "
                       "اینکه captcha.autoClick روشن باشد.",
        "tags": ["کپچا", "امنیت"],
        "steps": [
            {"id": "cp-1", "type": "captcha_solve",
             "label": "بررسی و حل کپچای صفحهٔ فعال"},
        ],
    },
    {
        "id": "op-builtin-deepseek-open",
        "builtin": "deepseek-open",
        "name": "اتصال به دیپ‌سیک",
        "description": "مرورگر اختصاصی ایجنت (نمایش :2، پورت 9223) را روی صفحهٔ چت "
                       "دیپ‌سیک می‌برد. اولین بار باید دستی لاگین کنید؛ نشست در "
                       "/data/ai-profile می‌ماند.",
        "tags": ["ایجنت", "چت"],
        "steps": [
            {"id": "ds-1", "type": "agent_open", "provider": "deepseek",
             "label": "باز کردن چت دیپ‌سیک در مرورگر ایجنت"},
        ],
    },
    {
        "id": "op-builtin-deepseek-ask",
        "builtin": "deepseek-ask",
        "name": "پرامپت دادن به دیپ‌سیک و دریافت پیامش",
        "description": "چت دیپ‌سیک را باز می‌کند، پرامپت را تایپ می‌کند، تا پایان "
                       "پاسخ صبر می‌کند (تشخیص پایان: متن دو بار پشت سر هم عوض "
                       "نشود) و متن پاسخ را در گزارش اجرا و در یادداشت‌ها می‌گذارد.",
        "tags": ["ایجنت", "چت"],
        "steps": [
            {"id": "da-1", "type": "agent_open", "provider": "deepseek",
             "label": "باز کردن چت دیپ‌سیک"},
            {"id": "da-2", "type": "agent_ask", "provider": "deepseek",
             "prompt": "سلام. وضعیت فعلی صفحهٔ من را در یک پاراگراف خلاصه کن.",
             "saveAs": True, "label": "پرسیدن و گرفتن پاسخ"},
        ],
    },
    {
        "id": "op-builtin-snapshot",
        "builtin": "snapshot",
        "name": "عکس و متن صفحهٔ فعال",
        "description": "یک اسکرین‌شات از دسکتاپ می‌گیرد و متن دیده‌شدهٔ صفحه را "
                       "استخراج می‌کند؛ پایهٔ بیشتر عملیات‌های دیگر.",
        "tags": ["تشخیص"],
        "steps": [
            {"id": "sn-1", "type": "screenshot", "name": "manual",
             "label": "اسکرین‌شات از دسکتاپ"},
            {"id": "sn-2", "type": "capture_text", "label": "متن صفحه"},
        ],
    },
]


class OperationError(Exception):
    """A refused operation, with the HTTP status it should produce."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class OperationLibrary:
    def __init__(self, store: AgentStore) -> None:
        self.store = store

    # -- seeding ----------------------------------------------------------
    def ensure_builtins(self, actor: str = "startup") -> int:
        """Create the factory operations that do not exist yet.

        Returns how many were created. Existing rows are left exactly as they
        are, including edits, so an operator's changes survive every restart.
        """
        existing = {row["id"] for row in self.store.list_operations()}
        created = 0
        for definition in BUILTINS:
            if definition["id"] in existing:
                continue
            steps, errors = self.validate_steps(definition["steps"])
            if errors:  # a shipped operation must never be invalid
                raise OperationError("builtin %s is invalid: %s"
                                     % (definition["id"], "; ".join(errors)), 500)
            self.store.create_operation(
                {"name": definition["name"], "description": definition["description"],
                 "kind": "steps", "builtin": definition["builtin"],
                 "tags": definition["tags"], "steps": steps},
                operation_id=definition["id"], actor=actor)
            created += 1
        return created

    def builtins(self) -> Dict[str, Dict[str, Any]]:
        return {definition["builtin"]: definition for definition in BUILTINS}

    def reset_builtin(self, operation_id: str,
                      actor: str = "operator") -> Dict[str, Any]:
        row = self.store.get_operation(operation_id)
        if row is None:
            raise OperationError("no operation with id %s" % operation_id, 404)
        if not row.get("builtin"):
            raise OperationError("only built-in operations can be reset")
        definition = self.builtins().get(str(row.get("builtinKey") or ""))
        if definition is None:
            raise OperationError("unknown builtin %r" % row.get("builtinKey"))
        steps, errors = self.validate_steps(definition["steps"])
        if errors:
            raise OperationError("builtin %s is invalid: %s"
                                 % (definition["id"], "; ".join(errors)), 500)
        updated = self.store.update_operation(
            operation_id,
            {"name": definition["name"], "description": definition["description"],
             "steps": steps, "tags": definition["tags"]}, actor=actor)
        return updated or {}

    # -- CRUD -------------------------------------------------------------
    @staticmethod
    def validate_steps(steps: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Validate a bare step list by wrapping it in a throwaway flow."""
        if not isinstance(steps, list):
            return [], ["steps must be a list"]
        if not steps:
            return [], ["an operation needs at least one step"]
        normalized, errors = schema.validate_flow({"name": "operation",
                                                   "steps": steps})
        if errors:
            return [], errors
        return list(normalized.get("steps") or []), []

    def list(self) -> List[Dict[str, Any]]:
        return self.store.list_operations()

    def get(self, operation_id: str) -> Dict[str, Any]:
        row = self.store.get_operation(operation_id)
        if row is None:
            raise OperationError("no operation with id %s" % operation_id, 404)
        return row

    def save(self, data: Dict[str, Any], operation_id: Optional[str] = None,
             actor: str = "operator") -> Dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise OperationError("an operation needs a name")
        steps, errors = self.validate_steps(data.get("steps") or [])
        if errors:
            raise OperationError("invalid steps: %s" % "; ".join(errors))
        payload = {"name": name,
                   "description": str(data.get("description") or ""),
                   "tags": [str(tag) for tag in (data.get("tags") or [])],
                   "steps": steps}
        if operation_id:
            updated = self.store.update_operation(operation_id, payload, actor=actor)
            if updated is None:
                raise OperationError("no operation with id %s" % operation_id, 404)
            return updated
        return self.store.create_operation(payload, actor=actor)

    def delete(self, operation_id: str, actor: str = "operator") -> Dict[str, Any]:
        if not self.store.delete_operation(operation_id, actor=actor):
            raise OperationError("no operation with id %s" % operation_id, 404)
        return {"deleted": operation_id}

    # -- running ----------------------------------------------------------
    def as_flow(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """The operation's steps as a flow the engine can start."""
        flow, errors = schema.validate_flow(
            {"name": str(operation.get("name") or "operation"),
             "steps": operation.get("steps") or []})
        if errors:
            raise OperationError("operation %s is invalid: %s"
                                 % (operation.get("id"), "; ".join(errors)), 409)
        return flow
