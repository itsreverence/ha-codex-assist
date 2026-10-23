"""Apply each recorded source mutation and confirm that its test catches it.

Usage: python scripts/check_mutations.py --suite fast|ha [--only NAME]

The fast suite runs under the project environment (`uv run`). The ha suite needs a
Home Assistant test environment, so run it with that environment's python.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = "custom_components/codex_assist/codex_runtime.py"
AI_TASK = "custom_components/codex_assist/ai_task.py"
CONVERSATION = "custom_components/codex_assist/conversation.py"
INTERLEAVINGS = "tests/test_codex_runtime_interleavings.py"
DELIVERY = "tests_ha/test_ai_task_delivery.py"
LOCKED = "        async with self._refresh_lock:\n"
PLAIN_TEXT = f"{DELIVERY}::test_generate_data_returns_native_plain_text_result"
STRUCTURE = f"{DELIVERY}::test_generate_data_service_delivers_selector_validated_structure"
ATTACHMENT = f"{DELIVERY}::test_generate_data_service_sends_local_image_attachment"
IMAGE = f"{DELIVERY}::test_generate_image_service_persists_native_result_as_media"


@dataclass(frozen=True)
class Mutation:
    name: str
    suite: Literal["fast", "ha"]
    path: str
    old: str
    new: str
    test: str
    expect: str


MUTATIONS = (
    Mutation(
        "resolve-without-lock",
        "fast",
        RUNTIME,
        LOCKED + "            return await resolve_runtime_tokens(",
        "        if True:\n            return await resolve_runtime_tokens(",
        INTERLEAVINGS,
        "NoRefreshReuse",
    ),
    Mutation(
        "rejection-without-lock",
        "fast",
        RUNTIME,
        LOCKED + "            entry_data = get_entry_data()",
        "        if True:\n            entry_data = get_entry_data()",
        INTERLEAVINGS,
        "NoRefreshReuse",
    ),
    Mutation(
        "resolve-ignores-newer-tokens",
        "fast",
        RUNTIME,
        "    if latest != tokens:\n        return latest\n",
        "",
        INTERLEAVINGS,
        "StoredTokenUsable",
    ),
    Mutation(
        "rejection-ignores-newer-tokens",
        "fast",
        RUNTIME,
        "            if latest != current:\n                return latest\n",
        "",
        INTERLEAVINGS,
        "StoredTokenUsable",
    ),
    Mutation(
        "rejection-refreshes-unrejected-tokens",
        "fast",
        RUNTIME,
        "            if current != rejected_tokens:\n                return current\n",
        "",
        INTERLEAVINGS,
        "NoRedundantRefresh",
    ),
    Mutation(
        "resolve-yields-before-persist",
        "fast",
        RUNTIME,
        "    if latest != tokens:\n        return latest\n    await _persist",
        "    if latest != tokens:\n        return latest\n    await asyncio.sleep(0)\n"
        "    await _persist",
        INTERLEAVINGS,
        "StoredTokenUsable",
    ),
    Mutation(
        "no-generate-data-flag",
        "ha",
        AI_TASK,
        "        ai_task.AITaskEntityFeature.GENERATE_DATA\n        | ",
        "        ",
        PLAIN_TEXT,
        "does not support generating data",
    ),
    Mutation(
        "no-attachments-flag",
        "ha",
        AI_TASK,
        "\n        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS",
        "",
        ATTACHMENT,
        "does not support attachments",
    ),
    Mutation(
        "no-generate-image-flag",
        "ha",
        AI_TASK,
        "\n        | ai_task.AITaskEntityFeature.GENERATE_IMAGE",
        "",
        IMAGE,
        "does not support generating images",
    ),
    Mutation(
        "empty-data-result",
        "ha",
        AI_TASK,
        "            data=_structured_data_from_text(text, task.structure),",
        '            data="",',
        PLAIN_TEXT,
        "assert '' == 'A concise task response.'",
    ),
    Mutation(
        "no-text-format",
        "ha",
        AI_TASK,
        "            text_format = _structured_output_format(task, chat_log)",
        "            text_format = None",
        STRUCTURE,
        "KeyError: 'format'",
    ),
    Mutation(
        "no-user-attachments",
        "ha",
        CONVERSATION,
        'attachments_by_item[id(item)] = getattr(content, "attachments", None)',
        "attachments_by_item[id(item)] = None",
        ATTACHMENT,
        "isinstance('Describe the supplied image.', list)",
    ),
    Mutation(
        "image-result-as-dict",
        "ha",
        AI_TASK,
        "        return ai_task.GenImageTaskResult(\n",
        "        return dict(\n",
        IMAGE,
        "'dict' object has no attribute 'as_dict'",
    ),
    Mutation(
        "empty-image-data",
        "ha",
        AI_TASK,
        "            image_data=result.image_data,",
        '            image_data=b"",',
        IMAGE,
        r"assert b'' == b'\x89PNG",
    ),
)


def run_pytest(test: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "-p", "no:cacheprovider", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def check(mutation: Mutation) -> str:
    path = ROOT / mutation.path
    original = path.read_bytes()
    eol = "\r\n" if b"\r\n" in original else "\n"
    source = original.decode()
    old = mutation.old.replace("\n", eol)
    if source.count(old) != 1:
        return "anchor not found"
    try:
        path.write_bytes(source.replace(old, mutation.new.replace("\n", eol)).encode())
        result = run_pytest(mutation.test)
    finally:
        path.write_bytes(original)
    assert path.read_bytes() == original, f"{mutation.path} was not restored"
    caught = result.returncode != 0 and mutation.expect in result.stdout + result.stderr
    return "caught" if caught else "SURVIVED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=("fast", "ha"), required=True)
    parser.add_argument("--only", metavar="NAME")
    args = parser.parse_args()
    selected = [m for m in MUTATIONS if m.suite == args.suite and args.only in (None, m.name)]
    if not selected:
        parser.error(f"no {args.suite} mutation named {args.only}")

    ok = True
    for mutation in selected:
        status = check(mutation)
        print(f"{status}: {mutation.name}")
        ok &= status == "caught"
    for test in dict.fromkeys(m.test for m in selected):
        passed = run_pytest(test).returncode == 0
        print(f"{'passes' if passed else 'FAILS'} unmutated: {test}")
        ok &= passed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
