# -*- coding: utf-8 -*-
"""settings.json → core/defaults.py 의 DEFAULT_SETTINGS 동기화(일회성·재실행 가능)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZOMBIE = "\n\n# [Zombie Parameter Prevention]"


def py_repr(v: object) -> str:
    if isinstance(v, bool):
        return "True" if v else "False"
    return json.dumps(v, ensure_ascii=False)


def main() -> None:
    with open(ROOT / "settings.json", encoding="utf-8") as f:
        sj = json.load(f)

    skip = {k for k in sj if k.startswith("opt_")}
    skip |= {
        "bt_start_date",
        "bt_end_date",
        "buy_fee_bt",
        "sell_fee_bt",
        "buy_fee_mock",
        "sell_fee_mock",
        "mock_account_id",
    }

    defaults_path = ROOT / "core" / "defaults.py"
    text = defaults_path.read_text(encoding="utf-8")
    start = text.index("DEFAULT_SETTINGS = ")
    zpos = text.index(ZOMBIE, start)
    before = text[: start + len("DEFAULT_SETTINGS = ")]
    tail = text[zpos:]  # \n\n# [Zombie Parameter Prevention]...

    block = text[start + len("DEFAULT_SETTINGS = ") : zpos]
    keys_order = re.findall(r'"([a-z0-9_]+)"\s*:', block)
    seen: set[str] = set()
    ordered: list[str] = []
    for k in keys_order:
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    for k in sorted(sj.keys()):
        if k in skip or k in seen:
            continue
        ordered.append(k)
        seen.add(k)

    inner_lines = ["{"]
    for k in ordered:
        if k in skip or k not in sj:
            continue
        inner_lines.append(f"    {json.dumps(k, ensure_ascii=False)}: {py_repr(sj[k])},")
    inner_lines.append("}")

    new_text = before + "\n".join(inner_lines) + tail
    defaults_path.write_text(new_text, encoding="utf-8")
    print(f"갱신 완료: {defaults_path} (DEFAULT_SETTINGS 항목 {len(inner_lines) - 2}개)")


if __name__ == "__main__":
    main()
