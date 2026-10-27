<script setup lang="ts">
defineProps<{ name: string }>();
</script>

<template>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
    <g v-if="name === 'overview'">
      <rect x="3" y="3" width="7" height="10" rx="2" class="icon-tint" />
      <rect x="14" y="3" width="7" height="6" rx="2" />
      <rect x="3" y="17" width="7" height="4" rx="1.5" />
      <rect x="14" y="13" width="7" height="8" rx="2" class="icon-tint" />
    </g>
    <g v-else-if="name === 'spaces'">
      <circle cx="12" cy="7" r="3" class="icon-tint" />
      <path d="M6.5 20v-2a5.5 5.5 0 0 1 11 0v2M18 4.5a3 3 0 0 1 0 5.5M20.5 19v-1.5a5 5 0 0 0-2-4M6 4.5a3 3 0 0 0 0 5.5M3.5 19v-1.5a5 5 0 0 1 2-4" />
    </g>
    <g v-else-if="name === 'nodes'">
      <rect x="8" y="3" width="8" height="6" rx="2" class="icon-tint" />
      <path d="M12 9v4M5 16v-1a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v1" />
      <rect x="2" y="16" width="6" height="5" rx="1.5" />
      <rect x="16" y="16" width="6" height="5" rx="1.5" />
    </g>
    <g v-else-if="name === 'jobs'">
      <rect x="3" y="3" width="18" height="18" rx="4" />
      <path d="m7 8 1.2 1.2L10.5 7M13 8h4m-10 7 1.2 1.2 2.3-2.2M13 15h4" />
    </g>
    <g v-else-if="name === 'cdks'">
      <path class="icon-tint" d="M4 5h16a1 1 0 0 1 1 1v3a3 3 0 0 0 0 6v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-3a3 3 0 0 0 0-6V6a1 1 0 0 1 1-1Z" />
      <path d="M15 8v1m0 3v1m0 3v.2M7.5 12h3" />
    </g>
    <g v-else-if="name === 'audit'">
      <path d="M3.5 9a9 9 0 1 1 .4 7.1M3.5 4.5V9H8" />
      <path d="M12 7v5l3.5 2" />
    </g>
    <g v-else-if="name === 'settings'">
      <path d="M3 6h4m5 0h9M3 12h10m5 0h3M3 18h4m5 0h9" />
      <circle cx="9.5" cy="6" r="2.5" class="icon-tint" />
      <circle cx="15.5" cy="12" r="2.5" class="icon-tint" />
      <circle cx="9.5" cy="18" r="2.5" class="icon-tint" />
    </g>
  </svg>
</template>

<style scoped>
.icon-tint { fill: color-mix(in srgb, currentColor 10%, transparent); }
</style>
