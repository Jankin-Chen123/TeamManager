import { createApp } from "vue";
import App from "./App.vue";
import Join from "./Join.vue";
import "./style.css";
import "./theme.css";
createApp(["/join", "/JoinTeam"].includes(location.pathname.replace(/\/$/, "")) ? Join : App).mount("#app");
