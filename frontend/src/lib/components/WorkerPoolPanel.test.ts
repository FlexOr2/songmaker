import { mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkerPoolResponse } from '$lib/api/types';

const api = vi.hoisted(() => ({
	listWorkers: vi.fn()
}));

vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/client')>();
	return { ...actual, ...api };
});

import WorkerPoolPanel from './WorkerPoolPanel.svelte';

let mounted: ReturnType<typeof mount> | undefined;

function workerPool(vramMeasured: boolean | null): WorkerPoolResponse {
	return {
		workers: [
			{
				identity: {
					id: 'acestep-worker-0',
					host: 'worker',
					port: 8100,
					gpu_id: 0,
					vram_total_gb: 24,
					registered_at: '2026-09-01T10:00:00Z',
					last_register_at: '2026-09-01T10:00:00Z'
				},
				state: {
					loaded: [],
					target_loading: null,
					loading_started_at: null,
					loading_last_log_line: null,
					queue_depth: 0,
					vram_used_gb: 12.4,
					vram_total_gb: 24,
					vram_measured: vramMeasured,
					available_modes: [],
					pinned: [],
					last_heartbeat_at: '2026-09-01T10:00:00Z'
				},
				status: 'online'
			}
		]
	};
}

async function renderVram(vramMeasured: boolean | null): Promise<HTMLSpanElement> {
	api.listWorkers.mockResolvedValue(workerPool(vramMeasured));
	const target = document.createElement('div');
	document.body.append(target);
	mounted = mount(WorkerPoolPanel, { target, props: { availableModes: [] } });
	await tick();
	await Promise.resolve();
	await tick();
	const row = Array.from(target.querySelectorAll('.card-row')).find(
		(candidate) => candidate.querySelector('.row-label')?.textContent === 'VRAM:'
	);
	const value = row?.querySelector<HTMLSpanElement>('.row-value');
	if (!value) throw new Error('Expected the VRAM value to be rendered');
	return value;
}

beforeEach(() => {
	api.listWorkers.mockReset();
});

afterEach(async () => {
	if (mounted) await unmount(mounted);
	mounted = undefined;
	document.body.replaceChildren();
});

function offlineWorkerPool(lastRegisterAt: string): WorkerPoolResponse {
	return {
		workers: [
			{
				identity: {
					id: 'acestep-worker-0',
					host: 'worker',
					port: 8100,
					gpu_id: 0,
					vram_total_gb: 24,
					registered_at: '2026-08-24T11:10:00Z',
					last_register_at: lastRegisterAt
				},
				state: null,
				status: 'offline'
			}
		]
	};
}

async function renderOffline(lastRegisterAt: string): Promise<HTMLElement> {
	api.listWorkers.mockResolvedValue(offlineWorkerPool(lastRegisterAt));
	const target = document.createElement('div');
	document.body.append(target);
	mounted = mount(WorkerPoolPanel, { target, props: { availableModes: [] } });
	await tick();
	await Promise.resolve();
	await tick();
	return target;
}

describe('WorkerPoolPanel with no heartbeat', () => {
	it('names how long the worker has been gone instead of just "no heartbeat"', async () => {
		// The GPU worker's container sat on "Created" for five days without
		// ever registering again (issue #252's live incident). "Offline (no
		// heartbeat)" told the operator nothing about how long that had been
		// true; last_register_at was already on the wire and unused.
		const sixDaysAgo = new Date(Date.now() - 6 * 24 * 60 * 60 * 1000).toISOString();
		const target = await renderOffline(sixDaysAgo);

		expect(target.textContent).toContain('last registered');
		expect(target.textContent).toContain('6d ago');
		expect(target.textContent).not.toContain('no heartbeat');
	});

	it('explains why Restart cannot help instead of leaving it clickable into a 502', async () => {
		const target = await renderOffline(new Date().toISOString());

		const restartButton = Array.from(target.querySelectorAll<HTMLButtonElement>('button')).find(
			(button) => button.textContent?.includes('Restart')
		);
		if (!restartButton) throw new Error('Expected a Restart button to be rendered');

		expect(restartButton.disabled).toBe(true);
		// The hint must name which worker's container is meant -- there can be
		// several -- not just say "its container" in the abstract.
		expect(restartButton.title).toContain('acestep-worker-0');
		expect(target.textContent).toContain('no worker process is running');
		expect(target.textContent).toContain('worker "acestep-worker-0"');
	});
});

function heartbeatingButOfflineWorkerPool(
	gpuHealthy: boolean | null,
	gpuHealthDetail: string | null
): WorkerPoolResponse {
	return {
		workers: [
			{
				identity: {
					id: 'acestep-worker-0',
					host: 'worker',
					port: 8100,
					gpu_id: 0,
					vram_total_gb: 24,
					registered_at: '2026-08-24T11:10:00Z',
					last_register_at: '2026-09-02T09:00:00Z'
				},
				state: {
					loaded: [{ mode: 'sft', size_gb: 6 }],
					target_loading: null,
					loading_started_at: null,
					loading_last_log_line: null,
					queue_depth: 0,
					vram_used_gb: null,
					vram_total_gb: 24,
					vram_measured: null,
					available_modes: ['sft'],
					pinned: [],
					last_heartbeat_at: new Date().toISOString(),
					gpu_healthy: gpuHealthy,
					gpu_health_detail: gpuHealthDetail
				},
				status: 'offline'
			}
		]
	};
}

async function renderHeartbeatingButOffline(
	gpuHealthy: boolean | null,
	gpuHealthDetail: string | null
): Promise<HTMLElement> {
	api.listWorkers.mockResolvedValue(heartbeatingButOfflineWorkerPool(gpuHealthy, gpuHealthDetail));
	const target = document.createElement('div');
	document.body.append(target);
	mounted = mount(WorkerPoolPanel, { target, props: { availableModes: [] } });
	await tick();
	await Promise.resolve();
	await tick();
	return target;
}

describe('WorkerPoolPanel with a heartbeating but GPU-broken worker', () => {
	// Issue #367: a worker whose GPU has gone away keeps heartbeating just
	// fine, so the API correctly marks it "offline" while state stays
	// non-null. The panel must not read state-presence as "Idle" — the red
	// status icon and the row text must agree, and nothing must be
	// clickable.
	it('names the GPU failure from the heartbeat and disables every action', async () => {
		const target = await renderHeartbeatingButOffline(false, 'Driver/library version mismatch');

		expect(target.textContent).toContain('GPU unavailable — Driver/library version mismatch');
		expect(target.textContent).not.toContain('Idle');
		expect(target.textContent).not.toContain('just now');

		const buttons = Array.from(target.querySelectorAll<HTMLButtonElement>('button'));
		expect(buttons.length).toBeGreaterThan(0);
		for (const button of buttons) {
			expect(button.disabled).toBe(true);
		}
		const select = target.querySelector<HTMLSelectElement>('select.mode-select');
		expect(select?.disabled).toBe(true);
	});

	it('names an old worker build that never learned to report GPU health', async () => {
		const target = await renderHeartbeatingButOffline(null, null);

		expect(target.textContent).toContain('GPU health not reported');
		expect(target.textContent).not.toContain('Idle');

		const buttons = Array.from(target.querySelectorAll<HTMLButtonElement>('button'));
		expect(buttons.length).toBeGreaterThan(0);
		for (const button of buttons) {
			expect(button.disabled).toBe(true);
		}
	});
});

describe('WorkerPoolPanel VRAM measurement', () => {
	it('marks estimated VRAM usage', async () => {
		const value = await renderVram(false);

		expect(value).toHaveTextContent('~12.4 / 24.0 GB est.');
		expect(value).toHaveAttribute('title', 'Estimated VRAM usage');
	});

	it.each([true, null])('leaves measured or legacy VRAM usage unchanged (%s)', async (measured) => {
		const value = await renderVram(measured);

		expect(value).toHaveTextContent('12.4 / 24.0 GB');
		expect(value).not.toHaveAttribute('title');
	});
});
