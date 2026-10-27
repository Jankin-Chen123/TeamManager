// Keep the shared domain's existing /api and /assets owned by the other service.
const sharedDomain = /^\/(TeamManager|JoinTeam)(\/|$)/.test(location.pathname);
export const apiBase = sharedDomain ? "/TeamManager/api" : "/api";
export const joinUrl = sharedDomain ? "/JoinTeam" : "/join";
