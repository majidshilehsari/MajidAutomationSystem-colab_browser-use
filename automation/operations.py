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

from . import convert
from . import schema
from .agent_store import AgentStore

#: Factory definitions. ``id`` is stable so seeding is idempotent across
#: restarts, upgrades and volumes that move between deployments.
BUILTINS: List[Dict[str, Any]] = [
    {
        "id": "op-builtin-lmarena",
        "builtin": "lmarena",
        "name": "باز کردن lmarena",
        "description": "سایت lmarena.ai را در مرورگر اتوماسیون باز می‌کند، کمی "
                       "صبر می‌کند تا صفحه بالا بیاید و متن صفحه را برای گزارش "
                       "اجرا می‌گیرد. اگر دیوار لاگین دیدید، یک مرحلهٔ پایتون "
                       "برای مکث دستی اضافه کنید.",
        "tags": ["وب", "چت"],
        "steps": [
            {"id": "lm-1", "type": "python",
             "code": "check(goto('https://lmarena.ai/'))",
             "label": "باز کردن lmarena.ai"},
            {"id": "lm-2", "type": "python", "code": "wait(3.5)",
             "label": "صبر برای بارگذاری"},
            {"id": "lm-3", "type": "python",
             "code": "log(check(capture_text()))",
             "label": "خواندن متن صفحه"},
        ],
    },
    {
        "id": "op-builtin-captcha",
        "builtin": "captcha",
        "name": "حل کپچا",
        "description": "از صفحهٔ فعال اسکرین‌شات می‌گیرد و زنجیرهٔ حل کپچا را "
                       "اجرا می‌کند: اول بینایی (مدل)، بعد انسان (تلگرام)، بعد "
                       "افزونه. خروجی یک *پیشنهاد* است و بدون تأیید شما کلیک "
                       "نمی‌شود، مگر اینکه captcha.autoClick روشن باشد.",
        "tags": ["کپچا", "امنیت"],
        "steps": [
            {"id": "cp-1", "type": "python",
             "code": "check(solve_captcha(label='کپچای صفحهٔ فعال'))",
             "label": "بررسی و حل کپچای صفحهٔ فعال"},
        ],
    },
    {
        "id": "op-builtin-deepseek-open",
        "builtin": "deepseek-open",
        "name": "باز کردن دیپ‌سیک",
        "description": "مرورگر اختصاصی ایجنت (نمایش :2، پورت 9223) را روی صفحهٔ "
                       "چت دیپ‌سیک می‌برد. اولین بار باید دستی لاگین کنید؛ نشست "
                       "در /data/ai-profile می‌ماند.",
        "tags": ["ایجنت", "چت"],
        "steps": [
            {"id": "ds-1", "type": "python",
             "code": "check(open_agent(provider='deepseek'))",
             "label": "باز کردن چت دیپ‌سیک در مرورگر ایجنت"},
        ],
    },
    {
        "id": "op-builtin-deepseek-ask",
        "builtin": "deepseek-ask",
        "name": "پرسش از دیپ‌سیک",
        "description": "چت دیپ‌سیک را باز می‌کند، پرسش را تایپ می‌کند، تا پایان "
                       "پاسخ صبر می‌کند (تشخیص پایان: متن دو بار پشت سر هم عوض "
                       "نشود) و متن پاسخ را در گزارش اجرا و در یادداشت‌ها "
                       "می‌گذارد.",
        "tags": ["ایجنت", "چت"],
        "steps": [
            {"id": "da-1", "type": "python",
             "code": "check(open_agent(provider='deepseek'))",
             "label": "باز کردن چت دیپ‌سیک"},
            {"id": "da-2", "type": "python",
             "code": "answer = check(ask_agent('سلام. وضعیت فعلی صفحهٔ من را در یک پاراگراف خلاصه کن.', provider='deepseek', save_as=True))\nlog(answer)",
             "label": "پرسیدن و گرفتن پاسخ"},
        ],
    },
    {
        "id": "op-builtin-snapshot",
        "builtin": "snapshot",
        "name": "دیدن صفحهٔ فعال",
        "description": "یک اسکرین‌شات از دسکتاپ می‌گیرد و متن دیده‌شدهٔ صفحه را "
                       "استخراج می‌کند؛ پایهٔ بیشتر عملیات‌های دیگر.",
        "tags": ["تشخیص"],
        "steps": [
            {"id": "sn-1", "type": "python",
             "code": "check(screenshot('manual'))",
             "label": "اسکرین‌شات از دسکتاپ"},
            {"id": "sn-2", "type": "python",
             "code": "log(check(capture_text()))",
             "label": "متن صفحه"},
        ],
    },
    {
        # The brain's fixed thinking: one screenshot of whatever is on screen
        # right now, one capture of all visible text, and both archived into
        # the log. Safe at any moment, so it is always allowed. Round 26: it
        # belongs to the brain alone (scope: brain).
        "id": "op-builtin-observe",
        "builtin": "observe",
        "name": "چرخهٔ مشاهده",
        "description": "یک اسکرین‌شات از صفحهٔ فعلی + ضبط کامل متنِ دیده‌شدنی؛ "
                       "هر دو به آرشیو گزارش‌ها می‌روند. این نگاهِ ثابتِ مغز است "
                       "و در هر لحظه بی‌خطر است، پس همیشه مجاز است. اختصاصی مغز.",
        "tags": ["مغز", "مشاهده", "گزارش"],
        "permission": "auto",
        "scope": "brain",
        "steps": [
            {"id": "ob-1", "type": "python",
             "code": "rc, shot, err = screenshot('observe')\nif rc != 0:\n    log('اسکرین\u200cشاتِ چرخهٔ مشاهده ناموفق بود: ' + str(err or shot or 'بدون پیام'))",
             "label": "اسکرین‌شاتِ چرخهٔ مشاهده (ناموفق بودنش چرخه را متوقف نمی‌کند)"},
            {"id": "ob-2", "type": "python",
             "code": "text = check(capture_text(limit=20000))\nif not str(text).strip():\n    raise RuntimeError('متن صفحه خالی بود؛ مشاهدهٔ معتبری ممکن نشد')\nlog(text)",
             "label": "متن کاملِ صفحهٔ فعلی"},
        ],
    },
    {
        # Round 26, brain tool #2: ask pi.ai for advice about whatever is on
        # screen and file the answer into the brain conversation so it can be
        # used later (تعامل با مغز).
        "id": "op-builtin-pi-consult",
        "builtin": "pi-consult",
        "name": "مشورت با پی‌آی",
        "description": "پی‌آی (pi.ai) را در مرورگر ایجنت باز می‌کند، متن صفحهٔ "
                       "فعال را برایش می‌فرستد و پاسخ مشاوره را هم در گزارش "
                       "اجرا و هم در «تعامل با مغز» ثبت می‌کند تا بعداً قابل "
                       "استفاده باشد. اختصاصی مغز.",
        "tags": ["مغز", "مشورت", "پی‌آی"],
        "permission": "auto",
        "scope": "brain",
        "steps": [
            {"id": "pc-1", "type": "python",
             "code": "check(open_agent(provider='pi'))",
             "label": "باز کردن پی‌آی در مرورگر ایجنت"},
            {"id": "pc-2", "type": "python",
             "code": "context = check(capture_text(4000))\nanswer = check(ask_agent('تو مشاور یک سیستم اتوماسیون فارسی\u200cزبان هستی. متن صفحهٔ فعال را می\u200cفرستم؛ در حداکثر سه جمله بگو اقدام بعدی چه باشد و چرا.\\nمتن صفحه:\\n' + context, provider='pi'))\nif not str(answer).strip():\n    raise RuntimeError('پی\u200cآی پاسخی برنگرداند؛ مشورت ناموفق بود')\nlog(answer)\ncheck(brain_note('مشورت با پی\u200cآی: ' + str(answer)[:1500]))",
             "label": "پرسیدن و نوشتن پاسخ در تعامل با مغز"},
        ],
    },
    {
        # Round 26, brain tool #3: send pi.ai a fixed prompt (the operation
        # list plus the visible page) and put the chosen operation into the
        # running suggestion (پیشنهاد جاری).
        "id": "op-builtin-pi-next",
        "builtin": "pi-next",
        "name": "انتخاب عملیات بعدی",
        "description": "فهرست عملیات‌ها و متن صفحهٔ فعال را با یک پرامپت ثابت "
                       "برای پی‌آی می‌فرستد؛ نام عملیاتی را که پی‌آی انتخاب "
                       "کرده در کتابخانه پیدا می‌کند و در «پیشنهاد جاری» مغز "
                       "می‌گذارد. اختصاصی مغز.",
        "tags": ["مغز", "مشورت", "پی‌آی", "پیشنهاد"],
        "permission": "auto",
        "scope": "brain",
        "steps": [
            {"id": "pn-1", "type": "python",
             "code": "check(open_agent(provider='pi'))",
             "label": "باز کردن پی‌آی در مرورگر ایجنت"},
            {"id": "pn-2", "type": "python",
             "code": "names_list = check(operations_list())\nif not names_list:\n    raise RuntimeError('کتابخانهٔ عملیات خالی است؛ انتخابی ممکن نیست')\nnames = '، '.join(str(name) for name in names_list)\ncontext = check(capture_text(2500))\nanswer = check(ask_agent('عملیات\u200cهای موجود: ' + names + '\\nمتن صفحهٔ فعال: ' + context + '\\nفقط نام یکی از عملیات\u200cها را به عنوان اقدام بعدی انتخاب کن و در یک جمله بگو چرا.', provider='pi'))\nif not str(answer).strip():\n    raise RuntimeError('پی\u200cآی پاسخی برنگرداند؛ انتخابی ممکن نشد')\nlog(answer)\npicked = check(brain_suggest(answer, 'مشورت با پی\u200cآی'))\nif not str(picked).strip():\n    log('نامی که پی\u200cآی گفت در کتابخانه پیدا نشد؛ پاسخ در «تعامل با مغز» ثبت شد: ' + str(answer)[:200])",
             "label": "مشورت برای انتخاب و گذاشتن در پیشنهاد جاری"},
        ],
    },
]


# Round 27, item 7: the round-26 factory version of the brain's three own
# tools. A stored row that still carries exactly these steps was never
# edited by the operator, so a restart hardens it; anything that differs
# is an edit and is kept exactly as it is.
_PREV_FACTORY_STEPS = {'op-builtin-observe': [{'id': 'ob-1',
                         'type': 'python',
                         'code': "check(screenshot('observe'))",
                         'label': 'اسکرین\u200cشاتِ چرخهٔ مشاهده'},
                        {'id': 'ob-2',
                         'type': 'python',
                         'code': 'log(check(capture_text(limit=20000)))',
                         'label': 'متن کاملِ صفحهٔ فعلی'}],
 'op-builtin-pi-consult': [{'id': 'pc-1',
                            'type': 'python',
                            'code': "check(open_agent(provider='pi'))",
                            'label': 'باز کردن پی\u200cآی در مرورگر ایجنت'},
                           {'id': 'pc-2',
                            'type': 'python',
                            'code': 'context = check(capture_text(4000))\n'
                                    "answer = check(ask_agent('تو مشاور یک سیستم اتوماسیون "
                                    'فارسی\u200cزبان هستی. متن صفحهٔ فعال را می\u200cفرستم؛ در '
                                    'حداکثر سه جمله بگو اقدام بعدی چه باشد و چرا.\\nمتن '
                                    "صفحه:\\n' + context, provider='pi'))\n"
                                    'log(answer)\n'
                                    "check(brain_note('مشورت با پی\u200cآی: ' + "
                                    'str(answer)[:1500]))',
                            'label': 'پرسیدن و نوشتن پاسخ در تعامل با مغز'}],
 'op-builtin-pi-next': [{'id': 'pn-1',
                         'type': 'python',
                         'code': "check(open_agent(provider='pi'))",
                         'label': 'باز کردن پی\u200cآی در مرورگر ایجنت'},
                        {'id': 'pn-2',
                         'type': 'python',
                         'code': "names = '، '.join(check(operations_list()))\n"
                                 'context = check(capture_text(2500))\n'
                                 "answer = check(ask_agent('عملیات\u200cهای موجود: ' + names + "
                                 "'\\nمتن صفحهٔ فعال: ' + context + '\\nفقط نام یکی از "
                                 'عملیات\u200cها را به عنوان اقدام بعدی انتخاب کن و در یک جمله '
                                 "بگو چرا.', provider='pi'))\n"
                                 'log(answer)\n'
                                 "check(brain_suggest(answer, 'مشورت با پی\u200cآی'))",
                         'label': 'مشورت برای انتخاب و گذاشتن در پیشنهاد جاری'}]}


def _steps_signature(steps: Any) -> list:
    """Comparable (id, type, code, label) tuples of a stored step list."""
    signature = []
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            signature.append((str(step.get("id") or ""),
                              str(step.get("type") or ""),
                              str(step.get("code") or ""),
                              str(step.get("label") or "")))
    return signature


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
                 "tags": definition["tags"], "steps": steps,
                 "permission": definition.get("permission") or "ask",
                 "scope": definition.get("scope") or "general"},
                operation_id=definition["id"], actor=actor)
            created += 1
        # Round-26 migration: factory operations that are still written in the
        # old step language are re-seeded into the one language (uniform
        # names, python steps, scope). Rows already spoken in python keep
        # whatever edits the operator made; only a drifted scope is corrected.
        rows = {row["id"]: row for row in self.store.list_operations()}
        for definition in BUILTINS:
            row = rows.get(definition["id"])
            if row is None:
                continue
            scope = definition.get("scope") or "general"
            stale = convert.needs_conversion({"steps": row.get("steps") or []})
            if stale:
                steps, errors = self.validate_steps(definition["steps"])
                if errors:  # a shipped operation must never be invalid
                    raise OperationError("builtin %s is invalid: %s"
                                         % (definition["id"], "; ".join(errors)), 500)
                self.store.update_operation(
                    definition["id"],
                    {"name": definition["name"],
                     "description": definition["description"],
                     "steps": steps, "tags": definition["tags"],
                     "permission": definition.get("permission") or "ask",
                     "scope": scope}, actor=actor)
            elif str(row.get("scope") or "general") != scope:
                self.store.update_operation(definition["id"], {"scope": scope},
                                            actor=actor)
            # Round 27, item 7: a row still carrying the exact round-26
            # factory steps of a brain tool was never edited, so it silently
            # gains the hardened steps. An operator edit differs and is kept.
            if not stale and definition["id"] in _PREV_FACTORY_STEPS \
                    and _steps_signature(row.get("steps")) == \
                    _steps_signature(_PREV_FACTORY_STEPS[definition["id"]]):
                steps, errors = self.validate_steps(definition["steps"])
                if errors:  # a shipped operation must never be invalid
                    raise OperationError("builtin %s is invalid: %s"
                                         % (definition["id"], "; ".join(errors)), 500)
                self.store.update_operation(definition["id"], {"steps": steps},
                                            actor=actor)
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
             "steps": steps, "tags": definition["tags"],
             "scope": definition.get("scope") or "general"}, actor=actor)
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
        # The brain's permission flag: 'auto' operations it may run by
        # itself; everything else it only ever proposes.
        permission = str(data.get("permission") or "").strip()
        if permission and permission not in ("auto", "ask"):
            raise OperationError("permission must be 'auto' or 'ask'")
        # Round-26 scope: 'general' operations serve the coworker agent and
        # the brain alike; 'brain' operations are the brain's own tools and
        # refuse to run inside an ordinary flow.
        scope = str(data.get("scope") or "").strip()
        if scope and scope not in ("general", "brain"):
            raise OperationError("scope must be 'general' or 'brain'")
        payload = {"name": name,
                   "description": str(data.get("description") or ""),
                   "tags": [str(tag) for tag in (data.get("tags") or [])],
                   "steps": steps}
        if permission:
            payload["permission"] = permission
        if scope:
            payload["scope"] = scope
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
