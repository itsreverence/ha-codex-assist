"""Apply each recorded source mutation and confirm that its test catches it.

Usage: python scripts/check_mutations.py --suite fast|ha [--only NAME]

The fast suite runs under the project environment (`uv run`). The ha suite needs a
Home Assistant test environment, so run it with that environment's python.

Mutations are applied to a temporary copy of the checkout, never to the checkout
itself, so an interrupted or concurrent run cannot leave a mutated file behind.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

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
    path: str
    old: str
    new: str
    test: str
    expect: str

    @property
    def suite(self) -> str:
        return "ha" if self.test.startswith("tests_ha/") else "fast"


MUTATIONS = (
    Mutation(
        name="resolve-without-lock",
        path=RUNTIME,
        old=LOCKED + "            return await resolve_runtime_tokens(",
        new="        if True:\n            return await resolve_runtime_tokens(",
        test=INTERLEAVINGS,
        expect="NoRefreshReuse",
    ),
    Mutation(
        name="rejection-without-lock",
        path=RUNTIME,
        old=LOCKED + "            entry_data = get_entry_data()",
        new="        if True:\n            entry_data = get_entry_data()",
        test=INTERLEAVINGS,
        expect="NoRefreshReuse",
    ),
    Mutation(
        name="resolve-ignores-newer-tokens",
        path=RUNTIME,
        old="    if latest != tokens:\n        return latest\n",
        new="",
        test=INTERLEAVINGS,
        expect="StoredTokenUsable",
    ),
    Mutation(
        name="rejection-ignores-newer-tokens",
        path=RUNTIME,
        old="            if latest != current:\n                return latest\n",
        new="",
        test=INTERLEAVINGS,
        expect="StoredTokenUsable",
    ),
    Mutation(
        name="rejection-refreshes-unrejected-tokens",
        path=RUNTIME,
        old="            if current != rejected_tokens:\n                return current\n",
        new="",
        test=INTERLEAVINGS,
        expect="NoRedundantRefresh",
    ),
    Mutation(
        name="resolve-yields-before-persist",
        path=RUNTIME,
        old="    if latest != tokens:\n        return latest\n    await _persist",
        new="    if latest != tokens:\n        return latest\n    await asyncio.sleep(0)\n"
        "    await _persist",
        test=INTERLEAVINGS,
        expect="StoredTokenUsable",
    ),
    Mutation(
        name="no-generate-data-flag",
        path=AI_TASK,
        old="        ai_task.AITaskEntityFeature.GENERATE_DATA\n        | ",
        new="        ",
        test=PLAIN_TEXT,
        expect="does not support generating data",
    ),
    Mutation(
        name="no-attachments-flag",
        path=AI_TASK,
        old="\n        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS",
        new="",
        test=ATTACHMENT,
        expect="does not support attachments",
    ),
    Mutation(
        name="no-generate-image-flag",
        path=AI_TASK,
        old="\n        | ai_task.AITaskEntityFeature.GENERATE_IMAGE",
        new="",
        test=IMAGE,
        expect="does not support generating images",
    ),
    Mutation(
        name="empty-data-result",
        path=AI_TASK,
        old="            data=_structured_data_from_text(text, task.structure),",
        new='            data="",',
        test=PLAIN_TEXT,
        expect="assert '' == 'A concise task response.'",
    ),
    Mutation(
        name="no-text-format",
        path=AI_TASK,
        old="            text_format = _structured_output_format(task, chat_log)",
        new="            text_format = None",
        test=STRUCTURE,
        expect="structured AI Task request has no JSON schema format",
    ),
    Mutation(
        name="no-user-attachments",
        path=CONVERSATION,
        old='attachments_by_item[id(item)] = getattr(content, "attachments", None)',
        new="attachments_by_item[id(item)] = None",
        test=ATTACHMENT,
        expect="isinstance('Describe the supplied image.', list)",
    ),
    Mutation(
        name="image-result-as-dict",
        path=AI_TASK,
        old="        return ai_task.GenImageTaskResult(\n",
        new="        return dict(\n",
        test=IMAGE,
        expect="'dict' object has no attribute 'as_dict'",
    ),
    Mutation(
        name="empty-image-data",
        path=AI_TASK,
        old="            image_data=result.image_data,",
        new='            image_data=b"",',
        test=IMAGE,
        expect=r"assert b'' == b'\x89PNG",
    ),
)


def copy_checkout(tree: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    for name in filter(None, listed.split("\0")):
        source = ROOT / name
        if source.is_file():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def run_pytest(test: str, tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q"],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def caught(returncode: int, output: str, expect: str) -> bool:
    return returncode != 0 and expect in output


def check(mutation: Mutation, tree: Path) -> str:
    path = tree / mutation.path
    original = path.read_bytes()
    eol = "\r\n" if b"\r\n" in original else "\n"
    source = original.decode()
    old = mutation.old.replace("\n", eol)
    matches = source.count(old)
    if matches == 0:
        return "anchor missing"
    if matches > 1:
        return f"anchor ambiguous ({matches} matches)"
    path.write_bytes(source.replace(old, mutation.new.replace("\n", eol)).encode())
    try:
        result = run_pytest(mutation.test, tree)
    finally:
        path.write_bytes(original)
    output = result.stdout + result.stderr
    return "caught" if caught(result.returncode, output, mutation.expect) else "SURVIVED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=("fast", "ha"), required=True)
    parser.add_argument("--only", metavar="NAME")
    args = parser.parse_args()
    selected = [m for m in MUTATIONS if m.suite == args.suite and args.only in (None, m.name)]
    if not selected:
        parser.error(f"no {args.suite} mutation named {args.only}")

    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp)
        copy_checkout(tree)
        for test in dict.fromkeys(m.test for m in selected):
            baseline = run_pytest(test, tree)
            if baseline.returncode != 0:
                print(baseline.stdout + baseline.stderr)
                print(f"FAILS unmutated: {test}. Mutation results mean nothing until it passes.")
                return 1
            print(f"passes unmutated: {test}")
        ok = True
        for mutation in selected:
            status = check(mutation, tree)
            print(f"{status}: {mutation.name}")
            ok &= status == "caught"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
