/* Remembers which tabs the editor opened to pick a photo in, and carries the
   clicked picture to the editor's server.

   A tab is "armed" when the editor opens it (the address carries a marker that
   content.js reads). Only armed tabs get the click-to-pick behaviour; every
   other tab is left alone. The picture is downloaded here, not in the page: a
   page may not read another site's pictures, an extension may. */

const MAX_BYTES = 16 * 1024 * 1024;
const keyOf = (tab) => `tab:${tab}`;

async function armed(tab) {
  const key = keyOf(tab);
  return (await chrome.storage.session.get(key))[key] || null;
}

function arm(tab, marker) {
  const wanted = marker || {};
  if (!/^https?:\/\/[^\s]+$/i.test(String(wanted.api || "")) || !wanted.task) {
    throw new Error("the editor's marker is not complete");
  }
  return chrome.storage.session.set({
    [keyOf(tab)]: {
      api: String(wanted.api).replace(/\/+$/, ""),
      task: String(wanted.task),
      session: String(wanted.session || ""),
      key: String(wanted.key || ""),
      min: Number(wanted.min) || 512,
    },
  });
}

async function pick(tab, chosen) {
  const to = await armed(tab);
  if (!to) throw new Error("this window was not opened by the editor");
  let blob;
  try {
    const got = await fetch(chosen.src);
    if (!got.ok) throw new Error(`its site answered ${got.status}`);
    blob = await got.blob();
  } catch (error) {
    throw new Error(`could not be downloaded (${error.message || error})`);
  }
  if (blob.size > MAX_BYTES) throw new Error("is too large a file");
  const body = new FormData();
  body.append("file", blob, "picked-photo");
  body.append("source", /^https?:/i.test(chosen.src) ? chosen.src : "");
  body.append("page", String(chosen.page || ""));
  body.append("session", to.session);
  let answer;
  try {
    answer = await fetch(
      `${to.api}/tasks/${encodeURIComponent(to.task)}/photos/picked`,
      { method: "POST", body, headers: to.key ? { "x-api-key": to.key } : {} });
  } catch (error) {
    throw new Error("could not reach the editor; is the app still running?");
  }
  let payload = null;
  try { payload = await answer.json(); } catch { /* an empty answer */ }
  if (!answer.ok) {
    throw new Error((payload && (payload.message || payload.detail))
      || `the editor answered ${answer.status}`);
  }
  // The picture is with the editor: this window has done its job.
  setTimeout(() => chrome.tabs.remove(tab).catch(() => {}), 700);
  return { ok: true };
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  const tab = sender.tab && sender.tab.id;
  if (tab == null || !message) return false;
  let work;
  if (message.type === "arm") work = Promise.resolve().then(() => arm(tab, message.marker)).then(() => armed(tab));
  else if (message.type === "state") work = armed(tab);
  else if (message.type === "pick") work = pick(tab, message.chosen || {});
  else return false;
  work.then(respond, (error) => respond({ error: String(error.message || error) }));
  return true;
});

// A source site opened from the armed window in a new tab is armed too.
chrome.tabs.onCreated.addListener(async (tab) => {
  if (tab.openerTabId == null) return;
  const from = await armed(tab.openerTabId);
  if (from) await chrome.storage.session.set({ [keyOf(tab.id)]: from });
});

chrome.tabs.onRemoved.addListener((tab) => {
  chrome.storage.session.remove(keyOf(tab));
});
