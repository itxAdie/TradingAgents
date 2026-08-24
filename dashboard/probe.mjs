import { chromium } from "playwright-core";
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage();
page.on("console", m => { if (m.type() === "error") console.log("CONSOLE:", m.text().slice(0,300)); });
page.on("pageerror", e => console.log("PAGEERROR:", String(e).slice(0,500)));
await page.goto("http://127.0.0.1:8931/", { waitUntil: "networkidle" });
console.log("TITLE:", await page.title());
console.log("BODY:", (await page.locator("body").innerText()).slice(0, 200));
await browser.close();
