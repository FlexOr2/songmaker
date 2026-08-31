<script lang="ts">
	// The library's three addresses (`/`, `/album/<slug>`, `/album/<slug>/<song-slug>`)
	// are entrances to one workspace, not three of them (issue #276): each used to
	// mount LibraryWorkspace itself, so crossing between addresses swapped the leaf
	// `+page.svelte` and tore the workspace down with it. This group layout is what
	// stands across that crossing, so LibraryWorkspace mounts here, once, for as
	// long as the browser stays on any of the three. A leaf page still resolves its
	// own address and renders an overlay in `children` while it does — stacked over
	// the workspace rather than replacing it, so the workspace underneath never
	// unmounts for a resolution that fails or is still in flight.
	import LibraryWorkspace from '$lib/components/LibraryWorkspace.svelte';

	let { children } = $props();
</script>

<div class="library-stack">
	<LibraryWorkspace />
	{@render children()}
</div>

<style>
	.library-stack {
		position: relative;
		display: flex;
		flex: 1;
		flex-direction: column;
		min-width: 0;
		min-height: 0;
	}
</style>
