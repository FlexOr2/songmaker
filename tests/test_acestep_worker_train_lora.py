"""Tests for the acestep_worker /tasks/train_lora endpoint + SSE progress.

The default_train_lora_runner is tested indirectly here; we install a
fake runner that emits a deterministic progress/done sequence so we can
assert event ordering and that the cache serialize-lock makes training
incompatible with concurrent generation on the same subprocess."""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from acestep_worker.heartbeat import HeartbeatLoop
from acestep_worker.model_cache import LoadedModel, ModelCache
from acestep_worker.models import TrainLoraRequest, TrainLoraTaskResult
from acestep_worker.task_store import TaskStore
from acestep_worker.wrapper import (
    WorkerDeps,
    create_app,
    default_train_lora_runner,
)


def _run(coro):
    return asyncio.run(coro)


def _make_deps(tmp_path: Path, with_train_runner: bool = True) -> WorkerDeps:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    operation_events: list[str] = []

    async def loader(mode: str) -> LoadedModel:
        operation_events.append(f"load_model:{mode}")
        return LoadedModel(mode=mode, handle=f"handle-{mode}", port=8101)

    async def unloader(_: LoadedModel) -> None:
        return None

    cache = ModelCache(
        vram_budget_gb=24.0,
        model_sizes={"sft": 6.0},
        loader=loader,
        unloader=unloader,
    )
    task_store = TaskStore()

    async def fake_generate_runner(*args, **kwargs):
        await task_store.mark_running(kwargs.get("task_id") or args[1])

    train_events: list[str] = []

    async def fake_train_runner(
        store: TaskStore, task_id: str,
        *, request: TrainLoraRequest, port: int, checkpoint_dir: Path,
        shared_audio_root: Path,
    ) -> None:
        operation_events.append(f"train_lora:{request.mode}")
        train_events.append(f"start:{port}")
        await store.mark_running(task_id)
        await store.update_progress(task_id, 0.1)
        await store.update_progress(task_id, 0.5)
        await store.complete(
            task_id,
            TrainLoraTaskResult(
                mode=request.mode,
                adapter_dir=str(Path(request.output_dir) / "final"),
                num_samples=3,
            ),
        )

    deps = WorkerDeps(
        worker_id="w0",
        cache=cache,
        task_store=task_store,
        heartbeat=None,  # type: ignore[arg-type]
        redis=redis,
        registry_client=None,
        registration=None,
        checkpoint_dir=tmp_path,
        audio_output_dir=tmp_path / "audio",
        generate_runner=fake_generate_runner,
        train_lora_runner=fake_train_runner if with_train_runner else None,
    )
    deps.heartbeat = HeartbeatLoop(
        redis=redis, worker_id="w0",
        state_provider=lambda: _state(deps),
    )
    deps._train_events = train_events  # type: ignore[attr-defined]
    deps._operation_events = operation_events  # type: ignore[attr-defined]
    return deps


async def _state(deps: WorkerDeps) -> dict[str, Any]:
    return {"loaded": deps.cache.loaded_modes(), "queue_depth": 0}


def _auth_headers() -> dict[str, str]:
    from songmaker_cli.internal_api import INTERNAL_TOKEN_HEADER

    return {INTERNAL_TOKEN_HEADER: "test-internal-token"}


def test_train_lora_requires_loaded_mode(tmp_path: Path) -> None:
    deps = _make_deps(tmp_path)
    app = create_app(deps)
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/train_lora",
            json={"mode": "sft", "dataset_dir": "/tmp/d", "output_dir": "/tmp/o"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 409


def test_train_lora_501_when_runner_missing(tmp_path: Path) -> None:
    deps = _make_deps(tmp_path, with_train_runner=False)
    _run(deps.cache.load("sft"))
    app = create_app(deps)
    with TestClient(app) as client:
        resp = client.post(
            "/tasks/train_lora",
            json={"mode": "sft", "dataset_dir": "/tmp/d", "output_dir": "/tmp/o"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 501


def test_train_lora_happy_path_emits_sse_events(tmp_path: Path) -> None:
    deps = _make_deps(tmp_path)
    app = create_app(deps)
    with TestClient(app) as client:
        loaded = client.post(
            "/load_model", json={"mode": "sft"}, headers=_auth_headers(),
        )
        assert loaded.status_code == 200
        resp = client.post(
            "/tasks/train_lora",
            json={
                "mode": "sft",
                "dataset_dir": str(tmp_path / "ds"),
                "output_dir": str(tmp_path / "out"),
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]
        with client.stream(
            "GET", f"/tasks/{task_id}/stream", headers=_auth_headers(),
        ) as stream:
            events = []
            for chunk in stream.iter_text():
                events.append(chunk)
                if "\"done\"" in "".join(events) or 'event: done' in "".join(events):
                    break
    joined = "".join(events)
    assert "event: done" in joined
    assert "adapter_dir" in joined
    assert deps._operation_events == [  # type: ignore[attr-defined]
        "load_model:sft", "train_lora:sft",
    ]


def test_train_lora_pins_mode_from_eviction(tmp_path: Path) -> None:
    """A training task in flight increments the cache's in-use count so
    the mode is protected from eviction — the same mechanism generation
    uses to serialize GPU work on the underlying ACE-Step subprocess."""
    deps = _make_deps(tmp_path)
    _run(deps.cache.load("sft"))

    release_event = asyncio.Event()

    async def blocking_runner(
        store: TaskStore, task_id: str,
        *, request: TrainLoraRequest, port: int, checkpoint_dir: Path,
        shared_audio_root: Path,
    ) -> None:
        await store.mark_running(task_id)
        await release_event.wait()
        await store.complete(
            task_id,
            TrainLoraTaskResult(
                mode=request.mode, adapter_dir="/tmp/x", num_samples=0,
            ),
        )

    deps.train_lora_runner = blocking_runner

    async def scenario() -> None:
        from acestep_worker.wrapper import spawn_background

        task_id = await deps.task_store.create("train_lora")
        from acestep_worker.downloads import spawn_background as _sp  # noqa: F401

        loaded = await deps.cache.acquire_for_use("sft")
        spawn_background(
            blocking_runner(deps.task_store, task_id, request=TrainLoraRequest(
                mode="sft", dataset_dir="/x", output_dir="/y",
            ), port=loaded.port, checkpoint_dir=deps.checkpoint_dir,
            shared_audio_root=deps.audio_output_dir.parent),
        )
        await asyncio.sleep(0)
        assert deps.cache._in_use.get("sft", 0) == 1
        release_event.set()
        await asyncio.sleep(0.05)
        await deps.cache.release("sft")

    _run(scenario())


def test_default_train_lora_runner_dispatches(tmp_path: Path) -> None:
    """Sanity test that the real default_train_lora_runner is wired to
    an AceStepTrainingClient; patch the client methods to avoid HTTP."""
    from unittest.mock import patch

    from acestep_engine.training_client import (
        ExportResult,
        PreprocessStatus,
        PreprocessTaskHandle,
        ScanDatasetResult,
        TrainingStartedHandle,
        TrainingStatus,
    )

    task_store = TaskStore()
    task_id = _run(task_store.create("train_lora"))

    output_dir = tmp_path / "shared" / "out"
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"audio")
    (dataset_dir / "sample.wav").symlink_to(sample)

    request = TrainLoraRequest(
        mode="sft", dataset_dir=str(dataset_dir), output_dir=str(output_dir),
        train_epochs=1, poll_interval_seconds=0.001,
    )

    preprocess_states = iter([
        PreprocessStatus(
            task_id="pre", status="running", progress="1/2", current=1, total=2,
        ),
        PreprocessStatus(
            task_id="pre", status="completed", progress="done", current=2, total=2,
        ),
    ])
    training_states = iter([
        TrainingStatus(is_training=True, current_epoch=0, current_loss=0.5),
        TrainingStatus(is_training=False, current_epoch=1, current_loss=0.2),
    ])

    calls: list[tuple[str, object]] = []

    def initialize_model(mode: str) -> None:
        calls.append(("initialize_model", mode))

    def scan_dataset(path: str) -> ScanDatasetResult:
        staged_sample = Path(path) / "sample.wav"
        assert staged_sample.read_bytes() == b"audio"
        assert not staged_sample.is_symlink()
        calls.append(("scan_dataset", path))
        return ScanDatasetResult(num_samples=3)

    def export_training(source: str, destination: str) -> ExportResult:
        calls.append(("export_training", (source, destination)))
        source_dir = Path(source) / "final"
        source_dir.mkdir(parents=True)
        (source_dir / "lokr_weights.safetensors").write_bytes(b"weights")
        shutil.copytree(source_dir, destination)
        return ExportResult(export_path=destination, source=str(source_dir))

    with (
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.initialize_model",
            side_effect=initialize_model,
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.scan_dataset",
            side_effect=scan_dataset,
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.start_preprocess",
            return_value=PreprocessTaskHandle(task_id="pre", total=2),
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.poll_preprocess",
            side_effect=lambda tid: next(preprocess_states),
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.start_lokr",
            return_value=TrainingStartedHandle(
                tensor_dir="unused", output_dir="unused",
            ),
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.poll_training",
            side_effect=lambda: next(training_states),
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.export_training",
            side_effect=export_training,
        ),
    ):
        _run(
            default_train_lora_runner(
                task_store, task_id, request=request, port=8001,
                checkpoint_dir=tmp_path / "safe-root",
                shared_audio_root=tmp_path,
            ),
        )

    snap = _run(task_store.get(task_id))
    assert snap is not None
    assert snap.state == "done"
    assert snap.result is not None
    assert snap.result.adapter_dir == str(output_dir)
    assert (output_dir / "lokr_weights.safetensors").read_bytes() == b"weights"
    assert calls[0] == ("initialize_model", "sft")
    assert calls[1] == (
        "scan_dataset",
        str(tmp_path / "safe-root" / "training" / task_id / "dataset"),
    )
    source, destination = calls[-1][1]
    assert source == str(tmp_path / "safe-root" / "training" / task_id / "output")
    assert destination == str(tmp_path / "safe-root" / "training" / task_id / "export")
    assert source != destination
    assert not (tmp_path / "safe-root" / "training" / task_id).exists()


def test_default_train_lora_runner_fails_on_preprocess_error(tmp_path: Path) -> None:
    from unittest.mock import patch

    from acestep_engine.training_client import (
        PreprocessStatus,
        PreprocessTaskHandle,
        ScanDatasetResult,
    )

    task_store = TaskStore()
    task_id = _run(task_store.create("train_lora"))
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    request = TrainLoraRequest(
        mode="sft", dataset_dir=str(source_dir), output_dir=str(tmp_path / "out"),
        train_epochs=1, poll_interval_seconds=0.001,
    )
    with (
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.initialize_model",
            return_value=None,
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.scan_dataset",
            return_value=ScanDatasetResult(num_samples=3),
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.start_preprocess",
            return_value=PreprocessTaskHandle(task_id="pre", total=2),
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.poll_preprocess",
            return_value=PreprocessStatus(
                task_id="pre", status="failed", error="OOM", progress="",
                current=0, total=2,
            ),
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.stop_training",
            return_value=None,
        ),
    ):
        _run(
            default_train_lora_runner(
                task_store, task_id, request=request, port=8001,
                checkpoint_dir=tmp_path / "safe-root",
                shared_audio_root=tmp_path,
            ),
        )
    snap = _run(task_store.get(task_id))
    assert snap.state == "error"
    assert "OOM" in (snap.error or "")
    assert not (tmp_path / "safe-root" / "training" / task_id).exists()


def test_default_train_lora_runner_fails_on_zero_samples(tmp_path: Path) -> None:
    from unittest.mock import patch

    from acestep_engine.training_client import ScanDatasetResult

    task_store = TaskStore()
    task_id = _run(task_store.create("train_lora"))
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    request = TrainLoraRequest(
        mode="sft", dataset_dir=str(source_dir), output_dir=str(tmp_path / "out"),
        train_epochs=1, poll_interval_seconds=0.001,
    )
    with (
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.initialize_model",
            return_value=None,
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.scan_dataset",
            return_value=ScanDatasetResult(num_samples=0),
        ),
        patch(
            "acestep_engine.training_client.AceStepTrainingClient.stop_training",
            return_value=None,
        ),
    ):
        _run(
            default_train_lora_runner(
                task_store, task_id, request=request, port=8001,
                checkpoint_dir=tmp_path / "safe-root",
                shared_audio_root=tmp_path,
            ),
        )
    snap = _run(task_store.get(task_id))
    assert snap.state == "error"


def test_foreign_output_is_rejected_before_fork_safe_path(tmp_path: Path) -> None:
    from unittest.mock import patch

    task_store = TaskStore()
    task_id = _run(task_store.create("train_lora"))
    shared_root = tmp_path / "shared"
    dataset_dir = shared_root / "dataset"
    dataset_dir.mkdir(parents=True)
    request = TrainLoraRequest(
        mode="sft",
        dataset_dir=str(dataset_dir),
        output_dir=str(tmp_path / "foreign-output"),
        train_epochs=1,
        poll_interval_seconds=0.001,
    )

    with patch(
        "acestep_engine.training_client.AceStepTrainingClient.initialize_model",
    ) as initialize_model:
        _run(default_train_lora_runner(
            task_store,
            task_id,
            request=request,
            port=8001,
            checkpoint_dir=tmp_path / "safe-root",
            shared_audio_root=shared_root,
        ))

    snap = _run(task_store.get(task_id))
    assert snap.state == "error"
    assert "outside shared audio root" in (snap.error or "")
    initialize_model.assert_not_called()
    assert not (tmp_path / "safe-root" / "training" / task_id).exists()


def test_existing_shared_output_is_not_reused_or_removed(tmp_path: Path) -> None:
    from acestep_worker.wrapper import _shared_audio_path

    shared_root = tmp_path / "shared"
    output_dir = shared_root / "training_tmp"
    output_dir.mkdir(parents=True)
    marker = output_dir / "keep"
    marker.write_text("keep")

    with pytest.raises(ValueError, match="must be a new shared directory"):
        _shared_audio_path(str(output_dir), shared_root, "output")

    assert marker.read_text() == "keep"


def test_cancellation_waits_for_staging_copy_before_workspace_cleanup(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    task_store = TaskStore()
    task_id = _run(task_store.create("train_lora"))
    shared_root = tmp_path / "shared"
    dataset_dir = shared_root / "dataset"
    dataset_dir.mkdir(parents=True)
    request = TrainLoraRequest(
        mode="sft",
        dataset_dir=str(dataset_dir),
        output_dir=str(shared_root / "training_tmp"),
        train_epochs=1,
        poll_interval_seconds=0.001,
    )
    copy_started = threading.Event()
    copy_release = threading.Event()
    workspace = tmp_path / "safe-root" / "training" / task_id

    def blocking_copytree(source, destination, *, symlinks):
        copy_started.set()
        copy_release.wait()
        return destination

    async def cancel_during_copy() -> None:
        runner = asyncio.create_task(default_train_lora_runner(
            task_store,
            task_id,
            request=request,
            port=8001,
            checkpoint_dir=tmp_path / "safe-root",
            shared_audio_root=shared_root,
        ))
        await asyncio.to_thread(copy_started.wait)
        runner.cancel()
        await asyncio.sleep(0)
        assert workspace.exists()
        copy_release.set()
        with pytest.raises(asyncio.CancelledError):
            await runner

    with patch(
        "acestep_worker.wrapper.shutil.copytree",
        side_effect=blocking_copytree,
    ), patch(
        "acestep_engine.training_client.AceStepTrainingClient.stop_training",
        return_value=None,
    ):
        _run(cancel_during_copy())

    assert not workspace.exists()
