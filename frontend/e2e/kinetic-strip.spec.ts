// The take strip's kinetic scrolling (issue #358): jsdom cannot compute
// layout, so the unit suite can only pin what's honestly checkable there —
// the axis-selection contract and the momentum/friction math against faked
// timers. Whether a drag actually moves the strip, whether momentum coasts
// past the release point, whether a click that catches a still-rolling strip
// stays silent, and which axis a real container query puts the strip on can
// only be shown here, against a real render.
//
// The strip only ever renders on the desktop shell today (the compact
// shell's Takes tab uses TakesList instead — see WriteColumn.svelte and
// SongDetailView.svelte) — the second test below is the honest proof of
// that at a real phone viewport, not a kinetic-scrolling proof, since there
// is nothing of this action's to exercise on that surface yet.

import { expect, request, test, type Page } from '@playwright/test';
import { EDITOR_VIEW_COWRITER_LABEL, TRANSPORT_PAUSE_LABEL } from '../src/lib/constants';
import { nowPlayingTakeLabel } from '../src/lib/constants/now-playing';
import { DESKTOP_VIEWPORT, FlowGuard, MOBILE_VIEWPORT } from './helpers';
import { BASE_URL, readSeededLibrary, seedTakeStripSong, STORAGE_STATE_FILE } from './seed';

/** Its own song (see seedTakeStripSong) rather than one of the base seed's
 *  SONG_TITLES, which other flows in the same run depend on staying at the
 *  base seed's one take each. */
const KINETIC_STRIP_SONG_TITLE = 'Kinetic Strip Takes';

/**
 * Takes seeded for that song — enough for the strip to genuinely overflow its
 * container in both the row and the column layout. Measured directly against
 * the real render, not assumed: the column layout's own container only turns
 * out to be height-bounded once its content exceeds it (~503px at
 * DESKTOP_VIEWPORT, ~35.5px per chip — under about 14 chips it simply grows
 * to fit everything, since the fix below is what makes it stop doing that),
 * and the row layout's own container is ~591px wide at ROW_LAYOUT_VIEWPORT
 * against ~72.7px per chip. 25 total takes overflows both with headroom.
 */
const TAKE_COUNT = 25;

/**
 * A container width below the `@container editor (min-width: 680px)` switch
 * in WriteColumn.svelte, but still well above the app's own 768px compact
 * breakpoint — measured directly against the real container query (not
 * assumed): 900px keeps the shell on its desktop layout while the editor
 * region it hosts renders under 680px, so the strip renders as a row. Kept
 * as its own narrower context rather than the desktop project's own 1440px
 * viewport, which renders the column layout instead (also measured).
 */
const ROW_LAYOUT_VIEWPORT = { width: 900, height: 900 };

/**
 * What this spec costs the API per test, measured on a green run against a
 * clean stack: a cold song open, the co-writer toggle, and playing two takes
 * (~31-32 requests for the column and row tests; the mobile-absence check
 * costs ~15 and never comes close). One shared ceiling with headroom — a flow
 * that suddenly needs more round trips is a regression, find the extra
 * requests instead of raising the number.
 */
const KINETIC_STRIP_FLOW_API_REQUEST_BUDGET = 40;

function expectedSongSlug(title: string): string {
	return title.toLowerCase().replace(/\s+/g, '-');
}

test.beforeAll(async () => {
	const library = readSeededLibrary();
	const api = await request.newContext({ baseURL: BASE_URL, storageState: STORAGE_STATE_FILE });
	try {
		await seedTakeStripSong(api, library.albumId, KINETIC_STRIP_SONG_TITLE, TAKE_COUNT);
	} finally {
		await api.dispose();
	}
});

/** Chip labels in the strip's real DOM order: newest take first (TakeStrip.svelte's sort). */
function takeLabelsNewestFirst(): string[] {
	return Array.from({ length: TAKE_COUNT }, (_, i) => nowPlayingTakeLabel(null, TAKE_COUNT - i));
}

async function openKineticStripSongCoWriter(page: Page): Promise<void> {
	const library = readSeededLibrary();
	const songAddress = `/album/${library.albumId}/${expectedSongSlug(KINETIC_STRIP_SONG_TITLE)}`;
	await page.goto(songAddress);
	await expect(page.getByRole('heading', { name: KINETIC_STRIP_SONG_TITLE })).toBeVisible();
	await page.getByRole('button', { name: EDITOR_VIEW_COWRITER_LABEL }).click();
	const strip = page.locator('.take-strip');
	await strip.waitFor();
	// At the row layout's narrower width the strip sits below a lot of other
	// content in the single stacked column, well past the fold — a real user
	// scrolls the page to reach it, and a raw page.mouse coordinate this test
	// computes from its bounding box needs it to already be on screen.
	await strip.scrollIntoViewIfNeeded();
}

/** The strip's own scroll position — a structural selector, like `.settings-sidebar`
 *  elsewhere in this suite (README): `.take-strip` carries no accessible role of its
 *  own to select by. */
function stripScroll(page: Page): Promise<{ left: number; top: number; flexDirection: string }> {
	return page.locator('.take-strip').evaluate((el: HTMLElement) => ({
		left: el.scrollLeft,
		top: el.scrollTop,
		flexDirection: getComputedStyle(el).flexDirection
	}));
}

/**
 * Home/End and the arrow keys each reveal their target with a native smooth
 * `scrollIntoView` — a real key press or two, not a burst, so waits for that
 * animation to actually finish (two unchanged reads in a row) before the next
 * key, rather than a fixed sleep racing an animation of unknown duration.
 */
async function waitForStripScrollSettled(page: Page): Promise<void> {
	await page.waitForFunction(
		() => {
			const el = document.querySelector('.take-strip') as
				(HTMLElement & { __lastScrollCheck?: string }) | null;
			if (!el) return false;
			const value = `${el.scrollLeft},${el.scrollTop}`;
			const settled = el.__lastScrollCheck === value;
			el.__lastScrollCheck = value;
			return settled;
		},
		{ timeout: 2000, polling: 60 }
	);
}

async function expectNothingPlaying(page: Page): Promise<void> {
	await expect(
		page.getByRole('contentinfo').getByRole('button', { name: TRANSPORT_PAUSE_LABEL, exact: true })
	).not.toBeVisible();
}

test.describe('kinetic take strip', () => {
	let guard: FlowGuard;

	test.beforeEach(({ page }) => {
		guard = new FlowGuard(page);
	});

	// eslint-disable-next-line no-empty-pattern -- Playwright requires the object-destructuring form even with no fixture named
	test.afterEach(({}, testInfo) => {
		console.log(`Kinetic-strip flow /api requests (${testInfo.title}): ${guard.apiRequestCount}`);
		guard.assertClean();
		guard.assertWithinBudget(KINETIC_STRIP_FLOW_API_REQUEST_BUDGET);
	});

	test('drags, coasts and stops on click without opening the take it lands on, in the column layout the strip renders in at desktop width', async ({
		browser,
		isMobile
	}) => {
		test.skip(Boolean(isMobile), 'this test opens its own desktop-width context');
		const context = await browser.newContext({ viewport: DESKTOP_VIEWPORT });
		const page = await context.newPage();
		guard = new FlowGuard(page);
		const labels = takeLabelsNewestFirst();

		await openKineticStripSongCoWriter(page);
		const before = await stripScroll(page);
		expect(before.flexDirection).toBe('column'); // the axis this test actually proves against

		const box = await page.locator('.take-strip').boundingBox();
		if (!box) throw new Error('take strip did not render a box');
		const cx = box.x + box.width / 2;
		const startY = box.y + box.height * 0.85;
		const endY = box.y + box.height * 0.15;

		await expectNothingPlaying(page);
		await page.mouse.move(cx, startY);
		await page.mouse.down();
		const dragSteps = 6;
		for (let i = 1; i <= dragSteps; i++) {
			await page.mouse.move(cx, startY + ((endY - startY) * i) / dragSteps);
			await page.waitForTimeout(20);
		}
		await page.mouse.up();

		const atRelease = await stripScroll(page);
		expect(atRelease.top).toBeGreaterThan(before.top); // the drag itself moved it

		await page.waitForTimeout(250);
		const midCoast = await stripScroll(page);
		expect(midCoast.top).toBeGreaterThan(atRelease.top); // momentum carried it on past the release point

		// The stop click: while it is still rolling, a click lands on whatever
		// chip is currently under the pointer and must not open it.
		await page.mouse.click(cx, box.y + box.height / 2);
		await expectNothingPlaying(page);

		// A plain click elsewhere still opens a take normally — the strip isn't
		// just inert.
		await page.getByRole('button', { name: labels[labels.length - 1], exact: true }).click();
		await expect(page.getByRole('contentinfo').getByText(labels[labels.length - 1])).toBeVisible();
		await expect(
			page
				.getByRole('contentinfo')
				.getByRole('button', { name: TRANSPORT_PAUSE_LABEL, exact: true })
		).toBeVisible();

		// The wheel: a plain vertical tick already scrolls a column natively —
		// nothing here should ever call preventDefault on it.
		const beforeWheel = await stripScroll(page);
		await page.mouse.move(cx, box.y + box.height / 2);
		await page.mouse.wheel(0, 150);
		await page.waitForTimeout(80);
		const afterWheel = await stripScroll(page);
		expect(afterWheel.top).not.toBe(beforeWheel.top);

		// Home/End and the vertical arrow keys.
		const firstChip = page.getByRole('button', { name: labels[0], exact: true });
		const lastChip = page.getByRole('button', { name: labels[labels.length - 1], exact: true });
		await firstChip.focus();
		await page.keyboard.press('End');
		await waitForStripScrollSettled(page);
		await expect(lastChip).toBeFocused();
		await page.keyboard.press('Home');
		await waitForStripScrollSettled(page);
		await expect(firstChip).toBeFocused();
		await page.keyboard.press('ArrowDown');
		await waitForStripScrollSettled(page);
		await expect(page.getByRole('button', { name: labels[1], exact: true })).toBeFocused();
		await page.keyboard.press('ArrowUp');
		await waitForStripScrollSettled(page);
		await expect(firstChip).toBeFocused();

		await context.close();
	});

	test('drags, coasts and stops on click without opening the take it lands on, in the row layout the strip renders in when its container narrows below the desktop floor', async ({
		browser,
		isMobile
	}) => {
		test.skip(Boolean(isMobile), 'this test opens its own narrow desktop-width context');
		const context = await browser.newContext({ viewport: ROW_LAYOUT_VIEWPORT });
		const page = await context.newPage();
		guard = new FlowGuard(page);
		const labels = takeLabelsNewestFirst();

		await openKineticStripSongCoWriter(page);
		const before = await stripScroll(page);
		expect(before.flexDirection).toBe('row'); // the axis this test actually proves against

		const box = await page.locator('.take-strip').boundingBox();
		if (!box) throw new Error('take strip did not render a box');
		const cy = box.y + box.height / 2;
		const startX = box.x + box.width * 0.85;
		const endX = box.x + box.width * 0.15;

		await expectNothingPlaying(page);
		await page.mouse.move(startX, cy);
		await page.mouse.down();
		const dragSteps = 6;
		for (let i = 1; i <= dragSteps; i++) {
			await page.mouse.move(startX + ((endX - startX) * i) / dragSteps, cy);
			await page.waitForTimeout(20);
		}
		await page.mouse.up();

		const atRelease = await stripScroll(page);
		expect(atRelease.left).toBeGreaterThan(before.left);

		await page.waitForTimeout(250);
		const midCoast = await stripScroll(page);
		expect(midCoast.left).toBeGreaterThan(atRelease.left);

		await page.mouse.click(box.x + box.width / 2, cy);
		await expectNothingPlaying(page);

		await page.getByRole('button', { name: labels[labels.length - 1], exact: true }).click();
		await expect(page.getByRole('contentinfo').getByText(labels[labels.length - 1])).toBeVisible();
		await expect(
			page
				.getByRole('contentinfo')
				.getByRole('button', { name: TRANSPORT_PAUSE_LABEL, exact: true })
		).toBeVisible();

		// The wheel: a row's own axis is horizontal, so a plain vertical tick
		// (deltaX 0) has to be converted rather than left to a browser that
		// would otherwise do nothing useful with it on this axis.
		const beforeWheel = await stripScroll(page);
		await page.mouse.move(box.x + box.width / 2, cy);
		await page.mouse.wheel(0, 150);
		await page.waitForTimeout(80);
		const afterWheel = await stripScroll(page);
		expect(afterWheel.left).not.toBe(beforeWheel.left);

		// Home/End and the horizontal arrow keys.
		const firstChip = page.getByRole('button', { name: labels[0], exact: true });
		const lastChip = page.getByRole('button', { name: labels[labels.length - 1], exact: true });
		await firstChip.focus();
		await page.keyboard.press('End');
		await waitForStripScrollSettled(page);
		await expect(lastChip).toBeFocused();
		await page.keyboard.press('Home');
		await waitForStripScrollSettled(page);
		await expect(firstChip).toBeFocused();
		await page.keyboard.press('ArrowRight');
		await waitForStripScrollSettled(page);
		await expect(page.getByRole('button', { name: labels[1], exact: true })).toBeFocused();
		await page.keyboard.press('ArrowLeft');
		await waitForStripScrollSettled(page);
		await expect(firstChip).toBeFocused();

		await context.close();
	});

	test('the strip does not render on the compact shell at phone width — the Takes tab carries takes there instead', async ({
		browser,
		isMobile
	}) => {
		test.skip(
			Boolean(isMobile),
			'this test opens its own mobile-emulated context regardless of project'
		);
		const context = await browser.newContext({
			viewport: MOBILE_VIEWPORT,
			isMobile: true,
			hasTouch: true
		});
		const page = await context.newPage();
		guard = new FlowGuard(page);
		const library = readSeededLibrary();

		await page.goto(`/album/${library.albumId}/${expectedSongSlug(library.pickedSongTitle)}`);
		await expect(page.getByRole('heading', { name: library.pickedSongTitle })).toBeVisible();

		await expect(page.locator('.take-strip')).toHaveCount(0);
		await expect(page.getByRole('tab', { name: /Takes/ })).toBeVisible();

		await context.close();
	});
});
