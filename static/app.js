// 康怡养生馆 — 前端小脚本

// 发送验证码（登录页 / 预约查询页共用），dev_code 仅开发模式存在
function sendCode(shopId, btnId, phoneId, hintId) {
  const btn = document.getElementById(btnId);
  const phone = document.getElementById(phoneId).value.trim();
  if (!/^1\d{10}$/.test(phone)) { alert("请输入 11 位手机号"); return; }
  fetch(`/s/${shopId}/api/send_code`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone: phone }),
  }).then(r => r.json()).then(d => {
    if (!d.ok) { alert(d.msg); return; }
    if (d.dev_code) {
      const h = document.getElementById(hintId);
      if (h) h.textContent = "（开发模式）验证码：" + d.dev_code;
    }
    let n = 60;
    btn.disabled = true;
    btn.textContent = n + "s";
    const t = setInterval(() => {
      n--;
      if (n <= 0) {
        clearInterval(t); btn.disabled = false; btn.textContent = "获取验证码";
      } else btn.textContent = n + "s";
    }, 1000);
  });
}

// 预约页：日期最早今天 + 右侧信息卡联动
const bdate = document.getElementById("bdate");
if (bdate) bdate.min = new Date().toISOString().slice(0, 10);

const ssel = document.getElementById("service");
if (ssel) {
  const upd = () => {
    const o = ssel.selectedOptions[0];
    document.getElementById("sum-service").textContent = o.value
      ? o.text.split("（")[0] : "未选择";
    document.getElementById("sum-price").textContent = o.dataset.price
      ? "¥" + o.dataset.price : "—";
    document.getElementById("sum-dur").textContent = o.dataset.duration
      ? o.dataset.duration + " 分钟" : "—";
  };
  ssel.addEventListener("change", upd);
  upd();
}
const tech = document.getElementById("tech");
if (tech) {
  tech.addEventListener("change", () => {
    document.getElementById("sum-tech").textContent =
      tech.selectedOptions[0].value ? tech.selectedOptions[0].text.split(" · ")[0] : "由门店安排";
  });
}
