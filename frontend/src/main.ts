import { createApp } from "vue";
import App from "./App.vue";
import Join from "./Join.vue";
import "./style.css";
import "./theme.css";
createApp(location.pathname.replace(/\/$/, "") === "/join" ? Join : App).mount("#app");
