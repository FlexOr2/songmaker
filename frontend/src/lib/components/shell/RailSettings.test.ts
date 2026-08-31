import { mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RAIL_SETTINGS_OPEN_STORAGE_KEY } from '$lib/constants';
import { currentUser } from '$lib/stores/auth';

const { pageState } = vi.hoisted(() => ({
	pageState: { url: new URL('https://songmaker.test/') }
}));

vi.mock('$app/state', () => ({
	page: pageState
}));

import RailSettings from './RailSettings.svelte';

const ADMIN = { id: 'u1', username: 'felix', role: 'admin' as const };
const USER = { id: 'u2', username: 'jane', role: 'user' as const };

let mounted: ReturnType<typeof mount> | undefined;

function requireElement<T extends Element>(root: ParentNode, selector: string): T {
	const element = root.querySelector<T>(selector);
	if (!element) throw new Error(`Expected ${selector} to be rendered`);
	return element;
}

async function render(): Promise<HTMLElement> {
	const target = document.createElement('div');
	document.body.append(target);
	mounted = mount(RailSettings, { target, props: {} });
	await tick();
	return target;
}

function itemLabels(target: HTMLElement): string[] {
	return Array.from(target.querySelectorAll<HTMLAnchorElement>('.row-sub')).map(
		(link) => link.textContent?.trim() ?? ''
	);
}

beforeEach(() => {
	localStorage.clear();
	pageState.url = new URL('https://songmaker.test/');
	currentUser.set(ADMIN);
});

afterEach(async () => {
	if (mounted) await unmount(mounted);
	mounted = undefined;
	document.body.replaceChildren();
	currentUser.set(null);
});

describe('RailSettings', () => {
	it('starts collapsed off a settings route and expands on click', async () => {
		const target = await render();
		const toggle = requireElement<HTMLButtonElement>(target, 'button.disclose');
		expect(toggle.getAttribute('aria-expanded')).toBe('false');

		toggle.click();
		await tick();
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		expect(itemLabels(target)).toEqual([
			'Generation',
			'Playback',
			'Voices',
			'Account',
			'Admin',
			'Cleanup',
			'Legal'
		]);
	});

	it('opens automatically and marks the active section when landing on a settings route', async () => {
		pageState.url = new URL('https://songmaker.test/settings/voices');
		const target = await render();
		const toggle = requireElement<HTMLButtonElement>(target, 'button.disclose');
		expect(toggle.getAttribute('aria-expanded')).toBe('true');

		const active = requireElement<HTMLAnchorElement>(target, '.row-active');
		expect(active.textContent?.trim()).toBe('Voices');
		expect(active.getAttribute('href')).toBe('/settings/voices');
	});

	it('omits admin-only sections for a non-admin', async () => {
		currentUser.set(USER);
		pageState.url = new URL('https://songmaker.test/settings/generation');
		const target = await render();
		expect(itemLabels(target)).toEqual(['Generation', 'Playback', 'Voices', 'Account', 'Legal']);
	});

	it('remembers an open disclosure across a fresh mount via localStorage', async () => {
		const first = await render();
		requireElement<HTMLButtonElement>(first, 'button.disclose').click();
		await tick();
		const firstInstance = mounted;
		if (firstInstance) await unmount(firstInstance);
		mounted = undefined;
		document.body.replaceChildren();

		expect(localStorage.getItem(RAIL_SETTINGS_OPEN_STORAGE_KEY)).toBe('true');

		const second = await render();
		expect(
			requireElement<HTMLButtonElement>(second, 'button.disclose').getAttribute('aria-expanded')
		).toBe('true');
	});

	it('still renders and toggles when localStorage throws', async () => {
		const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
			throw new Error('blocked in private mode');
		});
		const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
			throw new Error('blocked in private mode');
		});

		const target = await render();
		const toggle = requireElement<HTMLButtonElement>(target, 'button.disclose');
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		toggle.click();
		await tick();
		expect(toggle.getAttribute('aria-expanded')).toBe('true');

		getItem.mockRestore();
		setItem.mockRestore();
	});
});
