"""Running a clabe workflow headlessly in tests: prompts are answered by what they ask, not by their order.

Modelled on clabe's own ``tests/workflow/_helpers.py``. Prompts are matched by their question (a confirm's ``label``,
a table's ``prompt``) or by their ``field`` (e.g. ``"acquire.failed"`` for the Retry/Abort prompt of a failed step),
so a test names the questions it answers and fails loudly on one it did not expect.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clabe.workflow import Run, Workflow
from clabe.workflow.events import PromptOpened, StepSkipped, StepStarted


@dataclass
class FakeLauncher:
    """The few `Launcher` members the workflows' own steps use (the clabe steps that need more are replaced)."""

    session_directory: Path
    temp_dir: Path
    session_directories: list[Path] = field(default_factory=list)
    copied_logs: int = 0

    def add_session_directory(self, path: Path) -> Path:
        self.session_directories.append(Path(path))
        return Path(path)

    def copy_logs(self) -> None:
        self.copied_logs += 1


def question(prompt: PromptOpened) -> str:
    """What a prompt asks, as the operator reads it."""
    return str(prompt.spec.get("label") or prompt.spec.get("prompt") or prompt.spec.get("field"))


class AnsweringClient:
    """Answers each prompt from ``answers``, keyed by its question or its ``field``.

    A list answers the same question once per item, in order (e.g. ``[False, True]``: "Go back", then "Looks good").
    A question without an answer aborts the run, so a test never hangs on it.
    """

    def __init__(self, run: Run, answers: Mapping[str, Any]) -> None:
        self.run = run
        self.answers = {key: list(value) if isinstance(value, list) else [value] for key, value in answers.items()}
        self.asked: list[str] = []
        self.errors: list[str] = []
        self.threads: list[threading.Thread] = []
        run.events.subscribe(self._on_event)

    def _on_event(self, event: object) -> None:
        if isinstance(event, PromptOpened):
            self.asked.append(question(event))
            thread = threading.Thread(target=self._respond, args=(event,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _respond(self, prompt: PromptOpened) -> None:
        for key in (question(prompt), prompt.spec.get("field")):
            if self.answers.get(key):
                self.run.answer(prompt.prompt_id, self.answers[key].pop(0), client="test")
                return
        self.errors.append(f"unexpected prompt {question(prompt)!r}: {prompt.spec}")
        self.run.abort("unexpected prompt")


def execute(
    workflow: Workflow, answers: Mapping[str, Any] | None = None, *, launcher: Any = None, timeout: float = 10.0
) -> tuple[Run, AnsweringClient]:
    """Runs ``workflow`` until it ends, answering its prompts from ``answers``."""
    run = Run(workflow, launcher=launcher)
    client = AnsweringClient(run, answers or {})
    run.start()
    finished = run.wait(timeout)
    for thread in client.threads:
        thread.join(timeout)
    assert not client.errors, client.errors
    assert finished, f"the run did not finish; it asked {client.asked}"
    return run, client


def started_steps(run: Run) -> list[str]:
    """The steps the run started, in order (a step run again after going back appears again)."""
    return [event.step for event in run.events.since(0) if isinstance(event, StepStarted)]


def skipped_steps(run: Run) -> dict[str, str]:
    """The steps the run skipped, with why (``"condition"``: their ``when=`` did not hold; ``"declined"``)."""
    return {event.step: event.reason for event in run.events.since(0) if isinstance(event, StepSkipped)}
