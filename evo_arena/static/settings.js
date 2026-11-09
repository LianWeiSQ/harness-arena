"use strict";
const $ = (id) => document.getElementById(id);
let profiles;
let saving = false;
function el(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}
async function api(path, payload) {
  const response = await fetch(
    path,
    payload
      ? {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-EvoArena-Request": "1",
          },
          body: JSON.stringify(payload),
        }
      : undefined,
  );
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "配置保存失败");
  return result;
}
function status(text, failed = false) {
  $("save-status").textContent = text;
  $("save-status").className = `save-status ${failed ? "error" : "pass"}`;
}
function edit(profile) {
  $("model-form").reset();
  $("profile-id").value = profile?.editable ? profile.id : "";
  $("form-title").textContent = profile?.editable
    ? "编辑模型连接"
    : "添加模型连接";
  $("profile-name").value = profile?.name || "";
  $("profile-model").value = profile?.model || "";
  $("profile-url").value = profile?.base_url || "";
  $("profile-key").required = !profile?.editable;
  $("profile-key").placeholder = profile?.editable
    ? "留空沿用已保存的密钥"
    : "输入此连接的 API Key";
  $("profile-default").checked = profile?.id === profiles?.default;
  status("");
}
function render() {
  $("profile-count").textContent = `${profiles.profiles.length} 个连接`;
  $("profiles").replaceChildren(
    ...profiles.profiles.map((profile) => {
      const item = el("article", null, "profile");
      const title = el("div", null, "profile-title");
      title.append(el("strong", profile.name));
      if (profile.id === profiles.default)
        title.append(el("span", "默认", "tag"));
      item.append(title, el("p", profile.model), el("p", profile.base_url));
      const actions = el("div", null, "profile-actions");
      actions.append(
        el(
          "span",
          profile.credential_ready ? "● 密钥已配置" : "缺少密钥",
          "pass",
        ),
      );
      if (profile.editable) {
        const change = el("button", "编辑");
        change.onclick = () => {
          edit(profile);
          $("profile-name").focus();
        };
        const remove = el("button", "删除");
        const cancel = el("button", "取消");
        cancel.hidden = true;
        cancel.onclick = () => {
          remove.textContent = "删除";
          cancel.hidden = true;
        };
        remove.onclick = async () => {
          if (cancel.hidden) {
            remove.textContent = "确认删除";
            cancel.hidden = false;
            return;
          }
          remove.disabled = true;
          try {
            profiles = await api("/api/models/delete", { id: profile.id });
            if ($("profile-id").value === profile.id) edit(null);
            render();
            status("模型连接已删除。");
          } catch (error) {
            status(error.message, true);
            remove.disabled = false;
          }
        };
        actions.append(change, remove, cancel);
      } else actions.append(el("span", "由启动参数管理", "muted"));
      item.append(actions);
      return item;
    }),
  );
  if (!profiles.profiles.length)
    $("profiles").append(
      el("p", "添加第一个模型连接，就可以开始对局。", "empty"),
    );
}
$("new-profile").onclick = () => {
  edit(null);
  $("profile-name").focus();
};
$("model-form").onsubmit = async (event) => {
  event.preventDefault();
  if (saving) return;
  saving = true;
  $("save-model").disabled = true;
  try {
    const result = await api("/api/models", {
      id: $("profile-id").value,
      name: $("profile-name").value,
      model: $("profile-model").value,
      base_url: $("profile-url").value,
      api_key: $("profile-key").value,
      default: $("profile-default").checked,
    });
    profiles = result;
    edit(profiles.profiles.find((profile) => profile.id === result.id));
    render();
    status("连接已保存，可以回到竞技场选择此模型。");
  } catch (error) {
    status(error.message, true);
  } finally {
    saving = false;
    $("save-model").disabled = false;
  }
};
try {
  document.documentElement.dataset.theme =
    localStorage.getItem("arena-theme") || "light";
} catch {}
$("theme-toggle").onclick = () => {
  const theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("arena-theme", theme);
  } catch {}
};
api("/api/models")
  .then((data) => {
    profiles = data;
    render();
  })
  .catch((error) => status(error.message, true));
